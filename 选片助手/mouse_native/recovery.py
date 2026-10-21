"""独立消息线程中的全局恢复键；注册失败时禁止接管。"""
import ctypes as C
from ctypes import wintypes as W
import threading


class RecoveryHotkey:
    def __init__(self, release):
        self.release = release
        self.ready = threading.Event()
        self.thread_id = None
        self.error = ''
        self.thread = threading.Thread(target=self._run, name='mouse-recovery', daemon=True)
        self.thread.start()
        if not self.ready.wait(3) or self.error:
            self.close()
            raise ValueError(self.error or '恢复快捷键启动超时')

    def _run(self):
        registered = False
        try:
            user = C.WinDLL('user32', use_last_error=True)
            kernel = C.WinDLL('kernel32', use_last_error=True)
            user.RegisterHotKey.argtypes = [W.HWND, C.c_int, W.UINT, W.UINT]
            user.UnregisterHotKey.argtypes = [W.HWND, C.c_int]
            user.GetMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
            user.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
            kernel.GetCurrentThreadId.restype = W.DWORD
            self.thread_id = kernel.GetCurrentThreadId()
            msg = W.MSG()
            user.PeekMessageW(C.byref(msg), None, 0, 0, 0)
            registered = bool(user.RegisterHotKey(None, 1, 0x4003, 0x7A))
            if not registered:
                raise ValueError('Ctrl+Alt+F11 已被占用，无法启用鼠标遥控')
            self.ready.set()
            while True:
                result = user.GetMessageW(C.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise C.WinError(C.get_last_error())
                if msg.message == 0x0312:
                    self.release('已通过 Ctrl+Alt+F11 恢复普通鼠标')
        except Exception as exc:
            self.error = str(exc)
            if registered:
                self.release('恢复快捷键不可用，已停止遥控')
        finally:
            if registered:
                user.UnregisterHotKey(None, 1)
            self.ready.set()

    def close(self):
        if self.thread_id and self.thread.is_alive():
            user = C.WinDLL('user32', use_last_error=True)
            user.PostThreadMessageW.argtypes = [W.DWORD, W.UINT, W.WPARAM, W.LPARAM]
            user.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
            if self.thread is not threading.current_thread():
                self.thread.join(timeout=2)
