"""文件提交与自动清理的最小安全原语；不递归删除，不覆盖新出现的文件。"""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path
import tempfile


def atomic_write(path,payload):
    """仅用于程序自有配置；独占随机临时文件，禁止接管同名临时文件。"""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.framekeep-config-',suffix='.tmp',delete=False) as stream:
        temporary=Path(stream.name)
        try:stream.write(payload);stream.flush();os.fsync(stream.fileno())
        except BaseException:
            stream.close();temporary.unlink(missing_ok=True);raise
    try:os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def photo_directory(folder,data):
    root=Path(folder).resolve();internal=Path(data).resolve()
    if root==internal or root.is_relative_to(internal):
        raise ValueError('照片目录不能位于软件资料或缓存目录内，请选择独立的拍摄文件夹')
    import preview_cache
    active=preview_cache._LOCATIONS.get(internal)
    if active and (root==active or root.is_relative_to(active)):
        raise ValueError('照片目录不能位于当前图片缓存目录内')
    return root


def publish_new(source,target):
    """原子提交新文件；目标在检查后出现时也必须失败。"""
    if os.name=='nt':
        # Windows rename 默认拒绝已存在的目标；不可改成 replace。
        os.rename(source,target)
    else:
        os.link(source,target)
        Path(source).unlink()


def replace_verified(source,target,expected_digest):
    """经版本校验后原子替换；不保留完整原文件副本。"""
    source,target=Path(source),Path(target)
    if target.is_symlink() or target.is_junction():raise ValueError('目标为链接，未改写原文件')
    if expected_digest is None:
        publish_new(source,target);return None
    if os.name!='nt':raise OSError('当前平台不支持安全元数据提交')
    with target.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=expected_digest:
            raise ValueError('提交前检测到外部修改，未覆盖')
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.ReplaceFileW.argtypes=[W.LPCWSTR,W.LPCWSTR,W.LPCWSTR,W.DWORD,C.c_void_p,C.c_void_p]
    kernel.ReplaceFileW.restype=W.BOOL
    if not kernel.ReplaceFileW(str(target),str(source),None,0,None,None):
        raise OSError(f'安全提交失败（{C.get_last_error()}），原文件未被替换')


def delete_verified(path,digest):
    """Windows 同一文件句柄内校验和删除，拒绝并发写入/替换及重解析点。"""
    if os.name!='nt':
        # 不具备按句柄删除保护的平台保留缓存，不能退回检查后按路径 unlink。
        raise OSError('当前平台不支持安全自动缓存清理')
    import msvcrt
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
    kernel.CreateFileW.restype=W.HANDLE
    kernel.CloseHandle.argtypes=[W.HANDLE]
    kernel.SetFileInformationByHandle.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD]
    kernel.SetFileInformationByHandle.restype=W.BOOL
    handle=kernel.CreateFileW(str(path),0x80010000,1,None,3,0x00200000,None)
    if handle==C.c_void_p(-1).value:raise C.WinError(C.get_last_error())
    try:fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:kernel.CloseHandle(handle);raise
    with os.fdopen(fd,'rb') as stream:
        stat=os.fstat(stream.fileno())
        if stat.st_file_attributes&0x400 or stat.st_nlink!=1:return False
        actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=digest:return False
        # FileDispositionInfo 的 BOOLEAN 是单字节；关闭本句柄才完成删除。
        delete=C.c_ubyte(1)
        if not kernel.SetFileInformationByHandle(handle,4,C.byref(delete),C.sizeof(delete)):
            raise C.WinError(C.get_last_error())
    return True

