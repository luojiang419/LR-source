import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack,contextmanager
from unittest.mock import patch,Mock

from PIL import Image
from asset_catalog import AssetCatalog,_id
from archive_visits import ArchiveVisits


class ArchiveVisitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.photos=self.base/'photos';self.photos.mkdir()
        for name in ('A','B'):
            folder=self.photos/name;folder.mkdir();Image.new('RGB',(90,60),'teal').save(folder/'photo.jpg')
        self.catalog=AssetCatalog(self.base/'data');self.rid=_id(str(self.photos).casefold())
        with self.catalog.connect() as c:c.execute('INSERT INTO roots(id,path,created) VALUES(?,?,?)',(self.rid,str(self.photos),time.time()))
        self.visits=ArchiveVisits(self.catalog);self.catalog.visits=self.visits

    def tearDown(self):
        self.visits.close();self.catalog.close();self.temp.cleanup()

    def wait(self):
        until=time.monotonic()+20
        while self.visits.state()['running']:
            if time.monotonic()>until:self.fail(self.visits.state())
            time.sleep(.02)
        self.assertFalse(self.visits.state()['error'])

    def rows(self):
        with self.catalog.connect() as c:return [dict(r) for r in c.execute('SELECT * FROM items ORDER BY path')]

    def test_visit_scans_only_current_folder_and_reuses_thumbnail(self):
        with patch.object(self.catalog,'_media_paths',side_effect=AssertionError('禁止递归扫描')):
            self.visits.touch(self.rid,'A');self.wait()
        rows=self.rows();self.assertEqual([r['folder'] for r in rows],['A'])
        item=rows[0];cached=self.catalog.preview(item['id'],420)
        cached=Path(cached);stamp=cached.stat().st_mtime_ns;content=hashlib.sha256(cached.read_bytes()).hexdigest()
        self.visits.touch('',refresh=True)
        self.visits.touch(self.rid,'A');self.wait()
        self.assertEqual(cached.stat().st_mtime_ns,stamp)
        self.assertEqual(hashlib.sha256(cached.read_bytes()).hexdigest(),content)
        self.assertFalse(any('B'==r['folder'] for r in self.rows()))

    def test_revisit_updates_add_change_delete_without_hiding_other_folder(self):
        self.visits.touch(self.rid,'A');self.wait();original=self.rows()[0]['fingerprint']
        self.visits.touch(self.rid,'B');self.wait()
        Image.new('RGB',(120,80),'red').save(self.photos/'A/photo.jpg')
        Image.new('RGB',(50,30),'blue').save(self.photos/'A/new.jpg')
        self.visits.touch(self.rid,'A');self.wait()
        self.assertEqual(len(self.catalog.items(root=self.rid,folder='A')['rows']),2)
        self.assertNotEqual(next(r for r in self.rows() if r['folder']=='A' and r['name']=='photo.jpg')['fingerprint'],original)
        (self.photos/'A/photo.jpg').unlink();self.visits.touch(self.rid,'A',refresh=True);self.wait()
        online=[r for r in self.rows() if r['online']]
        self.assertEqual({r['relative'] for r in online},{'A/new.jpg','B/photo.jpg'})

    def test_parent_lists_child_folders_without_reading_their_files(self):
        self.visits.touch(self.rid);self.wait()
        self.assertEqual(self.rows(),[])
        self.assertEqual({r['folder'] for r in self.visits.tree()['roots'][0]['folders']},{'A','B'})

    def test_explorer_counts_only_media_and_keeps_unknown_child_folders(self):
        explorer=self.visits.explorer
        result=explorer.browse(self.rid)
        self.assertEqual((result['folders'],result['files']),(2,0))
        self.assertTrue(all(not r['known'] for r in self.visits.tree()['roots'][0]['folders']))
        (self.photos/'A/说明.txt').write_text('资料',encoding='utf8')
        result=explorer.browse(self.rid,'A')
        self.assertEqual((result['folders'],result['files']),(0,1))
        row=next(r for r in self.visits.tree()['roots'][0]['folders'] if r['folder']=='A')
        self.assertTrue(row['known']);self.assertEqual(row['count'],1)

    def test_explorer_browse_does_not_wait_for_catalog_writer(self):
        with self.catalog.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            started=time.monotonic()
            result=self.visits.explorer.browse(self.rid,'A')
            self.assertEqual(result['files'],1)
            self.assertLess(time.monotonic()-started,2)

    def test_original_order_is_exact_folder_in_visited_mode(self):
        self.visits.touch(self.rid,'A');self.wait()
        self.visits.touch(self.rid,'B');self.wait()
        self.assertEqual(self.catalog.items(root=self.rid,folder='',sort='original')['rows'],[])
        self.assertEqual({r['folder'] for r in self.catalog.items(root=self.rid,folder='A',sort='original')['rows']},{'A'})

    def test_explorer_operations_and_guards(self):
        explorer=self.visits.explorer
        def op(action,**values):return explorer.operate(dict(root=self.rid,folder='',action=action,**values))
        op('mkdir',name='新建');self.wait()
        op('copy',source='A/photo.jpg',name='copy.jpg');self.wait()
        self.assertTrue((self.photos/'A/photo.jpg').exists())
        self.assertEqual((self.photos/'copy.jpg').read_bytes(),(self.photos/'A/photo.jpg').read_bytes())
        op('rename',source='copy.jpg',name='renamed.jpg');self.wait()
        self.assertFalse((self.photos/'copy.jpg').exists())
        with self.assertRaises(ValueError):op('copy',source='A/photo.jpg',name='renamed.jpg')
        with self.assertRaises(ValueError):op('rename',source='',name='oops')
        with self.assertRaises(ValueError):op('mkdir',name='../escape')
        with self.assertRaises(ValueError):explorer.browse(self.rid,'../outside')
        with self.assertRaises(ValueError):explorer.operate(dict(root=self.rid,folder='A',action='move',source='A',name='child'))
        op('move',source='renamed.jpg',name='moved.jpg');self.wait()
        self.assertTrue((self.photos/'moved.jpg').exists())

    def test_explorer_keeps_xmp_with_media_and_rejects_sidecar_conflicts(self):
        explorer=self.visits.explorer
        (self.photos/'A/photo.xmp').write_text('评级与款号资料',encoding='utf8')
        def op(action,source,name):
            explorer.operate(dict(root=self.rid,folder='B',action=action,source=source,name=name));self.wait()
        op('copy','A/photo.jpg','copy.jpg')
        self.assertEqual((self.photos/'B/copy.xmp').read_text(encoding='utf8'),'评级与款号资料')
        op('rename','B/copy.jpg','rename.jpg')
        self.assertFalse((self.photos/'B/copy.xmp').exists())
        self.assertTrue((self.photos/'B/rename.xmp').exists())
        (self.photos/'B/conflict.xmp').write_text('保持不变',encoding='utf8')
        with self.assertRaises(ValueError):op('copy','A/photo.jpg','conflict.jpg')
        self.assertFalse((self.photos/'B/conflict.jpg').exists())

    def test_explorer_jobs_report_success_and_failure(self):
        explorer=self.visits.explorer
        def run(body):
            job=explorer.submit(dict(root=self.rid,folder='',**body))['job']
            until=time.monotonic()+5
            while explorer.job(job)['status'] in ('queued','running'):
                self.assertLess(time.monotonic(),until);time.sleep(.01)
            return explorer.job(job)
        self.assertEqual(run(dict(action='mkdir',name='job'))['status'],'complete')
        failed=run(dict(action='mkdir',name='../outside'))
        self.assertEqual(failed['status'],'failed');self.assertTrue(failed['error'])

    def test_explorer_failed_copy_does_not_publish_partial_target(self):
        explorer=self.visits.explorer
        with patch('archive_explorer.shutil.copy2',side_effect=OSError('磁盘已满')):
            with self.assertRaises(OSError):explorer.operate(dict(action='copy',root=self.rid,folder='',source='A/photo.jpg',name='partial.jpg'))
        self.assertFalse((self.photos/'partial.jpg').exists())
        self.assertFalse(list(self.photos.glob('.shiying-copy-*')))
        self.assertTrue((self.photos/'A/photo.jpg').is_file())

    def test_switch_stops_remaining_old_folder_previews(self):
        for n in range(5):Image.new('RGB',(20,10),'red').save(self.photos/f'A/{n}.jpg')
        started=threading.Event();release=threading.Event();seen=[]
        def preview(item,size,**options):
            row=self.catalog.item(item);seen.append(row['folder'])
            if row['folder']=='A':started.set();release.wait(5)
        with patch.object(self.catalog,'preview',side_effect=preview):
            self.visits.touch(self.rid,'A');self.assertTrue(started.wait(5))
            self.visits.touch(self.rid,'B');release.set();self.wait()
        self.assertLessEqual(seen.count('A'),self.visits.preview_workers);self.assertIn('B',seen)

    def test_thumbnails_overlap_with_bounded_window_and_keep_current_counts(self):
        for n in range(8):Image.new('RGB',(20,10),'red').save(self.photos/f'A/{n}.jpg')
        lock=threading.Lock();both=threading.Event();release=threading.Event();active=0;peak=0;calls=[]
        def preview(item,size,**options):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active);calls.append(item)
                if active==2:both.set()
            try:release.wait(5)
            finally:
                with lock:active-=1
        with patch.object(self.catalog,'preview',side_effect=preview):
            self.visits.touch(self.rid,'A')
            try:
                self.assertTrue(both.wait(5),'两张缩略图应同时读取')
                self.assertEqual(len(calls),2,'慢读期间不能提交整个目录')
                self.visits.touch(self.rid,'B')
            finally:release.set()
            self.wait()
        self.assertEqual(peak,2)
        self.assertEqual((self.visits.state()['total'],self.visits.state()['cached'],self.visits.state()['failed']),(1,1,0))

    def test_thumbnail_failure_does_not_stop_remaining_images(self):
        Image.new('RGB',(20,10),'red').save(self.photos/'A/second.jpg')
        def preview(item,size,**options):
            if self.catalog.item(item)['name']=='photo.jpg':raise OSError('测试不可读照片')
        with patch.object(self.catalog,'preview',side_effect=preview):
            self.visits.touch(self.rid,'A');self.wait()
        self.assertEqual((self.visits.state()['total'],self.visits.state()['cached'],self.visits.state()['failed']),(2,1,1))

    def test_cached_background_thumbnail_does_not_wait_for_cache_write_lock(self):
        from preview_cache import cache_for
        self.visits.touch(self.rid,'A');self.wait();row=self.rows()[0]
        done=threading.Event();errors=[]
        def read():
            try:self.visits._cache_preview(row,self.visits.generation)
            except Exception as exc:errors.append(exc)
            finally:done.set()
        with cache_for(self.catalog.previews).lock:
            worker=threading.Thread(target=read);worker.start()
            ready=done.wait(1)
        worker.join(5)
        self.assertTrue(ready,'已缓存缩略图不能等待批量缓存写锁')
        self.assertFalse(errors)

    def test_cached_thumbnail_hints_use_covering_index_and_keep_current_version(self):
        from preview_cache import cache_for
        self.visits.touch(self.rid,'A');self.wait()
        rows=self.catalog.items(root=self.rid,folder='A',sort='original')['rows']
        self.assertIn('cached=',rows[0]['preview'])
        index=cache_for(self.catalog.previews)
        with index.read_connection() as c:
            plan=' '.join(r[3] for r in c.execute('EXPLAIN QUERY PLAN SELECT name,fingerprint,edge,decoded,orientation FROM previews INDEXED BY preview_hint_v1 WHERE edge=420 AND decoded=0 AND fingerprint IN (?)',('sample',)))
        self.assertIn('COVERING INDEX preview_hint_v1',plan)
        Image.new('RGB',(120,90),'red').save(self.photos/'A/photo.jpg')
        self.visits.touch(self.rid,'A',refresh=True);self.wait()
        current=self.catalog.items(root=self.rid,folder='A',sort='original')['rows']
        self.assertNotEqual(rows[0]['preview'],current[0]['preview'])

    def test_thumbnail_preparation_uses_exact_directory_index(self):
        queries=[];connect=self.catalog.connect
        @contextmanager
        def traced():
            with connect() as c:
                c.set_trace_callback(queries.append)
                yield c
        with patch.object(self.catalog,'connect',traced):
            self.visits.touch(self.rid,'A');self.wait()
        query=next(sql for sql in queries if sql.startswith('SELECT id,path,media_type FROM items'))
        with connect() as c:plan=' '.join(r['detail'] for r in c.execute('EXPLAIN QUERY PLAN '+query))
        self.assertIn('catalog_folder_candidates (root=? AND online=? AND folder=?)',plan)
        self.assertNotIn('catalog_original_order',plan)

    def test_http_thumbnail_does_not_wait_for_startup_snapshot_disk_write(self):
        import server,urllib.request
        from http.server import ThreadingHTTPServer
        self.visits.touch(self.rid,'A');self.wait();item=self.rows()[0]
        started=threading.Event();release=threading.Event()
        def slow_write(*args):started.set();release.wait(5)
        with patch.object(server,'CATALOG',self.catalog),patch.object(server,'workbench_views',return_value=None),patch.object(self.catalog.startup,'thumbnail',side_effect=slow_write):
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{http.server_port}/api/catalog/preview/'+item['id']+'?size=420',timeout=1) as response:
                    self.assertTrue(response.read().startswith(b'\xff\xd8'))
                self.assertTrue(started.wait(1));self.assertFalse(release.is_set(),'快照写入仍阻塞时图片应已返回')
                for n in range(50):self.catalog.startup.queue_thumbnail(str(n)+'.jpg',b'jpeg')
                with self.catalog.startup.thumbnail_lock:self.assertLessEqual(len(self.catalog.startup.thumbnail_pending),24)
            finally:
                release.set();self.catalog.startup.close();http.shutdown();http.server_close()

    def test_metadata_does_not_read_unvisited_queued_files(self):
        self.visits.touch(self.rid,'A');self.wait()
        self.visits.touch(self.rid,'B');self.wait()
        with patch.object(self.catalog.metadata,'_extract',return_value=({'tags':{},'capture':{},'media':{},'description':{}},'')) as read:
            for _ in range(5):self.catalog.metadata.process_one(priority_only=True)
        self.assertTrue(read.called)
        self.assertTrue(all(Path(call.args[0]['path']).parent==self.photos/'B' for call in read.call_args_list))

    def test_browser_loads_folder_tree_and_only_clicked_folder(self):
        import server
        from asset_archive import AssetArchive
        from catalog_store import Store
        from model_providers import Providers
        from http.server import ThreadingHTTPServer
        from playwright.sync_api import sync_playwright
        root=Path(__file__).resolve().parents[1]
        store=Store(self.catalog.data,root);archive=AssetArchive(store,autostart=False)
        with ExitStack() as stack:
            for key,value in dict(STORE=store,DATA=store.data,CATALOG=self.catalog,ARCHIVE=archive,
                MODELS=Providers(store.data),SKU=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,
                UPDATES=None,ANALYSIS=None,REFERENCES=None,PREVIEWS=None).items():stack.enter_context(patch.object(server,key,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            self.catalog.metadata.start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
                        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.locator('#asset-archive-open').click()
                        self.assertEqual(page.locator('#catalog-grid .catalog-card').count(),0)
                        page.locator(f'[data-root="{self.rid}"][data-folder=""]').first.click()
                        root_node=page.locator(f'[data-root="{self.rid}"][data-folder=""]').first
                        if not root_node.evaluate('e=>e.parentElement.open'):root_node.click()
                        page.locator(f'[data-root="{self.rid}"][data-folder="A"]').first.click(timeout=15000)
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===1")
                        self.assertEqual({r['folder'] for r in self.rows()},{'A'})
                        generation=self.visits.generation
                        page.evaluate("document.dispatchEvent(new CustomEvent('framekeep:metadata-imported'))")
                        page.wait_for_timeout(500)
                        self.assertEqual(self.visits.generation,generation,'资料回显刷新不能重新扫描当前目录')
                        self.assertIn('当前文件夹',page.locator('#catalog-scan-status').inner_text())
                        # 媒体文件的浏览、导航、复制与重命名；文档保留但不出现在列表。
                        (self.photos/'A/说明.txt').write_text('浏览器文件操作',encoding='utf8')
                        Image.new('RGB',(90,60),'red').save(self.photos/'A/说明.jpg')
                        original=(self.photos/'A/说明.jpg').read_bytes()
                        page.locator('#explorer-view').click()
                        page.locator('#explorer-refresh').click()
                        file=page.locator('.explorer-entry',has_text='说明.jpg');file.wait_for()
                        self.assertIn('2 个文件',page.locator('#catalog-summary').inner_text())
                        file.click();page.locator('[data-file-action="copy"]').click()
                        page.locator('#explorer-up').click()
                        page.wait_for_function("()=>FramekeepArchiveBrowser.scope().folder===''")
                        page.locator('.explorer-entry[title="B"]').dblclick()
                        page.wait_for_function("()=>FramekeepArchiveBrowser.scope().folder==='B'")
                        page.locator('[data-file-action="paste"]').click()
                        file=page.locator('.explorer-entry',has_text='说明.jpg');file.wait_for()
                        file.click();page.locator('[data-file-action="rename"]').click()
                        page.locator('#explorer-name').fill('改名.jpg');page.locator('#explorer-name-dialog button[type="submit"]').click()
                        page.locator('.explorer-entry',has_text='改名.jpg').wait_for()
                        self.assertEqual((self.photos/'B/改名.jpg').read_bytes(),original)
                        self.assertTrue((self.photos/'A/说明.jpg').exists())
                        self.assertTrue((self.photos/'A/说明.txt').exists())
                        page.locator('[data-file-action="mkdir"]').click();page.locator('#explorer-name').fill('新目录');page.locator('#explorer-name-dialog button[type="submit"]').click()
                        page.locator('.explorer-entry',has_text='新目录').wait_for()
                        page.locator('#explorer-back').click()
                        page.wait_for_function("()=>FramekeepArchiveBrowser.scope().folder===''")
                        page.locator('#explorer-forward').click()
                        page.wait_for_function("()=>FramekeepArchiveBrowser.scope().folder==='B'")
                        page.locator('#explorer-address').fill(str(self.photos/'A'));page.locator('#explorer-address').press('Enter')
                        page.wait_for_function("()=>FramekeepArchiveBrowser.scope().folder==='A'")
                        page.locator('#catalog-search').fill('说明')
                        page.locator('.explorer-entry',has_text='说明.jpg').wait_for()
                        self.assertEqual(page.evaluate('FramekeepArchiveBrowser.scope().folder'),'A')
                        page.locator('#catalog-search').fill('')
                        page.wait_for_function("()=>document.querySelector('#catalog-grid').classList.contains('explorer-details')")
                        page.locator('.explorer-entry',has_text='photo.jpg').wait_for()
                        page.locator('#catalog-media').select_option('image')
                        page.locator('.explorer-entry',has_text='photo.jpg').wait_for()
                        page.wait_for_function("()=>!document.querySelectorAll('.explorer-entry strong').length||![...document.querySelectorAll('.explorer-entry strong')].some(e=>e.textContent==='说明.txt')")
                        page.locator('#catalog-media').select_option('')
                        page.locator('#explorer-view').click()
                        page.wait_for_function("()=>!FramekeepBoot.loading&&!document.querySelector('#explorer-name-dialog[open]')")
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===2")
                        self.assertEqual(page.locator('.explorer-entry').count(),0)
                        page.wait_for_timeout(500)
                        out=root/'build/archive-explorer';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'explorer-browser.png'),animations='disabled')
                        for width in (1000,680):
                            page.set_viewport_size({'width':width,'height':900})
                            self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close();archive.close()

    def test_browser_prefetches_every_page_without_scrolling_and_reuses_http_cache(self):
        import server
        from asset_archive import AssetArchive
        from catalog_store import Store
        from model_providers import Providers
        from http.server import ThreadingHTTPServer
        from playwright.sync_api import sync_playwright
        for n in range(135):Image.new('RGB',(40,30),'teal').save(self.photos/f'A/extra-{n:03d}.jpg')
        self.visits.touch(self.rid,'A');self.wait()
        root=Path(__file__).resolve().parents[1];store=Store(self.catalog.data,root);archive=AssetArchive(store,autostart=False)
        with ExitStack() as stack:
            for key,value in dict(STORE=store,DATA=store.data,CATALOG=self.catalog,ARCHIVE=archive,
                MODELS=Providers(store.data),SKU=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,
                UPDATES=None,ANALYSIS=None,REFERENCES=None,PREVIEWS=None).items():stack.enter_context(patch.object(server,key,value))
            http_reads=[];preview=self.catalog.preview
            def tracked_preview(*args,**kwargs):
                if 'process_request_thread' in threading.current_thread().name:http_reads.append(args[0])
                return preview(*args,**kwargs)
            stack.enter_context(patch.object(self.catalog,'preview',side_effect=tracked_preview))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':900});errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        # 目录树/快照暂不可用也不能让窗口外缩略图等到滚动才取回。
                        # 不使用page.route：路由拦截会关闭浏览器HTTP缓存，误伤本例缓存复用断言。
                        page.add_init_script("""Object.defineProperty(window,'FramekeepIO',{configurable:true,set(value){const api=value.api;value.api=(path,...args)=>path.startsWith('/api/catalog/tree')||path.startsWith('/api/catalog/bootstrap')?Promise.reject(new Error('slow tree')):api(path,...args);Object.defineProperty(window,'FramekeepIO',{value,writable:true,configurable:true})}})""")
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                        page.evaluate('(root)=>{void FramekeepArchiveBrowser.navigate(root,"A").catch(()=>{})}',self.rid)
                        page.wait_for_function('()=>document.getElementById("catalog-continuous-status").textContent.includes("已准备 136 / 136")',timeout=20000)
                        # 任务224：后续页自动追加为真实卡片并加载，无需滚动到底。
                        self.assertEqual(page.locator('#catalog-grid').evaluate('e=>e.scrollTop'),0)
                        self.assertEqual(page.locator('#catalog-grid .catalog-card').count(),136)
                        self.assertEqual(page.locator('#catalog-grid img').evaluate_all('(images)=>images.filter(e=>e.naturalWidth>0).length'),136)
                        self.assertEqual(len(http_reads),136,'每张缩略图只应读取一次')
                        before=len(http_reads)
                        # 重绘、换目录再返回都应复用HTTP缓存，不重新读取原片。
                        page.evaluate('(root)=>{void FramekeepArchiveBrowser.navigate(root,"B").catch(()=>{})}',self.rid)
                        page.wait_for_function('()=>document.querySelectorAll("#catalog-grid .catalog-card").length===1&&[...document.querySelectorAll("#catalog-grid img")].every(e=>e.naturalWidth>0)',timeout=10000)
                        page.evaluate('(root)=>{void FramekeepArchiveBrowser.navigate(root,"A").catch(()=>{})}',self.rid)
                        page.wait_for_function('()=>document.querySelectorAll("#catalog-grid .catalog-card").length===136&&[...document.querySelectorAll("#catalog-grid img")].every(e=>e.naturalWidth>0)',timeout=10000)
                        self.assertLessEqual(len(http_reads)-before,1,'返回已浏览目录应复用浏览器缓存')
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close();archive.close()

    def test_revisit_rechecks_changed_xmp_without_global_metadata_sweep(self):
        side=self.photos/'A/photo.xmp'
        def write(code):
            side.write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:subject><rdf:Bag><rdf:li>SKU:'+code+'</rdf:li></rdf:Bag></dc:subject></rdf:Description></rdf:RDF></x:xmpmeta>',encoding='utf8')
        write('OLD-123');self.visits.touch(self.rid,'A');self.wait()
        self.catalog.metadata.process_one(priority_only=True)
        self.assertEqual(self.rows()[0]['sku'],'OLD-123')
        write('NEW-456');self.visits.touch(self.rid,'A',refresh=True);self.wait()
        self.catalog.metadata.process_one(priority_only=True)
        self.assertEqual(self.rows()[0]['sku'],'NEW-456')

    def test_legacy_preview_and_root_scan_workers_do_not_start(self):
        from preview_cache import PreviewWarmer
        from reference_library import ReferenceLibrary
        from archive_pipeline import ArchivePipeline
        warmer=PreviewWarmer.__new__(PreviewWarmer);warmer.catalog=self.catalog;warmer.started=False
        warmer.start();self.assertTrue(warmer.started)
        self.assertFalse(warmer.enqueue('not-visited',[{}]))
        warmer.reconcile_archive(full=True)
        pipeline=ArchivePipeline.__new__(ArchivePipeline);pipeline.catalog=self.catalog;pipeline.tick()
        reference=ReferenceLibrary(self.catalog.data,None,catalog=self.catalog,autostart=False)
        try:
            reference.start()
            self.assertIsNone(reference.thread.ident)
            self.assertIsNotNone(reference.watch_thread.ident)
            self.assertEqual(reference.enqueue(self.rid)['mode'],'visited')
        finally:reference.close()

    def test_startup_defers_historical_dataset_network_restore(self):
        from archive_capture_service import ArchiveCaptureService
        api=ArchiveCaptureService.__new__(ArchiveCaptureService)
        api.catalog=self.catalog;api.closed=threading.Event();api.restore_roots=[str(self.photos/'B')]
        api.scan_cursor={};api.pending={};api.loading={};api.errors={}
        api.tick=Mock();api._dataset=Mock(side_effect=AssertionError('不应连接未访问的历史目录'))
        api.wake=Mock();api.wake.wait.side_effect=lambda _:api.closed.set()
        api._run()
        api._dataset.assert_not_called();self.assertEqual(api.restore_roots,[str(self.photos/'B')])


if __name__=='__main__':unittest.main()
