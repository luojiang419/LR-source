"""历史扫描/评级慢或失败时，新图SKU任务仍须真实落盘。"""
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch
from PIL import Image

from test_shooting_background_write import BackgroundRowWriteTests


class QueueIsolationTests(unittest.TestCase):
    setUp=BackgroundRowWriteTests.setUp
    tearDown=BackgroundRowWriteTests.tearDown
    prepare=BackgroundRowWriteTests.prepare
    rate=BackgroundRowWriteTests.rate
    choose=BackgroundRowWriteTests.choose
    drain=BackgroundRowWriteTests.drain
    ready=BackgroundRowWriteTests.ready
    rename=BackgroundRowWriteTests.rename

    def assert_written(self,timeout=4):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            with self.archive.connect() as c:
                jobs=[dict(r) for r in c.execute("SELECT status,reason FROM metadata_jobs WHERE json_extract(fields,'$.SKU')='NEW-200'")]
            if jobs and all(j['status']=='applied' for j in jobs):return
            time.sleep(.03)
        self.fail('队列没有推进：'+str(self.archive.metadata_queue_state())+' '+str(jobs))

    def test_backfill_failure_does_not_block_existing_queue(self):
        self.ready();self.rename()
        with patch('metadata_backfill.enqueue_missing',side_effect=sqlite3.OperationalError('模拟旧资料扫描失败')):
            self.archive.start_metadata();self.archive.request_backfill();self.assert_written()
        self.assertEqual(self.archive.writer.read(self.photos/'0.JPG')['XMP-fk:SKU'],'NEW-200')

    def test_malformed_old_assignment_does_not_poison_valid_missing_jobs(self):
        from metadata_backfill import enqueue_missing
        self.ready();self.rename()
        with self.archive.connect() as c:
            c.execute('DELETE FROM metadata_jobs')
            c.execute("INSERT INTO assets(id,created,sku,source,revision) VALUES ('bad-old',0,'OLD-BAD','manual',1)")
            c.execute("INSERT INTO assignments VALUES ('bad-old',1,'{',0)")
            c.execute("INSERT INTO files(id,asset,component,sha) VALUES ('bad-file','bad-old','jpg','unknown')")
            c.execute("INSERT INTO locations VALUES ('offline-old.JPG','bad-file',0,'offline')")
        self.assertEqual(enqueue_missing(self.archive),2)
        self.assertEqual(self.archive.backfill_unverified[0]['id'],'bad-old')
        self.archive.start_metadata();self.assert_written()

    def test_slow_backfill_does_not_block_existing_queue(self):
        self.ready();self.rename();entered=threading.Event();release=threading.Event()
        def slow(*args,**kwargs):entered.set();release.wait(10);return 0
        with patch('metadata_backfill.enqueue_missing',side_effect=slow):
            try:
                self.archive.start_metadata();self.archive.request_backfill();self.assertTrue(entered.wait(2));self.assert_written()
                self.assertFalse(release.is_set())
            finally:release.set()

    def test_slow_unrelated_rating_does_not_block_new_sku_files(self):
        self.ready();self.rename();old=self.base/'history.JPG';Image.new('RGB',(160,100),'red').save(old)
        self.archive.ratings.set([old],3);entered=threading.Event();release=threading.Event();original=self.archive.writer.write_rating
        def slow(path,rating):
            if Path(path)==old:entered.set();release.wait(10)
            return original(path,rating)
        with patch.object(self.archive.writer,'write_rating',side_effect=slow):
            rating=threading.Thread(target=self.archive.ratings.sync.process_one);rating.start()
            try:
                self.assertTrue(entered.wait(2));self.archive.start_metadata();self.assert_written()
                self.assertFalse(release.is_set())
            finally:release.set();rating.join(4)

    def test_same_photo_and_raw_jpg_pair_still_share_file_lock(self):
        held=threading.Event();release=threading.Event();acquired=threading.Event()
        def hold():
            with self.archive.writer.file_lock(self.photos/'same.JPG'):held.set();release.wait(3)
        def raw():
            with self.archive.writer.file_lock(self.photos/'same.CR3'):acquired.set()
        first=threading.Thread(target=hold);second=threading.Thread(target=raw)
        first.start();self.assertTrue(held.wait(1));second.start()
        self.assertFalse(acquired.wait(.15));release.set();first.join(2);second.join(2)
        self.assertTrue(acquired.is_set());self.assertEqual(self.archive.writer.path_locks,{})

    def test_same_photo_rating_then_sku_uses_registered_new_hash(self):
        self.ready();self.rename();path=self.photos/'0.JPG';self.archive.ratings.set([path],3)
        entered=threading.Event();release=threading.Event();original=self.archive.writer.write_rating
        def slow(*args):entered.set();release.wait(8);return original(*args)
        with patch.object(self.archive.writer,'write_rating',side_effect=slow):
            rating=threading.Thread(target=self.archive.ratings.sync.process_one);rating.start()
            try:
                self.assertTrue(entered.wait(2));self.archive.start_metadata()
                deadline=time.monotonic()+2
                while time.monotonic()<deadline:
                    if self.archive.metadata_queue_state()['phase']=='waiting_file':break
                    time.sleep(.03)
                else:self.fail('SKU未领取任务并等待同图锁')
                release.set();rating.join(4);self.assert_written()
            finally:release.set();rating.join(4)
        fields=self.archive.writer.read(path);self.assertEqual(fields['XMP-fk:SKU'],'NEW-200');self.assertEqual(fields['XMP-xmp:Rating'],3)

    def test_browser_exposes_queue_diagnostics_and_waiting_reason(self):
        from test_sku_input_confirm import InputConfirmTests
        from playwright.sync_api import sync_playwright,expect
        self.ready();self.rename()
        with InputConfirmTests.http(self) as url,sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000});page.goto(url,wait_until='domcontentloaded')
                page.wait_for_function('()=>!FramekeepBoot.loading');page.locator('#sku-open').click()
                expect(page.locator('#sku-rows .sku-metadata-status')).to_contain_text('尚未启动')
                toggle=page.locator('#progress-sidebar-toggle')
                if toggle.get_attribute('aria-expanded')=='false':toggle.click()
                page.locator('#sku-queue-open').click();expect(page.locator('#sku-queue-dialog')).to_be_visible()
                page.wait_for_function('()=>document.getElementById("sku-queue-details").value.includes("counts")')
                data=__import__('json').loads(page.locator('#sku-queue-details').input_value())
                self.assertEqual(data['counts']['queued'],2);self.assertNotIn('providers',data)
                page.locator('#sku-queue-close').click();expect(page.locator('#sku-queue-dialog')).not_to_be_visible()
            finally:browser.close()


if __name__=='__main__':unittest.main()
