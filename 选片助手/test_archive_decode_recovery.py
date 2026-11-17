"""真实索引/缓存验证崩溃窗口、核对断点和变化确认竞态。"""
import json
import sqlite3
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from PIL import Image
from preview_cache import PreviewWarmer
from test_archive_decode_pause import ArchiveDecodePauseTests


class ArchiveDecodeRecoveryTests(unittest.TestCase):
    setUp=ArchiveDecodePauseTests.setUp
    tearDown=ArchiveDecodePauseTests.tearDown
    state=ArchiveDecodePauseTests.state

    def wait(self,condition):
        until=time.monotonic()+8
        while not condition():
            if time.monotonic()>until:self.fail('后台接续未在期限内完成')
            time.sleep(.02)

    def reopen(self):
        self.worker.close()
        self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.catalog.on_previews=self.worker.enqueue

    def test_index_commit_before_enqueue_survives_restart_and_only_changed_files_decode(self):
        while self.worker.process_one():pass
        self.assertEqual(self.state()['ready'],4)
        self.catalog.scan(self.root)
        self.assertFalse(self.catalog.pending_preview_changes())
        self.catalog.on_previews=None
        photos=self.base/'photos'
        Image.new('RGB',(72,48),'red').save(photos/'0.jpg')
        Image.new('RGB',(72,48),'green').save(photos/'added.jpg')
        self.catalog.scan(self.root)
        self.assertEqual(len(self.catalog.pending_preview_changes()),2)
        self.reopen()
        with patch.object(self.catalog,'queue_all_previews',side_effect=AssertionError('增量恢复不得全库核对')):
            self.worker.reconcile_archive()
            self.wait(lambda:not self.catalog.pending_preview_changes() and not self.worker.reconciling)
        self.assertEqual(self.state()['pending'],2)
        with patch.object(self.catalog,'render_preview_batch',wraps=self.catalog.render_preview_batch) as render:
            while self.worker.process_one():pass
        self.assertEqual(render.call_count,2)
        self.assertEqual((self.state()['total'],self.state()['ready']),(5,5))

    def test_full_scan_resumes_checkpoint_and_does_not_visit_acknowledged_range(self):
        self.catalog.on_previews=None
        for number in range(260):Image.new('RGB',(16,12),'blue').save(self.base/'photos'/f'seed-{number:03}.jpg')
        self.catalog.scan(self.root)
        self.catalog.on_previews=self.worker.enqueue
        original=self.catalog.queue_previews;interrupted=threading.Event()
        def interrupt(keys):
            result=original(keys)
            if not interrupted.is_set():interrupted.set();self.worker.closed.set()
            return result
        with patch.object(self.catalog,'queue_previews',side_effect=interrupt):
            self.worker.reconcile_archive(full=True)
            self.assertTrue(interrupted.wait(4));self.worker.reconcile_thread.join(4)
            self.assertFalse(self.worker.reconcile_thread.is_alive())
        with self.cache.read_connection() as c:
            saved=json.loads(c.execute("SELECT value FROM configuration WHERE key='archive-scan-all-v1'").fetchone()[0])
        self.assertIsNotNone(saved['after'])
        self.reopen();visited=[]
        def observe(keys):visited.extend(keys);return original(keys)
        with patch.object(self.catalog,'queue_previews',side_effect=observe):
            self.worker.reconcile_archive()
            def completed():
                with self.cache.read_connection() as c:return not c.execute("SELECT 1 FROM configuration WHERE key='archive-scan-all-v1'").fetchone() and not self.worker.reconciling
            self.wait(completed)
        self.assertTrue(visited);self.assertTrue(all(key>saved['after'] for key in visited))
        self.assertEqual(self.state()['total'],264)
        self.assertFalse(self.catalog.pending_preview_changes())

    def test_acknowledgement_cannot_erase_a_newer_change(self):
        keys=self.catalog.pending_preview_changes()
        if not keys:
            with self.catalog.connect() as c:c.execute("UPDATE items SET fingerprint=fingerprint||'-first' WHERE name='0.jpg'")
            keys=self.catalog.pending_preview_changes()
        original=self.worker.enqueue
        def race(root,records):
            result=original(root,records)
            with self.catalog.connect() as c:c.execute("UPDATE items SET fingerprint=fingerprint||'-newer' WHERE name='0.jpg'")
            return result
        with patch.object(self.catalog,'on_previews',side_effect=race):self.catalog.queue_previews(keys)
        remaining=self.catalog.pending_preview_changes();self.assertEqual(len(remaining),1)
        self.catalog.queue_previews(remaining)
        self.assertFalse(self.catalog.pending_preview_changes())
        with self.cache.read_connection() as c:
            record=json.loads(c.execute("SELECT record FROM archive_jobs WHERE record LIKE '%-newer%'").fetchone()[0])
        self.assertTrue(record['fingerprint'])

    def test_existing_pending_recovery_does_not_wait_for_cache_write_lock(self):
        held=threading.Event();release=threading.Event()
        def hold():
            with self.cache.lock:held.set();release.wait(5)
        with ThreadPoolExecutor(2) as pool:
            owner=pool.submit(hold)
            try:
                self.assertTrue(held.wait(1))
                self.assertFalse(pool.submit(self.worker._restore_archive_jobs).result(.5))
            finally:release.set();owner.result(1)
        self.assertEqual(self.state()['pending'],4)

    def test_explicit_fill_queues_only_missing_and_reuses_valid_cache(self):
        while self.worker.process_one():pass
        from preview_cache import cache_for
        index=cache_for(self.catalog.previews)
        with index.lock,index.connect() as c:
            name=c.execute('SELECT name FROM previews WHERE edge=2048 LIMIT 1').fetchone()[0];index._remove(c,name)
        files={p.name:p.stat().st_mtime_ns for p in index.folder.glob('*.jpg')}
        original=self.catalog.queue_all_previews
        def verify_after_queueing(*args,**kwargs):
            with self.cache.read_connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM pending').fetchone()[0],1)
            return original(*args,**kwargs)
        with patch.object(self.catalog,'queue_all_previews',side_effect=verify_after_queueing):
            self.worker.reconcile_archive(full=True)
            self.wait(lambda:not self.worker.fill_missing_requested and not self.worker.reconciling)
        self.assertGreaterEqual(self.worker.scan_progress['checked'],4);self.assertEqual(self.worker.scan_progress['queued'],1)
        self.assertEqual(self.state()['pending'],1)
        with patch.object(self.catalog,'render_preview_batch',wraps=self.catalog.render_preview_batch) as render:
            self.assertTrue(self.worker.process_one());self.assertFalse(self.worker.process_one())
        self.assertEqual(render.call_count,1)
        self.assertEqual(self.state()['ready'],4)
        self.assertEqual(files,{name:(index.folder/name).stat().st_mtime_ns for name in files})

    def test_repeated_full_speed_clicks_share_one_active_scan(self):
        entered=threading.Event();release=threading.Event();original=self.catalog.queue_all_previews
        def scan(*args,**kwargs):
            entered.set();release.wait(4);return original(*args,**kwargs)
        with patch.object(self.catalog,'queue_all_previews',side_effect=scan) as scanning:
            self.worker.reconcile_archive(full=True)
            try:
                self.assertTrue(entered.wait(2))
                for _ in range(5):self.worker.reconcile_archive(full=True)
            finally:release.set()
            self.wait(lambda:not self.worker.fill_missing_requested and not self.worker.reconciling)
            self.assertEqual(scanning.call_count,1)

    def test_fill_intent_survives_restart_even_before_first_checkpoint(self):
        self.worker.set_paused(True);self.worker.reconcile_archive(full=True)
        self.assertTrue(self.worker.fill_missing_requested)
        self.reopen()
        self.assertTrue(self.worker.fill_missing_requested);self.assertTrue(self.worker.manual_paused)
        self.worker.set_paused(False);self.worker.reconcile_archive()
        self.wait(lambda:not self.worker.fill_missing_requested and not self.worker.reconciling)
        self.assertEqual(self.worker.scan_progress['queued'],0)
        self.assertEqual(self.state()['pending'],4)

    def test_transient_recovery_error_is_visible_and_automatically_retried(self):
        original=self.catalog.pending_preview_changes;failed=threading.Event()
        def transient():
            if not failed.is_set():failed.set();raise sqlite3.OperationalError('模拟暂时繁忙')
            return original()
        with patch.object(self.catalog,'pending_preview_changes',side_effect=transient):
            self.worker.reconcile_archive()
            self.assertTrue(failed.wait(2))
            self.wait(lambda:bool(self.worker.reconcile_error))
            self.assertIn('模拟暂时繁忙',self.state()['reconcile_error'])
            self.wait(lambda:not self.worker.reconcile_error and not self.worker.reconcile_requested.is_set() and not self.worker.reconciling)
        self.assertEqual(self.state()['pending'],4)


if __name__=='__main__':unittest.main()
