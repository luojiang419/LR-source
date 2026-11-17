"""手动补写与 Windows 真实占用恢复，全部使用隔离生成照片。"""
from contextlib import contextmanager,ExitStack
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from PIL import Image
import test_archive_capture_service as capture_fixture
import test_shooting_background_write as live_fixture


@contextmanager
def windows_handle(path,access=0x40000000,share=7):
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
    kernel.CreateFileW.restype=W.HANDLE;kernel.CloseHandle.argtypes=[W.HANDLE]
    handle=kernel.CreateFileW(str(path),access,share,None,3,0,None)
    if handle==C.c_void_p(-1).value:raise C.WinError(C.get_last_error())
    try:yield
    finally:kernel.CloseHandle(handle)


class ManualBackfillTests(unittest.TestCase):
    setUp=capture_fixture.ArchiveCaptureTests.setUp
    tearDown=capture_fixture.ArchiveCaptureTests.tearDown
    client=capture_fixture.ArchiveCaptureTests.client
    item=capture_fixture.ArchiveCaptureTests.item
    drain=capture_fixture.ArchiveCaptureTests.drain
    xmp=capture_fixture.ArchiveCaptureTests.xmp
    complete_fields=capture_fixture.ArchiveCaptureTests.complete_fields

    def prepare_missing(self):
        self.api.confirm(self.item(1),'MANUAL-223',whole_group=False)
        self.api.confirm(self.item(2),'MANUAL-224',whole_group=False)
        with self.dataset.connect() as c:c.execute("DELETE FROM tasks WHERE kind IN ('metadata','metadata_fill','metadata_sidecar')")

    def test_missing_and_failed_current_tasks_write_once_and_keep_pixels(self):
        self.prepare_missing();path=self.photos/'IMG_0001.JPG'
        with Image.open(path) as im:before=im.tobytes()
        with patch.object(self.sku,'recognizer',side_effect=AssertionError('补写不得识别')):
            first=self.api.write_metadata(self.rid);second=self.api.write_metadata(self.rid)
            self.assertEqual(first['queued'],2);self.assertEqual(second['queued'],0);self.assertEqual(second['pending'],2)
            with self.dataset.connect() as c:c.execute("UPDATE tasks SET status='failed',reason='旧版占用失败' WHERE record=?",(self.dataset.record(path=path.name)['id'],))
            self.assertEqual(self.api.write_metadata(self.rid)['retried'],1)
            self.drain()
        self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'MANUAL-223')
        self.assertEqual(self.api.state(self.rid)['metadata_verified'],2)
        with Image.open(path) as im:self.assertEqual(im.tobytes(),before)
        files={p:p.read_bytes() for p in self.photos.iterdir() if p.is_file()}
        self.assertEqual(self.api.write_metadata(self.rid)['queued'],0);self.drain()
        self.assertEqual(files,{p:p.read_bytes() for p in files})

    def test_read_interval_restores_missing_sku_without_rewriting_existing_marker(self):
        rows=[self.dataset.record(path=f'IMG_{n:04d}.JPG') for n in (1,2)]
        side=self.xmp(2,self.complete_fields(2,'INTERVAL-223',rating=2,members=rows))
        path=self.photos/'IMG_0002.JPG';before=(path.read_bytes(),side.read_bytes())
        self.api.edit(self.rid,enabled=False);self.api._scan(self.dataset)
        with patch.object(self.sku,'recognizer',side_effect=AssertionError('区间补写不得OCR')):
            result=self.api.write_metadata(self.rid);self.drain()
        self.assertGreater(result['queued']+result['pending'],0)
        self.assertEqual(self.archive.writer.read(self.photos/'IMG_0001.JPG')['XMP-fk:SKU'],'INTERVAL-223')
        self.assertEqual(before,(path.read_bytes(),side.read_bytes()))

    def test_guards_and_conflicts_do_not_write(self):
        self.prepare_missing();self.api.loading[str(self.dataset.root)]=True
        with self.assertRaisesRegex(ValueError,'读取完成'):self.api.write_metadata(self.rid)
        self.api.loading[str(self.dataset.root)]=False
        with patch.object(self.api,'readonly',return_value=True):
            with self.assertRaises(ValueError):self.api.write_metadata(self.rid)
        with self.dataset.connect() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('archive_freeze','test-freeze')")
        with self.assertRaises(ValueError):self.api.write_metadata(self.rid)
        with self.dataset.connect() as c:c.execute("DELETE FROM meta WHERE key='archive_freeze'")
        with self.dataset.connect() as c:
            c.execute("UPDATE records SET data=json_set(data,'$.metadata_conflict','需核对') WHERE id=?",(self.dataset.record(path='IMG_0001.JPG')['id'],))
        result=self.api.write_metadata(self.rid)
        self.assertEqual((result['queued'],result['skipped']),(1,1))

    def test_subfolder_only_enqueues_its_own_missing_photos(self):
        from folder_archive_catalog_sync import synchronize_catalog_paths
        self.prepare_missing();moves=[]
        for n,folder in ((1,'A'),(2,'B')):
            source=self.photos/f'IMG_{n:04d}.JPG';target=self.photos/folder/source.name
            target.parent.mkdir();source.rename(target);moves.append({'before':str(source),'after':str(target)})
        with self.catalog.connect() as c:synchronize_catalog_paths(c,moves)
        self.api.relocate(moves)
        result=self.api.write_metadata(self.rid,'A');self.assertEqual(result['queued'],1)
        with self.dataset.connect() as c:
            records=[r[0] for r in c.execute("SELECT record FROM tasks WHERE status='queued' AND kind='metadata'")]
        self.assertEqual(records,[self.dataset.record(path='A/IMG_0001.JPG')['id']])

    def test_resume_reuses_failed_task_and_clears_failure_count(self):
        self.prepare_missing();self.api.write_metadata(self.rid)
        with self.dataset.connect() as c:
            c.execute("UPDATE tasks SET status='failed' WHERE kind='metadata'")
            before=[r[0] for r in c.execute("SELECT id FROM tasks WHERE kind='metadata'")]
        self.api._recover_tasks(self.dataset)
        with self.dataset.connect() as c:
            after=[r[0] for r in c.execute("SELECT id FROM tasks WHERE kind='metadata'")]
        self.assertEqual(before,after);self.drain()
        self.assertEqual(self.api.state(self.rid)['metadata_tasks']['failed'],0)

    def prepare_selected_folder(self):
        from folder_archive_catalog_sync import synchronize_catalog_paths
        self.prepare_missing()
        self.api.confirm(self.item(3),'OUTSIDE-KEEP',whole_group=False)
        with self.dataset.connect() as c:c.execute('DELETE FROM tasks')
        moves=[]
        for n in range(1,5):
            source=self.photos/f'IMG_{n:04d}.JPG';target=self.photos/('所选拍摄目录' if n<3 else '其它拍摄目录')/source.name
            target.parent.mkdir(exist_ok=True);source.rename(target)
            moves.append({'before':str(source),'after':str(target)})
        with self.catalog.connect() as c:synchronize_catalog_paths(c,moves)
        self.api.relocate(moves)
        self.api.edit(self.rid,enabled=False)
        child=self.photos/'独立拍摄数据集';child.mkdir()
        independent=self.api._dataset(child);independent.acquire();independent.release()
        return '所选拍摄目录'

    def test_selected_folder_under_shared_parent_ignores_independent_sibling(self):
        folder=self.prepare_selected_folder()
        untouched={p:p.read_bytes() for p in self.photos.rglob('*') if p.is_file() and not p.is_relative_to(self.photos/folder) and '.shiying' not in p.parts}
        with self.assertRaisesRegex(ValueError,'独立子数据集'):self.api.edit(self.rid)
        with patch.object(self.sku,'recognizer',side_effect=AssertionError('补写不得OCR')):
            result=self.api.write_metadata(self.rid,folder)
            self.assertEqual(result['queued'],2)
            self.drain()
        for n in (1,2):
            self.assertEqual(self.archive.writer.read(self.photos/folder/f'IMG_{n:04d}.JPG')['XMP-fk:SKU'],f'MANUAL-{222+n}')
        self.assertEqual(untouched,{p:p.read_bytes() for p in untouched})
        self.assertFalse(self.archive.writer.read(self.photos/'其它拍摄目录/IMG_0003.JPG').get('XMP-fk:SKU'))
        self.assertEqual(self.api.write_metadata(self.rid,folder)['queued'],0)

    def test_manual_scope_survives_recovery_without_restoring_outside_tasks(self):
        folder=self.prepare_selected_folder();self.api.write_metadata(self.rid,folder)
        outside=self.dataset.record(path='其它拍摄目录/IMG_0003.JPG')
        self.api._recover_tasks(self.dataset)
        with self.dataset.connect() as c:
            self.assertFalse(c.execute('SELECT 1 FROM tasks WHERE record=?',(outside['id'],)).fetchone())
        self.drain()

    def test_explicit_editor_restores_normal_mode_after_manual_write(self):
        self.prepare_missing();self.api.edit(self.rid,enabled=False)
        self.api.write_metadata(self.rid)
        self.assertEqual(self.dataset.meta('manual_metadata_only'),'1')
        self.api.edit(self.rid)
        self.assertFalse(self.dataset.meta('manual_metadata_only'))
        self.drain()

    def test_request_returns_while_scope_is_slow_and_remains_queryable(self):
        self.prepare_missing();entered=threading.Event();release=threading.Event();original=self.api.scope
        def slow(*args,**kwargs):
            entered.set();release.wait(5);return original(*args,**kwargs)
        self.api.loading[str(self.dataset.root)]=True
        with patch.object(self.api,'scope',side_effect=slow):
            try:
                started=time.monotonic();first=self.api.request_metadata_write(self.rid)
                self.assertLess(time.monotonic()-started,1);self.assertTrue(entered.wait(2))
                second=self.api.request_metadata_write(self.rid)
                self.assertEqual(first['id'],second['id'])
                self.assertIn(self.api.metadata_write_state(self.rid)['status'],('queued','preparing'))
            finally:release.set()

    def test_request_waits_for_scan_then_submits_all_missing_once(self):
        self.prepare_missing();self.api.loading[str(self.dataset.root)]=True
        with patch.object(self.api,'_start_metadata_request_worker'):
            first=self.api.request_metadata_write(self.rid);second=self.api.request_metadata_write(self.rid)
        self.assertEqual(first['id'],second['id']);self.assertFalse(self.api.process_metadata_request())
        self.assertEqual(self.api.metadata_write_state(self.rid)['status'],'queued')
        self.api.loading[str(self.dataset.root)]=False
        with self.archive.connect() as c:c.execute('UPDATE capture_metadata_requests SET due=0')
        self.assertTrue(self.api.process_metadata_request());result=self.api.metadata_write_state(self.rid)
        self.assertEqual((result['status'],result['result']['queued']),('submitted',2))
        self.assertFalse(self.api.process_metadata_request());self.drain()

    def test_request_rejects_receiver_and_bad_folder_before_queueing(self):
        with patch.object(self.api,'readonly',return_value=True):
            with self.assertRaisesRegex(ValueError,'单向接收'):self.api.request_metadata_write(self.rid)
        for folder in ('../outside','/absolute','C:/outside'):
            with self.assertRaises(ValueError):self.api.request_metadata_write(self.rid,folder)
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM capture_metadata_requests').fetchone()[0],0)

    def test_request_reports_unassigned_photos_instead_of_all_written(self):
        self.prepare_missing()
        with patch.object(self.api,'_start_metadata_request_worker'):self.api.request_metadata_write(self.rid)
        self.assertTrue(self.api.process_metadata_request())
        result=self.api.metadata_write_state(self.rid)
        self.assertEqual(result['result']['unassigned'],2)
        self.assertEqual(result['result']['unassigned_examples'],['IMG_0003.JPG','IMG_0004.JPG'])
        self.assertIn('2 张无SKU，保持原样',result['reason'])
        self.assertEqual(result['result']['queued'],2)
        self.drain()
        with patch.object(self.api,'_start_metadata_request_worker'):self.api.request_metadata_write(self.rid)
        self.assertTrue(self.api.process_metadata_request())
        result=self.api.metadata_write_state(self.rid)
        self.assertEqual(result['result']['queued'],0);self.assertEqual(result['result']['unassigned'],2)
        self.assertNotIn('已全部写入',result['reason'])
        self.assertFalse(self.archive.writer.read(self.photos/'IMG_0003.JPG').get('XMP-fk:SKU'))

    def test_request_is_restored_after_service_restart(self):
        from archive_capture_service import ArchiveCaptureService
        with patch.object(self.api,'_start_metadata_request_worker'):first=self.api.request_metadata_write(self.rid)
        with self.archive.connect() as c:c.execute("UPDATE capture_metadata_requests SET status='preparing'")
        self.api.close()
        fresh=ArchiveCaptureService(self.store,self.archive,self.catalog,self.sku,autostart=False)
        try:
            restored=fresh.metadata_write_state(self.rid)
            self.assertEqual((restored['id'],restored['status']),(first['id'],'queued'))
            resumed=threading.Event()
            with patch.object(fresh,'_start_metadata_request_worker',side_effect=resumed.set),patch.object(fresh,'tick'):
                fresh.thread.start();self.assertTrue(resumed.wait(2))
        finally:fresh.close()

    def test_real_commit_lock_defers_then_recovers_without_starving_other_photo(self):
        self.prepare_missing();self.api.write_metadata(self.rid);path=self.photos/'IMG_0001.JPG'
        # 只读浏览程序拒绝共享写入：准备允许读，真正提交遭遇 WinError 32。
        with windows_handle(path,access=0x80000000,share=1):
            self.api.tick()
            with self.dataset.connect() as c:
                row=dict(c.execute("SELECT * FROM tasks WHERE kind='metadata' AND record=?",(self.dataset.record(path=path.name)['id'],)).fetchone())
            self.assertEqual(row['status'],'queued');self.assertIn('自动重试',row['reason'])
            self.assertEqual(self.api.state(self.rid)['metadata_tasks']['failed'],0)
            self.api.tick()
            self.assertEqual(self.archive.writer.read(self.photos/'IMG_0002.JPG')['XMP-fk:SKU'],'MANUAL-224')
        with self.dataset.connect() as c:c.execute("UPDATE tasks SET data=json_set(data,'$._retry_at',0) WHERE status='queued'")
        self.drain();self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'MANUAL-223')

    def test_browser_click_queues_and_reports_real_success(self):
        self.browser_case()

    def test_browser_timeout_still_allows_request_and_refresh_recovers_state(self):
        self.browser_case(state_unavailable=True)

    def test_browser_selected_folder_with_independent_sibling_writes(self):
        self.browser_case(selected_folder=True)

    def browser_case(self,*,state_unavailable=False,selected_folder=False):
        from playwright.sync_api import sync_playwright
        import server
        if selected_folder:self.prepare_selected_folder()
        else:self.prepare_missing()
        with ExitStack() as stack:
            values=dict(STORE=self.store,CATALOG=self.catalog,ARCHIVE=self.archive,SKU=self.sku,MODELS=self.sku.providers,DATA=self.store.data,WATCH=None,MOUSE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=None)
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000});errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        if state_unavailable:
                            page.route('**/api/archive-capture/state?*',lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"模拟目录状态请求超时"}'))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepArchiveOpenItem&&window.FramekeepArchiveCapture')
                        page.evaluate('(id)=>FramekeepArchiveOpenItem(id)',self.item(1))
                        page.locator('#capture-folder-tab').click()
                        page.wait_for_function('()=>!document.getElementById("capture-write-metadata").disabled')
                        if state_unavailable:
                            page.wait_for_function('()=>document.getElementById("capture-access").textContent.includes("模拟目录状态请求超时")')
                        page.locator('#capture-write-metadata').click()
                        page.wait_for_function('()=>document.getElementById("capture-write-result").textContent.includes("新增 2")')
                        self.drain()
                        if state_unavailable:
                            page.unroute('**/api/archive-capture/state?*');page.locator('#capture-refresh').click()
                        else:page.evaluate('()=>FramekeepArchiveCapture.refresh()')
                        page.wait_for_function('()=>document.getElementById("capture-sync").textContent.includes("完整字段已验证 2/2")')
                        if not selected_folder:
                            page.wait_for_function('()=>document.getElementById("capture-write-result").textContent.includes("无SKU保留 2 张")')
                            self.assertIn('临时拍摄无需分配SKU',page.locator('#capture-write-result').inner_text())
                        out=Path(__file__).resolve().parents[1]/'build/manual-metadata-backfill';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/('selected-folder.png' if selected_folder else 'timeout-recovered.png' if state_unavailable else 'verified.png')))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


class LiveLockRecoveryTests(unittest.TestCase):
    setUp=live_fixture.BackgroundRowWriteTests.setUp
    tearDown=live_fixture.BackgroundRowWriteTests.tearDown
    prepare=live_fixture.BackgroundRowWriteTests.prepare
    rate=live_fixture.BackgroundRowWriteTests.rate
    choose=live_fixture.BackgroundRowWriteTests.choose
    drain=live_fixture.BackgroundRowWriteTests.drain
    ready=live_fixture.BackgroundRowWriteTests.ready
    rename=live_fixture.BackgroundRowWriteTests.rename

    def test_real_prepare_lock_defers_current_job_and_other_photo_still_writes(self):
        self.ready();self.rename();path=self.photos/'0.JPG';before=path.read_bytes()
        with windows_handle(path):
            self.archive.process_one()
            with self.archive.connect() as c:
                row=dict(c.execute("SELECT * FROM metadata_jobs WHERE status='queued' AND path=?",(str(path),)).fetchone())
            self.assertEqual(row['retry_count'],1);self.assertIn('WinError 32',row['reason'])
            self.assertEqual(path.read_bytes(),before)
            self.archive.process_one()
            self.assertEqual(self.archive.writer.read(self.photos/'1.JPG')['XMP-fk:SKU'],'NEW-200')
        with self.archive.connect() as c:c.execute("UPDATE metadata_jobs SET retry_at=0 WHERE status='queued'")
        self.archive.process_one();self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'NEW-200')

    def test_missing_path_preserves_real_windows_error_and_is_not_lock(self):
        with self.assertRaises(OSError) as caught:
            with self.archive.writer.exclusive(self.photos/'missing.JPG'):pass
        self.assertEqual(caught.exception.winerror,2)
        from file_safety import metadata_retry_delay
        self.assertIsNone(metadata_retry_delay(caught.exception,0));self.assertIn('missing.JPG',str(caught.exception))

    def test_external_change_fails_without_auto_retry(self):
        self.ready();self.rename();path=self.photos/'0.JPG';path.write_bytes(path.read_bytes()+b'external')
        self.archive.process_one()
        with self.archive.connect() as c:
            row=c.execute("SELECT status,retry_at,reason FROM metadata_jobs WHERE path=? ORDER BY created DESC LIMIT 1",(str(path),)).fetchone()
        self.assertEqual((row['status'],row['retry_at']),('failed',0));self.assertIn('修改',row['reason'])

    def test_long_lock_and_old_failures_recover_on_restart(self):
        self.ready();self.rename();path=self.photos/'0.JPG'
        with self.archive.connect() as c:c.execute("UPDATE metadata_jobs SET retry_count=8 WHERE status='queued' AND path=?",(str(path),))
        with windows_handle(path):self.archive.process_one()
        with self.archive.connect() as c:
            row=c.execute("SELECT id,status FROM metadata_jobs WHERE path=? ORDER BY created DESC LIMIT 1",(str(path),)).fetchone()
            self.assertEqual(row['status'],'queued')
            c.execute("UPDATE metadata_jobs SET status='failed',reason='文件仍被写入或占用，请稍后重试' WHERE id=?",(row['id'],))
        self.archive.close()
        from asset_archive import AssetArchive
        self.archive=AssetArchive(self.store,autostart=False);self.archive.sku=self.sku;self.sku.archive=self.archive
        self.archive.process_one()
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT status FROM metadata_jobs WHERE id=?',(row['id'],)).fetchone()[0],'applied')


if __name__=='__main__':unittest.main()
