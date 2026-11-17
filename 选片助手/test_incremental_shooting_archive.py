"""大目录只准备必要区间；异步结果乱序不把未准备照片并入相邻款。"""
from datetime import datetime,timedelta
import json
from PIL import Image
import unittest
from unittest.mock import patch
import media_engine as media
import test_ocr_metadata as fixture


class IncrementalArchiveTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown

    def test_later_label_prepares_its_interval_before_unrelated_future_files(self):
        records=[]
        for index in range(120):
            path=self.photos/f'extra-{index:03}.JPG';Image.new('RGB',(20,20),(index,80,100)).save(path)
            stamp=(datetime(2026,10,21,10,1)+timedelta(seconds=index)).strftime('%Y:%m:%d %H:%M:%S')
            records.append(media.capture_record(self.photos,path,None,{'DateTimeOriginal':stamp}))
        self.store.append_live(self.sid,records)
        session=self.store.session(self.sid);state={**session['state'],'overrides':{**session['state']['overrides'],records[47]['id']:2,records[95]['id']:2}}
        self.store.save(self.sid,session['revision'],state)
        # 生产启动为autostart=False，然后由首屏释放后台线程。
        with patch.object(self.archive.thread,'is_alive',return_value=True):self.archive.sync_stream()
        with self.archive.connect() as c:self.assertLessEqual(c.execute('SELECT count(*) FROM catalog_links').fetchone()[0],36)
        for index,code in [(95,'LC-LATER-200'),(47,'LC-EARLIER-100')]:
            cid=records[index]['id'];self.sku.parse_capture(self.sid,cid);details=self.sku.ocr_details(self.sid,cid)
            self.sku.choose_ocr_sku(self.sid,cid,code,details['reading_key'],details['image_key'])
        with self.archive.connect() as c:
            assigned={row['capture']:row['sku'] for row in c.execute('SELECT k.capture,a.sku FROM catalog_links k JOIN assets a ON a.id=k.asset')}
            self.assertEqual({assigned.get(r['id']) for r in records[:48]},{'LC-EARLIER-100'})
            self.assertEqual({assigned.get(r['id']) for r in records[48:96]},{'LC-LATER-200'})
            self.assertTrue(all(not assigned.get(r['id']) for r in records[96:]))
            self.assertEqual(c.execute("SELECT count(*) FROM metadata_jobs WHERE status='queued'").fetchone()[0],96)

if __name__=='__main__':unittest.main()
