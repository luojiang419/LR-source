"""两端独立数据库、真实 HTTP、原始 JPG/XMP 与持久化回看集成验证。"""
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.error import HTTPError

from PIL import Image
from asset_archive import AssetArchive
from catalog_store import Store,stable_json
from lan_capture_sync import LanCaptureSync
from lan_sync_data import mapped_id,safe_path
import media_engine as media
from model_providers import Providers
from sku_service import SkuService
from test_sku_workflow import fixture_book,CODE

ROOT=Path(__file__).resolve().parents[1]


class Node:
    def __init__(self,base,name):
        self.base=base;self.photos=base/'photos';self.photos.mkdir(parents=True)
        self.store=Store(base/'data',ROOT);self.archive=AssetArchive(self.store,autostart=False)
        self.sku=SkuService(self.store,Providers(self.store.data),archive=self.archive,autostart=False)
        self.archive.sku=self.sku
        self.sync=LanCaptureSync(self.store,self.sku,self.archive,autostart=False,host_state=lambda:{'watch':{'enabled':True}})
        self.sync.configure({'enabled':True,'name':name,'password':'同步 001Ab!','folder':str(base/'received')})

    def photo(self,name='one.JPG',color='steelblue',stamp='2026-11-08T14:20:00.123'):
        p=self.photos/name;Image.new('RGB',(100,80),color).save(p)
        sid=self.store.ensure_live_session(self.photos)
        r=media.capture_record(self.photos,p,None,{})
        r.update(time=stamp,missingTime=False)
        self.store.append_live(sid,[r]);self.store.activate(sid)
        return sid,r

    def close(self):self.sync.close();self.sku.close();self.archive.close()


class LanCaptureSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.host=Node(self.base/'host','现场 A');self.receiver=Node(self.base/'receiver','办公室')

    def tearDown(self):
        self.receiver.close();self.host.close();self.temp.cleanup()

    def peer(self):
        peer={'device':self.host.sync.config['device'],'name':'现场 A','address':f'http://127.0.0.1:{self.host.sync.server.server_port}','receiving':False,'seen':time.time()}
        self.receiver.sync.peers[peer['device']]=peer
        return peer

    def connect(self):
        peer=self.peer();self.receiver.sync.connect_peer({'device':peer['device'],'password':'同步 001Ab!'})
        return peer

    def test_discovery_wrong_code_and_separate_listener(self):
        found=self.receiver.sync.discover(timeout=.5)
        self.assertIn(self.host.sync.config['device'],[p['device'] for p in found['peers']])
        peer=self.peer()
        with self.assertRaisesRegex(ValueError,'同步码错误'):self.receiver.sync.connect_peer({'device':peer['device'],'password':'wrong'})
        self.assertFalse(self.receiver.sync.receiving)
        with self.assertRaises(HTTPError) as error:self.receiver.sync.request_json(peer['address'],'/sync/manifest')
        self.assertEqual(error.exception.code,401)
        self.connect()
        for path in ('/api/sku','/api/watch/start','/../api/health'):
            with self.assertRaises(HTTPError) as error:self.receiver.sync.request_json(peer['address'],path,token=self.receiver.sync.token)
            self.assertEqual(error.exception.code,404)
        self.assertNotIn('key',stable_json(self.receiver.sync.state()))

    def test_full_incremental_xmp_progress_library_and_offline_reopen(self):
        sid,record=self.host.photo();cid=record['id']
        side=(self.host.photos/'one.xmp');side.write_text('<xmp>initial</xmp>')
        project=self.host.sku.import_file('计划.xlsx',fixture_book());pid=project['project']['id']
        self.host.sku.configure(pid,sid,'模特图',True)
        aid=self.host.archive.ensure(sid,cid)
        with self.host.archive.connect() as c:c.execute("UPDATE assets SET sku=?,normalized=?,scope='模特图',project=? WHERE id=?",(CODE,CODE,pid,aid))
        with self.host.sku.connect() as c:
            c.execute("UPDATE sku_rows SET status='已拍摄',baseline='已拍摄',completed_at='2026-11-08T14:20:00.123' WHERE project=? AND sku=?",(pid,CODE))
        self.connect();self.receiver.sync.sync_once()
        state=self.receiver.sync.state();self.assertEqual(state['phase'],'following')
        local_sid=mapped_id(self.host.sync.config['device'],sid);local_pid=mapped_id(self.host.sync.config['device'],pid)
        session=self.receiver.store.session(local_sid);records=self.receiver.store.records(local_sid)
        local_file=Path(session['root'])/records[0]['jpg']
        self.assertTrue(local_file.is_file());self.assertIn('2026-11-08',local_file.parts)
        self.assertEqual(local_file.read_bytes(),(self.host.photos/'one.JPG').read_bytes())
        self.assertEqual(local_file.with_suffix('.xmp').read_text(),side.read_text())
        self.assertEqual(self.receiver.archive.asset(aid)['files'][0]['path'],str(local_file))
        self.assertTrue((self.receiver.sku.root/local_pid/'source.xlsx').is_file())
        self.assertTrue(any(r['status']=='已拍摄' for r in self.receiver.sku.rows(local_pid)))
        self.assertEqual(self.receiver.sku.project(local_pid)['enabled'],0)
        with self.receiver.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shooting_bindings').fetchone()[0],0)
        first_count=len(records);revision=state['revision']
        self.receiver.sync.sync_once();self.assertEqual(self.receiver.sync.state()['revision'],revision)
        self.assertEqual(len(self.receiver.store.records(local_sid)),first_count)
        # 仅 XMP 外部更新也必须发现，不依赖主库变化。
        side.write_text('<xmp>updated rating 4</xmp>');self.receiver.sync.sync_once()
        self.assertEqual(local_file.with_suffix('.xmp').read_text(),side.read_text())
        fingerprint=self.receiver.store.records(local_sid)[0]['fingerprint']
        Image.new('RGB',(100,80),'green').save(self.host.photos/'one.JPG');self.receiver.sync.sync_once()
        self.assertEqual(local_file.read_bytes(),(self.host.photos/'one.JPG').read_bytes())
        self.assertNotEqual(self.receiver.store.records(local_sid)[0]['fingerprint'],fingerprint)
        # 新照片、业务状态与评级实时跟进。
        self.host.photo('two.JPG','tomato','2026-11-09T09:00:00.002')
        with self.host.store.connect() as c:
            session=self.host.store.session(sid);session['state']['overrides']={cid:4}
            c.execute('UPDATE sessions SET state=?,revision=revision+1 WHERE id=?',(stable_json(session['state']),sid))
        self.receiver.sync.sync_once()
        self.assertEqual(len(self.receiver.store.records(local_sid)),2)
        self.assertEqual(self.receiver.store.session(local_sid)['state']['overrides'][mapped_id(self.host.sync.config['device'],cid)],4)
        self.receiver.sync.disconnect();self.host.sync.configure({'enabled':False})
        self.assertTrue(local_file.is_file());self.assertEqual(len(self.receiver.store.records(local_sid)),2)
        reopened=Store(self.receiver.store.data,ROOT)
        self.assertIn(local_sid,[r['id'] for r in reopened.sessions()])
        self.receiver.sku.activate(local_pid)
        self.assertEqual(self.receiver.sku.state()['project']['id'],local_pid)
        # 退出后本地保存视图会重建 state，持久来源标记仍须阻止再次转发。
        local_session=self.receiver.store.session(local_sid);state=dict(local_session['state']);state.pop('sync_origin',None)
        self.receiver.store.save(local_sid,local_session['revision'],state)
        self.assertEqual(self.receiver.sync.manifest()['tables']['catalog']['sessions'],[])

    def test_path_and_protocol_boundaries(self):
        for p in ('../escape','/absolute','C:/outside','x/../../outside','x\\bad','x:stream','CON.jpg','A/COM1.xmp'):
            with self.assertRaises(ValueError):safe_path(self.base/'receiver',p)
        with self.assertRaises(ValueError):self.receiver.sync.configure({'folder':str(ROOT/'选片助手'/'received')})
        with self.assertRaises(ValueError):self.receiver.sync.request_json('http://8.8.8.8:80','/sync/challenge')
        sid,_=self.host.photo();self.connect();self.receiver.sync.sync_once()
        with self.assertRaises(ValueError):self.receiver.sync.configure({'folder':str(self.base/'other')})
        snapshot=self.host.sync.manifest();malformed=json.loads(stable_json(snapshot));malformed['name']='tampered'
        with self.assertRaisesRegex(ValueError,'校验失败'):self.receiver.sync.data.validate(malformed,self.host.sync.config['device'])

    def test_either_side_can_disconnect_without_deleting_data_or_disabling_discovery(self):
        sid,_=self.host.photo();peer=self.connect();self.receiver.sync.sync_once()
        local_sid=mapped_id(peer['device'],sid);session=self.receiver.store.session(local_sid)
        photo=Path(session['root'])/self.receiver.store.records(local_sid)[0]['jpg'];before=photo.read_bytes()
        self.assertEqual(self.host.sync.state()['sending_count'],1)
        self.assertEqual(self.host.sync.state()['connected_clients'][0]['name'],'办公室')
        token=self.receiver.sync.token;self.host.sync.disconnect()
        with self.assertRaises(HTTPError) as error:self.receiver.sync.request_json(peer['address'],'/sync/revision',token=token)
        self.assertEqual(error.exception.code,410)
        self.receiver.sync.sync_once()
        self.assertFalse(self.receiver.sync.receiving)
        self.assertIsNone(self.receiver.sync.config['remote'])
        self.assertIn('已断开同步',self.receiver.sync.state()['error'])
        self.assertEqual(photo.read_bytes(),before)
        self.assertTrue(self.host.sync.state()['enabled']);self.assertTrue(self.receiver.sync.state()['enabled'])
        self.connect();self.receiver.sync.sync_once();self.assertEqual(self.host.sync.state()['sending_count'],1)
        self.receiver.sync.disconnect();self.assertEqual(self.host.sync.state()['sending_count'],0)
        self.assertEqual(photo.read_bytes(),before)
        self.assertIn(self.receiver.sync.config['device'],[p['device'] for p in self.host.sync.discover(timeout=.5)['peers']])

    def test_new_sidecar_restart_reconnect_and_independent_sources(self):
        sid,r=self.host.photo();self.connect();self.receiver.sync.sync_once()
        local_sid=mapped_id(self.host.sync.config['device'],sid)
        root=Path(self.receiver.store.session(local_sid)['root']);local=root/self.receiver.store.records(local_sid)[0]['jpg']
        (self.host.photos/'one.xmp').write_text('<xmp>new sidecar</xmp>')
        self.receiver.sync.sync_once();self.assertEqual(local.with_suffix('.xmp').read_text(),'<xmp>new sidecar</xmp>')
        self.receiver.sync.close()
        self.receiver.sync=LanCaptureSync(self.receiver.store,self.receiver.sku,self.receiver.archive,autostart=False)
        self.assertTrue(self.receiver.sync.receiving)
        self.receiver.sync.sync_once();self.assertEqual(self.receiver.sync.state()['session_id'],local_sid)
        self.receiver.sync.disconnect()
        other=Node(self.base/'second-host','另一现场');self.addCleanup(other.close)
        other_sid,_=other.photo(color='red')
        peer={'device':other.sync.config['device'],'name':'另一现场','address':f'http://127.0.0.1:{other.sync.server.server_port}','receiving':False,'seen':time.time()}
        self.receiver.sync.peers[peer['device']]=peer
        self.receiver.sync.connect_peer({'device':peer['device'],'password':'同步 001Ab!'})
        self.receiver.sync.sync_once()
        other_local=self.receiver.store.session(mapped_id(peer['device'],other_sid))
        self.assertNotEqual(other_local['root'],str(root));self.assertTrue(local.is_file())

    def test_received_confirmation_can_browse_export_and_prepare_archive_offline(self):
        from test_ocr_metadata import OcrMetadataTests
        from sku_library import browse,labels
        from asset_catalog import AssetCatalog
        from folder_archive_sources import FolderArchiveSources
        fixture=OcrMetadataTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        # 给合成样片写入真实可读的相机身份，归档准备不能靠文件名猜配。
        for index,record in enumerate(fixture.store.records(fixture.sid)):
            path=fixture.photos/record['jpg']
            fixture.archive.writer.run('-overwrite_original','-Model=Canon EOS R6m2','-ExifIFD:SerialNumber=123456',
                f'-DateTimeOriginal=2026:10:21 10:00:0{index}','-SubSecTimeOriginal=001',f'-ExifIFD:ImageNumber={index+1}',path)
            fresh=media.capture_record(fixture.photos,path,None,{'DateTimeOriginal':f'2026:10:21 10:00:0{index}'})
            with fixture.store.connect() as c:c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(stable_json(fresh),fixture.sid,record['id']))
        fixture.parse();fixture.choose()
        while fixture.archive.process_one():pass
        fixture_sync=LanCaptureSync(fixture.store,fixture.sku,fixture.archive,autostart=False)
        self.addCleanup(fixture_sync.close);fixture_sync.configure({'enabled':True,'password':'001abc','name':'实拍确认'})
        peer={'device':fixture_sync.config['device'],'name':'实拍确认','address':f'http://127.0.0.1:{fixture_sync.server.server_port}','receiving':False,'seen':time.time()}
        self.receiver.sync.peers[peer['device']]=peer;self.receiver.sync.connect_peer({'device':peer['device'],'password':'001abc'})
        self.receiver.sync.sync_once();self.receiver.sync.disconnect();fixture_sync.configure({'enabled':False})
        local_sid=mapped_id(peer['device'],fixture.sid);local_pid=mapped_id(peer['device'],fixture.pid)
        result=browse(self.receiver.sku)
        self.assertEqual(result['groups'][0]['sku'],CODE)
        self.assertGreater(browse(self.receiver.sku,search='COTTON')['total'],0)
        self.assertTrue(all(Path(row['path']).is_file() for row in result['rows']))
        self.assertEqual(labels(self.receiver.sku,local_sid)['labels'][mapped_id(peer['device'],fixture.ids[0])],CODE)
        self.receiver.sku.activate(local_pid);row=next(r for r in self.receiver.sku.state()['rows'] if r['sku']==CODE and r['sheet']=='模特图')
        self.assertEqual(row['status'],'已拍摄');self.assertIsNotNone(row['sku_image']);self.assertIsNotNone(row['label_image'])
        self.receiver.store.activate(local_sid)
        from unittest.mock import patch
        from metadata_backfill import enqueue_missing
        with patch.object(self.receiver.archive,'repair_segment_jobs',side_effect=AssertionError('接收资料不能重建文件写入')):
            self.receiver.sku.tick()
        self.assertEqual(enqueue_missing(self.receiver.archive),0)
        with self.receiver.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM metadata_jobs').fetchone()[0],0)
        output=self.receiver.sku.export(local_pid);self.assertTrue(output.is_file())
        catalog=AssetCatalog(self.receiver.store.data);self.addCleanup(catalog.close)
        sources=FolderArchiveSources(self.receiver.store,self.receiver.archive,catalog)
        root=Path(self.receiver.store.session(local_sid)['root']);db=self.base/'archive-input.sqlite3'
        sources.snapshot(root,db,allow_unassigned=True)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(db)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM captures WHERE sku=?',(CODE,)).fetchone()[0],2)
        catalog.close()

    def test_verified_pending_projection_recovers_without_online_host(self):
        from unittest.mock import patch
        sid,r=self.host.photo();self.connect()
        with patch.object(self.receiver.sync.data,'apply',side_effect=RuntimeError('simulated power loss')):
            with self.assertRaisesRegex(RuntimeError,'power loss'):self.receiver.sync.sync_once()
        self.assertIsNone(self.receiver.store.session(mapped_id(self.host.sync.config['device'],sid)))
        self.host.sync.configure({'enabled':False})
        # 模拟重启后的首轮：无需主机在线即可完成已经校验到盘的待发布快照。
        with self.assertRaises(OSError):self.receiver.sync.sync_once()
        session=self.receiver.store.session(mapped_id(self.host.sync.config['device'],sid));self.assertIsNotNone(session)
        self.assertTrue((Path(session['root'])/self.receiver.store.records(session['id'])[0]['jpg']).is_file())


class LanCaptureSyncBrowserTests(unittest.TestCase):
    setUp=LanCaptureSyncTests.setUp
    tearDown=LanCaptureSyncTests.tearDown

    def test_settings_discovery_connect_live_readonly_and_exit(self):
        from http.server import ThreadingHTTPServer
        import threading
        from unittest.mock import patch
        from playwright.sync_api import sync_playwright
        import server
        sid,r=self.host.photo()
        p=self.host.sku.import_file('计划.xlsx',fixture_book())['project']['id'];self.host.sku.configure(p,sid,'模特图',True)
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        globals={'STORE':self.receiver.store,'DATA':self.receiver.store.data,'MODELS':self.receiver.sku.providers,'ARCHIVE':self.receiver.archive,'SKU':self.receiver.sku,'CAPTURESYNC':self.receiver.sync,
                 'WATCH':None,'MOUSE':None,'CANON':None,'ANALYSIS':None,'RAWLINK':None,'FILESEARCH':None,'PREVIEWS':None,'INVENTORY':None,'REFERENCES':None,'STORAGE':None,'CATALOG':None}
        patches=[patch.object(server,k,v) for k,v in globals.items()]
        for p in patches:p.start();self.addCleanup(p.stop)
        threading.Thread(target=http.serve_forever,daemon=True).start();self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
        self.receiver.sync.thread.start()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{http.server_port}');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
            page.locator('#app-settings-open').click();page.locator('[data-settings-section="sync"]').click()
            self.assertTrue(page.locator('#lan-sync-clients').is_visible())
            self.assertTrue(page.locator('#lan-sync-exit').is_visible())
            self.assertEqual(page.locator('#lan-sync-exit').inner_text(),'断开同步')
            self.assertTrue(page.locator('#lan-sync-exit').is_disabled())
            self.assertEqual(page.locator('#lan-sync-controls > .lan-sync-card').count(),3)
            self.assertEqual(page.locator('#lan-sync-peers-dialog,#lan-sync-discover').count(),0)
            peer=page.locator('.lan-sync-peer').filter(has_text='现场 A');peer.wait_for()
            self.assertGreater(page.locator('#lan-sync-clients').bounding_box()['x'],page.locator('.lan-sync-machine').bounding_box()['x'])
            discovery_requests=[];page.on('request',lambda r:discovery_requests.append(r.url) if r.url.endswith('/api/lan-sync/discover') else None)
            peer.focus()
            with page.expect_response(lambda r:r.url.endswith('/api/lan-sync/discover')):
                page.locator('#lan-sync-refresh').evaluate('(button)=>button.click()')
            page.wait_for_function('()=>!document.getElementById("lan-sync-refresh").disabled')
            self.assertTrue(peer.evaluate('(button)=>button===document.activeElement'))
            page.locator('[data-settings-section="models"]').click();before=len(discovery_requests)
            page.wait_for_timeout(8500);self.assertEqual(len(discovery_requests),before)
            page.locator('[data-settings-section="sync"]').click()
            with page.expect_response(lambda r:r.url.endswith('/api/lan-sync/discover')):pass
            page.wait_for_function('()=>!document.getElementById("lan-sync-refresh").disabled')
            peer.click(button='right');page.locator('#lan-sync-peer-connect').click()
            self.assertTrue(page.locator('#lan-sync-login').is_visible())
            page.locator('#lan-sync-login [data-login-cancel]').first.click()
            page.locator('#lan-sync-name').fill('办公室 B');page.locator('#lan-sync-password').fill('001aB 中文')
            page.locator('#lan-sync-save').click();page.wait_for_function('()=>document.getElementById("lan-sync-status").textContent==="已保存"')
            self.assertEqual(self.receiver.sync.state()['name'],'办公室 B')
            page.locator('#lan-sync-enabled').uncheck();page.wait_for_function('()=>document.getElementById("lan-sync-peers-status").textContent==="同步已关闭"')
            self.assertEqual(page.locator('.lan-sync-peer').count(),0)
            self.assertTrue(page.locator('#lan-sync-refresh').is_disabled())
            page.locator('#lan-sync-enabled').check();peer.wait_for()
            page.locator('.lan-sync-peer').filter(has_text='现场 A').dblclick();page.locator('#lan-sync-code').fill('wrong');page.locator('#lan-sync-login-submit').click()
            page.wait_for_function('()=>document.getElementById("lan-sync-login-status").textContent.includes("同步码错误")')
            page.locator('#lan-sync-code').fill('同步 001Ab!');page.locator('#lan-sync-login-submit').click()
            page.wait_for_function('()=>window.FramekeepLanSync?.receiving()&&window.FRAMEKEEP_SESSION.id',timeout=20000)
            page.wait_for_selector('#lan-sync-badge:visible')
            # 写接口由引擎拒绝；不能依靠按钮禁用保证单向接收。
            code=page.evaluate("""async()=>{const r=await fetch('/api/sku/full-auto',{method:'POST',headers:{'Content-Type':'application/json','X-Framekeep-Token':FRAMEKEEP_SESSION.requestToken},body:JSON.stringify({enabled:true})});return r.status}""")
            self.assertEqual(code,423)
            self.host.photo('new.JPG','lime','2026-11-09T12:20:00.000')
            page.wait_for_function('()=>window.DEMO_PHOTOS.length===2',timeout=20000)
            page.locator('#app-settings-open').click();page.locator('[data-settings-section="sync"]').click()
            self.assertTrue(page.locator('#lan-sync-exit').is_enabled())
            self.assertTrue(page.locator('#lan-sync-connection-disconnect').is_visible())
            self.assertIn('现场 A',page.locator('#lan-sync-connection-name').inner_text())
            shots=ROOT/'build/lan-sync-validation';shots.mkdir(parents=True,exist_ok=True)
            for theme in ('dark','light','pure-white','hardcore'):
                page.evaluate('(v)=>FramekeepTheme.set(v)',theme)
                page.screenshot(path=str(shots/(theme+'.png')))
            for width in (768,390):
                page.set_viewport_size({'width':width,'height':900})
                self.assertTrue(page.locator('#lan-sync-controls').is_visible())
                self.assertLessEqual(page.locator('#lan-sync-controls').bounding_box()['width'],width)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),width)
                self.assertGreaterEqual(page.locator('#lan-sync-clients').bounding_box()['y'],page.locator('.lan-sync-storage').bounding_box()['y'])
            page.set_viewport_size({'width':1440,'height':1000})
            page.locator('#lan-sync-connection-disconnect').click();page.wait_for_function('()=>!window.FramekeepLanSync.receiving()')
            self.assertTrue(page.locator('#lan-sync-exit').is_disabled())
            self.assertEqual(len(self.receiver.store.sessions()),1)
            self.assertEqual(len(self.receiver.store.records(self.receiver.sync.state()['session_id'])),2)
            # 发送端也显示联机对象并能主动断开，使用真实同步服务状态和断开逻辑。
            self.receiver.sync.connect_peer({'device':self.host.sync.config['device'],'password':'同步 001Ab!'})
            page.route('**/api/lan-sync',lambda route:route.fulfill(content_type='application/json',body=stable_json(self.host.sync.state())))
            page.route('**/api/lan-sync/disconnect',lambda route:route.fulfill(content_type='application/json',body=stable_json(self.host.sync.disconnect())))
            page.locator('[data-settings-section="sync"]').click()
            page.wait_for_function('()=>document.getElementById("lan-sync-connection-state").textContent.includes("正在发送")')
            self.assertTrue(page.locator('#lan-sync-exit').is_enabled())
            page.screenshot(path=str(shots/'disconnect-sender.png'))
            page.locator('#lan-sync-exit').click();page.wait_for_function('()=>document.getElementById("lan-sync-exit").disabled')
            for _ in range(100):
                if not self.receiver.sync.receiving:break
                time.sleep(.05)
            self.assertFalse(self.receiver.sync.receiving)
            self.assertTrue(self.host.sync.config['enabled'])
            self.assertEqual(errors,[])
            browser.close()


if __name__=='__main__':unittest.main()
