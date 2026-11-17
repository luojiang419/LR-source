"""现场已完成图不等待 SKU/归档锁；队列启动、领取与状态可观察。"""
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from folder_watch import FolderWatch
from test_shooting_background_write import BackgroundRowWriteTests


class LiveArrivalQueueTests(unittest.TestCase):
    setUp=BackgroundRowWriteTests.setUp
    tearDown=BackgroundRowWriteTests.tearDown
    prepare=BackgroundRowWriteTests.prepare
    rate=BackgroundRowWriteTests.rate
    choose=BackgroundRowWriteTests.choose
    drain=BackgroundRowWriteTests.drain
    ready=BackgroundRowWriteTests.ready
    rename=BackgroundRowWriteTests.rename

    def test_new_jpeg_is_indexed_and_acknowledged_while_sku_and_archive_are_busy(self):
        watch=FolderWatch(self.store,autostart=False);watch.on_arrivals=self.sku.live_arrivals
        held=threading.Event();release=threading.Event();done=threading.Event();errors=[]
        def busy():
            with self.sku.lock,self.archive.lock:
                held.set();release.wait(8)
        worker=threading.Thread(target=busy);writer=None
        try:
            watch.start(str(self.photos));watch.tick()
            worker.start();self.assertTrue(held.wait(2))
            photo=self.photos/'fresh.JPG';Image.new('RGB',(300,200),'green').save(photo)
            def ingest():
                try:watch.tick(paths=[photo])
                except Exception as exc:errors.append(exc)
                finally:done.set()
            writer=threading.Thread(target=ingest);writer.start()
            self.assertTrue(done.wait(2),'新图片入库仍等待 SKU/归档业务锁')
            self.assertFalse(errors,errors)
            record=next(r for r in self.store.records(self.sid) if r.get('jpg')=='fresh.JPG')
            self.assertEqual(watch.state()['last_photo_id'],record['id'])
            self.assertIsNone(watch.direct.state()['receiving'])
            self.assertIsNotNone(self.sku.shooting_context(self.sid,record['id']))
            with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shooting_stream_queue').fetchone()[0],1)
            self.assertFalse(release.is_set())
        finally:
            release.set();worker.join(2)
            if writer:writer.join(3)
            watch.close()
        self.sku.sync_shooting_streams()
        with self.archive.connect() as c:self.assertTrue(c.execute('SELECT 1 FROM bindings WHERE capture=?',(record['id'],)).fetchone())

    def test_pending_file_consumer_runs_without_releasing_deferred_startup_tasks(self):
        from startup_tasks import StartupTasks
        self.ready();self.rename()
        blocked=threading.Event();release=threading.Event()
        tasks=StartupTasks([('slow-index',lambda:(blocked.set(),release.wait(8))),('archive',self.archive.thread.start)],fallback_seconds=0)
        try:
            tasks.start();self.assertTrue(blocked.wait(2));self.archive.start_metadata()
            deadline=time.monotonic()+6
            while time.monotonic()<deadline:
                row=self.sku.state()['rows'][0]
                if row['metadata']['status']=='applied':break
                time.sleep(.03)
            else:self.fail(str(row['metadata']))
            self.assertFalse(self.archive.thread.is_alive())
            self.assertEqual(self.archive.writer.read(self.photos/'0.JPG')['XMP-fk:SKU'],'NEW-200')
            self.archive.start_metadata();self.assertTrue(self.archive.metadata_queue_state()['alive'])
        finally:release.set();tasks.close()

    def test_claim_is_visible_even_when_commit_waits_for_archive_lock(self):
        self.ready();self.rename();held=threading.Event();release=threading.Event()
        def busy():
            with self.archive.lock:held.set();release.wait(8)
        busy_thread=threading.Thread(target=busy);busy_thread.start()
        try:
            self.assertTrue(held.wait(2));self.archive.start_metadata();deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                state=self.archive.metadata_queue_state()
                if state['phase']=='waiting_archive':break
                time.sleep(.03)
            else:self.fail(str(state))
            with self.archive.connect() as c:self.assertTrue(c.execute("SELECT 1 FROM metadata_jobs WHERE status='running'").fetchone())
            metadata=self.archive.metadata_statuses([(self.sid,self.records[1]['id'])])[(self.sid,self.records[1]['id'])]
            self.assertIn('等待归档',metadata['worker_message'])
        finally:release.set();busy_thread.join(2)
        self.wait_complete()

    def wait_complete(self):
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            with self.archive.connect() as c:pending=c.execute("SELECT 1 FROM metadata_jobs WHERE status IN ('queued','running')").fetchone()
            if not pending:return
            time.sleep(.03)
        self.fail('后台队列没有完成')

    def test_rating_writer_does_not_hold_archive_business_lock_during_file_io(self):
        self.ready();entered=threading.Event();release=threading.Event();original=self.archive.writer.write_rating
        self.archive.ratings.set([str(self.photos/'0.JPG')],3)
        def slow(*args,**kwargs):entered.set();release.wait(5);return original(*args,**kwargs)
        with patch.object(self.archive.writer,'write_rating',side_effect=slow):
            worker=threading.Thread(target=self.archive.ratings.sync.process_one);worker.start()
            try:
                self.assertTrue(entered.wait(2));acquired=self.archive.lock.acquire(timeout=.5)
                if acquired:self.archive.lock.release()
                self.assertTrue(acquired,'评级原片写入占用归档业务锁')
            finally:release.set();worker.join(5)

    def test_watcher_observing_our_write_before_refresh_does_not_lose_completion(self):
        self.ready();self.rename();original=self.archive.writer.commit
        with self.sku.connect() as c:before_jobs=c.execute('SELECT count(*) FROM jobs').fetchone()[0]
        def commit_then_watch(payload,target,journal):
            original(payload,target,journal)
            # 模拟目录通知比 worker 的 _refresh_catalog 更快刷新 captures。
            import media_engine as media
            record=next(dict(r) for r in self.store.records(self.sid) if r.get('jpg')==Path(target).name)
            media._STATS[str(target)]=Path(target).stat()
            record['fingerprint']=media.fingerprint([Path(target)])
            self.store.append_live(self.sid,[record])
            if record['id']==self.records[1]['id']:
                result=self.sku.enqueue(self.sid,record['id'],trigger='auto')
                self.assertFalse(result['queued'])
                self.assertEqual(self.sku.state()['progress'][0]['shot'],1)
        with patch.object(self.archive.writer,'commit',side_effect=commit_then_watch):
            self.archive.process_one();self.archive.process_one()
        state=self.sku.state();self.assertEqual(state['progress'][0]['shot'],1)
        self.assertEqual(state['rows'][0]['metadata']['status'],'applied')
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],before_jobs)

    def test_legacy_confirmed_files_without_any_write_jobs_are_automatically_backfilled(self):
        self.pid=self.prepare()['id'];self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'LEGACY-100')
        with self.archive.connect() as c:c.execute('DELETE FROM metadata_jobs')
        self.assertEqual(self.sku.state()['progress'][0]['shot'],1)
        self.assertEqual(self.sku.state()['rows'][0]['metadata']['status'],'missing')
        self.archive.start_metadata();self.wait_complete()
        time.sleep(.8)
        self.assertEqual(self.sku.state()['rows'][0]['metadata']['status'],'missing','旧记录不得后台自动补写')
        self.assertIn('资产归档',self.sku.state()['rows'][0]['metadata']['worker_message'])
        self.archive.request_backfill()
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            state=self.sku.state()
            if state['rows'][0]['metadata']['status']=='applied':break
            self.assertEqual(state['progress'][0]['shot'],1)
            time.sleep(.03)
        else:self.fail(str(state['rows']))
        self.assertEqual(self.archive.writer.read(self.photos/'0.JPG')['XMP-fk:SKU'],'LEGACY-100')
        self.assertEqual(self.archive.metadata_queue_state()['backfilled_jobs'],2)

    def test_backfill_keeps_cancelled_jobs_and_unconfirmed_embedded_assets_untouched(self):
        from metadata_backfill import enqueue_missing
        self.pid=self.prepare()['id'];self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'LEGACY-100')
        with self.archive.connect() as c:
            c.execute("UPDATE metadata_jobs SET status='cancelled'")
            c.execute("INSERT INTO assets(id,created,sku,source,revision) VALUES ('unknown',0,'UNKNOWN','embedded',1)")
        self.assertEqual(enqueue_missing(self.archive),0)


if __name__=='__main__':unittest.main()
