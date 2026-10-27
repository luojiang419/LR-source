import json
import queue
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from catalog_store import Store
from mouse_native.interception import ClickRouter, Interception
from mouse_native.mouse_transport import MousePacket, MOUSE_FILTER_ALL, TARGET_ID, Win32MouseTransport
from mouse_remote import MouseRemote

ROOT = Path(__file__).resolve().parents[1]


class FakeTransport:
    def __init__(self):
        self.packets = queue.Queue()
        self.filters = []

    def enumerate(self):
        return [{'id': 'driver:16', 'driver_id': 16, 'hardware': 'HID\\' + TARGET_ID}]

    def open(self, device): return device
    def hardware(self, handle): return 'HID\\' + TARGET_ID
    def event(self): return threading.Event()
    def set_event(self, handle, event): self.signal = event
    def set_filter(self, handle, mask): self.filters.append(mask)
    def close(self, handle): pass
    def wait(self, event, timeout): return event.wait(timeout / 1000)

    def receive(self, handle):
        result = []
        self.signal.clear()
        while not self.packets.empty():
            result.append(self.packets.get_nowait())
        return result

    def feed(self, *states):
        for state in states:
            packet = MousePacket()
            packet.state = state
            self.packets.put(packet)
        self.signal.set()


class FakeHotkey:
    error = ''
    def __init__(self, release): self.release = release
    def close(self): pass


class FakeLease:
    def close(self): pass


class MouseRemoteTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.clock = [0.0]
        factory = lambda _, click, error: Interception(None, click, error, transport=self.transport)
        self.remote = MouseRemote(factory, FakeHotkey, lambda: self.clock[0], FakeLease, lambda: None)
        self.client = 'window-one'

    def tearDown(self): self.remote.close()

    def start(self): return self.remote.start(self.client, 16)

    def wait_clicks(self, count):
        until = time.monotonic() + 2
        while self.remote.sequence < count and time.monotonic() < until:
            time.sleep(.005)
        self.assertEqual(self.remote.sequence, count)

    def test_edge_state_and_other_device(self):
        router = ClickRouter(); router.select(16)
        self.assertEqual(router.route(15, 1), 'forward')
        self.assertEqual([router.route(16, s) for s in [1, 1, 1, 0, 2, 2, 1]],
                         ['like', 'drop', 'drop', 'drop', 'drop', 'drop', 'like'])
        router.select(None); self.assertEqual(router.route(16, 1), 'forward')

    def test_protocol_layout_and_unsupported_ioctl_rejected(self):
        import ctypes
        self.assertEqual(ctypes.sizeof(MousePacket), 24)
        self.assertEqual(MousePacket.state.offset, 4)
        self.assertEqual(MousePacket.x.offset, 12)
        transport = Win32MouseTransport.__new__(Win32MouseTransport)
        with self.assertRaisesRegex(ValueError, '禁止调用'):
            transport.ioctl(None, 0x222020)

    def test_packets_poll_retry_ack_and_window_ownership(self):
        started = self.start(); generation = started['generation']
        self.transport.feed(1, 1, 1, 2, 1, 2); self.wait_clicks(2)
        result = self.remote.poll(self.client, generation, 0)
        self.assertEqual(len(result['events']), 2)
        self.assertEqual(self.remote.poll(self.client, generation, 0), result)
        self.assertEqual(self.remote.poll('other-window', generation, 0)['events'], [])
        self.assertEqual(self.remote.poll(self.client, generation, 2)['events'], [])
        with self.assertRaises(ValueError): self.remote.start('other-window', 16)
        self.assertEqual(self.transport.filters[-1], MOUSE_FILTER_ALL)

    def test_timeout_stop_and_old_generation_cannot_revive(self):
        first = self.start(); self.clock[0] = 11; self.remote.expire()
        self.assertFalse(self.remote.state()['active']); self.assertEqual(self.transport.filters[-1], 0)
        self.assertFalse(self.remote.poll(self.client, first['generation'], 0)['active'])
        second = self.start(); self.assertNotEqual(first['generation'], second['generation'])
        self.assertFalse(self.remote.poll(self.client, first['generation'], 0)['active'])
        self.assertTrue(self.remote.poll(self.client, second['generation'], 0)['active'])
        self.remote.hotkey.release('紧急恢复')
        self.assertFalse(self.remote.state()['active']); self.assertEqual(self.transport.filters[-1], 0)

    def test_hotkey_failure_never_enables_filter(self):
        def failure(_): raise ValueError('恢复键占用')
        self.remote.hotkey_factory = failure
        with self.assertRaisesRegex(ValueError, '恢复键占用'): self.start()
        self.assertNotIn(MOUSE_FILTER_ALL, self.transport.filters)
        self.assertFalse(self.remote.state()['active'])

    def test_mismatched_hardware_never_enables_filter(self):
        self.transport.hardware = lambda _: 'HID\\VID_OTHER'
        with self.assertRaisesRegex(ValueError, '身份已变化'): self.start()
        self.assertNotIn(MOUSE_FILTER_ALL, self.transport.filters)

    def test_queue_overflow_releases_filter(self):
        self.start()
        for _ in range(257): self.remote._click()
        self.assertFalse(self.remote.state()['active']); self.assertEqual(self.transport.filters[-1], 0)

    def test_ambiguous_devices_and_input_error(self):
        original = self.transport.enumerate()
        self.transport.enumerate = lambda: original * 2
        with self.assertRaisesRegex(ValueError, '多只'): self.start()
        self.assertNotIn(MOUSE_FILTER_ALL, self.transport.filters)
        self.transport.enumerate = lambda: original
        self.start(); self.remote._error('测试断开')
        self.assertFalse(self.remote.state()['active']); self.assertEqual(self.transport.filters[-1], 0)

    def test_other_process_lock_blocks_even_enumeration(self):
        def blocked(): raise ValueError('另一个末帧进程已连接')
        self.remote.lease_factory = blocked
        self.assertIn('另一个末帧', self.remote.state(refresh=True)['error'])
        with self.assertRaisesRegex(ValueError, '另一个末帧'): self.start()
        self.assertIsNone(self.remote.bridge)
        self.assertEqual(self.transport.filters, [])

    def test_legacy_tool_active_blocks_takeover(self):
        def active(): raise ValueError('HeartRelay 正在接管')
        self.remote.legacy_guard = active
        with self.assertRaisesRegex(ValueError, 'HeartRelay'): self.start()
        self.assertIsNone(self.remote.bridge)


class MouseBrowserTests(unittest.TestCase):
    def test_packets_to_browser_rating_save_switch_and_recovery(self):
        from playwright.sync_api import sync_playwright
        import server
        chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
        if not chrome.exists(): self.skipTest('未安装测试用 Chrome')
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); store = Store(base / 'data', ROOT)
            sessions = []
            for name in ['first', 'second']:
                photos = base / name; photos.mkdir()
                for i in range(3): Image.new('RGB', (300, 200), (70 + i * 30, 90, 100)).save(photos / f'p{i}.JPG')
                with patch('catalog_store.threading.Thread'), patch('media_engine.find_exiftool', return_value=Path('test')), patch('media_engine.read_metadata', return_value=[{}] * 3):
                    job = store.scan(photos); store._scan(job, photos, True)
                session = store.session(); ids = [r['id'] for r in store.records()]
                store.save(session['id'], 0, {'overrides': {cid: 0 for cid in ids}, 'view': 'single', 'selectedId': ids[0]})
                sessions.append((session['id'], ids))
            store.activate(sessions[0][0])
            transport = FakeTransport()
            remote = MouseRemote(lambda _, click, error: Interception(None, click, error, transport=transport), FakeHotkey,
                                 lease_factory=FakeLease, legacy_guard=lambda: None)
            http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler); http.daemon_threads = True
            with patch.object(server, 'STORE', store), patch.object(server, 'MOUSE', remote), patch.object(server, 'DATA', base / 'data'):
                thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
                url = f'http://127.0.0.1:{http.server_port}'
                try:
                    # 验证控制接口仍要求会话令牌。
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        urllib.request.urlopen(urllib.request.Request(url + '/api/mouse/start', b'{}', {'Content-Type': 'application/json'}))
                    self.assertEqual(denied.exception.code, 403)
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(executable_path=str(chrome), headless=True)
                        try:
                            page = browser.new_page(viewport={'width': 1500, 'height': 980})
                            errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
                            page.goto(url, wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                            page.locator('#app-settings-open').click();page.locator('[data-settings-section=remote]').click();page.locator('#mouse-remote').click(); page.locator('#mouse-start').click()
                            page.wait_for_function("() => !document.getElementById('mouse-dialog').open")
                            def rating(n): page.wait_for_function('(n) => document.querySelectorAll("#stars .on").length===n', arg=n)
                            def click(): transport.feed(1, 2)
                            transport.feed(1, 1, 1); rating(1)
                            time.sleep(.2); self.assertEqual(page.locator('#stars .on').count(), 1)
                            transport.feed(2)
                            for n in range(2, 6): click(); rating(n)
                            click(); time.sleep(.25); rating(5)
                            page.keyboard.press('Control+z'); rating(4)
                            page.locator('#next').click(); rating(0); click(); rating(1)
                            page.evaluate('() => FramekeepIO.flush()')
                            self.assertEqual(store.session(sessions[0][0])['state']['overrides'][sessions[0][1][1]], 1)
                            # 按下发生在上一张；将响应延迟到切图之后再交付。
                            page.evaluate('() => window.__oldPhoto=Framekeep.remoteTarget().photoId')
                            delayed = []
                            def delayed_poll(route):
                                response = route.fetch()
                                if response.json().get('events') and not delayed:
                                    # 服务已经返回点击，但前端还未处理时切换到下一张。
                                    page.evaluate('() => document.getElementById("next").click()')
                                    delayed.append(True)
                                route.fulfill(response=response)
                            page.route('**/api/mouse/poll', delayed_poll)
                            click()
                            page.wait_for_function('() => Framekeep.persistedState().overrides[window.__oldPhoto]===2')
                            self.assertTrue(delayed); rating(0)
                            page.unroute('**/api/mouse/poll', delayed_poll)
                            # 弹窗中输入被丢弃，关闭后不补记。
                            page.locator('#help').click(); click(); time.sleep(.3)
                            page.locator('#help-dialog .close-action').click(); rating(0)
                            # 文件夹切换不串同名照片。
                            page.locator(f'[data-session="{sessions[1][0]}"]').click()
                            page.wait_for_function('(id) => FRAMEKEEP_SESSION.id===id', arg=sessions[1][0]); rating(0)
                            click(); rating(1); page.evaluate('() => FramekeepIO.flush()')
                            page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading'); rating(1)
                            # 刷新后的新窗口不会继承旧会话；停止按钮仍能恢复。
                            page.locator('#app-settings-open').click();page.locator('[data-settings-section=remote]').click();page.locator('#mouse-remote').click(); page.locator('#mouse-stop').click()
                            page.wait_for_function('() => document.getElementById("mouse-start").disabled===false && document.getElementById("mouse-stop").disabled===true')
                            self.assertEqual(transport.filters[-1], 0)
                            self.assertFalse(errors, errors)
                            out = ROOT / 'build/mouse-acceptance'; out.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(out / 'mouse-panel.png'))
                            (out / 'browser-result.json').write_text(json.dumps({'passed': True, 'page_errors': errors, 'checks': ['repeated DOWN', '0 to 5 capped', 'undo', 'switch photo', 'delayed input target', 'modal pause', 'folder isolation', 'save and reload', 'stop releases', 'POST auth']}, indent=2), encoding='utf8')
                        finally: browser.close()
                finally:
                    remote.close(); http.shutdown(); http.server_close(); thread.join(timeout=2)


if __name__ == '__main__': unittest.main()
