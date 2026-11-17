"""无XMP旧照片：二星当前图OCR、冷启动元数据复用及完整SKU区间。"""
import unittest
from unittest.mock import patch

from PIL import Image
from archive_capture_service import xmp_signature
import test_archive_capture_service as fixtures


class LegacyTwoStarTests(unittest.TestCase):
    setUp=fixtures.ArchiveCaptureTests.setUp
    tearDown=fixtures.ArchiveCaptureTests.tearDown
    client=fixtures.ArchiveCaptureTests.client
    item=fixtures.ArchiveCaptureTests.item
    drain=fixtures.ArchiveCaptureTests.drain

    def test_plain_jpeg_two_star_recognizes_before_scan_then_applies_whole_interval(self):
        self.assertFalse(list(self.photos.glob('*.xmp')))
        for n in range(20):Image.new('RGB',(40,40),'teal').save(self.photos/f'LARGE_{n:04d}.JPG')
        self.catalog.scan(self.rid)
        with self.catalog.connect() as c:marker=c.execute("SELECT id FROM items WHERE name='LARGE_0019.JPG'").fetchone()[0]
        self.api.last_scan.pop(str(self.photos),None)
        result=self.api.rate(marker,2,root_id=self.rid)
        self.assertTrue(result['preparing']);self.assertTrue(result['ocr_queued'])
        calls=[];self.sku.recognizer=lambda *_:calls.append(1) or {'text':'LC700001','codes':['LC700001']}
        self.api.tick()
        record=self.dataset.record(path='LARGE_0019.JPG')
        self.assertEqual(calls,[1],'普通二星也应自动OCR，不要求点击重新解析')
        self.assertTrue(self.api.loading[str(self.photos)])
        self.assertEqual(record['ocr_apply_state'],'waiting_interval')
        self.assertFalse(record.get('sku'),'目录未完整前不能只确认标签单图')
        self.drain()
        group=next(g for g in self.dataset.groups() if g.get('sku')=='LC700001')
        self.assertEqual(group['status'],'confirmed');self.assertEqual(len(group['members']),20)
        self.assertEqual(sum(r.get('sku')=='LC700001' for r in self.dataset.records()),20)
        self.assertEqual(calls,[1],'区间接续不能重复OCR')

    def test_restart_reuses_reader_version_without_xmp_or_exif_reread(self):
        self.assertFalse(list(self.photos.glob('*.xmp')))
        self.api.close();self.sku.close();self.catalog.close();self.archive.close()
        _,archive,_,_,api,rid=self.client(self.base/'A',self.photos)
        dataset,_=api.scope(rid)
        self.assertFalse(api.validated)
        with patch.object(archive.writer,'read',side_effect=AssertionError('稳定版本不能重新读取EXIF')):
            api._prefetch_scan(dataset);api._scan(dataset)
        self.assertEqual(len(dataset.records()),4)
        self.assertFalse(api.loading[str(self.photos)])

    def test_no_xmp_scan_uses_directory_listing_instead_of_per_photo_missing_probes(self):
        self.api.validated.clear()
        with patch('rating_xmp.sidecar_paths',side_effect=AssertionError('不能逐图探测不存在的XMP')):
            self.api._prefetch_scan(self.dataset);self.api._scan(self.dataset)
        self.assertFalse(self.api.loading[str(self.photos)])

    def test_explicit_focus_refresh_still_reads_real_file(self):
        self.api.validated.clear()
        with patch.object(self.archive.writer,'read',wraps=self.archive.writer.read) as read:
            self.api._scan(self.dataset,focus=self.photos/'IMG_0001.JPG')
        self.assertTrue(read.called)

    def test_listing_preserves_xmp_digest_and_detects_new_sidecar_after_prefetch(self):
        photo=self.photos/'IMG_0001.JPG';old=self.dataset.record(path=photo.name)
        self.dataset.change([{**old,'metadata_reader':0}],kind='reader_upgrade')
        prepared=self.api._prefetch_scan(self.dataset)
        side=photo.with_suffix('.xmp')
        side.write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Rating="1"/></rdf:RDF></x:xmpmeta>',encoding='utf8')
        versions=self.api._scan_versions([{'path':str(photo)}])
        self.assertEqual(versions[str(photo)][1],xmp_signature(photo))
        self.api.scan_reads=prepared
        self.api._scan(self.dataset)
        self.assertEqual(self.dataset.record(path=photo.name)['rating'],1,'预读后的新XMP不能被旧缓存盖过')


if __name__=='__main__':unittest.main()
