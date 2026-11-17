"""直读文件内嵌缩略图，不读写磁盘缩略图缓存。"""
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from http.server import ThreadingHTTPServer
import tempfile,threading,unittest,urllib.request,struct
from unittest.mock import patch
from PIL import Image
from archive_thumbnails import ArchiveThumbnails,jpeg_thumbnail,_exif_thumbnail,file_thumbnail
from test_jpeg_receiving_preview import camera_jpeg
from asset_catalog import AssetCatalog


class DirectThumbnailTests(unittest.TestCase):
    def test_embedded_thumbnail_pixels_and_orientation_are_used(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'photo.jpg';path.write_bytes(camera_jpeg(6))
            thumb=jpeg_thumbnail(path);self.assertIsNotNone(thumb)
            self.assertLess(len(thumb[0]),16000)
            content=file_thumbnail({'path':str(path),'media_type':'image'},420,lambda:None)
            with Image.open(BytesIO(content)) as image:
                self.assertEqual(image.size,(100,160));r,g,b=image.getpixel((50,80));self.assertGreater(r,200);self.assertLess(g,40)

    def test_missing_embedded_thumbnail_falls_back_without_disk_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'plain.jpg';Image.new('RGB',(2400,1600),'teal').save(path)
            self.assertIsNone(jpeg_thumbnail(path))
            with patch('preview_cache.cache_for',side_effect=AssertionError('不能读写磁盘缩略图缓存')):
                content=file_thumbnail({'path':str(path),'media_type':'image'},420,lambda:None)
            with Image.open(BytesIO(content)) as image:self.assertEqual(max(image.size),420)
            self.assertEqual([p.name for p in Path(temp).iterdir()],['plain.jpg'])

    def test_corrupt_exif_offsets_are_bounded(self):
        self.assertIsNone(_exif_thumbnail(b'Exif\0\0II*\0\xff\xff\xff\xff'))

    def test_big_endian_exif_thumbnail(self):
        stream=BytesIO();Image.new('RGB',(80,40),'red').save(stream,format='JPEG');payload=stream.getvalue()
        tiff=b'MM'+struct.pack('>HIH',42,8,1)+struct.pack('>HHIH',0x112,3,1,6)+b'\0\0'+struct.pack('>I',26)
        tiff+=struct.pack('>H',2)+struct.pack('>HHII',0x201,4,1,56)+struct.pack('>HHII',0x202,4,1,len(payload))+struct.pack('>I',0)+payload
        self.assertEqual(_exif_thumbnail(b'Exif\0\0'+tiff),(payload,6))

    def test_direct_video_keeps_configured_frame_number(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'clip.mp4';path.write_bytes(b'fixture')
            with patch('video_preview.extract_frame',return_value=Image.new('RGB',(320,180),'teal')) as extract:
                file_thumbnail({'path':str(path),'media_type':'video'},420,lambda:None,frame='10')
            self.assertEqual(extract.call_args.args[2],10)

    def test_direct_mode_ignores_legacy_cache_preference_and_cannot_enable(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            settings=Path(temp)/'archive-thumbnail-settings.json';settings.write_text(json.dumps(dict(cache_enabled=True)))
            original=settings.read_text();mode=ArchiveThumbnails(temp);self.assertFalse(mode.cache_enabled)
            mode.configure(False);self.assertFalse(ArchiveThumbnails(temp).cache_enabled)
            with self.assertRaises(ValueError):mode.configure(True)
            with self.assertRaises(ValueError):mode.configure(0)
            self.assertEqual(settings.read_text(),original)

    def test_http_direct_mode_bypasses_cached_argument_and_startup_copy(self):
        import server
        with tempfile.TemporaryDirectory() as temp,ExitStack() as stack:
            root=Path(temp);photos=root/'photos';photos.mkdir();path=photos/'photo.jpg';path.write_bytes(camera_jpeg())
            catalog=AssetCatalog(root/'data');rid=catalog.add_root(str(photos))['root_id'];catalog.scan(rid)
            rows=catalog.items(root=rid,sort='original')['rows'];item=rows[0]['id'];catalog.thumbnail_settings.configure(False)
            for name,value in dict(CATALOG=catalog,STORE=None).items():stack.enter_context(patch.object(server,name,value))
            stack.enter_context(patch.object(server,'workbench_views',return_value=None))
            stack.enter_context(patch.object(catalog,'render_preview',side_effect=AssertionError('不能读取旧缓存')))
            stack.enter_context(patch.object(catalog.startup,'queue_thumbnail',side_effect=AssertionError('不能另存启动缩略图')))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{http.server_port}/api/catalog/preview/{item}?size=420&cached='+('a'*64)+'.jpg') as response:
                    self.assertEqual(response.headers['Cache-Control'],'no-store');payload=response.read()
                with Image.open(BytesIO(payload)) as image:r,g,b=image.getpixel((40,40));self.assertGreater(r,200);self.assertLess(g,40)
            finally:http.shutdown();http.server_close();catalog.close()


class DirectSettingsBrowserTests(unittest.TestCase):
    import test_archive_visits as fixtures
    setUp=fixtures.ArchiveVisitTests.setUp
    tearDown=fixtures.ArchiveVisitTests.tearDown
    wait=fixtures.ArchiveVisitTests.wait
    rows=fixtures.ArchiveVisitTests.rows

    def test_archive_switch_does_not_change_sku_library_cache_urls(self):
        from playwright.sync_api import sync_playwright
        source=(Path(__file__).resolve().parents[1]/'演示方案/asset-library.js').read_text(encoding='utf8')
        function=source[source.index('  const previewUrl ='):source.index('  const previewIdentity=')]
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page();page.route('http://unit.test/**',lambda route:route.fulfill(body='<html></html>',content_type='text/html'));page.goto('http://unit.test')
                page.add_script_tag(content="const startupPreviews=new Map();const previewIdentity=value=>value;window.FramekeepArchiveThumbnailCacheEnabled=false;"+function+"window.testPreviewUrl=previewUrl;")
                sku=page.evaluate("testPreviewUrl({preview:'/api/sku/library/preview/photo?cached=kept'},420)")
                self.assertNotIn('cached=',sku);self.assertIn('preview_source=file',sku)
                archive=page.evaluate("testPreviewUrl({preview:'/api/catalog/preview/photo?cached=ignored'},420)")
                self.assertIn('thumbnail_source=file',archive);self.assertNotIn('cached=',archive)
            finally:browser.close()

    def test_switch_in_settings_changes_source_and_persists_without_touching_old_thumbnail(self):
        import server
        from catalog_store import Store
        from asset_archive import AssetArchive
        from model_providers import Providers
        from playwright.sync_api import sync_playwright
        path=self.photos/'A/photo.jpg';path.write_bytes(camera_jpeg())
        self.visits.touch(self.rid,'A');self.wait();row=self.rows()[0]
        cached=Path(self.catalog.preview(row['id'],420));stamp=cached.stat().st_mtime_ns
        store=Store(self.catalog.data,Path(__file__).resolve().parents[1]);archive=AssetArchive(store,autostart=False)
        with ExitStack() as stack:
            for name,value in dict(STORE=store,DATA=store.data,CATALOG=self.catalog,ARCHIVE=archive,SKU=None,MODELS=Providers(store.data),WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,ANALYSIS=None,REFERENCES=None,PREVIEWS=None).items():stack.enter_context(patch.object(server,name,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#app-settings-open').click()
                        page.locator('[data-settings-section="preview"]').click()
                        self.assertIn('文件直读',page.locator('#direct-preview-mode').inner_text())
                        self.assertEqual(page.locator('#archive-thumbnail-cache-enabled').count(),0)
                        self.assertFalse(page.locator('#preview-cache-enabled').is_visible())
                        self.assertFalse(page.locator('#cache-storage-migrate').is_visible())
                        self.assertFalse(ArchiveThumbnails(self.catalog.data).cache_enabled)
                        page.locator('#asset-archive-open').click();page.evaluate('(root)=>{void FramekeepArchiveBrowser.navigate(root,"A")}',self.rid)
                        page.wait_for_function('()=>document.querySelector("#catalog-grid img")?.naturalWidth===160',timeout=10000)
                        image=page.locator('#catalog-grid img');self.assertIn('thumbnail_source=file',image.get_attribute('data-preview-src'));self.assertNotIn('cached=',image.get_attribute('data-preview-src'))
                        self.assertEqual(cached.stat().st_mtime_ns,stamp);self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close();archive.close()


if __name__=='__main__':unittest.main()
