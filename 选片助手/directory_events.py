"""Windows 有名称的异步目录通知；失败或溢出交给既有全量补偿扫描。"""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import struct


class Overlapped(C.Structure):
    _fields_=[('Internal',C.c_size_t),('InternalHigh',C.c_size_t),('Offset',W.DWORD),('OffsetHigh',W.DWORD),('hEvent',W.HANDLE)]


class DirectoryEvents:
    def __init__(self,folder,recursive):
        self.handle=None;self.directory=None;self.paths=None;self.changes=None;self.folder=Path(folder);self.recursive=recursive
        if os.name!='nt':return
        self.kernel=C.WinDLL('kernel32',use_last_error=True);k=self.kernel
        k.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE];k.CreateFileW.restype=W.HANDLE
        k.CreateEventW.argtypes=[C.c_void_p,W.BOOL,W.BOOL,W.LPCWSTR];k.CreateEventW.restype=W.HANDLE
        k.ReadDirectoryChangesW.argtypes=[W.HANDLE,C.c_void_p,W.DWORD,W.BOOL,W.DWORD,C.POINTER(W.DWORD),C.POINTER(Overlapped),C.c_void_p];k.ReadDirectoryChangesW.restype=W.BOOL
        k.GetOverlappedResult.argtypes=[W.HANDLE,C.POINTER(Overlapped),C.POINTER(W.DWORD),W.BOOL];k.GetOverlappedResult.restype=W.BOOL
        k.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD];k.WaitForSingleObject.restype=W.DWORD
        k.ResetEvent.argtypes=[W.HANDLE];k.CloseHandle.argtypes=[W.HANDLE]
        k.CancelIoEx.argtypes=[W.HANDLE,C.POINTER(Overlapped)];k.CancelIoEx.restype=W.BOOL
        directory=k.CreateFileW(str(folder),1,7,None,3,0x02000000|0x40000000,None)
        if directory in (None,C.c_void_p(-1).value):return
        self.directory=directory;self.handle=k.CreateEventW(None,True,False,None)
        self.buffer=C.create_string_buffer(60*1024);self.overlapped=Overlapped();self.overlapped.hEvent=self.handle
        if not self.handle or not self._arm():self.close()

    def _arm(self):
        self.kernel.ResetEvent(self.handle)
        return bool(self.kernel.ReadDirectoryChangesW(self.directory,self.buffer,len(self.buffer),self.recursive,0x1|0x2|0x8|0x10,None,C.byref(self.overlapped),None))

    def changed(self):
        if self.handle is None:return False
        status=self.kernel.WaitForSingleObject(self.handle,0)
        if status==0xffffffff:self.close();return False
        if status!=0:return False
        length=W.DWORD();self.paths=None;self.changes=None
        success=self.kernel.GetOverlappedResult(self.directory,C.byref(self.overlapped),C.byref(length),False)
        if success and length.value:
            try:
                offset=0;paths=[];changes=[];payload=self.buffer.raw[:length.value]
                while True:
                    following,action,size=struct.unpack_from('<III',payload,offset)
                    if size%2 or offset+12+size>len(payload):raise ValueError('invalid notification')
                    name=payload[offset+12:offset+12+size].decode('utf-16-le')
                    path=Path(name)
                    if path.is_absolute() or '..' in path.parts:raise ValueError('invalid notification path')
                    paths.append(self.folder/path)
                    changes.append((action,self.folder/path))
                    if not following:break
                    if following<12+size:raise ValueError('invalid notification offset')
                    offset+=following
                self.paths=list(dict.fromkeys(paths))
                self.changes=changes
            except (ValueError,UnicodeError,struct.error):self.paths=None
        if not self._arm():self.close()
        return True

    def close(self):
        if self.directory is not None:
            if self.handle is not None:
                self.kernel.CancelIoEx(self.directory,C.byref(self.overlapped))
                # 等待取消完成后才释放 OVERLAPPED 与缓冲区，避免内核继续写入已释放内存。
                length=W.DWORD();self.kernel.GetOverlappedResult(self.directory,C.byref(self.overlapped),C.byref(length),True)
            self.kernel.CloseHandle(self.directory);self.directory=None
        if self.handle is not None:self.kernel.CloseHandle(self.handle);self.handle=None
