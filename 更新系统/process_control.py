"""更新专用进程句柄：按安装路径识别，保留句柄防止 PID 复用，保护独立更新器。"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path


def _kernel():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    return kernel


def process_parents():
    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                    ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                    ('name', wintypes.WCHAR * 260)]
    kernel = _kernel()
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = Entry(size=ctypes.sizeof(Entry)); result = {}
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            result[entry.pid] = entry.parent
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
        return result
    finally:
        kernel.CloseHandle(snapshot)


class Process:
    def __init__(self, pid):
        self.pid = pid; self.kernel = _kernel(); self.handle = None
        # 从发现实例起保留原始句柄，终止时不按 PID 重新打开进程。
        self.handle = self.kernel.OpenProcess(1 | 0x1000 | 0x100000, False, pid)
        self.can_terminate = bool(self.handle)
        if not self.handle:
            self.handle = self.kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD(32768); path = ctypes.create_unicode_buffer(size.value)
            if not self.kernel.QueryFullProcessImageNameW(self.handle, 0, path, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.path = os.path.normcase(os.path.abspath(path.value))
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not self.kernel.GetProcessTimes(self.handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.created = created.dwHighDateTime << 32 | created.dwLowDateTime
        except BaseException:
            self.close(); raise

    def alive(self):
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 258

    def terminate(self):
        if not self.alive(): return
        if not self.can_terminate:
            raise PermissionError(f'无法结束软件进程 {self.pid}，请使用相同账户或管理员权限运行安装器')
        if not self.kernel.TerminateProcess(self.handle, 1) and self.alive():
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle); self.handle = None


class ApplicationProcesses:
    def __init__(self, root):
        root = Path(root)
        self.paths = {os.path.normcase(os.path.abspath(root / name)) for name in
                      ('SHIYING 数字资产.exe', '末帧选片.exe', 'engine/framekeep-engine.exe',
                       # ADB服务会脱离启动它的短命令进程，不能只靠主程序进程树发现。
                       'engine/_internal/runtime/adb/adb.exe', 'engine/runtime/adb/adb.exe',
                       'runtime/adb/adb.exe')}
        self.processes = {}; self.roots = set(); self.protected = {os.getpid()}

    def refresh(self):
        if os.name != 'nt': return
        parents = process_parents(); opened = {}
        try:
            for pid in parents:
                if pid in self.processes or pid in self.protected: continue
                try: process = Process(pid)
                except OSError: continue  # 其它系统进程可能不可查询；目录复核仍为最终屏障。
                opened[pid] = process
                if process.path in self.paths:
                    self.roots.add(pid)
            # worker 可能由旧引擎派生；其原生窗口/父进程以及整条分支不能被一起杀掉。
            cursor = os.getpid(); seen = set()
            while cursor in parents and cursor not in seen:
                seen.add(cursor)
                if cursor in self.roots: break
                self.protected.add(cursor); cursor = parents[cursor]
            # 已退出父进程的句柄仍保留，用于发现退出前最后派生、现已成为孤儿的子进程。
            selected = set(self.processes)
            selected.update(pid for pid in self.roots if pid not in self.protected and pid in opened)
            while True:
                children = {pid for pid, parent in parents.items() if parent in selected and
                            pid not in self.protected and pid in opened and pid not in selected and
                            opened[pid].created >= (self.processes.get(parent) or opened[parent]).created}
                if not children: break
                selected.update(children)
            for pid in selected:
                if pid in opened: self.processes[pid] = opened.pop(pid)
        finally:
            for process in opened.values(): process.close()

    def alive(self):
        return [process for process in self.processes.values() if process.alive()]

    def terminate(self, before_each=lambda: None):
        self.refresh()
        # 先结束桌面/引擎，防止其继续创建新的后台任务；其它子进程逐个用句柄结束。
        for process in sorted(self.alive(), key=lambda p: p.pid not in self.roots):
            before_each()
            process.terminate()

    def __enter__(self):
        try: self.refresh(); return self
        except BaseException:
            self.__exit__(None, None, None); raise

    def __exit__(self, *_):
        for process in self.processes.values(): process.close()
