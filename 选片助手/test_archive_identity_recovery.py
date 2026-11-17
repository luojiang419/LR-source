import copy
import unittest
import uuid
from archive_identity_recovery import recover
from sku_folder_archive import camera_metadata,capture_key


class IdentityRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.gid=uuid.uuid4().hex;self.label=uuid.uuid4().hex
        self.full={'XMP-fk:FileId':str(uuid.uuid4()),'XMP-fk:AssetId':str(uuid.uuid4()),'XMP-fk:SegmentId':self.gid,
                   'XMP-fk:SKU':'SKU-1-A','XMP-fk:ShootType':'细节图','IFD0:Model':'Canon Test','Canon:SerialNumber':'12345',
                   'Canon:ShutterCount':123,'Composite:SubSecDateTimeOriginal':'2026:11:10 10:01:00.10+08:00'}
        self.old={'id':uuid.uuid4().hex,'file_id':uuid.uuid4().hex,'asset_id':uuid.uuid4().hex,'relative_path':'day/photo.jpg',
                  'media_type':'image','time':'2026-11-10T10:01:00.10+08:00','capture_identity':capture_key(camera_metadata(self.full)),
                  'sku':'','group_id':'','shoot_type':'未分类','assignment_state':'unassigned','identity_conflict':'FileId已变更'}
        label={'id':uuid.uuid4().hex,'asset_id':self.label,'relative_path':'day/label.jpg','media_type':'image','time':'2026-11-10T10:02:00.20+08:00',
               'sku':'SKU-1','group_id':self.gid,'shoot_type':'细节图','boundary':True,'rating':2,'camera':{'model':'Canon Test','serial':'12345'}}
        self.records=[self.old,label];self.groups=[{'id':self.gid,'label':self.label,'members':[self.label],'sku':'SKU-1','status':'confirmed'}]

    def test_bootstrap_upgrades_identity_and_uses_confirmed_interval(self):
        record,group=recover(self.old,self.full,self.records,self.groups)
        self.assertEqual(record['sku'],'SKU-1');self.assertEqual(record['shoot_type'],'细节图')
        self.assertEqual(record['file_id'],self.full['XMP-fk:FileId'].replace('-',''))
        self.assertIn(record['asset_id'],group['members']);self.assertNotIn('identity_conflict',record)
        self.assertEqual(record['write_state'],'pending')
        self.assertEqual(recover(self.old,{**self.full,'XMP-xmp:Rating':1},self.records,self.groups)[0]['rating'],1)
        self.assertIsNone(recover(self.old,{**self.full,'XMP-xmp:Rating':2},self.records,self.groups))

    def test_wrong_or_missing_shutter_identity_is_not_adopted(self):
        for changes in [{'Canon:ShutterCount':124},{'Canon:SerialNumber':''}]:
            with self.subTest(changes=changes):self.assertIsNone(recover(self.old,{**self.full,**changes},self.records,self.groups))

    def test_confirmed_cleared_or_branched_records_stay_protected(self):
        for changes in [{'sku':'OTHER'},{'decision_id':uuid.uuid4().hex},{'group_id':uuid.uuid4().hex},{'assignment_state':'cleared'}]:
            with self.subTest(changes=changes):self.assertIsNone(recover({**self.old,**changes},self.full,self.records,self.groups))
        self.assertIsNone(recover(self.old,{**self.full,'XMP-fk:DecisionId':uuid.uuid4().hex},self.records,self.groups))

    def test_wrong_group_type_scope_day_or_duplicate_identity_is_rejected(self):
        self.assertIsNone(recover(self.old,{**self.full,'XMP-fk:SegmentId':uuid.uuid4().hex},self.records,self.groups))
        self.assertIsNone(recover(self.old,{**self.full,'XMP-fk:ShootType':'上身图'},self.records,self.groups))
        self.assertIsNone(recover(self.old,{**self.full,'XMP-fk:CatalogScopeId':uuid.uuid4().hex},self.records,self.groups))
        for changes in [{'catalog_scope':'another'},{'time':'2026-11-11T10:02:00.20+08:00'},{'relative_path':'another/label.jpg'}]:
            records=copy.deepcopy(self.records);records[1].update(changes)
            old={**self.old,'catalog_scope':'one'}
            self.assertIsNone(recover(old,self.full,records,self.groups))
        records=self.records+[{'id':'other','file_id':self.full['XMP-fk:FileId'].replace('-','')}]
        self.assertIsNone(recover(self.old,self.full,records,self.groups))

    def test_photo_after_confirmed_label_is_not_in_its_interval(self):
        full={**self.full,'Composite:SubSecDateTimeOriginal':'2026:11:10 10:03:00.10+08:00'}
        old={**self.old,'capture_identity':capture_key(camera_metadata(full))}
        self.assertIsNone(recover(old,full,self.records,self.groups))


class RecoveryScanIntegrationTests(unittest.TestCase):
    def exercise(self,editing):
        from test_archive_capture_service import ArchiveCaptureTests
        from unittest.mock import patch
        fixture=ArchiveCaptureTests();fixture.setUp()
        try:
            fixture.api.rate(fixture.item(2),2,correction=True);fixture.drain()
            dataset=fixture.dataset;records=dataset.records();old=next(r for r in records if r['relative_path']=='IMG_0001.JPG')
            group=next(g for g in dataset.groups() if g['id']==old['group_id'])
            path=fixture.photos/'IMG_0001.JPG';before=path.read_bytes();full=fixture.archive.writer.read(path,full=True)
            allowed={'XMP-fk:FileId','XMP-fk:AssetId','XMP-fk:SKU','XMP-fk:SegmentId','XMP-fk:ShootType'}
            full={k:v for k,v in full.items() if not k.startswith('XMP-fk:') or k in allowed}
            full.update({'XMP-fk:ShootType':'细节图','IFD0:Model':'TestCam','Canon:SerialNumber':'12345','Canon:ShutterCount':101,'Composite:SubSecDateTimeOriginal':'2026:11:09 12:00:01.10'})
            original_read=fixture.archive.writer.read
            changed=[]
            for row in records:
                row=dict(row,shoot_type='细节图',camera={**row.get('camera',{}),'model':'TestCam','serial':'12345'})
                if row['id']==old['id']:
                    row.update(file_id=uuid.uuid4().hex,asset_id=uuid.uuid4().hex,sku='',group_id='',decision_id='',assignment_state='unassigned',
                               shoot_type='未分类',capture_identity=capture_key(camera_metadata(full)),identity_conflict='同名文件身份已改变')
                    row.pop('metadata_reader',None)
                changed.append(row)
            dataset.change(changed,[{**group,'members':[a for a in group['members'] if a!=old['asset_id']]}],kind='test_bootstrap')
            if not editing:dataset.release()
            def read(target,**kwargs):return dict(full) if target==path else original_read(target,**kwargs)
            with patch.object(fixture.archive.writer,'read',side_effect=read),patch.object(fixture.archive.writer,'read_asset',return_value={}):
                fixture.api._scan(dataset,focus=path)
            restored=dataset.record(path='IMG_0001.JPG')
            self.assertEqual(restored['sku'],group['sku']);self.assertEqual(restored['group_id'],group['id'])
            self.assertEqual(restored['shoot_type'],'细节图');self.assertNotIn('identity_conflict',restored)
            self.assertEqual(restored['file_id'],full['XMP-fk:FileId'].replace('-',''))
            self.assertEqual(path.read_bytes(),before,'扫描恢复不能直接重写照片')
            self.assertEqual(fixture.catalog.item(fixture.item(1))['sku'],group['sku'])
            self.assertIn(restored['asset_id'],next(g for g in dataset.groups() if g['id']==group['id'])['members'])
        finally:fixture.tearDown()

    def test_editing_scan_adopts_only_verified_bootstrap_identity(self):self.exercise(True)
    def test_readonly_scan_recovers_view_without_writing_original(self):self.exercise(False)
