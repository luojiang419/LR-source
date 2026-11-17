"""SKU 修改动画与真实文件队列联动；所有写入均使用隔离图片。"""
from contextlib import contextmanager,ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import test_sku_range as fixtures
from sku_library import browse

ROOT=Path(__file__).resolve().parents[1]

class SkuWriteProgressTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SkuRangeTests();self.fixture.setUp();self.f=self.fixture.f

    def tearDown(self):self.fixture.tearDown()

    @contextmanager
    def browser(self):
        import server
        f=self.f;http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=f.store,DATA=f.store.data,MODELS=f.providers,SKU=f.sku,ARCHIVE=f.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        yield page
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()

    def submit(self,page):
        page.locator('#sku-database-open').click();page.locator('#sku-database-list [data-db-sku="A100"]').click()
        cards=page.locator('#sku-database-grid .catalog-card');page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===4')
        cards.nth(0).click();cards.nth(1).click(modifiers=['Shift']);cards.nth(1).click(button='right')
        page.locator('[data-pool-action="correct_selection"]').click();page.locator('#sku-pool-name').fill('B-200');page.locator('#sku-pool-submit').click()

    def phase(self,page,state):page.wait_for_function('(state)=>document.getElementById("sku-write-view").dataset.state===state',arg=state)

    def completed(self,page):
        page.wait_for_function('()=>!document.getElementById("sku-pool-editor").open')
        page.wait_for_selector('#sku-database-list [data-db-sku="B200"]')
        self.assertEqual(browse(self.f.sku,code='B-200')['total'],2)
        for i in range(2):self.assertEqual(self.f.archive.writer.read(self.f.photos/f'{i}.JPG')['XMP-fk:SKU'],'B-200')

    def test_immediate_svg_delayed_submit_actual_progress_and_auto_completion(self):
        f=self.f;gate=threading.Event();entered=threading.Event();apply=f.archive.apply
        out=ROOT/'build/sku-write-animation';out.mkdir(parents=True,exist_ok=True)
        def delayed(*args,**kwargs):
            entered.set();gate.wait(timeout=50);return apply(*args,**kwargs)
        with self.browser() as page:
            with patch.object(f.archive,'apply',side_effect=delayed):
                try:
                    self.submit(page);self.phase(page,'preparing');self.assertTrue(entered.wait(timeout=5))
                    self.assertTrue(page.locator('#sku-pool-form').is_hidden());self.assertTrue(page.locator('#sku-write-view svg').is_visible())
                    self.assertIsNone(page.locator('#sku-write-bar').get_attribute('value'))
                    offset=page.locator('.sku-write-current').evaluate('(e)=>getComputedStyle(e).strokeDashoffset');page.wait_for_timeout(180)
                    self.assertNotEqual(offset,page.locator('.sku-write-current').evaluate('(e)=>getComputedStyle(e).strokeDashoffset'))
                    page.keyboard.press('Escape');self.assertTrue(page.locator('#sku-pool-editor').evaluate('(e)=>e.open'))
                    for theme in ['dark','light','pure-white','hardcore']:
                        page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                        for width,height in [(1440,900),(375,620)]:
                            page.set_viewport_size({'width':width,'height':height})
                            self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'),(theme,width))
                            self.assertTrue(page.locator('#sku-write-title').is_visible())
                            page.screenshot(path=str(out/f'preparing-{theme}-{width}.png'))
                    page.set_viewport_size({'width':1440,'height':900});page.evaluate('()=>FramekeepTheme.set("dark")')
                    page.emulate_media(reduced_motion='reduce')
                    self.assertEqual(page.locator('.sku-write-current').evaluate('(e)=>getComputedStyle(e).animationName'),'none')
                finally:gate.set()
                self.phase(page,'writing')
            self.assertEqual(page.locator('#sku-write-bar').evaluate('(e)=>e.value'),0)
            f.archive.process_one();page.wait_for_function('()=>document.getElementById("sku-write-bar").value===1')
            self.assertEqual(page.locator('#sku-write-percent').inner_text(),'50%');self.assertTrue(page.locator('#sku-pool-editor').evaluate('(e)=>e.open'))
            page.screenshot(path=str(out/'writing-half.png'));self.fixture.drain();self.completed(page)
            self.assertIn('已写入 2 个文件',page.locator('#sku-pool-status').inner_text())
            page.locator('#sku-view-tree').click();page.wait_for_selector('#sku-database-tree [data-db-sku="B200"]')
            page.screenshot(path=str(out/'completed-tree.png'))

    def test_failed_file_stops_animation_and_retry_reuses_write_batch(self):
        f=self.f;write=f.archive.writer.write;attempts=[];submissions=[]
        def fail_once(*args,**kwargs):
            attempts.append(1)
            if len(attempts)==1:raise ValueError('隔离测试：文件被占用')
            return write(*args,**kwargs)
        with self.browser() as page:
            page.emulate_media(reduced_motion='reduce');page.on('request',lambda r:submissions.append(r.url) if r.method=='POST' and '/api/sku/library/manage' in r.url else None)
            self.submit(page);self.phase(page,'writing')
            with patch.object(f.archive.writer,'write',side_effect=fail_once):self.fixture.drain()
            self.phase(page,'failed');self.assertTrue(page.locator('#sku-pool-editor').evaluate('(e)=>e.open'))
            self.assertEqual(page.locator('#sku-write-retry').inner_text(),'重试失败写入')
            page.locator('#sku-write-errors summary').click()
            self.assertIn('文件被占用',page.locator('#sku-write-errors li').inner_text())
            self.assertEqual(page.locator('.sku-write-current').evaluate('(e)=>getComputedStyle(e).animationName'),'none')
            page.screenshot(path=str(ROOT/'build/sku-write-animation/failed-file.png'))
            page.locator('#sku-write-retry').click();self.phase(page,'writing');self.fixture.drain();self.completed(page)
            self.assertEqual(len(submissions),1)

    def test_poll_and_final_refresh_failures_resume_without_resubmitting(self):
        submissions=[]
        with self.browser() as page:
            page.emulate_media(reduced_motion='reduce');page.on('request',lambda r:submissions.append(r.url) if r.method=='POST' and '/api/sku/library/manage' in r.url else None)
            def disconnect(route):route.abort('failed')
            page.route('**/api/metadata/operations/*',disconnect)
            self.submit(page);self.phase(page,'failed')
            self.assertEqual(page.locator('#sku-write-retry').inner_text(),'继续查看进度')
            page.unroute('**/api/metadata/operations/*',disconnect);page.locator('#sku-write-retry').click();self.phase(page,'writing')
            def unavailable(route):route.fulfill(status=503,json={'error':'隔离测试：清单暂不可用'})
            page.route('**/api/sku/library?*',unavailable);self.fixture.drain();self.phase(page,'failed')
            self.assertEqual(page.locator('#sku-write-title').inner_text(),'清单暂未同步')
            self.assertEqual(page.locator('#sku-write-retry').inner_text(),'重新同步清单')
            page.unroute('**/api/sku/library?*',unavailable);page.locator('#sku-write-retry').click();self.completed(page)
            self.assertEqual(len(submissions),1)

    def test_reload_can_resume_existing_range_write_without_a_second_assignment(self):
        submissions=[]
        with self.browser() as page:
            page.emulate_media(reduced_motion='reduce');page.on('request',lambda r:submissions.append(r.post_data_json) if r.method=='POST' and '/api/sku/library/manage' in r.url else None)
            self.submit(page);self.phase(page,'writing')
            page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
            page.locator('#sku-database-open').click();page.locator('#sku-write-resume').click();self.phase(page,'writing')
            self.fixture.drain();self.completed(page)
            self.assertEqual(len(submissions),2);self.assertEqual(submissions[0]['request_id'],submissions[1]['request_id'])
            with self.f.archive.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM operations WHERE source LIKE 'library-correction:%'").fetchone()[0],1)


if __name__=='__main__':unittest.main()
