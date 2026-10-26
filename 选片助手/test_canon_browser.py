"""真实 HTTP 和浏览器：无需输入监控目录即可接收 EOS 照片并切换工作台。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from canon_link import CanonLink
from catalog_store import Store
from folder_watch import FolderWatch

ROOT=Path(__file__).resolve().parents[1]


class CanonBrowserTests(unittest.TestCase):
    def test_tether_save_dialog_network_paths_pending_profile_and_original_file(self):
        import server
        from datetime import date
        from canon_link import read_settings
        from test_canon_link import CanonLinkTests
        fixture=CanonLinkTests();fixture.setUp();fixture.existing_config();fixture.watch.thread.start()
        out=ROOT/'build/tether-save-acceptance';out.mkdir(parents=True,exist_ok=True)
        try:
            # EOS 尚未关联本软件：历史启用与目录记录不能主动登记或接管。
            fixture.service.destination=str(fixture.photos);fixture.service._persist()
            fixture.watch.start(str(fixture.photos),source='canon');fixture.service.close();fixture.watch.close()
            fixture.watch=FolderWatch(fixture.store)
            fixture.service=CanonLink(fixture.store,fixture.watch,fixture.app,discover=lambda:fixture.install,running=lambda:fixture.running,autostart=False)
            startup_profile=fixture.config.read_bytes();startup_preferences=fixture.service.preferences.read_bytes()
            fixture.running=True;fixture.service.tick()
            with ExitStack() as patches:
                for name,value in dict(STORE=fixture.store,DATA=fixture.store.data,WATCH=fixture.watch,CANON=fixture.service,PREVIEWS=None,
                                       MOUSE=None,MODELS=None,SKU=None,ARCHIVE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None).items():
                    patches.enter_context(patch.object(server,name,value))
                http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
                threading.Thread(target=http.serve_forever,daemon=True).start()
                try:
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                        try:
                            page=browser.new_page(viewport={'width':1440,'height':940},reduced_motion='reduce');errors=[]
                            page.on('pageerror',lambda error:errors.append(str(error)))
                            page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                            page.wait_for_timeout(3400)
                            self.assertFalse(page.locator('dialog[open]').count());self.assertFalse(fixture.service.state()['enabled']);self.assertFalse(fixture.watch.state()['enabled'])
                            fixture.running=False;fixture.service.tick();page.reload(wait_until='domcontentloaded');page.wait_for_timeout(3400)
                            self.assertFalse(page.locator('dialog[open]').count());self.assertEqual(fixture.config.read_bytes(),startup_profile);self.assertEqual(fixture.service.preferences.read_bytes(),startup_preferences)
                            self.assertFalse(fixture.watch.state()['enabled'])
                            pending=[];page.route('**/api/canon',lambda route:pending.append(route));page.locator('#canon-tether-open').click()
                            page.locator('#canon-save-path').fill(r'Z:\输入期间的新目录')
                            for route in pending:route.continue_()
                            page.unroute('**/api/canon')
                            page.wait_for_function('()=>document.getElementById("canon-save-status").textContent.includes("应用后")')
                            self.assertEqual(page.locator('#canon-save-path').input_value(),r'Z:\输入期间的新目录')
                            for path,label in [(r'\\192.168.1.25\摄影共享\现场',r'\\192.168.1.25\摄影共享'),(r'Z:\联机拍摄','Z:'),(r'\\?\UNC\NAS\照片\拍摄',r'\\NAS\照片')]:
                                page.locator('#canon-save-path').fill(path)
                                self.assertEqual(page.locator('#canon-save-location').inner_text(),label)
                                self.assertEqual(page.locator('#canon-save-preview').inner_text(),path+'\\'+date.today().isoformat()+'\\IMG_0001.JPG')
                            self.assertIn('保留相机原文件名',page.locator('.canon-save-preview').inner_text())
                            page.evaluate('(path)=>window.FramekeepDesktop={chooseFolders:async()=>[path]}',str(fixture.photos))
                            page.locator('#canon-save-choose').click();page.wait_for_function('(path)=>document.getElementById("canon-save-path").value===path',arg=str(fixture.photos))
                            fixture.running=True;before=fixture.config.read_bytes();page.locator('#canon-save-apply').click()
                            page.wait_for_function('()=>document.getElementById("canon-save-status").textContent.includes("等待生效")')
                            self.assertTrue(page.locator('#canon-save-dialog').evaluate('e=>e.open'));self.assertEqual(fixture.config.read_bytes(),before)
                            fixture.running=False;fixture.service.tick()
                            page.wait_for_function('()=>document.getElementById("canon-save-status").textContent.includes("已生效")')
                            page.locator('#canon-save-apply').click();page.wait_for_function('()=>!document.getElementById("canon-save-dialog").open')
                            settings=read_settings(fixture.config)
                            self.assertEqual(settings['SaveFolder'],str(fixture.photos));self.assertEqual(settings['FileCustomizeIndex'],'0');self.assertEqual(settings['FolderNameSeparator'],'1')
                            photo=fixture.picture(fixture.photos/date.today().isoformat(),'IMG_1842.JPG');result=fixture.service.receive([str(photo)])
                            page.wait_for_function('(sid)=>FRAMEKEEP_SESSION.id===sid&&document.getElementById("filename").textContent==="IMG_1842.JPG"',arg=result['session_id'])
                            self.assertEqual(page.evaluate('FRAMEKEEP_SESSION.root'),str(photo.parent))
                            self.assertIn(date.today().isoformat(),page.locator('#folder-list').inner_text())
                            self.assertTrue(photo.is_file());self.assertEqual(list(photo.parent.iterdir()),[photo])
                            # EOS 已运行时启动/刷新也不自动弹窗；仅显式点击才打开并记忆上次目录。
                            fixture.running=True;fixture.service.tick();page.reload(wait_until='domcontentloaded')
                            page.wait_for_timeout(3400);self.assertFalse(page.locator('#canon-save-dialog').evaluate('e=>e.open'))
                            page.locator('#canon-tether-open').click();page.wait_for_function('(path)=>document.getElementById("canon-save-path").value===path',arg=str(fixture.photos))
                            page.locator('#canon-save-cancel').click();page.wait_for_timeout(3400)
                            self.assertFalse(page.locator('#canon-save-dialog').evaluate('e=>e.open'))
                            for theme in ['dark','light','pure-white','hardcore']:
                                page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme);page.locator('#canon-tether-open').click()
                                for width in [1440,1024,375]:
                                    page.set_viewport_size({'width':width,'height':940})
                                    page.locator('#canon-save-path').fill(r'\\192.168.1.25\摄影共享\联机拍摄')
                                    self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'),(theme,width))
                                    self.assertTrue(page.locator('#canon-save-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth'))
                                    self.assertTrue(page.locator('#canon-save-apply').is_visible())
                                    page.screenshot(path=str(out/f'save-dialog-{theme}-{width}.png'))
                                page.locator('#canon-save-cancel').click();page.set_viewport_size({'width':1440,'height':940})
                            self.assertEqual(fixture.service.state()['destination'],str(fixture.photos));self.assertFalse(errors,errors)
                            (out/'browser-result.json').write_text(json.dumps({'passed':True,'errors':errors,'network_display_cases':3,'themes':4,'widths':3,'original_filename':'IMG_1842.JPG','date_folder':date.today().isoformat(),'eos_running_staged':True,'real_smb_tested':False},ensure_ascii=False,indent=2),encoding='utf8')
                        finally:browser.close()
                finally:http.shutdown();http.server_close()
        finally:fixture.tearDown()

    def test_notification_auto_enters_new_folder_keeps_ratings_and_can_disable(self):
        import server
        out=ROOT/'build/canon-link-acceptance';out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory() as temporary,ExitStack() as patches:
            base=Path(temporary);store=Store(base/'data',ROOT);watch=FolderWatch(store)
            service=CanonLink(store,watch,base/'末帧.exe',allow_registration=False,autostart=False)
            service.configure(True)
            for name,value in dict(STORE=store,DATA=store.data,WATCH=watch,CANON=service,PREVIEWS=None,
                                   MOUSE=None,MODELS=None,SKU=None,ARCHIVE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None).items():
                patches.enter_context(patch.object(server,name,value))
            patches.enter_context(patch.dict(os.environ,FRAMEKEEP_DESKTOP_TOKEN='canon-browser'))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{http.server_port}'
            def send(path):
                request=urllib.request.Request(url+'/api/desktop/canon',data=json.dumps({'paths':[str(path)]}).encode(),headers={'Content-Type':'application/json','X-Framekeep-Desktop':'canon-browser'})
                with urllib.request.urlopen(request,timeout=5) as response:return json.load(response)
            def picture(folder,name):
                folder.mkdir(parents=True,exist_ok=True);path=folder/name;Image.new('RGB',(900,600),'green').save(path);return path
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':940},reduced_motion='reduce');errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepWatch&&window.FramekeepLayout')
                        first=picture(base/'新电脑 拍摄/2026-10-22','001.JPG');received=send(first)
                        page.wait_for_function('(sid)=>FRAMEKEEP_SESSION.id===sid&&DEMO_PHOTOS.length===1',arg=received['session_id'])
                        page.wait_for_function('()=>document.querySelector("#photos img")?.naturalWidth>0')
                        self.assertTrue(page.evaluate('FramekeepLayout.isMonitoring()'))
                        page.locator('#stars [data-rating="4"]').click();page.evaluate('FramekeepIO.flush()')
                        before=watch.state()['generation'];second=picture(first.parent,'002.JPG');send(second)
                        page.wait_for_function('()=>DEMO_PHOTOS.length===2&&document.getElementById("filename").textContent==="002.JPG"')
                        self.assertEqual(watch.state()['generation'],before)
                        third=picture(base/'另一个日期','003.JPG');new=send(third)
                        page.wait_for_function('(sid)=>FRAMEKEEP_SESSION.id===sid&&document.getElementById("filename").textContent==="003.JPG"',arg=new['session_id'])
                        self.assertEqual(next(iter(store.session(received['session_id'])['state']['overrides'].values())),4)
                        for theme in ['dark','light','pure-white','hardcore']:
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme);page.locator('#folder-watch').click()
                            self.assertTrue(page.locator('#canon-link-enabled').is_visible());self.assertTrue(page.locator('#canon-link-status').is_visible())
                            page.screenshot(path=str(out/f'canon-{theme}.png'));page.locator('#watch-close').click()
                        page.locator('#folder-watch').click();page.locator('#canon-link-enabled').uncheck()
                        page.wait_for_function('()=>!document.getElementById("canon-link-enabled").checked')
                        page.wait_for_function('()=>document.getElementById("folder-watch").getAttribute("aria-pressed")==="false"')
                        self.assertFalse(send(third)['accepted']);self.assertFalse(watch.state()['enabled']);self.assertFalse(errors,errors)
                        (out/'result.json').write_text(json.dumps({'passed':True,'auto_folder':True,'same_generation':True,'ratings_preserved':True,'themes':4,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf8')
                    finally:browser.close()
            finally:http.shutdown();http.server_close();service.close();watch.close()


if __name__=='__main__':unittest.main()
