"""常驻元数据通道与原CLI等价、失败恢复、缩略图完整性。"""
import queue
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from PIL import Image
from metadata_writer import MetadataWriter,jpeg_thumbnail,sha,field_text
from metadata_memory import MemoryEditor

ROOT=Path(__file__).resolve().parents[1]


class MetadataLatencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.data=Path(self.tmp.name)/'中文资料';self.data.mkdir()
        self.writer=MetadataWriter(ROOT,self.data);self.photo=self.data/'照片.JPG'
        Image.new('RGB',(160,120),'teal').save(self.photo)

    def tearDown(self):self.writer.close();self.tmp.cleanup()

    def test_numeric_sku_windows_properties_write_verify_and_undo(self):
        for code in ('7876953','0','00123'):
            with self.subTest(code=code):
                Image.new('RGB',(160,120),'teal').save(self.photo)
                self.photo.write_bytes(self.writer.edit(self.photo,['-XPSubject=0','-XPKeywords=123456']))
                with Image.open(self.photo) as image:pixels=image.tobytes()
                payload,target,journal=self.writer.write(self.photo,{'SKU':code},sha(self.photo))
                self.writer.commit(payload,target,journal);self.writer.commit_sidecars(journal);self.writer.verify_committed(journal)
                actual=self.writer.read(self.photo)
                self.assertEqual(field_text(actual['IFD0:XPSubject']),code)
                self.assertEqual(actual['IFD0:XPKeywords'],'123456;SKU:'+code)
                self.assertEqual(journal['before']['WindowsSubject'],'0')
                payload,target,undo=self.writer.write(self.photo,{'SKU':code},sha(self.photo),restore=journal['before'],applied=journal['after'],sidecars_restore=journal.get('sidecars'))
                self.writer.commit(payload,target,undo);self.writer.commit_sidecars(undo);self.writer.verify_committed(undo)
                restored=self.writer.read(self.photo)
                self.assertEqual(field_text(restored['IFD0:XPSubject']),'0')
                self.assertEqual(field_text(restored['IFD0:XPKeywords']),'123456')
                with Image.open(self.photo) as image:self.assertEqual(image.tobytes(),pixels)

    def test_numeric_windows_property_tampering_still_rejects_verification_and_undo(self):
        payload,target,journal=self.writer.write(self.photo,{'SKU':'7876953'},sha(self.photo))
        self.writer.commit(payload,target,journal)
        self.photo.write_bytes(self.writer.edit(self.photo,['-XPSubject=9999999']))
        with self.assertRaisesRegex(ValueError,'Windows 图片属性不一致'):
            self.writer.verify_committed(journal)
        before=self.photo.read_bytes()
        with self.assertRaisesRegex(ValueError,'Windows 属性已被后来修改'):
            self.writer.write(self.photo,{'SKU':'7876953'},sha(self.photo),restore=journal['before'],applied=journal['after'])
        self.assertEqual(self.photo.read_bytes(),before)

    def test_sku_text_is_literal_in_memory_and_disk_readers(self):
        for code in ('中文款号','Abc 混合 12','123.00','1E3','TRUE','false','00123','0'):
            with self.subTest(code=code):
                payload,target,journal=self.writer.write(self.photo,{'SKU':code,'Rating':2},sha(self.photo))
                for cli in (False,True):
                    with patch.object(self.writer,'_memory_edit',return_value=None) if cli else patch.object(self.writer,'metadata_workers',self.writer.metadata_workers):
                        actual=self.writer.read(payload)
                    self.assertEqual(actual['XMP-fk:SKU'],code)
                    self.assertEqual(actual['IFD0:XPSubject'],code)
                    self.assertEqual(actual['XMP-xmp:Rating'],2)
                self.writer.commit(payload,target,journal);self.writer.commit_sidecars(journal);self.writer.verify_committed(journal)
                actual=self.writer.read(self.photo)
                self.assertEqual(actual['XMP-fk:SKU'],code)
                self.assertEqual(actual['IFD0:XPSubject'],code)

    def test_memory_read_matches_cli_and_does_not_spawn_per_photo(self):
        import json
        payload=self.writer.edit(self.photo,['-XMP-fk:SKU=LC123-P905','-XMP-dc:Subject=中文','-XMP-dc:Subject=SKU:LC123-P905','-XPSubject=中文款号'])
        expected=json.loads(self.writer.binary('-G1','-s','-json','-all:all','-',input_data=payload))[0]
        with patch.object(self.writer,'binary',side_effect=AssertionError('内存回读不应启动CLI')):
            actual=self.writer.read(payload,full=True)
        clean=lambda value:{k:v for k,v in value.items() if not k.startswith('System:')}
        self.assertEqual(clean(actual),clean(expected))

    def test_cached_input_does_not_hide_external_replacement_before_commit(self):
        import io
        stream=io.BytesIO();Image.new('RGB',(160,120),'red').save(stream,format='JPEG');replacement=stream.getvalue()
        before=sha(self.photo);edit=self.writer.edit
        def replaced(target,args):
            result=edit(target,args);self.photo.write_bytes(replacement);return result
        with patch.object(self.writer,'edit',side_effect=replaced):
            with self.assertRaisesRegex(ValueError,'外部修改|其他程序修改'):self.writer.write(self.photo,{'SKU':'CACHE-100'},before)
        self.assertEqual(self.photo.read_bytes(),replacement)

    def test_legacy_sku_keywords_are_replaced_without_losing_user_keywords_or_undo(self):
        payload=self.writer.edit(self.photo,['-XMP-dc:Subject=保留标签','-XMP-dc:Subject=SKU:OLD-100','-XPSubject=原主题','-XPKeywords=手动标签;SKU:OLD-100'])
        self.photo.write_bytes(payload);before=self.writer.read(self.photo)
        payload,target,journal=self.writer.write(self.photo,{'SKU':'NEW-200','ShootType':'现场记录'},sha(self.photo))
        self.writer.commit(payload,target,journal);self.writer.verify_committed(journal)
        after=self.writer.read(self.photo)
        self.assertEqual(after['XMP-dc:Subject'],['保留标签','SKU:NEW-200','ShootType:现场记录'])
        self.assertEqual(after['IFD0:XPKeywords'],'手动标签;SKU:NEW-200')
        payload,target,restore=self.writer.write(self.photo,{'SKU':'NEW-200'},sha(self.photo),restore=journal['before'],applied=journal['after'])
        self.writer.commit(payload,target,restore);restored=self.writer.read(self.photo)
        for key in ('XMP-dc:Subject','IFD0:XPSubject','IFD0:XPKeywords'):self.assertEqual(restored[key],before[key])

    def test_memory_edit_matches_cli_for_unicode_lists_and_deletion(self):
        args=['-XMP-fk:SKU=LC123-P905','-XMP-fk:ShootType=模特图','-XPSubject=中文款号',
              '-XMP-dc:Subject=保留关键词','-XMP-dc:Subject=SKU:LC123-P905','-XMP-xmp:Rating=2']
        original=self.photo.read_bytes()
        with patch.object(MemoryEditor,'available',return_value=False):legacy=self.writer.edit(self.photo,args)
        fast=self.writer.edit(self.photo,args)
        self.assertEqual(self.writer.read(legacy,True),self.writer.read(fast,True))
        self.assertEqual(self.photo.read_bytes(),original)
        text=self.data/'说明.txt';text.write_text('中文\n第二行',encoding='utf-8')
        self.photo.write_bytes(fast)
        args=['-XMP-fk:SKU=','-XMP-dc:Title-x-default<='+str(text),'-XPSubject=']
        with patch.object(MemoryEditor,'available',return_value=False):legacy=self.writer.edit(self.photo,args)
        fast=self.writer.edit(self.photo,args)
        self.assertEqual(self.writer.read(legacy,True),self.writer.read(fast,True))

    def test_process_reuse_close_and_reopen(self):
        for _ in range(8):self.writer.read(self.photo)
        processes=[s._process for s in self.writer._read_sessions]
        self.assertEqual(len(processes),4);self.assertTrue(all(p and p.poll() is None for p in processes))
        for n in range(5):self.writer.edit(self.photo,[f'-XMP-xmp:Rating={n}'])
        processes += [e.process for e in self.writer._edit_sessions]
        self.assertEqual(len(processes),6)
        self.writer.close();self.assertTrue(all(p.poll() is not None for p in processes))
        self.writer.read(self.photo);self.writer.edit(self.photo,['-XMP-xmp:Rating=2'])

    def test_idle_workers_do_not_lock_the_user_data_directory(self):
        self.writer.read(self.photo);self.writer.edit(self.photo,['-XMP-xmp:Rating=2'])
        moved=self.data.with_name('已迁移资料');self.data.rename(moved);moved.rename(self.data)

    def test_invalid_edit_does_not_poison_next_request_or_touch_original(self):
        original=self.photo.read_bytes()
        with self.assertRaises(ValueError):self.writer.edit(self.photo,['-NotARealTag=broken'])
        result=self.writer.edit(self.photo,['-XMP-xmp:Rating=3'])
        self.assertEqual(self.writer.read(result)['XMP-xmp:Rating'],3)
        self.assertEqual(self.photo.read_bytes(),original)

    def test_stalled_input_pipe_has_bounded_timeout_and_releases_process(self):
        editor=MemoryEditor(self.writer._exiftool(),self.writer.config,timeout=.2)
        script=self.data/'stalled.pl';script.write_text('sleep 30;',encoding='ascii');editor.script=script
        start=time.monotonic()
        try:
            with self.assertRaises(queue.Empty):editor.edit(['-XMP-xmp:Rating=2'],b'x'*1024*1024)
            self.assertGreaterEqual(time.monotonic()-start,.15)
            self.assertLess(time.monotonic()-start,3);self.assertIsNone(editor.process)
        finally:editor.close()

    def test_thumbnail_ranges_are_checked_and_corrupt_content_cannot_be_accepted(self):
        image=b'\xff\xd8'+b'header'+b'\xff\xd8thumb\xff\xd9'+b'end'
        meta={'IFD1:ThumbnailOffset':8,'IFD1:ThumbnailLength':9}
        self.assertEqual(jpeg_thumbnail(image,meta),b'\xff\xd8thumb\xff\xd9')
        self.assertIsNone(jpeg_thumbnail(image,dict(meta,**{'IFD1:ThumbnailLength':100})))
        self.assertIsNone(jpeg_thumbnail(image,dict(meta,**{'IFD1:ThumbnailOffset':-1})))
        self.assertIsNone(jpeg_thumbnail(image.replace(b'thumb\xff\xd9',b'thumbXX'),meta))

    def test_real_thumbnail_change_is_rejected_before_any_commit(self):
        example=ROOT/'错误/例图/XSY_9291.JPG'
        if not example.exists():example=ROOT/'build/field-trial/full/photos/XSY_2297.JPG'
        if not example.exists():self.skipTest('本机真实缩略图样本不存在')
        self.photo.write_bytes(example.read_bytes());original=self.photo.read_bytes()
        extraction=jpeg_thumbnail;calls=0
        def corrupt(source,metadata):
            nonlocal calls
            calls+=1
            value=extraction(source,metadata)
            return value[:-3]+b'X'+value[-2:] if calls==2 and value else value
        with patch('metadata_writer.jpeg_thumbnail',side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,'缩略图'):self.writer.write(self.photo,{'SKU':'THUMB-123'},sha(self.photo))
        self.assertEqual(self.photo.read_bytes(),original)

if __name__=='__main__':unittest.main()
