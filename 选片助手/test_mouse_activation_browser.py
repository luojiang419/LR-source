"""真实 HTTP/Chrome 验证免重启刷新，系统加载边界用替身隔离。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from catalog_store import Store
from model_providers import Providers
from sku_service import SkuService
from mouse_native.driver_setup import DriverSetup
from mouse_native.profiles import describe_device
from test_mouse_auto_enable import remote_for
from test_mouse_profiles import ProfileTransport, NEW
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class MouseActivationBrowserTests(unittest.TestCase):
    def test_file_deployed_waits_for_reload_then_lists_and_reports_failures(self):
        import server
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            base=Path(folder);photos=base/'photos';photos.mkdir();photo=photos/'sample.JPG'
            Image.new('RGB',(300,200),'teal').save(photo)
            store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos);store.activate(sid)
            store.append_live(sid,[media.capture_record(photos,photo,None,{})])
            providers=Providers(store.data);archive=AssetArchive(store,autostart=False);sku=SkuService(store,providers,autostart=False)
            transport=ProfileTransport([]);remote=remote_for(transport,store.data/'mouse.json')
            mode=['ready'];calls=[]
            def native():
                calls.append(mode[0]);time.sleep(.7)
                if mode[0]=='cancel':raise ValueError('鼠标免重启识别未获 Windows 授权；允许授权后可再次刷新')
                if mode[0]=='restart':return {'usable':False,'restart_required':True,'message':'Windows 仍要求重启电脑'}
                transport.devices=[describe_device(13,NEW)]
                return {'usable':True,'restart_required':False,'message':'鼠标已识别，无需重启电脑'}
            path=base/'mouse.sys';path.write_bytes(b'verified')
            stack.enter_context(patch('mouse_native.driver_setup.SUPPORTED_DRIVER_SHA256',hashlib.sha256(b'verified').hexdigest()))
            driver=DriverSetup(Mock(),store.data,driver_path=path,activator=lambda:remote.activate_driver(native))
            for key,value in dict(STORE=store,DATA=store.data,MODELS=providers,ARCHIVE=archive,SKU=sku,DRIVER=driver,MOUSE=remote,WATCH=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,COMPONENTS=None).items():
                stack.enter_context(patch.object(server,key,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1400,'height':1000});errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                        page.locator('#app-settings-open').click();page.locator('#mouse-remote').click()
                        page.locator('#component-progress').wait_for(state='visible')
                        self.assertTrue(page.locator('#mouse-start').is_disabled())
                        page.wait_for_function('()=>document.querySelectorAll("#mouse-device option").length===1')
                        self.assertFalse(page.locator('#mouse-start').is_disabled());self.assertEqual(calls,['ready'])
                        self.assertEqual(transport.filters,[])
                        for state,expected in [('cancel','未获 Windows 授权'),('restart','仍要求重启电脑')]:
                            mode[0]=state;transport.devices=[]
                            page.locator('#mouse-refresh').click()
                            page.wait_for_function('(s)=>document.getElementById("mouse-status").textContent.includes(s)',arg=expected)
                            self.assertTrue(page.locator('#mouse-start').is_disabled())
                            self.assertEqual(page.locator('#mouse-device option').count(),0)
                            self.assertTrue(page.locator('#component-retry').is_visible())
                        mode[0]='ready';page.locator('#mouse-refresh').click()
                        page.wait_for_function('()=>document.querySelectorAll("#mouse-device option").length===1')
                        self.assertEqual(calls,['ready','cancel','restart','ready'])
                        self.assertFalse(page.locator('#mouse-start').is_disabled());self.assertFalse(errors,errors)
                        output=ROOT/'build/mouse-activation';output.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(output/'refresh-ready.png'))
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close();remote.close();sku.close();archive.close()


if __name__=='__main__':unittest.main()
