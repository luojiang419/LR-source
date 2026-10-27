"""资料后台入库、只查数据库的详情、全文索引和并发版本边界。"""
from contextlib import ExitStack, contextmanager
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, TiffImagePlugin
from asset_catalog import AssetCatalog

ROOT = Path(__file__).resolve().parents[1]


class CatalogMetadataTests(unittest.TestCase):
    def test_start_returns_while_restart_recovery_waits_for_database_writer(self):
        returned=threading.Event();processed=threading.Event()
        metadata=self.catalog.metadata
        with self.catalog.connect() as c:
            c.execute("UPDATE catalog_metadata SET status='running' WHERE item=?",(self.item,))
        def idle():
            processed.set();return False
        starter=threading.Thread(target=lambda:(metadata.start(),returned.set()),daemon=True)
        with patch.object(metadata,'process_one',side_effect=idle):
            try:
                with self.catalog.connect() as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    starter.start()
                    self.assertTrue(returned.wait(1),'后台恢复等待写锁不能阻塞启动调用方')
                    self.assertFalse(processed.is_set())
                self.assertTrue(processed.wait(5),'释放写锁后应继续恢复历史任务')
                with self.catalog.connect() as c:
                    self.assertEqual(c.execute('SELECT status FROM catalog_metadata WHERE item=?',(self.item,)).fetchone()[0],'queued')
            finally:
                metadata.close();starter.join(5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.photos = self.base/'photos'; self.photos.mkdir()
        self.path = self.photos/'portrait.JPG'
        exif=Image.Exif();exif[36867]='2026:10:25 10:20:30';exif[272]='TEST CAMERA';exif[34855]=400
        exif[33437]=TiffImagePlugin.IFDRational(28,10);exif[33434]=TiffImagePlugin.IFDRational(1,125)
        Image.new('RGB',(160,100),'teal').save(self.path,exif=exif)
        self.catalog = AssetCatalog(self.base/'data')
        self.root = self.catalog.add_root(str(self.photos))['root_id']
        self.item = self.catalog.items()['rows'][0]['id']

    def tearDown(self):
        self.catalog.close();self.temp.cleanup()

    def sidecar(self, title):
        self.path.with_suffix('.xmp').write_text('''<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:dc="http://purl.org/dc/elements/1.1/">
          <rdf:RDF><rdf:Description><dc:title><rdf:Alt><rdf:li xml:lang="x-default">'''+title+'''</rdf:li></rdf:Alt></dc:title>
          <dc:subject><rdf:Bag><rdf:li>蓝色丝绸</rdf:li></rdf:Bag></dc:subject></rdf:Description></rdf:RDF></x:xmpmeta>''',encoding='utf8')

    def wait_ready(self):
        until=time.monotonic()+15
        while self.catalog.metadata.state()['pending']:
            if time.monotonic()>until:self.fail(str(self.catalog.metadata.state()))
            time.sleep(.02)

    def test_auto_index_and_search_exif_xmp_chinese_literals_and_restart_without_extraction(self):
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.sidecar('秋季人像 100%_ "样片"')
        Image.new('RGB',(20,10),'red').save(self.photos/'other.JPG');self.catalog.scan(self.root)
        self.catalog.metadata.start();self.wait_ready()
        self.assertEqual(self.catalog.details(self.item)['capture']['camera'],'TEST CAMERA')
        for word in ('TEST CAMERA','2026-10-25','ISO 400','f/2.8','1/125','蓝色','秋季人像','100%_', '"样片"'):
            self.assertEqual(self.catalog.items(text=word)['total'],1,word)
            self.assertEqual(self.catalog.tree(text=word)['roots'][0]['folders'][0]['count'],1,word)
        self.assertEqual(self.catalog.items(text='不存在')['total'],0)
        self.assertEqual(self.catalog.items(text='蓝色',media='video')['total'],0)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
        self.catalog.close();self.catalog=AssetCatalog(self.base/'data')
        with patch('media_engine.ExifToolSession.execute',side_effect=AssertionError('未变化不再次提取')):
            self.catalog.metadata.start();self.wait_ready()
        self.assertEqual(self.catalog.metadata.state()['failed'],0)
        self.assertEqual(self.catalog.items(text='蓝色')['total'],1)

    def test_details_returns_during_blocked_extraction_and_under_writer_lock(self):
        started=threading.Event();release=threading.Event()
        def blocked(row):
            started.set();release.wait(10)
            return {'capture':{'camera':'ASYNC'},'media':{},'description':{}},'ASYNC'
        with patch.object(self.catalog.metadata,'_extract',side_effect=blocked):
            self.catalog.metadata.start();self.assertTrue(started.wait(5))
            try:
                with self.catalog.connect() as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    with patch.object(Path,'stat',side_effect=AssertionError('详情不可读取原文件')):
                        start=time.monotonic();data=self.catalog.details(self.item)
                        self.assertLess(time.monotonic()-start,.2)
                        self.assertEqual(data['metadata_status'],'running');self.assertTrue(data['files'])
            finally:release.set()
            self.wait_ready()
        self.assertEqual(self.catalog.details(self.item)['capture']['camera'],'ASYNC')

    def test_sidecar_notifications_and_inflight_generations_do_not_publish_stale_data(self):
        self.sidecar('旧标题');self.catalog.metadata.process_one()
        self.sidecar('新标题');self.catalog.metadata.changed_path(self.path.with_suffix('.xmp'))
        self.catalog.metadata.process_one()
        self.assertEqual(self.catalog.items(text='新标题')['total'],1)
        self.assertEqual(self.catalog.items(text='旧标题')['total'],0)
        self.catalog.metadata.request(self.item,force=True)
        original=self.catalog.metadata._extract
        def racing(row):
            result=original(row)
            self.sidecar('最终标题');self.catalog.metadata.changed_path(self.path.with_suffix('.xmp'))
            return result
        with patch.object(self.catalog.metadata,'_extract',side_effect=racing):self.catalog.metadata.process_one()
        self.assertEqual(self.catalog.metadata.cached(self.item)['status'],'queued')
        self.catalog.metadata.process_one()
        self.assertEqual(self.catalog.items(text='最终标题')['total'],1)
        self.assertEqual(self.catalog.items(text='新标题')['total'],0)

    def test_file_changes_reconcile_before_publish_and_failed_jobs_do_not_spin(self):
        self.catalog.metadata.process_one()
        Image.new('RGB',(90,60),'red').save(self.path)
        self.catalog.metadata.request(self.item,force=True)
        self.catalog.metadata.process_one();self.assertEqual(self.catalog.metadata.cached(self.item)['status'],'queued')
        self.catalog.metadata.process_one();self.assertEqual(self.catalog.details(self.item)['capture']['width'],90)
        self.path.unlink();self.catalog.metadata.request(self.item,force=True);self.catalog.metadata.process_one()
        self.assertEqual(self.catalog.metadata.cached(self.item)['status'],'failed')
        self.assertFalse(self.catalog.metadata.process_one())
        # 已入库资料在原图离线时可继续显示和搜索，不做现场文件探测。
        with patch.object(Path,'stat',side_effect=AssertionError('已入库详情不探测文件')):
            self.assertEqual(self.catalog.details(self.item)['capture']['width'],90)
        Image.new('RGB',(90,60),'blue').save(self.path);self.catalog.scan(self.root);self.catalog.metadata.process_one()
        self.assertEqual(self.catalog.metadata.cached(self.item)['status'],'ready')

    def test_video_auto_probe_and_text_search(self):
        from video_preview import find_tool
        subprocess.run([find_tool('ffmpeg'),'-v','error','-f','lavfi','-i','color=c=blue:s=96x64:r=5',
                        '-t','0.4','-c:v','libx264','-y',str(self.photos/'clip.MOV')],check=True,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.catalog.scan(self.root)
        self.catalog.metadata.start();self.wait_ready()
        video=self.catalog.items(media='video')['rows'][0];details=self.catalog.details(video['id'])
        self.assertEqual(details['metadata_status'],'ready');self.assertEqual(details['capture']['width'],96)
        self.assertGreater(details['media']['duration_ms'],0)
        self.assertEqual(self.catalog.items(text='H.264',media='video')['total'],1)
        with patch('asset_catalog.subprocess.run',side_effect=AssertionError('详情不再启动探测')):
            self.assertEqual(self.catalog.details(video['id'])['media'],details['media'])

    def test_legacy_backfill_and_raw_pair_search_keep_pagination_and_filters(self):
        # 旧数据库没有资料行；读取时优先补齐当前项，后台续接剩余条目。
        with self.catalog.connect() as c:c.execute('DELETE FROM catalog_metadata')
        self.catalog.details(self.item);self.catalog.metadata.process_one()
        self.assertEqual(self.catalog.items(text='TEST CAMERA')['total'],1)
        (self.photos/'portrait.CR3').write_bytes(b'fixture raw')
        self.catalog.scan(self.root)
        raw=next(row for row in self._all_items() if row['extension']=='.cr3')
        with self.catalog.connect() as c:
            c.execute("UPDATE catalog_metadata SET search_text='RAW 唯一镜头',status='ready' WHERE item=?",(raw['id'],))
        self.assertEqual(self.catalog.items(text='唯一镜头')['rows'][0]['id'],self.item)
        self.assertEqual(self.catalog.items(text='唯一镜头',root='missing')['total'],0)
        # 关闭 trigram 后短词/字面查询结果保持一致。
        self.catalog.metadata.fts=False
        self.assertEqual(self.catalog.items(text='唯一')['total'],1)

    def _all_items(self):
        with self.catalog.connect() as c:return [dict(row) for row in c.execute('SELECT * FROM items')]

    def test_legacy_queue_backfills_beyond_first_batch(self):
        with self.catalog.connect() as c:
            for number in range(519):
                c.execute('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,last_seen,pair_key)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (str(number),self.root,str(self.photos/f'{number}.jpg'),f'{number}.jpg','/',f'{number}.jpg','.jpg',1,1,str(number),1,str(number)))
            c.execute('DELETE FROM catalog_metadata')
        payload={'capture':{},'media':{},'description':{}}
        with patch.object(self.catalog.metadata,'_extract',return_value=(payload,'migration')):
            self.catalog.metadata.start()
            until=time.monotonic()+35
            while self.catalog.metadata.state()['pending']:
                if time.monotonic()>until:self.fail(str(self.catalog.metadata.state()))
                time.sleep(.03)
        self.assertEqual(self.catalog.metadata.state()['ready'],520)

    def test_shared_catalog_schema_refresh_keeps_browsing_available(self):
        started=threading.Event();release=threading.Event();errors=[]
        original=AssetCatalog.connect
        @contextmanager
        def paused(catalog):
            with original(catalog) as connection:
                def trace(sql):
                    if 'CREATE VIEW capture_items AS' in sql:
                        started.set();release.wait(5)
                connection.set_trace_callback(trace)
                yield connection
        def reopen():
            try:
                other=AssetCatalog(self.catalog.data);other.close()
            except Exception as error:errors.append(error)
        with patch.object(AssetCatalog,'connect',paused):
            worker=threading.Thread(target=reopen);worker.start()
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(self.catalog.items()['total'],1)
                self.assertEqual(self.catalog.details(self.item)['item']['id'],self.item)
            finally:release.set();worker.join(10)
        self.assertFalse(errors,errors)

    def test_reference_watcher_requeues_both_xmp_names_without_media_rescan(self):
        from reference_library import ReferenceLibrary
        self.sidecar('初始标题');self.catalog.metadata.process_one()
        library=ReferenceLibrary(self.catalog.data,catalog=self.catalog)
        try:
            self.sidecar('通知后的标题')
            with patch.object(library,'_index_paths',side_effect=AssertionError('XMP 不应作为媒体重新索引')):
                library._apply_changes(self.root,{'portrait.xmp'})
            self.assertEqual(self.catalog.metadata.cached(self.item)['status'],'queued')
            self.catalog.metadata.process_one();self.assertEqual(self.catalog.items(text='通知后的标题')['total'],1)
            self.path.with_suffix('.xmp').rename(Path(str(self.path)+'.xmp'))
            library._apply_changes(self.root,{'portrait.JPG.xmp','portrait.xmp'})
            # 原生通知和显式通知可重叠；工作线程接续新代次后应最终就绪。
            self.catalog.metadata.start();self.wait_ready()
            self.assertEqual(self.catalog.metadata.cached(self.item)['status'],'ready')
        finally:library.close()

    def test_browser_pending_details_update_search_and_close_without_stale_polling(self):
        import server
        from asset_archive import AssetArchive
        from catalog_store import Store
        from model_providers import Providers
        from playwright.sync_api import sync_playwright
        self.sidecar('秋季人像')
        original=self.catalog.metadata._extract
        started=threading.Event();release=threading.Event()
        def held(row):
            started.set();release.wait(20);return original(row)
        store=Store(self.catalog.data,ROOT);archive=AssetArchive(store,autostart=False)
        out=ROOT/'build/catalog-metadata';out.mkdir(parents=True,exist_ok=True)
        with ExitStack() as stack:
            for key,value in dict(STORE=store,DATA=store.data,CATALOG=self.catalog,ARCHIVE=archive,
                MODELS=Providers(store.data),SKU=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,
                UPDATES=None,ANALYSIS=None,REFERENCES=None,PREVIEWS=None).items():
                stack.enter_context(patch.object(server,key,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with patch.object(self.catalog.metadata,'_extract',side_effect=held):
                    self.catalog.metadata.start();self.assertTrue(started.wait(5))
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                        try:
                            page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
                            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                            page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                            page.locator('#asset-archive-open').click();page.locator('#catalog-grid .catalog-card').dblclick()
                            page.wait_for_function("()=>document.querySelector('#asset-viewer-info-status').textContent.includes('后台入库')")
                            self.assertIn('原图路径',page.locator('#asset-viewer-fields').inner_text())
                            page.screenshot(path=str(out/'pending.png'))
                            release.set()
                            page.wait_for_function("()=>document.querySelector('#asset-viewer-info-status').textContent===''")
                            self.assertIn('TEST CAMERA',page.locator('#asset-viewer-fields').inner_text())
                            self.assertIn('秋季人像',page.locator('#asset-viewer-fields').inner_text())
                            page.wait_for_selector('.asset-viewer-metadata input[name=title]')
                            self.assertEqual(page.locator('.asset-viewer-metadata input[name=title]').input_value(),'秋季人像')
                            for theme in ('dark','light','pure-white','hardcore'):
                                page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                                page.set_viewport_size({'width':1440,'height':1000});page.screenshot(path=str(out/f'ready-{theme}.png'))
                                page.set_viewport_size({'width':375,'height':950})
                                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                            page.screenshot(path=str(out/'ready-narrow.png'))
                            page.locator('#asset-viewer-close').click();page.set_viewport_size({'width':1440,'height':1000})
                            page.locator('#catalog-search').fill('蓝色丝绸')
                            page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===1")
                            page.wait_for_function("()=>document.querySelectorAll('.catalog-search-match').length===1")
                            page.screenshot(path=str(out/'search.png'))
                            page.locator('#catalog-search').fill('不存在的标题')
                            page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===0")
                            self.assertFalse(errors,errors)
                            (out/'browser-result.json').write_text(json.dumps({'passed':True,'errors':errors,'pending_auto_update':True,'themes':4,'narrow_width':375,'chinese_metadata_search':True},ensure_ascii=False,indent=2),encoding='utf8')
                        finally:release.set();browser.close()
            finally:release.set();http.shutdown();http.server_close();archive.close()


if __name__=='__main__':unittest.main()
