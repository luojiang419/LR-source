"""父目录多日期来源：无关未备案照片不阻挡，最终选用者仍须确认SKU。"""
from contextlib import closing
from pathlib import Path
import io,json,sqlite3,unittest
from openpyxl import load_workbook
from PIL import Image
from metadata_writer import sha
from folder_archive_report import build_report
from sku_folder_archive import camera_metadata,capture_key
import test_sku_archive_exclusions as fixtures
import test_folder_archive_main as main_fixtures


class PendingReferenceTests(unittest.TestCase):
    setUp=fixtures.ExclusionTests.setUp

    def pending(self,metadata=None,rating=0):
        path=self.small/'later-day.JPG';Image.new('RGB',(30,40),'green').save(path)
        metadata=metadata or camera_metadata({**self.kept,'Canon:ShutterCount':9999})
        with closing(sqlite3.connect(self.db)) as c,c:
            c.execute('INSERT INTO captures VALUES(?,?,?,?,?,?,?,?)',('pending',str(path),capture_key(metadata),'','细节图',rating,json.dumps(metadata),''))
        return path

    def test_unrelated_pending_reference_is_audited_and_not_moved(self):
        pending=self.pending();before=sha(pending)
        plan=self.core.plan(self.small,self.final,self.target,fast=True)
        self.assertEqual(plan['unassigned_reference_count'],1)
        self.assertEqual(len(plan['reference_records']),1)
        self.assertEqual(plan['unassigned_reference_records'][0]['source'],str(pending))
        result=self.core.execute(plan)
        self.assertEqual(result['verified'],1);self.assertEqual(sha(pending),before)
        content,_=build_report(result)
        book=load_workbook(io.BytesIO(content));sheet=book['未选用来源记录']
        self.assertEqual(sheet['F6'].value,str(pending));self.assertIn('无关',sheet['H6'].value)
        self.assertFalse(sheet['G6'].value);self.assertFalse(sheet['A6'].value)

    def test_pending_reference_selected_by_final_blocks_when_exclusion_disabled(self):
        self.pending(camera_metadata(self.unknown))
        with self.assertRaisesRegex(ValueError,'本次最终大档对应的来源图片尚无确认SKU'):
            self.core.plan(self.small,self.final,self.target,fast=True,exclude_unregistered=False)
        self.assertFalse(list(self.target.rglob('*.JPG')))

    def test_pending_reference_is_excluded_and_preserved_with_raw_jpg_pair(self):
        pending=self.pending(camera_metadata(self.unknown));before=sha(pending)
        final_before={str(p):sha(p) for p in self.final.glob('unknown.*')}
        plan=self.core.plan(self.small,self.final,self.target,fast=True)
        self.assertEqual(plan['expected'],1);self.assertEqual(plan['excluded_count'],2)
        self.assertEqual(plan['unassigned_reference_count'],0)
        self.assertTrue(all(r['reference']==str(pending) for r in plan['excluded_records']))
        self.assertTrue(all('尚无确认SKU' in r['reason'] for r in plan['excluded_records']))
        self.assertEqual(sha(pending),before)
        self.assertEqual(final_before,{str(p):sha(p) for p in self.final.glob('unknown.*')})
        self.assertFalse(list(self.target.rglob('unknown.*')))
        content,_=build_report(plan);book=load_workbook(io.BytesIO(content))
        self.assertIn('尚无确认SKU',book['未入库排除记录']['J6'].value)

    def test_pending_reference_with_final_business_identity_still_blocks(self):
        self.pending(camera_metadata(self.unknown));self.unknown['XMP-fk:SKU']='LC7979103'
        with self.assertRaisesRegex(ValueError,'尚无确认SKU'):
            self.core.plan(self.small,self.final,self.target,fast=True)

    def test_mixed_confirmed_and_pending_reference_still_blocks(self):
        self.pending(camera_metadata(self.kept))
        with self.assertRaisesRegex(ValueError,'尚无确认SKU'):
            self.core.plan(self.small,self.final,self.target,fast=True)

    def test_pending_reference_with_asset_identity_still_blocks(self):
        import uuid
        self.pending(camera_metadata(self.unknown));self.unknown['XMP-fk:AssetId']=str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError,'尚无确认SKU'):
            self.core.plan(self.small,self.final,self.target,fast=True)

    def test_export_time_ambiguity_includes_unassigned_references(self):
        self.pending(camera_metadata({**self.kept,'Canon:ShutterCount':9999}))
        self.kept.pop('Canon:ShutterCount')
        with self.assertRaisesRegex(ValueError,'多次快门'):self.core.plan(self.small,self.final,self.target,fast=True)


class ParentSourceHttpTests(unittest.TestCase):
    setUp=main_fixtures.MainFolderArchiveTests.setUp
    tearDown=main_fixtures.MainFolderArchiveTests.tearDown
    request=main_fixtures.MainFolderArchiveTests.request
    wait=main_fixtures.MainFolderArchiveTests.wait

    def setup_loose(self):
        final=self.base/'最终';final.mkdir()
        writer=self.archive.writer
        for index,path in enumerate(self.small.glob('*.jpg')):
            writer.run('-overwrite_original','-Model=Camera Test','-SerialNumber=123456',
                       '-DateTimeOriginal=2026:11:04 10:00:00','-SubSecTimeOriginal=01',f'-ImageNumber={index+1}',path)
            (final/path.name).write_bytes(path.read_bytes())
        unknown=self.small/'2026-11-05'/'new.JPG';unknown.parent.mkdir();Image.new('RGB',(80,100),'green').save(unknown)
        writer.run('-overwrite_original','-Model=Camera Test','-SerialNumber=123456',
                   '-DateTimeOriginal=2026:11:05 10:00:00','-SubSecTimeOriginal=02','-ImageNumber=3',unknown)
        self.catalog._scan(self.root)
        with self.catalog.connect() as c:c.execute("UPDATE items SET sku='',source='unmatched' WHERE path=?",(str(unknown),))
        return final,unknown

    def prepare_loose(self,final,target):
        job=self.request('/api/folder-archive/prepare',{'root_id':self.root,'route':'loose','final_path':str(final),'target_path':str(target),'preprocess':True})
        return job

    def test_parent_root_second_archive_to_another_target(self):
        final,unknown=self.setup_loose();unknown_sha=sha(unknown)
        first=self.prepare_loose(final,self.target);first=self.wait(first['id'],('ready',))
        self.assertEqual(first['unassigned_reference_count'],1)
        self.request('/api/folder-archive/start',{'id':first['id']});self.wait(first['id'],('completed',))
        target2=self.base/'第二归档位置';second=self.prepare_loose(final,target2);second=self.wait(second['id'],('ready',))
        self.assertEqual(second['expected'],2);self.assertEqual(second['unassigned_reference_count'],1)
        self.request('/api/folder-archive/start',{'id':second['id']});second=self.wait(second['id'],('completed',))
        self.assertEqual(second['done'],2);self.assertEqual(sha(unknown),unknown_sha)
        for target in (self.target,target2):self.assertEqual(len(list(target.glob('LC78/LC7877847/*.jpg'))),2)

    def test_final_matching_unconfirmed_source_is_excluded_without_moving(self):
        final,unknown=self.setup_loose();(final/unknown.name).write_bytes(unknown.read_bytes())
        before=sha(unknown)
        job=self.prepare_loose(final,self.target);ready=self.wait(job['id'],('ready','failed'))
        self.assertEqual(ready['status'],'ready',ready.get('error'))
        self.assertEqual(ready['expected'],2);self.assertEqual(ready['excluded_count'],1)
        self.assertEqual(ready['unassigned_reference_count'],0)
        self.assertIn('尚无确认SKU',ready['excluded_records'][0]['reason'])
        self.assertTrue(unknown.exists());self.assertFalse(list(self.target.iterdir()))
        self.request('/api/folder-archive/start',{'id':job['id']})
        result=self.wait(job['id'],('completed','failed'))
        self.assertEqual(result['status'],'completed',result.get('error'))
        self.assertEqual(sha(unknown),before);self.assertTrue((final/unknown.name).exists())

    def test_unrelated_unconfirmed_two_star_label_is_not_supplemented(self):
        final,unknown=self.setup_loose();self.archive.writer.run('-overwrite_original','-XMP-xmp:Rating=2',unknown)
        job=self.prepare_loose(final,self.target);ready=self.wait(job['id'],('ready',))
        self.assertEqual(ready['expected'],2);self.assertEqual(ready['label_count'],0)
        self.assertEqual(ready['unassigned_reference_count'],1);self.assertTrue(unknown.exists())

    def test_missing_source_identity_is_not_silently_ignored(self):
        final,unknown=self.setup_loose();self.archive.writer.run('-overwrite_original','-SerialNumber=',unknown)
        job=self.prepare_loose(final,self.target);failed=self.wait(job['id'],('failed',))
        self.assertIn('来源相机身份字段不完整',failed['error'])


if __name__=='__main__':unittest.main()
