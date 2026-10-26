"""独立于 Everything 的只读名称发现和 Windows 增量目录通知。

目录枚举是完整性依据；NTFS MFT 仅提供首批候选，不保证权限或硬链接完整。
不读取文件内容，不创建 USN 日志，不请求提权，不沿重解析点继续遍历。
"""
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
import ctypes as C
from ctypes import wintypes as W
import os
import struct
import threading
import time
from archive_resources import INDEX_WORKERS


_WINDOWS = os.name == 'nt'
_REPARSE = 0x400
_DIRECTORY = 0x10
_EPOCH = 116444736000000000
_IGNORED = {'#recycle', '$recycle.bin', 'system volume information'}
_INVALID = C.c_void_p(-1).value
_DIRECTORY_POOL=ThreadPoolExecutor(max_workers=INDEX_WORKERS,thread_name_prefix='archive-directory')
_DIRECTORY_BATCH=128


def _key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _excluded(path, excludes):
    key = _key(path)
    return any(key == item or key.startswith(item.rstrip(os.sep) + os.sep) for item in excludes)


def _ignored(name):
    return name.casefold() in _IGNORED or name.casefold().startswith('.framekeep')


def _check_stop(stop):
    if stop is not None and stop():
        raise InterruptedError('目录索引已取消')


def _extended(path):
    path = os.path.abspath(os.fspath(path))
    if path.startswith('\\\\?\\'):
        return path
    if path.startswith('\\\\'):
        return '\\\\?\\UNC\\' + path[2:]
    return '\\\\?\\' + path


class _FindData(C.Structure):
    _fields_ = [('attributes', W.DWORD), ('created', W.FILETIME),
                ('accessed', W.FILETIME), ('modified', W.FILETIME),
                ('size_high', W.DWORD), ('size_low', W.DWORD),
                ('reserved0', W.DWORD), ('reserved1', W.DWORD),
                ('name', W.WCHAR * 260), ('alternate', W.WCHAR * 14)]


class _FileInfo(C.Structure):
    _fields_ = [('attributes', W.DWORD), ('created', W.FILETIME),
                ('accessed', W.FILETIME), ('modified', W.FILETIME),
                ('serial', W.DWORD), ('size_high', W.DWORD), ('size_low', W.DWORD),
                ('links', W.DWORD), ('index_high', W.DWORD), ('index_low', W.DWORD)]


class _Overlapped(C.Structure):
    _fields_ = [('internal', C.c_size_t), ('internal_high', C.c_size_t),
                ('offset', W.DWORD), ('offset_high', W.DWORD), ('event', W.HANDLE)]


def _kernel():
    if not _WINDOWS:
        raise OSError('当前平台不支持 Windows 目录接口')
    kernel = C.WinDLL('kernel32', use_last_error=True)
    declarations = {
        'FindFirstFileExW': ([W.LPCWSTR, C.c_int, C.c_void_p, C.c_int, C.c_void_p, W.DWORD], W.HANDLE),
        'FindNextFileW': ([W.HANDLE, C.POINTER(_FindData)], W.BOOL),
        'FindClose': ([W.HANDLE], W.BOOL),
        'GetFileAttributesW': ([W.LPCWSTR], W.DWORD),
        'CreateFileW': ([W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
        'CloseHandle': ([W.HANDLE], W.BOOL),
        'GetFileInformationByHandle': ([W.HANDLE, C.POINTER(_FileInfo)], W.BOOL),
        'DeviceIoControl': ([W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p], W.BOOL),
        'CreateEventW': ([C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
        'ResetEvent': ([W.HANDLE], W.BOOL),
        'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
        'ReadDirectoryChangesW': ([W.HANDLE, C.c_void_p, W.DWORD, W.BOOL, W.DWORD, C.POINTER(W.DWORD), C.POINTER(_Overlapped), C.c_void_p], W.BOOL),
        'GetOverlappedResult': ([W.HANDLE, C.POINTER(_Overlapped), C.POINTER(W.DWORD), W.BOOL], W.BOOL),
        'CancelIoEx': ([W.HANDLE, C.c_void_p], W.BOOL),
    }
    for name, (arguments, result) in declarations.items():
        method = getattr(kernel, name)
        method.argtypes, method.restype = arguments, result
    return kernel


def _win_error(path=None):
    error = C.WinError(C.get_last_error())
    if path is not None:
        error.filename = os.fspath(path)
    return error


def _win_entries(kernel, folder):
    data = _FindData()
    pattern = _extended(folder).rstrip('\\') + '\\*'
    handle = kernel.FindFirstFileExW(pattern, 1, C.byref(data), 0, None, 2)
    if handle == _INVALID:
        code = C.get_last_error()
        if code == 87:  # 某些旧重定向器不支持 LARGE_FETCH，保留 Basic 枚举。
            handle = kernel.FindFirstFileExW(pattern, 1, C.byref(data), 0, None, 0)
            code = C.get_last_error()
        if handle == _INVALID:
            if code == 2:  # 空目录；离线/不存在/权限拒绝使用各自错误码。
                attributes = kernel.GetFileAttributesW(_extended(folder))
                if attributes != 0xffffffff and attributes & _DIRECTORY:
                    return
            raise _win_error(folder)
    try:
        while True:
            name = data.name
            if name not in ('.', '..'):
                ticks = (data.modified.dwHighDateTime << 32) | data.modified.dwLowDateTime
                yield name, data.attributes, ((data.size_high << 32) | data.size_low), (ticks - _EPOCH) * 100
            if not kernel.FindNextFileW(handle, C.byref(data)):
                if C.get_last_error() != 18:
                    raise _win_error(folder)
                break
    finally:
        kernel.FindClose(handle)


def _portable_entries(folder):
    with os.scandir(folder) as entries:
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                # 普通文件在枚举后被删除不妨碍剩余目录遍历。
                continue
            import stat
            attributes = getattr(info, 'st_file_attributes', 0)
            if stat.S_ISDIR(info.st_mode):
                attributes |= _DIRECTORY
            elif not stat.S_ISREG(info.st_mode):
                continue
            yield entry.name, attributes, info.st_size, info.st_mtime_ns


def _directory_batch(entries,stop,aborted):
    rows=[]
    for _ in range(_DIRECTORY_BATCH):
        _check_stop(lambda:aborted.is_set() or stop is not None and stop())
        try:rows.append(next(entries))
        except StopIteration:break
    return rows


def _parallel_tree_records(root,kernel,excludes,stop,on_progress,workers):
    """全局共享枚举池，每个在途任务最多缓冲 128 项，不整目录载入内存。"""
    folders=deque([(root,None)]);active={};opened=set();aborted=threading.Event()
    completed=0;discovered=1
    if on_progress:on_progress(completed,discovered)
    try:
        while folders or active:
            _check_stop(stop)
            while folders and len(active)<workers:
                folder,entries=folders.popleft()
                if entries is None:
                    entries=_win_entries(kernel,folder) if kernel else _portable_entries(folder)
                    opened.add(entries)
                active[_DIRECTORY_POOL.submit(_directory_batch,entries,stop,aborted)]=(folder,entries)
            done,_=wait(active,timeout=.1,return_when=FIRST_COMPLETED)
            for future in done:
                folder,entries=active.pop(future)
                rows=future.result()
                for name,attributes,size,modified_ns in rows:
                    _check_stop(stop)
                    path=os.path.join(folder,name)
                    if _ignored(name) or attributes & _REPARSE or _excluded(path,excludes):continue
                    if attributes & _DIRECTORY:
                        folders.append((path,None));discovered+=1
                    else:yield {'path':path,'size':size,'modified_ns':modified_ns}
                if len(rows)==_DIRECTORY_BATCH:
                    # 优先继续已打开的句柄，防止宽目录树积累大量半读迭代器。
                    folders.appendleft((folder,entries))
                else:
                    opened.remove(entries);entries.close();completed+=1
                if on_progress:on_progress(completed,discovered)
    finally:
        aborted.set()
        for future in active:future.cancel()
        if active:wait(active)
        # 等待正在 next() 的批次退出后再关闭枚举句柄，取消或异常不能泄漏句柄。
        for entries in opened:entries.close()


def iter_tree_records(root, *, exclude=(), stop=None, on_progress=None, workers=1):
    """BFS 返回文件路径、字节数和 mtime；取消/未读完目录抛出错误。

    Windows 一次枚举直接取得元数据，不为每个文件额外调用 stat。
    排除参数是绝对或相对当前工作目录的路径，不是通配符。
    on_progress 返回已完成和已发现目录数；总数随发现子目录增长，并非文件总数。
    workers>1 时并行枚举子目录，返回顺序不固定；默认保持原 BFS 顺序。
    """
    root = os.path.abspath(os.fspath(root))
    excludes = tuple(_key(item) for item in exclude)
    _check_stop(stop)
    if _excluded(root, excludes):
        return
    kernel = _kernel() if _WINDOWS else None
    if kernel is not None:
        attributes = kernel.GetFileAttributesW(_extended(root))
        if attributes == 0xffffffff:
            raise _win_error(root)
        if not attributes & _DIRECTORY or attributes & _REPARSE:
            raise OSError('索引根路径必须为普通目录', root)
    elif not os.path.isdir(root) or os.path.islink(root):
        raise OSError('索引根路径必须为普通目录', root)
    if workers>1:
        yield from _parallel_tree_records(root,kernel,excludes,stop,on_progress,min(workers,INDEX_WORKERS))
        return
    folders = deque([root])
    completed = 0
    while folders:
        _check_stop(stop)
        if on_progress:on_progress(completed,completed+len(folders))
        folder = folders.popleft()
        entries = _win_entries(kernel, folder) if kernel else _portable_entries(folder)
        for name, attributes, size, modified_ns in entries:
            _check_stop(stop)
            path = os.path.join(folder, name)
            if _ignored(name) or attributes & _REPARSE or _excluded(path, excludes):
                continue
            if attributes & _DIRECTORY:
                folders.append(path)
            else:
                yield {'path': path, 'size': size, 'modified_ns': modified_ns}
        completed += 1
        if on_progress:on_progress(completed,completed+len(folders))


def _parse_usn_records(data):
    """解析 ENUM_USN 的 8 字节续传位置及 V2 记录；拒绝损坏边界。"""
    if len(data) < 8:
        raise ValueError('MFT 返回数据不完整')
    offset = 8
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError('MFT 记录头不完整')
        length, major, _minor = struct.unpack_from('<IHH', data, offset)
        if length < 8 or length % 8 or offset + length > len(data):
            raise ValueError('MFT 记录长度无效')
        if major == 2:
            if length < 60:
                raise ValueError('MFT V2 记录不完整')
            frn, parent, usn = struct.unpack_from('<QQq', data, offset + 8)
            attributes = struct.unpack_from('<I', data, offset + 52)[0]
            name_length, name_offset = struct.unpack_from('<HH', data, offset + 56)
            if name_offset < 60 or name_offset % 2 or name_length % 2 or name_offset + name_length > length:
                raise ValueError('MFT 文件名边界无效')
            name = data[offset + name_offset:offset + name_offset + name_length].decode('utf-16-le')
            if name and not any(part in name for part in ('\\', '/', '\x00')):
                yield frn, parent, usn, attributes, name
        offset += length


def ntfs_candidates(root, *, exclude=(), stop=None, limit=20000, timeout=3.0):
    """尽力从本机 NTFS MFT 提取首批名称；失败返回空，永不据此标记扫描完成。

    FRN 每个文件仅代表一个名字；硬链接、权限、并发重命名以及预算截断均需
    iter_tree_records 补齐。这里只登记名称，调用方在预览前验证访问权限和存在性。
    """
    if not _WINDOWS or limit <= 0 or timeout <= 0:
        return
    root = os.path.abspath(os.fspath(root))
    drive, tail = os.path.splitdrive(root)
    if len(drive) != 2 or drive[1] != ':':
        return  # SMB/UNC 没有本机 MFT 接口。
    excludes = tuple(_key(item) for item in exclude)
    if _excluded(root, excludes):
        return
    kernel = _kernel()
    handle = kernel.CreateFileW(_extended(root), 0, 7, None, 3, 0x02200000, None)
    if handle in (None, _INVALID):
        return
    info = _FileInfo()
    try:
        if not kernel.GetFileInformationByHandle(handle, C.byref(info)):
            return
        if info.attributes & _REPARSE or not info.attributes & _DIRECTORY:
            return
        root_frn = (info.index_high << 32) | info.index_low
    finally:
        kernel.CloseHandle(handle)
    volume = kernel.CreateFileW('\\\\.\\' + drive, 0x80000000, 7, None, 3, 0, None)
    if volume in (None, _INVALID):
        return
    started = time.monotonic()
    buffer = C.create_string_buffer(256 * 1024)
    returned = W.DWORD()
    cursor = 0
    paths = {root_frn: root}
    pending = defaultdict(list)
    emitted = 0
    visited = 0
    try:
        while time.monotonic() - started < timeout and visited < 250000:
            _check_stop(stop)
            arguments = struct.pack('<Qqq', cursor, 0, 0x7fffffffffffffff)
            if not kernel.DeviceIoControl(volume, 0x000900b3, arguments, len(arguments), buffer, len(buffer), C.byref(returned), None):
                return
            raw = buffer.raw[:returned.value]
            if len(raw) < 8:
                return
            next_cursor = struct.unpack_from('<Q', raw)[0]
            try:
                records = _parse_usn_records(raw)
                for record in records:
                    _check_stop(stop)
                    visited += 1
                    if visited % 256 == 0 and time.monotonic() - started >= timeout:
                        return
                    frn, parent, usn, attributes, name = record
                    if frn == root_frn:
                        continue
                    if parent not in paths:
                        pending[parent].append(record)
                        continue
                    ready = deque([record])
                    while ready:
                        _check_stop(stop)
                        child_frn, child_parent, child_usn, child_attributes, child_name = ready.popleft()
                        parent_path = paths.get(child_parent)
                        path = os.path.join(parent_path, child_name) if parent_path else None
                        blocked = path is None or _ignored(child_name) or child_attributes & _REPARSE or _excluded(path, excludes)
                        if child_attributes & _DIRECTORY:
                            paths[child_frn] = None if blocked else path
                            ready.extend(pending.pop(child_frn, ()))
                        elif not blocked:
                            yield {'path': path, 'size': None, 'modified_ns': None,
                                   'version': f'{child_frn}:{child_usn}'}
                            emitted += 1
                            if emitted >= limit:
                                return
            except (ValueError, UnicodeError):
                return
            if next_cursor <= cursor:
                return
            cursor = next_cursor
    finally:
        kernel.CloseHandle(volume)


def _parse_notifications(data):
    paths = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError('目录通知头不完整')
        following, action, size = struct.unpack_from('<III', data, offset)
        end = offset + 12 + size
        if size % 2 or end > len(data) or action not in range(1, 6):
            raise ValueError('目录通知边界无效')
        name = data[offset + 12:end].decode('utf-16-le')
        if not name or '\x00' in name or name.startswith(('\\', '/')) or ':' in name or '..' in name.replace('\\', '/').split('/'):
            raise ValueError('目录通知路径无效')
        paths.append(name)
        if not following:
            break
        if following % 4 or following < 12 + size or offset + following >= len(data):
            raise ValueError('目录通知偏移无效')
        offset += following
    return list(dict.fromkeys(paths))


class DirectoryChanges:
    """Overlapped 原生通知；poll 不阻塞，缓冲区丢失时明确报告 overflow。

    无后台 Python 线程；close 取消并等待正在执行的 I/O 完成后才释放缓冲区。
    调用方串行 poll/close，目录重命名返回旧名和新名，需自行处理对应子树。
    """
    def __init__(self, root, recursive=True):
        self.root = os.path.abspath(os.fspath(root))
        self.kernel = _kernel()
        self.recursive = bool(recursive)
        self.handle = None
        self.event = None
        self.pending = False
        self.error = ''
        self.buffer = C.create_string_buffer(32 * 1024)
        self.overlapped = _Overlapped()
        handle = self.kernel.CreateFileW(_extended(self.root), 1, 7, None, 3, 0x42000000, None)
        if handle in (None, _INVALID):
            raise _win_error(self.root)
        self.handle = handle
        self.event = self.kernel.CreateEventW(None, True, False, None)
        if not self.event:
            error = _win_error(self.root)
            self.close()
            raise error
        self.overlapped.event = self.event
        try:
            self._arm()
        except OSError:
            self.close()
            raise

    @property
    def active(self):
        return self.handle is not None and self.pending

    def _arm(self):
        self.kernel.ResetEvent(self.event)
        if not self.kernel.ReadDirectoryChangesW(self.handle, self.buffer, len(self.buffer), self.recursive,
                                                0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x40,
                                                None, C.byref(self.overlapped), None):
            if C.get_last_error() != 997:
                raise _win_error(self.root)
        self.pending = True

    def poll(self):
        result = {'paths': [], 'overflow': False}
        if not self.active:
            return result
        wait = self.kernel.WaitForSingleObject(self.event, 0)
        if wait == 258:
            return result
        size = W.DWORD()
        if wait != 0:
            self.error = '目录通知等待失败'
            result['overflow'] = True
            self.close()
            return result
        if not self.kernel.GetOverlappedResult(self.handle, C.byref(self.overlapped), C.byref(size), False):
            self.pending = False
            self.error = str(_win_error(self.root))
            result['overflow'] = True
            self.close()
            return result
        self.pending = False
        if not size.value:
            result['overflow'] = True
        else:
            try:
                result['paths'] = _parse_notifications(self.buffer.raw[:size.value])
            except (ValueError, UnicodeError) as error:
                self.error = str(error)
                result['overflow'] = True
        try:
            self._arm()
        except OSError as error:
            self.error = str(error)
            result['overflow'] = True
            self.close()
        return result

    def close(self):
        if self.handle is not None:
            if self.pending:
                self.kernel.CancelIoEx(self.handle, C.byref(self.overlapped))
                size = W.DWORD()
                self.kernel.GetOverlappedResult(self.handle, C.byref(self.overlapped), C.byref(size), True)
                self.pending = False
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.event is not None:
            self.kernel.CloseHandle(self.event)
            self.event = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
