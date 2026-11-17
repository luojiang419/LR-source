"""归档移动后拍摄证据、双图及旧版恢复，使用隔离照片与真实服务。"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image
from asset_catalog import AssetCatalog
from folder_archive_sources import FolderArchiveSources
from sku_service import SkuService
from sku_workbook import inspect_workbook
import test_ocr_metadata as fixture


class ArchiveShootingImagesTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose

    def prepare(self):
        self.parse();self.choose()
        while self.archive.process_one():pass
        self.catalog=AssetCatalog(self.store.data);self.addCleanup(self.catalog.close)
        self.sources=FolderArchiveSources(self.store,self.archive,self.catalog)
        return self.own()

    def own(self):return next(r for r in self.sku.state()['rows'] if r['sheet']=='模特图' and r['excel_row']==2)

    def move(self,legacy=False):
        folder=self.store.data/'folder-archive-jobs'/'fixture';folder.mkdir(parents=True)
        journal=folder/'moves.json';journal.write_text(json.dumps({'state':'completed'}),encoding='utf8')
        moves=[]
        for source in list(self.photos.glob('*.JPG')):
            s=source.stat();target=self.photos/'LC78'/fixture.CODE/source.name;target.parent.mkdir(parents=True,exist_ok=True)
            source.rename(target);moves.append({'before':str(source),'after':str(target),'signature':[s.st_size,s.st_mtime_ns]})
        plan={'preprocess_moves':moves,'preprocess_journal':str(journal)}
        (folder/'plan.json').write_text(json.dumps(plan),encoding='utf8')
        (folder/'paths-synchronized.json').write_text(json.dumps({'done':True,'journal':str(journal)}),encoding='utf8')
        with patch.object(self.archive,'sku',None if legacy else self.sku):self.sources.synchronize(plan)
        return plan,folder

    def reopen(self):
        self.sku.close();self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False);self.archive.sku=self.sku

    def assert_images(self,before):
        row=self.own();self.assertEqual(row['status'],'已拍摄');self.assertEqual(row['completed_at'],before['completed_at'])
        self.assertEqual(row['sku_image']['capture_id'],self.ids[0]);self.assertEqual(row['label_image']['capture_id'],self.ids[1])
        self.assertEqual(row['completion'],before['completion'])
        for field in ('sku_image','label_image'):
            image=row[field];record=self.store.records(image['session_id'],ids=[image['capture_id']])[0]
            path=self.photos/record['jpg'];self.assertTrue(path.is_file())
            with Image.open(path) as bitmap:bitmap.verify()
        return row

    def test_new_move_preserves_images_export_restart_and_replay(self):
        before=self.prepare();source=(self.sku.root/self.pid/'source.xlsx').read_bytes()
        plan,_=self.move();self.assert_images(before)
        self.sources.synchronize(plan);self.assert_images(before)
        output=self.sku.export(self.pid);pictures=inspect_workbook(output)[0]['images']
        self.assertTrue({'C2','D2'}.issubset({p['cell'] for p in pictures}))
        self.assertEqual(source,(self.sku.root/self.pid/'source.xlsx').read_bytes())
        self.reopen();self.assert_images(before)

    def test_legacy_move_restores_exact_versions_and_is_idempotent(self):
        before=self.prepare();self.move(legacy=True)
        with patch('folder_archive_shooting.recover_versions',return_value=0):self.assertEqual(self.own()['status'],'未拍摄')
        self.reopen();self.assert_images(before)
        with self.sku.connect() as c:count=c.execute('SELECT count(*) FROM metadata_versions').fetchone()[0]
        self.reopen();self.assert_images(before)
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM metadata_versions').fetchone()[0],count)

    def test_legacy_changed_photo_is_not_restored(self):
        self.prepare();self.move(legacy=True)
        with (self.photos/'LC78'/fixture.CODE/'1.JPG').open('ab') as stream:stream.write(b'changed')
        self.reopen();self.assertEqual(self.own()['status'],'未拍摄')

    def test_incomplete_journal_is_not_restored(self):
        self.prepare();_,folder=self.move(legacy=True)
        (folder/'moves.json').write_text(json.dumps({'state':'prepared'}),encoding='utf8')
        self.reopen();self.assertEqual(self.own()['status'],'未拍摄')

    def test_revoked_two_star_remains_unshot_after_recovery(self):
        self.prepare();self.move(legacy=True)
        session=self.store.session(self.sid);state=session['state'];state['overrides'][self.ids[1]]=0
        self.store.save(self.sid,session['revision'],state)
        self.reopen();self.assertEqual(self.own()['status'],'未拍摄')

    def test_version_is_saved_before_store_commit_and_survives_interruption(self):
        before=self.prepare();original=self.sku.metadata_changed;seen=[]
        def interrupted(sid,cid,old,new):
            original(sid,cid,old,new);seen.append((cid,old,new));raise RuntimeError('模拟版本保存后中断')
        with patch.object(self.sku,'metadata_changed',side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError,'模拟版本保存后中断'):self.move()
        cid,old,new=seen[0]
        self.assertEqual(self.store.records(self.sid,ids=[cid])[0]['fingerprint'],old)
        plan=json.loads((self.store.data/'folder-archive-jobs/fixture/plan.json').read_text(encoding='utf8'))
        self.sources.synchronize(plan);self.assert_images(before)


if __name__=='__main__':unittest.main()
