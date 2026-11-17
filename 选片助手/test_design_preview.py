"""PSD/PSB真实资源结构、缓存复用、失败分类和版本恢复。"""
from io import BytesIO
from contextlib import ExitStack
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image,ImageOps
from asset_catalog import AssetCatalog
from design_preview import embedded_thumbnail
import media_engine as media
from preview_cache import PreviewWarmer,cache_for,cache_name


def write_psd(path,version=1,thumbnail=True,depth=8):
    payload=BytesIO();Image.new('RGB',(40,60),'red').save(payload,format='JPEG');jpeg=payload.getvalue()
    thumb=struct.pack('>6I2H',1,40,60,120,7200,len(jpeg),24,1)+jpeg
    resource=b'8BIM'+struct.pack('>H',1036)+b'\0\0'+struct.pack('>I',len(thumb))+thumb+b'\0'*(len(thumb)%2) if thumbnail else b''
    header=struct.pack('>4sH6sHIIHH',b'8BPS',version,b'\0'*6,3,60,40,depth,3)
    # 合成图为三通道无压缩PSD；PSB通过资源缩略图显示，无需展开合成图。
    path.write_bytes(header+b'\0'*4+struct.pack('>I',len(resource))+resource+b'\0'*(8 if version==2 else 4)+b'\0\0'+bytes([255])*2400+b'\0'*4800)


class DesignPreviewTests(unittest.TestCase):
    def test_missing_thumbnail_repair_reuses_large_cache_and_checks_designs_only_once(self):
        write_psd(self.photos/'missing.psd');write_psd(self.photos/'cached.psb',version=2)
        Image.new('RGB',(50,60),'blue').save(self.photos/'photo.jpg')
        self.catalog.add_root(str(self.photos));self.drain()
        rows=self.catalog.items()['rows'];row=next(row for row in rows if row['name']=='missing.psd')
        _,record=self.catalog.preview_record(row,[row]);index=cache_for(self.catalog.previews)
        small=index.get(cache_name(record,420));small.unlink();(self.photos/'missing.psd').unlink()
        with patch('design_preview.preview',side_effect=AssertionError('已有大缓存不应重读PSD')):
            job=self.worker.repair_design_thumbnails(automatic=True);self.worker.design_repair_thread.join(5)
            state=self.worker.design_repair_state();self.assertEqual((state['status'],state['checked'],state['cached'],state['queued']),('completed',2,1,1))
            self.drain();self.assertIsNotNone(index.get(cache_name(record,420),read=True))
            self.assertEqual(self.worker.repair_design_thumbnails(automatic=True)['id'],job['id'])

    def test_missing_thumbnail_scan_covers_more_than_one_batch(self):
        for number in range(133):write_psd(self.photos/f'{number:03d}.psd')
        self.catalog.add_root(str(self.photos));self.worker.repair_design_thumbnails();self.worker.design_repair_thread.join(10)
        state=self.worker.design_repair_state();self.assertEqual((state['status'],state['checked'],state['queued']),('completed',133,133))

    def test_old_failed_queue_is_repaired_once_and_offline_share_remains_retryable(self):
        path=self.photos/'recover.psd';write_psd(path);self.catalog.add_root(str(self.photos))
        with patch('design_preview.preview',side_effect=PermissionError('temporary')):self.drain()
        self.assertEqual(self.worker.archive_state()['failed'],1)
        with self.cache.connect() as c:c.execute("DELETE FROM configuration WHERE key='design-preview-repair-v1'")
        self.worker.close();self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.catalog.preview_warmer=self.worker;self.catalog.on_previews=self.worker.enqueue;self.drain()
        self.assertEqual(self.worker.archive_state()['ready'],1)
        from preview_errors import skip_reason
        with patch.object(Path,'is_dir',return_value=False):
            self.assertIsNone(skip_reason({'jpg':'missing.jpg'},self.photos,FileNotFoundError('share offline')))

    def test_browser_renders_psd_pixels_and_skipped_progress_reaches_completion(self):
        import server
        from catalog_store import Store
        from asset_archive import AssetArchive
        from model_providers import Providers
        from sku_service import SkuService
        from playwright.sync_api import sync_playwright,expect
        write_psd(self.photos/'poster.psd');(self.photos/'unsupported.pdf').write_bytes(b'%PDF-1.4 fixture')
        self.catalog.add_root(str(self.photos));self.drain();self.assertTrue(self.worker.refresh_phase()['ready'])
        root=Path(__file__).resolve().parents[1];store=Store(self.catalog.data,root)
        providers=Providers(store.data);archive=AssetArchive(store,autostart=False);sku=SkuService(store,providers,autostart=False)
        with ExitStack() as stack:
            for key,value in dict(STORE=store,DATA=store.data,ARCHIVE=archive,CATALOG=self.catalog,PREVIEWS=self.worker,
                                  MODELS=providers,SKU=sku,REFERENCES=None,WATCH=None,MOUSE=None,ANALYSIS=None,
                                  RAWLINK=None,FILESEARCH=None,UPDATES=None).items():stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                        expect(page.locator('#catalog-preview-status')).to_contain_text('已跳过 1 项')
                        expect(page.locator('#catalog-preview-status')).to_contain_text('全部完成')
                        self.assertEqual(page.locator('#catalog-preview-bar').get_attribute('value'),'100')
                        image=page.locator('#catalog-grid img[alt="poster.psd"]')
                        page.wait_for_function('()=>document.querySelector("img[alt=\\"poster.psd\\"]")?.naturalWidth>0')
                        pixel=image.evaluate('''img=>{const c=document.createElement('canvas');c.width=c.height=1;c.getContext('2d').drawImage(img,0,0,1,1);return [...c.getContext('2d').getImageData(0,0,1,1).data]}''')
                        self.assertGreater(pixel[0],240);self.assertLess(pixel[1],10)
                        # 模拟升级前已显示的成功占位图：即使后台缓存完整，手动修复也要刷新它。
                        import base64
                        row=next(row for row in self.catalog.items()['rows'] if row['name']=='poster.psd')
                        placeholder='data:image/jpeg;base64,'+base64.b64encode(self.catalog._media_placeholder(row,420)).decode()
                        image.click()
                        page.evaluate('''src=>{document.querySelector('#catalog-grid img[alt="poster.psd"]').src=src;document.getElementById('catalog-selected-preview').src=src}''',placeholder)
                        page.wait_for_function('()=>document.querySelector("#catalog-grid img[alt=\\"poster.psd\\"]").naturalWidth===420')
                        page.locator('#app-settings-open').click();page.locator('[data-settings-section=preview]').click()
                        page.locator('#preview-repair-missing').click()
                        expect(page.locator('#preview-repair-status')).to_contain_text('已检查 1 项')
                        page.locator('#asset-archive-open').click()
                        page.wait_for_function('()=>document.querySelector("#catalog-grid img[alt=\\"poster.psd\\"]").naturalWidth===40&&document.getElementById("catalog-selected-preview").naturalWidth===40')
                        self.assertIn('design_preview=1',image.get_attribute('data-preview-src'))
                        self.assertIn('thumbnail_revision=',image.get_attribute('data-preview-src'))
                        image.dblclick();expect(page.locator('#asset-image-viewer')).to_be_visible()
                        expect(page.locator('.asset-viewer-toolbar')).to_be_hidden()
                        page.keyboard.press('Equal');self.assertEqual(page.locator('#asset-viewer-scale').inner_text(),'125%')
                        expect(page.locator('#asset-viewer-fields')).to_contain_text('poster.psd')
                        self.assertEqual(errors,[])
                    finally:browser.close()
            finally:http.shutdown();http.server_close();sku.close();archive.close()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        self.catalog=AssetCatalog(self.base/'data');self.cache=cache_for(self.base/'data/previews')
        self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.catalog.on_previews=self.worker.enqueue;self.catalog.preview_warmer=self.worker

    def tearDown(self):
        self.worker.close();self.catalog.close();self.temp.cleanup()

    def drain(self):
        for _ in range(40):
            if not self.worker.process_one():return
        self.fail('队列未在有限尝试内完成')

    def test_psd_psb_16bit_resource_thumbnails_cache_and_unsupported_persist(self):
        for name,version,depth in [('eight.psd',1,8),('sixteen.psd',1,16),('large.psb',2,16)]:write_psd(self.photos/name,version,depth=depth)
        (self.photos/'document.pdf').write_bytes(b'%PDF-1.4 fixture')
        original={p.name:p.read_bytes() for p in self.photos.iterdir()}
        self.catalog.add_root(str(self.photos));self.drain();state=self.worker.archive_state()
        self.assertEqual((state['ready'],state['ignored'],state['failed'],state['pending']),(3,1,0,0))
        row=next(r for r in self.catalog.items()['rows'] if r['name']=='sixteen.psd')
        payload=self.catalog.preview(row['id'],420,read=True)
        with Image.open(BytesIO(payload)) as im:self.assertEqual(im.size,(40,60));self.assertGreater(im.getpixel((20,30))[0],240)
        self.assertIn('cached=',row['preview'])
        self.assertEqual({p.name:p.read_bytes() for p in self.photos.iterdir()},original)
        (self.photos/row['name']).unlink()
        with patch('design_preview.preview',side_effect=AssertionError('已缓存PSD不应再次读取源图')):
            self.assertEqual(self.catalog.preview(row['id'],420,read=True),payload)
        self.worker.close();self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.catalog.on_previews=self.worker.enqueue;self.catalog.preview_warmer=self.worker
        self.catalog.queue_all_previews();self.assertFalse(self.worker.process_one())
        self.assertEqual(self.worker.archive_state()['ignored'],1)

    def test_no_thumbnail_uses_supported_merged_image_and_invalid_resource_is_bounded(self):
        path=self.photos/'merged.psd';write_psd(path,thumbnail=False)
        from design_preview import preview
        image=preview(path,420);self.assertEqual(image.size,(40,60));self.assertEqual(image.getpixel((0,0)),(255,0,0))
        path.write_bytes(b'8BPS'+struct.pack('>H',2)+b'\0'*20+b'\xff'*4)
        with self.assertRaises(ValueError):embedded_thumbnail(path)

    def test_missing_source_is_skipped_and_changed_version_is_requeued(self):
        path=self.photos/'missing.jpg';Image.new('RGB',(80,60),'blue').save(path)
        root=self.catalog.add_root(str(self.photos))['root_id'];path.unlink();self.drain()
        self.assertEqual(self.worker.archive_state()['ignored'],1)
        self.catalog.queue_all_previews();self.assertFalse(self.worker.process_one())
        Image.new('RGB',(90,60),'red').save(path);self.catalog.scan(root);self.drain()
        self.assertEqual((self.worker.archive_state()['ready'],self.worker.archive_state()['ignored']),(1,0))

    def test_permission_and_cache_failures_are_not_silently_ignored(self):
        write_psd(self.photos/'locked.psd');self.catalog.add_root(str(self.photos))
        with patch('design_preview.preview',side_effect=PermissionError('temporary permission failure')):self.drain()
        state=self.worker.archive_state();self.assertEqual((state['failed'],state['ignored']),(1,0))

    def test_empty_jpeg_falls_back_to_absolute_paired_raw_in_background(self):
        (self.photos/'pair.jpg').write_bytes(b'');(self.photos/'pair.CR2').write_bytes(b'raw-container-fixture')
        root=self.catalog.add_root(str(self.photos))['root_id']
        stream=BytesIO();Image.new('RGB',(240,360),'green').save(stream,format='JPEG')
        def embedded(path,tool):
            self.assertEqual(path.resolve(),(self.photos/'pair.CR2').resolve());return 6,stream.getvalue()
        with patch.object(media,'raw_embedded_metadata',side_effect=embedded) as reader:
            self.drain();self.assertEqual(reader.call_count,1)
        self.assertEqual(self.worker.archive_state([root])['ready'],1)
        row=self.catalog.items()['rows'][0]
        with Image.open(BytesIO(self.catalog.preview(row['id'],420,read=True))) as im:
            self.assertEqual(ImageOps.exif_transpose(im).size,(360,240))


if __name__=='__main__':unittest.main()
