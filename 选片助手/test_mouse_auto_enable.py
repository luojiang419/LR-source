"""自动遥控只使用隔离设置、模拟协议和真实浏览器，不打开实体驱动。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from asset_archive import AssetArchive
from model_providers import Providers
from sku_service import SkuService
import media_engine as media
from mouse_remote import MouseRemote
from mouse_native.interception import Interception
from mouse_native.profiles import describe_device
from test_mouse_remote import FakeHotkey, FakeLease, FakeTransport
from test_mouse_profiles import ProfileTransport, OLD, NEW

ROOT = Path(__file__).resolve().parents[1]


def remote_for(transport, path):
    return MouseRemote(lambda _, click, error: Interception(None, click, error, transport=transport),
                       FakeHotkey, lease_factory=FakeLease, legacy_guard=lambda: None, settings_path=path)


class MouseAutoEnableTests(unittest.TestCase):
    def test_disabled_and_invalid_config_never_opens_driver(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'mouse.json'
            path.write_text(json.dumps({'auto_enable': 'true', 'preferred_receiver': 'invalid'}), encoding='utf8')
            remote = remote_for(FakeTransport(), path)
            try:
                self.assertFalse(remote.auto_start('auto-window')['active'])
                self.assertIsNone(remote.bridge)
                for value in (1, None, 'true', [], {}):
                    with self.assertRaises(ValueError):remote.configure({'auto_enable': value})
                self.assertIsNone(remote.bridge)
            finally:remote.close()

    def test_preferred_receiver_survives_restart_and_driver_slot_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'mouse.json'
            remote = remote_for(ProfileTransport([describe_device(13, OLD), describe_device(16, NEW)]), path)
            try:
                remote.configure({'auto_enable': True})
                with self.assertRaisesRegex(ValueError, '多只接收器'):remote.auto_start('auto-window')
                remote.start('auto-window', 16)
                with self.assertRaisesRegex(ValueError, '其他窗口'):remote.configure({'auto_enable': False}, 'other-window')
            finally:remote.close()
            transport = ProfileTransport([describe_device(16, OLD), describe_device(19, NEW)])
            remote = remote_for(transport, path)
            try:
                self.assertTrue(remote.auto_start('new-window')['active'])
                self.assertEqual(transport.opened, [19])
                self.assertEqual(remote.state()['diagnostics']['profile'], 'logitech-c534')
                remote.configure({'auto_enable': False}, 'new-window')
                self.assertFalse(remote.state()['active'])
                self.assertEqual(transport.filters[-1], (19, 0))
                self.assertFalse(remote.options['auto_enable'])
            finally:remote.close()
            # 目标缺失时不能接管同槽位的另一型号。
            remote = remote_for(ProfileTransport([describe_device(16, OLD)]), path)
            try:
                remote.configure({'auto_enable': True})
                with self.assertRaisesRegex(ValueError, '未找到'):remote.auto_start('auto-window')
                self.assertEqual(remote.bridge.transport.opened, [])
            finally:remote.close()

    def test_auto_start_keeps_ownership_and_recovery_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            remote = remote_for(FakeTransport(), Path(folder)/'mouse.json')
            try:
                remote.configure({'auto_enable': True})
                started = remote.auto_start('auto-window')
                with self.assertRaisesRegex(ValueError, '已有窗口'):remote.auto_start('other-window')
                remote.hotkey.release('紧急恢复')
                self.assertFalse(remote.poll('auto-window', started['generation'], 0)['active'])
                self.assertTrue(remote.options['auto_enable'])
                remote.hotkey.error = '恢复键占用'
                with self.assertRaisesRegex(ValueError, '恢复键占用'):remote.auto_start('auto-window')
                self.assertEqual(remote.bridge.transport.filters[-1], 0)
            finally:remote.close()


class MouseAutoBrowserTests(unittest.TestCase):
    def test_immediate_enable_restart_pause_stop_and_themes(self):
        import server
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            base = Path(folder);photos = base/'photos';photos.mkdir()
            photo = photos/'sample.JPG';Image.new('RGB', (300, 200), 'teal').save(photo)
            store = Store(base/'data', ROOT);sid = store.ensure_live_session(photos);store.activate(sid)
            store.append_live(sid, [media.capture_record(photos, photo, None, {})])
            cid = store.records(sid)[0]['id'];store.save(sid, 0, {'view':'single','selectedId':cid,'overrides':{cid:0}})
            path = store.data/'mouse-remote.json';transport = FakeTransport();remote = remote_for(transport, path)
            providers = Providers(store.data);archive = AssetArchive(store, autostart=False);sku = SkuService(store, providers, autostart=False)
            for key, value in [('STORE',store),('DATA',store.data),('MOUSE',remote),('WATCH',None),('SKU',sku),('ARCHIVE',archive),('ANALYSIS',None),('MODELS',providers),('RAWLINK',None),('FILESEARCH',None)]:
                stack.enter_context(patch.object(server, key, value))
            http = ThreadingHTTPServer(('127.0.0.1',0), server.Handler);http.daemon_threads = True
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                    try:
                        page = browser.new_page(viewport={'width':1680,'height':1050});errors = []
                        page.on('pageerror', lambda e:errors.append(str(e)))
                        url = f'http://127.0.0.1:{http.server_port}';page.goto(url, wait_until='networkidle')
                        self.assertIsNone(remote.bridge)
                        page.locator('#app-settings-open').click();page.locator('[data-settings-section=remote]').click()
                        switch = page.get_by_role('switch', name='全自动切换');switch.click()
                        page.wait_for_function('()=>document.getElementById("mouse-remote").getAttribute("aria-pressed")==="true"')
                        self.assertTrue(page.locator('#app-settings-page').is_visible())
                        self.assertEqual(switch.get_attribute('aria-checked'), 'true')
                        transport.feed(1,2);page.wait_for_timeout(300)
                        self.assertEqual(page.evaluate('(id)=>Framekeep.persistedState().overrides[id]', cid), 0)
                        out = ROOT/'build/mouse-auto-enable';out.mkdir(parents=True, exist_ok=True)
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(t)=>FramekeepTheme.set(t)', theme)
                            for width in (1680,1000,375,320):
                                page.set_viewport_size({'width':width,'height':1050})
                                switch.scroll_into_view_if_needed()
                                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                                self.assertFalse(page.locator('#mouse-auto-card').evaluate('(e)=>e.scrollWidth>e.clientWidth'))
                                self.assertEqual(switch.evaluate('(e)=>getComputedStyle(e).borderRadius'), '30px')
                            page.set_viewport_size({'width':1680,'height':1050});switch.scroll_into_view_if_needed()
                            page.screenshot(path=str(out/f'{theme}.png'))
                        page.locator('#app-settings-close').click();transport.feed(1,2)
                        page.wait_for_function('()=>document.querySelectorAll("#stars .on").length===1')
                        page.evaluate('()=>FramekeepIO.flush()')
                        # 模拟真实进程重启：重新读取磁盘设置，再由新浏览器窗口自动建立新会话。
                        page.close();remote.close();transport = FakeTransport();remote = remote_for(transport, path);server.MOUSE = remote
                        page = browser.new_page();page.on('pageerror', lambda e:errors.append(str(e)))
                        page.goto(url, wait_until='domcontentloaded')
                        page.wait_for_function('()=>document.getElementById("mouse-remote").getAttribute("aria-pressed")==="true"')
                        self.assertTrue(remote.state()['active'])
                        transport.feed(1,2);page.wait_for_function('()=>document.querySelectorAll("#stars .on").length===2')
                        remote.hotkey.release('紧急恢复')
                        page.wait_for_function('()=>document.getElementById("mouse-remote").getAttribute("aria-pressed")==="false"')
                        page.wait_for_timeout(600);self.assertFalse(remote.state()['active'])
                        page.locator('#app-settings-open').click();page.locator('[data-settings-section=remote]').click()
                        switch = page.get_by_role('switch',name='全自动切换');self.assertEqual(switch.get_attribute('aria-checked'),'true')
                        switch.click();page.wait_for_function('()=>document.getElementById("mouse-auto-enable").getAttribute("aria-checked")==="false"')
                        switch.click();page.wait_for_function('()=>document.getElementById("mouse-remote").getAttribute("aria-pressed")==="true"')
                        switch.click();page.wait_for_function('()=>document.getElementById("mouse-remote").getAttribute("aria-pressed")==="false"')
                        self.assertEqual(transport.filters[-1],0)
                        page.reload(wait_until='networkidle');self.assertFalse(remote.state()['active'])
                        self.assertFalse(errors, errors)
                    finally:browser.close()
            finally:
                remote.close();http.shutdown();http.server_close();sku.close();archive.close()


if __name__ == '__main__':unittest.main()
