"""设置页面实际调用切换、选目录、进度与迁移后路径恢复。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from everything_search import EverythingSearch
from reference_library import ReferenceLibrary
import test_full_auto_ocr as fixture

ROOT=Path(__file__).resolve().parents[1]


class ReferenceStorageBrowserTests(unittest.TestCase):
    setUp=fixture.FullAutoWorkflowTests.setUp
    tearDown=fixture.FullAutoWorkflowTests.tearDown

    def test_settings_switch_picker_migration_progress_and_reload(self):
        import server
        search=EverythingSearch(SimpleNamespace(store=SimpleNamespace(root=self.base,data=self.store.data)))
        search.set_enabled(False)
        library=ReferenceLibrary(self.store.data,search);http=None
        release=threading.Event();reader=None
        try:
            root=library.add_root(self.photos)['root_id'];deadline=time.monotonic()+8
            while library._root(root)['status']!='ready':
                self.assertLess(time.monotonic(),deadline);time.sleep(.02)
            with ExitStack() as stack:
                for name,value in dict(STORE=self.store,DATA=self.store.data,SKU=self.sku,MODELS=self.providers,
                    ARCHIVE=self.archive,REFERENCES=library,MOUSE=None,WATCH=None,ANALYSIS=None,RAWLINK=None,
                    FILESEARCH=search,COMPONENTS=None,DRIVER=None,STORAGE=None).items():
                    stack.enter_context(patch.object(server,name,value))
                http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
                threading.Thread(target=http.serve_forever,daemon=True).start()
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1100},reduced_motion='reduce');errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        def open_settings():
                            page.locator('#app-settings-open').click()
                            page.locator('[data-settings-section="library"]').click()
                            page.wait_for_selector('#split-index-storage-current')
                        open_settings();toggle=page.locator('#split-everything-enabled')
                        self.assertFalse(toggle.is_checked())
                        with page.expect_response('**/api/reference/everything'):toggle.check()
                        page.wait_for_function('()=>document.getElementById("split-everything-enabled").checked&&!document.getElementById("split-everything-enabled").disabled')
                        page.reload(wait_until='domcontentloaded');open_settings()
                        self.assertTrue(toggle.is_checked())
                        with page.expect_response('**/api/reference/everything'):toggle.uncheck()
                        page.wait_for_function('()=>document.getElementById("split-everything-status").textContent.includes("已关闭调用")')
                        target=self.base/'fast-ssd'
                        page.evaluate('(path)=>{window.FramekeepDesktop={chooseFolders:async()=>[path]}}',str(target))
                        page.locator('#split-index-storage-browse').click()
                        self.assertEqual(page.locator('#split-index-storage-path').input_value(),str(target))
                        entered=threading.Event()
                        def active():
                            with library.storage.gate.read():entered.set();release.wait(15)
                        reader=threading.Thread(target=active);reader.start();self.assertTrue(entered.wait(3))
                        with page.expect_response('**/api/reference/index-storage'):page.locator('#split-index-storage-migrate').click()
                        page.wait_for_selector('#split-index-storage-progress',state='visible')
                        self.assertTrue(page.locator('#split-index-storage-migrate').is_disabled())
                        release.set();reader.join(3)
                        page.wait_for_function('()=>document.getElementById("split-index-storage-message").textContent.includes("迁移完成")')
                        current=page.locator('#split-index-storage-current').inner_text()
                        self.assertTrue(Path(current).is_relative_to(target))
                        self.assertIn('原数据库保留于',page.locator('#split-index-storage-previous').inner_text())
                        page.reload(wait_until='domcontentloaded');open_settings()
                        self.assertFalse(toggle.is_checked());self.assertEqual(page.locator('#split-index-storage-current').inner_text(),current)
                        output=ROOT/'build/index-settings';output.mkdir(parents=True,exist_ok=True)
                        for theme in ('dark','light'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            page.locator('#split-index-storage').scroll_into_view_if_needed()
                            page.screenshot(path=str(output/f'settings-{theme}.png'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
        finally:
            release.set()
            if reader:reader.join(5)
            if http:http.shutdown();http.server_close()
            library.close()


if __name__=='__main__':unittest.main()
