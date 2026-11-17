import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from portable_capture_store import Dataset, DatasetBusy, resolve_under, read_json


class PortableStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'photos'
        self.root.mkdir()
        self.a = Dataset(self.root, self.base / 'A')
        self.b = Dataset(self.root, self.base / 'B')

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.temp.cleanup()

    def record(self, path='folder/image.jpg'):
        return {'id': uuid.uuid4().hex, 'asset_id': uuid.uuid4().hex, 'relative_path': path,
                'sku': '00123-A', 'rating': 2, 'decision_id': uuid.uuid4().hex}

    def test_single_writer_many_readers_and_handoff(self):
        self.a.acquire()
        value = self.record()
        self.a.change([value])
        with self.assertRaises(DatasetBusy):
            self.b.acquire()
        self.assertTrue(self.b.refresh())
        self.assertEqual(self.b.records(), [value])
        self.assertFalse(self.b.editing)
        self.a.release()
        self.b.acquire()
        value = {**value, 'sku': 'LC0002'}
        self.b.change([value])
        self.a.refresh()
        self.assertEqual(self.a.records()[0]['sku'], 'LC0002')

    def test_cannot_modify_without_edit_ownership(self):
        with self.assertRaises(DatasetBusy):
            self.a.change([self.record()])
        self.assertFalse((self.root / '.shiying').exists())

    def test_same_client_can_reacquire_after_normal_handoff(self):
        self.a.acquire()
        first = self.a.ownership.session
        self.a.change([self.record()])
        self.a.release()
        self.a.acquire()
        self.assertNotEqual(first, self.a.ownership.session)
        self.a.change([self.record('second.jpg')])
        self.b.refresh()
        self.assertEqual(len(self.b.records()), 2)

    def test_snapshot_loads_in_fresh_client_without_shared_sqlite(self):
        self.a.acquire()
        record = self.record()
        self.a.change([record])
        self.a.release()
        self.assertEqual(len(list((self.root / '.shiying/snapshots').glob('*.sqlite3'))), 1)
        self.assertFalse(list((self.root / '.shiying').rglob('*-wal')))
        self.b.refresh()
        self.assertEqual(self.b.records(), [record])
        with self.b.connect() as c:
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_unpublished_change_survives_and_retries_idempotently(self):
        self.a.acquire()
        from portable_capture_store import publish_file
        def fail_commit(path, content):
            if Path(path).parent.name == 'commits':
                raise OSError('模拟NAS断连')
            return publish_file(path, content)
        record = self.record()
        with patch('portable_capture_store.publish_file', side_effect=fail_commit):
            self.a.change([record], tasks=[{'kind': 'metadata', 'record': record['id'], 'revision': record['decision_id']}])
        self.assertIn('断连', self.a.last_error)
        self.b.refresh()
        self.assertEqual(self.b.records(), [])
        self.a.publish()
        self.a.publish()
        self.b.refresh()
        self.assertEqual(self.b.records(), [record])
        self.assertEqual(len(list((self.root / '.shiying/commits').glob('*.json'))), 1)
        with self.a.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)

    def test_late_commit_dependency_waits_without_partial_state(self):
        self.a.acquire()
        self.a.change([self.record()])
        commits = self.root / '.shiying/commits'
        marker = next(commits.glob('*.json'))
        content = marker.read_bytes()
        marker.unlink()
        self.a.change([self.record('other.jpg')])
        self.b.refresh()
        self.assertEqual(self.b.records(), [])
        marker.write_bytes(content)
        self.b.refresh()
        self.assertEqual(len(self.b.records()), 2)

    def test_invalid_and_absolute_references_are_rejected(self):
        self.a.acquire()
        for path in ('../escape.jpg', '/absolute.jpg', 'Z:/photo.jpg', '//NAS/share/a.jpg', 'a/../b.jpg', 'a.jpg:stream'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.a.change([self.record(path)])
        self.assertEqual(self.a.records(), [])

    def test_changed_dataset_identity_is_not_overwritten(self):
        self.a.acquire()
        descriptor = self.root / '.shiying/dataset.json'
        value = read_json(descriptor)
        value['dataset_id'] = uuid.uuid4().hex
        descriptor.write_text(json.dumps(value), encoding='utf8')
        with self.assertRaisesRegex(ValueError, '身份发生改变'):
            self.a.refresh()

    def test_corrupt_snapshot_falls_back_to_verified_events(self):
        self.a.acquire()
        record = self.record()
        self.a.change([record])
        self.a.release()
        snapshot = next((self.root / '.shiying/snapshots').glob('*.sqlite3'))
        snapshot.write_bytes(b'bad snapshot')
        self.b.refresh()
        self.assertEqual(self.b.records(), [record])
        self.assertIn('快照回退', self.b.last_error)

    def test_only_relative_paths_are_published(self):
        self.a.acquire()
        self.a.change([self.record()])
        for path in (self.root / '.shiying/events').glob('*.json'):
            content = path.read_text(encoding='utf8')
            self.assertNotIn(str(self.root), content)
            self.assertNotIn(str(self.base / 'A'), content)

    def test_offline_pending_branch_cannot_overwrite_new_remote_commit(self):
        self.a.acquire()
        from portable_capture_store import publish_file
        def offline(path, content):
            raise OSError('offline')
        with patch('portable_capture_store.publish_file', side_effect=offline):
            self.a.change([self.record()])
            self.a.change([self.record('second.jpg')])
        self.a.ownership.release()
        self.b.acquire()
        self.b.change([self.record('remote.jpg')])
        self.b.release()
        with self.assertRaisesRegex(ValueError, '分叉'):
            self.a.acquire()
        self.assertFalse(self.a.editing)


if __name__ == '__main__':
    unittest.main()
