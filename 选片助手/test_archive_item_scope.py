"""父数据集混有独立子库时，单图二星/OCR仍只写真实所属数据集。"""
from pathlib import Path
import time
import unittest

from PIL import Image
from portable_capture_store import DatasetBusy
import test_archive_capture_service as fixtures


class ArchiveItemScopeTests(unittest.TestCase):
    setUp=fixtures.ArchiveCaptureTests.setUp
    tearDown=fixtures.ArchiveCaptureTests.tearDown
    client=fixtures.ArchiveCaptureTests.client
    item=fixtures.ArchiveCaptureTests.item
    drain=fixtures.ArchiveCaptureTests.drain

    def child(self):
        folder=self.photos/'另一天'
        folder.mkdir()
        path=folder/'CHILD.JPG'
        Image.new('RGB',(120,160),'teal').save(path)
        self.catalog.scan(self.rid)
        until=time.monotonic()+10
        while time.monotonic()<until:
            with self.catalog.connect() as c:
                row=c.execute('SELECT id FROM items WHERE path=?',(str(path),)).fetchone()
            if row:break
            time.sleep(.02)
        self.assertIsNotNone(row)
        child=self.api._dataset(folder)
        child.acquire()
        self.api._scan(child)
        return child,row[0],path

    def test_two_star_with_independent_sibling_runs_ocr_and_embeds_whole_interval(self):
        child,_,path=self.child()
        before=path.read_bytes()
        result=self.api.rate(self.item(2),2,root_id=self.rid)
        self.assertTrue(result['ocr_queued'])
        self.drain()
        self.assertEqual(len(self.dataset.groups()[0]['members']),2)
        for n in (1,2):
            tags=self.archive.writer.read_asset(self.photos/f'IMG_{n:04d}.JPG')
            self.assertEqual(tags['XMP-fk:SKU'],'LC123456')
        self.assertFalse(self.dataset.record(path='IMG_0003.JPG').get('sku'))
        self.assertFalse(child.records()[0].get('sku'))
        self.assertEqual(path.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'独立子数据集'):
            self.api.edit(self.rid)

    def test_parent_selection_routes_rating_confirm_and_ocr_to_child(self):
        child,item,path=self.child()
        original=self.dataset.records()
        self.api.rate(item,2,root_id=self.rid)
        self.drain(dataset=child)
        self.assertEqual(child.records()[0]['sku'],'LC123456')
        self.api.confirm(item,'LC999999',root_id=self.rid,auto_edit=True)
        self.drain(dataset=child)
        self.assertEqual(self.archive.writer.read_asset(path)['XMP-fk:SKU'],'LC999999')
        self.assertTrue(self.api.recognize(item,auto_edit=True)['ocr_queued'])
        self.drain(dataset=child)
        self.assertEqual(self.dataset.records(),original)

    def test_unloaded_child_keeps_foreign_edit_lock(self):
        child,item,_=self.child()
        _,_,_,_,other,rid=self.client(self.base/'B',self.photos)
        other.scope(rid)
        self.assertNotIn(str(child.root),other.datasets)
        with self.assertRaises(DatasetBusy):
            other.rate(item,2,root_id=rid)
        self.assertFalse(other.datasets[str(child.root)].editing)
        self.assertEqual(child.records()[0]['rating'],0)

    def test_explicit_scope_still_rejects_outside_item(self):
        self.child()
        with self.assertRaisesRegex(ValueError,'不在当前目录范围'):
            self.api.rate(self.item(2),2,root_id=self.rid,folder='另一天')
        self.assertEqual(self.dataset.record(path='IMG_0002.JPG')['rating'],0)

    def test_parent_batch_cannot_mix_child_dataset_records(self):
        _,item,_=self.child()
        with self.assertRaisesRegex(ValueError,'独立子数据集'):
            self.api.batch_preview(self.rid,'',[item],'MIXED-01')

    def test_explicit_ocr_and_manual_confirm_allow_independent_sibling(self):
        self.child()
        self.assertTrue(self.api.recognize(self.item(2),auto_edit=True)['ocr_queued'])
        self.drain()
        self.api.confirm(self.item(1),'MANUAL-01',whole_group=False,auto_edit=True,root_id=self.rid)
        self.drain()
        self.assertEqual(self.archive.writer.read_asset(self.photos/'IMG_0001.JPG')['XMP-fk:SKU'],'MANUAL-01')


if __name__=='__main__':unittest.main()
