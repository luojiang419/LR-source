"""归档整款手填纯数字 SKU，以及新确认版本不再显示旧写入失败。"""
import unittest
import uuid

from metadata_writer import field_text
import test_archive_capture_service as fixtures


class ArchiveManualNumericSkuTests(unittest.TestCase):
    setUp=fixtures.ArchiveCaptureTests.setUp
    tearDown=fixtures.ArchiveCaptureTests.tearDown
    client=fixtures.ArchiveCaptureTests.client
    item=fixtures.ArchiveCaptureTests.item
    drain=fixtures.ArchiveCaptureTests.drain

    def test_manual_numeric_sku_writes_the_whole_interval(self):
        self.api.rate(self.item(2),2,root_id=self.rid);self.drain()
        self.api.confirm(self.item(2),'7876953',whole_group=True,root_id=self.rid);self.drain()
        for name,rating in (('IMG_0001.JPG',0),('IMG_0002.JPG',2)):
            actual=self.archive.writer.read(self.photos/name)
            self.assertEqual(field_text(actual['XMP-fk:SKU']),'7876953')
            self.assertEqual(field_text(actual['IFD0:XPSubject']),'7876953')
            self.assertIn('SKU:7876953',actual['IFD0:XPKeywords'])
            self.assertEqual(actual['XMP-xmp:Rating'],rating)
        self.assertFalse(self.api.state(self.rid)['tasks'])

    def test_old_metadata_failures_are_preserved_but_only_current_failures_are_shown(self):
        self.api.confirm(self.item(1),'7876953',whole_group=False,root_id=self.rid)
        old=self.dataset.record(path='IMG_0001.JPG')['decision_id']
        with self.dataset.connect() as c:c.execute("UPDATE tasks SET status='failed',reason='Windows 图片属性读回不一致' WHERE revision=?",(old,))
        self.api.confirm(self.item(1),'LC7876953',whole_group=False,root_id=self.rid)
        # 旧失败必须留作历史证据；fixture.drain 会拒绝所有历史失败，故只等待当前队列结束。
        for _ in range(50):
            self.api.tick()
            with self.dataset.connect() as c:
                if not c.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running')").fetchone():break
        self.assertFalse(self.api.state(self.rid)['tasks'])
        record=self.dataset.record(path='IMG_0001.JPG')
        with self.dataset.connect() as c:
            self.assertGreater(c.execute("SELECT count(*) FROM tasks WHERE revision=? AND status='failed'",(old,)).fetchone()[0],0)
            task_id=uuid.uuid4().hex
            c.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(task_id,'metadata',record['id'],record['decision_id'],'failed','真实当前版本失败','{}'))
        tasks=self.api.state(self.rid)['tasks']
        self.assertEqual([(t['id'],t['reason']) for t in tasks],[(task_id,'真实当前版本失败')])
        self.assertEqual(self.api.state(self.rid)['metadata_tasks']['failed'],1)


if __name__=='__main__':unittest.main()
