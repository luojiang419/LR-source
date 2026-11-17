"""分屏参考区纯图全屏、固定参考切换、缩放平移与动效联测。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

from reference_library import ReferenceLibrary
from test_reference_library import available_port
import test_full_auto_ocr as fixture

ROOT = Path(__file__).resolve().parents[1]


class ReferenceFullscreenBrowserTests(unittest.TestCase):
    setUp = fixture.FullAutoWorkflowTests.setUp
    tearDown = fixture.FullAutoWorkflowTests.tearDown

    def test_fullscreen_selected_references_zoom_pan_restore_and_motion(self):
        import server
        local = ReferenceLibrary(self.base/'reference-local')
        remote = ReferenceLibrary(self.base/'reference-remote')
        http = None
        try:
            photos = self.base/'references';photos.mkdir()
            for index, name in enumerate(('A-local.JPG', 'B-search-only.JPG', 'C-api.JPG')):
                image = Image.new('RGB', (1400, 950), (95+index*35, 120, 155-index*20))
                draw = ImageDraw.Draw(image)
                draw.rectangle((160, 130, 1240, 820), outline=(225, 210, 180), width=12)
                draw.ellipse((440, 210, 960, 740), fill=(165, 150+index*20, 110))
                draw.line((700, 0, 700, 950), fill=(235, 225, 200), width=5)
                image.save(photos/name)
            local_root = local.add_root(photos, '本机参考')['root_id']
            remote_root = remote.add_root(photos, 'API 参考')['root_id']
            deadline = time.monotonic()+10
            while any(library._root(root)['status']!='ready' for library, root in ((local, local_root), (remote, remote_root))):
                self.assertLess(time.monotonic(), deadline);time.sleep(.02)
            remote.configure_sharing(True, available_port())
            key = remote.create_key('全屏验收', [remote_root])
            peer = local.add_peer('API 设备', remote.public_config()['endpoint'], key['api_key'])['id']
            with ExitStack() as stack:
                for name, value in dict(STORE=self.store, DATA=self.store.data, SKU=self.sku, MODELS=self.providers,
                    ARCHIVE=self.archive, REFERENCES=local, MOUSE=None, WATCH=None, ANALYSIS=None,
                    RAWLINK=None, FILESEARCH=None, COMPONENTS=None, DRIVER=None).items():
                    stack.enter_context(patch.object(server, name, value))
                http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler);http.daemon_threads = True
                threading.Thread(target=http.serve_forever, daemon=True).start()
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                    try:
                        page = browser.new_page(viewport={'width':1680, 'height':1050}, reduced_motion='reduce')
                        errors, queries = [], []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.on('request', lambda request: queries.append(request.url) if '/api/reference/library/items?' in request.url or '/api/reference/search?' in request.url else None)
                        page.goto(f'http://127.0.0.1:{http.server_port}', wait_until='domcontentloaded')
                        page.wait_for_selector('#split-preview-grid [data-row]', state='attached')
                        page.locator('#view-open').click();page.locator('#layout-split').click();page.keyboard.press('Escape')
                        grid = page.locator('#split-preview-grid');stage = page.locator('#split-reference-stage')
                        image = page.locator('#split-reference-image');reference = page.locator('#split-reference')
                        grid.locator('[data-row]').filter(has=page.locator('img[alt="A-local.JPG"]')).locator('[data-pin-row]').click()
                        root = page.locator(f'[data-resource-root][data-device="{peer}"]')
                        root.click()
                        with page.expect_response('**/api/reference/library/items?*'):
                            root.click()
                        grid.locator('[data-row]').filter(has=page.locator('img[alt="C-api.JPG"]')).locator('[data-pin-row]').click()
                        grid.locator('[data-row]').filter(has=page.locator('img[alt="B-search-only.JPG"]')).click()
                        self.assertEqual(page.locator('#split-reference-count').inner_text(), '2')
                        # 保留关键词、目录展开状态、底部面板状态及拍摄照片。
                        with page.expect_response('**/api/reference/search?*'):
                            page.locator('#split-search').fill('JPG')
                        page.wait_for_function('()=>document.getElementById("split-results").textContent.startsWith("6 张")')
                        grid.locator('[data-row]').filter(has=page.locator('img[alt="B-search-only.JPG"]')).first.click()
                        page.locator('#split-reference-tray').evaluate('(e)=>e.open=false')
                        flags = reference.get_attribute('class') or ''
                        selected = page.evaluate('Framekeep.getSnapshot().selectedId')
                        right = page.locator('#selection-page>.library-layout').bounding_box()
                        before_queries = len(queries)
                        page.locator('#split-reference-focus').click()
                        page.wait_for_function('()=>{const e=document.getElementById("split-reference-image");return e.alt==="C-api.JPG"&&e.complete&&e.naturalWidth>0}')
                        self.assertTrue(reference.evaluate('(e)=>e.classList.contains("reference-fullscreen")'))
                        self.assertEqual(page.locator('#selection-page>.library-layout').bounding_box(), right)
                        ref_box, stage_box = reference.bounding_box(), stage.bounding_box()
                        for dimension in ('x', 'y', 'width', 'height'):
                            self.assertAlmostEqual(ref_box[dimension], stage_box[dimension], delta=1.1)
                        for selector in ('.split-resource-dock', '.split-preview-heading', '.split-result-toolbar', '#split-index-overview', '.split-reference-caption', '#split-reference-tray', '#split-pin-large', '#split-reference-prev', '#split-reference-next', '#split-zoom-reset'):
                            self.assertFalse(reference.locator(selector).is_visible(), selector)
                        self.assertEqual(image.evaluate('(e)=>e.getAnimations().length'), 0)
                        # 鼠标移到右侧后，仍在全屏参考集合内切换，不切拍摄照片。
                        page.mouse.move(right['x']+right['width']/2, right['y']+right['height']/2)
                        page.keyboard.press('ArrowLeft')
                        page.wait_for_function('()=>document.getElementById("split-reference-image").alt==="A-local.JPG"')
                        self.assertIn('device=local', image.get_attribute('src'))
                        page.keyboard.press('ArrowLeft')
                        self.assertEqual(image.get_attribute('alt'), 'A-local.JPG')
                        page.keyboard.press('ArrowRight')
                        page.wait_for_function('()=>{const e=document.getElementById("split-reference-image");return e.alt==="C-api.JPG"&&e.complete&&e.naturalWidth>0}')
                        self.assertIn('device='+peer, image.get_attribute('src'))
                        page.keyboard.press('ArrowRight')
                        self.assertEqual(image.get_attribute('alt'), 'C-api.JPG')
                        self.assertEqual(page.evaluate('Framekeep.getSnapshot().selectedId'), selected)
                        stage.hover();page.mouse.wheel(0, -900)
                        matrix = image.evaluate('(e)=>{const m=new DOMMatrix(getComputedStyle(e).transform);return [m.a,m.e,m.f]}')
                        self.assertGreater(matrix[0], 2)
                        self.assertEqual(image.get_attribute('alt'), 'C-api.JPG')
                        box = stage.bounding_box();cx, cy = box['x']+box['width']/2, box['y']+box['height']/2
                        page.mouse.move(cx, cy);page.mouse.down();page.mouse.move(cx+60, cy+35)
                        self.assertEqual(image.evaluate('(e)=>getComputedStyle(e).transitionDuration'), '0s')
                        moved = image.evaluate('(e)=>{const m=new DOMMatrix(getComputedStyle(e).transform);return [m.e,m.f]}')
                        self.assertGreater(moved[0], matrix[1]+40);self.assertGreater(moved[1], matrix[2]+20)
                        stage.dispatch_event('pointercancel', {'pointerId':1})
                        self.assertFalse(stage.evaluate('(e)=>e.classList.contains("is-dragging")||e.hasPointerCapture(1)'))
                        page.mouse.up()
                        self.assertEqual(len(queries), before_queries)
                        output = ROOT/'build/reference-fullscreen';output.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(output/'fullscreen-zoom.png'))
                        page.keyboard.press('Escape')
                        self.assertEqual(reference.get_attribute('class'), flags)
                        self.assertTrue(grid.is_visible());self.assertTrue(page.locator('#split-search').is_visible())
                        self.assertEqual(page.locator('#split-search').input_value(), 'JPG')
                        self.assertFalse(page.locator('#split-reference-tray').evaluate('(e)=>e.open'))
                        self.assertEqual(page.locator('#selection-page>.library-layout').bounding_box(), right)
                        self.assertEqual(page.locator('#split-reference-count').inner_text(), '2')
                        # 从大图进入可恢复原缩放；动画采用项目贝塞尔曲线。
                        page.locator('#split-reference-tray').evaluate('(e)=>e.open=true')
                        page.locator('[data-show-reference="0"]').click()
                        page.wait_for_function('()=>{const e=document.getElementById("split-reference-image");return e.complete&&e.naturalWidth>0}')
                        stage.hover();page.keyboard.down('Control');page.mouse.wheel(0,-120);page.keyboard.up('Control')
                        self.assertEqual(page.locator('#split-zoom-reset').inner_text(), '110%')
                        page.emulate_media(reduced_motion='no-preference')
                        page.locator('#split-reference-focus').click()
                        page.wait_for_function('()=>document.getElementById("split-reference-image").getAnimations().some(a=>a.effect.getTiming().easing==="cubic-bezier(0.22, 1, 0.36, 1)")')
                        page.keyboard.press('Escape')
                        page.wait_for_function('()=>!document.getElementById("split-reference").classList.contains("reference-transitioning")')
                        self.assertTrue(stage.is_visible());self.assertEqual(page.locator('#split-zoom-reset').inner_text(), '110%')
                        # 双击和快速退出不残留动画，四主题及窄屏均保持区域边界。
                        for theme in ('dark', 'light', 'pure-white', 'hardcore'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)', theme)
                            page.locator('#split-reference-focus').click()
                            page.keyboard.press('Escape')
                            stage.dblclick()
                            page.wait_for_function('()=>!document.getElementById("split-reference").classList.contains("reference-transitioning")')
                            self.assertTrue(reference.evaluate('(e)=>e.classList.contains("reference-fullscreen")'))
                            self.assertEqual(image.evaluate('(e)=>getComputedStyle(e).scale'), 'none')
                            page.screenshot(path=str(output/f'{theme}-fullscreen.png'))
                            for width in (1024, 375, 320):
                                page.set_viewport_size({'width':width, 'height':1050})
                                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'), (theme, width))
                                self.assertAlmostEqual(stage.bounding_box()['width'], reference.bounding_box()['width'], delta=1.1)
                            page.set_viewport_size({'width':1680, 'height':1050})
                            page.keyboard.press('Escape')
                        page.emulate_media(reduced_motion='reduce')
                        # 进入全屏时仍在途的查询不能盖住参考图，退出后应补齐结果。
                        held = []
                        def hold_search(route):
                            if not held:
                                held.append(route)
                            else:
                                route.continue_()
                        page.route('**/api/reference/search?*', hold_search)
                        with page.expect_request('**/api/reference/search?*'):
                            page.locator('#split-search').fill('A-local')
                        page.wait_for_timeout(30)
                        page.locator('#split-reference-focus').click()
                        held[0].fulfill(json=local.search_all('A-local'))
                        self.assertTrue(reference.evaluate('(e)=>e.classList.contains("reference-fullscreen")'))
                        with page.expect_response('**/api/reference/search?*'):
                            page.keyboard.press('Escape')
                        page.wait_for_function('()=>document.getElementById("split-results").textContent.startsWith("2 张")')
                        # 临时加载图片也在同一个参考集合里切换。
                        with page.expect_file_chooser() as chooser:
                            page.locator('#split-import').click()
                        chooser.value.set_files(str(photos/'B-search-only.JPG'))
                        page.locator('#split-reference-focus').click()
                        blob = image.get_attribute('src');self.assertTrue(blob.startswith('blob:'))
                        page.keyboard.press('ArrowLeft')
                        self.assertEqual(image.get_attribute('alt'), 'C-api.JPG')
                        page.keyboard.press('ArrowRight')
                        self.assertEqual(image.get_attribute('src'), blob)
                        page.keyboard.press('Escape')
                        page.locator('#split-reference-focus').click()
                        page.evaluate('()=>FramekeepIO.flush()')
                        page.reload();page.wait_for_selector('#split-search', state='visible')
                        self.assertFalse(reference.evaluate('(e)=>e.classList.contains("reference-fullscreen")'))
                        self.assertEqual(page.locator('#split-reference-count').inner_text(), '2')
                        page.locator('#split-reference-focus').click()
                        page.locator('#split-exit').click()
                        self.assertFalse(reference.evaluate('(e)=>e.classList.contains("reference-fullscreen")'))
                        self.assertFalse(errors, errors)
                    finally:
                        browser.close()
        finally:
            if http:http.shutdown();http.server_close()
            local.close();remote.close()


if __name__=='__main__':unittest.main()
