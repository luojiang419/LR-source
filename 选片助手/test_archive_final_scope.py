"""最终目录权威范围、二星标签与已整理母子SKU目录的HTTP执行回归。"""
from pathlib import Path
import json
import threading
import unittest
from unittest.mock import patch

from metadata_writer import sha
import test_rearchive_sources as fixtures


class FinalScopeTests(unittest.TestCase):
    setUp=fixtures.ParentSourceHttpTests.setUp
    tearDown=fixtures.ParentSourceHttpTests.tearDown
    request=fixtures.ParentSourceHttpTests.request
    wait=fixtures.ParentSourceHttpTests.wait
    setup_loose=fixtures.ParentSourceHttpTests.setup_loose

    def prepare(self,final,target=None,folder='',fast=True,route='loose'):
        job=self.request('/api/folder-archive/prepare',{
            'root_id':self.root,'folder':folder,'route':route,'final_path':str(final),
            'target_path':str(target or self.target),'preprocess':True,'fast_archive':fast})
        return self.wait(job['id'],('ready','failed'))

    def execute(self,job):
        self.assertEqual(job['status'],'ready',job.get('error'))
        self.request('/api/folder-archive/start',{'id':job['id']})
        result=self.wait(job['id'],('completed','failed'))
        self.assertEqual(result['status'],'completed',result.get('error'))
        return result

    def test_missing_small_label_is_not_added_or_moved(self):
        final,_=self.setup_loose()
        label=self.small/'照片1.jpg'
        self.archive.writer.run('-overwrite_original','-XMP-xmp:Rating=2',label)
        (final/label.name).unlink();before=sha(label)
        job=self.prepare(final);self.assertEqual(job['expected'],1)
        self.assertEqual(job['label_file_count'],0);self.assertEqual(job['label_count'],0)
        result=self.execute(job)
        self.assertEqual(result['done'],1);self.assertEqual(sha(label),before)
        self.assertFalse((self.small/'LC78/LC7877847'/label.name).exists())
        self.assertFalse((self.target/'LC78/LC7877847'/label.name).exists())

    def test_label_in_final_is_archived_and_counted(self):
        final,_=self.setup_loose();label=self.small/'照片1.jpg'
        self.archive.writer.run('-overwrite_original','-XMP-xmp:Rating=2',final/label.name)
        job=self.prepare(final);self.assertEqual(job['expected'],2)
        self.assertEqual(job['label_file_count'],1);self.assertEqual(job['label_count'],0)
        result=self.execute(job)
        labels=[r for r in result['items'] if r['photo_role']=='label']
        self.assertEqual(len(labels),1);self.assertEqual(labels[0]['origin'],'final')
        self.assertEqual(self.archive.writer.read(labels[0]['destination'])['XMP-xmp:Rating'],2)

    def test_arranged_small_and_final_reuse_all_selection_levels(self):
        final,unknown=self.setup_loose();unknown_before=sha(unknown)
        self.execute(self.prepare(final))
        before={str(p):sha(p) for root in (self.small,final) for p in root.rglob('*.jpg')}
        for index,(small_folder,final_folder) in enumerate((
            ('',''),('LC78','LC78'),('LC78/LC7877847','LC78/LC7877847'),
            ('','LC78/LC7877847'),('LC78/LC7877847',''))):
            with self.subTest(small=small_folder,final=final_folder):
                job=self.prepare(final/final_folder,self.base/f'重复归档{index}',small_folder,fast=index%2==0)
                result=self.execute(job);self.assertEqual(result['done'],2)
                plan=json.loads((self.http.folder_archives.root/job['id']/'plan.json').read_text(encoding='utf8'))
                self.assertEqual(plan['preprocess_moves'],[])
                self.assertTrue(all(Path(r['destination']).parent.name=='LC7877847' for r in result['items']))
        self.assertEqual(before,{str(p):sha(p) for root in (self.small,final) for p in root.rglob('*.jpg')})
        self.assertEqual(sha(unknown),unknown_before)

    def test_final_sku_outside_small_selection_uses_own_identity(self):
        final,_=self.setup_loose()
        for path in list(final.glob('*.jpg')):
            self.archive.writer.run('-overwrite_original','-ImageNumber=100',path)
            folder=final/'LC79/LC7979103';folder.mkdir(parents=True,exist_ok=True)
            path.rename(folder/path.name)
        for index,level in enumerate(('', 'LC79', 'LC79/LC7979103')):
            with self.subTest(level=level):
                job=self.prepare(final/level,self.base/f'独立大档{index}',fast=index%2==0)
                result=self.execute(job);self.assertEqual(result['done'],2)
                self.assertTrue(all(r['sku']=='LC7979103' for r in result['items']))
                self.assertTrue(all(r['match_method']=='confirmed_final_metadata' for r in result['items']))
                self.assertFalse(list(self.small.glob('LC78')))

    def test_final_export_without_shutter_uses_own_confirmed_sku(self):
        final,_=self.setup_loose()
        for path in final.glob('*.jpg'):
            self.archive.writer.run('-overwrite_original','-ImageNumber=','-DateTimeOriginal=2026:11:06 10:00:00',
                                    '-XMP-fk:SKU=LC7979103',path)
        job=self.prepare(final,fast=False);result=self.execute(job)
        self.assertEqual(result['done'],2)
        self.assertTrue(all(r['metadata_fingerprint'].startswith('capture-moment-v1:') for r in result['items']))

    def test_final_folder_conflicts_with_fingerprint_sku_blocks(self):
        final,_=self.setup_loose()
        folder=final/'LC79/LC7979103';folder.mkdir(parents=True)
        for path in final.glob('*.jpg'):path.rename(folder/path.name)
        job=self.prepare(final);self.assertEqual(job['status'],'failed')
        self.assertIn('款号目录与指纹匹配SKU冲突',job['error'])
        self.assertFalse(list(self.target.iterdir()))

    def test_child_sku_target_reuses_directory_and_existing_files(self):
        final,_=self.setup_loose();target=self.target/'LC78/LC7877847'
        first=self.execute(self.prepare(final,target))
        second=self.execute(self.prepare(final,target,folder='LC78/LC7877847'))
        self.assertTrue(all(Path(r['destination']).parent==target for r in first['items']))
        self.assertTrue(all(r['status']=='identical_verified_skip' for r in second['items']))
        self.assertEqual(len(list(target.glob('*.jpg'))),2)

    def test_direct_child_sku_folder_is_not_nested(self):
        child=self.small/'LC78/LC7877847';child.mkdir(parents=True)
        for path in self.small.glob('*.jpg'):path.rename(child/path.name)
        job=self.prepare(child,folder='LC78/LC7877847',route='folders')
        result=self.execute(job)
        self.assertTrue(all(Path(r['source']).parent==child for r in result['items']))
        self.assertEqual(len(list(child.glob('*.jpg'))),2)
        self.assertFalse((child/'LC78').exists())

    def test_small_without_photos_does_not_block_confirmed_final(self):
        final,_=self.setup_loose()
        for path in self.small.rglob('*.jpg'):path.unlink()
        for path in self.small.rglob('*.JPG'):path.unlink()
        for path in final.glob('*.jpg'):
            self.archive.writer.run('-overwrite_original','-XMP-fk:SKU=LC7979103',path)
        result=self.execute(self.prepare(final));self.assertEqual(result['done'],2)

    def test_multiple_skus_cannot_target_one_child_sku(self):
        final,_=self.setup_loose()
        for index,path in enumerate(final.glob('*.jpg')):
            self.archive.writer.run('-overwrite_original',f'-ImageNumber={100+index}',
                                    '-XMP-fk:SKU='+('LC7877847','LC7877848')[index],path)
        job=self.prepare(final,self.target/'LC7877847')
        self.assertEqual(job['status'],'failed');self.assertIn('多子SKU',job['error'])

    def test_unassigned_label_present_in_final_is_listed_as_excluded(self):
        final,unknown=self.setup_loose()
        self.archive.writer.run('-overwrite_original','-XMP-xmp:Rating=2',unknown)
        (final/unknown.name).write_bytes(unknown.read_bytes())
        job=self.prepare(final);self.assertEqual(job['status'],'ready',job.get('error'))
        self.assertEqual(job['expected'],2);self.assertEqual(job['excluded_count'],1)
        self.assertEqual(job['label_file_count'],0)
        self.execute(job);self.assertTrue(unknown.exists());self.assertTrue((final/unknown.name).exists())

    def test_final_identity_conflict_is_not_resolved_by_folder_name(self):
        final,_=self.setup_loose()
        for index,path in enumerate(final.glob('*.jpg')):
            self.archive.writer.run('-overwrite_original','-ImageNumber=100',path)
            folder=final/('LC78/LC7877847','LC79/LC7979103')[index]
            folder.mkdir(parents=True);path.rename(folder/path.name)
        job=self.prepare(final);self.assertEqual(job['status'],'failed')
        self.assertIn('冲突',job['error'])

    def test_legacy_ready_plan_rematches_before_any_copy(self):
        from folder_archive_jobs import FolderArchiveJobs,save
        from sku_folder_archive import SkuFolderArchive
        final,_=self.setup_loose();label=self.small/'照片1.jpg'
        self.archive.writer.run('-overwrite_original','-XMP-xmp:Rating=2',label)
        (final/label.name).unlink()
        job=self.prepare(final);jobs=self.http.folder_archives;folder=jobs.root/job['id']
        old=SkuFolderArchive(self.archive.writer,folder/'sources.sqlite3').plan(self.small,final,self.target,fast=True,include_label_images=True)
        self.assertEqual(old['expected'],2);old.pop('reference_scope');save(folder/'plan.json',old)
        jobs.close();self.http.folder_archives=FolderArchiveJobs(self.store,self.archive,self.catalog)
        stale=self.request('/api/folder-archive/state?id='+job['id'])
        self.assertTrue(stale['requires_rematch']);self.assertFalse(stale['items'])
        jobs=self.http.folder_archives;progress=jobs._progress
        entered=threading.Event();release=threading.Event()
        def gated(jid,**values):
            progress(jid,**values)
            if values.get('stage')=='sources' and values.get('done')==0:
                entered.set();release.wait(10)
        with patch.object(jobs,'_progress',side_effect=gated):
            self.request('/api/folder-archive/start',{'id':job['id']})
            try:
                self.assertTrue(entered.wait(5))
                live=self.request('/api/folder-archive/state?id='+job['id'])
                self.assertEqual(live['status'],'preparing');self.assertEqual(live['done'],0)
                self.assertGreaterEqual(live['total'],2)
            finally:release.set()
            fresh=self.wait(job['id'],('ready','failed'))
        self.assertEqual(fresh['expected'],1);self.assertEqual(fresh['label_count'],0)
        self.assertTrue((folder/'plan-before-scope-rules.json').is_file())
        self.assertFalse(list(self.target.glob('LC*')));self.assertTrue(label.exists())
        self.execute(fresh);self.assertTrue(label.exists())


if __name__=='__main__':unittest.main()
