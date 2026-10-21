"""通过 Windows Shell 定位库内原文件，不接受任意外部路径。"""
import ctypes as C
from ctypes import wintypes as W
import json
import os

import media_engine as media


def reveal_path(path):
    if os.name!='nt':raise ValueError('在文件夹中选中当前仅支持 Windows')
    ole=C.OleDLL('ole32');shell=C.OleDLL('shell32')
    ole.CoInitializeEx.argtypes=[C.c_void_p,W.DWORD];ole.CoInitializeEx.restype=C.c_long
    ole.CoUninitialize.argtypes=[];ole.CoUninitialize.restype=None
    ole.CoTaskMemFree.argtypes=[C.c_void_p];ole.CoTaskMemFree.restype=None
    shell.SHParseDisplayName.argtypes=[W.LPCWSTR,C.c_void_p,C.POINTER(C.c_void_p),W.DWORD,C.c_void_p];shell.SHParseDisplayName.restype=C.c_long
    shell.SHOpenFolderAndSelectItems.argtypes=[C.c_void_p,W.UINT,C.c_void_p,W.DWORD];shell.SHOpenFolderAndSelectItems.restype=C.c_long
    pidl=C.c_void_p();initialized=False
    try:
        # HTTP 工作线程独立初始化 COM；RPC_E_CHANGED_MODE 时沿用已有公寓。
        try:ole.CoInitializeEx(None,2);initialized=True
        except OSError as exc:
            if getattr(exc,'winerror',None)!=-2147417850:raise
        shell.SHParseDisplayName(str(path),None,C.byref(pidl),0,None)
        shell.SHOpenFolderAndSelectItems(pidl,0,None,0)
    except OSError as exc:raise ValueError('无法在资源管理器中选中照片：'+str(exc)) from None
    finally:
        if pidl:ole.CoTaskMemFree(pidl)
        if initialized:ole.CoUninitialize()


def reveal_capture(store,sid,cid,component=None):
    session=store.session(sid)
    if not session:raise ValueError('照片目录不存在')
    if component not in (None,'jpg','raw'):raise ValueError('文件组件无效')
    with store.connect() as c:row=c.execute('SELECT data FROM captures WHERE session=? AND id=?',(sid,cid)).fetchone()
    if not row:raise ValueError('照片不存在')
    record=json.loads(row[0]);kinds=[component] if component else ['jpg','raw']
    for kind in kinds:
        if not record.get(kind):continue
        try:path=media.resolved_file(session['root'],record[kind])
        except (OSError,ValueError):continue
        if path.is_file():reveal_path(path);return {'ok':True,'component':kind}
    raise ValueError('照片已移动或目录离线，请先重新定位')


def open_session_folder(store,sid):
    session=store.session(sid)
    if not session or store.is_removed(sid):raise ValueError('文件夹不存在或已移除')
    if os.name!='nt':raise ValueError('打开路径当前仅支持 Windows')
    try:os.startfile(session['root'])
    except OSError as exc:raise ValueError('无法打开文件夹，请检查路径和网络连接：'+str(exc)) from None
    return {'ok':True}
