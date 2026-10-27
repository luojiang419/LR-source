"""隔离验收选片空态尺寸、目录/联机切换和首张图入库。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
import media_engine as media

ROOT = Path(__file__).resolve().parents[1]


class PreviewEmptyTests(unittest.TestCase):
    def test_empty_modes_fill_stage_and_first_capture_replaces_animation(self):
        import server
        out = ROOT / 'build/preview-empty-acceptance'
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            photos = base / 'photos'
            photos.mkdir()
            store = Store(base / 'data', ROOT)
            sid = store.ensure_live_session(photos)
            store.activate(sid)
            patches = [patch.object(server, k, v) for k, v in [
                ('STORE', store), ('DATA', store.data), ('WATCH', None), ('MODELS', None),
                ('MOUSE', None), ('SKU', None), ('ARCHIVE', None), ('ANALYSIS', None),
                ('RAWLINK', None), ('FILESEARCH', None), ('CANON', None), ('CATALOG', None)]]
            for p in patches:
                p.start()
            http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            http.daemon_threads = True
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                    try:
                        page = browser.new_page(viewport={'width': 1680, 'height': 1050}, reduced_motion='reduce')
                        errors = []
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}', wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        self.assertTrue(page.locator('#sku-asset-panel').evaluate('(el)=>el.open&&el.previousElementSibling.classList.contains("rating-section")'))
                        self.assertIsNone(page.locator('#photo-stage').get_attribute('title'))
                        for theme in ['dark', 'light', 'pure-white', 'hardcore']:
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)', theme)
                            for width, height in [(1680, 1050), (1024, 768), (600, 800)]:
                                page.set_viewport_size({'width': width, 'height': height})
                                for view in ['grid', 'list', 'single']:
                                    page.locator(f'[data-view="{view}"]').click()
                                    empty = page.locator('.preview-empty:visible')
                                    self.assertEqual(empty.count(), 1)
                                    self.assertEqual(empty.get_attribute('data-empty-state'), 'folder')
                                    page.wait_for_function('()=>document.querySelector(".preview-empty:not(.hidden) img").naturalWidth>0')
                                    stage = page.locator('#photo-stage' if view == 'single' else '#catalog-view').bounding_box()
                                    bounds = empty.bounding_box()
                                    self.assertGreater(bounds['width'], stage['width'] * .9)
                                    self.assertGreater(bounds['height'], stage['height'] * .85)
                                    art = empty.locator('img').bounding_box()
                                    self.assertAlmostEqual(art['x'] + art['width'] / 2, stage['x'] + stage['width'] / 2, delta=2)
                                    self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'))
                                    self.assertFalse(page.locator('.thumbnail-control').is_visible())
                                if width == 1680:
                                    page.screenshot(path=str(out / f'empty-{theme}.png'))
                        page.set_viewport_size({'width': 1680, 'height': 1050})
                        page.evaluate('Framekeep.setSessionLoading(true)')
                        self.assertEqual(page.locator('#photos .preview-empty').get_attribute('data-empty-state'), 'loading')
                        self.assertTrue(page.locator('#photos .preview-empty-art').evaluate('(img)=>img.currentSrc.endsWith("camera-transfer-still.svg")'))
                        page.emulate_media(reduced_motion='no-preference')
                        page.wait_for_function('()=>document.querySelector("#photos .preview-empty-art").currentSrc.endsWith("camera-transfer.svg")')
                        page.emulate_media(reduced_motion='reduce')
                        page.evaluate('Framekeep.setSessionLoading(false);Framekeep.setMonitoring(true)')
                        self.assertEqual(page.locator('#photos .preview-empty').get_attribute('data-empty-state'), 'waiting')
                        self.assertTrue(page.locator('#previous').is_disabled())
                        page.screenshot(path=str(out / 'waiting.png'))
                        # 真正向隔离索引加入首张照片，再走现有实时更新入口。
                        path = photos / 'FIRST.JPG'
                        Image.new('RGB', (1200, 800), '#466879').save(path)
                        store.append_live(sid, [media.capture_record(photos, path, None, {'ImageWidth': 1200, 'ImageHeight': 800})])
                        # 由真实 state API 获取前端目录结构（含预览 URL）。
                        state = page.evaluate('async()=>await (await fetch("/api/state")).json()')
                        page.evaluate('(s)=>Framekeep.updateLiveCatalog(s.session,s.catalog,s.catalog[0].id,true)', state)
                        page.wait_for_function('()=>document.querySelector("#photos img")?.naturalWidth>0')
                        self.assertEqual(page.locator('#photos>.photo-frame').count(), 1)
                        self.assertEqual(page.locator('#photos .preview-empty').count(), 0)
                        page.locator('.preview-surface').hover()
                        self.assertEqual(page.locator('#photo-stage [title]').count(),0)
                        self.assertTrue(page.locator('.thumbnail-control').is_hidden());page.locator('#app-settings-open').click();page.locator('[data-settings-section=preview]').click();page.locator('#preview-zoom-position').select_option('top-left');page.locator('#app-settings-close').click();page.locator('.preview-surface').hover();self.assertTrue(page.locator('.thumbnail-control').is_visible())
                        page.screenshot(path=str(out / 'first-photo.png'))
                        # 带筛选的非空目录应提供恢复操作，不能误报联机等待。
                        page.evaluate('Framekeep.setMonitoring(false)')
                        page.locator('#search').fill('NO-MATCH')
                        self.assertEqual(page.locator('#photos .preview-empty').get_attribute('data-empty-state'), 'filtered')
                        page.locator('#photos [data-empty-clear]').click()
                        self.assertEqual(page.locator('#photos>.photo-frame').count(), 1)
                        page.locator('[data-view="grid"]').click()
                        page.locator('#search').fill('NO-MATCH')
                        page.locator('#catalog-empty [data-empty-clear]').click()
                        self.assertEqual(page.locator('#photo-grid .photo-card').count(), 1)
                        # 外部 SVG 的减少动态效果偏好也由浏览器验证。
                        response = page.request.get(f'http://127.0.0.1:{http.server_port}/assets/camera-transfer.svg')
                        self.assertTrue(response.ok)
                        svg_page = browser.new_page(reduced_motion='reduce')
                        svg_page.goto(f'http://127.0.0.1:{http.server_port}/assets/camera-transfer.svg');svg_page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        self.assertEqual(svg_page.locator('.packet').first.evaluate('(el)=>getComputedStyle(el).animationName'), 'none')
                        svg_page.close()
                        self.assertFalse(errors, errors)
                    finally:
                        browser.close()
            finally:
                http.shutdown()
                http.server_close()
                for p in reversed(patches):
                    p.stop()


if __name__ == '__main__':
    unittest.main()
