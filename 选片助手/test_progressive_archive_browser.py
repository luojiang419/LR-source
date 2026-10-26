"""隔离 HTTP 与可控图片响应，验证渐进预览和首次归档的请求上限。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

from PIL import Image
from playwright.sync_api import sync_playwright, Error
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from model_providers import Providers

ROOT = Path(__file__).resolve().parents[1]


class ProgressiveArchiveBrowserTests(unittest.TestCase):
    def test_cold_grid_progressive_order_failure_cancellation_and_themes(self):
        import server
        out = ROOT / 'build/progressive-archive'
        out.mkdir(parents=True, exist_ok=True)
        small = BytesIO(); Image.new('RGB', (420, 280), '#648b88').save(small, format='JPEG')
        large = BytesIO(); Image.new('RGB', (2400, 1600), '#648b88').save(large, format='JPEG')
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            base = Path(folder); photos = base / 'photos'; photos.mkdir()
            for i in range(48):
                Image.new('RGB', (60, 40), '#648b88').save(photos / f'{i:03}.jpg')
            store = Store(base / 'data', ROOT); archive = AssetArchive(store, autostart=False)
            catalog = AssetCatalog(store.data); catalog.add_root(str(photos))
            for key, value in dict(STORE=store, DATA=store.data, ARCHIVE=archive, CATALOG=catalog,
                                   MODELS=Providers(store.data), SKU=None, WATCH=None, MOUSE=None,
                                   RAWLINK=None, FILESEARCH=None, UPDATES=None, ANALYSIS=None).items():
                stack.enter_context(patch.object(server, key, value))
            http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler); http.daemon_threads = True
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                    try:
                        page = browser.new_page(viewport={'width':1440, 'height':900})
                        page.set_default_timeout(8000)
                        errors = []; pending = []; requests = []
                        page.on('pageerror', lambda error: errors.append(str(error)))

                        def hold(route):
                            edge = int(parse_qs(urlparse(route.request.url).query)['size'][0])
                            requests.append((edge, route.request.url))
                            if edge == 900:
                                route.fulfill(body=small.getvalue(), content_type='image/jpeg')
                            else:
                                pending.append((edge, route))

                        def release(edge, fail=False):
                            target = None
                            if page.locator('#asset-image-viewer').evaluate('e=>e.open'):
                                target = page.locator('#asset-viewer-image' if edge != 420 else '#asset-viewer-thumbnail').get_attribute('src')
                            at = next(i for i in range(len(pending)-1, -1, -1)
                                      if pending[i][0] == edge and (not target or pending[i][1].request.url == target))
                            _, route = pending.pop(at)
                            if fail: route.abort()
                            else: route.fulfill(body=(large if edge != 420 else small).getvalue(), content_type='image/jpeg')

                        def release_full(fail=False):
                            source=page.locator('#asset-viewer-image').get_attribute('src')
                            edge=int(parse_qs(urlparse(source).query)['size'][0])
                            release(edge,fail)

                        page.route('**/api/catalog/preview/**', hold)
                        page.goto(f'http://127.0.0.1:{http.server_port}', wait_until='domcontentloaded')
                        page.locator('#asset-archive-open').click()
                        page.wait_for_function('()=>document.querySelectorAll("#catalog-grid .catalog-card").length===48')
                        page.wait_for_timeout(150)
                        self.assertEqual(len([r for r in requests if r[0] == 420]), 3)
                        self.assertGreater(page.locator('.catalog-thumb[data-preview-state=loading] .asset-loading-art').count(), 0)
                        page.screenshot(path=str(out / 'cold-grid.png'))
                        first = page.locator('#catalog-grid .catalog-card').first
                        first.dblclick()
                        page.wait_for_function('()=>document.getElementById("asset-viewer-stage").dataset.previewState==="loading"')
                        page.wait_for_timeout(80)
                        self.assertIn('size=2048',page.locator('#asset-viewer-image').get_attribute('src'))
                        colors = {}
                        for theme in ['dark', 'light', 'pure-white', 'hardcore']:
                            page.evaluate('(t)=>FramekeepTheme.set(t)', theme)
                            self.assertTrue(page.locator('#asset-viewer-loading').is_visible())
                            self.assertEqual(page.locator('#asset-viewer-loading .asset-loading-art').evaluate('e=>getComputedStyle(e).color'),
                                             page.locator('#asset-viewer-loading').evaluate('e=>getComputedStyle(e).color'))
                            colors[theme] = page.locator('#asset-viewer-loading').evaluate('e=>getComputedStyle(e).color')
                            page.screenshot(path=str(out / f'loading-{theme}.png'))
                            page.set_viewport_size({'width':375, 'height':900})
                            self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                            self.assertTrue(page.locator('#asset-viewer-loading .asset-loading-art').is_visible())
                            page.set_viewport_size({'width':1440, 'height':900})
                        self.assertNotEqual(colors['dark'], colors['light'])
                        # 小图先到：可操作预览，清晰大图到达后保持用户缩放。
                        release(420)
                        page.wait_for_function('()=>document.getElementById("asset-viewer-stage").dataset.previewState==="preview"')
                        self.assertTrue(page.locator('#asset-viewer-thumbnail').is_visible())
                        self.assertFalse(page.locator('#asset-viewer-loading').is_visible())
                        page.keyboard.press('Equal')
                        self.assertEqual(page.locator('#asset-viewer-scale').inner_text(), '125%')
                        page.screenshot(path=str(out / 'thumbnail-first.png'))
                        release_full()
                        page.wait_for_function('()=>document.getElementById("asset-viewer-stage").dataset.previewState==="ready"')
                        self.assertEqual(page.locator('#asset-viewer-image').evaluate('e=>e.naturalWidth'), 2400)
                        self.assertEqual(page.locator('#asset-viewer-scale').inner_text(), '125%')
                        page.screenshot(path=str(out / 'full-ready.png'))
                        # 下一张大图先到：迟到的小图不能覆盖清晰图。
                        page.locator('#asset-viewer-next').click(); page.wait_for_timeout(80)
                        release_full()
                        page.wait_for_function('()=>document.getElementById("asset-viewer-stage").dataset.previewState==="ready"')
                        release(420); page.wait_for_timeout(250)
                        self.assertTrue(page.locator('#asset-viewer-thumbnail').is_hidden())
                        self.assertEqual(page.locator('#asset-viewer-image').evaluate('e=>e.naturalWidth'), 2400)
                        # 大图失败保留小图，并结束忙碌状态。
                        page.locator('#asset-viewer-next').click(); page.wait_for_timeout(80)
                        release(420)
                        page.wait_for_function('()=>document.getElementById("asset-viewer-stage").dataset.previewState==="preview"')
                        release_full(fail=True)
                        page.wait_for_function('()=>document.getElementById("asset-viewer-stage").dataset.previewState==="error"')
                        self.assertTrue(page.locator('#asset-viewer-thumbnail').is_visible())
                        self.assertIn('保留缩略图', page.locator('#asset-viewer-image-status').inner_text())
                        self.assertEqual(page.locator('#asset-viewer-stage').get_attribute('aria-busy'), 'false')
                        page.screenshot(path=str(out / 'large-failed.png'))
                        # 全部失败停止加载动画，仍可退出。
                        page.locator('#asset-viewer-next').click(); page.wait_for_timeout(80)
                        release(420, fail=True); release_full(fail=True)
                        page.wait_for_function('()=>document.getElementById("asset-viewer-stage").dataset.previewState==="error"')
                        self.assertFalse(page.locator('#asset-viewer-loading').is_visible())
                        # 快速切图和关闭后，旧的响应不能覆盖当前图片或重开窗口。
                        page.locator('#asset-viewer-next').click(); page.wait_for_timeout(80)
                        stale = [r for edge, r in pending if edge != 420][-1]
                        page.locator('#asset-viewer-next').click(); page.wait_for_timeout(80)
                        title = page.locator('#asset-viewer-title').inner_text()
                        try: stale.fulfill(body=large.getvalue(), content_type='image/jpeg')
                        except Error: pass  # 浏览器已取消的请求可能拒绝应答。
                        self.assertEqual(page.locator('#asset-viewer-title').inner_text(), title)
                        self.assertTrue(page.locator('#asset-viewer-image').is_hidden())
                        release_full()
                        page.wait_for_function('()=>document.getElementById("asset-viewer-stage").dataset.previewState==="ready"')
                        low_url = page.locator('#asset-viewer-thumbnail').get_attribute('src')
                        late_small = next(r for edge, r in pending if edge == 420 and r.request.url == low_url)
                        page.keyboard.press('Escape'); page.wait_for_timeout(120)
                        self.assertFalse(page.locator('#asset-image-viewer').evaluate('e=>e.open'))
                        try: late_small.fulfill(body=small.getvalue(), content_type='image/jpeg')
                        except Error: pass
                        pending[:] = [(edge, route) for edge, route in pending if route is not late_small]
                        page.wait_for_timeout(40)
                        self.assertIsNone(page.locator('#asset-viewer-image').get_attribute('src'))
                        self.assertFalse(page.locator('#asset-image-viewer').evaluate('e=>e.open'))
                        # 网格恢复，加载完成的可见小图再次打开时不再请求小图。
                        release(420)
                        page.wait_for_function('()=>document.querySelector("#catalog-grid img[data-preview-done=ready]")')
                        cached = page.locator('#catalog-grid img[data-preview-done=ready]').first
                        selected_url = cached.evaluate('e=>e.currentSrc')
                        prior = len([r for r in requests if r[0] == 420 and r[1] == selected_url])
                        cached.dblclick(); page.wait_for_timeout(120)
                        self.assertIn(page.locator('#asset-viewer-stage').get_attribute('data-preview-state'), ['preview', 'ready'])
                        self.assertTrue(page.locator('#asset-viewer-thumbnail').get_attribute('src').startswith('data:image/'))
                        self.assertEqual(len([r for r in requests if r[0] == 420 and r[1] == selected_url]), prior)
                        page.emulate_media(reduced_motion='reduce')
                        self.assertEqual(page.locator('#asset-viewer-loading .asset-loading-orbit').evaluate('e=>getComputedStyle(e).animationName'), 'none')
                        if page.locator('#asset-viewer-stage').get_attribute('data-preview-state') != 'ready':
                            release_full()
                        page.wait_for_function('()=>document.getElementById("asset-viewer-stage").dataset.previewState==="ready"')
                        self.assertEqual(page.locator('#asset-viewer-image').evaluate('e=>getComputedStyle(e).animationName'), 'none')
                        page.keyboard.press('Escape')
                        self.assertFalse(errors, errors)
                    finally:
                        for _, route in pending:
                            try: route.abort()
                            except Error: pass
                        browser.close()
            finally:
                http.shutdown(); http.server_close(); archive.close()

    def test_grid_retries_after_transient_preview_failure(self):
        import server
        thumbnail=BytesIO();Image.new('RGB',(420,280),'#648b88').save(thumbnail,format='JPEG')
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            base=Path(folder);photos=base/'photos';photos.mkdir();Image.new('RGB',(60,40),'#648b88').save(photos/'retry.jpg')
            store=Store(base/'data',ROOT);archive=AssetArchive(store,autostart=False);catalog=AssetCatalog(store.data);catalog.add_root(str(photos))
            for key,value in dict(STORE=store,DATA=store.data,ARCHIVE=archive,CATALOG=catalog,
                                  MODELS=Providers(store.data),SKU=None,WATCH=None,MOUSE=None,
                                  RAWLINK=None,FILESEARCH=None,UPDATES=None,ANALYSIS=None).items():stack.enter_context(patch.object(server,key,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':900});calls=[];errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        def preview(route):
                            calls.append(route.request.url)
                            if len(calls)==1:route.abort()
                            else:route.fulfill(body=thumbnail.getvalue(),content_type='image/jpeg')
                        page.route('**/api/catalog/preview/**',preview)
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                        page.wait_for_function('()=>document.querySelector("#catalog-grid img[data-preview-done=\\"ready\\"]")',timeout=8000)
                        self.assertEqual(len(calls),2)
                        self.assertEqual(page.locator('#catalog-grid img[data-preview-done=error]').count(),0)
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close();archive.close()


if __name__ == '__main__': unittest.main()
