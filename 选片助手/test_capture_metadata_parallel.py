"""并行补写的实际JPG回读、版本冲突、故障隔离、互斥及关闭收尾。"""
import json
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from PIL import Image
from types import SimpleNamespace
from pathlib import Path
import test_manual_metadata_backfill as fixture


class ParallelMetadataTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ManualBackfillTests();self.f.setUp();f=self.f;f.api.metadata_workers=4
        self.pixels={}
        for n in range(1,5):
            f.api.confirm(f.item(n),f'PARALLEL-{n}',whole_group=False)
            with Image.open(f.photos/f'IMG_{n:04d}.JPG') as im:self.pixels[n]=im.tobytes()

    def tearDown(self):self.f.tearDown()

    def pump(self,*,failed=False,timeout=30):
        f=self.f;end=time.monotonic()+timeout
        while time.monotonic()<end:
            f.api.tick(parallel_metadata=True)
            with f.dataset.connect() as c:
                pending=c.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running') LIMIT 1").fetchone()
                failures=[dict(r) for r in c.execute("SELECT * FROM tasks WHERE status='failed'")]
            if not pending and not f.api.metadata_inflight:
                if not failed:self.assertEqual(failures,[])
                return
            time.sleep(.005)
        self.fail('并发写入未在时限内结束')

    def assert_files(self,except_n=()):
        f=self.f
        for n in range(1,5):
            if n in except_n:continue
            path=f.photos/f'IMG_{n:04d}.JPG'
            self.assertEqual(f.archive.writer.read(path)['XMP-fk:SKU'],f'PARALLEL-{n}')
            with Image.open(path) as im:self.assertEqual(im.tobytes(),self.pixels[n])

    def test_four_files_are_actually_in_flight_and_pixels_and_skus_verify(self):
        f=self.f;started=threading.Event();release=threading.Event();lock=threading.Lock();active=0;peak=0;original=f.api._metadata_task
        def blocked(dataset,task):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active)
                if active==4:started.set()
            try:release.wait(8);return original(dataset,task)
            finally:
                with lock:active-=1
        with patch.object(f.api,'_metadata_task',side_effect=blocked):
            try:
                t=time.monotonic();f.api.tick(parallel_metadata=True)
                self.assertLess(time.monotonic()-t,1);self.assertTrue(started.wait(3))
                self.assertEqual(f.api.state(f.rid)['metadata_tasks']['running'],4)
                with self.assertRaisesRegex(ValueError,'正在写入'):f.api.edit(f.rid,enabled=False)
                with self.assertRaisesRegex(ValueError,'仍在写入'):
                    with f.api.freeze_archive([str(f.photos)],'parallel-test'):pass
            finally:release.set()
            self.pump()
        self.assertEqual(peak,4);self.assert_files()
        self.assertEqual(f.api.write_metadata(f.rid)['queued'],0)

    def test_duplicate_tasks_for_one_file_never_overlap(self):
        f=self.f;record=f.dataset.record(path='IMG_0001.JPG')
        with f.dataset.connect() as c:c.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,'metadata_sidecar',record['id'],record['decision_id'],'queued','','{}'))
        active=set();lock=threading.Lock();overlaps=[];original=f.api._metadata_task
        def track(dataset,task):
            with lock:
                if task['record'] in active:overlaps.append(task['record'])
                active.add(task['record'])
            try:time.sleep(.05);return original(dataset,task)
            finally:
                with lock:active.remove(task['record'])
        with patch.object(f.api,'_metadata_task',side_effect=track):self.pump()
        self.assertEqual(overlaps,[]);self.assert_files()

    def test_one_failure_does_not_stop_other_files_and_can_retry(self):
        f=self.f;record=f.dataset.record(path='IMG_0001.JPG');original=f.api._metadata_task
        def fail_one(dataset,task):
            if task['record']==record['id']:raise ValueError('模拟单图写入失败')
            return original(dataset,task)
        with patch.object(f.api,'_metadata_task',side_effect=fail_one):self.pump(failed=True)
        self.assert_files(except_n=(1,));self.assertEqual(f.api.state(f.rid)['metadata_tasks']['failed'],1)
        self.assertEqual(f.api.write_metadata(f.rid)['retried'],1);self.pump();self.assert_files()

    def test_new_confirmation_cancels_prepared_old_write(self):
        f=self.f;path=f.photos/'IMG_0001.JPG';ready=threading.Event();release=threading.Event();original=f.archive.writer.write;held=False
        def prepare(*args,**kwargs):
            nonlocal held
            result=original(*args,**kwargs)
            if args[0]==path and not held:
                held=True;ready.set();release.wait(8)
            return result
        with patch.object(f.archive.writer,'write',side_effect=prepare):
            try:
                f.api.tick(parallel_metadata=True);self.assertTrue(ready.wait(4))
                f.api.confirm(f.item(1),'LATEST-VERSION',whole_group=False)
            finally:release.set()
            self.pump()
        self.assertEqual(f.archive.writer.read(path)['XMP-fk:SKU'],'LATEST-VERSION')
        self.assert_files(except_n=(1,))

    def test_close_waits_for_accepted_files_before_releasing_ownership(self):
        f=self.f;started=threading.Event();release=threading.Event();closed=threading.Event();original=f.api._metadata_task
        def blocked(*args):started.set();release.wait(8);return original(*args)
        with patch.object(f.api,'_metadata_task',side_effect=blocked):
            f.api.tick(parallel_metadata=True);self.assertTrue(started.wait(2))
            thread=threading.Thread(target=lambda:(f.api.close(),closed.set()));thread.start()
            try:self.assertFalse(closed.wait(.1));self.assertTrue(f.dataset.editing)
            finally:release.set();thread.join(12)
        self.assertTrue(closed.is_set());self.assertFalse(f.dataset.editing);self.assert_files()
        with f.dataset.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0],0)

    def test_unclaimed_queue_survives_restart_without_rewriting_finished_files(self):
        from archive_capture_service import ArchiveCaptureService
        f=self.f;f.api.metadata_workers=2;started=threading.Event();release=threading.Event();original=f.api._metadata_task
        def blocked(*args):started.set();release.wait(8);return original(*args)
        with patch.object(f.api,'_metadata_task',side_effect=blocked):
            f.api.tick(parallel_metadata=True);self.assertTrue(started.wait(2))
            thread=threading.Thread(target=f.api.close);thread.start();release.set();thread.join(12)
        with f.dataset.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE status='queued'").fetchone()[0],2)
        completed={p:p.read_bytes() for p in f.photos.glob('*.JPG') if f.archive.writer.read(p).get('XMP-fk:SKU')}
        fresh=ArchiveCaptureService(f.store,f.archive,f.catalog,f.sku,autostart=False,metadata_workers=4)
        try:
            f.api=fresh;f.dataset,_=fresh.scope(f.rid);f.dataset.acquire();fresh._recover_tasks(f.dataset)
            self.pump();self.assert_files()
            self.assertEqual(completed,{p:p.read_bytes() for p in completed})
        finally:fresh.close()

    def test_status_does_not_probe_unrelated_network_datasets(self):
        class UnreachableFolder:
            def __truediv__(self,_):raise AssertionError('不应探测无关NAS目录')
        f=self.f;unrelated=SimpleNamespace(root=Path(r'\\offline-status-probe\unrelated'),folder=UnreachableFolder())
        f.api.datasets[str(unrelated.root)]=unrelated
        try:
            state=f.api.state(f.rid)
            self.assertEqual(state['total'],4)
        finally:f.api.datasets.pop(str(unrelated.root),None)

    def test_slow_directory_scan_does_not_stop_refilling_write_slots(self):
        f=self.f;f.api.metadata_workers=2;scanning=threading.Event();release=threading.Event()
        def slow_scan(*args,**kwargs):scanning.set();release.wait(8);return {}
        with patch.object(f.api,'_prefetch_scan',side_effect=slow_scan):
            f.api.pending[str(f.dataset.root)]=True;f.api.thread.start()
            try:
                self.assertTrue(scanning.wait(3));end=time.monotonic()+6
                while time.monotonic()<end:
                    with f.dataset.connect() as c:done=c.execute("SELECT count(*) FROM tasks WHERE status='complete'").fetchone()[0]
                    if done==4:break
                    time.sleep(.01)
                self.assertEqual(done,4);self.assertFalse(release.is_set());self.assert_files()
            finally:release.set()

    def test_ocr_and_write_do_not_overlap_on_one_photo_but_other_files_continue(self):
        f=self.f;record=f.dataset.record(path='IMG_0001.JPG');token=uuid.uuid4().hex
        f.dataset.change([{**record,'ocr_token':token}],kind='ocr_read',tasks=[{'kind':'ocr_read','record':record['id'],'revision':token}])
        reading=threading.Event();release=threading.Event()
        def slow_ocr(*args):reading.set();release.wait(8);return 'complete'
        with patch.object(f.api,'_ocr_task',side_effect=slow_ocr):
            f.api.thread.start()
            try:
                self.assertTrue(reading.wait(3))
                current=f.dataset.record(path='IMG_0001.JPG')
                f.dataset.change([{**current,'ocr_token':uuid.uuid4().hex}],kind='ocr_read')
                end=time.monotonic()+5
                while time.monotonic()<end:
                    with f.dataset.connect() as c:done=c.execute("SELECT count(*) FROM tasks WHERE kind='metadata' AND status='complete'").fetchone()[0]
                    if done==3:break
                    time.sleep(.01)
                self.assertEqual(done,3)
                with f.dataset.connect() as c:
                    self.assertEqual(c.execute("SELECT status FROM tasks WHERE kind='metadata' AND record=?",(record['id'],)).fetchone()[0],'queued')
            finally:release.set()
            end=time.monotonic()+8
            while time.monotonic()<end:
                with f.dataset.connect() as c:left=c.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running')").fetchone()[0]
                if not left:break
                time.sleep(.01)
            self.assertEqual(left,0);self.assert_files()

    def test_large_queue_uses_indexed_claims_and_ocr_lookup(self):
        f=self.f
        with f.dataset.connect() as c:
            c.executemany('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',[(uuid.uuid4().hex,'metadata',uuid.uuid4().hex,uuid.uuid4().hex,'queued','','{}') for _ in range(4000)])
            sql=next(value for value in f.api._claim_task.__code__.co_consts if isinstance(value,str) and 'SELECT t.*' in value)
            plan='\n'.join(row[-1] for row in c.execute('EXPLAIN QUERY PLAN '+sql,(time.time(),0,0,1,1,0)))
        self.assertIn('portable_task_record_state',plan)
        self.assertIn('portable_task_ready_order',plan)
        self.assertNotIn('SCAN o',plan)

    def test_verification_receipts_do_not_requery_large_catalog(self):
        f=self.f;f.api.pending.clear()
        with patch.object(f.api,'_project',side_effect=AssertionError('写入回执不应重查素材库')):self.pump()
        self.assert_files()
        self.assertEqual(f.api.state(f.rid)['metadata_verified'],4)

    def test_distinct_file_commits_overlap_but_confirmation_waits_for_them(self):
        f=self.f;release=threading.Event();all_committing=threading.Event();changed=threading.Event();lock=threading.Lock();active=0;peak=0;original=f.archive.writer.commit
        def commit(*args,**kwargs):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active)
                if active==4:all_committing.set()
            try:release.wait(8);return original(*args,**kwargs)
            finally:
                with lock:active-=1
        def confirm():f.api.confirm(f.item(1),'AFTER-COMMIT',whole_group=False);changed.set()
        with patch.object(f.archive.writer,'commit',side_effect=commit):
            try:
                f.api.tick(parallel_metadata=True);self.assertTrue(all_committing.wait(4))
                thread=threading.Thread(target=confirm);thread.start()
                self.assertFalse(changed.wait(.1))
            finally:release.set()
            thread.join(8);self.assertTrue(changed.is_set());self.pump()
        self.assertEqual(peak,4)
        self.assertEqual(f.archive.writer.read(f.photos/'IMG_0001.JPG')['XMP-fk:SKU'],'AFTER-COMMIT')
        self.assert_files(except_n=(1,))


if __name__=='__main__':unittest.main()
