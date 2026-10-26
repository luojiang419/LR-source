"""后台索引轮询的节点、焦点、滚动与真实进度展示回归。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from reference_library import ReferenceLibrary
import test_full_auto_ocr as fixture

ROOT=Path(__file__).resolve().parents[1]


class IndexProgressBrowserTests(unittest.TestCase):
    setUp=fixture.FullAutoWorkflowTests.setUp
    tearDown=fixture.FullAutoWorkflowTests.tearDown

    def test_poll_keeps_directory_nodes_focus_scroll_and_shows_progress(self):
        import server
        for index in range(40):(self.photos/f'Folder {index:02d}').mkdir()
        local=ReferenceLibrary(self.base/'references');http=None
        try:
            root=local.add_root(self.photos,'资料目录')['root_id']
            deadline=time.monotonic()+10
            while local._root(root)['status']!='ready':
                self.assertLess(time.monotonic(),deadline);time.sleep(.02)
            progress={'status':'scanning','progress':{'phase':'indexing','done':25,'total':100,'percent':25}};offline=[False];remote=[]
            def config(route):
                data=local.public_config();data['roots'][0].update(progress);route.fulfill(json=data)
            def sources(route):
                data=local.sources();data['devices'][0]['roots'][0].update(progress)
                if remote:data['devices'].append({'id':'remote-progress','name':'API 资料机','remote':True,'roots':remote})
                route.fulfill(json=data)
            with ExitStack() as stack:
                for name,value in dict(STORE=self.store,DATA=self.store.data,SKU=self.sku,MODELS=self.providers,ARCHIVE=self.archive,
                                       REFERENCES=local,MOUSE=None,WATCH=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,COMPONENTS=None,DRIVER=None).items():
                    stack.enter_context(patch.object(server,name,value))
                http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
                threading.Thread(target=http.serve_forever,daemon=True).start()
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050},reduced_motion='reduce');errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.route('**/api/reference/config',config);page.route('**/api/reference/sources',sources)
                        page.route('**/api/reference/library/folders?*',lambda route:route.fulfill(json={'folders':[]}) if 'device=remote-progress' in route.request.url else route.fulfill(status=503,json={'error':'模拟目录离线'}) if offline[0] else route.continue_())
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.locator('#view-open').click();page.locator('#layout-split').click();page.keyboard.press('Escape')
                        page.wait_for_selector('[data-resource-folder]');page.wait_for_function('()=>document.getElementById("split-index-percent").textContent==="25%"')
                        page.locator('[data-resource-folder]').nth(10).focus()
                        page.evaluate('''()=>{window.indexRoot=document.querySelector('[data-resource-group]');window.indexFolder=document.activeElement;
                            const tree=document.getElementById('split-tree');tree.scrollTop=180;window.indexScroll=tree.scrollTop;}''')
                        progress['progress'].update(done=65,percent=65)
                        page.wait_for_function('()=>document.getElementById("split-index-percent").textContent==="65%"')
                        self.assertTrue(page.evaluate('()=>indexRoot===document.querySelector("[data-resource-group]")&&indexFolder.isConnected&&document.activeElement===indexFolder&&document.getElementById("split-tree").scrollTop===indexScroll'))
                        self.assertEqual(page.locator('#split-index-progress').get_attribute('value'),'65')
                        self.assertEqual(page.locator('#split-index-overview-percent').inner_text(),'65%')
                        page.locator('#split-hide-resources').click()
                        self.assertTrue(page.locator('#split-index-overview-progress').is_visible())
                        self.assertFalse(page.locator('#split-index-progress').is_visible())
                        page.locator('#split-show-resources').click()
                        out=ROOT/'build/index-progress';out.mkdir(exist_ok=True,parents=True)
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            page.locator('#split-resource-panel').screenshot(path=str(out/f'{theme}.png'))
                        progress['progress'].update(done=128,total=0,percent=40,estimated=True,basis='directories',directories_done=2,directories_total=5)
                        page.wait_for_function('()=>document.getElementById("split-index-percent").textContent==="约 40%"')
                        self.assertEqual(page.locator('#split-index-progress').get_attribute('value'),'40')
                        self.assertEqual(page.locator('#split-index-overview-progress').get_attribute('value'),'40')
                        self.assertIn('目录估算',page.locator('#split-index-overview-status').inner_text())
                        self.assertIn('2 / 已发现 5',page.locator('.split-resource-group .split-root-index').inner_text())
                        self.assertIn('约 40%',page.locator('#split-shared-roots .split-root-index').inner_text())
                        self.assertIn('128 个文件',page.locator('#split-index-status').inner_text())
                        offline[0]=True;progress['progress'].update(done=75,total=100,percent=75,estimated=False)
                        page.wait_for_function('()=>document.getElementById("split-tree").textContent.includes("模拟目录离线")')
                        offline[0]=False;progress['progress'].update(done=80,percent=80)
                        page.wait_for_function('()=>document.querySelectorAll("[data-resource-folder]").length===40')
                        page.locator('[data-resource-root]').click()
                        progress['progress'].update(done=85,percent=85)
                        page.wait_for_function('()=>document.getElementById("split-index-percent").textContent==="85%"')
                        self.assertEqual(page.locator('[data-resource-root]').get_attribute('aria-expanded'),'false')
                        progress.update(status='ready',progress={'phase':'completed','done':100,'total':100,'percent':100})
                        page.wait_for_function('()=>document.getElementById("split-index-overview-percent").textContent==="100%"')
                        self.assertTrue(page.locator('#split-index-overview-progress').is_visible())
                        self.assertIn('索引完成',page.locator('#split-index-overview-status').inner_text())
                        self.assertTrue(page.evaluate('()=>indexRoot===document.querySelector("[data-resource-group]")'))
                        remote.extend([{'id':'first','name':'共享 A','status':'scanning','progress':{'done':20,'total':100,'percent':20}},
                                       {'id':'second','name':'共享 B','status':'scanning','progress':{'done':60,'total':100,'percent':60}}])
                        page.reload();page.wait_for_function('()=>document.getElementById("split-index-overview-percent").textContent==="40%"')
                        remote[0].update(status='ready',progress={'phase':'completed','done':100,'total':100,'percent':100})
                        page.wait_for_function('()=>document.getElementById("split-index-overview-percent").textContent==="80%"')
                        remote[1]['progress'].update(done=80,percent=80)
                        page.wait_for_function('()=>document.getElementById("split-index-overview-percent").textContent==="90%"')
                        remote[1].update(status='error',error='API 目录断开')
                        page.wait_for_function('()=>document.getElementById("split-index-overview-status").textContent.includes("索引失败")')
                        self.assertEqual(page.locator('#split-index-overview-percent').inner_text(),'90%')
                        self.assertFalse(errors,errors)
                    finally:browser.close()
        finally:
            if http:http.shutdown();http.server_close()
            local.close()
