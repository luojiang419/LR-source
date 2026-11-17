"""后期导出移除MakerNotes：唯一标准EXIF身份回连，冲突不可猜配。"""
from contextlib import closing
from pathlib import Path
import json,sqlite3,unittest
from PIL import Image
from sku_folder_archive import camera_metadata,capture_key,capture_moment_key,match_capture,image_pixels
import test_sku_archive_exclusions as fixtures


class ExportIdentityTests(unittest.TestCase):
    def setUp(self):
        self.meta={'model':'Canon EOS R6m2','serial':'364022008657','time':'2026:11:04 10:05:47',
                   'combined':'2026:11:04 10:05:47.11+08:00','subsecond':'11','number':'127772'}
        self.ref={'fingerprint':capture_key(self.meta),'sku':'LC7877836-P1'}
        self.by_key={self.ref['fingerprint']:[self.ref]};self.by_moment={capture_moment_key(self.meta):[self.ref]}

    def test_missing_makernotes_returns_original_full_identity(self):
        exported={**self.meta,'number':''}
        key,candidates,method=match_capture(exported,self.by_key,self.by_moment,exported_jpg=True)
        self.assertEqual(key,self.ref['fingerprint']);self.assertEqual(candidates,[self.ref])
        self.assertEqual(method,'exported_jpg_metadata')
        with self.assertRaises(ValueError):capture_key(exported)

    def test_same_moment_two_shutters_blocks_even_when_sku_same(self):
        other={**self.ref,'fingerprint':capture_key({**self.meta,'number':'127773'})}
        self.by_moment[capture_moment_key(self.meta)].append(other)
        with self.assertRaisesRegex(ValueError,'多次快门'):
            match_capture({**self.meta,'number':''},self.by_key,self.by_moment,exported_jpg=True)

    def test_present_counter_is_never_overridden_by_time(self):
        _,candidates,method=match_capture({**self.meta,'number':'127773'},self.by_key,self.by_moment,exported_jpg=True)
        self.assertFalse(candidates);self.assertEqual(method,'capture_metadata')

    def test_raw_still_requires_full_identity(self):
        with self.assertRaises(ValueError):match_capture({**self.meta,'number':''},self.by_key,self.by_moment)

    def test_missing_serial_or_original_subseconds_blocks(self):
        for change in ({'serial':''},{'combined':'','subsecond':''},{'time':'2026:02:30 10:05:47','combined':''}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                match_capture({**self.meta,'number':'',**change},self.by_key,self.by_moment,exported_jpg=True)

    def test_adobe_standard_fields_and_zero_image_number(self):
        metadata=camera_metadata({'XMP-tiff:Model':self.meta['model'],'XMP-aux:SerialNumber':364022008657,
          'XMP-exif:DateTimeOriginal':self.meta['combined'],'XMP-aux:ImageNumber':0})
        self.assertFalse(metadata['number'])
        key,_,_=match_capture(metadata,self.by_key,self.by_moment,exported_jpg=True)
        self.assertEqual(key,self.ref['fingerprint'])
        with self.assertRaises(ValueError):capture_moment_key(camera_metadata({'IFD0:Model':self.meta['model'],
          'ExifIFD:LensSerialNumber':364022008657,'Composite:SubSecDateTimeOriginal':self.meta['combined']}))

    def test_subsecond_leading_zero_keeps_distinct_capture_moments(self):
        a={**self.meta,'combined':'2026:11:04 10:05:47.08'};b={**a,'combined':'2026:11:04 10:05:47.8'}
        self.assertNotEqual(capture_moment_key(a),capture_moment_key(b))


class ExportArchiveTests(unittest.TestCase):
    setUp=fixtures.ExclusionTests.setUp

    def test_retouched_renamed_jpg_uses_identity_without_repeated_full_read(self):
        self.kept.pop('Canon:ShutterCount');self.kept['XMP-aux:ImageNumber']=0
        (self.final/'kept.JPG').unlink();exported=self.final/'renamed-retouched.JPG'
        Image.new('RGB',(30,40),'green').save(exported)
        plan=self.core.plan(self.small,self.final,self.target,preprocess=False,fast=True)
        self.assertEqual(plan['expected'],1);self.assertEqual(plan['excluded_count'],2)
        row=plan['items'][0];self.assertEqual(row['match_method'],'exported_jpg_metadata')
        with closing(sqlite3.connect(self.db)) as c:original_key=c.execute('SELECT fingerprint FROM captures').fetchone()[0]
        self.assertEqual(row['metadata_fingerprint'],original_key)
        reads=[c for c in self.writer.read.call_args_list if c.args and c.args[0]==exported]
        self.assertEqual(len(reads),1)
        result=self.core.execute(plan);destination=result['items'][0]['destination']
        self.assertEqual(image_pixels(exported),image_pixels(destination))
        self.assertNotEqual(image_pixels(self.small/'kept.JPG'),image_pixels(destination))
        self.assertFalse(list(self.target.rglob('*.xmp')))

    def test_ambiguous_reference_shutters_block_planning(self):
        metadata=camera_metadata(self.kept);other={**metadata,'number':str(int(metadata['number'])+1)}
        second=self.small/'second.JPG';second.write_bytes((self.small/'kept.JPG').read_bytes())
        with closing(sqlite3.connect(self.db)) as c,c:
            c.execute('INSERT INTO captures VALUES(?,?,?,?,?,?,?,?)',
              ('two',str(second),capture_key(other),'LC7877847','细节图',0,json.dumps(other),''))
        self.kept.pop('Canon:ShutterCount')
        with self.assertRaisesRegex(ValueError,'多次快门'):self.core.plan(self.small,self.final,self.target,preprocess=False,fast=True)
        self.assertFalse(list(self.target.rglob('*.JPG')))

    def test_strict_export_archive_and_existing_target_verification(self):
        self.kept.pop('Canon:ShutterCount')
        first=self.core.execute(self.core.plan(self.small,self.final,self.target,preprocess=False))
        self.assertEqual(first['verified'],1)
        again=self.core.execute(self.core.plan(self.small,self.final,self.target,preprocess=False))
        self.assertEqual(again['items'][0]['status'],'identical_verified_skip')

    def test_export_target_with_changed_camera_identity_is_rejected(self):
        self.kept.pop('Canon:ShutterCount')
        result=self.core.execute(self.core.plan(self.small,self.final,self.target,preprocess=False,fast=True))
        self.kept['ExifIFD:SerialNumber']='999999'
        with self.assertRaisesRegex(ValueError,'复制后相机身份改变'):self.core.verify_item(result['items'][0])


if __name__=='__main__':unittest.main()
