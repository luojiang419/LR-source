"""任务224：归档小图HTTP/内存复用、浅扫描复用目录属性与启动绑定不做反向DNS。"""
from contextlib import ExitStack
from pathlib import Path
import tempfile,threading,time,unittest,urllib.request
from unittest.mock import patch
from test_jpeg_receiving_preview import camera_jpeg
from asset_catalog import AssetCatalog


class FastBindTests(unittest.TestCase):
    def test_http_server_bind_skips_reverse_dns(self):
        import fast_http_bind  # noqa: F401
        from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
        with patch('socket.getfqdn',side_effect=lambda *_:time.sleep(5) or 'slow') as fqdn:
            started=time.monotonic();http=ThreadingHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
            try:
                self.assertLess(time.monotonic()-started,1);fqdn.assert_not_called()
                self.assertEqual(http.server_name,'127.0.0.1');self.assertGreater(http.server_port,0)
            finally:http.server_close()


class ArchivePreviewReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.photos=root/'photos';self.photos.mkdir()
        for name in ('a.jpg','b.jpg'):(self.photos/name).write_bytes(camera_jpeg())
        self.catalog=AssetCatalog(root/'data');self.rid=self.catalog.add_root(str(self.photos))['root_id']

    def tearDown(self):self.catalog.close();self.temp.cleanup()

    def test_visit_scan_reuses_directory_entry_attributes(self):
        original=Path.stat;calls=[]
        def counted(path,*args,**kwargs):
            if path.suffix.lower()=='.jpg':calls.append(path.name)
            return original(path,*args,**kwargs)
        with patch.object(Path,'stat',counted):self.catalog._scan(self.rid,visit_folder='')
        self.assertEqual(calls,[],'浅扫描应复用 scandir 已提供的大小与修改时间')
        rows=self.catalog.items(root=self.rid,folder='/',sort='original')['rows'];self.assertEqual(len(rows),2)
        # 指纹与逐文件 stat 结果一致，不会因换取属性方式让缩略图地址失效。
        from hashlib import sha256
        for row in rows:
            stat=original(Path(row['path']))
            self.assertEqual(row['fingerprint'],sha256(f"{row['path'].casefold()}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest())

    def test_thumbnail_bytes_are_reused_until_source_version_changes(self):
        self.catalog._scan(self.rid,visit_folder='')
        row=self.catalog.items(root=self.rid,folder='/',sort='original')['rows'][0]
        import archive_thumbnails
        real=archive_thumbnails.file_thumbnail
        with patch.object(archive_thumbnails,'file_thumbnail',side_effect=real) as decode:
            first=self.catalog.preview(row['id'],420,read=True);second=self.catalog.preview(row['id'],420,read=True)
            self.assertEqual(first,second);self.assertEqual(decode.call_count,1)
            self.catalog.thumbnail_settings.remember([dict(row,fingerprint='changed')])
            self.catalog.preview(row['id'],420,read=True);self.assertEqual(decode.call_count,2)

    def test_http_fingerprinted_thumbnail_is_cacheable_and_unversioned_is_not(self):
        import server
        self.catalog._scan(self.rid,visit_folder='')
        row=self.catalog.items(root=self.rid,folder='/',sort='original')['rows'][0];self.assertIn('?v=',row['preview'])
        with ExitStack() as stack:
            for name,value in dict(CATALOG=self.catalog,STORE=None).items():stack.enter_context(patch.object(server,name,value))
            stack.enter_context(patch.object(server,'workbench_views',return_value=None))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                base=f'http://127.0.0.1:{http.server_port}'
                with urllib.request.urlopen(base+row['preview']+'&size=420&preview_source=file&thumbnail_source=file') as response:
                    self.assertEqual(response.headers['Cache-Control'],'private, max-age=86400, immutable');self.assertTrue(response.read().startswith(b'\xff\xd8'))
                with urllib.request.urlopen(base+'/api/catalog/preview/'+row['id']+'?size=420') as response:
                    self.assertEqual(response.headers['Cache-Control'],'no-store')
            finally:http.shutdown();http.server_close()


class FolderFollowBrowserTests(unittest.TestCase):
    import test_archive_visits as fixtures
    setUp=fixtures.ArchiveVisitTests.setUp
    tearDown=fixtures.ArchiveVisitTests.tearDown

    def test_first_visit_shows_contents_when_scan_finishes_without_polling(self):
        import server
        from asset_archive import AssetArchive
        from catalog_store import Store
        from model_providers import Providers
        from http.server import ThreadingHTTPServer
        from playwright.sync_api import sync_playwright
        from PIL import Image
        for n in range(30):Image.new('RGB',(40,30),'teal').save(self.photos/f'B/extra-{n:03d}.jpg')
        root=Path(__file__).resolve().parents[1];store=Store(self.catalog.data,root);archive=AssetArchive(store,autostart=False)
        scan=self.catalog._scan
        def slow_scan(*args,**kwargs):time.sleep(.6);return scan(*args,**kwargs)
        with ExitStack() as stack:
            for key,value in dict(STORE=store,DATA=store.data,CATALOG=self.catalog,ARCHIVE=archive,
                MODELS=Providers(store.data),SKU=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,
                UPDATES=None,ANALYSIS=None,REFERENCES=None,PREVIEWS=None).items():stack.enter_context(patch.object(server,key,value))
            stack.enter_context(patch.object(self.catalog,'_scan',side_effect=slow_scan))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':900});errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        # 固定资料版本：证明内容来自访问跟进，而非2秒版本轮询触发的整页刷新。
                        page.add_init_script("""Object.defineProperty(window,'FramekeepIO',{configurable:true,set(value){const api=value.api;value.api=(path,...args)=>path.startsWith('/api/catalog/revision')?Promise.resolve({revision:[0,0]}):api(path,...args);Object.defineProperty(window,'FramekeepIO',{value,writable:true,configurable:true})}})""")
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                        page.wait_for_function('()=>window.FramekeepArchiveBrowser&&FramekeepArchiveBrowser.roots().length>0',timeout=10000)
                        started=time.monotonic()
                        page.evaluate('(root)=>{void FramekeepArchiveBrowser.navigate(root,"B").catch(()=>{})}',self.rid)
                        page.wait_for_function('()=>document.querySelectorAll("#catalog-grid .catalog-card").length===31&&[...document.querySelectorAll("#catalog-grid img")].every(e=>e.naturalWidth>0)',timeout=10000)
                        elapsed=time.monotonic()-started;print(f'[task224] first-visit elapsed={elapsed:.3f}s',flush=True)
                        self.assertLess(elapsed,1.9,f'首次访问应在扫描完成后立即显示，实际 {elapsed:.2f}s')
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close();archive.close()


if __name__=='__main__':unittest.main()
