"""重开发的设备控制端：定点打开已确认鼠标，不再加载不匹配的旧 DLL。"""
import logging
import threading
from .mouse_transport import MOUSE_FILTER_ALL, Win32MouseTransport
from .profiles import PROFILES, hardware_key, receiver_profile


class ClickRouter:
    def __init__(self):
        self.target = None
        self.pressed = False
        self.right_pressed = False
        self.middle_pressed = False
        self.wheel_remainder = 0

    def select(self, target):
        self.target = target
        self.pressed = False
        self.right_pressed = False
        self.middle_pressed = False
        self.wheel_remainder = 0

    def route(self, device, state):
        if device != self.target:
            return 'forward'
        clicked = bool(state & 1) and not self.pressed
        if state & 1:
            self.pressed = True
        if state & 2:
            self.pressed = False
        return 'like' if clicked else 'drop'

    def actions(self, device, state, rolling=0):
        result=self.route(device,state)
        if result=='forward':return []
        actions=['increase'] if result=='like' else []
        if state & 4 and not self.right_pressed:actions.append('decrease')
        if state & 4:self.right_pressed=True
        if state & 8:self.right_pressed=False
        if state & 16:self.middle_pressed=True
        if state & 32:self.middle_pressed=False
        if state & 0x400:
            delta=(int(rolling)+32768)%65536-32768
            self.wheel_remainder+=delta
            steps=int(self.wheel_remainder/120)
            self.wheel_remainder-=steps*120
            actions.extend(['previous' if steps>0 else 'next']*abs(steps))
        return actions


class Interception:
    def __init__(self, _legacy_dll_path, on_like, on_error, *, transport=None):
        self.lock = threading.RLock()
        self.router = ClickRouter()
        self.on_like, self.on_error = on_like, on_error
        self.transport = transport
        self.thread = None
        self.closed = threading.Event()
        self.error = ''
        self.devices = []
        self.handle = self.event = None
        self.accepted = False
        self.selected = None
        self.packets = self.clicks = 0
        try:
            self.transport = transport or Win32MouseTransport()
            self.refresh()
            self.thread = threading.Thread(target=self._run, name='mouse-filter', daemon=True)
            self.thread.start()
        except Exception as exc:
            self.error = str(exc)
            logging.info('Mouse bridge unavailable: %s', exc)

    def refresh(self):
        with self.lock:
            if self.handle is None and self.transport:
                self.devices = self.transport.enumerate()
            return list(self.devices)

    def diagnostics(self):
        with self.lock:
            profile = receiver_profile(self.selected['hardware']) if self.selected else None
            return {'backend': 'rea-verified-win32', 'target': profile.target if profile else None,
                    'profile': profile.id if profile else None,
                    'supported_targets': [p.target for p in PROFILES],
                    'filter_request_accepted': self.accepted, 'packets_received': self.packets,
                    'physical_clicks_received': self.clicks,
                    'hardware_input_observed': self.packets > 0}

    def reset_wheel_buffer(self):
        with self.lock:self.router.wheel_remainder=0

    def start(self, device, expected_hardware=None):
        with self.lock:
            if not self.thread or not self.thread.is_alive() or self.closed.is_set():
                raise ValueError('输入服务不可用，请重新启动工具')
            selected = next((d for d in self.refresh() if d['driver_id'] == device), None)
            if selected is None:
                raise ValueError('未找到已确认的无线鼠标，请插入接收器后刷新')
            expected = expected_hardware or selected['hardware']
            if hardware_key(selected['hardware']) != hardware_key(expected):
                raise ValueError('设备身份已变化，请刷新接收器后重新选择')
            self.stop()
            handle = event = None
            verified = False
            try:
                handle = self.transport.open(device)
                hardware = self.transport.hardware(handle)
                if not receiver_profile(hardware) or hardware_key(hardware) != hardware_key(expected):
                    raise ValueError('设备身份已变化，拒绝接管其他鼠标')
                verified = True
                event = self.transport.event()
                self.transport.set_event(handle, event)
                # 成功返回证明驱动接受请求；实际输入单独通过数据包/点击计数验证。
                self.transport.set_filter(handle, MOUSE_FILTER_ALL)
                self.router.select(device)
                self.handle, self.event = handle, event
                self.packets = self.clicks = 0
                self.accepted = True
                self.selected = dict(selected)
                self.error = ''
            except Exception:
                if handle is not None:
                    try:
                        if verified:
                            self.transport.set_filter(handle, 0)
                            self.transport.set_event(handle, None)
                    finally:
                        self.transport.close(handle)
                if event is not None:
                    self.transport.close(event)
                raise

    def stop(self):
        with self.lock:
            handle, event = self.handle, self.event
            self.handle = self.event = None
            self.accepted = False
            self.selected = None
            self.router.select(None)
            if handle is not None:
                try:
                    self.transport.set_filter(handle, 0)
                    self.transport.set_event(handle, None)
                except OSError:
                    logging.exception('Device release failed; closing owning handle')
                finally:
                    self.transport.close(handle)
            if event is not None:
                self.transport.close(event)

    def _run(self):
        try:
            while not self.closed.wait(.002):
                actions = []
                with self.lock:
                    if self.handle is not None and self.transport.wait(self.event, 30):
                        packets = self.transport.receive(self.handle)
                        self.packets += len(packets)
                        for packet in packets:
                            actions.extend(self.router.actions(self.router.target, packet.state, packet.rolling))
                        self.clicks += actions.count('increase')
                for action in actions:
                    if action=='increase':self.on_like()
                    else:self.on_like(action)
        except Exception as exc:
            logging.exception('Mouse input bridge failed')
            self.error = str(exc)
            self.stop()
            self.on_error(str(exc))
        finally:
            self.stop()

    def close(self):
        self.closed.set()
        self.stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
