"""用普通文件的排他句柄保护驱动访问；进程退出会由系统释放。"""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import tempfile
import json
import urllib.request


def check_heartrelay():
    """旧控制端没有共享本项目的进程锁，启用前只读检查其已知服务。"""
    try:
        with urllib.request.urlopen('http://127.0.0.1:18765/api/state', timeout=.4) as response:
            state = json.load(response)
    except (OSError, ValueError):
        return
    from .profiles import PROFILES
    if state.get('active') and state.get('driver_diagnostics', {}).get('target') in {p.target for p in PROFILES}:
        raise ValueError('HeartRelay 正在接管此鼠标，请先在原工具停止专属点赞')


class ReceiverLease:
    def __init__(self):
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
        self.kernel.CreateFileW.restype = W.HANDLE
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        # 新旧配置共用枚举器。沿用旧锁名，使旧版 LR 也不能同时访问控制节点。
        path = str(Path(tempfile.gettempdir()) / 'framekeep-VID_367B-PID_1919.lock')
        # OPEN_ALWAYS，不写文件；普通文件系统强制 share=0，禁止并行枚举/接管。
        self.handle = self.kernel.CreateFileW(path, 0x80000000, 0, None, 4, 0, None)
        if self.handle == C.c_void_p(-1).value:
            raise ValueError('另一个末帧进程已连接此接收器，请关闭那个程序后重试')

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
