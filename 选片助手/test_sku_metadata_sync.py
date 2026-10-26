"""新机器先归档、后导入 SKU 备份的真实文件同步验收。"""
import copy
import json
from pathlib import Path
import shutil
import unittest

from PIL import Image
from asset_catalog import AssetCatalog
from raw_reconcile import RawReconcile
from sku_metadata_backup import import_metadata,export_metadata,_canonical
from sku_metadata_sync import MetadataArchiveSync
from sku_library import browse
from metadata_writer import sha
from metadata_writer import raw_integrity
import media_engine as media
import test_sku_metadata_backup as backups


class MetadataArchiveSyncTests(unittest.TestCase):
    def setUp(self):
        self.b=backups.MetadataBackupTests();self.b.setUp();b=self.b
        self.sku=b.sku;self.archive=b.archive;self.store=b.store
        self.catalog=AssetCatalog(self.store.data);self.link=RawReconcile(self.archive,autostart=False)
        self.sync=MetadataArchiveSync(self.sku,self.catalog,self.link,autostart=False)
        self.final=Path(b.target_temp.name)/'新机器'/'资产归档';self.final.mkdir(parents=True)
        for i in range(4):shutil.copy2(b.f.photos/f'{i}.JPG',self.final/f'{i}.JPG')
        self.catalog.add_root(str(self.final))
        self.old=b.f.photos.with_name('原机器离线');b.f.photos.rename(self.old)

    def tearDown(self):
        self.sync.close();self.link.close();self.old.rename(self.b.f.photos);self.b.tearDown()

    def drain(self):
        for _ in range(160):
            self.sync.tick()
            for _ in range(8):self.archive.process_one()
            state=self.sync.state()
            if state and state['status'] not in ('queued','running'):return state
        self.fail('换机同步未完成：'+str(self.sync.state()))

    def load(self):return import_metadata(self.sku,self.b.content,sync=self.sync)

    def test_changed_paths_copy_hash_and_embedded_identity_restore_everything(self):
        self.load();state=self.drain();self.assertEqual(state['status'],'completed',state)
        self.assertEqual(state['report']['applied'],4)
        with self.archive.connect() as c:
            paused=c.execute("SELECT status FROM metadata_jobs WHERE reason LIKE '旧机器待写入%'").fetchall()
            self.assertTrue(paused);self.assertTrue(all(row['status']=='cancelled' for row in paused))
        for i in range(4):
            actual=self.archive.writer.read(self.final/f'{i}.JPG')
            self.assertEqual(actual['XMP-fk:SKU'],'A-100' if i<2 else 'B-200')
            self.assertEqual(actual['XMP-fk:ShootType'],'模特图')
            self.assertEqual(actual['XMP-fk:ProjectId'],self.b.f.pid)
            self.assertTrue(actual['XMP-fk:SegmentId']);self.assertEqual(actual['XMP-xmp:Rating'],2 if i in (1,3) else 0)
        active=browse(self.sku);removed=browse(self.sku,bin_id='trash')
        self.assertEqual(active['total'],2);self.assertEqual(removed['total'],2)
        self.assertTrue(all(not row['offline'] and str(self.final) in row['path'] for row in active['rows']))
        for row in active['rows']:
            details=self.sku.ocr_details(row['session_id'],row['capture_id'])
            self.assertIn('COTTON',details['text']);self.assertEqual(details['selected_sku'],'A-100')
        self.assertEqual(browse(self.sku,search='COTTON')['total'],2)
        self.assertEqual(self.catalog.items()['rows'][0]['source'],'embedded')
        hashes=[sha(self.final/f'{i}.JPG') for i in range(4)]
        self.assertTrue(self.load()['already_imported']);self.drain()
        self.assertEqual(hashes,[sha(self.final/f'{i}.JPG') for i in range(4)])

    def test_preindexed_embedded_assets_merge_without_false_conflicts(self):
        sid=self.store.ensure_live_session(self.final)
        paths=list(self.final.glob('*.JPG'));metadata=media.read_metadata(paths,media.find_exiftool(backups.ROOT))
        self.store.append_live(sid,[media.capture_record(self.final,p,None,m) for p,m in zip(paths,metadata)])
        for record in self.store.records(sid):self.archive.ensure(sid,record['id'],inherit=False)
        self.load();state=self.drain();self.assertEqual(state['status'],'completed',state)
        with self.archive.connect() as c:
            for row in c.execute('SELECT asset FROM catalog_links WHERE session=?',(sid,)):
                self.assertTrue(c.execute('SELECT 1 FROM assignments WHERE asset=?',(row['asset'],)).fetchone())

    def test_same_directory_on_new_machine_merges_scanned_placeholders(self):
        same=self.b.f.photos;same.mkdir()
        for path in self.final.glob('*.JPG'):shutil.copy2(path,same/path.name)
        with self.catalog.connect() as c:c.execute('UPDATE roots SET removed=1')
        self.final=same;self.catalog.add_root(str(same))
        sid=self.store.ensure_live_session(same);self.assertEqual(sid,self.b.f.sid)
        paths=list(same.glob('*.JPG'));metadata=media.read_metadata(paths,media.find_exiftool(backups.ROOT))
        self.store.append_live(sid,[media.capture_record(same,p,None,m) for p,m in zip(paths,metadata)])
        for record in self.store.records(sid):self.archive.ensure(sid,record['id'],inherit=False)
        try:
            self.load();result=self.drain();self.assertEqual(result['status'],'completed',result)
            self.assertEqual(self.archive.writer.read(same/'1.JPG')['XMP-fk:SKU'],'A-100')
            self.assertEqual(self.archive.writer.read(same/'3.JPG')['XMP-xmp:Rating'],2)
        finally:
            self.assertEqual(same.resolve().parent,self.b.f.base.resolve())
            shutil.rmtree(same)

    def test_same_name_different_content_never_matches_and_reports_missing(self):
        for path in self.final.glob('*.JPG'):Image.new('RGB',(90,80),'orange').save(path)
        hashes=[sha(p) for p in self.final.glob('*.JPG')]
        self.load();state=self.drain()
        self.assertEqual(state['status'],'attention');self.assertEqual(state['report']['unmatched'],4)
        self.assertEqual(state['report']['missing_assets'],4)
        self.assertEqual(hashes,[sha(p) for p in self.final.glob('*.JPG')])

    def test_catalog_manual_sku_conflict_is_reported_without_writing(self):
        item=next(row for row in self.catalog.items()['rows'] if row['name']=='0.JPG')
        self.catalog.assign(item['id'],'LOCAL-ONLY');before=sha(self.final/'0.JPG')
        self.load();result=self.drain()
        self.assertEqual(result['status'],'attention');self.assertEqual(result['report']['review'],1)
        self.assertEqual(sha(self.final/'0.JPG'),before)
        self.assertEqual(self.catalog.item(item['id'])['sku'],'LOCAL-ONLY')

    def test_reimport_rechecks_changed_files_instead_of_reusing_completed_result(self):
        self.load();self.assertEqual(self.drain()['status'],'completed')
        path=self.final/'0.JPG';Image.new('RGB',(160,120),'orange').save(path);before=sha(path)
        self.load();result=self.drain()
        self.assertEqual(result['status'],'attention');self.assertEqual(result['report']['unmatched'],1,result)
        self.assertEqual(sha(path),before)

    def test_manual_target_rating_conflict_preserved_and_reported(self):
        sid=self.store.ensure_live_session(self.final)
        paths=list(self.final.glob('*.JPG'));metadata=media.read_metadata(paths,media.find_exiftool(backups.ROOT))
        self.store.append_live(sid,[media.capture_record(self.final,p,None,m) for p,m in zip(paths,metadata)])
        session=self.store.session(sid);state=copy.deepcopy(session['state']);cid=self.store.records(sid)[0]['id'];state['overrides']={cid:5}
        self.store.save(sid,session['revision'],state)
        before=sha(self.final/'0.JPG');self.load();result=self.drain()
        self.assertEqual(result['status'],'attention');self.assertEqual(result['report']['review'],1)
        self.assertEqual(sha(self.final/'0.JPG'),before)

    def test_edited_archive_uses_camera_identity_instead_of_old_paths_or_hash(self):
        for i in range(4):
            path=self.final/f'{i}.JPG';Image.new('RGB',(160,120),'purple').save(path)
            self.archive.writer.run('-overwrite_original','-Model=TestCam','-ExifIFD:SerialNumber=123456',f'-DateTimeOriginal=2026:10:21 10:00:0{i}',path)
        def identity(backup):
            for row in backup['payload']['databases']['catalog']['captures']:
                record=json.loads(row['data']);i=record['jpg'].split('.')[0]
                record.update(camera='TestCam',cameraKey='TestCam:123456',captureIdentity={'Model':'TestCam','SerialNumber':'123456','DateTimeOriginal':f'2026:10:21 10:00:0{i}'})
                row['data']=json.dumps(record)
        import_metadata(self.sku,self.b.changed(identity),sync=self.sync)
        result=self.drain();self.assertEqual(result['status'],'completed',result)
        self.assertEqual(self.archive.writer.read(self.final/'0.JPG')['XMP-fk:SKU'],'A-100')

    def test_restart_continues_committed_sync_and_no_archive_prompts_import_first(self):
        self.load();self.sync.tick();self.sync.close()
        self.sync=MetadataArchiveSync(self.sku,self.catalog,self.link,autostart=False)
        result=self.drain();self.assertEqual(result['status'],'completed',result)
        with self.catalog.connect() as c:c.execute('UPDATE roots SET removed=1')
        self.load();result=self.drain();self.assertEqual(result['status'],'waiting_archive')

    @unittest.skipUnless((backups.ROOT/'build/raw-reconcile/r6ii-6402.CR3').is_file(),'本机缺少真实 RAW 夹具')
    def test_real_raw_jpg_pair_restores_fields_and_preserves_sensor_payload(self):
        for path in self.final.glob('*.JPG'):path.unlink()
        raw=self.final/'IMG_0346.CR3';shutil.copy2(backups.ROOT/'build/raw-reconcile/r6ii-6402.CR3',raw)
        metadata=media.read_metadata([raw],media.find_exiftool(backups.ROOT))[0]
        jpg=self.final/'IMG_0346.JPG';Image.new('RGB',(160,120),'green').save(jpg)
        record=media.capture_record(self.final,raw,jpg,metadata)
        def identity(backup):
            source=backup['payload']['databases']['catalog']['captures'][0]
            old=json.loads(source['data'])
            for field in ('captureIdentity','camera','cameraKey','time','name'):old[field]=record[field]
            source['data']=json.dumps(old)
        before=raw_integrity(raw)
        import_metadata(self.sku,self.b.changed(identity),sync=self.sync);result=self.drain()
        self.assertEqual(result['report']['applied'],1,result)
        for path in (raw,jpg):
            actual=self.archive.writer.read_asset(path)
            self.assertEqual(actual['XMP-fk:SKU'],'A-100');self.assertEqual(actual['XMP-fk:ShootType'],'模特图')
            self.assertEqual(actual['XMP-fk:ProjectId'],self.b.f.pid);self.assertEqual(actual['XMP-xmp:Rating'],0)
        self.assertEqual(raw_integrity(raw),before)


if __name__=='__main__':unittest.main()
