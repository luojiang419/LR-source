"""混入独立档案的追加接续，保留原提交、照片及二星，拒绝冲突与损坏。"""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

from PIL import Image
from portable_capture_store import Dataset,DatasetBusy,digest,encoded
from portable_capture_recovery import recover,is_recovered


class ForeignRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.root=self.base/'photos';self.root.mkdir();self.incoming=self.base/'incoming';self.incoming.mkdir()
        self.a=Dataset(self.root,self.base/'A');self.b=Dataset(self.root,self.base/'B')
        self.source=Dataset(self.incoming,self.base/'source')
        records,groups=self.make_group(self.root,['old.jpg'],'OLD')
        self.a.acquire();self.a.change(records,groups,kind='live_confirm');self.a.release()
        self.records,self.groups=self.make_group(self.incoming,['new.jpg','label.jpg'],'LC7715075')
        self.source.acquire();self.source.change(self.records,self.groups,kind='live_confirm');self.source.release()
        self.commit_path=next((self.source.folder/'commits').glob('*.json'))
        self.commit=json.loads(self.commit_path.read_text(encoding='utf8'))
        for directory in ('events','commits'):
            for file in (self.source.folder/directory).glob('*.json'):shutil.copy2(file,self.a.folder/directory/file.name)
        for record in self.records:shutil.copy2(self.incoming/record['relative_path'],self.root/record['relative_path'])

    def tearDown(self):
        self.source.close();self.b.close();self.a.close();self.temp.cleanup()

    @staticmethod
    def make_group(root,names,sku):
        gid=uuid.uuid4().hex;decision=uuid.uuid4().hex;rows=[]
        for n,name in enumerate(names):
            path=root/name;Image.new('RGB',(20,30),(20+n*30,90,120)).save(path)
            aid=uuid.uuid4().hex;fid=uuid.uuid4().hex;label=n==len(names)-1;stat=path.stat()
            rows.append({'id':uuid.uuid4().hex,'file_id':fid,'asset_id':aid,'relative_path':name,'sku':sku,
                'media_type':'image','rating':2 if label else 0,'boundary':label,'role':'sku_label' if label else 'product',
                'decision_id':decision,'group_id':gid,'group_revision':decision,'assignment_state':'confirmed',
                'signature':[stat.st_size,stat.st_mtime_ns],'file_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'write_state':'pending'})
        members=sorted(r['asset_id'] for r in rows);files=sorted(r['file_id'] for r in rows)
        g={'id':gid,'sku':sku,'label':rows[-1]['asset_id'],'members':members,'status':'confirmed',
           'expected_files':len(rows),'expected_labels':1,'expected_products':len(rows)-1,
           'members_digest':digest(encoded(members)),'files_digest':digest(encoded(files))}
        return rows,[g]

    def test_multiple_sources_read_without_writing_shared_files_then_append_when_editing(self):
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.b.refresh()
        self.assertEqual(len(self.b.records()),3)
        self.assertEqual(self.b.record(path='label.jpg')['rating'],2)
        self.assertFalse(self.b.editing)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.assertFalse((self.a.folder/'recovered').exists())
        result=recover(self.b,self.commit_path.name)
        self.assertEqual((result['records'],result['groups'],result['target_revision']),(2,1,2))
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.assertEqual(self.b.record(path='label.jpg')['rating'],2)
        self.assertEqual(self.b.record(path='label.jpg')['sku'],'LC7715075')
        self.assertTrue(is_recovered(self.b,self.commit))
        self.a.refresh();self.assertEqual(len(self.a.records()),3)
        self.a.acquire();self.assertTrue(self.a.editing);self.a.release()

    def test_later_complete_confirmation_from_same_source_can_join_without_revision_collision(self):
        recover(self.b,self.commit_path.name)
        rows,groups=self.make_group(self.incoming,['later-product.jpg','later-label.jpg'],'NEXT-CAMERA')
        self.source.acquire();self.source.change(rows,groups,kind='live_confirm');self.source.release()
        for directory in ('events','commits'):
            for path in (self.source.folder/directory).glob('*.json'):
                target=self.a.folder/directory/path.name
                if not target.exists():shutil.copy2(path,target)
        for r in rows:shutil.copy2(self.incoming/r['relative_path'],self.root/r['relative_path'])
        self.b.refresh();self.assertEqual(len(self.b.records()),5)
        self.assertEqual(int(self.b.meta('revision')),2)
        self.b.acquire();self.assertEqual(int(self.b.meta('revision')),3);self.b.release()
        self.assertEqual(self.b.record(path='later-label.jpg')['rating'],2)

    def test_new_reader_recovers_snapshot_and_repeat_is_idempotent(self):
        recover(self.b,self.commit_path.name)
        fresh=Dataset(self.root,self.base/'fresh')
        try:
            fresh.refresh();self.assertEqual(len(fresh.groups()),2)
            self.assertEqual(fresh.record(path='label.jpg')['rating'],2)
            self.assertFalse(recover(fresh,self.commit_path.name)['recovered'])
            self.assertEqual(int(fresh.meta('revision')),2)
        finally:fresh.close()

    def test_corrupt_source_hash_is_rejected(self):
        p=self.a.folder/'events'/self.commit['event'];p.write_bytes(p.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'摘要不一致'):recover(self.b,self.commit_path.name)
        self.assertFalse((self.a.folder/'recovered').exists())

    def test_modified_photo_is_rejected_before_appending_history(self):
        path=self.root/'new.jpg';path.write_bytes(path.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'图片内容'):recover(self.b,self.commit_path.name)
        self.assertEqual(len(list((self.a.folder/'commits').glob('*.json'))),2)

    def test_path_or_identity_collision_is_rejected(self):
        self.a.ownership.acquire()
        try:
            record=self.a.record(path='old.jpg')
            with self.a.connect() as c:
                altered={**record,'relative_path':'new.jpg'}
                c.execute('UPDATE records SET path=?,data=? WHERE id=?',('new.jpg',encoded(altered).decode(),record['id']))
            # 直接本地制造碰撞，再用相同工作库执行恢复。
        finally:self.a.ownership.release()
        with self.assertRaisesRegex(ValueError,'冲突'):recover(self.a,self.commit_path.name)

    def test_incomplete_manifest_is_rejected(self):
        p=self.a.folder/'events'/self.commit['event'];event=json.loads(p.read_text());event['groups'][0]['expected_files']=99
        content=encoded(event);p.write_bytes(content);self.commit['sha256']=digest(content)
        (self.a.folder/'commits'/self.commit_path.name).write_bytes(encoded(self.commit))
        with self.assertRaisesRegex(ValueError,'区间成员'):recover(self.b,self.commit_path.name)

    def test_interrupted_marker_publish_reuses_canonical_recovery_event(self):
        import portable_capture_recovery as recovery
        original=recovery.publish_file
        def fail_marker(path,content):
            if path.parent.name=='recovered':raise OSError('模拟标记发布中断')
            return original(path,content)
        with patch.object(recovery,'publish_file',side_effect=fail_marker):
            with self.assertRaisesRegex(OSError,'发布中断'):recover(self.b,self.commit_path.name)
        self.assertEqual(int(self.b.meta('revision')),2)
        recover(self.b,self.commit_path.name)
        self.assertEqual(int(self.b.meta('revision')),2)
        self.assertEqual(len(list((self.a.folder/'commits').glob('*.json'))),3)

    def test_tampered_marker_cannot_bypass_foreign_identity_guard(self):
        recover(self.b,self.commit_path.name)
        p=next((self.a.folder/'recovered').glob('*.json'));value=json.loads(p.read_text());value['target']['sha256']='0'*64;p.write_bytes(encoded(value))
        with self.assertRaisesRegex(ValueError,'摘要不一致'):self.a.refresh()

    def test_other_editor_is_never_forced_out(self):
        self.a.ownership.acquire()
        try:
            with self.assertRaises(DatasetBusy):recover(self.b,self.commit_path.name)
        finally:self.a.ownership.release()


if __name__=='__main__':unittest.main()
