"""真实 JPEG 元数据写入、旧 OCR 补写、幂等与失效恢复。"""
import unittest

from PIL import Image
from catalog_ocr import backfill,install
from catalog_sku_writeback import CatalogSkuWriteback
import test_catalog_ocr as ocr_fixture
import test_analysis_workflow as workflow_fixture


class CatalogSkuWritebackTests(unittest.TestCase):
    setUp=ocr_fixture.CatalogOcrTests.setUp
    tearDown=workflow_fixture.AnalysisWorkflowTests.tearDown
    save=ocr_fixture.CatalogOcrTests.save
    group=ocr_fixture.CatalogOcrTests.group
    items=ocr_fixture.CatalogOcrTests.items

    def writer(self):
        return CatalogSkuWriteback(self.archive,self.catalog,self.sku)

    def drain(self,writer):
        for _ in range(15):
            writer.tick();self.archive.process_one()
            counts=writer.state()['counts']
            if not any(counts.get(status) for status in ('queued','writing','reverting')):return counts
        self.fail('元数据队列没有结束')

    def test_confirmed_label_is_immediate_and_real_file_write_preserves_ocr_and_pixels(self):
        before={r['id']:Image.open(r['path']).convert('RGB').tobytes() for r in self.rows}
        self.save(self.rows[0],'款号 LC12345')
        self.assertEqual(self.group()['status'],'confirmed')
        self.assertEqual(self.group()['processed'],1)
        self.assertTrue(all(r['sku']=='LC12345' for r in self.items()))
        from unittest.mock import patch
        writer=self.writer()
        with patch.object(self.catalog.metadata,'changed_path',side_effect=AssertionError('已知主键不能回扫路径')):
            self.assertEqual(self.drain(writer),{'applied':2})
        for row in self.items():
            metadata=self.archive.writer.read_asset(row['path'])
            self.assertEqual(metadata['XMP-fk:SKU'],'LC12345')
            self.assertIn('SKU:LC12345',metadata['XMP-dc:Subject'])
            self.assertEqual(Image.open(row['path']).convert('RGB').tobytes(),before[row['id']])
        with self.catalog.connect() as c:
            row=c.execute('SELECT i.fingerprint,o.fingerprint FROM items i JOIN intelligence_ocr o ON o.item=i.id WHERE i.id=?',(self.rows[0]['id'],)).fetchone()
            self.assertEqual(row[0],row[1])
        self.catalog.scan(self.ocr_root)
        self.assertEqual(self.group()['status'],'confirmed')
        self.assertTrue(all(r['sku']=='LC12345' for r in self.items()))
        self.assertEqual(self.drain(writer),{'applied':2})
        self.ocr.assert_not_called()

    def test_old_confirmed_results_seed_without_model_and_migrate_pending_match(self):
        self.save(self.rows[0],'LC12345')
        with self.catalog.connect() as c:
            c.execute('DELETE FROM catalog_sku_writeback')
            c.execute("DELETE FROM intelligence_settings WHERE key IN ('ocr_metadata_seed_v1','ocr_immediate_confirmation_v1')")
            c.execute("UPDATE intelligence_ocr_groups SET status='pending',sku='' WHERE root=?",(self.ocr_root,))
            c.execute("UPDATE items SET sku='',normalized='',source='unmatched' WHERE root=?",(self.ocr_root,))
            install(c);backfill(c,self.sku.sku_rules())
        self.assertEqual(self.group()['status'],'confirmed')
        self.assertEqual(self.drain(self.writer()),{'applied':2})
        self.ocr.assert_not_called()

    def test_changed_assignment_before_write_never_persists_stale_sku(self):
        self.save(self.rows[0],'LC12345');writer=self.writer();writer.tick()
        self.save(self.rows[1],'LC99999')
        self.drain(writer)
        for row in self.rows:self.assertFalse(self.archive.writer.read_asset(row['path']).get('XMP-fk:SKU'))

    def test_later_conflict_restores_written_fields(self):
        self.save(self.rows[0],'LC12345');writer=self.writer();self.drain(writer)
        self.save(self.catalog.item(self.rows[1]['id']),'LC99999')
        counts=self.drain(writer)
        self.assertEqual(counts,{'cancelled':2})
        for row in self.items():
            self.assertEqual(row['sku'],'')
            self.assertFalse(self.archive.writer.read_asset(row['path']).get('XMP-fk:SKU'))

    def test_crash_between_archive_enqueue_and_outbox_ack_does_not_duplicate_jobs(self):
        from unittest.mock import patch
        self.save(self.rows[0],'LC12345');writer=self.writer()
        original=writer.update
        def interrupted(item,status,*args,**kwargs):
            if status=='writing':raise RuntimeError('模拟跨库确认中断')
            return original(item,status,*args,**kwargs)
        with patch.object(writer,'update',side_effect=interrupted):writer.tick()
        with self.archive.connect() as c:before=c.execute('SELECT count(*) FROM metadata_jobs').fetchone()[0]
        writer.retry()
        with self.catalog.connect() as c:entry=dict(c.execute("SELECT * FROM catalog_sku_writeback WHERE request_key<>''").fetchone())
        writer.process(entry)
        with self.archive.connect() as c:after=c.execute('SELECT count(*) FROM metadata_jobs').fetchone()[0]
        self.assertEqual(after,before)

    def test_written_sku_is_recovered_into_fresh_catalog_and_quick_index(self):
        import shutil
        from asset_catalog import AssetCatalog
        from quick_file_index import QuickFileIndex
        self.save(self.rows[0],'LC12345');self.drain(self.writer())
        folder=self.base/'neutral';folder.mkdir()
        shutil.copy2(self.rows[0]['path'],folder/'renamed.jpg')
        catalog=AssetCatalog(self.base/'fresh-data')
        try:
            catalog.add_root(str(folder))
            while catalog.metadata.process_one():pass
            rows=catalog.items()['rows'];self.assertEqual(rows[0]['sku'],'LC12345');self.assertEqual(rows[0]['source'],'embedded')
            quick=QuickFileIndex(catalog)
            try:
                quick.build()
                while quick.sync():pass
                self.assertEqual(quick.query('12345')['rows'][0]['sku'],'LC12345')
            finally:quick.close()
        finally:catalog.close()

    def test_unacknowledged_file_commit_is_recovered_before_later_conflict(self):
        from unittest.mock import patch
        self.save(self.rows[0],'LC12345');writer=self.writer();original=writer.update
        def interrupted(item,status,*args,**kwargs):
            if status=='writing':raise RuntimeError('模拟跨库确认中断')
            return original(item,status,*args,**kwargs)
        with patch.object(writer,'update',side_effect=interrupted):writer.tick()
        self.archive.process_one()
        self.save(self.catalog.item(self.rows[1]['id']),'LC99999')
        self.drain(writer)
        for row in self.items():self.assertFalse(self.archive.writer.read_asset(row['path']).get('XMP-fk:SKU'))

    def test_manual_assignment_and_external_file_changes_are_protected(self):
        self.save(self.rows[0],'LC12345');writer=self.writer()
        self.catalog.assign(self.rows[0]['id'],'MANUAL99')
        path=self.rows[1]['path'];Image.new('RGB',(160,120),'purple').save(path)
        self.drain(writer)
        self.assertEqual(self.catalog.item(self.rows[0]['id'])['sku'],'MANUAL99')
        self.assertFalse(self.archive.writer.read_asset(path).get('XMP-fk:SKU'))
        self.assertTrue(writer.state()['failed'])

    def test_paired_jpg_and_raw_write_both_components_and_preserve_raw_bytes(self):
        import hashlib
        import media_engine as media
        raw=self.folder/'pair.ARW';raw.write_bytes(b'RAW payload preserved'*1000)
        jpg=self.folder/'pair.JPG';Image.new('RGB',(160,120),'green').save(jpg)
        sid=self.store.ensure_live_session(self.folder)
        self.store.append_live(sid,[media.capture_record(self.folder,raw,jpg,{'DateTimeOriginal':'2026:11:03 12:00:00'})])
        self.catalog.scan(self.ocr_root);self.save(self.rows[0],'LC12345')
        before=hashlib.sha256(raw.read_bytes()).hexdigest()
        writer=self.writer();counts=self.drain(writer)
        self.assertEqual(counts,{'applied':4})
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(),before)
        self.assertEqual(self.archive.writer.read(raw.with_suffix('.xmp'))['XMP-fk:SKU'],'LC12345')
        self.assertEqual(self.archive.writer.read(jpg)['XMP-fk:SKU'],'LC12345')

    def test_import_pause_survives_restart_and_explicit_retry_resumes(self):
        self.save(self.rows[0],'LC12345')
        with self.catalog.connect() as c:c.execute("INSERT INTO intelligence_settings VALUES('ocr_metadata_paused','1')")
        writer=self.writer();self.assertFalse(writer.tick());self.assertTrue(writer.state()['paused'])
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM metadata_jobs').fetchone()[0],0)
        writer=self.writer();self.assertFalse(writer.tick())
        writer.retry();self.assertEqual(self.drain(writer),{'applied':2})

    def test_progress_polling_does_not_open_the_catalog(self):
        from unittest.mock import patch
        self.save(self.rows[0],'LC12345');writer=self.writer();writer.refresh_state()
        with patch.object(self.catalog,'connect',side_effect=AssertionError('状态轮询不能访问主库')),patch('catalog_sku_writeback.sqlite3.connect',side_effect=AssertionError('状态轮询不能访问 SQLite')):
            self.assertEqual(writer.state()['counts'],{'queued':2})

    def test_mpf_jpeg_metadata_keeps_secondary_image_and_rejects_changed_payload(self):
        from unittest.mock import patch
        from metadata_writer import sha
        from asset_archive import uid
        path=self.folder/'mpf.JPG'
        first=Image.new('RGB',(80,64),'red');second=Image.new('RGB',(40,32),'blue')
        first.save(path,format='MPO',save_all=True,append_images=[second])
        before=sha(path);writer=self.archive.writer
        fields={'SKU':'LC12345','AssetId':uid(),'FileId':uid()}
        secondary=writer.binary('-b','-MPImage2:MPImage2',str(path),input_data=None)
        self.assertTrue(secondary)
        payload,target,journal=writer.write(path,fields,before)
        self.assertTrue(journal['mp_integrity'])
        self.assertEqual(writer.binary('-b','-MPImage2:MPImage2','-',input_data=payload),secondary)
        binary=writer.binary
        def changed(*args,**kwargs):
            if args[:2]==('-b','-MPImage2:MPImage2') and kwargs.get('input_data'):return b'changed secondary image'
            return binary(*args,**kwargs)
        with patch.object(writer,'binary',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'附加图像内容校验失败'):writer.write(path,fields,before)
        self.assertEqual(sha(path),before)


if __name__=='__main__':unittest.main()
