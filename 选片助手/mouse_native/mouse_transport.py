"""基于 REA 已确认的旧版签名驱动协议，直接检查 Win32 调用结果。

不调用该驱动未实现的 GET_FILTER / GET_PRECEDENCE，避免产生未完成 IRP。
"""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path
from .profiles import LEGACY, describe_device

SUPPORTED_DRIVER_SHA256 = '0f12d47d01864ca5e1eb663a52b3d2c060521e57b68ff99d70e7f01506e400f9'
TARGET_ID = LEGACY.target  # 保留旧控制端和测试的兼容标识；枚举使用独立适配配置。
SET_FILTER = 0x222010
SET_EVENT = 0x222040
READ = 0x222100
HARDWARE_ID = 0x222200
ALLOWED_IOCTLS = frozenset((SET_FILTER, SET_EVENT, READ, HARDWARE_ID))
MOUSE_FILTER_ALL = 0x1FFF


class MousePacket(C.Structure):
    """WDK MOUSE_INPUT_DATA 原始线格式，不是 DLL 的 20 字节 MouseStroke。"""
    _fields_ = [('unit', C.c_ushort), ('flags', C.c_ushort),
                ('state', C.c_ushort), ('rolling', C.c_ushort),
                ('raw_buttons', C.c_uint32), ('x', C.c_int32),
                ('y', C.c_int32), ('information', C.c_uint32)]


class Win32MouseTransport:
    def __init__(self):
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateFileW': ([W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
            'DeviceIoControl': ([W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.c_void_p,
                                 W.DWORD, C.POINTER(W.DWORD), C.c_void_p], W.BOOL),
            'CreateEventW': ([C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
            'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
            'CloseHandle': ([W.HANDLE], W.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result
        installed = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/drivers/mouse.sys'
        if not installed.is_file():
            raise RuntimeError('尚未安装鼠标输入驱动')
        if hashlib.sha256(installed.read_bytes()).hexdigest() != SUPPORTED_DRIVER_SHA256:
            raise RuntimeError('驱动版本不匹配，禁止发送未经核实的设备控制请求')

    def ioctl(self, handle, code, source=None, destination=None):
        if code not in ALLOWED_IOCTLS:
            raise ValueError('禁止调用未经 REA 确认的驱动 IOCTL')
        length = W.DWORD()
        C.set_last_error(0)
        ok = self.kernel.DeviceIoControl(handle, code,
            C.byref(source) if source is not None else None, C.sizeof(source) if source is not None else 0,
            C.byref(destination) if destination is not None else None,
            C.sizeof(destination) if destination is not None else 0, C.byref(length), None)
        if not ok:
            error = C.get_last_error()
            if code == READ and error == 259:
                return 0
            raise C.WinError(error)
        return length.value

    def open(self, device):
        if not 11 <= device <= 20:
            raise ValueError('设备不在鼠标控制节点范围内')
        handle = self.kernel.CreateFileW('\\\\.\\interception%02d' % (device - 1),
                                        0x80000000, 0, None, 3, 0, None)
        if handle == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        return handle

    def hardware(self, handle):
        buffer = C.create_unicode_buffer(1024)
        length = self.ioctl(handle, HARDWARE_ID, destination=buffer)
        if length > C.sizeof(buffer):
            raise RuntimeError('驱动返回的硬件标识长度无效')
        return buffer.value

    def enumerate(self):
        devices = []
        self.enumeration_errors = []
        for device in range(11, 21):
            handle = None
            try:
                handle = self.open(device)
                hardware = self.hardware(handle)
                record = describe_device(device, hardware)
                if record:
                    devices.append(record)
            except OSError as exc:
                self.enumeration_errors.append({'device': device, 'code': exc.winerror})
                if exc.winerror not in (1, 2, 5, 6, 31, 32, 55, 1167):
                    raise
            finally:
                if handle is not None:
                    self.close(handle)
        return devices

    def event(self):
        handle = self.kernel.CreateEventW(None, True, False, None)
        if not handle:
            raise C.WinError(C.get_last_error())
        return handle

    def set_event(self, handle, event):
        handles = (C.c_uint64 * 2)(event or 0, 0)
        self.ioctl(handle, SET_EVENT, source=handles)

    def set_filter(self, handle, mask):
        self.ioctl(handle, SET_FILTER, source=C.c_ushort(mask))

    def wait(self, event, timeout):
        result = self.kernel.WaitForSingleObject(event, timeout)
        if result == 0xFFFFFFFF:
            raise C.WinError(C.get_last_error())
        return result == 0

    def receive(self, handle):
        packets = (MousePacket * 64)()
        length = self.ioctl(handle, READ, destination=packets)
        if length > C.sizeof(packets) or length % C.sizeof(MousePacket):
            raise RuntimeError('驱动返回的鼠标数据长度无效')
        return list(packets[:length // C.sizeof(MousePacket)])

    def close(self, handle):
        if not self.kernel.CloseHandle(handle):
            raise C.WinError(C.get_last_error())
