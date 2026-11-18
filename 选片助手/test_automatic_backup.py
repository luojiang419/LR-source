"""整拍摄目录备份、故障接续及独立归档客户端读取验收。"""
from contextlib import contextmanager, ExitStack, nullcontext
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from PIL import Image
from automatic_backup import AutomaticBackup, digest, folder_path
from portable_capture_store import Dataset

ROOT = Path(__file__).resolve().parents[1]


class FolderBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.photos = self.base / '现场拍摄'
        self.photos.mkdir()
        self.target = self.base / 'NAS映射盘'
        self.target.mkdir()
        self.data = self.base / 'local-data'
        self.data.mkdir()
        self.photo = self.photos / '棚拍/001.JPG'
        self.photo.parent.mkdir()
        Image.new('RGB', (80, 60), 'steelblue').save(self.photo)
        self.dataset = Dataset(self.photos, self.data)
        self.dataset.acquire()
        self.record = {'id': uuid.uuid4().hex, 'path': '棚拍/001.JPG', 'relative_path': '棚拍/001.JPG',
                       'sku': '款号-001', 'write_state': 'verified', 'file_sha256': digest(self.photo),
                       'signature': [self.photo.stat().st_size, self.photo.stat().st_mtime_ns]}
        self.dataset.change([self.record])
        self.writer = SimpleNamespace(file_lock=lambda *a, **k: nullcontext(), capture_service=SimpleNamespace(dataset_for_path=lambda p: self.dataset))
        self.backup = AutomaticBackup(self.data, self.writer, roots=lambda: [self.photos], autostart=False)
        self.backup.configure({'enabled': True, 'folder': str(self.target), 'interval_seconds': 1})
        # 此组只测复制/接续；真实拍摄补写由 ArchiveReaderBackupTests 覆盖。
        self.prepare = patch.object(AutomaticBackup, '_prepare_metadata', return_value=None)
        self.prepare.start()
        self.addCleanup(self.prepare.stop)

    def tearDown(self):
        self.backup.close()
        self.dataset.close()
        self.temp.cleanup()

    @property
    def output(self):
        return self.target / self.photos.name

    def sync(self):
        self.backup.retry()
        self.assertTrue(self.backup.process_one(force=True), self.backup.state())

    def test_preserves_relative_tree_sidecars_portable_events_and_snapshot(self):
        (self.photo.with_suffix('.xmp')).write_text('<xmp>测试侧车</xmp>', 'utf8')
        (self.photos/'拍摄说明.txt').write_text('测试目录资料', 'utf8')
        self.dataset.snapshot()
        self.sync()
        source = {p.relative_to(self.photos).as_posix(): digest(p) for p in self.photos.rglob('*') if p.is_file() and p.name != 'write.lock' and not p.name.startswith('owner-')}
        for relative, checksum in source.items():
            self.assertEqual(digest(self.output/relative), checksum, relative)
        reader = Dataset(self.output, self.base/'independent-reader')
        try:
            reader.refresh()
            self.assertEqual(reader.records()[0]['sku'], '款号-001')
            self.assertEqual(reader.dataset_id, self.dataset.dataset_id)
            self.assertFalse(reader.editing)
        finally:
            reader.close()
        self.assertEqual(self.backup.state()['completed'], 1)
        self.assertEqual(self.backup.state()['last_output'], str(self.output))

    def test_pending_file_metadata_blocks_backup_until_verified(self):
        self.dataset.change([{**self.record, 'decision_id': 'rev', 'write_state': 'pending'}], tasks=[{'kind': 'metadata', 'record': self.record['id'], 'revision': 'rev'}])
        self.assertFalse(self.backup.process_one(force=True))
        self.assertFalse(self.output.exists())
        self.assertIn('元数据', self.backup.state()['error'])
        self.assertEqual(self.backup.state()['waiting'], 1)
        self.assertIsNone(self.backup.state()['progress']['total_files'])
        self.assertEqual(self.backup.state()['progress']['phase'], 'retrying')
        self.assertGreater(self.backup.state()['retry_at'], time.time())
        self.dataset.change([self.record])
        with self.dataset.connect() as c:
            c.execute("UPDATE tasks SET status='complete'")
        self.sync()

    def test_old_failed_task_does_not_block_current_verified_record(self):
        self.dataset.change([{**self.record, 'decision_id': 'current'}])
        with self.dataset.connect() as c:
            c.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', ('old-failure', 'metadata', self.record['id'], 'obsolete', 'failed', '旧版失败', '{}'))
        self.sync()
        self.assertEqual(digest(self.output/'棚拍/001.JPG'), digest(self.photo))

    def test_sku_only_receipt_is_ready_without_using_old_full_file_hash(self):
        self.dataset.change([{**self.record, 'write_state': 'sku_verified', 'file_sha256': 'old-full-file-hash'}])
        self.sync()
        self.assertEqual(self.backup.state()['waiting'], 0)
        self.assertEqual(digest(self.output/'棚拍/001.JPG'), digest(self.photo))

    def test_unverified_new_photo_waits_then_is_added_without_losing_subfolder(self):
        new = self.photos/'棚拍/002.JPG'
        Image.new('RGB', (80, 60), 'red').save(new)
        record = {**self.record, 'id': uuid.uuid4().hex, 'path': '棚拍/002.JPG', 'relative_path': '棚拍/002.JPG', 'sku': '', 'write_state': 'unknown'}
        self.dataset.change([record])
        self.sync()
        self.assertTrue((self.output/'棚拍/001.JPG').is_file())
        self.assertFalse((self.output/'棚拍/002.JPG').exists())
        self.assertEqual(self.backup.state()['waiting'], 1)
        self.dataset.change([{**record, 'sku': '款号-002', 'write_state': 'verified', 'file_sha256': digest(new)}])
        self.sync()
        self.assertEqual(digest(new), digest(self.output/'棚拍/002.JPG'))
        self.assertEqual(self.backup.state()['waiting'], 0)

    def test_offline_destination_and_restart_resume_durable_queue(self):
        self.target.rmdir()
        self.assertFalse(self.backup.process_one(force=True))
        self.assertIn('离线', self.backup.state()['error'])
        self.assertFalse(self.target.exists())
        self.backup.close()
        self.target.mkdir()
        self.backup = AutomaticBackup(self.data, self.writer, roots=lambda: [self.photos], autostart=False)
        self.sync()

    def test_interrupted_copy_never_publishes_partial_file_or_metadata_commit(self):
        original = self.backup._copy
        def interrupt(source, target, previous, destination):
            if source == self.photo:
                raise OSError('模拟网络中断')
            return original(source, target, previous, destination)
        with patch.object(self.backup, '_copy', side_effect=interrupt):
            self.assertFalse(self.backup.process_one(force=True))
        self.assertFalse((self.output/'棚拍/001.JPG').exists())
        self.assertFalse(list((self.output/'.shiying/commits').glob('*.json')))
        self.assertEqual(self.backup.state()['retrying'], 1)
        self.sync()

    def test_managed_update_protects_external_edits_and_source_deletions(self):
        self.sync()
        target = self.output/'棚拍/001.JPG'
        target.write_bytes('其他用户的内容'.encode('utf8'))
        self.dataset.change([{**self.record, 'sku': '新款号'}])
        self.backup.retry()
        self.assertFalse(self.backup.process_one(force=True))
        self.assertEqual(target.read_bytes(), '其他用户的内容'.encode('utf8'))
        self.assertIn('其它用户', self.backup.state()['error'])

    def test_existing_unknown_folder_and_same_dataset_other_writer_are_preserved(self):
        self.output.mkdir()
        unknown = self.output/'用户文件.txt'
        unknown.write_text('保留', 'utf8')
        self.assertFalse(self.backup.process_one(force=True))
        self.assertEqual(unknown.read_text('utf8'), '保留')
        unknown.unlink()
        self.sync()
        editor = Dataset(self.output, self.base/'target-editor')
        try:
            editor.acquire()
            self.backup.retry()
            self.assertFalse(self.backup.process_one(force=True))
            self.assertIn('其它客户端', self.backup.state()['error'])
        finally:
            editor.close()

    def test_source_change_during_copy_defers_new_metadata_commit(self):
        original = self.backup._copy
        def update(source, target, previous, destination):
            result = original(source, target, previous, destination)
            if source == self.photo:
                self.dataset.change([{**self.record, 'sku': '新款号'}])
            return result
        with patch.object(self.backup, '_copy', side_effect=update):
            self.assertFalse(self.backup.process_one(force=True))
        self.assertIn('更新', self.backup.state()['error'])
        self.assertFalse(list((self.output/'.shiying/commits').glob('*.json')))
        self.sync()

    def test_settings_validation_pause_target_change_and_interval(self):
        for seconds in (0, -1, 86401, 1.5, True, '5'):
            with self.assertRaises(ValueError):
                self.backup.configure({'interval_seconds': seconds})
        for path in (self.photos, self.photo.parent, self.base):
            with self.assertRaises(ValueError):
                self.backup.configure({'folder': str(path)})
        self.backup.configure({'interval_seconds': 60})
        self.assertFalse(self.backup.process_one())
        self.backup.configure({'enabled': False})
        self.assertFalse(self.backup.process_one(force=True))
        self.backup.configure({'enabled': True, 'interval_seconds': 1})
        self.sync()
        other = self.base/'其它映射盘'
        other.mkdir()
        self.backup.configure({'folder': str(other)})
        self.sync()
        self.assertTrue((other/self.photos.name/'棚拍/001.JPG').is_file())
        self.assertTrue((self.output/'棚拍/001.JPG').is_file())

    def test_filesystem_protocol_paths(self):
        self.assertEqual(folder_path(self.target.as_uri()), str(self.target))
        self.assertEqual(folder_path('smb://NAS/share/拍摄'), os.path.normpath('//NAS/share/拍摄'))
        for value in ('relative/path', 'sftp://nas/a', 'ftp://nas/a', 'https://nas/dav', 'smb://user:password@nas/share', 'D:\\a\nb'):
            with self.assertRaises(ValueError, msg=value):
                folder_path(value)

    @unittest.skipUnless(os.name == 'nt', 'Windows 映射驱动器验证')
    def test_mapped_drive_folder_and_source_alias_guard(self):
        import ctypes
        import subprocess
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        letter = next((c + ':' for c in 'ZYXWVUTSRQP' if not mask & (1 << (ord(c) - ord('A')))), None)
        if not letter:
            self.skipTest('没有空闲驱动器盘符')
        subprocess.run(['subst', letter, str(self.target)], check=True, capture_output=True)
        try:
            self.backup.configure({'folder': letter + '\\'})
            self.sync()
            self.assertEqual(digest(Path(letter + '\\')/self.photos.name/'棚拍/001.JPG'), digest(self.photo))
        finally:
            subprocess.run(['subst', letter, '/D'], check=True, capture_output=True)
        subprocess.run(['subst', letter, str(self.base)], check=True, capture_output=True)
        try:
            self.backup.configure({'folder': letter + '\\'})
            self.backup.retry()
            self.assertFalse(self.backup.process_one(force=True))
            self.assertIn('现场拍摄目录内', self.backup.state()['error'])
        finally:
            subprocess.run(['subst', letter, '/D'], check=True, capture_output=True)

    def test_real_windows_target_lock_defers_atomic_replace_and_preserves_previous_copy(self):
        from test_manual_metadata_backfill import windows_handle
        self.sync()
        target = self.output/'棚拍/001.JPG'
        previous = target.read_bytes()
        Image.new('RGB', (80, 60), 'yellow').save(self.photo)
        self.dataset.change([{**self.record, 'file_sha256': digest(self.photo)}])
        self.backup.retry()
        with windows_handle(target, access=0x80000000, share=1):
            self.assertFalse(self.backup.process_one(force=True))
            self.assertEqual(target.read_bytes(), previous)
        self.sync()
        self.assertEqual(digest(target), digest(self.photo))

    def test_successful_metadata_notification_invalidates_cached_unchanged_mtime(self):
        from metadata_history import save_record
        self.sync()
        before = self.photo.stat()
        Image.new('RGB', (80, 60), 'orange').save(self.photo)
        os.utime(self.photo, ns=(before.st_atime_ns, before.st_mtime_ns))
        updated = {**self.record, 'file_sha256': digest(self.photo)}
        self.dataset.change([updated])
        journal = {'target': str(self.photo), 'after_hash': digest(self.photo)}
        save_record(self.data, uuid.uuid4().hex, journal, source=self.photo, status='applied')
        # 不调用 retry，不清空已验证缓存；成功回执必须推动更新。
        self.backup.next_sync = 0
        self.assertTrue(self.backup.process_one(), self.backup.state())
        self.assertEqual(digest(self.output/'棚拍/001.JPG'), digest(self.photo))

    def test_unchanged_poll_reuses_verified_versions_without_reading_all_images(self):
        self.sync()
        self.backup.touch(self.photos, changed=False)
        self.backup.next_sync = 0
        with patch.object(self.backup, '_hash', side_effect=AssertionError('未变化轮询不应重复全量读取文件')):
            self.assertTrue(self.backup.process_one(), self.backup.state())
        progress = self.backup.state()['progress']
        self.assertEqual(progress['done_files'], progress['total_files'])
        self.assertEqual(progress['done_bytes'], progress['total_bytes'])

    def test_live_progress_is_readable_during_copy_and_verification_without_disk_access(self):
        # 真实多块文件；在复制及回读阶段暂停后台，状态读取必须即时返回且不能探测目标盘。
        payload = self.photos/'大文件.bin'
        payload.write_bytes(b'progress' * (512 * 1024))
        reached, resume = threading.Event(), threading.Event()
        original = self.backup._progress
        observed = []
        def progress(**values):
            original(**values)
            if values.get('phase') in ('copying', 'verifying_copy') and self.backup.progress.get('current_file') == payload.name:
                reached.set()
                if not resume.wait(5):
                    raise TimeoutError('进度测试未释放后台')
                resume.clear()
        worker = threading.Thread(target=self.backup.process_one, kwargs={'force': True})
        with patch.object(self.backup, '_progress', side_effect=progress):
            worker.start()
            try:
                for phase in ('copying', 'verifying_copy'):
                    self.assertTrue(reached.wait(5))
                    reached.clear()
                    with patch.object(Path, 'stat', side_effect=AssertionError('状态接口不应读取文件系统')):
                        state = self.backup.state()
                    p = state['progress']
                    self.assertEqual(p['phase'], phase)
                    self.assertEqual(p['current_file'], payload.name)
                    self.assertEqual(p['stage_total'], payload.stat().st_size)
                    self.assertGreater(p['total_files'], p['done_files'])
                    self.assertGreaterEqual(p['elapsed_seconds'], 0)
                    observed.append(p)
                    resume.set()
            finally:
                resume.set()
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(observed), 2)
        state = self.backup.state()
        self.assertEqual(state['progress']['phase'], 'complete', state)
        self.assertEqual(state['progress']['done_files'], state['progress']['total_files'])
        self.assertEqual(state['progress']['done_bytes'], state['progress']['total_bytes'])
        self.assertEqual(digest(payload), digest(self.output/payload.name))
        self.backup.configure({'folder': str(self.target), 'enabled': False})
        self.assertEqual(self.backup.state()['progress'], {})

    def test_restart_cleans_only_registered_own_partial_files(self):
        self.sync()
        folder = self.output/'棚拍'
        own = folder/'.001.JPG.owned.writing'
        unknown = folder/'.001.JPG.unknown.writing'
        own.write_bytes(b'partial')
        unknown.write_bytes(b'user temporary file')
        stat = own.stat()
        from automatic_backup import key
        with self.backup.connect() as c:
            c.execute('INSERT INTO transfer_staging VALUES(?,?,?,?)', (str(own), key(self.output), str(stat.st_ino), str(stat.st_ctime_ns)))
        self.backup.close()
        self.backup = AutomaticBackup(self.data, self.writer, roots=lambda: [self.photos], autostart=False)
        self.sync()
        self.assertFalse(own.exists())
        self.assertEqual(unknown.read_bytes(), b'user temporary file')


class ArchiveReaderBackupTests(unittest.TestCase):
    from test_archive_capture_service import ArchiveCaptureTests as Fixture
    client = Fixture.client
    item = Fixture.item
    drain = Fixture.drain

    def setUp(self):
        self.Fixture.setUp(self)
        self.target = self.base/'共享备份'
        self.target.mkdir()
        self.backup = AutomaticBackup(self.store.data, self.archive.writer, roots=lambda: [self.photos], autostart=False)
        self.backup.configure({'enabled': True, 'folder': str(self.target), 'interval_seconds': 1})

    def tearDown(self):
        self.backup.close()
        self.Fixture.tearDown(self)

    def ready(self):
        self.api.rate(self.item(2), 2)
        self.api.rate(self.item(4), 2)
        self.drain()

    def test_unloaded_dataset_on_another_installation_backs_up_completed_metadata(self):
        self.ready()
        self.api.edit(self.rid, enabled=False)
        store, archive, catalog, sku, other, rid = self.client(self.base/'另一安装', self.photos)
        self.assertIsNone(other.dataset_for_path(self.photos/'__backup_probe__'))
        backup = AutomaticBackup(store.data, archive.writer, roots=lambda: [self.photos], autostart=False)
        try:
            backup.configure({'enabled': True, 'folder': str(self.target), 'interval_seconds': 1})
            with patch.object(sku, 'recognizer', side_effect=AssertionError('恢复备份不得触发识别')):
                self.assertTrue(backup.process_one(force=True), backup.state())
            self.assertEqual(backup.state()['waiting'], 0)
            self.assertFalse(other.dataset_for_path(self.photos/'__backup_probe__').editing)
            for photo in self.photos.glob('*.JPG'):
                self.assertEqual(digest(photo), digest(self.target/self.photos.name/photo.name))
        finally:
            backup.close()

    def test_manual_sync_of_completed_source_does_not_take_another_writers_lock(self):
        self.ready()
        store, archive, catalog, sku, other, rid = self.client(self.base/'另一只读安装', self.photos)
        backup = AutomaticBackup(store.data, archive.writer, roots=lambda: [self.photos], autostart=False)
        try:
            backup.configure({'enabled': True, 'folder': str(self.target), 'interval_seconds': 1})
            backup.retry()
            self.assertTrue(backup.process_one(force=True), backup.state())
            self.assertTrue(self.dataset.editing)
            self.assertFalse(other.dataset_for_path(self.photos/'__backup_probe__').editing)
        finally:
            backup.close()

    def test_click_once_restores_missing_and_failed_writes_then_syncs_automatically(self):
        self.ready()
        for n in (1, 2):
            self.api.confirm(self.item(n), f'补写-{n}', whole_group=False)
        with self.dataset.connect() as c:
            c.execute("DELETE FROM tasks WHERE record=? AND kind='metadata'", (self.dataset.record(path='IMG_0001.JPG')['id'],))
            c.execute("UPDATE tasks SET status='failed',reason='旧版写入中断' WHERE record=? AND status='queued'", (self.dataset.record(path='IMG_0002.JPG')['id'],))
        self.backup.retry()
        with patch.object(self.sku, 'recognizer', side_effect=AssertionError('同步补写不得OCR')):
            self.assertFalse(self.backup.process_one(force=True))
            with self.dataset.connect() as c:
                self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE status='queued' AND kind='metadata'").fetchone()[0], 2)
            self.backup.thread.start()
            self.wait_backup(lambda: self.backup.state()['completed'] == 1)
        for n in (1, 2):
            target = self.target/self.photos.name/f'IMG_{n:04d}.JPG'
            self.assertEqual(self.archive.writer.read_asset(target)['XMP-fk:SKU'], f'补写-{n}')
            self.assertEqual(digest(target), digest(self.photos/target.name))

    def test_missing_metadata_request_survives_backup_restart(self):
        self.ready()
        self.api.confirm(self.item(1), '重启后补写', whole_group=False)
        with self.dataset.connect() as c:
            c.execute("DELETE FROM tasks WHERE status='queued' AND kind='metadata'")
        self.backup.retry()
        self.backup.close()
        self.backup = AutomaticBackup(self.store.data, self.archive.writer, roots=lambda: [self.photos], autostart=False)
        self.assertFalse(self.backup.process_one(force=True))
        self.drain()
        self.backup.thread.start()
        self.wait_backup(lambda: self.backup.state()['completed'] == 1)
        target = self.target/self.photos.name/'IMG_0001.JPG'
        self.assertEqual(self.archive.writer.read_asset(target)['XMP-fk:SKU'], '重启后补写')

    def test_failed_current_sidecar_receipt_is_retried_by_sync(self):
        self.ready()
        record = self.dataset.record(path='IMG_0001.JPG')
        with self.dataset.connect() as c:
            c.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', ('failed-receipt', 'metadata_sidecar', record['id'], record['decision_id'], 'failed', '旧版摘要回执中断', '{}'))
        self.backup.retry()
        self.assertFalse(self.backup.process_one(force=True))
        self.drain()
        self.backup.thread.start()
        self.wait_backup(lambda: self.backup.state()['completed'] == 1)
        with self.dataset.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM tasks WHERE id='failed-receipt'").fetchone()[0], 'complete')

    def test_receiving_mode_does_not_enqueue_or_write_missing_metadata(self):
        self.ready()
        self.api.confirm(self.item(1), '只读不写', whole_group=False)
        with self.dataset.connect() as c:
            c.execute("DELETE FROM tasks WHERE status='queued' AND kind='metadata'")
        before = {p: p.read_bytes() for p in self.photos.glob('*.JPG')}
        self.backup.retry()
        with patch.object(self.api, 'readonly', return_value=True):
            self.assertFalse(self.backup.process_one(force=True))
        self.assertIn('单向接收', self.backup.state()['error'])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        with self.dataset.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM tasks WHERE status='queued' AND kind='metadata'").fetchone())

    def test_files_with_embedded_metadata_rebuild_missing_portable_directory_on_click(self):
        import shutil
        self.ready()
        photos = self.base/'仅有已写照片'
        photos.mkdir()
        for photo in self.photos.glob('*.JPG'):
            shutil.copy2(photo, photos/photo.name)
        store, archive, catalog, sku, other, rid = self.client(self.base/'无随行目录安装', photos)
        # 生产环境带按访问加载；用户停在设置页时也必须能恢复拍摄元数据。
        visits = SimpleNamespace(lock=threading.RLock(), key=None, path=None, allows=lambda _: False)
        backup = AutomaticBackup(store.data, archive.writer, roots=lambda: [photos], autostart=False)
        try:
            backup.configure({'enabled': True, 'folder': str(self.target), 'interval_seconds': 1})
            backup.retry()
            with patch.object(sku, 'recognizer', side_effect=AssertionError('文件恢复不得OCR')), patch.object(catalog, 'visits', visits, create=True):
                self.assertTrue(backup.process_one(force=True), backup.state())
                self.assertIsNone(visits.key)
                self.assertIsNone(visits.path)
            self.assertTrue((photos/'.shiying/dataset.json').is_file())
            self.assertEqual(backup.state()['waiting'], 0)
            for photo in photos.glob('*.JPG'):
                self.assertEqual(digest(photo), digest(self.target/photos.name/photo.name))
            reader = Dataset(self.target/photos.name, self.base/'重建后独立读取')
            try:
                reader.refresh()
                self.assertEqual(len(reader.records()), 4)
                self.assertEqual(len(reader.groups()), 2)
            finally:
                reader.close()
        finally:
            backup.close()

    def sync(self):
        self.backup.retry()
        self.assertTrue(self.backup.process_one(force=True), self.backup.state())
        return self.target/self.photos.name

    def test_real_jpg_and_whole_metadata_folder_restore_two_intervals_in_new_archive(self):
        self.ready()
        self.dataset.snapshot()
        with patch.object(self.sku, 'recognizer', side_effect=AssertionError('备份不能触发识别')):
            output = self.sync()
        original = {r['relative_path']: r for r in self.dataset.records()}
        store, archive, catalog, sku, other, rid = self.client(self.base/'另一用户', output)
        sku.recognizer = lambda *_: self.fail('归档读取备份不能重新识别')
        ds, _ = other.scope(rid)
        other._scan(ds)
        actual = {r['relative_path']: r for r in ds.records()}
        self.assertEqual(ds.dataset_id, self.dataset.dataset_id)
        for relative, before in original.items():
            self.assertEqual(actual[relative]['sku'], before['sku'])
            self.assertEqual(actual[relative]['group_id'], before['group_id'])
            self.assertEqual(actual[relative]['rating'], before['rating'])
            self.assertEqual(digest(output/relative), digest(self.photos/relative))
            self.assertEqual(archive.writer.read_asset(output/relative)['XMP-fk:SKU'], before['sku'])
        self.assertEqual(len(ds.groups()), 2)
        self.assertEqual(sorted(g['sku'] for g in ds.groups()), sorted(g['sku'] for g in self.dataset.groups()))
        self.assertFalse(ds.editing)
        self.assertEqual(catalog.items()['total'], 4)
        from playwright.sync_api import sync_playwright
        with self.http(dict(STORE=store, CATALOG=catalog, ARCHIVE=archive, SKU=sku, MODELS=sku.providers, DATA=store.data)) as url, sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page(viewport={'width': 1600, 'height': 1000}, reduced_motion='reduce')
                page.goto(url, wait_until='domcontentloaded')
                page.wait_for_function('()=>!!window.FramekeepArchiveOpenItem&&!window.FramekeepBoot?.loading')
                page.evaluate('(id)=>FramekeepArchiveOpenItem(id)', self.item(2, catalog))
                page.wait_for_function('()=>document.querySelectorAll("#catalog-grid .catalog-card-sku").length===4')
                texts = page.locator('#catalog-grid .catalog-card-sku').all_text_contents()
                for code in set(r['sku'] for r in original.values()):
                    self.assertEqual(texts.count('SKU：' + code), 2)
                page.wait_for_function('()=>document.querySelectorAll("#capture-groups .capture-group").length===2')
                artifact = ROOT/'build/task230-automatic-backup'
                artifact.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(artifact/'independent-archive.png'))
            finally:
                browser.close()

    def test_actual_metadata_queue_waits_and_later_rename_reaches_independent_reader(self):
        self.ready()
        output = self.sync()
        self.api.confirm(self.item(2), '复拍 修正款号🙂', auto_edit=True)
        self.backup.retry()
        self.assertFalse(self.backup.process_one(force=True))
        self.drain()
        self.sync()
        reader = Dataset(output, self.base/'只读用户')
        try:
            reader.refresh()
            source = {r['relative_path']: r['sku'] for r in self.dataset.records()}
            self.assertEqual({r['relative_path']: r['sku'] for r in reader.records()}, source)
            self.assertIn('复拍 修正款号🙂', source.values())
        finally:
            reader.close()

    def wait_backup(self, check):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.api.tick()
            if check():
                return
            time.sleep(.05)
        self.fail('自动备份未完成：' + str(self.backup.state()))

    def test_manual_sku_and_rating_automatically_update_without_clicking_sync(self):
        self.ready()
        output = self.sync()
        self.backup.thread.start()
        code = '手填 后自动备份🙂'
        self.api.confirm(self.item(2), code, auto_edit=True)
        self.drain()
        target = output/'IMG_0001.JPG'
        self.wait_backup(lambda: self.archive.writer.read_asset(target).get('XMP-fk:SKU') == code)
        self.api.rate(self.item(1), 4)
        self.drain()
        self.wait_backup(lambda: self.archive.writer.read_asset(target).get('XMP-xmp:Rating') == 4)
        self.wait_backup(lambda: self.backup.state()['completed'] == 1)
        reader = Dataset(output, self.base/'自动更新只读用户')
        try:
            reader.refresh()
            value = reader.record(path='IMG_0001.JPG')
            self.assertEqual((value['sku'], value['rating']), (code, 4))
        finally:
            reader.close()

    def test_manual_description_automatically_updates_after_real_file_commit(self):
        from photo_metadata import read, save
        self.ready()
        output = self.sync()
        self.backup.thread.start()
        item = self.item(1)
        before = read(self.archive, self.catalog, item)
        save(self.archive, self.catalog, item, {'title': '手工标题🙂', 'description': '人工修改后自动同步', 'keywords': ['新品', '质检'], 'label': 'Blue'}, before['token'])
        for _ in range(10):
            if not self.archive.process_one():
                break
        target = output/'IMG_0001.JPG'
        self.assertEqual(self.archive.writer.read_asset(self.photos/target.name)['XMP-dc:Title'], '手工标题🙂')
        self.wait_backup(lambda: self.archive.writer.read_asset(target).get('XMP-dc:Title') == '手工标题🙂')
        self.wait_backup(lambda: self.backup.state()['completed'] == 1)
        self.assertEqual(self.archive.writer.read_asset(target)['XMP-dc:Description'], '人工修改后自动同步')
        # 来回改回同一份内容也需刷新回执，不能被已完成的历史校验任务挡住。
        for title in ('第二次标题', '手工标题🙂'):
            before = read(self.archive, self.catalog, item)
            save(self.archive, self.catalog, item, {'title': title, 'description': '人工修改后自动同步', 'keywords': ['新品', '质检'], 'label': 'Blue'}, before['token'])
            for _ in range(10):
                if not self.archive.process_one():
                    break
            self.wait_backup(lambda: self.archive.writer.read_asset(target).get('XMP-dc:Title') == title)
            self.wait_backup(lambda: self.backup.state()['completed'] == 1)
        reader = Dataset(output, self.base/'描述更新只读用户')
        try:
            reader.refresh()
            self.assertEqual(reader.record(path=target.name)['file_sha256'], digest(target))
        finally:
            reader.close()

    @contextmanager
    def http(self, overrides=None):
        import server
        values = dict(STORE=self.store, CATALOG=self.catalog, ARCHIVE=self.archive, SKU=self.sku,
                      MODELS=self.sku.providers, DATA=self.store.data, BACKUP=self.backup, BOOT_GATE=None,
                      WATCH=None, MOUSE=None, ANALYSIS=None, RAWLINK=None, FILESEARCH=None, UPDATES=None,
                      COMPONENTS=None, DRIVER=None, CANON=None, PREVIEWS=None, REFERENCES=None)
        values.update(overrides or {})
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.daemon_threads = True
        with ExitStack() as stack:
            for name, value in values.items():
                stack.enter_context(patch.object(server, name, value))
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                yield f'http://127.0.0.1:{http.server_port}'
            finally:
                http.shutdown()
                http.server_close()

    def test_browser_settings_save_custom_seconds_folder_picker_and_narrow_layout(self):
        from playwright.sync_api import sync_playwright
        self.ready()
        out = ROOT/'build/task230-automatic-backup'
        out.mkdir(parents=True, exist_ok=True)
        self.backup.configure({'enabled': False})
        with self.http() as url, sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page(viewport={'width': 1600, 'height': 1000}, reduced_motion='reduce')
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(url, wait_until='domcontentloaded')
                page.wait_for_function('()=>!!window.FramekeepArchiveOpenItem&&!window.FramekeepBoot?.loading')
                page.locator('#app-settings-open').click()
                page.locator('#settings-tab-backup').click()
                page.wait_for_function('()=>!document.getElementById("backup-save").disabled')
                page.evaluate('(folder)=>window.FramekeepDesktop={chooseFolders:async()=>[folder]}', str(self.target))
                page.locator('#backup-browse').click()
                page.wait_for_function('(folder)=>document.getElementById("backup-folder").value===folder', arg=str(self.target))
                page.locator('#backup-preset').select_option('30')
                self.assertEqual(page.locator('#backup-interval').input_value(), '30')
                page.locator('#backup-interval').fill('7')
                self.assertEqual(page.locator('#backup-preset').input_value(), 'custom')
                page.locator('#backup-enabled').check()
                with page.expect_response(lambda r: r.url.endswith('/api/automatic-backup/configure')) as saved:
                    page.locator('#backup-save').click()
                self.assertEqual(saved.value.json()['config']['interval_seconds'], 7)
                self.assertEqual(self.backup.config['interval_seconds'], 7)
                self.assertTrue(self.backup.config['enabled'])
                page.locator('#backup-retry').click()
                self.assertTrue(self.backup.process_one(force=True), self.backup.state())
                page.wait_for_function('()=>document.getElementById("backup-progress").textContent.includes("已同步 1")', timeout=8000)
                self.assertEqual(page.locator('#backup-output,#backup-path-help').count(), 0)
                self.assertFalse(page.locator('#backup-message').is_visible())
                page.wait_for_function('()=>!document.getElementById("toast").classList.contains("visible")')
                page.screenshot(path=str(out/'settings-desktop.png'))
                for theme in ('dark', 'light', 'pure-white', 'hardcore'):
                    page.evaluate('(t)=>FramekeepTheme.set(t)', theme)
                    for width in (375, 320):
                        page.set_viewport_size({'width': width, 'height': 950})
                        self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'), (theme, width))
                page.screenshot(path=str(out/'settings-narrow.png'))
                self.assertEqual(page.locator('.workspace-bottom-bar .workspace-tab-label').first.bounding_box()['height'], 1)
                page.reload(wait_until='domcontentloaded')
                page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                page.locator('#app-settings-open').click()
                page.locator('#settings-tab-backup').click()
                page.wait_for_function('()=>!document.getElementById("backup-save").disabled')
                self.assertEqual(page.locator('#backup-interval').input_value(), '7')
                self.assertEqual(page.locator('#backup-folder').input_value(), str(self.target))
                self.assertTrue(page.locator('#backup-enabled').is_checked())
                self.assertFalse(errors, errors)
            finally:
                browser.close()

    def test_browser_live_copy_verify_retry_and_pause_progress(self):
        from playwright.sync_api import sync_playwright
        self.ready()
        payload = self.photos/('长文件名_现场拍摄进度_' * 6 + '.bin')
        payload.write_bytes(b'progress' * (512 * 1024))
        out = ROOT/'build/task230-backup-progress'
        out.mkdir(parents=True, exist_ok=True)
        reached, resume = threading.Event(), threading.Event()
        original = self.backup._progress
        def progress(**values):
            original(**values)
            if values.get('phase') in ('copying', 'verifying_copy') and self.backup.progress.get('current_file') == payload.name:
                reached.set()
                if not resume.wait(20):
                    raise TimeoutError('浏览器未释放备份阶段')
                resume.clear()
        with self.http() as url, sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            worker = threading.Thread(target=self.backup.process_one, kwargs={'force': True})
            try:
                page = browser.new_page(viewport={'width': 1400, 'height': 1000}, reduced_motion='reduce')
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(url, wait_until='domcontentloaded')
                page.wait_for_function('()=>!!window.FramekeepArchiveOpenItem&&!window.FramekeepBoot?.loading')
                page.locator('#app-settings-open').click()
                page.locator('#settings-tab-backup').click()
                page.wait_for_function('()=>!document.getElementById("backup-save").disabled')
                with patch.object(self.backup, '_progress', side_effect=progress):
                    worker.start()
                    for phase, text in [('copying', '复制文件'), ('verifying_copy', '回读校验备份')]:
                        self.assertTrue(reached.wait(5))
                        reached.clear()
                        page.wait_for_function('(text)=>document.getElementById("backup-stage").textContent===text', arg=text)
                        self.assertTrue(page.locator('#backup-meter').is_visible())
                        self.assertIn(payload.name, page.locator('#backup-current').inner_text())
                        self.assertIn('MB', page.locator('#backup-detail').inner_text())
                        page.wait_for_function('()=>!document.getElementById("toast").classList.contains("visible")')
                        page.locator('#automatic-backup-controls').screenshot(path=str(out/(phase+'.png')))
                        if phase == 'copying':
                            for theme in ('dark', 'light', 'pure-white', 'hardcore'):
                                page.evaluate('(t)=>FramekeepTheme.set(t)', theme)
                                for width in (375, 320):
                                    page.set_viewport_size({'width': width, 'height': 950})
                                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'), (theme, width))
                            page.evaluate('FramekeepTheme.set("dark")')
                            page.set_viewport_size({'width': 375, 'height': 950})
                            page.locator('#automatic-backup-controls').screenshot(path=str(out/'copying-narrow.png'))
                            page.set_viewport_size({'width': 1400, 'height': 1000})
                        resume.set()
                    worker.join(10)
                self.assertFalse(worker.is_alive())
                page.wait_for_function('()=>document.getElementById("backup-percent").textContent==="100%"')
                page.locator('#automatic-backup-controls').screenshot(path=str(out/'complete.png'))
                target = self.target/self.photos.name/payload.name
                target.write_bytes(b'other user edit')
                self.backup.retry()
                self.assertFalse(self.backup.process_one(force=True))
                page.wait_for_function('()=>document.getElementById("backup-stage").textContent==="等待自动重试"')
                self.assertFalse(page.locator('#backup-meter').is_visible())
                self.assertIn('未覆盖', page.locator('#backup-issues').inner_text())
                self.assertIn('自动重试', page.locator('#backup-detail').inner_text())
                page.locator('#automatic-backup-controls').screenshot(path=str(out/'retrying.png'))
                self.backup.configure({'enabled': False})
                page.wait_for_function('()=>document.getElementById("backup-stage").textContent==="已暂停"')
                self.assertFalse(page.locator('#backup-meter').is_visible())
                self.assertFalse(errors, errors)
            finally:
                resume.set()
                if worker.ident:
                    worker.join(10)
                browser.close()


if __name__ == '__main__':
    unittest.main()
