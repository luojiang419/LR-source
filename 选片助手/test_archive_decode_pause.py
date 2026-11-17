"""手动暂停保留真实缓存队列、在途收尾、后台请求及重启恢复。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from asset_catalog import AssetCatalog
from preview_cache import PreviewWarmer,cache_for,MAX_WARM_WORKERS


class ArchiveDecodePauseTests(unittest.TestCase):
    def test_wal_pressure_respects_full_speed_and_only_limits_automatic_mode(self):
        self.worker.set_full_speed(True,'max')
        self.worker.interactive=lambda:True
        self.catalog.wal_maintenance.pressure.set()
        self.assertEqual(self.worker.worker_limit(),MAX_WARM_WORKERS)
        self.assertTrue(self.worker.full_speed)
        self.assertFalse(self.worker.manual_paused)
        entered=threading.Barrier(5,timeout=5);release=threading.Event()
        original=self.catalog.render_preview_batch
        def render(*args):
            entered.wait()
            if not release.wait(5):raise AssertionError('四路在途任务未释放')
            return original(*args)
        with patch.object(self.catalog,'render_preview_batch',side_effect=render),ThreadPoolExecutor(4) as pool:
            futures=[pool.submit(self.worker.process_one) for _ in range(4)]
            try:
                entered.wait()
                self.assertEqual(len(self.worker.active),4)
            finally:release.set()
            self.assertTrue(all(future.result(5) for future in futures))
        self.worker.set_full_speed(False)
        self.assertEqual(self.worker.worker_limit(),2)
        self.worker.set_full_speed(True,'max')
        self.assertEqual(self.worker.worker_limit(),MAX_WARM_WORKERS)
        self.catalog.wal_maintenance.pressure.clear()
        self.assertEqual(self.worker.worker_limit(),MAX_WARM_WORKERS)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        photos=self.base/'photos';photos.mkdir()
        for number in range(4):Image.new('RGB',(640,480),(number*30,70,90)).save(photos/f'{number}.jpg')
        self.catalog=AssetCatalog(self.base/'data');self.cache=cache_for(self.base/'data/previews')
        self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.catalog.on_previews=self.worker.enqueue
        self.root=self.catalog.add_root(str(photos))['root_id']

    def tearDown(self):
        self.worker.close();self.catalog.close();self.temp.cleanup()

    def state(self):return self.worker.archive_state([self.root])

    def test_controls_persist_while_cache_writer_is_blocked(self):
        held=threading.Event();release=threading.Event()
        def hold():
            with self.cache.lock,self.cache.connect() as c:
                c.execute('BEGIN IMMEDIATE');held.set();release.wait(5)
        with ThreadPoolExecutor(2) as pool:
            owner=pool.submit(hold)
            try:
                self.assertTrue(held.wait(1))
                pool.submit(self.worker.set_paused,True).result(.5)
                self.assertTrue(self.worker.manual_paused)
                pool.submit(self.worker.set_full_speed,True,4).result(.5)
                pool.submit(self.worker.set_paused,False).result(.5)
            finally:release.set();owner.result(1)
        self.worker.close();self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.assertTrue(self.worker.full_speed);self.assertEqual(self.worker.requested_workers,4)
        self.assertFalse(self.worker.manual_paused)

    def test_pause_does_not_wait_for_slow_claim_database_io(self):
        entered=threading.Event();release=threading.Event();connect=self.cache.connect
        class SlowConnection:
            def __init__(self,connection):self.connection=connection
            def execute(self,sql,*args):
                if sql.startswith('SELECT * FROM pending'):
                    entered.set();release.wait(5)
                return self.connection.execute(sql,*args)
        @contextmanager
        def slow():
            with connect() as c:yield SlowConnection(c)
        with patch.object(self.cache,'connect',slow),ThreadPoolExecutor(2) as pool:
            claiming=pool.submit(self.worker.process_one,wait_for_cache=False)
            try:
                self.assertTrue(entered.wait(1))
                pool.submit(self.worker.set_paused,True).result(.5)
                self.assertTrue(self.worker.manual_paused)
            finally:release.set()
            self.assertFalse(claiming.result(1))
        self.assertFalse(self.worker.active);self.assertEqual(self.state()['pending'],4)

    def test_control_write_failure_preserves_previous_state(self):
        self.worker.set_full_speed(True,4);before=self.worker.controls_path.read_bytes()
        with patch('preview_cache.atomic_write',side_effect=OSError('模拟配置写入失败')):
            with self.assertRaises(OSError):self.worker.set_paused(True)
            with self.assertRaises(OSError):self.worker.set_full_speed(False)
        self.assertFalse(self.worker.manual_paused);self.assertTrue(self.worker.full_speed)
        self.assertEqual(self.worker.controls_path.read_bytes(),before)

    def test_controls_do_not_wait_for_slow_native_worker_expansion(self):
        self.worker.set_paused(True);self.worker.start()
        self.worker.set_full_speed(True,'max')
        entered=threading.Event();release=threading.Event();original=self.worker._start_pending_worker
        def slow(thread):entered.set();release.wait(5);original(thread)
        with patch.object(self.worker,'_start_pending_worker',side_effect=slow),ThreadPoolExecutor(1) as pool:
            try:
                pool.submit(self.worker.set_paused,False).result(.5)
                self.assertTrue(entered.wait(1))
                pool.submit(self.worker.set_paused,True).result(.5)
                pool.submit(self.worker.set_full_speed,False).result(.5)
            finally:release.set()
        self.assertTrue(self.worker.manual_paused)

    def test_legacy_controls_are_loaded_until_independent_config_is_written(self):
        self.worker.close()
        with self.cache.connect() as c:
            c.executemany('INSERT OR REPLACE INTO configuration VALUES (?,?)',[
                ('archive_full_speed','1'),('archive_workers','4'),('archive_decode_paused','1')])
        self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.assertTrue(self.worker.full_speed);self.assertTrue(self.worker.manual_paused)
        self.worker.set_paused(False);self.worker.close()
        self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.assertFalse(self.worker.manual_paused);self.assertEqual(self.worker.requested_workers,4)

    def test_busy_cache_does_not_queue_idle_claims_or_hold_claim_lock(self):
        held=threading.Event();release=threading.Event()
        def hold():
            with self.cache.lock:held.set();release.wait(5)
        with ThreadPoolExecutor(2) as pool:
            owner=pool.submit(hold)
            try:
                self.assertTrue(held.wait(1))
                self.assertFalse(pool.submit(self.worker.process_one,wait_for_cache=False).result(1))
                self.assertFalse(self.worker.active)
                self.assertTrue(self.worker.lock.acquire(blocking=False))
                self.worker.lock.release()
            finally:release.set();owner.result(1)
        self.assertTrue(self.worker.process_one())
        self.assertEqual(self.state()['ready'],1)

    def test_unchanged_controls_do_not_wait_for_cache_or_write_configuration(self):
        self.worker.set_full_speed(True,4)
        with patch.object(self.cache,'connect',side_effect=AssertionError('重复控制不应写缓存库')):
            self.worker.set_full_speed(True,4);self.worker.set_paused(False)
        self.assertEqual(self.worker.worker_limit(),4)
        self.worker.set_paused(True)
        with patch.object(self.cache,'connect',side_effect=AssertionError('重复暂停不应写缓存库')):
            self.worker.set_paused(True)
        self.assertTrue(self.worker.manual_paused)

    def test_pause_persists_queue_and_completed_cache_resume_finishes_remaining(self):
        self.assertTrue(self.worker.process_one())
        self.worker.set_paused(True)
        with patch.object(self.catalog,'render_preview_batch',side_effect=AssertionError('暂停时不应解码')):
            self.assertFalse(self.worker.process_one())
        state=self.state()
        self.assertEqual((state['ready'],state['pending'],state['failed']),(1,3,0))
        self.assertTrue(state['manual_paused']);self.assertTrue(state['paused'])
        self.assertEqual(state['effective_workers'],0);self.assertIsNone(state['eta_seconds'])
        self.worker.close()
        self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.assertTrue(self.worker.manual_paused);self.assertFalse(self.worker.process_one())
        self.worker.set_paused(False)
        with patch.object(self.catalog,'render_preview_batch',wraps=self.catalog.render_preview_batch) as render:
            while self.worker.process_one():pass
            self.assertEqual(render.call_count,3)
        self.assertEqual((self.state()['ready'],self.state()['failed']),(4,0))
        self.worker.close()
        self.worker=PreviewWarmer(self.cache,'unused',catalog=self.catalog,autostart=False)
        self.assertFalse(self.worker.manual_paused)

    def test_new_process_restores_totals_and_only_decodes_interrupted_queue(self):
        self.assertTrue(self.worker.process_one())
        self.worker.set_full_speed(True,4)
        before=self.worker.archive_state(persisted=True)
        self.assertEqual((before['ready'],before['pending']),(1,3))
        self.worker.close();self.catalog.close()
        script='''
import json,sys,time
from pathlib import Path
from asset_catalog import AssetCatalog
from preview_cache import PreviewWarmer,cache_for
catalog=AssetCatalog(Path(sys.argv[1]))
worker=PreviewWarmer(cache_for(catalog.data/'previews'),'unused',catalog=catalog,autostart=False)
try:
    before=worker.archive_state(persisted=True)
    assert (before['ready'],before['pending'])==(1,3),before
    assert before['full_speed'] and before['effective_workers']==4,before
    assert before['completed_this_run']==0,before
    worker.start()
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        after=worker.archive_state(persisted=True)
        if after['pending']==0 and after['active_workers']==0:break
        time.sleep(.02)
    assert (after['ready'],after['failed'],after['completed_this_run'])==(4,0,3),after
    print(json.dumps({'before':before,'after':after}))
finally:worker.close();catalog.close()
'''
        result=subprocess.run([sys.executable,'-X','utf8','-c',script,str(self.base/'data')],
                              cwd=Path(__file__).parent,capture_output=True,text=True,encoding='utf8',timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['after']['ready'],4)

    def test_restored_totals_exclude_removed_folder_and_root(self):
        with self.catalog.connect() as c:
            row=c.execute('SELECT id FROM items LIMIT 1').fetchone()
            c.execute('UPDATE items SET online=0 WHERE id=?',(row[0],))
        self.assertEqual(self.worker.archive_state(persisted=True)['total'],3)
        self.catalog.remove(self.root)
        self.assertEqual(self.worker.archive_state(persisted=True)['total'],0)

    def test_pause_allows_inflight_file_to_finish_without_claiming_the_next(self):
        entered=threading.Event();release=threading.Event();original=self.catalog.render_preview_batch
        def render(*args):
            entered.set()
            if not release.wait(5):raise AssertionError('在途文件未释放')
            return original(*args)
        with patch.object(self.catalog,'render_preview_batch',side_effect=render),ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.worker.process_one)
            try:
                self.assertTrue(entered.wait(2));self.worker.set_paused(True)
                self.assertFalse(self.worker.process_one())
                self.assertEqual(self.state()['active_workers'],1)
            finally:release.set()
            self.assertTrue(future.result(3))
        self.assertEqual((self.state()['ready'],self.state()['pending']),(1,3))
        self.assertFalse(self.worker.process_one())

    def test_paused_reconciliation_waits_and_close_exits_without_resume(self):
        entered=threading.Event();finished=threading.Event()
        def wait():
            entered.set();result=self.worker._wait_for_background();finished.set();return result
        self.worker.set_paused(True)
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(wait);self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(.1));self.worker.close()
            self.assertTrue(future.result(1))

    def test_paused_foreground_requests_remain_queued_until_resume(self):
        self.worker.set_paused(True)
        queued=threading.Event();row=self.catalog.items()['rows'][0]
        root,record=self.catalog.preview_record(row,[row])
        with patch.object(self.worker,'enqueue',side_effect=lambda *_:queued.set()):
            self.worker.start();self.worker.request(root,[record])
            self.assertFalse(queued.wait(.15))
            with self.worker.request_lock:self.assertEqual(len(self.worker.requests),1)
            self.worker.set_paused(False);self.assertTrue(queued.wait(2))

    def test_resume_preserves_speed_and_grows_pool_when_full_speed_selected_while_paused(self):
        self.worker.set_paused(True);self.worker.start()
        self.worker.set_full_speed(True,'max')
        self.assertEqual(self.worker.worker_limit(),0)
        self.worker.set_paused(False)
        self.assertEqual(self.worker.worker_limit(),MAX_WARM_WORKERS)
        self.assertGreaterEqual(len(self.worker.threads),MAX_WARM_WORKERS)
        until=time.monotonic()+5
        while any(thread.ident is None for thread in self.worker.threads) and time.monotonic()<until:time.sleep(.01)
        self.assertTrue(all(thread.ident is not None for thread in self.worker.threads))

    def test_resume_resets_eta_and_rejects_invalid_pause_value(self):
        self.worker.decode_samples.extend([(self.root,90,92),(self.root,90,100)])
        self.worker.set_paused(True)
        self.worker.decode_samples.append((self.root,90,100))  # 在途文件在暂停后完成。
        self.worker.set_paused(False)
        self.assertFalse(self.worker.decode_samples)
        self.assertIsNone(self.state()['eta_seconds'])
        for invalid in (0,1,None,'true'):
            with self.assertRaises(ValueError):self.worker.set_paused(invalid)
        self.assertFalse(self.worker.manual_paused)


if __name__=='__main__':unittest.main()
