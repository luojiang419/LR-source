"""无线鼠标遥控会话：单窗口所有权、可重取事件、失联恢复。"""
from collections import deque
import json
from pathlib import Path
import threading
import time
import uuid

from mouse_native.interception import Interception
from mouse_native.recovery import RecoveryHotkey
from mouse_native.lease import ReceiverLease, check_heartrelay
from mouse_native.profiles import hardware_key, receiver_profile


class MouseRemote:
    TIMEOUT = 10

    def __init__(self, bridge_factory=Interception, hotkey_factory=RecoveryHotkey, clock=time.monotonic,
                 lease_factory=ReceiverLease, legacy_guard=check_heartrelay, settings_path=None):
        self.lock = threading.RLock()
        self.bridge_factory, self.hotkey_factory, self.clock = bridge_factory, hotkey_factory, clock
        self.bridge = self.hotkey = None
        self.lease = None
        self.lease_factory, self.legacy_guard = lease_factory, legacy_guard
        self.owner = self.generation = None
        self.events = deque()
        self.settings_path=Path(settings_path) if settings_path else None
        self.options={'right_decrease':False,'wheel_browse':False,'rating_mode':'incremental','auto_enable':False}
        self.preferred_receiver = ''
        if self.settings_path and self.settings_path.is_file():
            try:
                saved=json.loads(self.settings_path.read_text(encoding='utf8'))
                for key in ('right_decrease','wheel_browse','auto_enable'):
                    if type(saved.get(key)) is bool:self.options[key]=saved[key]
                if saved.get('rating_mode') in ('incremental','fixed'):self.options['rating_mode']=saved['rating_mode']
                preferred = saved.get('preferred_receiver')
                if receiver_profile(preferred):self.preferred_receiver = preferred
            except (OSError,ValueError,AttributeError):pass
        self.sequence = 0
        self.last_seen = 0
        self.message = '未启用；无线鼠标仍为普通鼠标'
        self.closed = threading.Event()
        self.watch = threading.Thread(target=self._watch, name='mouse-watchdog', daemon=True)
        self.watch.start()

    def _ensure_bridge(self):
        # 输入线程在设备错误后退出；用户重新插入并刷新时重建，继续持有进程锁。
        if self.bridge is not None and self.bridge.error and self.owner is None:
            thread = self.bridge.thread
            if thread is None or not thread.is_alive():
                self.bridge.close()
                self.bridge = None
        if self.bridge is None:
            if self.lease is None:
                self.lease = self.lease_factory()
            self.bridge = self.bridge_factory(None, self._click, self._error)
        return self.bridge

    def state(self, refresh=False):
        with self.lock:
            if refresh:
                try:
                    if self.owner is None:
                        self.legacy_guard()
                    self._ensure_bridge()
                except ValueError as exc:
                    return {'active': self.owner is not None, 'devices': [], 'options':dict(self.options),'message': str(exc), 'error': str(exc), 'diagnostics': {}}
            bridge = self._ensure_bridge() if refresh else self.bridge
            devices = bridge.refresh() if refresh else (bridge.devices if bridge else [])
            return {'active': self.owner is not None, 'devices': devices,'options':dict(self.options),
                    'message': self.message, 'error': bridge.error if bridge else '',
                    'hotkey': 'Ctrl+Alt+F11',
                    'diagnostics': bridge.diagnostics() if bridge else {}}

    def activate_driver(self, activate=None):
        """保持枚举互斥，释放旧句柄后才允许系统重新挂接目标设备。"""
        from mouse_native.activation import activate as native_activate
        with self.lock:
            if self.owner is not None:
                return {'usable': True, 'restart_required': False, 'message': '鼠标遥控正在使用，无需重新加载'}
            self.legacy_guard()
            if self.lease is None:
                self.lease = self.lease_factory()
            if self.bridge is not None:
                self.bridge.close()
                self.bridge = None
            return (activate or native_activate)()

    def start(self, client, device):
        if not isinstance(client, str) or not 8 <= len(client) <= 100:
            raise ValueError('工作台窗口标识无效，请刷新后重试')
        with self.lock:
            if self.owner is not None:
                raise ValueError('已有窗口启用鼠标遥控，请先停止')
            self.legacy_guard()
            bridge = self._ensure_bridge()
            devices = bridge.refresh()
            expected_hardware = None
            if isinstance(device, dict):
                selection = device
                device = selection.get('driver_id')
                expected_hardware = selection.get('hardware')
                profile = receiver_profile(expected_hardware)
                if not profile or selection.get('profile') != profile.id:
                    raise ValueError('接收器选择无效，请刷新设备后重试')
            if type(device) is not int:
                raise ValueError('接收器编号无效')
            if device not in [d['driver_id'] for d in devices]:
                raise ValueError(bridge.error or '未找到目标接收器，或设备正被其他工具占用')
            selected = next(d for d in devices if d['driver_id'] == device)
            if expected_hardware is not None and hardware_key(selected['hardware']) != hardware_key(expected_hardware):
                raise ValueError('设备身份已变化，请刷新接收器后重新选择')
            profile = receiver_profile(selected['hardware'])
            if not profile:
                raise ValueError('接收器尚未适配')
            if sum(receiver_profile(d['hardware']) == profile for d in devices) > 1:
                raise ValueError('检测到多只同型号接收器，请只保留要遥控的那一只')
            if self.hotkey is None:
                self.hotkey = self.hotkey_factory(self._hotkey_release)
            if getattr(self.hotkey, 'error', ''):
                raise ValueError(self.hotkey.error)
            preferred = 'HID\\' + hardware_key(selected['hardware'])
            self._save_settings(self.options, preferred)
            self.preferred_receiver = preferred
            bridge.start(device, expected_hardware=selected['hardware'])
            self.owner, self.generation = client, uuid.uuid4().hex
            self.last_seen = self.clock()
            self.events.clear()
            self.message = profile.label + '遥控已开启：' + self._control_hint()
            return {'generation': self.generation, 'sequence': self.sequence, **self.state()}

    def auto_start(self, client):
        with self.lock:
            if not self.options['auto_enable']:
                return self.state()
            self.expire()
            if self.owner is not None:
                raise ValueError('已有窗口启用鼠标遥控，请先停止')
            self.legacy_guard()
            bridge = self._ensure_bridge()
            devices = bridge.refresh()
            candidates = [d for d in devices if receiver_profile(d['hardware']) and
                          (not self.preferred_receiver or hardware_key(d['hardware']) == hardware_key(self.preferred_receiver))]
            if not candidates:
                raise ValueError(bridge.error or '自动启用未完成：未找到上次使用的接收器，请插入后刷新设备或手动选择')
            if len(candidates) != 1:
                raise ValueError('自动启用未完成：检测到多只接收器，请先手动选择；同型号仅保留一只')
            selected = candidates[0]
            return self.start(client, {**selected, 'profile': receiver_profile(selected['hardware']).id})

    def _save_settings(self, options, preferred=None):
        if not self.settings_path:
            return
        saved = dict(options)
        preferred = self.preferred_receiver if preferred is None else preferred
        if preferred:saved['preferred_receiver'] = preferred
        self.settings_path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.settings_path.with_suffix('.tmp')
        temp.write_text(json.dumps(saved,ensure_ascii=False),encoding='utf8')
        temp.replace(self.settings_path)

    def _hotkey_release(self, message):
        self.stop(message)

    def _error(self, error):
        self.stop('输入设备已断开或不可用：' + error)

    def configure(self,options,client=None):
        if not isinstance(options,dict) or not options:raise ValueError('鼠标功能设置无效')
        for key,value in options.items():
            if key=='rating_mode':
                if value not in ('incremental','fixed'):raise ValueError('鼠标标星方案无效')
            elif key not in ('right_decrease','wheel_browse','auto_enable') or type(value) is not bool:raise ValueError('鼠标功能开关无效')
        with self.lock:
            if self.owner is not None and client!=self.owner:raise ValueError('遥控由其他窗口启用，请先停止后修改功能')
            updated={**self.options,**options}
            self._save_settings(updated)
            self.options=updated
            if options.get('auto_enable') is False and self.owner is not None:
                self.stop('全自动切换已关闭，恢复普通鼠标')
            if self.owner is not None:self.message=self.message.split('遥控已开启：',1)[0]+'遥控已开启：'+self._control_hint()
            if self.bridge:self.bridge.reset_wheel_buffer()
            self.events=deque(e for e in self.events if self._allowed(e['action']))
            return self.state()

    def _allowed(self,action):
        if action in ('previous','next'):return self.options['wheel_browse']
        if self.options['rating_mode']=='fixed':return action in ('set_one','set_two')
        return action=='increase' or action=='decrease' and self.options['right_decrease']

    def _control_hint(self):
        return '左键一星、右键两星，重复点击不累加' if self.options['rating_mode']=='fixed' else '左键每次加一星，五星封顶'

    def _click(self,action='increase'):
        with self.lock:
            if self.options['rating_mode']=='fixed':action={'increase':'set_one','decrease':'set_two'}.get(action,action)
            if self.owner is None or not self._allowed(action):
                return
            if len(self.events) >= 256:
                self.stop('工作台未及时接收操作，已恢复普通鼠标')
                return
            self.sequence += 1
            self.events.append({'sequence': self.sequence, 'time_ms': time.time_ns() / 1_000_000,'action':action})

    def poll(self, client, generation, after):
        with self.lock:
            self.expire()
            if self.owner != client or self.generation != generation or self.owner is None:
                return {'active': False, 'events': [], 'message': self.message}
            if type(after) is not int or after < 0 or after > self.sequence:
                raise ValueError('无效的鼠标事件序号')
            self.last_seen = self.clock()
            while self.events and self.events[0]['sequence'] <= after:
                self.events.popleft()
            return {'active': True, 'events': list(self.events), 'message': self.message}

    def stop(self, message='已停止遥控，恢复普通鼠标'):
        with self.lock:
            self.owner = None
            self.events.clear()
            if self.bridge:
                self.bridge.stop()
            self.message = message
            return self.state()

    def expire(self):
        with self.lock:
            if self.owner is not None and self.clock() - self.last_seen > self.TIMEOUT:
                self.stop('工作台失联超过 10 秒，已恢复普通鼠标')

    def _watch(self):
        while not self.closed.wait(.2):
            self.expire()

    def close(self):
        self.closed.set()
        try:
            self.stop('程序退出，已恢复普通鼠标')
            if self.bridge:
                self.bridge.close()
        finally:
            try:
                if self.hotkey:
                    self.hotkey.close()
            finally:
                if self.lease:
                    self.lease.close()
                self.watch.join(timeout=2)
