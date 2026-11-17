"""参考即时搜索、防抖、结果网格、大图浏览及固定参考联测。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image
from playwright.sync_api import sync_playwright

from reference_library import ReferenceLibrary
import test_full_auto_ocr as fixture
from test_reference_library import available_port

ROOT = Path(__file__).resolve().parents[1]


class ReferenceBrowserTests(unittest.TestCase):
    setUp = fixture.FullAutoWorkflowTests.setUp
    tearDown = fixture.FullAutoWorkflowTests.tearDown

    def test_debounce_composition_stale_response_and_task_switch_preserve_reference(self):
        import server
        library = ReferenceLibrary(self.base/'debounced-reference')
        http = None
        try:
            photos = self.base/'references';photos.mkdir()
            for name in ('Look 000.JPG', 'Look 001.JPG', 'Other.JPG'):
                Image.new('RGB', (240, 180), (40, 70, 160)).save(photos/name)
            root = library.add_root(photos)['root_id']
            deadline = time.monotonic()+10
            while library._root(root)['status']!='ready':
                self.assertLess(time.monotonic(), deadline);time.sleep(.02)
            with ExitStack() as stack:
                for name, value in dict(STORE=self.store, DATA=self.store.data, SKU=self.sku, MODELS=self.providers,
                    ARCHIVE=self.archive, REFERENCES=library, MOUSE=None, WATCH=None, ANALYSIS=None,
                    RAWLINK=None, FILESEARCH=None, COMPONENTS=None, DRIVER=None).items():
                    stack.enter_context(patch.object(server, name, value))
                http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler);http.daemon_threads = True
                threading.Thread(target=http.serve_forever, daemon=True).start()
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                    try:
                        page = browser.new_page(viewport={'width':1680, 'height':1050}, reduced_motion='reduce')
                        queries, errors = [], []
                        page.on('request', lambda request: queries.append(request.url) if '/api/reference/library/items?' in request.url else None)
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}', wait_until='domcontentloaded')
                        page.wait_for_selector('#split-preview-grid [data-row]', state='attached')
                        page.locator('#view-open').click();page.locator('#layout-split').click();page.keyboard.press('Escape')
                        search = page.locator('#split-search');grid = page.locator('#split-preview-grid')
                        with page.expect_response('**/api/reference/library/items?*'):
                            page.locator('#split-search-range').select_option('current')
                        page.wait_for_function('()=>document.getElementById("split-results").textContent.startsWith("3 张")')
                        # 组合输入期间停顿超过防抖时长也不能提交未选定的文字。
                        before = len(queries)
                        search.evaluate('''e=>{e.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));
                            e.value='Look';e.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:true,data:'Look'}));}''')
                        page.wait_for_timeout(450)
                        self.assertEqual(len(queries), before)
                        with page.expect_response('**/api/reference/library/items?*'):
                            search.dispatch_event('compositionend')
                        page.wait_for_function('()=>document.querySelectorAll("#split-preview-grid [data-row]").length===2')
                        self.assertEqual(len(queries), before+1)
                        # 搜索按钮立即提交并取消尚未执行的防抖请求。
                        before = len(queries)
                        with page.expect_response('**/api/reference/library/items?*'):
                            search.fill('');page.locator('#split-search-submit').click()
                        page.wait_for_timeout(450)
                        self.assertEqual(len(queries), before+1)
                        self.assertEqual(grid.locator('[data-row]').count(), 3)
                        # 回车立即搜索并取消尚未执行的防抖请求。
                        before = len(queries)
                        with page.expect_response('**/api/reference/library/items?*'):
                            search.fill('Other');search.press('Enter')
                        page.wait_for_timeout(450)
                        self.assertEqual(len(queries), before+1)
                        self.assertEqual(grid.locator('[data-row] img').get_attribute('alt'), 'Other.JPG')
                        held = []
                        def delay_old_query(route):
                            if parse_qs(urlsplit(route.request.url).query).get('q')==['Look 000']:
                                held.append(route)
                            else:
                                route.continue_()
                        page.route('**/api/reference/library/items?*', delay_old_query)
                        with page.expect_request(lambda request: parse_qs(urlsplit(request.url).query).get('q')==['Look 000']):
                            search.fill('Look 000')
                        page.wait_for_timeout(50);self.assertEqual(len(held), 1)
                        search.fill('Look 001')
                        held.pop().fulfill(status=200, content_type='application/json', body=json.dumps(library.items(root, text='Look 000')))
                        page.wait_for_timeout(80)
                        self.assertEqual(grid.locator('[data-row] img').get_attribute('alt'), 'Other.JPG')
                        page.wait_for_function('()=>document.querySelector("#split-preview-grid [data-row] img").alt==="Look 001.JPG"')
                        grid.locator('[data-pin-row]').click()
                        # 临时本机图片与固定参考都应跨拍摄目录和文档保留。
                        with page.expect_file_chooser() as chooser:
                            page.locator('#split-import').click()
                        chooser.value.set_files(str(photos/'Other.JPG'))
                        page.wait_for_function('()=>document.getElementById("split-reference-count").textContent==="2"')
                        preview = page.locator('#split-reference-image');source = preview.get_attribute('src')
                        self.assertTrue(source.startswith('blob:'))
                        page.locator('#split-reference-tray').evaluate('(e)=>e.open=false')
                        page.evaluate('()=>FramekeepIO.flush()')
                        next_photos = self.base/'next-shoot';next_photos.mkdir()
                        sid = self.store.ensure_live_session(next_photos);self.store.activate(sid)
                        with page.expect_response('**/api/reference/task'):
                            page.evaluate('(id)=>{FRAMEKEEP_SESSION.id=id;document.dispatchEvent(new CustomEvent("framekeep:selection"))}', sid)
                        self.assertEqual(page.locator('#split-reference-count').inner_text(), '2')
                        self.assertEqual(preview.get_attribute('src'), source)
                        self.assertFalse(page.locator('#split-reference-tray').evaluate('(e)=>e.open'))
                        with patch.object(self.sku, 'project', return_value={**self.sku.project(), 'id':'another-project'}):
                            with page.expect_response('**/api/reference/task'):
                                page.evaluate('()=>{const original=FramekeepSku.shootingTypes;FramekeepSku.shootingTypes=()=>({...original(),project_id:"another-project"});document.dispatchEvent(new CustomEvent("framekeep:sku-project"));}')
                        self.assertEqual(page.locator('#split-reference-count').inner_text(), '2')
                        self.assertEqual(preview.get_attribute('src'), source)
                        self.assertEqual(len(library.preferences()['references']), 1)
                        output = ROOT/'build/split-search-reference';output.mkdir(parents=True, exist_ok=True)
                        page.locator('#split-reference-tray').evaluate('(e)=>e.open=true')
                        page.screenshot(path=str(output/'retained-references.png'))
                        self.assertFalse(errors, errors)
                    finally:
                        browser.close()
        finally:
            if http:http.shutdown();http.server_close()
            library.close()

    def test_search_grid_single_pinning_zoom_and_local_remote_requests(self):
        import server
        local = ReferenceLibrary(self.base/'reference-local')
        remote = ReferenceLibrary(self.base/'reference-remote')
        http = None
        try:
            for index in range(65):
                folder=self.photos/('系列 A' if index<33 else '系列 B');folder.mkdir(exist_ok=True)
                Image.new('RGB', (240, 180), (35+index*3, 70+index*2, 160)).save(folder/f'Look {index:03d}.JPG')
            local_root = local.add_root(self.photos, '本机参考')['root_id']
            remote_root = remote.add_root(self.photos, 'API 参考')['root_id']
            deadline = time.monotonic()+10
            while any(library._root(root)['status']!='ready' for library, root in ((local, local_root), (remote, remote_root))):
                self.assertLess(time.monotonic(), deadline, '参考索引超时')
                time.sleep(.02)
            remote.configure_sharing(True, available_port())
            key = remote.create_key('浏览器验收', [remote_root])
            peer = local.add_peer('API 设备', remote.public_config()['endpoint'], key['api_key'])['id']
            with ExitStack() as stack:
                reveal = stack.enter_context(patch('file_reveal.reveal_path'))
                services = dict(STORE=self.store, DATA=self.store.data, SKU=self.sku, MODELS=self.providers,
                                ARCHIVE=self.archive, REFERENCES=local, MOUSE=None, WATCH=None, ANALYSIS=None,
                                RAWLINK=None, FILESEARCH=None, COMPONENTS=None, DRIVER=None)
                for name, value in services.items():
                    stack.enter_context(patch.object(server, name, value))
                http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
                http.daemon_threads = True
                threading.Thread(target=http.serve_forever, daemon=True).start()
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                    page = browser.new_page(viewport={'width':1680, 'height':1050}, reduced_motion='reduce')
                    queries, previews, errors = [], [], []
                    def requested(request):
                        path = urlsplit(request.url)
                        if path.path=='/api/reference/library/items':
                            queries.append(parse_qs(path.query, keep_blank_values=True))
                        elif path.path=='/api/reference/library/preview':
                            previews.append(parse_qs(path.query))
                    page.on('request', requested)
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{http.server_port}', wait_until='domcontentloaded')
                    page.wait_for_selector('#split-preview-grid [data-row]', state='attached')
                    page.locator('#view-open').click();page.locator('#layout-split').click();page.keyboard.press('Escape')
                    selected = page.evaluate('Framekeep.getSnapshot().selectedId')
                    grid = page.locator('#split-preview-grid')
                    search = page.locator('#split-search')
                    self.assertEqual(page.locator('#split-search-range').input_value(),'all')
                    page.locator('#split-search-range').select_option('current')
                    for device in ('local', peer):
                        if device!='local':
                            root_button = page.locator(f'[data-resource-root][data-device="{device}"]')
                            root_button.click()
                            with page.expect_response(lambda response: '/api/reference/library/items?' in response.url):
                                root_button.click()
                        before = len(queries)
                        with page.expect_response(lambda response: '/api/reference/library/items?' in response.url):
                            search.press_sequentially('Look', delay=100)
                            self.assertEqual(len(queries), before)
                        page.wait_for_function('()=>document.querySelectorAll("#split-preview-grid [data-row]").length===60')
                        self.assertEqual(len(queries), before+1)
                        self.assertTrue(grid.is_visible())
                        self.assertEqual(grid.locator('.split-folder-divider>span').all_text_contents(), ['系列 A','系列 B'])
                        self.assertEqual(queries[-1]['device'], [device])
                        if device=='local':
                            self.assertFalse(any(request.get('size')==['1800'] for request in previews))
                        before = len(queries)
                        card = grid.locator('[data-row="1"]')
                        expected = card.locator('img').get_attribute('alt')
                        card.click(button='right')
                        if device=='local':
                            self.assertTrue(page.locator('#split-context').is_visible())
                            with page.expect_response(lambda response: response.url.endswith('/api/reference/reveal')):
                                page.locator('#split-context-reveal').click()
                            reveal.assert_called_once_with(self.photos/'系列 A'/expected)
                        else:
                            self.assertFalse(page.locator('#split-context').is_visible())
                        page.locator('#split-reference-tray').evaluate('(e)=>e.open=false')
                        card.locator('[data-pin-row]').click()
                        self.assertTrue(page.locator('#split-reference-tray').evaluate('(e)=>e.open'))
                        self.assertIn(expected, page.locator('#split-reference-thumbnails').inner_text())
                        # 已固定其他图片时，双击仍显示实际点中的搜索结果。
                        target = grid.locator('[data-row="2"]')
                        target_name = target.locator('img').get_attribute('alt')
                        target.dblclick()
                        self.assertTrue(page.locator('#split-reference-stage').is_visible())
                        self.assertEqual(page.locator('#split-reference-image').get_attribute('alt'), target_name)
                        self.assertIn('size=1800', page.locator('#split-reference-image').get_attribute('src'))
                        self.assertTrue(page.locator('#split-fade-control').is_visible())
                        page.locator('#split-fade-curve').select_option('linear')
                        stage=page.locator('#split-reference-stage');stage.hover();page.mouse.wheel(0,120)
                        page.wait_for_function('(name)=>document.getElementById("split-reference-image").alt!==name',arg=target_name)
                        self.assertEqual(page.locator('#split-zoom-reset').inner_text(),'适应')
                        name=page.locator('#split-reference-image').get_attribute('alt')
                        page.keyboard.down('Control');page.mouse.wheel(0,-120);page.keyboard.up('Control')
                        page.wait_for_function('()=>document.getElementById("split-zoom-reset").textContent==="110%"')
                        self.assertEqual(page.locator('#split-reference-image').get_attribute('alt'),name)
                        page.locator('#split-zoom-reset').click();stage.focus()
                        page.emulate_media(reduced_motion='no-preference')
                        page.wait_for_function('()=>{const e=document.getElementById("split-reference-image");return e.complete&&e.naturalWidth>0}')
                        for curve,easing in (('smooth','ease-in-out'),('linear','linear'),('easeIn','ease-in'),('easeOut','ease-out')):
                            page.locator('#split-fade-curve').select_option(curve);stage.press('ArrowRight')
                            page.wait_for_function('(easing)=>document.getElementById("split-reference-image").getAnimations().some(a=>a.effect.getTiming().easing===easing)',arg=easing)
                            page.wait_for_function('()=>document.getElementById("split-reference-outgoing").hidden')
                        page.locator('#split-fade-curve').select_option('none');stage.press('ArrowRight')
                        page.wait_for_function('()=>{const e=document.getElementById("split-reference-image");return e.complete&&e.naturalWidth>0}')
                        self.assertEqual(page.locator('#split-reference-image').evaluate('(e)=>e.getAnimations().length'),0)
                        page.locator('#split-fade-curve').select_option('linear')
                        for _ in range(5):stage.press('ArrowRight')
                        page.wait_for_function('()=>{const e=document.getElementById("split-reference-image");return e.complete&&e.naturalWidth>0&&document.getElementById("split-reference-outgoing").hidden}')
                        page.emulate_media(reduced_motion='reduce')
                        page.keyboard.press('ArrowRight')
                        self.assertNotEqual(page.locator('#split-reference-image').get_attribute('alt'), target_name)
                        page.keyboard.press('Escape')
                        self.assertTrue(grid.is_visible())
                        page.keyboard.press('ArrowRight')
                        self.assertEqual(page.evaluate('Framekeep.getSnapshot().selectedId'), selected)
                        self.assertEqual(len(queries), before)
                        grid.hover();page.mouse.wheel(0, 360)
                        page.wait_for_function('()=>document.getElementById("split-preview-grid").scrollTop>0')
                        self.assertEqual(page.locator('#split-grid-scale-value').inner_text(),'100%')
                        page.keyboard.down('Control');page.mouse.wheel(0, -120);page.keyboard.up('Control')
                        page.wait_for_function('()=>document.getElementById("split-grid-scale-value").textContent==="110%"')
                        # 使用同一个滑杆事件检查上下限，并验证滚轮不会越界。
                        page.locator('#split-grid-scale').evaluate('(e)=>{e.value=400;e.dispatchEvent(new Event("input",{bubbles:true}))}')
                        grid.hover();page.keyboard.down('Control');page.mouse.wheel(0, -120);page.keyboard.up('Control')
                        self.assertEqual(page.locator('#split-grid-scale-value').inner_text(), '400%')
                        page.locator('#split-grid-scale').evaluate('(e)=>{e.value=10;e.dispatchEvent(new Event("input",{bubbles:true}))}')
                        grid.hover();page.keyboard.down('Control');page.mouse.wheel(0, 120);page.keyboard.up('Control')
                        page.wait_for_function('()=>document.getElementById("split-grid-scale-value").textContent==="10%"')
                        self.assertFalse(grid.locator('[data-row="0"]>span').is_visible())
                        self.assertEqual(len(queries), before)
                        page.locator('#split-grid-scale').evaluate('(e)=>{e.value=100;e.dispatchEvent(new Event("input",{bubbles:true}))}')
                        self.assertTrue(grid.locator('[data-row="0"]>span').is_visible())
                        # 防抖尚未到期时翻页，先按新条件查询第一页。
                        with page.expect_response(lambda response: '/api/reference/library/items?' in response.url):
                            search.fill('Look 06');page.locator('#split-result-next').click()
                        page.wait_for_function('()=>document.querySelectorAll("#split-preview-grid [data-row]").length===5')
                        self.assertEqual(queries[-1]['q'], ['Look 06'])
                        self.assertEqual(queries[-1]['offset'], ['0'])
                        with page.expect_response(lambda response: '/api/reference/library/items?' in response.url):
                            search.fill('Look')
                        page.wait_for_function('()=>document.querySelectorAll("#split-preview-grid [data-row]").length===60')
                        with page.expect_response(lambda response: '/api/reference/library/items?' in response.url):
                            page.locator('#split-result-next').click()
                        self.assertEqual(queries[-1]['q'], ['Look'])
                        page.wait_for_function('()=>document.querySelectorAll("#split-preview-grid [data-row]").length===5')
                        with page.expect_response(lambda response: '/api/reference/library/items?' in response.url):
                            search.fill('')
                        page.wait_for_function('()=>document.getElementById("split-result-page").textContent.startsWith("1 /")')
                        self.assertEqual(queries[-1]['q'], [''])
                        self.assertEqual(queries[-1]['offset'], ['0'])
                    page.locator('#split-search-range').select_option('all');search.fill('Look')
                    with page.expect_response('**/api/reference/search?*'):
                        page.locator('#split-search-submit').click()
                    page.wait_for_function('()=>document.getElementById("split-results").textContent.startsWith("130 张")')
                    self.assertEqual(grid.locator('[data-row]').count(),60)
                    self.assertIn('全部已添加目录',page.locator('#split-search-scope').inner_text())
                    # 全部范围下结果携带自己的来源，不能被当前选中的 API 目录覆盖。
                    first=grid.locator('[data-row="0"]');self.assertIn('device=local',first.locator('img').get_attribute('src'))
                    first.click(button='right');self.assertTrue(page.locator('#split-context-reveal').is_visible());page.keyboard.press('Escape')
                    first.locator('[data-pin-row]').click();page.wait_for_function('()=>document.getElementById("split-reference-count").textContent==="3"')
                    self.assertEqual(first.locator('[data-pin-row]').get_attribute('aria-pressed'),'true')
                    first.locator('[data-pin-row]').click();page.wait_for_function('()=>document.getElementById("split-reference-count").textContent==="2"')
                    first.dblclick();preview=page.locator('#split-reference-image');old_source=preview.get_attribute('src')
                    page.locator('#split-reference-stage').press('ArrowRight')
                    self.assertNotEqual(preview.get_attribute('src'),old_source)
                    page.locator('#split-reference-stage').press('Escape')
                    with page.expect_response('**/api/reference/library/items?*'):
                        page.locator('#split-search-range').select_option('current')
                    page.wait_for_function('()=>document.getElementById("split-search-scope").textContent.startsWith("当前目录及子目录")')
                    search.fill('Look 06')
                    with page.expect_response(lambda response:'/api/reference/library/items?' in response.url):
                        page.locator('#split-search-submit').click()
                    page.wait_for_function('()=>document.querySelectorAll("#split-preview-grid [data-row]").length===5')
                    page.locator('#split-view-single').click()
                    page.locator('#split-reference-stage').focus();page.keyboard.press('Escape')
                    self.assertEqual(page.evaluate('Framekeep.getSnapshot().selectedId'), selected)
                    self.assertEqual(self.store.session(self.sid)['state']['overrides'], self.ratings)
                    out = ROOT/'build/reference-result-grid';out.mkdir(parents=True, exist_ok=True)
                    for theme in ('dark', 'light', 'pure-white', 'hardcore'):
                        page.evaluate('(value)=>FramekeepTheme.set(value)', theme)
                        for width in (1680, 1024, 375, 320):
                            page.set_viewport_size({'width':width, 'height':1050})
                            self.assertTrue(grid.is_visible())
                            self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'), (theme, width))
                            if width<=375:
                                self.assertGreaterEqual(grid.bounding_box()['width'], width-30)
                                self.assertGreaterEqual(grid.bounding_box()['height'], 180)
                                if width==375 and theme=='dark':
                                    page.screenshot(path=str(out/'narrow.png'))
                        page.set_viewport_size({'width':1680, 'height':1050})
                        page.wait_for_function('()=>[...document.querySelectorAll("#split-preview-grid img")].every(e=>e.complete&&e.naturalWidth>0)')
                        page.screenshot(path=str(out/f'{theme}.png'))
                    page.locator('#split-grid-scale').evaluate('(e)=>{e.value=250;e.dispatchEvent(new Event("input",{bubbles:true}))}')
                    page.wait_for_timeout(400)
                    page.reload();page.wait_for_selector('#split-preview-grid [data-row]')
                    self.assertTrue(grid.is_visible())
                    self.assertEqual(page.locator('#split-grid-scale-value').inner_text(), '250%')
                    self.assertEqual(page.locator('#split-fade-curve').input_value(),'linear')
                    self.assertEqual(page.locator('#split-reference-thumbnails [data-show-reference]').count(), 2)
                    with page.expect_response(lambda response: response.url.endswith('/api/reference/preferences')):
                        page.locator('#split-clear-reference').click()
                    self.assertEqual(page.locator('#split-reference-thumbnails [data-show-reference]').count(), 1)
                    self.assertEqual(len(local.preferences()['references']), 1)
                    next_photos = self.base/'new-shoot';next_photos.mkdir()
                    next_session = self.store.ensure_live_session(next_photos);self.store.activate(next_session)
                    with page.expect_response(lambda response: response.url.endswith('/api/reference/task')):
                        page.evaluate('(id)=>{FRAMEKEEP_SESSION.id=id;document.dispatchEvent(new CustomEvent("framekeep:selection"))}', next_session)
                    self.assertEqual(page.locator('#split-reference-thumbnails [data-show-reference]').count(), 1)
                    self.assertEqual(len(local.preferences()['references']), 1)
                    self.assertFalse(page.locator('#split-clear-reference').is_disabled())
                    self.assertTrue(list(local.cache.folder.glob('*.webp')) or list(local.cache.folder.glob('*.jpg')))
                    page.reload();page.wait_for_selector('#split-reference-thumbnails [data-show-reference]')
                    self.assertEqual(page.locator('#split-reference-thumbnails [data-show-reference]').count(), 1)
                    self.assertTrue(any(request.get('size')==['1800'] for request in previews))
                    self.assertFalse(errors, errors)
                    browser.close()
        finally:
            if http:
                http.shutdown();http.server_close()
            local.close();remote.close()


if __name__=='__main__':
    unittest.main()
