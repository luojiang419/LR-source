"""已确认接收器的独立适配；接口和集合属于设备身份，不能忽略。"""
from dataclasses import dataclass
import re


def hardware_key(hardware):
    if not isinstance(hardware, str):
        return ''
    identity = hardware.upper().split('\\')
    if len(identity) < 2 or identity[0] != 'HID':
        return ''
    return re.sub(r'&REV_[0-9A-F]{4}(?=&|$)', '', identity[1])


@dataclass(frozen=True)
class ReceiverProfile:
    id: str
    label: str
    target: str
    interface: str = ''
    collection: str = ''

    def matches(self, hardware):
        parts = hardware_key(hardware).split('&')
        required = self.target.split('&')
        if self.interface:
            required.append(self.interface)
        if self.collection:
            required.append(self.collection)
        return all(part in parts for part in required)


LEGACY = ReceiverProfile('legacy-367b-1919', '原无线鼠标', 'VID_367B&PID_1919')
LOGITECH = ReceiverProfile('logitech-c534', '罗技无线鼠标 · C534',
                           'VID_046D&PID_C534', 'MI_01', 'COL01')
PROFILES = (LEGACY, LOGITECH)


def receiver_profile(hardware):
    return next((profile for profile in PROFILES if profile.matches(hardware)), None)


def describe_device(device, hardware):
    profile = receiver_profile(hardware)
    if profile is None:
        return None
    return {'id': 'driver:' + str(device), 'driver_id': device, 'hardware': hardware,
            'profile': profile.id, 'label': profile.label, 'target': profile.target}
