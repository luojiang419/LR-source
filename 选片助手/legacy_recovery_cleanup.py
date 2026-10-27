"""本次升级首次运行时清理旧恢复目录；完成后不再扫描。"""
from contextlib import contextmanager,ExitStack
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re
import threading
import time

from file_safety import atomic_write

LEGACY_NAME=re.compile(r'\.framekeep-recovery-[0-9a-f]{32}',re.IGNORECASE)
MIGRATION='legacy-recovery-folders-v1'


@contextmanager
def _directory(path):
    """锁住目录的重命名/删除，逐层检查重解析点，防止删除时路径被换向。"""
    if os.name!='nt':raise OSError('当前平台不支持旧恢复目录的安全自动清理')
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
    kernel.CreateFileW.restype=W.HANDLE
    kernel.CloseHandle.argtypes=[W.HANDLE]
    kernel.GetFileInformationByHandleEx.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD]
    kernel.GetFileInformationByHandleEx.restype=W.BOOL
    handle=kernel.CreateFileW(str(path),0x80000000,3,None,3,0x02200000,None)
    if handle==C.c_void_p(-1).value:raise C.WinError(C.get_last_error())
    try:
        info=(W.DWORD*2)()
        if not kernel.GetFileInformationByHandleEx(handle,9,C.byref(info),C.sizeof(info)):
            raise C.WinError(C.get_last_error())
        if not info[0]&0x10 or info[0]&0x400:raise OSError('目录为链接或重解析点，未进入')
        yield
    finally:kernel.CloseHandle(handle)


def _remove_tree(path,stop):
    """只在受锁目录内逐项移除；链接只解除链接，普通子目录先锁住再进入。"""
    released=0
    with _directory(path):
        with os.scandir(path) as entries:
            for entry in entries:
                if stop.is_set():raise InterruptedError('清理已停止，剩余内容下次继续')
                child=Path(entry.path)
                if child.is_junction():child.rmdir()
                elif entry.is_symlink():child.unlink()
                elif entry.is_dir(follow_symlinks=False):released+=_remove_tree(child,stop)
                else:
                    info=entry.stat(follow_symlinks=False)
                    try:child.unlink()
                    except PermissionError:
                        _unlink_readonly(child)
                    released+=info.st_size
    # 父目录仍被调用方锁住；此处仅删空目录，不会递归进入后来替换的路径。
    path.rmdir()
    return released


def _unlink_readonly(path):
    """按句柄删除只读备份的当前链接，不修改硬链接原照片的共享属性。"""
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
    kernel.CreateFileW.restype=W.HANDLE;kernel.CloseHandle.argtypes=[W.HANDLE]
    kernel.SetFileInformationByHandle.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD]
    kernel.SetFileInformationByHandle.restype=W.BOOL
    handle=kernel.CreateFileW(str(path),0x10080,3,None,3,0x00200000,None)
    if handle==C.c_void_p(-1).value:raise C.WinError(C.get_last_error())
    try:
        # FileDispositionInfoEx: DELETE | IGNORE_READONLY_ATTRIBUTE。
        flags=W.DWORD(0x11)
        if not kernel.SetFileInformationByHandle(handle,21,C.byref(flags),C.sizeof(flags)):
            raise C.WinError(C.get_last_error())
    finally:kernel.CloseHandle(handle)


def remove_legacy_folder(folder,root,stop):
    root=Path(root).absolute();folder=Path(folder).absolute()
    if not LEGACY_NAME.fullmatch(folder.name) or folder==root or not folder.is_relative_to(root):
        raise ValueError('恢复目录不在指定清理范围内')
    with ExitStack() as locks:
        # 包括根目录之上的祖先，防止任何父路径在递归删除期间被替换。
        for ancestor in reversed(folder.parents):locks.enter_context(_directory(ancestor))
        if folder.is_junction():folder.rmdir();return 0
        if folder.is_symlink():folder.unlink();return 0
        if not folder.resolve().is_relative_to(root.resolve()):raise ValueError('恢复目录越出清理范围')
        return _remove_tree(folder,stop)


def clean_root(root,stop=None,*,exclude=()):
    stop=stop or threading.Event();root=Path(root).absolute()
    report={'root':str(root),'removed':0,'released_bytes':0,'scanned_directories':0,'errors':[],'complete':False}
    excluded={Path(p).resolve() for p in exclude};root_resolved=root.resolve();stack=[root]
    while stack and not stop.is_set():
        current=stack.pop()
        try:
            if current.is_symlink() or current.is_junction():continue
            resolved=current.resolve()
            if any(resolved==p or resolved.is_relative_to(p) for p in excluded):continue
            if not resolved.is_relative_to(root_resolved):continue
            report['scanned_directories']+=1
            with os.scandir(current) as entries:
                for entry in entries:
                    if stop.is_set():break
                    child=Path(entry.path)
                    if child.absolute() in excluded:continue
                    if LEGACY_NAME.fullmatch(entry.name):
                        if not entry.is_dir(follow_symlinks=False) and not child.is_symlink() and not child.is_junction():continue
                        try:
                            report['released_bytes']+=remove_legacy_folder(child,root,stop)
                            report['removed']+=1
                        except (OSError,ValueError) as exc:
                            if len(report['errors'])<100:report['errors'].append({'path':str(child),'reason':str(exc)[:300]})
                    elif not entry.name.startswith('.') and entry.is_dir(follow_symlinks=False) and not child.is_junction() and not entry.is_symlink():
                        stack.append(child)
        except OSError as exc:
            if len(report['errors'])<100:report['errors'].append({'path':str(current),'reason':str(exc)[:300]})
        # 让出时间片，并允许退出立即取消，不阻塞首屏和业务工作线程。
        stop.wait(.002)
    report['complete']=not stop.is_set()
    return report


class LegacyRecoveryCleanup:
    def __init__(self,data,roots):
        self.data=Path(data);self.roots=roots;self.marker=self.data/'legacy-recovery-cleanup.json'
        self.closed=threading.Event();self.thread=threading.Thread(target=self._run,name='legacy-recovery-cleanup',daemon=True)

    def start(self):self.thread.start()

    def run_once(self):
        try:
            previous=json.loads(self.marker.read_text(encoding='utf8'))
            if previous.get('migration')==MIGRATION and previous.get('complete') is True:return []
        except (OSError,ValueError):pass
        reports=[]
        from preview_cache import _LOCATIONS
        excluded=[self.data,*_LOCATIONS.values()]
        roots=sorted({Path(p).absolute() for p in self.roots() if p},key=lambda p:len(p.parts))
        selected=[]
        for root in roots:
            if any(root.is_relative_to(parent) for parent in selected):continue
            if self.closed.is_set():break
            result=clean_root(root,self.closed,exclude=excluded);reports.append(result)
            if result['complete'] and not result['errors']:selected.append(root)
        # 只有本轮执行结束才记为完成；退出中断后，下次启动继续完成这一次升级清理。
        atomic_write(self.marker,json.dumps({'migration':MIGRATION,'time':time.time(),'complete':not self.closed.is_set(),'roots':reports},ensure_ascii=False,indent=2).encode('utf8'))
        return reports

    def _run(self):
        try:self.run_once()
        except Exception as exc:
            from operation_errors import record_error
            record_error(self.data,'legacy-recovery-cleanup',exc)

    def close(self):
        self.closed.set()
        if self.thread.is_alive():self.thread.join(timeout=2)


def configured_roots(store,catalog=None,watch=None,references=None):
    roots=[s['root'] for s in store.sessions()]
    if catalog:
        with catalog.connect() as c:roots.extend(r[0] for r in c.execute('SELECT path FROM roots WHERE removed=0'))
    if watch:
        state=watch.state()
        if state.get('folder'):roots.append(state['folder'])
    if references:
        with references.lock:roots.extend(r['path'] for r in references.config['roots'] if not r.get('removed'))
    return roots
