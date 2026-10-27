"""接收器适配回归：只使用模拟传输、临时目录和隔离浏览器。"""
from contextlib import ExitStack
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mouse_native.interception import Interception
from mouse_native.mouse_transport import MousePacket, MOUSE_FILTER_ALL, Win32MouseTransport
from mouse_native.profiles import LEGACY, LOGITECH, describe_device, hardware_key, receiver_profile
from mouse_remote import MouseRemote


ROOT = Path(__file__).resolve().parents[1]
OLD = r'HID\VID_367B&PID_1919&REV_0100'
NEW = r'HID\VID_046D&PID_C534&REV_2901&MI_01&Col01'


class ProfileTransport:
    def __init__(self, devices):
        self.devices = [dict(d) for d in devices]
        self.actual = {d['driver_id']: d['hardware'] for d in devices}
        self.filters = []
        self.opened = []
        self.closed = []
        self.packets = queue.Queue()
        self.signal = threading.Event()

    def enumerate(self): return [dict(d) for d in self.devices]
    def open(self, device):
        self.opened.append(device)
        return device
    def hardware(self, handle): return self.actual[handle]
    def event(self): return self.signal
    def set_event(self, handle, event): pass
    def set_filter(self, handle, mask): self.filters.append((handle, mask))
    def close(self, handle): self.closed.append(handle)
    def wait(self, event, timeout): return event.wait(timeout / 1000)

    def receive(self, handle):
        self.signal.clear()
        packets = []
        while not self.packets.empty():
            packet = self.packets.get_nowait()
            if isinstance(packet, Exception):
                raise packet
            packets.append(packet)
        return packets

    def feed(self, *packets):
        for state, rolling in packets:
            packet = MousePacket()
            packet.state, packet.rolling = state, rolling & 65535
            self.packets.put(packet)
        self.signal.set()

    def disconnect(self):
        self.packets.put(OSError('模拟罗技接收器拔出'))
        self.signal.set()


class Hotkey:
    error = ''
    def __init__(self, release): self.release = release
    def close(self): pass


class Lease:
    def close(self): pass


def make_remote(transport, clock=None):
    return MouseRemote(
        lambda _, click, error: Interception(None, click, error, transport=transport),
        Hotkey, clock=clock or time.monotonic, lease_factory=Lease, legacy_guard=lambda: None)


class ReceiverProfileTests(unittest.TestCase):
    def test_exact_receiver_tokens_case_and_revision_compatibility(self):
        for hardware, profile in [(OLD, LEGACY), (NEW, LOGITECH),
                                  (r'HID\VID_046D&PID_C534&MI_01&Col01', LOGITECH),
                                  (r'hid\vid_046d&pid_c534&rev_abcd&mi_01&col01', LOGITECH)]:
            with self.subTest(hardware=hardware):
                self.assertEqual(receiver_profile(hardware), profile)
        self.assertEqual(hardware_key(NEW), hardware_key(NEW.replace('REV_2901&', '')))
        self.assertEqual(hardware_key(NEW), hardware_key(NEW.replace('REV_2901', 'REV_ABCD')))
        self.assertNotEqual(hardware_key(NEW), hardware_key(NEW.replace('MI_01', 'MI_00')))

    def test_wrong_interface_collection_and_unrelated_mice_are_excluded(self):
        rejected = [NEW.replace('MI_01', 'MI_00'), NEW.replace('MI_01', 'MI_02'),
                    NEW.replace('Col01', 'Col02'), NEW.replace('&MI_01', ''),
                    NEW.replace('&Col01', ''), NEW.replace('MI_01', 'MI_010'),
                    NEW.replace('Col01', 'Col010'), NEW.replace('PID_C534', 'PID_C5340'),
                    NEW.replace('VID_046D', 'VID_1046D'), NEW.replace('PID_C534', 'PID_C539'),
                    OLD.replace('PID_1919', 'PID_19190'), NEW.replace('HID\\', 'USB\\'),
                    '', None, 16, 'VID_046D&PID_C534&MI_01&COL01']
        for hardware in rejected:
            with self.subTest(hardware=hardware):
                self.assertIsNone(receiver_profile(hardware))
                self.assertIsNone(describe_device(16, hardware))

    def test_transport_enumeration_keeps_both_profiles_and_closes_all_nodes(self):
        transport = Win32MouseTransport.__new__(Win32MouseTransport)
        identities = {11: OLD, 14: NEW, 17: NEW.replace('Col01', 'Col02')}
        opened, closed = [], []
        def open_device(device):
            opened.append(device)
            return device
        transport.open = open_device
        transport.hardware = lambda handle: identities.get(handle, r'HID\VID_0001&PID_0002')
        transport.close = closed.append
        devices = transport.enumerate()
        self.assertEqual([d['driver_id'] for d in devices], [11, 14])
        self.assertEqual([d['profile'] for d in devices], [LEGACY.id, LOGITECH.id])
        self.assertEqual(opened, list(range(11, 21)))
        self.assertEqual(closed, opened)


class ReceiverSessionTests(unittest.TestCase):
    def setUp(self):
        self.transport = ProfileTransport([describe_device(13, OLD), describe_device(16, NEW)])
        self.clock = [0.0]
        self.remote = make_remote(self.transport, lambda: self.clock[0])
        self.client = 'receiver-test-window'

    def tearDown(self): self.remote.close()

    def wait_for(self, predicate):
        end = time.monotonic() + 3
        while not predicate() and time.monotonic() < end:
            time.sleep(.005)
        self.assertTrue(predicate(), '模拟输入未在期限内处理')

    def choose(self, device):
        return next(d for d in self.remote.state(refresh=True)['devices'] if d['driver_id'] == device)

    def test_old_and_logitech_can_be_selected_separately(self):
        for device, profile in [(16, LOGITECH), (13, LEGACY)]:
            with self.subTest(profile=profile.id):
                state = self.remote.start(self.client, self.choose(device))
                self.assertTrue(state['active'])
                self.assertEqual(state['diagnostics']['profile'], profile.id)
                self.assertEqual(state['diagnostics']['target'], profile.target)
                self.assertIn(profile.label, state['message'])
                self.assertEqual(self.transport.filters[-1], (device, MOUSE_FILTER_ALL))
                self.remote.stop()
                self.assertEqual(self.transport.filters[-1], (device, 0))

    def test_multiple_receivers_of_selected_model_are_rejected(self):
        self.transport.devices.append(describe_device(18, NEW))
        with self.assertRaisesRegex(ValueError, '多只同型号'):
            self.remote.start(self.client, self.choose(16))
        self.assertEqual(self.transport.filters, [])
        # 罗技有歧义不影响独立的旧型号。
        self.assertTrue(self.remote.start(self.client, self.choose(13))['active'])

    def test_old_model_duplicate_is_rejected_and_logitech_still_available(self):
        self.transport.devices.append(describe_device(18, OLD))
        with self.assertRaisesRegex(ValueError, '多只同型号'):
            self.remote.start(self.client, self.choose(13))
        self.assertEqual(self.transport.filters, [])
        self.assertTrue(self.remote.start(self.client, self.choose(16))['active'])

    def test_slot_reuse_after_selection_is_rejected(self):
        selection = self.choose(16)
        self.transport.devices = [describe_device(16, OLD)]
        with self.assertRaisesRegex(ValueError, '身份已变化'):
            self.remote.start(self.client, selection)
        self.assertEqual(self.transport.opened, [])
        self.assertEqual(self.transport.filters, [])

    def test_interface_or_collection_change_after_selection_is_rejected(self):
        selection = self.choose(16)
        for changed in [NEW.replace('MI_01', 'MI_00'), NEW.replace('Col01', 'Col02')]:
            with self.subTest(changed=changed):
                self.transport.devices = [{**selection, 'hardware': changed}]
                with self.assertRaisesRegex(ValueError, '身份已变化'):
                    self.remote.start(self.client, selection)
                self.assertEqual(self.transport.filters, [])

    def test_hardware_changes_between_enumeration_and_open_are_rejected(self):
        for changed in [OLD, NEW.replace('MI_01', 'MI_00'), r'HID\VID_1234&PID_5678']:
            with self.subTest(changed=changed):
                self.transport.actual[16] = changed
                with self.assertRaisesRegex(ValueError, '身份已变化'):
                    self.remote.start(self.client, self.choose(16))
                self.assertEqual(self.transport.filters, [])
                self.assertIn(16, self.transport.closed)
                self.assertFalse(self.remote.state()['active'])

    def test_revision_change_remains_compatible_at_selection_and_open(self):
        selection = self.choose(16)
        self.transport.devices[1]['hardware'] = NEW.replace('REV_2901&', '')
        self.transport.actual[16] = NEW.replace('REV_2901', 'REV_ABCD')
        self.assertTrue(self.remote.start(self.client, selection)['active'])

    def test_invalid_selection_profile_or_device_type_never_enables_filter(self):
        selection = self.choose(16)
        for invalid in [{**selection, 'profile': LEGACY.id}, {**selection, 'driver_id': True},
                        {**selection, 'hardware': None}, '16', True]:
            with self.subTest(selection=invalid):
                with self.assertRaises(ValueError): self.remote.start(self.client, invalid)
                self.assertEqual(self.transport.filters, [])

    def test_logitech_button_edges_wheel_stop_and_restart_reset(self):
        self.remote.configure({'right_decrease': True, 'wheel_browse': True})
        started = self.remote.start(self.client, self.choose(16))
        self.transport.feed((1, 0), (1, 0), (2, 0), (4, 0), (4, 0), (8, 0),
                            (0x400, 120), (0x400, -120), (16, 0), (32, 0))
        self.wait_for(lambda: self.remote.bridge.packets == 10)
        events = self.remote.poll(self.client, started['generation'], 0)['events']
        self.assertEqual([e['action'] for e in events], ['increase', 'decrease', 'previous', 'next'])
        self.assertEqual(self.remote.bridge.diagnostics()['physical_clicks_received'], 1)
        self.remote.stop()
        self.assertEqual(self.transport.filters[-1], (16, 0))
        self.assertIsNone(self.remote.bridge.handle)
        self.assertFalse(self.remote.poll(self.client, started['generation'], 0)['active'])
        again = self.remote.start(self.client, self.choose(16))
        self.transport.feed((1, 0))
        self.wait_for(lambda: self.remote.sequence == 5)
        self.assertNotEqual(again['generation'], started['generation'])

    def test_logitech_emergency_and_timeout_release_filter(self):
        self.remote.start(self.client, self.choose(16))
        self.remote.hotkey.release('紧急恢复')
        self.assertFalse(self.remote.state()['active'])
        self.assertEqual(self.transport.filters[-1], (16, 0))
        self.remote.start(self.client, self.choose(16))
        self.clock[0] = 11
        self.remote.expire()
        self.assertFalse(self.remote.state()['active'])
        self.assertEqual(self.transport.filters[-1], (16, 0))

    def test_logitech_disconnect_releases_filter_and_reports_error(self):
        started = self.remote.start(self.client, self.choose(16))
        with self.assertLogs(level='ERROR'):
            self.transport.disconnect()
            self.wait_for(lambda: not self.remote.state()['active'])
        state = self.remote.state()
        self.assertIn('接收器拔出', state['message'])
        self.assertEqual(self.transport.filters[-1], (16, 0))
        self.assertIsNone(self.remote.bridge.handle)
        self.assertFalse(self.remote.poll(self.client, started['generation'], 0)['active'])

    def test_logitech_refresh_after_disconnect_rebuilds_bridge_and_can_restart(self):
        started = self.remote.start(self.client, self.choose(16))
        previous, lease = self.remote.bridge, self.remote.lease
        with self.assertLogs(level='ERROR'):
            self.transport.disconnect()
            self.wait_for(lambda: not self.remote.state()['active'])
            previous.thread.join(timeout=2)
        self.assertFalse(previous.thread.is_alive())
        self.assertEqual(self.transport.filters[-1], (16, 0))

        state = self.remote.state(refresh=True)
        self.assertIsNot(self.remote.bridge, previous)
        self.assertIs(self.remote.lease, lease)
        self.assertTrue(previous.closed.is_set())
        self.assertTrue(self.remote.bridge.thread.is_alive())
        self.assertEqual(state['error'], '')
        self.assertFalse(state['active'])
        selection = next(d for d in state['devices'] if d['driver_id'] == 16)
        restarted = self.remote.start(self.client, selection)
        self.assertTrue(restarted['active'])
        self.assertNotEqual(restarted['generation'], started['generation'])
        self.assertEqual(self.transport.filters[-1], (16, MOUSE_FILTER_ALL))
        self.assertFalse(self.remote.poll(self.client, started['generation'], 0)['active'])
        self.transport.feed((1, 0), (2, 0))
        self.wait_for(lambda: self.remote.sequence == 1)
        self.assertEqual([e['action'] for e in self.remote.poll(
            self.client, restarted['generation'], 0)['events']], ['increase'])


class ReceiverBrowserTests(unittest.TestCase):
    def test_receiver_dropdown_sends_selected_identity_and_refresh_preserves_choice(self):
        from playwright.sync_api import sync_playwright
        chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
        if not chrome.is_file(): self.skipTest('未安装测试用 Chrome')
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            base = Path(folder)
            # server 导入时的令牌文件也仅写入临时目录。
            stack.enter_context(patch.dict(os.environ, {'FRAMEKEEP_DATA': str(base / 'data')}))
            import server
            from catalog_store import Store
            store = Store(base / 'data', ROOT)
            photos = base / 'photos'
            photos.mkdir()
            sid = store.ensure_live_session(photos)
            store.activate(sid)
            transport = ProfileTransport([describe_device(13, OLD), describe_device(16, NEW)])
            remote = make_remote(transport)
            stack.callback(remote.close)
            for key, value in [('STORE', store), ('DATA', store.data), ('MOUSE', remote),
                               ('WATCH', None), ('SKU', None), ('ARCHIVE', None),
                               ('ANALYSIS', None), ('MODELS', None), ('RAWLINK', None), ('FILESEARCH', None)]:
                stack.enter_context(patch.object(server, key, value))
            http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            http.daemon_threads = True
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path=str(chrome), headless=True)
                    try:
                        page = browser.new_page(viewport={'width': 1500, 'height': 980})
                        errors, selections = [], []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.on('request', lambda request: selections.append(request.post_data_json['device'])
                                if request.url.endswith('/api/mouse/start') else None)
                        page.goto(f'http://127.0.0.1:{http.server_port}', wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        page.locator('#app-settings-open').click();page.locator('[data-settings-section=remote]').click();page.locator('#mouse-remote').click()
                        page.wait_for_function('() => document.querySelectorAll("#mouse-device option").length === 2')
                        labels = page.locator('#mouse-device option').all_text_contents()
                        self.assertIn(LEGACY.label, labels[0])
                        self.assertIn(LOGITECH.label, labels[1])
                        page.locator('#mouse-device').select_option('16')
                        page.locator('#mouse-refresh').click()
                        page.wait_for_timeout(200)
                        self.assertEqual(page.locator('#mouse-device').input_value(), '16')
                        for device, profile in [(16, LOGITECH), (13, LEGACY)]:
                            page.locator('#mouse-device').select_option(str(device))
                            page.locator('#mouse-start').click()
                            page.wait_for_function('() => !document.getElementById("mouse-dialog").open')
                            self.assertEqual(remote.bridge.selected['driver_id'], device)
                            self.assertEqual(selections[-1], {'driver_id': device,
                                'hardware': NEW if device == 16 else OLD, 'profile': profile.id})
                            page.locator('#app-settings-open').click();page.locator('[data-settings-section=remote]').click();page.locator('#mouse-remote').click()
                            page.locator('#mouse-stop').click()
                            page.wait_for_function('() => !document.getElementById("mouse-start").disabled')
                            self.assertEqual(transport.filters[-1], (device, 0))
                        self.assertFalse(errors, errors)
                    finally:
                        browser.close()
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=2)


if __name__ == '__main__': unittest.main()
