"""分屏左侧参考区：搜索结果卡片与大图需从内嵌小缩略图自动换成清晰预览。"""
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image
from playwright.sync_api import expect, sync_playwright

import server
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from asset_intelligence import AssetIntelligence
from catalog_store import Store
from model_providers import Providers
from reference_library import ReferenceLibrary
from retrieval_store import save_result
from sku_service import SkuService
from test_asset_intelligence import result
from visual_features import APPEARANCE, appearance

ROOT = Path(__file__).resolve().parents[1]


def exif_with_thumbnail(size=(160, 107)):
    """构造带 IFD1 内嵌缩略图的 EXIF，模拟相机 JPG 的 160 像素小图。"""
    stream = BytesIO();Image.new('RGB', size, 'gray').save(stream, 'JPEG');thumb = stream.getvalue()
    ifd0 = struct.pack('<H', 0)+struct.pack('<I', 14)
    ifd1 = struct.pack('<H', 2)+struct.pack('<HHII', 0x201, 4, 1, 44)+struct.pack('<HHII', 0x202, 4, 1, len(thumb))+struct.pack('<I', 0)
    tiff = b'II*\x00'+struct.pack('<I', 8)+ifd0+ifd1
    assert len(tiff)==44
    return b'Exif\x00\x00'+tiff+thumb


class SplitSharpPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.base = Path(self.tmp.name);self.stack = ExitStack()
        self.store = Store(self.base/'data', ROOT);self.catalog = AssetCatalog(self.store.data)
        self.providers = Providers(self.store.data);self.archive = AssetArchive(self.store, autostart=False)
        self.sku = SkuService(self.store, self.providers, autostart=False)
        self.service = AssetIntelligence(self.catalog, self.providers, self.sku, autostart=False)
        self.library = ReferenceLibrary(self.base/'reference-data')
        folder = self.base/'LC1234';folder.mkdir()
        for name, color in [('衬衫正面.jpg', (200, 40, 40)), ('衬衫背面.jpg', (180, 60, 40))]:
            Image.new('RGB', (2400, 1600), color).save(folder/name, 'JPEG', exif=exif_with_thumbnail())
        self.catalog.add_root(str(folder))
        for row in self.catalog.items()['rows']:
            with self.catalog.connect() as c:
                save_result(c, row['id'], row['fingerprint'], 'test', 'fixture', [{'seconds':0, 'result':result('红色')}],
                            [(0, APPEARANCE, appearance(Path(row['path']).read_bytes()))])
            self.catalog.assign(row['id'], 'LC1234')
        self.service.quick_index.build()
        while self.service.quick_index.sync():pass
        values = dict(STORE=self.store, CATALOG=self.catalog, ARCHIVE=self.archive, SKU=self.sku, MODELS=self.providers,
                      INTELLIGENCE=self.service, LANWORKSPACE=None, DATA=self.store.data, WATCH=None, MOUSE=None, ANALYSIS=None,
                      RAWLINK=None, FILESEARCH=None, UPDATES=None, COMPONENTS=None, DRIVER=None, CANON=None, PREVIEWS=None,
                      REFERENCES=self.library)
        for name, value in values.items():self.stack.enter_context(patch.object(server, name, value))
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler);self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start();self.url = f'http://127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.stack.close();self.service.close();self.library.close()
        self.sku.close();self.archive.close();self.catalog.close();self.tmp.cleanup()

    def test_search_cards_and_single_view_upgrade_to_sharp_previews(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page(viewport={'width':1680, 'height':1000}, device_scale_factor=2, reduced_motion='reduce')
                errors, sizes, held = [], [], []
                page.on('pageerror', lambda error:errors.append(str(error)))
                def requested(request):
                    url = urlsplit(request.url)
                    if url.path.startswith('/api/catalog/preview/'):sizes.append(parse_qs(url.query).get('size', ['420'])[0])
                page.on('request', requested)
                # 暂扣大图响应，检查加载期间先显示已有缩略图而非空白。
                page.route(lambda url:'/api/catalog/preview/' in url and 'size=1800' in url, lambda route:route.continue_() if held else held.append(route))
                page.goto(self.url, wait_until='domcontentloaded')
                page.wait_for_function('()=>window.FramekeepWorkspace&&!FramekeepBoot.loading')
                page.locator('#view-open').click();page.locator('#layout-split').click();page.keyboard.press('Escape')
                page.locator('#split-search').fill('1234');page.locator('#split-search').press('Enter')
                cards = page.locator('#split-preview-grid .split-resource-card img')
                expect(cards).to_have_count(2)
                page.wait_for_function('()=>[...document.querySelectorAll("#split-preview-grid .split-card-preview img")].every(e=>e.complete&&e.naturalWidth>0)')
                # 默认 100% 网格：内嵌 160 像素缩略图不足以填满卡片，应自动换成更大的预览。
                page.wait_for_function('()=>[...document.querySelectorAll("#split-preview-grid .split-card-preview img")].every(e=>e.complete&&e.naturalWidth>=640)', timeout=15000)
                self.assertTrue(any(size in ('640', '1024') for size in sizes), sizes)
                first = page.locator('#split-preview-grid .split-resource-card').first
                thumb = first.locator('img').evaluate('e=>e.currentSrc')
                first.dblclick()
                expect(page.locator('#split-reference-stage')).to_be_visible()
                image = page.locator('#split-reference-image')
                self.assertIn('size=1800', image.get_attribute('src'))
                outgoing = page.locator('#split-reference-outgoing')
                while not held:page.wait_for_timeout(50)
                # 清晰大图返回前显示同图已解码缩略图，而不是空白或模糊放大的旧图。
                self.assertTrue(outgoing.is_visible())
                self.assertEqual(outgoing.evaluate('e=>e.currentSrc'), thumb)
                held[0].continue_()
                page.wait_for_function('()=>{const e=document.getElementById("split-reference-image");return e.complete&&e.naturalWidth===1800}', timeout=15000)
                expect(outgoing).to_be_hidden()
                page.keyboard.press('Escape')
                expect(page.locator('#split-preview-grid')).to_be_visible()
                # 放大网格后，可见卡片继续换成匹配卡片像素的更大预览。
                page.evaluate('()=>{const e=document.getElementById("split-grid-scale");e.value="300";e.dispatchEvent(new Event("input",{bubbles:true}))}')
                page.wait_for_function('()=>{const e=document.querySelector("#split-preview-grid .split-card-preview img");return e.complete&&e.naturalWidth>=1024}', timeout=15000)
                self.assertFalse(errors, errors)
            finally:browser.close()


if __name__=='__main__':
    unittest.main()
