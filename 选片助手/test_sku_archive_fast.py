from contextlib import closing
from pathlib import Path
import io,json,sqlite3,threading,unittest
from unittest.mock import patch
from PIL import Image
from metadata_writer import sha
from jpeg_archive_integrity import jpeg_image_digest
import test_sku_archive_exclusions as exclusion_fixtures

class FastArchiveTests(unittest.TestCase):
    setUp=exclusion_fixtures.ExclusionTests.setUp

    def plan(self,preprocess=False):return self.core.plan(self.small,self.final,self.target,preprocess=preprocess,fast=True)

    def test_fast_preview_does_not_hash_image_contents(self):
        def reject_image(path):raise AssertionError('预览不应通读图片：'+str(path))
        with patch('sku_folder_archive.sha',side_effect=reject_image):plan=self.plan()
        self.assertTrue(plan['fast']);self.assertIsNone(plan['items'][0]['input_sha256'])
        self.assertIsInstance(next(iter(plan['manifest'].values())),list)

    def test_inventory_reuses_directory_entry_versions(self):
        from sku_archive_fast import inventory
        original=Path.stat
        def stat(path,*args,**kwargs):
            if path.is_relative_to(self.final) and path.suffix.lower() in ('.jpg','.cr3'):
                raise AssertionError('目录枚举已有版本信息，不应再逐图片stat')
            return original(path,*args,**kwargs)
        with patch.object(Path,'stat',stat):versions=inventory(self.final)
        self.assertEqual(len(versions),3)
        self.assertEqual(versions[str(self.final/'kept.JPG')][0],(self.final/'kept.JPG').stat().st_size)

    def test_parent_path_checks_are_shared_without_skipping_leaf_checks(self):
        from sku_archive_preprocess import FastPathChecks
        original=Path.lstat;calls=[]
        def lstat(path,*args,**kwargs):calls.append(path);return original(path,*args,**kwargs)
        checks=FastPathChecks()
        with patch.object(Path,'lstat',lstat):
            checks.inspect(self.final/'kept.JPG',self.final)
            checks.inspect(self.final/'unknown.JPG',self.final)
        self.assertEqual(calls.count(self.final),1)
        self.assertIn(self.final/'kept.JPG',calls);self.assertIn(self.final/'unknown.JPG',calls)

    def test_stream_copy_reads_source_once_and_reuses_receipt(self):
        plan=self.plan();source=self.final/'kept.JPG';calls=[];original=Path.open
        def opened(path,*args,**kwargs):
            if path==source and args and args[0]=='rb':calls.append(path)
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',opened):first=self.core.execute(plan)
        self.assertEqual(len(calls),1)
        self.assertEqual(first['verified'],1);self.assertEqual(first['excluded'],2)
        target=Path(first['items'][0]['destination']);before=target.read_bytes()
        second_plan=self.plan();calls.clear()
        with patch.object(Path,'open',opened):again=self.core.execute(second_plan)
        self.assertEqual(len(calls),0);self.assertEqual(target.read_bytes(),before)
        self.assertEqual(again['items'][0]['status'],'identical_verified_skip')
        self.assertEqual(again['items'][0]['archived_at'],first['items'][0]['archived_at'])

    def test_raw_is_untouched_and_xmp_written_without_raw_metadata_editor(self):
        raw=self.final/'kept.CR3';raw.write_bytes(b'real raw bytes fixture')
        before=sha(raw);plan=self.plan()
        original=self.writer.write
        def only_jpg(path,*args,**kwargs):
            self.assertNotEqual(Path(path).suffix.lower(),'.cr3');return original(path,*args,**kwargs)
        with patch.object(self.writer,'write',side_effect=only_jpg):result=self.core.execute(plan)
        row=next(r for r in result['items'] if r['raw'])
        self.assertEqual(sha(row['destination']),before);self.assertEqual(sha(raw),before)
        self.assertEqual(self.writer.read(row['xmp'])['XMP-fk:SKU'],'LC7877847')
        self.assertIn('同一快门',row['preview_note'])

    def test_raw_copy_corruption_is_caught_before_commit(self):
        raw=self.final/'kept.CR3';raw.write_bytes(b'raw-content');plan=self.plan()
        import sku_archive_fast
        original=sku_archive_fast.copy_stream
        def corrupt(writer,source,target,*args):
            digest=original(writer,source,target,*args)
            if source.suffix=='.CR3':target.write_bytes(b'corrupt-data')
            return digest
        with patch.object(sku_archive_fast,'copy_stream',side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,'RAW复制读回校验失败'):self.core.execute(plan)
        self.assertFalse(list(self.target.rglob('*.CR3')));self.assertEqual(raw.read_bytes(),b'raw-content')

    def test_raw_xmp_edits_are_preserved_and_changed_source_is_not_reused(self):
        (self.final/'kept.CR3').write_bytes(b'raw-content')
        side=self.final/'kept.xmp'
        side.write_text('''<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" crs:Exposure2012="+0.75"/></rdf:RDF></x:xmpmeta>''',encoding='utf8')
        result=self.core.execute(self.plan())
        row=next(r for r in result['items'] if r['raw'])
        metadata=self.writer.read(row['xmp'],full=True)
        self.assertEqual(float(metadata['XMP-crs:Exposure2012']),.75)
        before=Path(row['xmp']).read_bytes()
        side.write_text(side.read_text(encoding='utf8').replace('+0.75','+1.25'),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'来源XMP编辑已更新'):self.core.execute(self.plan())
        self.assertEqual(Path(row['xmp']).read_bytes(),before)

    def test_xmp_added_after_preview_blocks_copy(self):
        (self.final/'kept.CR3').write_bytes(b'raw-content');plan=self.plan()
        (self.final/'kept.xmp').write_text('<x/>',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'来源XMP在预览后新增或改变'):self.core.execute(plan)
        self.assertFalse(list(self.target.rglob('*.CR3')))

    def test_changed_inventory_blocks_before_any_move(self):
        plan=self.plan(True);(self.final/'kept.JPG').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'清单/版本'):self.core.execute(plan)
        self.assertTrue((self.small/'kept.JPG').exists());self.assertFalse(list(self.target.glob('LC*')))

    def test_fast_preprocess_uses_version_journal_and_recovers_without_content_reads(self):
        plan=self.plan(True);result=self.core.execute(plan)
        self.assertTrue((self.small/'LC78/LC7877847/kept.JPG').exists())
        self.assertEqual(result['verified'],1)
        log=next(self.root.glob('.归档前处理-*.json'));journal=json.loads(log.read_text(encoding='utf8'))
        self.assertTrue(all(row['sha256'] is None for row in journal['moves']))

    def test_completed_preprocessing_checkpoint_is_reused_without_moves(self):
        from sku_archive_preprocess import arrange_sources,preprocessing_complete
        result=self.core.execute(self.plan(True));count=len(list(self.root.glob('.归档前处理-*.json')))
        self.assertTrue(preprocessing_complete(result,self.db))
        with patch('sku_archive_preprocess.move_no_replace',side_effect=AssertionError('已完成整理不可重复移动')):
            again=arrange_sources(result,self.db)
        self.assertEqual(again['preprocess_journal'],result['preprocess_journal'])
        self.assertEqual(len(list(self.root.glob('.归档前处理-*.json'))),count)

    def test_changed_checkpoint_assignment_is_not_silently_reused(self):
        from sku_archive_preprocess import preprocessing_complete
        result=self.core.execute(self.plan(True))
        with closing(sqlite3.connect(self.db)) as c,c:c.execute("UPDATE captures SET sku='LC7999999'")
        with self.assertRaisesRegex(ValueError,'检查点与SKU库不一致'):preprocessing_complete(result,self.db)

    def test_copy_workers_overlap(self):
        (self.final/'kept2.JPG').write_bytes((self.final/'kept.JPG').read_bytes());plan=self.plan()
        import sku_archive_fast
        original=sku_archive_fast.copy_stream;barrier=threading.Barrier(2);lock=threading.Lock();active=0;peak=0
        def gated(*args):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            barrier.wait(timeout=5)
            try:return original(*args)
            finally:
                with lock:active-=1
        with patch.object(sku_archive_fast,'copy_stream',side_effect=gated):result=self.core.execute(plan)
        self.assertEqual(peak,2);self.assertEqual(result['verified'],2)

class JpegStreamTests(unittest.TestCase):
    def jpeg(self,color):
        output=io.BytesIO();Image.new('RGB',(100,80),color).save(output,'JPEG',progressive=True);return output.getvalue()
    def test_metadata_only_changes_preserve_encoded_digest(self):
        original=self.jpeg('red');app=b'Exif\0\0test';changed=original[:2]+b'\xff\xe1'+(len(app)+2).to_bytes(2,'big')+app+original[2:]
        self.assertEqual(jpeg_image_digest(original),jpeg_image_digest(changed))
    def test_actual_image_changes_are_detected(self):self.assertNotEqual(jpeg_image_digest(self.jpeg('red')),jpeg_image_digest(self.jpeg('blue')))
    def test_truncated_image_is_rejected(self):
        with self.assertRaises(ValueError):jpeg_image_digest(self.jpeg('red')[:-2])

if __name__=='__main__':unittest.main()
