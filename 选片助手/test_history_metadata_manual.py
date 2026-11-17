"""历史漏写和遗留队列只由资产归档手动释放；使用隔离照片验证字节与回读。"""
import unittest

from asset_archive import AssetArchive
import test_shooting_background_write as live_fixture
import test_manual_metadata_backfill as manual_fixture


class LiveHistoryTests(unittest.TestCase):
    setUp=live_fixture.BackgroundRowWriteTests.setUp
    tearDown=live_fixture.BackgroundRowWriteTests.tearDown
    prepare=live_fixture.BackgroundRowWriteTests.prepare
    rate=live_fixture.BackgroundRowWriteTests.rate
    choose=live_fixture.BackgroundRowWriteTests.choose
    drain=live_fixture.BackgroundRowWriteTests.drain
    ready=live_fixture.BackgroundRowWriteTests.ready
    rename=live_fixture.BackgroundRowWriteTests.rename

    def reopen(self):
        self.archive.close()
        self.archive=AssetArchive(self.store,autostart=False)
        self.archive.sku=self.sku;self.sku.archive=self.archive

    def test_old_jobs_stay_paused_in_same_session_and_after_monitor_stops(self):
        self.ready();self.rename()
        # 模拟升级前未显式申请补写的历史任务。
        with self.archive.connect() as c:c.execute("UPDATE metadata_jobs SET fields=json_remove(fields,'$._manual_backfill') WHERE status='queued'")
        self.reopen()
        paths=[self.photos/f'{n}.JPG' for n in (0,1)]
        before={p:p.read_bytes() for p in paths}
        self.archive.live_session=lambda:self.sid
        self.archive.process_one()
        self.archive.live_session=lambda:None
        self.archive.process_one()
        self.assertEqual(before,{p:p.read_bytes() for p in paths})
        self.assertIn('手动补写',self.sku.state()['rows'][0]['metadata']['worker_message'])
        # 手动范围只释放一张；已提交批次重启仍可继续，邻图继续等待手动。
        self.archive.request_backfill(str(paths[0]));self.reopen()
        self.archive.process_one();self.archive.process_one()
        self.assertEqual(self.archive.writer.read(paths[0])['XMP-fk:SKU'],'NEW-200')
        self.assertEqual(paths[1].read_bytes(),before[paths[1]])

    def test_new_confirmation_after_restart_still_writes(self):
        self.ready();self.rename();self.reopen()
        self.rename('NEW-200','CURRENT-300');self.drain()
        self.assertEqual(self.archive.writer.read(self.photos/'0.JPG')['XMP-fk:SKU'],'CURRENT-300')


class PortableHistoryTests(unittest.TestCase):
    setUp=manual_fixture.ManualBackfillTests.setUp
    tearDown=manual_fixture.ManualBackfillTests.tearDown
    client=manual_fixture.ManualBackfillTests.client
    item=manual_fixture.ManualBackfillTests.item
    drain=manual_fixture.ManualBackfillTests.drain
    prepare_missing=manual_fixture.ManualBackfillTests.prepare_missing

    def test_missing_recovery_waits_for_manual_write(self):
        self.prepare_missing()
        before={p:p.read_bytes() for p in self.photos.glob('*.JPG')}
        self.api._recover_tasks(self.dataset)
        task,_=self.api._claim_task(self.dataset,False,metadata_only=True)
        self.assertIsNone(task)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.api.write_metadata(self.rid);self.drain()
        self.assertEqual(self.archive.writer.read(self.photos/'IMG_0001.JPG')['XMP-fk:SKU'],'MANUAL-223')

    def test_old_queued_tasks_pause_but_manual_batch_survives_reopening(self):
        self.api.confirm(self.item(1),'OLD-QUEUE',whole_group=False)
        with self.dataset.connect() as c:c.execute("UPDATE tasks SET data=json_remove(data,'$._manual_backfill') WHERE kind='metadata'")
        key=str(self.dataset.root)
        self.dataset.release();self.api.datasets.pop(key)
        self.dataset=self.api._dataset(self.photos);self.dataset.acquire()
        task,_=self.api._claim_task(self.dataset,False,metadata_only=True)
        self.assertIsNone(task)
        self.api.write_metadata(self.rid)
        # 再次打开不会把用户已提交的补写任务重新暂停。
        self.dataset.release();self.api.datasets.pop(key)
        self.dataset=self.api._dataset(self.photos);self.dataset.acquire()
        self.drain()
        self.assertEqual(self.archive.writer.read(self.photos/'IMG_0001.JPG')['XMP-fk:SKU'],'OLD-QUEUE')


if __name__=='__main__':unittest.main()
