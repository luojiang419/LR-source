"""缓存持久化、入库预生成、失效和容量淘汰的真实文件验证。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager,closing
import hashlib
import io
from pathlib import Path
import tempfile
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from catalog_store import Store
import media_engine as media
import preview_cache as preview_cache_module
from preview_cache import MAX_CACHE_BYTES,PreviewCache,PreviewWarmer,cache_name

ROOT=Path(__file__).resolve().parents[1]


class PreviewCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.photos=self.root/'照片';self.photos.mkdir();self.folder=self.root/'cache'
        self.cache=PreviewCache(self.folder)
        self.patch=patch.object(media,'cache_for',return_value=self.cache);self.patch.start()

    def tearDown(self):self.patch.stop();self.temp.cleanup()

    def record(self,name='001.JPG',color='red'):
        path=self.photos/name;Image.new('RGB',(1200,800),color).save(path)
        media._STATS.pop(str(path),None)
        return media.capture_record(self.photos,path,None,{})

    def warm(self,record):
        worker=PreviewWarmer(self.cache,Path('unused'),autostart=False)
        worker.enqueue(self.photos,[record]);self.assertTrue(worker.process_one());self.assertFalse(worker.process_one());return worker

    def test_restart_reuses_both_sizes_without_opening_source_or_exiftool(self):
        record=self.record();self.warm(record)
        self.assertEqual(self.cache.state()['images'],2)
        self.assertEqual(self.cache.state()['limit_bytes'],MAX_CACHE_BYTES)
        restarted=PreviewCache(self.folder)
        (self.photos/'001.JPG').unlink()  # 缓存命中在源目录离线时也可浏览。
        with patch.object(media,'cache_for',return_value=restarted),patch.object(media,'_companion_preview',side_effect=AssertionError('重复解码')):
            for edge in (480,2048):
                payload=media.preview_bytes(record,self.photos,self.folder,lambda:self.fail('缓存不应查找ExifTool'),edge)
                with Image.open(io.BytesIO(payload)) as image:self.assertLessEqual(max(image.size),edge)

    def test_mature_startup_does_not_wait_for_another_cache_writer(self):
        with self.cache.connect() as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("INSERT INTO configuration VALUES ('writer_test','1')")
            started=time.monotonic()
            with patch.object(PreviewCache,'_initialize',side_effect=AssertionError('成熟库不应迁移')):
                restarted=PreviewCache(self.folder,defer_maintenance=True)
            self.assertLess(time.monotonic()-started,1)
            self.assertEqual(restarted.state()['images'],0)

    def test_schema_change_repairs_trigger_once_and_preserves_portable_counts(self):
        with self.cache.connect() as c:
            c.execute('DROP TRIGGER portable_alias_added')
            c.execute('CREATE TRIGGER portable_alias_added AFTER INSERT ON content_previews BEGIN SELECT 1; END')
        restarted=PreviewCache(self.folder,defer_maintenance=True)
        with restarted.connect() as c:
            sql=c.execute("SELECT sql FROM sqlite_master WHERE name='portable_alias_added'").fetchone()[0]
            self.assertIn('INSERT INTO portable_names',sql)
            version=c.execute('PRAGMA schema_version').fetchone()[0]
        again=PreviewCache(self.folder,defer_maintenance=True)
        with again.connect() as c:self.assertEqual(c.execute('PRAGMA schema_version').fetchone()[0],version)

    def test_queue_is_durable_and_resumes_after_restart(self):
        record=self.record();PreviewWarmer(self.cache,'unused',autostart=False).enqueue(self.photos,[record])
        restarted=PreviewCache(self.folder);worker=PreviewWarmer(restarted,'unused',autostart=False)
        self.assertEqual(restarted.state()['pending'],1);self.assertTrue(worker.process_one());self.assertFalse(worker.process_one())
        self.assertEqual(restarted.state()['pending'],0);self.assertEqual(restarted.state()['images'],2)

    def test_connection_scope_keeps_individual_transactions_and_isolates_nested_calls(self):
        with self.cache.connection_scope():
            with self.cache.connect() as first:first.execute("INSERT INTO configuration VALUES ('committed','yes')")
            with closing(sqlite3.connect(self.cache.db)) as observer:
                self.assertEqual(observer.execute("SELECT value FROM configuration WHERE key='committed'").fetchone()[0],'yes')
            with self.assertRaisesRegex(ValueError,'rollback'):
                with self.cache.connect() as outer:
                    self.assertIs(outer,first)
                    outer.execute("INSERT INTO configuration VALUES ('uncommitted','no')")
                    with self.cache.connect() as inner:
                        self.assertIsNot(inner,outer)
                        self.assertIsNone(inner.execute("SELECT value FROM configuration WHERE key='uncommitted'").fetchone())
                    raise ValueError('rollback')
            with self.assertRaises(sqlite3.ProgrammingError):inner.execute('SELECT 1')
            with self.cache.connect() as again:
                self.assertIs(again,first)
                self.assertIsNone(again.execute("SELECT value FROM configuration WHERE key='uncommitted'").fetchone())
                self.assertEqual(again.execute("SELECT value FROM configuration WHERE key='committed'").fetchone()[0],'yes')
        with self.assertRaises(sqlite3.ProgrammingError):first.execute('SELECT 1')

    def test_connection_scope_closes_on_exception_and_is_thread_private(self):
        with self.assertRaisesRegex(RuntimeError,'scope failed'):
            with self.cache.connection_scope():
                with self.cache.connect() as failed:failed.execute('SELECT 1')
                raise RuntimeError('scope failed')
        with self.assertRaises(sqlite3.ProgrammingError):failed.execute('SELECT 1')
        barrier=threading.Barrier(2)
        def reader():
            with self.cache.connection_scope():
                with self.cache.connect() as connection:
                    barrier.wait(2);self.assertEqual(connection.execute('SELECT 1').fetchone()[0],1)
                with self.cache.connect() as again:self.assertIs(again,connection)
                return connection
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(reader);second=pool.submit(reader)
            self.assertIsNot(first.result(3),second.result(3))

    def test_enqueue_unchanged_ready_does_not_write_or_change_database_version(self):
        record=dict(self.record(),_catalog=True,_catalog_root='root',_catalog_pair='pair')
        index=preview_cache_module.cache_for(self.root/'catalog_previews')
        media.warm_previews(record,self.photos,index.folder,None,cache_index=index)
        worker=PreviewWarmer(self.cache,None,autostart=False);self.addCleanup(worker.close)
        worker.enqueue(self.photos,[record]);changes=[];connect=self.cache.connect
        @contextmanager
        def observed():
            with connect() as c:
                before=c.total_changes
                try:yield c
                finally:changes.append(c.total_changes-before)
        with closing(sqlite3.connect(self.cache.db)) as observer:
            before=observer.execute('PRAGMA data_version').fetchone()[0]
            with patch.object(self.cache,'connect',observed):worker.enqueue(self.photos,[record])
            self.assertEqual(sum(changes),0)
            self.assertEqual(observer.execute('PRAGMA data_version').fetchone()[0],before)
        with self.cache.connect() as c:self.assertEqual(c.execute('SELECT state FROM archive_jobs').fetchone()[0],'ready')

    def test_enqueue_ready_transitions_preserve_failures_until_source_version_changes(self):
        record=dict(self.record(),_catalog=True,_catalog_root='root',_catalog_pair='pair')
        index=preview_cache_module.cache_for(self.root/'catalog_previews')
        worker=PreviewWarmer(self.cache,None,autostart=False);self.addCleanup(worker.close)
        def state():
            with self.cache.connect() as c:
                job=dict(c.execute('SELECT * FROM archive_jobs').fetchone())
                pending=c.execute('SELECT attempts FROM pending').fetchone()
                return job['state'],job['small_ready'],pending[0] if pending else None
        worker.enqueue(self.photos,[record]);self.assertEqual(state(),('queued',0,0))
        media.warm_previews(record,self.photos,index.folder,None,cache_index=index)
        worker.enqueue(self.photos,[record]);self.assertEqual(state(),('ready',1,None))
        with index.lock,index.connect() as c:index._remove(c,cache_name(record,2048))
        worker.enqueue(self.photos,[record]);self.assertEqual(state(),('queued',1,0))
        with self.cache.connect() as c:
            c.execute("UPDATE archive_jobs SET state='failed'");c.execute('UPDATE pending SET attempts=3')
        worker.enqueue(self.photos,[record]);self.assertEqual(state(),('failed',1,3))
        changed=dict(record,fingerprint=record['fingerprint']+'-changed')
        worker.enqueue(self.photos,[changed]);self.assertEqual(state(),('queued',0,0))
        with self.cache.connect() as c:self.assertIn(changed['fingerprint'],c.execute('SELECT record FROM archive_jobs').fetchone()[0])

    def test_foreground_preview_defers_history_and_releases_after_errors(self):
        record=self.record();worker=PreviewWarmer(self.cache,'unused',autostart=False)
        worker.enqueue(self.photos,[record])
        with self.assertRaisesRegex(RuntimeError,'foreground failed'):
            with worker.foreground():
                with patch.object(preview_cache_module.time,'monotonic',return_value=10**12):
                    self.assertFalse(worker.process_one())
                self.assertEqual(self.cache.state()['pending'],1)
                raise RuntimeError('foreground failed')
        self.assertEqual(worker.foreground_active,0)
        with patch.object(preview_cache_module.time,'monotonic',return_value=worker.foreground_until+1):
            self.assertTrue(worker.process_one())
        self.assertEqual(self.cache.state()['pending'],0)

    def test_waiting_history_claim_rechecks_foreground_before_decode(self):
        worker=PreviewWarmer(self.cache,'unused',autostart=False);self.addCleanup(worker.close)
        worker.enqueue(self.photos,[self.record()]);checked=threading.Event()
        worker.interactive=lambda:(checked.set() or False)
        with patch.object(media,'warm_previews') as decode,ThreadPoolExecutor(max_workers=1) as pool:
            with self.cache.lock:
                future=pool.submit(worker.process_one);self.assertTrue(checked.wait(2))
                foreground=worker.foreground();foreground.__enter__()
            try:self.assertFalse(future.result(2))
            finally:foreground.__exit__(None,None,None)
        decode.assert_not_called();self.assertEqual(self.cache.state()['pending'],1)

    def test_recovery_yields_between_sixteen_item_batches_without_losing_records(self):
        from types import SimpleNamespace
        first=threading.Event();resume=threading.Event();second=threading.Event();finished=threading.Event();sizes=[]
        records=[{'id':str(i),'fingerprint':str(i)} for i in range(41)]
        worker=PreviewWarmer(self.cache,None,autostart=False);self.addCleanup(worker.close)
        def scan(stop,**kwargs):
            if stop():return
            worker.enqueue(self.photos,iter(records));finished.set()
        worker.catalog=SimpleNamespace(queue_all_previews=scan)
        original=worker._enqueue_batch
        def batch(*args):
            original(*args);sizes.append(len(args[-1]))
            if len(sizes)==1:
                first.set()
                if not resume.wait(3):raise RuntimeError('测试恢复未释放')
            else:second.set()
        with patch.object(worker,'_enqueue_batch',side_effect=batch):
            worker.reconcile_archive()
            try:
                self.assertTrue(first.wait(2))
                with worker.foreground():
                    resume.set();self.assertFalse(second.wait(.15))
                    self.assertEqual(self.cache.state()['pending'],16)
                self.assertTrue(finished.wait(3))
            finally:resume.set()
        self.assertEqual(sizes,[16,16,9])
        self.assertEqual(self.cache.state()['pending'],41)

    def test_close_interrupts_recovery_waiting_for_foreground(self):
        from types import SimpleNamespace
        scan=threading.Event()
        worker=PreviewWarmer(self.cache,None,autostart=False,
            catalog=SimpleNamespace(queue_all_previews=lambda *a,**k:scan.set()))
        self.addCleanup(worker.close)
        with worker.foreground():
            worker.reconcile_archive()
            until=time.monotonic()+2
            while not worker.reconciling and time.monotonic()<until:time.sleep(.005)
            self.assertTrue(worker.reconciling);self.assertFalse(scan.is_set())
            started=time.monotonic();worker.close()
            self.assertLess(time.monotonic()-started,1)
        self.assertFalse(worker.reconcile_thread.is_alive());self.assertFalse(scan.is_set())

    def test_live_monitor_limits_history_jobs_without_blocking_foreground(self):
        records=[self.record(f'{i}.JPG') for i in range(3)]
        worker=PreviewWarmer(self.cache,'unused',autostart=False);worker.interactive=lambda:True
        worker.enqueue(self.photos,records);entered=threading.Event();release=threading.Event();count=[]
        def warm(*args,**kwargs):
            count.append(1)
            if len(count)==2:entered.set()
            if not release.wait(3):raise RuntimeError('历史任务未释放')
        with patch.object(media,'warm_previews',side_effect=warm),ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(worker.process_one) for _ in range(2)]
            try:
                self.assertTrue(entered.wait(3));self.assertFalse(worker.process_one())
                with worker.foreground():self.assertEqual(worker.foreground_active,1)
            finally:release.set()
            self.assertTrue(all(f.result() for f in futures))
        self.assertEqual(len(count),2)
        with patch.object(preview_cache_module.time,'monotonic',return_value=worker.foreground_until+1):
            self.assertTrue(worker.process_one())

    @unittest.skipUnless(preview_cache_module.WARM_WORKERS>=4,'本机预算不足四路')
    def test_full_speed_bypasses_monitor_cap_and_persists(self):
        worker=PreviewWarmer(self.cache,'unused',autostart=False);worker.interactive=lambda:True
        self.assertEqual(worker.worker_limit(),2)
        worker.set_full_speed(True)
        restarted=PreviewWarmer(self.cache,'unused',autostart=False);restarted.interactive=lambda:True
        self.assertEqual(restarted.worker_limit(),preview_cache_module.WARM_WORKERS);restarted.close()
        worker.enqueue(self.photos,[self.record(f'full-speed-{i}.JPG') for i in range(4)])
        barrier=threading.Barrier(4,timeout=3);calls=[]
        def render(*args,**kwargs):calls.append(args[0]['id']);barrier.wait()
        try:
            with patch.object(media,'warm_previews',side_effect=render),ThreadPoolExecutor(4) as pool:
                self.assertTrue(all(f.result(5) for f in [pool.submit(worker.process_one) for _ in range(4)]))
            self.assertEqual(len(calls),4)
            worker.set_full_speed(False);self.assertEqual(worker.worker_limit(),2)
        finally:worker.close()

    def test_extreme_concurrency_grows_running_pool_and_is_memory_bounded(self):
        worker=PreviewWarmer(self.cache,'unused',autostart=True);worker.interactive=lambda:True
        limit=preview_cache_module.MAX_WARM_WORKERS
        try:
            worker.set_full_speed(True,'max')
            self.assertEqual(worker.worker_limit(),limit)
            self.assertGreaterEqual(len(worker.threads),limit)
            until=time.monotonic()+5
            while any(thread.ident is None for thread in worker.threads) and time.monotonic()<until:time.sleep(.01)
            self.assertTrue(all(thread.ident is not None for thread in worker.threads))
            for invalid in (0,limit+1,True,'unlimited'):
                with self.assertRaises(ValueError):worker.set_full_speed(True,invalid)
            self.assertEqual(worker.worker_limit(),limit)
            restored=PreviewWarmer(self.cache,'unused',autostart=False)
            try:self.assertEqual(restored.worker_limit(),limit)
            finally:restored.close()
            worker.set_full_speed(False);self.assertEqual(worker.worker_limit(),min(2,preview_cache_module.WARM_WORKERS))
        finally:worker.close()

    def test_instant_live_preview_uses_bounded_memory_without_disk_cache_or_gpu(self):
        record=self.record()
        with patch.dict(media._QUICK_PREVIEWS,{},clear=True),patch.object(media,'cache_for',side_effect=AssertionError('不应等待磁盘缓存')),patch('gpu_media.decode_jpeg',side_effect=AssertionError('不应等待GPU')):
            payload=media.quick_preview_bytes(record,self.photos)
            with Image.open(io.BytesIO(payload)) as image:
                self.assertLessEqual(max(image.size),480);self.assertFalse(image.getexif())
            (self.photos/'001.JPG').unlink()
            self.assertEqual(media.quick_preview_bytes(record,self.photos),payload)
            for i in range(10):media.quick_preview_bytes(self.record(f'quick-{i}.JPG'),self.photos)
            self.assertLessEqual(len(media._QUICK_PREVIEWS),8)

    def test_cache_reads_batch_access_writes_and_trim_uses_latest_access(self):
        record=self.record();self.warm(record);name=cache_name(record,480)
        with self.cache.connect() as c:c.execute('UPDATE previews SET accessed=1')
        self.cache._last_touch_flush=time.monotonic()
        for _ in range(5):self.assertIsNotNone(self.cache.get(name,read=True))
        with self.cache.connect() as c:self.assertEqual(c.execute('SELECT accessed FROM previews WHERE name=?',(name,)).fetchone()[0],1)
        with self.cache.lock,self.cache.connect() as c:
            self.cache._trim(c)
            self.assertGreater(c.execute('SELECT accessed FROM previews WHERE name=?',(name,)).fetchone()[0],1)
        self.assertFalse(self.cache._touches)

    def test_cache_hits_do_not_wait_for_another_writer_and_retry_access_time(self):
        record=self.record();self.warm(record);name=cache_name(record,480)
        with self.cache.connect() as c:c.execute('UPDATE previews SET accessed=1')
        for read in (False,True):
            with self.subTest(read=read),closing(sqlite3.connect(self.cache.db)) as writer:
                writer.execute('BEGIN IMMEDIATE')
                writer.execute("INSERT OR REPLACE INTO configuration VALUES ('busy_writer','1')")
                self.cache._last_touch_flush=0
                started=time.monotonic()
                self.assertIsNotNone(self.cache.get(name,read=read))
                self.assertLess(time.monotonic()-started,.25,'缓存命中等待了无关写事务')
                self.assertIn(name,self.cache._touches)
                writer.rollback()
            self.assertIsNotNone(self.cache.get(name,read=read))
            if read:self.cache.flush_deferred_touches()
            self.assertNotIn(name,self.cache._touches)
            with self.cache.connect() as c:
                self.assertGreater(c.execute('SELECT accessed FROM previews WHERE name=?',(name,)).fetchone()[0],1)
        self.assertFalse(self.cache._read_leases)

    def test_access_flush_keeps_touches_on_outer_rollback_and_restores_timeout(self):
        record=self.record();self.warm(record);name=cache_name(record,480)
        with self.cache.connect() as c:c.execute('UPDATE previews SET accessed=1')
        self.cache.get(name)
        with self.assertRaisesRegex(ValueError,'rollback'):
            with self.cache.lock,self.cache.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                self.cache._flush_touches(c,nonblocking=True)
                self.assertEqual(c.execute('PRAGMA busy_timeout').fetchone()[0],30000)
                self.assertIn(name,self.cache._touches)
                raise ValueError('rollback')
        with self.cache.connect() as c:
            self.assertEqual(c.execute('SELECT accessed FROM previews WHERE name=?',(name,)).fetchone()[0],1)
        self.assertIn(name,self.cache._touches);self.assertFalse(self.cache._touch_flushes)
        with self.cache.lock,self.cache.connect() as c:self.cache._flush_touches(c)
        self.assertFalse(self.cache._touches)

    def test_long_writer_contention_keeps_only_bounded_recent_touches(self):
        records=[self.record(f'bounded-{i}.JPG') for i in range(4)]
        for record in records:self.warm(record)
        names=[cache_name(record,480) for record in records]
        with patch.object(preview_cache_module,'_MAX_PENDING_TOUCHES',3),closing(sqlite3.connect(self.cache.db)) as writer:
            writer.execute('BEGIN IMMEDIATE');self.cache._last_touch_flush=0
            for name in names:self.assertIsNotNone(self.cache.get(name,read=True))
            self.assertEqual(list(self.cache._touches),names[1:])
            self.cache.get(names[1],read=True)
            self.assertEqual(list(self.cache._touches),[names[2],names[3],names[1]])
            writer.rollback()
        # 写锁释放后，淘汰路径仍强制合并最近的访问，不能把刚看过的图当成旧图。
        with self.cache.lock,self.cache.connect() as c:
            self.cache._trim(c)
            for name in names[1:]:
                self.assertGreater(c.execute('SELECT accessed FROM previews WHERE name=?',(name,)).fetchone()[0],1)
        self.assertFalse(self.cache._touches)

    def test_failed_commit_does_not_acknowledge_access_touches(self):
        record=self.record();self.warm(record);name=cache_name(record,480)
        self.cache.get(name)
        original_connect=sqlite3.connect
        class FailedCommit(sqlite3.Connection):
            def __exit__(self,kind,value,traceback):
                if kind is None:
                    self.rollback();raise sqlite3.OperationalError('simulated commit failure')
                return super().__exit__(kind,value,traceback)
        def failing_connect(*args,**kwargs):return original_connect(*args,**kwargs,factory=FailedCommit)
        with patch.object(preview_cache_module.sqlite3,'connect',side_effect=failing_connect):
            with self.assertRaisesRegex(sqlite3.OperationalError,'simulated commit failure'):
                with self.cache.lock,self.cache.connect() as c:
                    c.execute('BEGIN IMMEDIATE');self.cache._flush_touches(c)
        self.assertIn(name,self.cache._touches);self.assertFalse(self.cache._touch_flushes)
        with self.cache.lock,self.cache.connect() as c:self.cache._flush_touches(c)
        self.assertNotIn(name,self.cache._touches)

    def test_truncated_cached_payload_is_a_miss_and_preserves_replacement(self):
        record=self.record();self.warm(record);name=cache_name(record,480)
        replacement=self.folder/name;replacement.write_bytes(b'externally replaced')
        self.assertIsNone(self.cache.get(name,read=True))
        self.assertEqual(replacement.read_bytes(),b'externally replaced')
        self.assertFalse(self.cache._read_leases)
        with self.cache.connect() as c:
            self.assertIsNone(c.execute('SELECT name FROM previews WHERE name=?',(name,)).fetchone())

    def test_parallel_payload_reads_release_index_lock_but_hold_eviction_lease(self):
        first=self.record('001.JPG','red');second=self.record('002.JPG','blue')
        self.warm(first);self.warm(second)
        names=[cache_name(record,480) for record in (first,second)]
        entered=threading.Event();release=threading.Event();count=0;guard=threading.Lock()
        original=preview_cache_module.validate_stream
        def validate(stream,size):
            nonlocal count
            with guard:
                count+=1
                if count==2:entered.set()
            release.wait(3);return original(stream,size)
        with patch.object(preview_cache_module,'validate_stream',side_effect=validate),ThreadPoolExecutor(max_workers=2) as pool:
            first_read=pool.submit(self.cache.get,names[0],read=True)
            second_read=pool.submit(self.cache.get,names[1],read=True)
            self.assertTrue(entered.wait(2),'第二个缓存读取仍被全局锁串行阻塞')
            with self.cache.lock,self.cache.connect() as c:
                self.assertFalse(self.cache._remove(c,names[0]))
            release.set();self.assertIsNotNone(first_read.result(timeout=3));self.assertIsNotNone(second_read.result(timeout=3))

    def test_cached_payload_does_not_wait_for_batch_writer_lock(self):
        record=self.record();self.warm(record);name=cache_name(record,480)
        held=threading.Event();release=threading.Event()
        def writer():
            with self.cache.lock,self.cache.connect() as c:
                c.execute('BEGIN IMMEDIATE');held.set();release.wait(3)
        with ThreadPoolExecutor(2) as pool:
            writing=pool.submit(writer);self.assertTrue(held.wait(1))
            try:
                with patch.object(self.cache,'connect',side_effect=AssertionError('命中读取不能打开写连接')):
                    payload=pool.submit(self.cache.get,name,read=True).result(timeout=.5)
                    self.assertTrue(payload.startswith(b'\xff\xd8'))
            finally:release.set();writing.result(3)

    def test_lru_evicts_only_published_cache_and_preserves_unknown_files(self):
        payload=io.BytesIO();Image.new('RGB',(60,40),'blue').save(payload,format='JPEG');data=payload.getvalue()
        records=[{'fingerprint':str(i)} for i in range(3)];keys=[cache_name(r,480) for r in records]
        for r,name in zip(records[:2],keys[:2]):
            temp=(self.folder/name).with_suffix('.tmp');temp.write_bytes(data)
            self.cache.publish(temp,self.folder/name,r,480,False)
        unrelated=self.folder/'user-note.jpg';unrelated.write_bytes(b'keep')
        hashed=self.folder/('f'*64+'.jpg');hashed.write_bytes(data)
        abandoned=hashed.with_suffix('.tmp');abandoned.write_bytes(b'keep temporary')
        __import__('os').utime(abandoned,(0,0))
        bounded=PreviewCache(self.folder,limit=len(data)*2)
        with bounded.connect() as c:c.execute('UPDATE previews SET accessed=1')
        bounded.get(keys[0])
        temp=(self.folder/keys[2]).with_suffix('.tmp');temp.write_bytes(data)
        bounded.publish(temp,self.folder/keys[2],records[2],480,False)
        self.assertIsNotNone(bounded.get(keys[0]));self.assertIsNone(bounded.get(keys[1]))
        self.assertTrue(unrelated.exists());self.assertEqual(hashed.read_bytes(),data);self.assertEqual(abandoned.read_bytes(),b'keep temporary');self.assertLessEqual(bounded.state()['bytes'],bounded.limit)
        with bounded.connect() as c:
            self.assertEqual(c.execute('SELECT bytes FROM usage').fetchone()[0],bounded.state()['bytes'])

    def test_startup_enforces_limit_and_removes_missing_entries(self):
        self.warm(self.record());state=self.cache.state()
        smallest=min(self.folder.glob('*.jpg'),key=lambda p:p.stat().st_size)
        bounded=PreviewCache(self.folder,limit=smallest.stat().st_size)
        self.assertLessEqual(bounded.state()['bytes'],bounded.limit)
        for path in self.folder.glob('*.webp'):path.unlink()
        self.assertEqual(PreviewCache(self.folder).state()['bytes'],0)
        self.assertGreater(state['bytes'],0)

    def test_corrupt_and_deleted_cache_regenerates(self):
        record=self.record();path=media.preview(record,self.photos,self.folder,'unused')
        path.write_bytes(b'broken')
        # 无法区分外部替换与损坏；保留原文件，通过受管恢复槽重新生成。
        payload=media.preview_bytes(record,self.photos,self.folder,'unused')
        self.assertEqual(path.read_bytes(),b'broken')
        with Image.open(io.BytesIO(payload)) as image:self.assertEqual(image.size,(480,320))
        recovered=media.preview(record,self.photos,self.folder,'unused');self.assertNotEqual(recovered,path)
        recovered.unlink();self.assertTrue(media.preview(record,self.photos,self.folder,'unused').is_file())
        self.assertEqual(path.read_bytes(),b'broken')

    def test_eviction_preserves_external_replacement_and_same_name_source(self):
        record=self.record();path=media.preview(record,self.photos,self.folder,'unused')
        Image.new('RGB',(100,80),'yellow').save(path);payload=path.read_bytes()
        PreviewCache(self.folder,limit=0)
        self.assertEqual(path.read_bytes(),payload)
        recovered=media.preview(record,self.photos,self.folder,'unused')
        self.assertNotEqual(recovered,path)
        self.assertTrue(recovered.is_file())
        self.assertEqual(self.cache.get(path.name,read=True),recovered.read_bytes())
        self.assertEqual(path.read_bytes(),payload)

    def test_old_index_migration_preserves_files_without_claiming_ownership(self):
        record=self.record();path=media.preview(record,self.photos,self.folder,'unused');payload=path.read_bytes()
        unknown=self.folder/('a'*64+'.jpg');unknown.write_bytes(payload)
        with self.cache.connect() as c:
            c.execute('UPDATE previews SET digest=NULL')
            c.execute('INSERT INTO previews(name,bytes,accessed) VALUES (?,?,?)',(unknown.name,len(payload),1))
            c.execute('ALTER TABLE previews DROP COLUMN digest')
        migrated=PreviewCache(self.folder)
        self.assertIsNone(migrated.get(path.name,read=True))
        self.assertEqual(path.read_bytes(),payload)
        self.assertEqual(migrated.state()['images'],0);self.assertEqual(unknown.read_bytes(),payload)

    def test_existing_hash_temp_file_is_never_overwritten_or_cleaned(self):
        record=self.record();temp=self.folder/Path(cache_name(record,480)).with_suffix('.tmp')
        temp.write_bytes(b'other software temporary original')
        media.preview(record,self.photos,self.folder,'unused')
        self.assertEqual(temp.read_bytes(),b'other software temporary original')

    def test_file_version_and_orientation_use_separate_cache_keys(self):
        record=self.record();old=media.preview(record,self.photos,self.folder,'unused')
        changed=self.record(color='green');changed['orientation']=6;self.warm(changed)
        new=media.preview(changed,self.photos,self.folder,'unused')
        self.assertNotEqual(old,new)
        with Image.open(new) as image:self.assertEqual(image.size,(320,480))

    def test_parallel_foreground_and_warmer_generate_each_size_once(self):
        record=self.record();original=self.cache.publish;calls=[];lock=threading.Lock()
        def load(*args):
            with lock:calls.append(args[3])
            time.sleep(.02);return original(*args)
        worker=PreviewWarmer(self.cache,'unused',autostart=False);worker.enqueue(self.photos,[record])
        with patch.object(self.cache,'publish',side_effect=load),ThreadPoolExecutor(max_workers=8) as pool:
            work=[pool.submit(worker.process_one)]
            work += [pool.submit(media.preview_bytes,record,self.photos,self.folder,'unused',edge) for edge in [480,2048]*4]
            for result in work:result.result(timeout=15)
        self.assertEqual(sorted(calls),[480,2048]);self.assertEqual(self.cache.state()['images'],2)

    def test_failure_is_bounded_and_does_not_cache_placeholder(self):
        record=self.record();(self.photos/'001.JPG').unlink()
        worker=PreviewWarmer(self.cache,'unused',autostart=False);worker.enqueue(self.photos,[record])
        for _ in range(3):self.assertTrue(worker.process_one())
        self.assertFalse(worker.process_one());self.assertEqual(self.cache.state()['failed'],1)
        self.assertEqual(self.cache.state()['images'],0);self.assertFalse(list(self.folder.glob('*.tmp')))

    def test_live_append_and_pair_updates_schedule_without_changing_ratings(self):
        store=Store(self.root/'data',ROOT);worker=PreviewWarmer(self.cache,'unused',autostart=False)
        store.on_previews=worker.enqueue;sid=store.ensure_live_session(self.photos)
        record=self.record();result=store.append_live(sid,[record]);self.assertEqual(len(result['added']),1)
        self.assertEqual(self.cache.state()['pending'],1);worker.process_one();worker.process_one()
        old=store.records(sid)[0];new=dict(old,fingerprint='new-version')
        store.append_live(sid,[new]);worker.process_one();worker.process_one()
        self.assertEqual(self.cache.state()['images'],4)  # 极速默认按文件版本缓存，不为去重额外完整读取原图。
        self.assertEqual(store.records(sid)[0]['liveRating'],old['liveRating'])

    def test_import_scan_schedules_all_records(self):
        self.record();store=Store(self.root/'data',ROOT);seen=[];store.on_previews=lambda root,records:seen.extend(records)
        job=store.scan(str(self.photos));deadline=time.monotonic()+15
        while store.jobs[job]['status'] in ('queued','running') and time.monotonic()<deadline:time.sleep(.05)
        self.assertEqual(store.jobs[job]['status'],'completed',store.jobs[job]);self.assertEqual({r['id'] for r in seen},{store.records(store.jobs[job]['session_id'])[0]['id']})

    def test_newer_record_is_not_removed_by_old_job_completion(self):
        old=self.record();new=dict(old,fingerprint='replacement');worker=PreviewWarmer(self.cache,'unused',autostart=False)
        worker.enqueue(self.photos,[old]);original=media._preview
        def replaced(*args,**kwargs):
            worker.enqueue(self.photos,[new]);return original(*args,**kwargs)
        with patch.object(media,'_preview',side_effect=replaced):worker.process_one()
        self.assertEqual(self.cache.state()['pending'],1);worker.process_one();worker.process_one();self.assertEqual(self.cache.state()['pending'],0)


class PreviewLockTests(unittest.TestCase):
    def test_different_previews_do_not_share_lock_stripes(self):
        # 两个 key 的前缀相同，旧的 64 条带锁会让第二个预览阻塞。
        entered=threading.Event()
        def second():
            with media._preview_lock('00000001'+'b'*56):entered.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with media._preview_lock('00000001'+'a'*56):
                future=pool.submit(second)
                self.assertTrue(entered.wait(3))
            future.result()

    def test_unused_per_preview_locks_are_released(self):
        for number in range(1000):
            key=f'unused-test-{number}'
            with media._preview_lock(key):self.assertIn(key,media._PREVIEW_LOCKS)
            self.assertNotIn(key,media._PREVIEW_LOCKS)


if __name__=='__main__':unittest.main()
