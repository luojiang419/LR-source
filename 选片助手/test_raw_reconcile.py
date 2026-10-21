import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from catalog_store import Store
from asset_archive import AssetArchive
from raw_reconcile import RawReconcile,compare,identity
from metadata_writer import sha,raw_integrity
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]
SAMPLE=ROOT/'build/raw-reconcile/r6ii-6402.CR3'

def camera_record(name='IMG_0346',serial='033020003848',stamp='2023:02:19 14:31:58',sub='37'):
    return {'name':name,'captureIdentity':{'Model':'Canon EOS R6m2','SerialNumber':serial,'DateTimeOriginal':stamp,'SubSecTimeOriginal':sub}}

class IdentityTests(unittest.TestCase):
    def test_filename_alone_is_never_enough_and_time_serial_subseconds_protect(self):
        a=camera_record();self.assertTrue(compare(a,a)['certain'])
        for different in [camera_record(serial='other'),camera_record(stamp='2023:02:20 14:31:58'),camera_record(sub='39')]:self.assertIsNone(compare(a,different))
        self.assertTrue(compare(camera_record('renamed'),a)['certain'])
        weak=camera_record(serial='',sub='');self.assertFalse(compare(weak,weak)['certain'])
        self.assertIsNone(compare({'name':'IMG_0346'},{'name':'IMG_0346'}))
        self.assertIsNone(compare(camera_record(stamp='0000:00:00 00:00:00'),camera_record(stamp='0000:00:00 00:00:00')))
        self.assertIsNone(compare(camera_record(sub='00'),camera_record(sub='01')))

@unittest.skipUnless(SAMPLE.is_file(),'公开 R6 II 样片不在本机，真实 RAW 用例需准备样片')
class RawWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.jpg=self.base/'现场JPG';self.raw=self.base/'后拷RAW';self.jpg.mkdir();self.raw.mkdir()
        self.store=Store(self.base/'data',ROOT);self.archive=AssetArchive(self.store,autostart=False);self.link=RawReconcile(self.archive,autostart=False)
        self.store.on_catalog=self.link.request
        import rawpy
        with rawpy.imread(str(SAMPLE)) as r:thumb=r.extract_thumb().data
        p=self.jpg/'IMG_0346.JPG';p.write_bytes(thumb)
        self.meta=media.read_metadata([SAMPLE],media.find_exiftool(ROOT))[0]
        self.archive.writer.run('-overwrite_original','-Make=Canon','-Model='+self.meta['Model'],'-ExifIFD:SerialNumber='+str(self.meta['SerialNumber']),'-DateTimeOriginal='+self.meta['DateTimeOriginal'],'-SubSecTimeOriginal='+str(self.meta['SubSecTimeOriginal']),'-OffsetTimeOriginal='+self.meta['OffsetTimeOriginal'],p)
        self.sid=self.scan(self.jpg);r=self.store.records(self.sid)[0];self.cid=r['id']
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides']={self.cid:4};self.store.save(self.sid,session['revision'],state)
        self.preview=self.archive.preview(self.sid,[self.cid],'SKU-远程123','细节图');self.operation=self.archive.apply(self.preview['preview_id'])['operation']['id']
        self.archive.process_one();self.link.index_sources(self.sid)
        self.target=self.raw/'IMG_0346.CR3';shutil.copy2(SAMPLE,self.target)
    def tearDown(self):self.link.close();self.archive.close();self.temp.cleanup()
    def scan(self,path):
        job=self.store.scan(str(path));
        import time
        for _ in range(200):
            if self.store.jobs[job]['status'] in ('completed','failed'):break
            time.sleep(.02)
        self.assertEqual(self.store.jobs[job]['status'],'completed',self.store.jobs[job]);return self.store.jobs[job]['session_id']
    def finish(self,sid):
        self.link.process_session(sid)
        for _ in range(5):self.archive.process_one();self.link.refresh_completed()
        return self.link.state(sid)['pairs'][0]
    def test_cross_folder_scan_embeds_rating_sku_and_preserves_raw_payload(self):
        original=raw_integrity(self.target);original_sample=sha(SAMPLE);rawsid=self.scan(self.raw)
        self.assertIn(rawsid,self.link.pending)
        pair=self.finish(rawsid);self.assertEqual(pair['status'],'applied',pair)
        fields=self.archive.writer.read(self.target);self.assertEqual(fields['XMP-xmp:Rating'],4);self.assertEqual(fields['XMP-fk:SKU'],'SKU-远程123');self.assertEqual(fields['XMP-fk:ShootType'],'细节图')
        self.assertEqual(raw_integrity(self.target),original);self.assertEqual(sha(SAMPLE),original_sample);self.assertFalse(self.target.with_suffix('.xmp').exists())
        self.assertEqual(self.store.records(rawsid)[0]['liveRating'],4)
        aid_jpg=self.archive.ensure(self.sid,self.cid);aid_raw=self.archive.ensure(rawsid,self.store.records(rawsid)[0]['id']);self.assertEqual(aid_jpg,aid_raw)
        count=len(self.archive.asset(aid_raw)['jobs']);self.link.process_session(rawsid);self.assertEqual(len(self.archive.asset(aid_raw)['jobs']),count)
        self.link.close();self.link=RawReconcile(self.archive,autostart=False);self.assertEqual(self.link.state(rawsid)['pairs'][0]['status'],'applied')
    def test_ambiguous_sources_do_not_write_then_manual_confirmation_works(self):
        another=self.base/'重复JPG';another.mkdir();shutil.copy2(self.jpg/'IMG_0346.JPG',another/'IMG_0346.JPG');sid2=self.scan(another);self.link.index_sources(sid2)
        # 制造两个不同人工来源资产，不能因文件名和 EXIF 相同猜选哪个。
        with self.archive.connect() as c:c.execute('DELETE FROM catalog_links WHERE session=?',(sid2,))
        rawsid=self.scan(self.raw);before=sha(self.target);self.link.process_session(rawsid);pair=self.link.state(rawsid)['pairs'][0]
        self.assertEqual(pair['status'],'review',pair);self.assertEqual(sha(self.target),before)
        self.link.review(pair['id'],self.sid,self.cid)
        for _ in range(4):self.archive.process_one();self.link.refresh_completed()
        self.assertEqual(self.link.state(rawsid)['pairs'][0]['status'],'applied')
    def test_zero_rating_and_reject_are_explicit_not_tail_rule(self):
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides'][self.cid]=0;self.store.save(self.sid,session['revision'],state)
        sid=self.scan(self.raw);pair=self.finish(sid);self.assertEqual(pair['status'],'applied',pair);self.assertEqual(self.archive.writer.read(self.target)['XMP-xmp:Rating'],0)
    def test_final_archive_paired_raw_jpg_syncs_after_source_deletion(self):
        companion=self.raw/'IMG_0346.JPG';shutil.copy2(self.jpg/'IMG_0346.JPG',companion);(self.jpg/'IMG_0346.JPG').unlink()
        target_id=self.link.add_target(str(self.raw))['target_id'];self.link.sync_target(target_id)
        import time
        for _ in range(300):
            self.link.tick();self.archive.process_one();self.link.tick()
            target=self.link.targets()[0]
            if target['status'] in ('completed','failed'):break
            time.sleep(.02)
        pair=self.link.state(target['session'])['pairs'][0];self.assertEqual(pair['status'],'applied',pair)
        files=self.link.task_state()['batches'][0]['files'];self.assertEqual({f['format'] for f in files},{'JPG','RAW'})
        self.assertTrue(all(f['source_name']=='IMG_0346.JPG' and str(self.jpg) in f['source_path'] for f in files),files)
        self.assertEqual(len(self.archive.operation(pair['operation'])['jobs']),2)
        for path in (self.target,companion):
            fields=self.archive.writer.read(path);self.assertEqual(fields['XMP-xmp:Rating'],4);self.assertEqual(fields['XMP-fk:SKU'],'SKU-远程123')
        before=[sha(p) for p in (self.target,companion)];self.link.process_session(target['session']);self.assertEqual([sha(p) for p in (self.target,companion)],before)
    def test_existing_raw_rating_conflict_requires_explicit_overwrite(self):
        # 只制造星级冲突，不引入另一永久资产身份（身份冲突不能被星级覆盖授权绕过）。
        temp,target,journal=self.archive.writer.write(self.target,{'Rating':5},sha(self.target));temp.replace(target)
        sid=self.scan(self.raw);self.link.process_session(sid);pair=self.link.state(sid)['pairs'][0];self.assertEqual(pair['status'],'review',pair)
        self.link.review(pair['id'],self.sid,self.cid,overwrite=True)
        for _ in range(4):self.archive.process_one();self.link.refresh_completed()
        self.assertEqual(self.link.state(sid)['pairs'][0]['status'],'applied',self.link.state(sid))
        self.assertEqual(self.archive.writer.read(self.target)['XMP-xmp:Rating'],4)
    def test_later_jpg_rating_change_is_written_without_replacing_sku(self):
        sid=self.scan(self.raw);self.assertEqual(self.finish(sid)['status'],'applied')
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides'][self.cid]=5;self.store.save(self.sid,session['revision'],state)
        self.link.process_session(self.sid);pair=self.finish(sid);self.assertEqual(pair['status'],'applied',pair)
        fields=self.archive.writer.read(self.target);self.assertEqual(fields['XMP-xmp:Rating'],5);self.assertEqual(fields['XMP-fk:SKU'],'SKU-远程123')
    def test_failed_raw_validation_does_not_touch_file_and_retry_replans(self):
        sid=self.scan(self.raw);self.link.process_session(sid);before=sha(self.target)
        with patch.object(self.archive.writer,'write',side_effect=ValueError('RAW 校验失败')):self.archive.process_one()
        self.link.refresh_completed();pair=self.link.state(sid)['pairs'][0];self.assertEqual(pair['status'],'failed');self.assertEqual(sha(self.target),before)
        self.link.retry(pair['id']);self.assertEqual(self.finish(sid)['status'],'applied')
    def test_new_copy_in_already_selected_folder_and_internal_files_ignored(self):
        sid=self.scan(self.raw);self.finish(sid)
        staged=self.raw/'.framekeep-write-test';staged.mkdir();(staged/'fake.CR3').write_bytes(b'internal temporary')
        (self.raw/'.framekeep-legacy.CR3').write_bytes(b'internal temporary')
        _,shots=media.discover(self.raw);self.assertEqual(len(shots),1)
        self.target.unlink();shutil.copy2(SAMPLE,self.target)
        self.link.discover_raws(sid);pair=self.finish(sid);self.assertEqual(pair['status'],'applied',pair);self.assertEqual(self.archive.writer.read(self.target)['XMP-xmp:Rating'],4)

if __name__=='__main__':unittest.main()
