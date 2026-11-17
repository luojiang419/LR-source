"""一主多客户端：独立数据库、真实HTTP并发、独立浏览与断开隔离。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from catalog_store import stable_json
from lan_sync_data import mapped_id
import test_lan_capture_sync as fixtures

ROOT=Path(__file__).resolve().parents[1]


class LanMultiClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.host=fixtures.Node(self.base/'host','主机 A');self.addCleanup(self.host.close)

    def client(self,index):
        client=fixtures.Node(self.base/f'client-{index}',f'客户端 {index}');self.addCleanup(client.close)
        client.sync.peers[self.host.sync.config['device']]={'device':self.host.sync.config['device'],'name':'主机 A','address':f'http://127.0.0.1:{self.host.sync.server.server_port}','receiving':False,'seen':time.time()}
        return client

    def connect(self,client):return client.sync.connect_peer({'device':self.host.sync.config['device'],'password':'同步 001Ab!'})

    def parallel(self,clients,operation):
        with ThreadPoolExecutor(max_workers=len(clients)) as pool:return list(pool.map(operation,clients))

    def test_twelve_clients_incremental_readonly_and_individual_disconnect(self):
        sid,first=self.host.photo('001.JPG');self.host.photo('002.JPG','red');self.host.photo('003.JPG','green')
        clients=[self.client(i) for i in range(12)]
        self.parallel(clients,self.connect)
        self.assertEqual(self.host.sync.state()['sending_count'],12)
        self.parallel(clients,lambda c:c.sync.sync_once())
        device=self.host.sync.config['device'];local_sid=mapped_id(device,sid)
        for client in clients:
            self.assertEqual(len(client.store.records(local_sid)),3)
            self.assertTrue(client.sync.receiving)
            for record in client.store.records(local_sid):
                path=Path(client.store.session(local_sid)['root'])/record['jpg']
                self.assertEqual(path.read_bytes(),(self.host.photos/Path(record['jpg']).name).read_bytes())
        # 每台保留自己的布局和选中项，主机后续更新不能覆盖。
        local_views=[]
        for index,client in enumerate(clients):
            state={'view':'list' if index%2 else 'grid','selectedId':client.store.records(local_sid)[index%3]['id']};local_views.append(state)
            current=client.store.session(local_sid)
            client.store.save(local_sid,current['revision'],{**current['state'],**state})
        with self.host.store.connect() as db:
            db.execute('UPDATE sessions SET state=?,revision=revision+1 WHERE id=?',(stable_json({'overrides':{first['id']:4},'selectedId':'主机选中位置','view':'single'}),sid))
        with patch('lan_capture_sync.digest',side_effect=AssertionError('已校验小档不应重新计算散列')):
            self.parallel(clients,lambda c:c.sync.sync_once())
        for index,client in enumerate(clients):
            state=client.store.session(local_sid)['state']
            self.assertEqual({k:state[k] for k in local_views[index]},local_views[index]);self.assertEqual(state['overrides'][mapped_id(device,first['id'])],4)
        (self.host.photos/'001.xmp').write_text('<xmp>rating 4</xmp>')
        self.host.photo('004.JPG','purple')
        self.parallel(clients,lambda c:c.sync.sync_once())
        for client in clients:
            self.assertEqual(len(client.store.records(local_sid)),4)
            path=next(Path(client.store.session(local_sid)['root']).rglob('001.xmp'));self.assertEqual(path.read_text(),'<xmp>rating 4</xmp>')
        # 主机逐台踢出；旧令牌收到410，仅被踢的接收端停止重连。
        kicked=clients[0];old=kicked.sync.token;self.host.sync.disconnect_client(kicked.sync.config['device'])
        self.assertEqual(self.host.sync.state()['sending_count'],11)
        kicked.sync.sync_once();self.assertFalse(kicked.sync.receiving)
        self.parallel(clients[1:],lambda c:c.sync.sync_once())
        self.assertTrue(all(c.sync.receiving for c in clients[1:]))
        self.assertEqual(len(kicked.store.records(local_sid)),4)
        # 同设备重新鉴权替换令牌，不增加其它客户端或留下重复会话。
        self.connect(kicked);kicked.sync.sync_once();self.assertEqual(self.host.sync.state()['sending_count'],12)
        token,key,salt=kicked.sync.authenticate(kicked.sync.config['remote'])
        self.assertEqual(self.host.sync.state()['sending_count'],12);self.assertNotEqual(token,old)
        kicked.sync.token=token
        clients[1].sync.disconnect();self.assertEqual(self.host.sync.state()['sending_count'],11)
        self.assertTrue(all(c.sync.receiving for c in clients[2:]))
        result={'clients':12,'initial_captures':3,'final_captures':4,'verified_file_rehashes_on_rating_update':0,'individual_disconnect_preserved_others':True,'client_views_preserved':12,'bytes_equal':True}
        output=ROOT/'build/lan-multi-client';output.mkdir(parents=True,exist_ok=True);(output/'multi-client.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')

    def test_shared_polling_snapshot_pins_and_token_replacement(self):
        self.host.photo();client=self.client(1);self.connect(client)
        address=client.sync.config['remote']['address'];token=client.sync.token
        first=self.host.sync.manifest();old=first['revision'];sha=first['tables']['catalog']['captures'][0]['sync_files'][0]['sha256']
        with patch.object(self.host.sync.data,'revision',wraps=self.host.sync.data.revision) as checked:
            with ThreadPoolExecutor(max_workers=12) as pool:
                revisions=list(pool.map(lambda _:client.sync.request_json(address,'/sync/revision',token=token)['revision'],range(12)))
            self.assertEqual(set(revisions),{old});self.assertEqual(checked.call_count,0,'同一轮轮询复用主机快照，无需按客户端扫描目录')
        self.host.photo('new.JPG','orange');latest=self.host.sync.manifest()
        self.assertNotEqual(latest['revision'],old)
        pinned=client.sync.request_json(address,'/sync/manifest?revision='+old,token=token);self.assertEqual(pinned['revision'],old)
        with client.sync.request(address,'/sync/blob/'+sha,token=token) as response:self.assertEqual(hashlib.sha256(response.read()).hexdigest(),sha)
        replacement,_,_=client.sync.authenticate(client.sync.config['remote'])
        with self.assertRaises(HTTPError) as error:client.sync.request_json(address,'/sync/revision',token=token)
        self.assertEqual(error.exception.code,410);client.sync.token=replacement
        self.assertEqual(self.host.sync.state()['sending_count'],1)
        # 上游断线不导致其它连接被撤销，已校验本地资料保留并可重连。
        client.sync.sync_once();count=len(client.store.records())
        with patch.object(client.sync,'request_json',side_effect=OSError('临时网络断开')):
            with self.assertRaises(OSError):client.sync.sync_once()
        self.assertTrue(client.sync.receiving);client.sync.sync_once();self.assertEqual(len(client.store.records()),count)

    def test_failed_password_limit_remains_and_serving_cannot_receive_another_host(self):
        self.host.photo();client=self.client(1);self.connect(client)
        with self.assertRaisesRegex(ValueError,'正在向客户端发送'):self.host.sync.connect_peer({'device':client.sync.config['device'],'password':'同步 001Ab!'})
        address=client.sync.config['remote']['address']
        for _ in range(10):
            challenge=client.sync.request_json(address,'/sync/challenge')
            with self.assertRaises(HTTPError) as error:client.sync.request_json(address,'/sync/login',{'nonce':challenge['nonce'],'proof':'wrong'})
            self.assertEqual(error.exception.code,403)
        challenge=client.sync.request_json(address,'/sync/challenge')
        with self.assertRaises(HTTPError) as error:client.sync.request_json(address,'/sync/login',{'nonce':challenge['nonce'],'proof':'wrong'})
        self.assertEqual(error.exception.code,429)
        # 已认证的其它客户端轮询继续工作。
        self.assertTrue(client.sync.request_json(address,'/sync/revision',token=client.sync.token)['revision'])

    def test_file_change_during_snapshot_is_checked_again(self):
        self.host.photo();side=self.host.photos/'one.xmp';side.write_text('old')
        self.host.sync.manifest();original=self.host.sync.data.snapshot
        def changed(*args):
            payload,blobs=original(*args);side.write_text('new xmp data');return payload,blobs
        self.host.sync.cached_signature=None
        with patch.object(self.host.sync.data,'snapshot',side_effect=changed):stale=self.host.sync.manifest()
        current=self.host.sync.manifest()
        self.assertNotEqual(current['revision'],stale['revision'])
        files=current['tables']['catalog']['captures'][0]['sync_files']
        self.assertEqual(next(f for f in files if f['kind']=='xmp')['sha256'],hashlib.sha256(b'new xmp data').hexdigest())


class LanMultiClientBrowserTests(unittest.TestCase):
    def test_sync_preserves_browsing_and_host_can_disconnect_one_client(self):
        from contextlib import ExitStack
        from datetime import datetime,timedelta
        from http.server import ThreadingHTTPServer
        from PIL import Image
        from playwright.sync_api import sync_playwright
        import media_engine as media
        import server
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            base=Path(folder);host=fixtures.Node(base/'host','主机 A');b=fixtures.Node(base/'B','客户端 B');c=fixtures.Node(base/'C','客户端 C')
            for node in (host,b,c):stack.callback(node.close)
            sid=host.store.ensure_live_session(host.photos);host.store.activate(sid);records=[]
            for index in range(110):
                path=host.photos/f'{index:03}.JPG';Image.new('RGB',(400,600),(70+index,100,150)).save(path)
                record=media.capture_record(host.photos,path,None,{});record.update(time=(datetime(2026,11,11,10)+timedelta(seconds=index)).isoformat(),missingTime=False);records.append(record)
            host.store.append_live(sid,records)
            for client in (b,c):
                peer={'device':host.sync.config['device'],'name':'主机 A','address':f'http://127.0.0.1:{host.sync.server.server_port}','receiving':False,'seen':time.time()}
                client.sync.peers[peer['device']]=peer;client.sync.connect_peer({'device':peer['device'],'password':'同步 001Ab!'});client.sync.sync_once()
            values=dict(STORE=b.store,DATA=b.store.data,MODELS=b.sku.providers,ARCHIVE=b.archive,SKU=b.sku,CAPTURESYNC=b.sync,WATCH=None,MOUSE=None,CANON=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,PREVIEWS=None,INVENTORY=None,REFERENCES=None,STORAGE=None,CATALOG=None)
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            stack.enter_context(patch.object(server,'workbench_views',return_value=None))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start();stack.callback(http.server_close);stack.callback(http.shutdown)
            out=ROOT/'build/lan-multi-client';out.mkdir(parents=True,exist_ok=True)
            with sync_playwright() as pw,pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True) as browser:
                page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];reads=[]
                page.on('pageerror',lambda error:errors.append(str(error)));page.on('request',lambda request:reads.append(request.url) if '/api/preview/' in request.url and 'size=480' in request.url else None)
                page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading&&window.FramekeepLanSync?.receiving()&&DEMO_PHOTOS.length===110')
                page.evaluate('FramekeepWorkspace.show("selection")')
                target=mapped_id(host.sync.config['device'],records[70]['id'])
                page.evaluate('(id)=>Framekeep.browsePhoto(id)',target);page.locator('[data-view="list"]').click()
                page.wait_for_function('()=>document.querySelector("#photo-list .focused img")?.naturalWidth>0&&!FramekeepThumbnailMemory.stats.active&&!FramekeepThumbnailMemory.stats.queued')
                page.locator('#catalog-view').evaluate('(el)=>el.scrollTop+=70')
                page.evaluate('()=>FramekeepIO.flush()');page.wait_for_timeout(600)
                top=page.locator('#catalog-view').evaluate('(el)=>el.scrollTop');count=len(reads)
                with host.store.connect() as db:db.execute('UPDATE sessions SET state=?,revision=revision+1 WHERE id=?',(stable_json({'overrides':{records[0]['id']:4},'selectedId':records[-1]['id'],'view':'single'}),sid))
                b.sync.sync_once();c.sync.sync_once()
                page.wait_for_function('(id)=>Framekeep.photoItems().some(row=>row.photo.id===id&&row.rating===4)',arg=mapped_id(host.sync.config['device'],records[0]['id']))
                self.assertEqual(page.evaluate('Framekeep.remoteTarget().photoId'),target);self.assertEqual(page.evaluate('Framekeep.persistedState().view'),'list')
                self.assertAlmostEqual(page.locator('#catalog-view').evaluate('(el)=>el.scrollTop'),top,delta=1)
                page.wait_for_function('()=>!FramekeepThumbnailMemory.stats.active&&!FramekeepThumbnailMemory.stats.queued')
                self.assertEqual(len(reads),count,'仅业务评级更新不应重复加载可见缩略图')
                page.screenshot(path=str(out/'client-browsing-preserved.png'))
                # 发送端界面使用同一真实主机服务的状态和逐台断开操作。
                page.route('**/api/lan-sync',lambda route:route.fulfill(content_type='application/json',body=stable_json(host.sync.state())))
                page.route('**/api/lan-sync/disconnect-client',lambda route:route.fulfill(content_type='application/json',body=stable_json(host.sync.disconnect_client(route.request.post_data_json['device']))))
                page.locator('#app-settings-open').click();page.locator('[data-settings-section="sync"]').click()
                page.wait_for_function('()=>document.querySelectorAll(".lan-sync-connected-client").length===2')
                page.screenshot(path=str(out/'host-two-clients.png'))
                page.set_viewport_size({'width':390,'height':900});page.wait_for_timeout(200)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),390)
                page.screenshot(path=str(out/'host-mobile.png'));page.set_viewport_size({'width':1680,'height':1050})
                page.locator('[data-client-device="'+b.sync.config['device']+'"] button').click()
                page.wait_for_function('()=>document.querySelectorAll(".lan-sync-connected-client").length===1')
                b.sync.sync_once();self.assertFalse(b.sync.receiving);c.sync.sync_once();self.assertTrue(c.sync.receiving)
                self.assertEqual(host.sync.state()['sending_count'],1);self.assertFalse(errors,errors)
                (out/'browser.json').write_text(json.dumps({'selected_photo_preserved':True,'layout':'list','scroll_top':top,'thumbnail_reads_added':len(reads)-count,'individual_disconnect_preserved_C':True,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':unittest.main()
