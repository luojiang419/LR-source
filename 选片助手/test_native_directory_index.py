"""原生枚举、通知与 MFT 数据边界验证；只创建临时测试文件。"""
import ctypes as C
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import native_directory_index as native


def notification(name, action=1, following=False):
    encoded = name.encode('utf-16-le')
    length = 12 + len(encoded)
    padded = (length + 3) & ~3
    return struct.pack('<III', padded if following else 0, action, len(encoded)) + encoded + b'\x00' * (padded - length)


def usn(name='照片.jpg', frn=12, parent=5, attributes=0, version=2):
    encoded = name.encode('utf-16-le')
    length = (60 + len(encoded) + 7) & ~7
    data = bytearray(length)
    struct.pack_into('<IHHQQqqIIIIHH', data, 0, length, version, 0, frn, parent, 8, 0,
                     0, 0, 0, attributes, len(encoded), 60)
    data[60:60 + len(encoded)] = encoded
    return bytes(data)


class DirectoryEnumerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='framekeep-native-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_metadata_and_breadth_first(self):
        nested = self.root / '子目录'
        nested.mkdir()
        child = nested / 'LC123.jpg'
        child.write_bytes(b'child')
        top = self.root / 'top.png'
        top.write_bytes(b'top')
        records = list(native.iter_tree_records(self.root))
        self.assertEqual([row['path'] for row in records], [str(top), str(child)])
        self.assertEqual([row['size'] for row in records], [3, 5])
        self.assertAlmostEqual(records[1]['modified_ns'], child.stat().st_mtime_ns, delta=100)

    @unittest.skipUnless(os.name == 'nt', 'Win32 直接枚举验证')
    def test_windows_does_not_stat_each_file(self):
        (self.root / 'a.jpg').write_bytes(b'123')
        with patch.object(native.os, 'stat', side_effect=AssertionError('不应逐文件 stat')):
            records = list(native.iter_tree_records(self.root))
        self.assertEqual(len(records), 1)

    def test_empty_directory_is_complete(self):
        self.assertEqual(list(native.iter_tree_records(self.root)), [])

    def test_parallel_trees_share_one_pool_and_keep_complete_metadata(self):
        roots=[self.root/'first',self.root/'second'];expected=set();peak=active=0
        lock=threading.Lock();overlap=threading.Event();events=[]
        for root in roots:
            for index in range(8):
                folder=root/str(index);folder.mkdir(parents=True)
                photo=folder/'照片.jpg';photo.write_bytes(b'photo');expected.add(str(photo))
            (root/'skip').mkdir();(root/'skip/hidden.jpg').write_bytes(b'excluded')
        original=native._win_entries if native._WINDOWS else native._portable_entries
        def entries(*args):
            nonlocal peak,active
            child=Path(args[-1]).parent in roots
            if child:
                with lock:
                    active+=1;peak=max(peak,active)
                    if active==4:overlap.set()
            try:
                if child and not overlap.wait(5):raise AssertionError('子目录没有并行枚举')
                yield from original(*args)
            finally:
                if child:
                    with lock:active-=1
        with ThreadPoolExecutor(max_workers=4) as pool,patch.object(native,'_DIRECTORY_POOL',pool),patch.object(native,'INDEX_WORKERS',4),patch.object(native,'_win_entries' if native._WINDOWS else '_portable_entries',entries),ThreadPoolExecutor(max_workers=2) as callers:
            def scan(root):
                progress=[]
                rows=list(native.iter_tree_records(root,workers=4,exclude=[root/'skip'],on_progress=lambda *value:progress.append(value)))
                events.append(progress);return rows
            futures=[callers.submit(scan,root) for root in roots]
            rows=[row for future in futures for row in future.result()]
        self.assertEqual(peak,4);self.assertEqual(active,0)
        self.assertEqual({row['path'] for row in rows},expected);self.assertEqual(len(rows),16)
        self.assertTrue(all(row['size']==5 for row in rows))
        self.assertTrue(all(progress[0]==(0,1) and progress[-1]==(9,9) for progress in events))

    def test_parallel_streams_large_directory_and_closes_on_cancel_or_early_exit(self):
        for cancel in (True,False):
            stopped=threading.Event();opened=set();produced=[]
            def entries(*args):
                marker=object();opened.add(marker)
                try:
                    for index in range(10000):
                        produced.append(index);yield f'{index}.jpg',0,10,1700000000000000000
                finally:opened.remove(marker)
            with patch.object(native,'_win_entries' if native._WINDOWS else '_portable_entries',entries):
                stream=native.iter_tree_records(self.root,workers=4,stop=stopped.is_set)
                self.assertEqual(next(stream)['size'],10)
                self.assertLessEqual(len(produced),128)
                if cancel:
                    stopped.set()
                    with self.assertRaises(InterruptedError):next(stream)
                else:stream.close()
                self.assertFalse(opened)

    def test_parallel_read_error_does_not_report_complete(self):
        (self.root/'bad').mkdir();progress=[]
        original=native._win_entries if native._WINDOWS else native._portable_entries
        def entries(*args):
            if Path(args[-1]).name=='bad':raise PermissionError('测试权限拒绝')
            yield from original(*args)
        with patch.object(native,'_win_entries' if native._WINDOWS else '_portable_entries',entries):
            with self.assertRaises(PermissionError):
                list(native.iter_tree_records(self.root,workers=4,on_progress=lambda *value:progress.append(value)))
        self.assertEqual(progress[-1],(1,2))

    def test_parallel_large_subdirectories_bound_open_handles(self):
        active=peak=0;lock=threading.Lock()
        def entries(*args):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            try:
                if Path(args[-1])==self.root:
                    for index in range(20):yield str(index),native._DIRECTORY,0,0
                else:
                    for index in range(300):yield f'{index}.jpg',0,10,1700000000000000000
            finally:
                with lock:active-=1
        with patch.object(native,'_win_entries' if native._WINDOWS else '_portable_entries',entries):
            rows=list(native.iter_tree_records(self.root,workers=4))
        self.assertEqual(len(rows),6000);self.assertEqual(active,0);self.assertLessEqual(peak,4)

    def test_directory_progress_uses_same_pass_and_excludes_ignored_folders(self):
        for name in ('A/child','B','.framekeep-hidden','skip'):(self.root/name).mkdir(parents=True)
        (self.root/'A/a.jpg').write_bytes(b'image')
        events=[];visited=[];original=native._win_entries if native._WINDOWS else native._portable_entries
        def entries(*args):
            visited.append(Path(args[-1]));yield from original(*args)
        with patch.object(native,'_win_entries' if native._WINDOWS else '_portable_entries',entries):
            records=list(native.iter_tree_records(self.root,exclude=[self.root/'skip'],on_progress=lambda done,total:events.append((done,total))))
        self.assertEqual(len(records),1)
        self.assertEqual(len(visited),4);self.assertEqual(len(set(visited)),4)
        self.assertEqual(events[0],(0,1));self.assertEqual(events[-1],(4,4))
        self.assertIn((1,3),events);self.assertIn((2,4),events)

    def test_missing_root_is_an_error(self):
        with self.assertRaises(OSError):
            list(native.iter_tree_records(self.root / 'missing'))

    def test_explicit_and_internal_exclusions(self):
        for name in ('include', 'exclude', 'excluded-looking', '.framekeep-data', '#recycle', '$RECYCLE.BIN', 'System Volume Information'):
            folder = self.root / name
            folder.mkdir()
            (folder / 'a.jpg').write_bytes(b'1')
        rows = list(native.iter_tree_records(self.root, exclude=[self.root / 'exclude']))
        self.assertEqual({Path(row['path']).parent.name for row in rows}, {'include', 'excluded-looking'})

    def test_excluded_root_returns_no_files(self):
        (self.root / 'a.jpg').write_bytes(b'1')
        self.assertEqual(list(native.iter_tree_records(self.root, exclude=[self.root])), [])

    def test_cancel_is_not_a_complete_scan(self):
        (self.root / 'a.jpg').write_bytes(b'1')
        with self.assertRaises(InterruptedError):
            list(native.iter_tree_records(self.root, stop=lambda: True))

    @unittest.skipUnless(os.name == 'nt', 'Win32 错误传播')
    def test_unreadable_subdirectory_is_not_silently_complete(self):
        (self.root / 'child').mkdir()
        original = native._win_entries

        def entries(kernel, folder):
            if Path(folder).name == 'child':
                raise PermissionError('测试权限拒绝')
            yield from original(kernel, folder)

        with patch.object(native, '_win_entries', entries):
            with self.assertRaises(PermissionError):
                list(native.iter_tree_records(self.root))

    @unittest.skipUnless(os.name == 'nt', 'Windows junction')
    def test_reparse_directory_not_followed(self):
        target = self.root / 'target'
        target.mkdir()
        (target / 'a.jpg').write_bytes(b'1')
        link = self.root / 'link'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(str(error))
        self.assertEqual(len(list(native.iter_tree_records(self.root))), 1)


class BinaryBoundaryTests(unittest.TestCase):
    def test_multiple_notification_records_unicode_and_rename(self):
        data = notification('旧名.jpg', 4, True) + notification('目录\\新名.jpg', 5)
        self.assertEqual(native._parse_notifications(data), ['旧名.jpg', '目录\\新名.jpg'])

    def test_notification_rejects_truncated_name_invalid_offset_and_escape(self):
        for data in (b'\x00', notification('a')[:13], struct.pack('<III', 4, 1, 2) + b'a\x00', notification('..\\a'), notification('C:\\a')):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    native._parse_notifications(data)

    def test_usn_multiple_records(self):
        rows = list(native._parse_usn_records(struct.pack('<Q', 88) + usn() + usn('目录', frn=13, attributes=0x10)))
        self.assertEqual(rows[0], (12, 5, 8, 0, '照片.jpg'))
        self.assertEqual(rows[1][3:], (0x10, '目录'))

    def test_usn_unknown_version_skipped(self):
        data = struct.pack('<Q', 88) + usn(version=3) + usn()
        self.assertEqual(len(list(native._parse_usn_records(data))), 1)

    def test_usn_rejects_truncation_and_zero_length(self):
        for data in (b'\0', struct.pack('<Q', 88) + usn()[:-1], struct.pack('<QQ', 88, 0)):
            with self.subTest(data=data[:12]):
                with self.assertRaises(ValueError):
                    list(native._parse_usn_records(data))

    def test_usn_rejects_name_boundary(self):
        record = bytearray(usn())
        struct.pack_into('<H', record, 58, 65534)
        with self.assertRaises(ValueError):
            list(native._parse_usn_records(struct.pack('<Q', 88) + record))

    def test_ntfs_optional_and_zero_budget(self):
        self.assertEqual(list(native.ntfs_candidates('\\\\server\\share')), [])
        self.assertEqual(list(native.ntfs_candidates('G:\\', limit=0)), [])
        self.assertEqual(list(native.ntfs_candidates('G:\\', timeout=0)), [])


@unittest.skipUnless(os.name == 'nt', 'Windows MFT 调用协议')
class NtfsCandidateTests(unittest.TestCase):
    def kernel(self, records):
        kernel = Mock()
        kernel.CreateFileW.side_effect = [10, 11]

        def info(handle, address):
            value = C.cast(address, C.POINTER(native._FileInfo)).contents
            value.attributes = 0x10
            value.index_low = 100
            return True

        kernel.GetFileInformationByHandle.side_effect = info
        buffers = [struct.pack('<Q', 200) + b''.join(records)]

        def ioctl(handle, control, args, arg_size, output, capacity, size, overlapped):
            self.assertEqual(control, 0x000900b3)
            self.assertEqual(struct.unpack('<Qqq', args), (0 if buffers else 200, 0, 0x7fffffffffffffff))
            if not buffers:
                return False
            data = buffers.pop()
            C.memmove(output, data, len(data))
            C.cast(size, C.POINTER(native.W.DWORD)).contents.value = len(data)
            return True

        kernel.DeviceIoControl.side_effect = ioctl
        return kernel

    def test_children_before_parents_and_only_root_subtree(self):
        kernel = self.kernel([usn('child.jpg', frn=12, parent=22),
                              usn('unrelated.jpg', frn=13, parent=77),
                              usn('sub', frn=22, parent=100, attributes=0x10)])
        root = 'G:\\example'
        with patch.object(native, '_kernel', return_value=kernel):
            rows = list(native.ntfs_candidates(root))
        self.assertEqual([row['path'] for row in rows], [root + '\\sub\\child.jpg'])
        self.assertIsNone(rows[0]['size'])
        self.assertEqual(rows[0]['version'], '12:8')
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_excluded_subtree_and_limit(self):
        root = 'G:\\example'
        kernel = self.kernel([usn('skip.jpg', frn=12, parent=22),
                              usn('exclude', frn=22, parent=100, attributes=0x10),
                              usn('first.jpg', frn=13, parent=100),
                              usn('second.jpg', frn=14, parent=100)])
        with patch.object(native, '_kernel', return_value=kernel):
            rows = list(native.ntfs_candidates(root, exclude=[root + '\\exclude'], limit=1))
        self.assertEqual([row['path'] for row in rows], [root + '\\first.jpg'])
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_cancel_closes_volume(self):
        kernel = self.kernel([usn('first.jpg', parent=100), usn('second.jpg', frn=13, parent=100)])
        cancelled = False
        with patch.object(native, '_kernel', return_value=kernel):
            stream = native.ntfs_candidates('G:\\example', stop=lambda: cancelled)
            next(stream)
            cancelled = True
            with self.assertRaises(InterruptedError):
                next(stream)
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_api_unavailable_returns_empty_and_closes(self):
        kernel = self.kernel([])
        kernel.DeviceIoControl.side_effect = None
        kernel.DeviceIoControl.return_value = False
        with patch.object(native, '_kernel', return_value=kernel):
            self.assertEqual(list(native.ntfs_candidates('G:\\example')), [])
        self.assertEqual(kernel.CloseHandle.call_count, 2)


@unittest.skipUnless(os.name == 'nt', 'Windows 原生通知')
class DirectoryChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='framekeep-notify-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.changes = native.DirectoryChanges(self.root)
        self.addCleanup(self.changes.close)

    def wait_for(self, names):
        observed = set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.changes.poll()
            self.assertFalse(result['overflow'], self.changes.error)
            observed.update(result['paths'])
            if set(names) <= observed:
                return observed
            time.sleep(.01)
        self.fail(f'未收到预期目录通知: {set(names) - observed}')

    def test_create_modify_rename_delete(self):
        path = self.root / '照片.jpg'
        path.write_bytes(b'1')
        self.wait_for(['照片.jpg'])
        path.write_bytes(b'123')
        self.wait_for(['照片.jpg'])
        renamed = self.root / 'new.jpg'
        path.rename(renamed)
        self.wait_for(['照片.jpg', 'new.jpg'])
        renamed.unlink()
        self.wait_for(['new.jpg'])

    def test_subtree_creation_and_directory_rename(self):
        folder = self.root / 'sub'
        folder.mkdir()
        self.wait_for(['sub'])
        (folder / 'a.jpg').write_bytes(b'1')
        self.wait_for(['sub\\a.jpg'])
        folder.rename(self.root / 'renamed')
        self.wait_for(['sub', 'renamed'])

    def test_close_cancels_pending_io_and_is_idempotent(self):
        self.assertTrue(self.changes.active)
        started = time.monotonic()
        self.changes.close()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(self.changes.active)
        self.changes.close()
        self.assertEqual(self.changes.poll(), {'paths': [], 'overflow': False})

    def test_zero_byte_completion_requires_rescan(self):
        (self.root / 'a.jpg').write_bytes(b'1')
        deadline = time.monotonic() + 3
        while self.changes.kernel.WaitForSingleObject(self.changes.event, 0) != 0:
            if time.monotonic() > deadline:
                self.fail('目录通知未就绪')
            time.sleep(.01)
        kernel = self.changes.kernel
        actual = kernel.GetOverlappedResult

        def zero_bytes(handle, overlapped, size, wait):
            result = actual(handle, overlapped, size, wait)
            C.cast(size, C.POINTER(native.W.DWORD)).contents.value = 0
            return result

        with patch.object(kernel, 'GetOverlappedResult', zero_bytes):
            self.assertTrue(self.changes.poll()['overflow'])
        self.assertTrue(self.changes.active)


if __name__ == '__main__':
    unittest.main()
