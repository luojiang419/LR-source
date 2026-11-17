"""客户机旧占用、长时占用、跨客户端接力和改款真实落盘回归。"""
import ctypes
import hashlib
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
import test_shooting_background_write as live
import test_portable_capture_store as portable
import test_archive_capture_service as capture
from test_manual_metadata_backfill import windows_handle


class QueueRecoveryTests(unittest.TestCase):
    setUp=live.BackgroundRowWriteTests.setUp
    tearDown=live.BackgroundRowWriteTests.tearDown
    prepare=live.BackgroundRowWriteTests.prepare
    rate=live.BackgroundRowWriteTests.rate
    choose=live.BackgroundRowWriteTests.choose
    drain=live.BackgroundRowWriteTests.drain
    ready=live.BackgroundRowWriteTests.ready
    rename=live.BackgroundRowWriteTests.rename

    def test_busy_photo_yields_and_latest_manual_rename_wins(self):
        self.ready();self.rename();held=threading.Event();release=threading.Event()
        path=self.photos/'0.JPG'
        with Image.open(path) as im:pixels=hashlib.sha256(im.tobytes()).hexdigest()
        def hold():
            with self.archive.writer.file_lock(path):held.set();release.wait(10)
        thread=threading.Thread(target=hold);thread.start()
        try:
            self.assertTrue(held.wait(2));started=time.monotonic();self.archive.process_one()
            self.assertLess(time.monotonic()-started,1)
            self.archive.process_one()
            self.assertEqual(self.archive.writer.read(self.photos/'1.JPG')['XMP-fk:SKU'],'NEW-200')
            self.rename('NEW-200','FINAL-300')
            self.archive.process_one();self.archive.process_one()
            self.assertEqual(self.archive.writer.read(self.photos/'1.JPG')['XMP-fk:SKU'],'FINAL-300')
        finally:release.set();thread.join(3)
        with self.archive.connect() as c:c.execute("UPDATE metadata_jobs SET retry_at=0 WHERE status='queued'")
        self.drain()
        self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'FINAL-300')
        self.assertEqual(self.sku.state()['rows'][0]['metadata']['status'],'applied')
        with Image.open(path) as im:self.assertEqual(hashlib.sha256(im.tobytes()).hexdigest(),pixels)
        self.assertEqual(self.archive.writer.path_locks,{})

    def test_real_windows_lock_beyond_old_limit_recovers_automatically(self):
        self.ready();self.rename();path=self.photos/'0.JPG'
        with self.archive.connect() as c:c.execute("UPDATE metadata_jobs SET retry_count=50 WHERE path=? AND status='queued'",(str(path),))
        with windows_handle(path):
            self.archive.process_one()  # 新任务优先，已重试 50 次的 0.JPG 排在其后
            with self.archive.connect() as c:self.assertEqual(c.execute("SELECT retry_count FROM metadata_jobs WHERE path=? AND status='queued'",(str(path),)).fetchone()[0],50)
            self.archive.process_one()
            with self.archive.connect() as c:job=dict(c.execute("SELECT * FROM metadata_jobs WHERE path=? AND status='queued'",(str(path),)).fetchone())
            self.assertEqual(job['retry_count'],51);self.assertGreater(job['retry_at'],time.time())
            self.assertEqual(self.archive.writer.read(self.photos/'1.JPG')['XMP-fk:SKU'],'NEW-200')
        with self.archive.connect() as c:c.execute('UPDATE metadata_jobs SET retry_at=0 WHERE id=?',(job['id'],))
        self.archive.start_metadata()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with self.archive.connect() as c:status=c.execute('SELECT status FROM metadata_jobs WHERE id=?',(job['id'],)).fetchone()[0]
            if status=='applied':break
            time.sleep(.03)
        self.assertEqual(status,'applied');self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'NEW-200')

    def test_orphan_running_resumes_without_restart_or_duplicate_commit(self):
        self.ready();self.rename()
        with self.archive.connect() as c:job=dict(c.execute("SELECT * FROM metadata_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone())
        payload,target,journal=self.archive.writer.write(job['path'],json.loads(job['fields']),job['expected_sha'])
        self.archive.writer.commit(payload,target,journal)
        with self.archive.connect() as c:c.execute("UPDATE metadata_jobs SET status='running',journal=? WHERE id=?",(json.dumps(journal),job['id']))
        with patch.object(self.archive.writer,'write',side_effect=AssertionError('不应重复生成已提交原片')):
            self.archive.process_one()
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT status FROM metadata_jobs WHERE id=?',(job['id'],)).fetchone()[0],'applied')
        self.drain();self.assertEqual(self.sku.state()['rows'][0]['metadata']['status'],'applied')

    def test_archive_lock_wait_releases_photo_and_retries(self):
        self.ready();self.rename();held=threading.Event();release=threading.Event()
        def hold():
            with self.archive.lock:held.set();release.wait(10)
        thread=threading.Thread(target=hold);thread.start()
        try:
            self.assertTrue(held.wait(2));self.archive.process_one()
            with self.archive.connect() as c:
                job=dict(c.execute("SELECT * FROM metadata_jobs WHERE status='queued' AND retry_count>0").fetchone())
            self.assertIn('归档处理尚未收尾',job['reason'])
            with self.archive.writer.file_lock(job['path'],timeout=.1):pass
        finally:release.set();thread.join(3)
        with self.archive.connect() as c:c.execute("UPDATE metadata_jobs SET retry_at=0 WHERE status='queued'")
        self.drain();self.assertEqual(self.sku.state()['rows'][0]['metadata']['status'],'applied')

    def test_retried_old_jobs_cannot_starve_fresh_jobs(self):
        """客户现场：大量反复重试的旧任务按创建时间排在队头，新确认的照片一直 0 进度。"""
        self.ready();self.rename();old=self.photos/'0.JPG';fresh=self.photos/'1.JPG'
        with self.archive.connect() as c:
            c.execute("UPDATE metadata_jobs SET created=1,retry_count=9,retry_at=0 WHERE path=? AND status='queued'",(str(old),))
            c.execute("UPDATE metadata_jobs SET created=2,fields=json_set(fields,'$._catalog_ocr',json('{}')) WHERE path=? AND status='queued'",(str(fresh),))
            guards='';params=[time.time()]
            # 归档 OCR 补写最迟，旧的重试任务排在全部新任务之后。
            self.assertEqual(self.archive._next_metadata_job(c,guards,params)['path'],str(fresh))
            c.execute("UPDATE metadata_jobs SET fields=json_remove(fields,'$._catalog_ocr') WHERE path=?",(str(fresh),))
            self.assertEqual(self.archive._next_metadata_job(c,guards,params)['path'],str(fresh))
            c.execute("UPDATE metadata_jobs SET status='cancelled' WHERE path=? AND status='queued'",(str(fresh),))
            self.assertEqual(self.archive._next_metadata_job(c,guards,params)['path'],str(old))
            c.execute("UPDATE metadata_jobs SET status='queued' WHERE path=? AND status='cancelled'",(str(fresh),))
        self.archive.process_one()
        self.assertEqual(self.archive.writer.read(fresh)['XMP-fk:SKU'],'NEW-200')
        with self.archive.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM metadata_jobs WHERE path=? AND retry_count=9",(str(old),)).fetchone()[0],'queued')

    def test_live_shooting_writes_only_current_session_and_pauses_old_work(self):
        """拍摄中只写当前会话；旧照片任务、归档 OCR 补写和补齐扫描全部让路。"""
        self.ready();self.rename()
        with self.archive.connect() as c:
            sessions=[r[0] for r in c.execute('SELECT DISTINCT session FROM catalog_links')]
            self.assertEqual(len(sessions),1);live=sessions[0]
            old=c.execute("SELECT asset FROM metadata_jobs WHERE path=? AND status='queued'",(str(self.photos/'0.JPG'),)).fetchone()[0]
            # 把 0.JPG 所属资产改挂到另一个旧会话，1.JPG 仍属当前拍摄。
            c.execute('UPDATE catalog_links SET session=? WHERE asset=?',('old-session',old))
            c.execute("UPDATE metadata_jobs SET created=1 WHERE asset=? AND status='queued'",(old,))
        self.archive.live_session=lambda:live
        self.archive.process_one()
        self.assertEqual(self.archive.writer.read(self.photos/'1.JPG')['XMP-fk:SKU'],'NEW-200')
        self.archive.process_one()
        with self.archive.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM metadata_jobs WHERE asset=? AND status='queued'",(old,)).fetchone()[0],1)
        self.assertEqual(self.archive.metadata_queue_state()['phase'],'idle')
        self.assertEqual(self.archive.metadata_queue_state()['live_session'],live)
        class Catalog:
            ticks=0
            def tick(self):self.ticks+=1
        self.archive.catalog_writeback=catalog=Catalog()
        self.archive.tick()
        self.assertEqual(catalog.ticks,0)
        self.archive.live_session=lambda:None
        self.archive.tick(process_metadata=False)
        self.assertEqual(catalog.ticks,1)
        self.archive.process_one()
        self.assertEqual(self.archive.writer.read(self.photos/'0.JPG')['XMP-fk:SKU'],'NEW-200')

    def test_backfill_runs_only_after_manual_request_and_waits_for_shooting_end(self):
        from metadata_backfill import enqueue_missing
        self.ready();self.rename()
        with self.archive.connect() as c:c.execute('DELETE FROM metadata_jobs')
        self.archive.start_metadata();time.sleep(1.2)
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM metadata_jobs').fetchone()[0],0)
        live=[object()]
        self.archive.live_session=lambda:'x' if live else None
        self.archive.request_backfill(str(self.photos))
        time.sleep(1.2)
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM metadata_jobs').fetchone()[0],0)
        self.assertEqual(self.archive.backfill_progress['phase'],'paused_live')
        live.clear()
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            with self.archive.connect() as c:count=c.execute('SELECT count(*) FROM metadata_jobs').fetchone()[0]
            if count:break
            time.sleep(.1)
        self.assertEqual(count,2)
        # 选择范围之外的目录不补。
        with self.archive.connect() as c:c.execute('DELETE FROM metadata_jobs')
        self.assertEqual(enqueue_missing(self.archive,prefix=str(self.base/'elsewhere')),0)
        self.assertEqual(enqueue_missing(self.archive,prefix=str(self.photos)),2)

    def test_archive_lock_contention_retries_quickly_and_reports_queue(self):
        from file_safety import ArchiveBusyError,metadata_retry_delay
        self.assertLessEqual(metadata_retry_delay(ArchiveBusyError('x'),50),5)
        self.assertEqual(metadata_retry_delay(TimeoutError('x'),50),None)
        self.ready();self.rename();held=threading.Event();release=threading.Event()
        def hold():
            with self.archive.lock:held.set();release.wait(10)
        thread=threading.Thread(target=hold);thread.start()
        try:
            self.assertTrue(held.wait(2));self.archive.process_one()
            with self.archive.connect() as c:job=dict(c.execute("SELECT * FROM metadata_jobs WHERE status='queued' AND retry_count>0").fetchone())
            self.assertLessEqual(job['retry_at']-time.time(),2)
            diagnostics=self.archive.metadata_queue_diagnostics()
            self.assertEqual(diagnostics['queue']['retrying'],1)
            self.assertTrue(any(t['name']=='MainThread' or t['stack'] for t in diagnostics['threads']))
        finally:release.set();thread.join(3)
        with self.archive.connect() as c:c.execute("UPDATE metadata_jobs SET retry_at=0 WHERE status='queued'")
        self.drain();self.assertEqual(self.sku.state()['rows'][0]['metadata']['status'],'applied')

    def test_persisted_writes_prioritize_restore_without_browsing_that_root(self):
        from types import SimpleNamespace
        from archive_capture_service import ArchiveCaptureService
        self.ready();self.rename()
        service=SimpleNamespace(archive=self.archive,restore_roots=[str(self.base/'offline'),str(self.photos)])
        self.assertEqual(ArchiveCaptureService._pending_write_restores(service),[str(self.photos)])

    def test_browser_rename_recovers_real_lock_and_clears_waiting_hint(self):
        from test_sku_input_confirm import InputConfirmTests
        from playwright.sync_api import sync_playwright,expect
        self.ready();errors=[];path=self.photos/'0.JPG'
        out=Path(__file__).resolve().parents[1]/'build/task228-metadata-recovery';out.mkdir(parents=True,exist_ok=True)
        with InputConfirmTests.http(self) as url,sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1600,'height':1000});page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading');page.locator('#sku-open').click()
                with windows_handle(path):
                    page.locator('[data-edit-sku]').first.dblclick();page.locator('.sku-code-input').fill('NEW-200');page.locator('.sku-code-input').press('Enter')
                    expect(page.locator('[data-edit-sku]').first).to_have_text('NEW-200')
                    self.archive.start_metadata()
                    expect(page.locator('#sku-rows .sku-metadata-status')).to_contain_text('自动重试',timeout=10000)
                    page.screenshot(path=str(out/'busy-retry.png'))
                expect(page.locator('#sku-rows .sku-metadata-status')).to_have_count(0,timeout=10000)
                self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'NEW-200')
                page.screenshot(path=str(out/'complete.png'));self.assertFalse(errors,errors)
            finally:browser.close()


class OwnershipRecoveryTests(unittest.TestCase):
    setUp=portable.PortableStoreTests.setUp
    tearDown=portable.PortableStoreTests.tearDown
    record=portable.PortableStoreTests.record

    def test_invalid_own_handle_reconnects_and_refreshes_before_write(self):
        self.a.acquire();record=self.record();self.a.change([record]);session=self.a.ownership.session
        ctypes.WinDLL('kernel32',use_last_error=True).CloseHandle(ctypes.c_void_p(self.a.ownership.handle))
        self.a.ensure_ownership()
        self.assertNotEqual(self.a.ownership.session,session)
        self.a.change([{**record,'sku':'NEW-200'}]);self.b.refresh()
        self.assertEqual(self.b.records()[0]['sku'],'NEW-200')

    def test_reconnect_does_not_steal_other_client_and_refreshes_after_handoff(self):
        from portable_capture_store import DatasetBusy
        self.a.acquire();record=self.record();self.a.change([record]);self.a.ownership.release()
        self.b.acquire();self.b.change([{**record,'sku':'CLIENT-B'}])
        with self.assertRaises(DatasetBusy):self.a.ensure_ownership()
        self.assertFalse(self.a.editing);self.assertTrue(self.b.editing)
        self.b.release();self.a.ensure_ownership()
        self.assertEqual(self.a.records()[0]['sku'],'CLIENT-B')
        self.a.change([{**self.a.records()[0],'sku':'CLIENT-A'}]);self.b.refresh()
        self.assertEqual(self.b.records()[0]['sku'],'CLIENT-A')

    def test_readonly_or_released_session_does_not_auto_acquire(self):
        from portable_capture_store import DatasetBusy
        with self.assertRaises(DatasetBusy):self.a.ensure_ownership()
        self.a.acquire();self.a.release()
        with self.assertRaises(DatasetBusy):self.a.ensure_ownership()
        self.assertFalse(self.a.editing)


class CaptureRecoveryTests(unittest.TestCase):
    setUp=capture.ArchiveCaptureTests.setUp
    tearDown=capture.ArchiveCaptureTests.tearDown
    client=capture.ArchiveCaptureTests.client
    item=capture.ArchiveCaptureTests.item
    drain=capture.ArchiveCaptureTests.drain

    def test_manual_confirm_over_old_lock_limit_writes_after_release(self):
        self.api.confirm(self.item(1),'手动 00123',whole_group=False)
        with self.dataset.connect() as c:
            c.execute("UPDATE tasks SET data=json_set(data,'$._retry_count',10) WHERE kind='metadata' AND status='queued'")
        path=self.photos/'IMG_0001.JPG'
        with windows_handle(path):
            self.api.tick()
            with self.dataset.connect() as c:task=dict(c.execute("SELECT * FROM tasks WHERE kind='metadata' AND status='queued'").fetchone())
            self.assertEqual(json.loads(task['data'])['_retry_count'],11)
        with self.dataset.connect() as c:c.execute("UPDATE tasks SET data=json_remove(data,'$._retry_at') WHERE status='queued'")
        self.drain()
        self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'手动 00123')

    def test_dispatch_reconnects_lost_handle_without_holding_up_other_datasets(self):
        self.api.confirm(self.item(1),'RECONNECT-200',whole_group=False)
        self.dataset.ownership.release()
        self.api.tick();self.drain()
        self.assertTrue(self.dataset.editing)
        self.assertEqual(self.archive.writer.read(self.photos/'IMG_0001.JPG')['XMP-fk:SKU'],'RECONNECT-200')

    def test_failed_executor_receipt_requeues_orphan_task(self):
        from concurrent.futures import Future
        self.api.confirm(self.item(1),'RECEIPT-200',whole_group=False)
        task,key=self.api._claim_task(self.dataset,True,metadata_only=True)
        future=Future();future.set_exception(RuntimeError('模拟执行回执中断'))
        self.api.metadata_inflight[future]=(self.dataset,task,key);self.api.metadata_active_keys.add(key)
        self.api._reap_metadata()
        with self.dataset.connect() as c:self.assertEqual(c.execute('SELECT status FROM tasks WHERE id=?',(task['id'],)).fetchone()[0],'queued')
        self.assertFalse(self.api.metadata_inflight);self.assertFalse(self.api.metadata_active_keys)
        self.drain();self.assertEqual(self.archive.writer.read(self.photos/'IMG_0001.JPG')['XMP-fk:SKU'],'RECEIPT-200')

    def test_reconnect_cancels_old_manual_task_before_overwriting_new_client_choice(self):
        import uuid
        from portable_capture_store import Dataset
        self.api.confirm(self.item(1),'CLIENT-A',whole_group=False)
        with self.dataset.connect() as c:task=dict(c.execute("SELECT * FROM tasks WHERE kind='metadata' AND status='queued'").fetchone())
        path=self.photos/'IMG_0001.JPG';before=path.read_bytes()
        self.dataset.ownership.release()
        other=Dataset(self.photos,self.base/'other-client')
        try:
            other.acquire();record=other.record(file_id=task['record'])
            other.change([{**record,'sku':'CLIENT-B','decision_id':uuid.uuid4().hex}]);other.release()
            self.assertEqual(self.api._metadata_task(self.dataset,task),'cancelled')
            self.assertEqual(self.dataset.record(file_id=task['record'])['sku'],'CLIENT-B')
            self.assertEqual(path.read_bytes(),before)
        finally:other.close()


if __name__=='__main__':unittest.main()
