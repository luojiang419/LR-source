"""桌面关闭交互与设置的隔离浏览器验证，原生 IPC 由可控适配器模拟。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from catalog_store import Store
from asset_archive import AssetArchive
from sku_service import SkuService
from model_providers import Providers

ROOT = Path(__file__).resolve().parents[1]


class DesktopLifecycleTests(unittest.TestCase):
    def test_close_remember_settings_failures_and_responsive(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'data', ROOT)
            providers = Providers(store.data)
            archive = AssetArchive(store, autostart=False)
            sku = SkuService(store, providers, autostart=False)
            http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            http.daemon_threads = True
            patches = [patch.object(server, key, value) for key, value in {
                'STORE': store, 'DATA': store.data, 'MODELS': providers, 'ARCHIVE': archive,
                'SKU': sku, 'ANALYSIS': None, 'WATCH': None, 'MOUSE': None, 'RAWLINK': None, 'FILESEARCH': None,
            }.items()]
            for item in patches:
                item.start()
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                    context = browser.new_context(viewport={'width': 1440, 'height': 960}, reduced_motion='reduce')
                    context.add_init_script('''
                      window.nativeCalls=[];window.failSettings=false;window.failHide=false;window.exitAllowed=false;
                      window.__TAURI__={core:{invoke:async(name,args)=>{
                        nativeCalls.push([name,args]);
                        const prefs=JSON.parse(localStorage.getItem('native-test-prefs')||'{"close_behavior":"ask","startup_mode":"disabled"}');
                        const save=()=>localStorage.setItem('native-test-prefs',JSON.stringify(prefs));
                        if(name==='get_desktop_preferences')return prefs;
                        if(name==='set_desktop_preferences'){
                          if(failSettings)throw new Error('启动项写入失败');
                          if(args.closeBehavior)prefs.close_behavior=args.closeBehavior;
                          if(args.startupMode)prefs.startup_mode=args.startupMode;
                          save();return prefs;
                        }
                        if(name==='hide_to_tray'){
                          if(failHide)throw new Error('托盘不可用');
                          if(args.remember){prefs.close_behavior='tray';save()}return;
                        }
                        if(name==='finish_close'){
                          if(exitAllowed&&args.remember){prefs.close_behavior='exit';save()}return exitAllowed;
                        }
                      }}};
                    ''')
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{http.server_port}')
                    page.wait_for_function('()=>!!window.FramekeepSidebar&&!window.FramekeepBoot?.loading')
                    page.locator('#app-settings-open').click()
                    page.locator('[data-settings-section=general]').click()
                    page.wait_for_function('()=>!document.getElementById("desktop-close-behavior").disabled')
                    self.assertEqual(page.locator('#desktop-close-behavior').input_value(), 'ask')
                    self.assertEqual(page.locator('#desktop-startup-mode').input_value(), 'disabled')
                    page.locator('#desktop-startup-mode').select_option('tray')
                    page.wait_for_function('()=>document.getElementById("desktop-preferences-message").textContent.includes("已保存")')
                    page.evaluate('failSettings=true')
                    page.locator('#desktop-startup-mode').select_option('window')
                    page.wait_for_function('()=>document.getElementById("desktop-preferences-message").textContent.includes("保存失败")')
                    self.assertEqual(page.locator('#desktop-startup-mode').input_value(), 'tray')
                    page.evaluate('failSettings=false')
                    page.evaluate('FramekeepDesktop.close();FramekeepDesktop.close()')
                    page.wait_for_selector('#desktop-close-dialog[open]')
                    self.assertEqual(page.locator('#desktop-close-dialog').count(), 1)
                    page.keyboard.press('Escape')
                    self.assertFalse(page.locator('#desktop-close-dialog').is_visible())
                    page.evaluate('FramekeepDesktop.close()')
                    page.wait_for_selector('#desktop-close-dialog[open]')
                    out = ROOT / 'build/close-startup'
                    for theme in ('dark', 'light', 'pure-white', 'hardcore'):
                        page.evaluate('(theme)=>FramekeepTheme.set(theme)', theme)
                        page.screenshot(path=str(out / f'close-{theme}.png'))
                    page.set_viewport_size({'width': 320, 'height': 720})
                    self.assertLessEqual(page.locator('#desktop-close-dialog').evaluate('(e)=>e.getBoundingClientRect().right'), 320)
                    self.assertFalse(page.locator('#desktop-close-dialog').evaluate('(e)=>e.scrollWidth>e.clientWidth'))
                    page.screenshot(path=str(out / 'close-narrow.png'))
                    page.set_viewport_size({'width': 1440, 'height': 960})
                    # 保存失败不得隐藏；开关和选择保留以便重试。
                    page.evaluate("()=>{window.realFlush=FramekeepIO.flush;FramekeepIO.flush=async()=>{throw new Error('评级保存失败')}}")
                    page.locator('#desktop-close-remember').click()
                    page.locator('#desktop-close-tray').click()
                    page.wait_for_function('()=>document.getElementById("desktop-close-message").textContent.includes("评级保存失败")')
                    self.assertEqual(page.evaluate('nativeCalls.filter(c=>c[0]==="hide_to_tray").length'), 0)
                    page.evaluate('FramekeepIO.flush=realFlush;failHide=true')
                    page.locator('#desktop-close-tray').click()
                    page.wait_for_function('()=>document.getElementById("desktop-close-message").textContent.includes("托盘不可用")')
                    self.assertTrue(page.locator('#desktop-close-dialog').is_visible())
                    page.evaluate('failHide=false')
                    page.locator('#desktop-close-tray').click()
                    page.wait_for_function('()=>!document.getElementById("desktop-close-dialog").open')
                    self.assertEqual(page.locator('#desktop-close-behavior').input_value(), 'tray')
                    page.reload()
                    page.wait_for_function('()=>!!window.FramekeepSidebar&&!window.FramekeepBoot?.loading')
                    page.locator('#app-settings-open').click()
                    page.locator('[data-settings-section=general]').click()
                    page.wait_for_function('()=>!document.getElementById("desktop-close-behavior").disabled')
                    self.assertEqual(page.locator('#desktop-close-behavior').input_value(), 'tray')
                    self.assertEqual(page.locator('#desktop-startup-mode').input_value(), 'tray')
                    page.evaluate('FramekeepDesktop.close()')
                    page.wait_for_function('()=>nativeCalls.some(c=>c[0]==="hide_to_tray")')
                    self.assertEqual(page.locator('#desktop-close-dialog[open]').count(), 0)
                    # 托盘的显式退出跳过已记住的隐藏行为；后台任务取消退出仍可继续使用。
                    page.evaluate('FramekeepDesktop.close("exit")')
                    page.wait_for_function('()=>nativeCalls.some(c=>c[0]==="finish_close")')
                    page.locator('#desktop-close-behavior').select_option('ask')
                    page.wait_for_function('()=>document.getElementById("desktop-close-behavior").value==="ask"&&!document.getElementById("desktop-close-behavior").disabled')
                    page.evaluate('FramekeepDesktop.close()')
                    page.wait_for_selector('#desktop-close-dialog[open]')
                    page.locator('#desktop-close-exit').click()
                    page.wait_for_function('()=>!document.getElementById("desktop-close-dialog").open')
                    self.assertEqual(page.locator('#desktop-close-behavior').input_value(), 'ask')
                    page.evaluate('exitAllowed=true;FramekeepDesktop.close()')
                    page.wait_for_selector('#desktop-close-dialog[open]')
                    page.locator('#desktop-close-remember').click()
                    page.locator('#desktop-close-exit').click()
                    page.wait_for_function('()=>JSON.parse(localStorage.getItem("native-test-prefs")).close_behavior==="exit"')
                    self.assertFalse(errors, errors)
                    context.close()
                    # 普通浏览器禁用原生设置，仍能正常导航。
                    page = browser.new_page()
                    page.goto(f'http://127.0.0.1:{http.server_port}')
                    page.wait_for_function('()=>!!window.FramekeepSidebar&&!window.FramekeepBoot?.loading')
                    page.locator('#app-settings-open').click()
                    page.locator('[data-settings-section=general]').click()
                    self.assertTrue(page.locator('#desktop-close-behavior').is_disabled())
                    self.assertTrue(page.locator('#desktop-startup-mode').is_disabled())
                    browser.close()
            finally:
                http.shutdown()
                http.server_close()
                sku.close()
                archive.close()
                for item in reversed(patches):
                    item.stop()


if __name__ == '__main__':
    unittest.main()
