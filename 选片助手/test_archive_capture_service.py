import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from PIL import Image
from archive_capture_service import ArchiveCaptureService
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from model_providers import Providers
from portable_capture_store import DatasetBusy
from sku_service import SkuService

ROOT = Path(__file__).resolve().parents[1]


class ArchiveCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.photos = self.base / '真实操作副本'
        self.photos.mkdir()
        for n in range(1, 5):
            exif = Image.Exif()
            exif[36867] = f'2026:11:09 12:00:0{n}'
            exif[272] = 'TestCam'
            exif[42033] = '1234567'
            Image.new('RGB', (120, 160), (n * 30, 90, 140)).save(self.photos / f'IMG_{n:04d}.JPG', exif=exif)
        self.handles = []
        self.store, self.archive, self.catalog, self.sku, self.api, self.rid = self.client(self.base / 'A', self.photos)
        self.dataset, _ = self.api.scope(self.rid)
        self.api._scan(self.dataset)
        self.api.edit(self.rid)

    def client(self, data, photos):
        store = Store(data, ROOT)
        archive = AssetArchive(store, autostart=False)
        catalog = AssetCatalog(store.data)
        providers = Providers(store.data)
        provider = providers.save({'name': 'OCR test', 'category': 'ocr', 'protocol': 'chat', 'url': 'http://127.0.0.1:1/v1', 'model': 'fixture'})['providers'][0]
        providers.select(provider['id'])
        sku = SkuService(store, providers, archive=archive, autostart=False, recognizer=lambda p, image: {'text': 'LC123456', 'codes': ['LC123456']})
        archive.sku = sku
        api = ArchiveCaptureService(store, archive, catalog, sku, autostart=False)
        rid = catalog.add_root(str(photos))['root_id']
        until = time.monotonic() + 10
        while time.monotonic() < until:
            with catalog.connect() as c:
                count = c.execute('SELECT count(*) FROM items').fetchone()[0]
            if count >= len(list(photos.glob('*.JPG'))):
                break
            time.sleep(.02)
        self.handles.append((api, sku, catalog, archive))
        return store, archive, catalog, sku, api, rid

    def tearDown(self):
        for api, sku, catalog, archive in reversed(self.handles):
            api.close()
            sku.close()
            catalog.close()
            archive.close()
        self.temp.cleanup()

    def item(self, n, catalog=None):
        with (catalog or self.catalog).connect() as c:
            return c.execute('SELECT id FROM items WHERE name=?', (f'IMG_{n:04d}.JPG',)).fetchone()[0]

    def drain(self, api=None, dataset=None):
        api, dataset = api or self.api, dataset or self.dataset
        for _ in range(160):
            api.tick()
            with dataset.connect() as c:
                if not c.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running') LIMIT 1").fetchone() and not api.loading.get(str(dataset.root)) and not api.scan_cursor.get(str(dataset.root)) and not api.projection_pending:
                    failures = [dict(r) for r in c.execute("SELECT * FROM tasks WHERE status='failed'")]
                    self.assertEqual(failures, [])
                    return
        self.fail('任务未在预期轮次完成')

    def xmp(self,n,fields):
        from metadata_writer import sha
        path=self.photos/f'IMG_{n:04d}.JPG'
        content,target,journal=self.archive.writer.write(path,fields,sha(path),_target=path.with_suffix('.xmp'))
        self.archive.writer.commit(content,target,journal)
        return target

    def complete_fields(self,n,code,*,group=None,decision=None,rating=0,members=None):
        import uuid
        from portable_capture_store import digest,encoded
        record=self.dataset.record(path=f'IMG_{n:04d}.JPG');rows=members or [record]
        marker={r['asset_id'] for r in rows if r['relative_path']==f'IMG_{n:04d}.JPG' and rating==2}
        return {**self.api._fields(self.dataset,record),'SKU':code,'BaseSKU':code,'Rating':rating,
                'SegmentId':group or uuid.uuid4().hex,'DecisionId':decision or uuid.uuid4().hex,'GroupRevision':decision or record.get('decision_id') or uuid.uuid4().hex,
                'AssignmentState':'confirmed','GroupProductCount':str(len({r['asset_id'] for r in rows})-len(marker)),'GroupLabelCount':str(len(marker)),
                'GroupFileCount':str(len(rows)),'GroupMembersDigest':digest(encoded(sorted({r['asset_id'] for r in rows}))),
                'GroupFilesDigest':digest(encoded(sorted(r['file_id'] for r in rows)))}

    def test_full_new_xmp_restores_identity_sku_group_and_never_calls_ocr(self):
        fields=self.complete_fields(2,'00123-B',rating=2)
        self.xmp(2,fields)
        self.api.edit(self.rid,enabled=False)
        _,_,catalog,sku,other,rid=self.client(self.base/'new-reader',self.photos)
        sku.recognizer=lambda *_:self.fail('完整XMP不得重做OCR')
        ds,_=other.scope(rid);other._scan(ds)
        record=ds.record(path='IMG_0002.JPG')
        self.assertEqual(record['file_id'],fields['FileId']);self.assertEqual(record['sku'],'00123-B')
        self.assertEqual(record['metadata_format'],'complete');self.assertTrue(record['boundary'])
        self.assertEqual(ds.groups()[0]['id'],fields['SegmentId']);self.assertFalse(ds.editing)
        other.edit(rid);self.drain(other,ds)
        self.assertEqual(record['sku'],catalog.item(self.item(2,catalog))['sku'])

    def test_empty_ocr_keeps_interval_and_manual_sku_updates_every_member(self):
        self.sku.recognizer=lambda *_:{'text':'','codes':[],'is_label':False}
        self.api.rate(self.item(3),2);self.drain()
        self.assertEqual(len(self.dataset.groups()[0]['members']),3)
        self.assertEqual(self.dataset.groups()[0]['status'],'manual')
        state=self.api.state(self.rid)
        self.assertEqual(len(state['groups']),1);self.assertEqual(state['unclosed'],1)
        self.api.confirm(self.item(3),'MANUAL-001',auto_edit=True);self.drain()
        self.assertEqual(len([r for r in self.dataset.records() if r.get('sku')=='MANUAL-001']),3)

    def test_missing_model_is_manual_fill_not_lost_interval(self):
        self.api.sku=None
        self.api.rate(self.item(2),2);self.drain()
        self.assertEqual(self.dataset.groups()[0]['status'],'manual')
        self.assertEqual(len(self.dataset.groups()[0]['members']),2)
        self.api.confirm(self.item(2),'NO-MODEL-01',auto_edit=True);self.drain()
        self.assertEqual(self.dataset.groups()[0]['sku'],'NO-MODEL-01')

    def test_readonly_interval_displays_existing_sku_and_only_missing_file_is_written(self):
        import hashlib
        rows=[self.dataset.record(path=f'IMG_{n:04d}.JPG') for n in (1,2)]
        fields=self.complete_fields(2,'RESTORED-01',rating=2,members=rows)
        side=self.xmp(2,fields);image=self.photos/'IMG_0002.JPG'
        before=(image.read_bytes(),side.read_bytes())
        self.api.edit(self.rid,enabled=False);self.api._scan(self.dataset)
        self.assertEqual(self.catalog.items(text='RESTORED-01')['total'],2)
        self.assertEqual(self.dataset.record(path='IMG_0001.JPG').get('inherited_sku'),'RESTORED-01')
        self.sku.recognizer=lambda *_:self.fail('区间已有款号不得再OCR')
        self.api.edit(self.rid);self.drain()
        self.assertEqual(before,(image.read_bytes(),side.read_bytes()))
        self.assertEqual(self.archive.writer.read_asset(self.photos/'IMG_0001.JPG')['XMP-fk:SKU'],'RESTORED-01')
        with self.dataset.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE kind='metadata_fill'").fetchone()[0],1)

    def test_already_saved_sku_files_are_byte_identical_after_read_and_edit_acquire(self):
        import uuid
        rows=[self.dataset.record(path=f'IMG_{n:04d}.JPG') for n in (1,2)]
        fields=self.complete_fields(2,'ALREADY-01',group=uuid.uuid4().hex,decision=uuid.uuid4().hex,rating=2,members=rows)
        self.xmp(2,fields)
        self.xmp(1,{**fields,'FileId':rows[0]['file_id'],'AssetId':rows[0]['asset_id'],'Rating':1,'PhotoRole':'product'})
        files=[self.photos/f'IMG_{n:04d}{ext}' for n in (1,2) for ext in ('.JPG','.xmp')]
        before={str(p):p.read_bytes() for p in files}
        self.api.edit(self.rid,enabled=False);self.api._scan(self.dataset)
        self.sku.recognizer=lambda *_:self.fail('已写入的SKU不应再识别')
        self.api.edit(self.rid);self.drain()
        self.assertEqual(self.catalog.items(text='ALREADY-01')['total'],2)
        self.assertEqual(before,{str(p):p.read_bytes() for p in files})
        with self.dataset.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE kind IN ('metadata','metadata_fill')").fetchone()[0],0)

    def test_manual_confirm_of_inherited_sku_does_not_rewrite_existing_marker_file(self):
        rows=[self.dataset.record(path=f'IMG_{n:04d}.JPG') for n in (1,2)]
        side=self.xmp(2,self.complete_fields(2,'FILL-ONLY-01',rating=2,members=rows))
        image=self.photos/'IMG_0002.JPG';before=(image.read_bytes(),side.read_bytes())
        self.api.edit(self.rid,enabled=False);self.api._scan(self.dataset)
        self.api.confirm(self.item(2),'FILL-ONLY-01',auto_edit=True);self.drain()
        self.assertEqual(before,(image.read_bytes(),side.read_bytes()))
        self.assertEqual(self.archive.writer.read_asset(self.photos/'IMG_0001.JPG')['XMP-fk:SKU'],'FILL-ONLY-01')

    def test_newer_file_revision_is_imported_without_rewriting_file(self):
        import uuid
        self.api.confirm(self.item(1),'OLD-001',whole_group=False);self.drain()
        record=self.dataset.record(path='IMG_0001.JPG');new=uuid.uuid4().hex
        fields=self.complete_fields(1,'NEW-002',group=record['group_id'],decision=new)
        fields['DecisionParents']=json.dumps([record['decision_id']])
        side=self.xmp(1,fields);before=side.read_bytes()
        self.api.edit(self.rid,enabled=False);self.api._scan(self.dataset)
        restored=self.dataset.record(path='IMG_0001.JPG')
        self.assertEqual(restored['sku'],'NEW-002');self.assertEqual(restored['parents'],[record['decision_id']])
        self.assertFalse(restored.get('metadata_conflict'));self.assertEqual(before,side.read_bytes())

    def test_same_revision_conflict_can_choose_xmp_without_rewriting_matching_file(self):
        self.api.confirm(self.item(1),'ARCHIVE-001',whole_group=False);self.drain()
        record=self.dataset.record(path='IMG_0001.JPG')
        fields=self.complete_fields(1,'FILE-002',group=record['group_id'],decision=record['decision_id'])
        side=self.xmp(1,fields);before=side.read_bytes();self.api._scan(self.dataset)
        conflict=self.dataset.record(path='IMG_0001.JPG')['metadata_conflict_data']
        self.api.resolve_metadata(self.item(1),'xmp',conflict['token'],root_id=self.rid)
        self.drain();self.api._scan(self.dataset)
        self.assertEqual(self.dataset.record(path='IMG_0001.JPG')['sku'],'FILE-002')
        self.assertFalse(self.dataset.record(path='IMG_0001.JPG').get('metadata_conflict'))
        self.assertEqual(before,side.read_bytes())

    def test_stale_conflict_choice_cannot_override_new_file_change(self):
        self.api.confirm(self.item(1),'ARCHIVE-001',whole_group=False);self.drain()
        record=self.dataset.record(path='IMG_0001.JPG');fields=self.complete_fields(1,'FILE-002',group=record['group_id'],decision=record['decision_id'])
        self.xmp(1,fields);self.api._scan(self.dataset)
        token=self.dataset.record(path='IMG_0001.JPG')['metadata_conflict_data']['token']
        self.xmp(1,{**fields,'SKU':'FILE-003'})
        with self.assertRaisesRegex(ValueError,'已变化'):self.api.resolve_metadata(self.item(1),'archive',token,root_id=self.rid)
        self.assertEqual(self.dataset.record(path='IMG_0001.JPG')['sku'],'ARCHIVE-001')

    def test_xmp_content_change_with_preserved_timestamp_is_detected(self):
        import os
        self.api.confirm(self.item(1),'ARCHIVE-001',whole_group=False);self.drain()
        record=self.dataset.record(path='IMG_0001.JPG');fields=self.complete_fields(1,'FILE-002',group=record['group_id'],decision=record['decision_id'])
        side=self.xmp(1,fields);self.api._scan(self.dataset);stamp=side.stat()
        raw=side.read_bytes();self.assertIn(b'FILE-002',raw)
        side.write_bytes(raw.replace(b'FILE-002',b'FILE-003'));os.utime(side,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        _,current=self.api.record_for_item(self.item(1),allow_conflict=True)
        self.assertEqual(current['metadata_conflict_data']['file']['patch']['sku'],'FILE-003')
        self.assertEqual(current['sku'],'ARCHIVE-001')

    def test_two_stars_always_create_a_boundary(self):
        self.api.rate(self.item(2), 2)
        self.drain()
        self.assertEqual(len(self.dataset.groups()),1)
        with self.dataset.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE kind='ocr'").fetchone()[0], 1)

    def test_direct_two_star_automatically_acquires_editor_and_queues_ocr(self):
        self.api.edit(self.rid,enabled=False)
        result=self.api.rate(self.item(2),2,correction=True,root_id=self.rid)
        self.assertTrue(self.dataset.editing)
        self.assertTrue(result['ocr_queued'])
        self.drain()
        self.assertEqual(self.dataset.groups()[0]['sku'],'LC123456')

    def test_direct_rating_cannot_take_other_clients_ownership(self):
        _,_,_,_,other,rid=self.client(self.base/'B',self.photos)
        other_ds,_=other.scope(rid);other._scan(other_ds)
        with self.assertRaises(DatasetBusy):other.rate(self.item(2),2,correction=True,root_id=rid)
        self.assertFalse(other_ds.editing)

    def test_sync_receiver_cannot_auto_acquire_or_run_queued_ocr(self):
        self.api.rate(self.item(2),2,correction=True)
        self.api.readonly=lambda:True
        self.api.tick()
        self.assertFalse(self.dataset.record(path='IMG_0002.JPG').get('ocr'))
        with self.assertRaisesRegex(DatasetBusy,'单向接收'):self.api.rate(self.item(2),2,correction=True)

    def test_ocr_result_cannot_commit_after_switching_to_sync_receiver(self):
        self.api.rate(self.item(2),2,correction=True)
        def recognize(*args):
            self.api.readonly=lambda:True
            return {'text':'LC123456','codes':['LC123456']}
        self.sku.recognizer=recognize
        self.api.tick()
        self.assertFalse(any(r.get('sku') for r in self.dataset.records()))
        with self.dataset.connect() as c:
            self.assertIn('单向接收',c.execute("SELECT reason FROM tasks WHERE kind='ocr' AND status='failed'").fetchone()[0])

    def test_sidecar_only_change_is_read_without_changing_jpg_signature(self):
        path=self.photos/'IMG_0002.JPG'
        before=path.stat().st_mtime_ns
        path.with_suffix('.xmp').write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:xmp="http://ns.adobe.com/xap/1.0/"><rdf:RDF><rdf:Description xmp:Rating="2"/></rdf:RDF></x:xmpmeta>',encoding='utf8')
        self.api.edit(self.rid,enabled=False)
        self.api.state(self.rid,capture_mode=True);self.api._scan(self.dataset)
        record=self.dataset.record(path=path.name)
        self.assertEqual(path.stat().st_mtime_ns,before)
        self.assertEqual(record['rating'],2)
        self.assertTrue(record['boundary'])
        self.assertEqual(len(self.dataset.groups()[0]['members']),2)
        self.assertFalse(self.dataset.editing)
        self.assertFalse(record.get('sku'))
        side=path.with_suffix('.xmp');side.write_text(side.read_text(encoding='utf8').replace('Rating="2"','Rating="0"'),encoding='utf8')
        self.api._scan(self.dataset)
        self.assertFalse(self.dataset.record(path=path.name)['boundary'])
        self.assertTrue(all(g['status']=='cancelled' for g in self.dataset.groups()))

    def test_rating_only_xmp_never_erases_newer_confirmed_sku(self):
        self.api.confirm(self.item(2),'CONFIRMED-001',whole_group=False);self.drain()
        path=self.photos/'IMG_0002.JPG'
        path.with_suffix('.xmp').write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:xmp="http://ns.adobe.com/xap/1.0/"><rdf:RDF><rdf:Description xmp:Rating="2"/></rdf:RDF></x:xmpmeta>',encoding='utf8')
        self.api._scan(self.dataset)
        record=self.dataset.record(path=path.name)
        self.assertEqual(record['sku'],'CONFIRMED-001')
        self.assertEqual(record['rating'],0)
        self.assertIn('rating',record['metadata_conflict_data']['fields'])

    def test_existing_xmp_sku_is_applied_to_interval_without_repeating_ocr(self):
        import uuid
        path=self.photos/'IMG_0002.JPG'
        path.with_suffix('.xmp').write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:fk="https://framekeep.local/ns/asset/1.0/"><rdf:RDF><rdf:Description xmp:Rating="2" fk:SKU="00123-X" fk:SegmentId="'+uuid.uuid4().hex+'"/></rdf:RDF></x:xmpmeta>',encoding='utf8')
        self.api.edit(self.rid,enabled=False)
        self.api.state(self.rid,capture_mode=True);self.api._scan(self.dataset)
        self.sku.recognizer=lambda *_:self.fail('已有XMP款号不应再调用OCR')
        result=self.api.rate(self.item(2),2,correction=True,root_id=self.rid)
        self.assertTrue(result['applied_existing_sku'])
        self.assertFalse(result['ocr_queued'])
        self.drain()
        self.assertEqual(len([r for r in self.dataset.records() if r.get('sku')=='00123-X']),2)

    def test_background_partition_does_not_treat_member_order_as_boundary_change(self):
        self.api.rate(self.item(3),2,correction=True);self.drain()
        group=self.dataset.groups()[0]
        self.dataset.change(groups=[{**group,'members':list(reversed(group['members']))}])
        for _ in range(2):self.api._scan(self.dataset)
        self.assertEqual(self.dataset.groups()[0]['status'],'confirmed')

    def test_explicit_reparse_reads_again_but_does_not_overwrite_confirmed_sku(self):
        self.api.rate(self.item(2),2,correction=True);self.drain();calls=[]
        self.sku.recognizer=lambda *_:calls.append(1) or {'text':'LC999999','codes':['LC999999']}
        result=self.api.recognize(self.item(2));self.assertTrue(result['ocr_queued']);self.drain()
        self.assertEqual(len(calls),1)
        self.assertEqual(self.dataset.groups()[0]['sku'],'LC123456')
        self.assertEqual(self.dataset.groups()[0]['status'],'review')

    def test_slow_folder_scan_defers_ocr_until_all_members_are_known(self):
        for n in range(20):Image.new('RGB',(40,40),'teal').save(self.photos/f'LARGE_{n:04d}.JPG')
        self.catalog.scan(self.rid)
        until=time.monotonic()+10
        while time.monotonic()<until:
            with self.catalog.connect() as c:row=c.execute("SELECT id FROM items WHERE name='LARGE_0019.JPG'").fetchone()
            if row:break
            time.sleep(.02)
        self.api.last_scan.pop(str(self.photos),None)
        result=self.api.rate(row['id'],2,correction=True,root_id=self.rid)
        self.assertTrue(result['preparing'])
        self.assertTrue(result['ocr_queued'])
        self.assertFalse(any(r.get('ocr') for r in self.dataset.records()))
        self.drain()
        group=next(g for g in self.dataset.groups() if g.get('sku')=='LC123456')
        self.assertEqual(len(group['members']),20)

    def test_two_star_intervals_and_duplicate_suffix_are_stable(self):
        self.api.rate(self.item(2), 2, correction=True)
        self.drain()
        self.api.rate(self.item(4), 2, correction=True)
        self.drain()
        groups = sorted(self.dataset.groups(), key=lambda g: g['created'])
        self.assertEqual([g['sku'] for g in groups], ['LC123456', 'LC123456-A'])
        self.assertEqual([g['expected_products'] for g in groups], [1, 1])
        self.assertEqual([len(g['members']) for g in groups], [2, 2])
        self.api.rate(self.item(4), 2, correction=True)
        self.drain()
        self.assertEqual(sorted(g['sku'] for g in self.dataset.groups()), ['LC123456', 'LC123456-A'])

    def test_every_group_member_embeds_sku_identity_and_role(self):
        self.api.rate(self.item(3), 2, correction=True)
        self.drain()
        for n in (1, 2, 3):
            tags = self.archive.writer.read_asset(self.photos / f'IMG_{n:04d}.JPG')
            self.assertEqual(tags['XMP-fk:SKU'], 'LC123456')
            self.assertTrue(tags['XMP-fk:FileId'])
            self.assertTrue(tags['XMP-fk:DecisionId'])
            self.assertEqual(tags['XMP-fk:PhotoRole'], 'sku_label' if n == 3 else 'product')
            self.assertEqual(str(tags['XMP-fk:GroupProductCount']), '2')

    def test_fresh_client_can_read_portable_without_editing(self):
        self.api.rate(self.item(2), 2, correction=True)
        self.drain()
        _, _, _, _, other, rid = self.client(self.base / 'B', self.photos)
        dataset, _ = other.scope(rid)
        other._scan(dataset)
        self.assertEqual(len([r for r in dataset.records() if r.get('sku')]), 2)
        with self.assertRaises(DatasetBusy):
            other.edit(rid)
        self.assertFalse(dataset.editing)

    def test_no_archive_folder_rebuilds_search_and_sku_projection(self):
        self.api.rate(self.item(2), 2, correction=True)
        self.drain()
        destination = self.base / '仅图片'
        destination.mkdir()
        for path in self.photos.glob('*.JPG'):
            shutil.copy2(path, destination / path.name)
        _, _, catalog, sku, other, rid = self.client(self.base / 'B', destination)
        dataset, _ = other.scope(rid)
        other._scan(dataset)
        self.assertFalse((destination / '.shiying').exists())
        from sku_library import browse
        data = browse(sku, 'LC123456')
        self.assertEqual(data['total'], 2)
        self.assertEqual(len(catalog.items(text='LC123456')['rows']), 2)
        self.assertTrue(all(r['provenance'] == 'portable' for r in data['rows']))

    def test_group_manual_change_removes_stale_keywords_and_clear_star_keeps_manual_sku(self):
        self.api.rate(self.item(2), 2, correction=True)
        self.drain()
        self.api.confirm(self.item(1), '00123-B')
        self.drain()
        for n in (1, 2):
            tags = self.archive.writer.read_asset(self.photos / f'IMG_{n:04d}.JPG')
            self.assertEqual(tags['XMP-fk:SKU'], '00123-B')
            self.assertNotIn('SKU:LC123456', tags.get('XMP-dc:Subject', []))
        self.api.rate(self.item(2), 0)
        self.drain()
        self.assertEqual(len([r for r in self.dataset.records() if r.get('sku')=='00123-B']),2)
        self.assertEqual(self.api.state(self.rid)['groups'],[])

    def test_clear_boundary_undo_restores_every_member(self):
        self.api.rate(self.item(2),2,correction=True);self.drain()
        operation=self.api.rate(self.item(2),0)['operation_id'];self.drain()
        self.assertFalse(any(r.get('sku') for r in self.dataset.records()))
        self.api.undo(self.rid,'',operation);self.drain()
        self.assertEqual(len([r for r in self.dataset.records() if r.get('sku')=='LC123456']),2)
        self.assertTrue(next(r for r in self.dataset.records() if r['relative_path']=='IMG_0002.JPG')['boundary'])

    def test_multiple_changes_before_file_write_keep_valid_parent_chain(self):
        self.api.confirm(self.item(1),'FIRST',whole_group=False);self.drain()
        self.api.confirm(self.item(1),'SECOND',whole_group=False)
        self.api.confirm(self.item(1),'THIRD',whole_group=False)
        self.drain()
        self.assertEqual(self.archive.writer.read_asset(self.photos/'IMG_0001.JPG')['XMP-fk:SKU'],'THIRD')

    def test_editing_scope_does_not_rewrite_other_copy(self):
        self.api.rate(self.item(2), 2, correction=True)
        self.drain()
        copy = self.base / '另一副本'
        copy.mkdir()
        shutil.copy2(self.photos / 'IMG_0001.JPG', copy / 'IMG_0001.JPG')
        before = (copy / 'IMG_0001.JPG').read_bytes()
        self.api.confirm(self.item(1), 'LC999999')
        self.drain()
        self.assertEqual((copy / 'IMG_0001.JPG').read_bytes(), before)

    def test_export_is_named_by_folder_and_capture_day(self):
        self.api.rate(self.item(2), 2, correction=True)
        self.drain()
        content, name = self.api.export(self.rid)
        self.assertEqual(name, '真实操作副本_2026-11-09.xlsx')
        from openpyxl import load_workbook
        import io
        book = load_workbook(io.BytesIO(content))
        main=book[book.sheetnames[0]]
        self.assertEqual(main['B2'].value, 'LC123456')
        self.assertEqual([c.value for c in main[1]],['序号','款号','款号图片','标签图片','来源文件','现场文件夹','入库时间','拍摄完成时间','拍摄状态'])
        self.assertEqual(main['A2'].value,1)
        self.assertEqual(main['I2'].value,'已拍摄')
        self.assertEqual({image.anchor._from.col for image in main._images},{2,3})
        self.assertIn('全部款号',book.sheetnames)
        self.assertEqual(book['文件明细'].max_row, 5)
        book.close()

    def test_export_checks_directory_once_and_keeps_missing_records(self):
        from unittest.mock import patch
        import os,io
        from openpyxl import load_workbook
        from archive_capture_export import export_records
        records=self.dataset.records()
        records[0]['relative_path']='missing.jpg'
        with patch('archive_capture_export.os.scandir',wraps=os.scandir) as scan:
            content,_=export_records(self.dataset,self.photos,records,'',self.catalog)
        source_calls=[call for call in scan.call_args_list if isinstance(call.args[0],(str,os.PathLike)) and Path(call.args[0])==self.photos]
        self.assertEqual(len(source_calls),1,'同一目录不能逐文件重复校验NAS路径')
        book=load_workbook(io.BytesIO(content))
        self.assertEqual(book['文件明细'].max_row,len(records)+1)
        self.assertEqual(book['文件明细']['J2'].value,'缺失／离线')
        book.close()

    def test_export_keeps_recorded_label_time_after_label_file_removed(self):
        import io
        from openpyxl import load_workbook
        self.api.rate(self.item(2),2,correction=True);self.drain()
        (self.photos/'IMG_0002.JPG').unlink()
        content,_=self.api.export(self.rid)
        book=load_workbook(io.BytesIO(content));main=book[book.sheetnames[0]]
        self.assertEqual(main['E2'].value,'IMG_0002.JPG')
        self.assertTrue(main['H2'].value)
        self.assertEqual(main['I2'].value,'已拍摄')
        self.assertEqual(main['D2'].value,'图片暂不可用')
        book.close()

    def test_undo_restores_group_and_protects_later_edits(self):
        self.api.rate(self.item(2), 2, correction=True)
        self.drain()
        operation = self.api.confirm(self.item(1), '00123-B')['operation_id']
        self.drain()
        self.api.undo(self.rid, '', operation)
        self.drain()
        self.assertEqual({r.get('sku') for r in self.dataset.records() if r.get('group_id')}, {'LC123456'})
        self.assertEqual(next(g for g in self.dataset.groups() if g.get('status') == 'confirmed')['sku'], 'LC123456')
        second = self.api.confirm(self.item(1), 'LC999999')['operation_id']
        self.api.confirm(self.item(1), 'LC888888')
        with self.assertRaisesRegex(ValueError, '新的修改'):
            self.api.undo(self.rid, '', second)

    def test_batch_stale_preview_does_not_partially_commit(self):
        preview = self.api.batch_preview(self.rid, '', [self.item(1), self.item(2)], 'LC777777')
        self.api.confirm(self.item(2), 'LC111111', whole_group=False)
        with self.assertRaisesRegex(ValueError, '预览后素材已改变'):
            self.api.batch_apply(self.rid, '', preview['preview_id'])
        record = next(r for r in self.dataset.records() if r['relative_path'] == 'IMG_0001.JPG')
        self.assertFalse(record.get('sku'))

    def test_batch_has_one_undoable_atomic_operation(self):
        preview = self.api.batch_preview(self.rid, '', [self.item(1), self.item(2)], '0000123-A')
        result = self.api.batch_apply(self.rid, '', preview['preview_id'])
        self.drain()
        self.assertEqual(len([r for r in self.dataset.records() if r.get('sku') == '0000123-A']), 2)
        self.assertTrue(all(g['complete'] for g in self.api.state(self.rid)['groups']))
        self.api.undo(self.rid, '', result['operation_id'])
        self.drain()
        self.assertFalse(any(r.get('sku') for r in self.dataset.records()))

    def test_current_member_count_alone_does_not_prove_completeness(self):
        self.api.rate(self.item(2), 2, correction=True)
        self.drain()
        self.assertTrue(self.api.state(self.rid)['groups'][0]['complete'])
        record = next(r for r in self.dataset.records() if r['relative_path'] == 'IMG_0001.JPG')
        import uuid
        self.dataset.change([{**record, 'asset_id': uuid.uuid4().hex}])
        self.assertFalse(self.api.state(self.rid)['groups'][0]['complete'])

    def test_sku_projection_invalidation_after_initial_empty_cache(self):
        from sku_library_index import SkuLibraryIndex
        index = SkuLibraryIndex(self.sku)
        try:
            self.assertTrue(index.refresh())
            self.assertFalse(index.value['projection']['rows'])
            self.api.confirm(self.item(1), '00123-A', whole_group=False)
            self.drain()
            self.assertTrue(index.refresh())
            self.assertEqual(index.value['projection']['rows'][0]['sku'], '00123-A')
        finally:
            index.close()

    def test_parent_browsing_does_not_override_child_dataset(self):
        parent = self.api._dataset(self.base)
        self.api._scan(parent)
        self.api.rate(self.item(2), 2, correction=True)
        self.drain()
        self.api._scan(parent)
        with self.catalog.connect() as c:
            self.assertEqual(c.execute('SELECT sku FROM items WHERE id=?', (self.item(1),)).fetchone()[0], 'LC123456')
        parent_id=self.catalog.add_root(str(self.base))['root_id']
        with self.assertRaisesRegex(ValueError, '独立子数据集'):
            self.api.edit(parent_id)

    def test_duplicate_file_id_keeps_two_locations_and_updates_both(self):
        self.api.confirm(self.item(1), 'LC100001', whole_group=False)
        self.drain()
        source=self.photos/'IMG_0001.JPG'
        target=self.photos/'COPY_0001.JPG'
        shutil.copy2(source,target)
        self.catalog.scan(self.rid)
        until=time.monotonic()+10
        while time.monotonic()<until:
            with self.catalog.connect() as c:
                copied=c.execute('SELECT id FROM items WHERE path=?',(str(target),)).fetchone()
            if copied:break
            time.sleep(.02)
        self.api._scan(self.dataset)
        records=[r for r in self.dataset.records() if r.get('sku')=='LC100001']
        self.assertEqual(len(records),2)
        self.assertEqual(len({r['file_id'] for r in records}),1)
        self.assertEqual(len({r['id'] for r in records}),2)
        preview=self.api.batch_preview(self.rid,'',[copied['id']],'LC100002')
        self.assertEqual(preview['count'],2)
        self.assertTrue(any(r['paired'] for r in preview['rows']))
        self.api.batch_apply(self.rid,'',preview['preview_id'])
        self.drain()
        for path in (source,target):self.assertEqual(self.archive.writer.read_asset(path)['XMP-fk:SKU'],'LC100002')

    def test_customer_brand_namespace_and_numeric_sku_are_lossless(self):
        self.api.edit(self.rid,customer='客户甲',brand='品牌甲')
        self.api.confirm(self.item(1),'00123',whole_group=False)
        self.drain()
        other_root=self.base/'另一品牌'
        other_root.mkdir()
        shutil.copy2(self.photos/'IMG_0002.JPG',other_root/'IMG_0002.JPG')
        rid=self.catalog.add_root(str(other_root))['root_id']
        until=time.monotonic()+10
        while time.monotonic()<until:
            with self.catalog.connect() as c:row=c.execute('SELECT id FROM items WHERE path=?',(str(other_root/'IMG_0002.JPG'),)).fetchone()
            if row:break
            time.sleep(.02)
        other,_=self.api.scope(rid)
        self.api.edit(rid,customer='客户乙',brand='品牌乙')
        self.api.confirm(row['id'],'00123',whole_group=False)
        self.drain(dataset=other)
        from sku_library import browse
        result=browse(self.sku,'00123')
        self.assertEqual(len(result['groups']),2)
        self.assertEqual({r['sku'] for r in result['rows']},{'00123'})
        self.assertEqual(str(self.archive.writer.read_asset(self.photos/'IMG_0001.JPG')['XMP-fk:SKU']),'00123')

    def test_large_folder_ingestion_is_bounded_and_focusable(self):
        for n in range(40):
            Image.new('RGB',(40,40),'teal').save(self.photos/f'LARGE_{n:04d}.JPG')
        self.catalog.scan(self.rid)
        until=time.monotonic()+10
        while time.monotonic()<until:
            with self.catalog.connect() as c:count=c.execute('SELECT count(*) FROM items').fetchone()[0]
            if count==44:break
            time.sleep(.02)
        self.api._scan(self.dataset)
        self.assertTrue(self.api.loading[str(self.photos)])
        self.assertLess(len(self.dataset.records()),44)
        with self.catalog.connect() as c:item=c.execute("SELECT id FROM items WHERE name='LARGE_0039.JPG'").fetchone()[0]
        _,focused=self.api.record_for_item(item)
        self.assertEqual(focused['relative_path'],'LARGE_0039.JPG')
        for _ in range(5):
            self.api._scan(self.dataset)
            if not self.api.loading[str(self.photos)]:break
        self.assertEqual(len(self.dataset.records()),44)
        self.assertFalse(self.api.loading[str(self.photos)])

    def test_operation_history_travels_with_snapshot_and_is_undoable_on_another_client(self):
        self.api.rate(self.item(2),2,correction=True);self.drain()
        operation=self.api.confirm(self.item(1),'LC777777')['operation_id'];self.drain()
        self.api.edit(self.rid,enabled=False)
        _,_,_,_,other,rid=self.client(self.base/'B',self.photos)
        dataset,_=other.scope(rid);other._scan(dataset)
        with dataset.connect() as c:self.assertIsNotNone(c.execute('SELECT 1 FROM history WHERE id=?',(operation,)).fetchone())
        other.edit(rid);other.undo(rid,'',operation);self.drain(other,dataset)
        self.assertEqual({r['sku'] for r in dataset.records() if r.get('group_id')},{'LC123456'})

    def test_business_undo_keeps_current_location_after_folder_move(self):
        from folder_archive_catalog_sync import synchronize_catalog_paths
        self.api.rate(self.item(2),2,correction=True);self.drain()
        operation=self.api.confirm(self.item(1),'LC777777')['operation_id'];self.drain()
        moves=[]
        for n in (1,2):
            old=self.photos/f'IMG_{n:04d}.JPG';new=self.photos/'LC77'/'LC777777'/old.name
            new.parent.mkdir(parents=True,exist_ok=True)
            from rating_xmp import sidecar_paths
            # 完整XMP现已自动创建，模拟正式归档时照片与侧车一起移动。
            for side in sidecar_paths(old):side.rename(new.with_suffix('.xmp') if side==old.with_suffix('.xmp') else Path(str(new)+'.xmp'))
            old.rename(new);moves.append({'before':str(old),'after':str(new)})
        with self.catalog.connect() as c:synchronize_catalog_paths(c,moves)
        self.api.relocate(moves)
        self.api.undo(self.rid,'',operation);self.drain()
        restored=[r for r in self.dataset.records() if r.get('group_id')]
        self.assertTrue(all(r['relative_path'].startswith('LC77/LC777777/') for r in restored))
        self.assertTrue(all(r['sku']=='LC123456' for r in restored))
        self.assertFalse((self.photos/'IMG_0001.JPG').exists())

    def test_namespace_undo_restores_context_and_rejects_older_context(self):
        self.api.edit(self.rid,customer='客户甲',brand='品牌甲')
        with self.dataset.connect() as c:operation=c.execute("SELECT id FROM history WHERE kind='scope' ORDER BY revision DESC LIMIT 1").fetchone()[0]
        self.api.undo(self.rid,'',operation);self.drain()
        self.assertEqual(self.dataset.meta('customer'),'')
        self.assertEqual(self.dataset.meta('brand'),'')
        self.assertTrue(all(not r.get('customer') for r in self.dataset.records()))

    def test_imported_subfolder_cannot_create_second_write_authority(self):
        child=self.photos/'子目录';child.mkdir()
        shutil.copy2(self.photos/'IMG_0001.JPG',child/'IMG_0001.JPG')
        _,_,_,_,other,rid=self.client(self.base/'B',child)
        with self.assertRaisesRegex(DatasetBusy,'上级拍摄数据集'):other.edit(rid)
        self.assertFalse((child/'.shiying/dataset.json').exists())

    def test_snapshot_projects_all_skus_before_full_media_scan(self):
        import uuid
        updates=[]
        for n in range(45):
            target=self.photos/f'PORTABLE_{n:03d}.JPG'
            shutil.copy2(self.photos/'IMG_0001.JPG',target)
            stat=target.stat()
            updates.append({'id':uuid.uuid4().hex,'asset_id':uuid.uuid4().hex,'file_id':uuid.uuid4().hex,
                            'relative_path':target.name,'signature':[stat.st_size,stat.st_mtime_ns],
                            'media_type':'image','sku':'BULK-001','decision_id':uuid.uuid4().hex,'rating':3})
        self.dataset.change(updates);self.dataset.publish();self.dataset.snapshot()
        _,_,catalog,sku,other,rid=self.client(self.base/'B',self.photos)
        dataset,_=other.scope(rid)
        reads=[];original=other.archive.writer.read
        def read(path,*args,**kwargs):reads.append(str(path));return original(path,*args,**kwargs)
        other.archive.writer.read=read
        other._scan(dataset)
        self.assertLessEqual(len(set(reads)),16)
        with other.archive.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM portable_sku_evidence WHERE json_extract(data,'$.sku')='BULK-001'").fetchone()[0],45)
        self.assertEqual(catalog.items(text='BULK-001')['total'],45)

    def test_projection_queue_is_bounded_and_uses_current_business_version(self):
        old=dict(self.dataset.records()[0])
        self.dataset.change([{**old,'sku':'CURRENT-SKU'}])
        self.dataset.imported_records=[{**old,'sku':'OLD-SKU'} for _ in range(600)]
        self.api._seed_imported(self.dataset)
        self.assertEqual(len(self.dataset.imported_records),88)
        with self.archive.connect() as c:
            value=json.loads(c.execute('SELECT data FROM portable_sku_evidence WHERE path=?',(str(self.photos/old['relative_path']),)).fetchone()[0])
        self.assertEqual(value['sku'],'CURRENT-SKU')

    def test_jpg_without_embedded_id_never_inherits_raw_sidecar_file_id(self):
        import uuid
        from metadata_writer import sha
        path=self.photos/'IMG_0001.JPG';raw_id=uuid.uuid4().hex
        path.with_suffix('.CR3').write_bytes(b'isolated raw companion identity fixture')
        payload,target,journal=self.archive.writer.write(path,{'FileId':raw_id,'SKU':'RAW-SKU'},sha(path),_target=path.with_suffix('.xmp'))
        self.archive.writer.commit(payload,target,journal)
        recovered=self.archive.writer.read_asset(path)
        self.assertEqual(recovered['XMP-fk:SKU'],'RAW-SKU')
        self.assertFalse(recovered.get('XMP-fk:FileId'))


    def test_background_metadata_prepare_does_not_block_rating_or_commit_stale_revision(self):
        import threading
        from unittest.mock import patch
        self.api.rate(self.item(2),2,correction=True)
        record=self.dataset.record(path='IMG_0002.JPG')
        with self.dataset.connect() as c:
            task=dict(c.execute("SELECT * FROM tasks WHERE kind='metadata' AND record=? AND revision=?",(record['id'],record['decision_id'])).fetchone())
        entered=threading.Event();release=threading.Event();outcome=[]
        original=self.archive.writer.write
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError('等待测试放行超时')
            return original(*args,**kwargs)
        def run():
            try:outcome.append(self.api._metadata_task(self.dataset,task))
            except BaseException as exc:outcome.append(exc)
        with patch.object(self.archive.writer,'write',side_effect=slow):
            worker=threading.Thread(target=run);worker.start()
            try:
                self.assertTrue(entered.wait(5))
                started=time.monotonic()
                self.api.rate(self.item(2),1)
                self.assertLess(time.monotonic()-started,1.5)
            finally:release.set();worker.join(10)
        self.assertEqual(outcome,['cancelled'])
        self.assertEqual(self.dataset.record(path='IMG_0002.JPG')['rating'],1)
        self.assertNotEqual(self.archive.writer.read(self.photos/'IMG_0002.JPG').get('XMP-xmp:Rating'),2)

    def test_background_scan_reads_do_not_hold_interaction_lock(self):
        import os
        import threading
        from unittest.mock import patch
        entered=threading.Event();release=threading.Event();outcome=[]
        self.api.validated.pop((str(self.photos),'img_0001.jpg'),None)
        changed=self.photos/'IMG_0001.JPG';stamp=changed.stat()
        os.utime(changed,ns=(stamp.st_atime_ns,stamp.st_mtime_ns+1_000_000_000))
        self.api.pending[str(self.photos)]=True
        original=self.archive.writer.read
        def slow(path,*args,**kwargs):
            if Path(path).name=='IMG_0001.JPG':
                entered.set()
                if not release.wait(5):raise AssertionError('等待测试放行超时')
            return original(path,*args,**kwargs)
        def run():
            try:self.api.tick()
            except BaseException as exc:outcome.append(exc)
        with patch.object(self.archive.writer,'read',side_effect=slow):
            worker=threading.Thread(target=run);worker.start()
            try:
                self.assertTrue(entered.wait(5))
                started=time.monotonic()
                _,record=self.api.record_for_item(self.item(4))
                self.assertEqual(record['relative_path'],'IMG_0004.JPG')
                self.assertLess(time.monotonic()-started,1)
            finally:release.set();worker.join(10)
        self.assertFalse(outcome)

    def test_metadata_write_preserves_photo_time_and_rescan_order(self):
        import os
        for n in range(1,5):os.utime(self.photos/f'IMG_{n:04d}.JPG',(1700000000+n,1700000000+n))
        # scan异步入口由现有客户端轮询，直接等后台完成。
        self.catalog.scan(self.rid)
        until=time.monotonic()+10
        while time.monotonic()<until:
            with self.catalog.connect() as c:
                if c.execute('SELECT modified FROM items WHERE id=?',(self.item(1),)).fetchone()[0]==1700000001:break
            time.sleep(.02)
        before=[r['id'] for r in self.catalog.items(root=self.rid)['rows']]
        self.api._scan(self.dataset)
        stamp=(self.photos/'IMG_0002.JPG').stat().st_mtime_ns
        self.api.rate(self.item(2),2,correction=True);self.drain()
        self.assertEqual((self.photos/'IMG_0002.JPG').stat().st_mtime_ns,stamp)
        self.catalog.scan(self.rid)
        until=time.monotonic()+10
        while time.monotonic()<until:
            with self.catalog.connect() as c:
                if c.execute('SELECT size FROM items WHERE id=?',(self.item(2),)).fetchone()[0]==(self.photos/'IMG_0002.JPG').stat().st_size:break
            time.sleep(.02)
        self.assertEqual([r['id'] for r in self.catalog.items(root=self.rid)['rows']],before)

    def test_busy_catalog_writer_defers_projection_and_keeps_latest_sku(self):
        import sqlite3
        record=self.dataset.record(path='IMG_0001.JPG')
        with self.catalog.connect() as locked:
            locked.execute('BEGIN IMMEDIATE')
            started=time.monotonic()
            self.api._project(self.dataset,{**record,'sku':'OLD'})
            self.assertLess(time.monotonic()-started,1)
            self.assertTrue(self.api.projection_pending)
            self.dataset.change([{**record,'sku':'LATEST'}])
        self.api.tick()
        self.assertFalse(self.api.projection_pending)
        self.assertEqual(self.catalog.item(self.item(1))['sku'],'LATEST')

    def test_explicit_ocr_runs_while_initial_scan_is_pending(self):
        from unittest.mock import patch
        self.api.rate(self.item(2),2,correction=True)
        self.api.last_scan.pop(str(self.photos),None)
        self.api.loading[str(self.photos)]=True
        result=self.api.recognize(self.item(2))
        self.assertTrue(result['ocr_queued'])
        calls=[];self.sku.recognizer=lambda *_:calls.append(1) or {'text':'NOW-123','codes':['NOW-123']}
        # 保持一次真实未完成扫描的状态；四张小夹具的热版本读取可能当轮结束。
        with patch.object(self.api,'_prefetch_scan',return_value={}),patch.object(self.api,'_scan'):
            self.api.tick()
        record=self.dataset.record(path='IMG_0002.JPG')
        self.assertEqual(calls,[1]);self.assertEqual(record['ocr']['text'],'NOW-123')
        self.assertTrue(self.api.loading[str(self.photos)])
        self.assertFalse(record.get('sku'))

    def test_optional_modified_sort_cannot_change_original_filename_order(self):
        from unittest.mock import patch
        original=self.catalog.items(root=self.rid,sort='original',limit=2)
        first=original['rows'][0]['id'];last=self.item(4)
        with self.catalog.connect() as c:c.execute('UPDATE items SET modified=? WHERE id=?',(9999999999,last))
        stable=self.catalog.items(root=self.rid,sort='original',limit=2)
        self.assertEqual([r['id'] for r in stable['rows']],[r['id'] for r in original['rows']])
        self.assertEqual(self.catalog.items(root=self.rid,sort='modified')['rows'][0]['id'],last)
        second=self.catalog.items(root=self.rid,sort='original',limit=2,cursor=stable['next_cursor'])
        self.assertEqual(len(set(r['id'] for r in stable['rows']+second['rows'])),4)
        self.assertEqual(second['rows'][-1]['id'],last)

    def test_readonly_scan_refreshes_history_once_per_cycle(self):
        from unittest.mock import patch
        self.api.edit(self.rid,enabled=False)
        self.api.refreshed.clear()
        with patch.object(self.dataset,'refresh',wraps=self.dataset.refresh) as refresh:
            for _ in range(4):self.api._scan(self.dataset,limit=1)
            self.assertEqual(refresh.call_count,1)
        self.assertFalse(self.api.loading[str(self.photos)])

    def test_explicit_ocr_automatically_confirms_the_whole_interval(self):
        self.api.rate(self.item(2),2,correction=True)
        self.api.recognize(self.item(2));self.drain()
        rows={r['relative_path']:r for r in self.dataset.records()}
        self.assertEqual([rows[f'IMG_{n:04d}.JPG'].get('sku','') for n in range(1,5)],['LC123456','LC123456','',''])
        self.assertEqual(rows['IMG_0002.JPG']['ocr_apply_state'],'applied')
        self.assertEqual(self.dataset.groups()[0]['status'],'confirmed')
        self.assertEqual(len(self.dataset.groups()[0]['members']),2)

    def test_initial_scan_defers_application_without_repeating_explicit_ocr(self):
        for n in range(20):Image.new('RGB',(32,32),'teal').save(self.photos/f'LARGE_{n:04d}.JPG')
        self.catalog.scan(self.rid)
        until=time.monotonic()+10
        while time.monotonic()<until:
            with self.catalog.connect() as c:item=c.execute("SELECT id FROM items WHERE name='LARGE_0019.JPG'").fetchone()
            if item:break
            time.sleep(.02)
        self.api.last_scan.pop(str(self.photos),None)
        self.api.rate(item[0],2,correction=True)
        self.api.recognize(item[0]);calls=[]
        self.sku.recognizer=lambda *_:calls.append(1) or {'text':'LC700001','codes':['LC700001']}
        self.api.tick()
        marker=self.dataset.record(path='LARGE_0019.JPG')
        self.assertEqual(marker['ocr_apply_state'],'waiting_interval')
        self.assertFalse(marker.get('sku'))
        self.drain()
        group=next(g for g in self.dataset.groups() if g.get('sku')=='LC700001')
        self.assertEqual(len(group['members']),20)
        self.assertEqual(calls,[1])
        self.assertEqual(sum(r.get('sku')=='LC700001' for r in self.dataset.records()),20)

    def test_upgrade_resumes_stored_ocr_without_recognizing_again(self):
        self.api.rate(self.item(2),2,correction=True)
        record=self.dataset.record(path='IMG_0002.JPG')
        self.dataset.change([{**record,'ocr':{'text':'LC888888','codes':['LC888888']},'candidates':['LC888888'],'ocr_state':'ready'}],kind='ocr_result')
        with self.dataset.connect() as c:c.execute("UPDATE tasks SET status='cancelled' WHERE kind='ocr'")
        self.sku.recognizer=lambda *_:self.fail('已有识别结果不应重新调用模型')
        self.api._recover_tasks(self.dataset)
        self.api._recover_tasks(self.dataset)
        with self.dataset.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE kind='ocr_apply'").fetchone()[0],1)
        self.drain()
        self.assertEqual(self.dataset.record(path='IMG_0001.JPG')['sku'],'LC888888')
        self.assertEqual(self.dataset.groups()[0]['status'],'confirmed')

    def test_manual_confirmation_invalidates_queued_ocr_application(self):
        self.api.rate(self.item(2),2,correction=True);self.api.recognize(self.item(2))
        self.api.tick()
        self.assertEqual(self.dataset.record(path='IMG_0002.JPG')['ocr_apply_state'],'waiting_interval')
        self.api.confirm(self.item(2),'MANUAL-SKU')
        self.drain()
        self.assertEqual(self.dataset.record(path='IMG_0001.JPG')['sku'],'MANUAL-SKU')
        with self.dataset.connect() as c:self.assertEqual(c.execute("SELECT status FROM tasks WHERE kind='ocr_apply'").fetchone()[0],'cancelled')

    def test_capture_scan_uses_path_range_instead_of_global_name_sort(self):
        prefix=str(self.photos)+'\\';upper=prefix[:-1]+chr(ord(prefix[-1])+1)
        with self.catalog.connect() as c:
            plan=c.execute('EXPLAIN QUERY PLAN SELECT rowid,* FROM items INDEXED BY catalog_capture_paths WHERE path>=? AND path<? AND path>? AND online=1 ORDER BY path LIMIT 2',(prefix,upper,prefix)).fetchall()
        details=' '.join(r[3] for r in plan)
        self.assertIn('path>? AND path<?',details)
        self.assertNotIn('TEMP B-TREE',details)
        self.assertEqual(len(self.api._rows(self.dataset,limit=1)),1)

    def test_live_rating_cannot_be_overwritten_by_an_older_folder_scan(self):
        import copy,threading
        from unittest.mock import patch
        import media_engine as media
        sid=self.store.ensure_live_session(self.photos)
        capture=media.capture_record(self.photos,self.photos/'IMG_0001.JPG',None,{})
        self.store.append_live(sid,[capture])
        session=self.store.session(sid);state=copy.deepcopy(session['state']);state['overrides']={capture['id']:3}
        self.store.save(sid,session['revision'],state)
        self.api.validated.pop((str(self.photos),'img_0001.jpg'),None)
        reading=threading.Event();release=threading.Event();saved=threading.Event();errors=[]
        original=self.archive.writer.read
        def read(path,*args,**kwargs):
            if Path(path).name=='IMG_0001.JPG' and kwargs.get('full'):
                reading.set()
                if not release.wait(5):raise AssertionError('未放行目录扫描')
            return original(path,*args,**kwargs)
        def scan():
            try:self.api._scan(self.dataset,focus=self.photos/'IMG_0001.JPG')
            except BaseException as exc:errors.append(exc)
        def save():
            try:self.api.prepare_live(sid);saved.set()
            except BaseException as exc:errors.append(exc)
        with patch.object(self.archive.writer,'read',side_effect=read):
            scanner=threading.Thread(target=scan);scanner.start()
            self.assertTrue(reading.wait(5))
            writer=threading.Thread(target=save);writer.start()
            try:self.assertFalse(saved.wait(.1))
            finally:release.set();scanner.join(10);writer.join(10)
        self.assertFalse(errors)
        self.assertTrue(saved.is_set())
        self.assertEqual(self.dataset.record(path='IMG_0001.JPG')['rating'],3)
        self.assertEqual(self.archive.ratings.get([self.photos/'IMG_0001.JPG']),3)

    def test_inactive_readonly_datasets_do_not_delay_current_scan(self):
        from unittest.mock import patch
        passive=self.base/'历史视频目录';passive.mkdir()
        dataset=self.api._dataset(passive)
        self.api.scope(self.rid)
        self.api.pending={str(passive):True,str(self.photos):True}
        visited=[];original=self.api._prefetch_scan
        def read(current):
            visited.append(current.root)
            if current is dataset:self.fail('未打开的历史只读目录不应阻塞当前区间')
            return original(current)
        with patch.object(self.api,'_prefetch_scan',side_effect=read):self.api.tick()
        self.assertEqual(visited,[self.photos])
        self.assertNotIn(str(passive),self.api.pending)
        dataset.acquire();self.api.pending={str(passive):True}
        with patch.object(self.api,'_prefetch_scan',return_value={}) as read:self.api.tick()
        read.assert_called_once_with(dataset)

    def test_startup_restores_passive_dataset_without_scanning_its_files(self):
        from unittest.mock import patch
        passive=self.base/'历史只读目录';passive.mkdir()
        self.api.restore_roots=[str(passive)]
        with patch.object(self.api,'tick',side_effect=lambda **_:self.api.closed.set()):self.api._run()
        self.assertNotIn(str(passive),self.api.datasets)
        self.assertIn(str(passive),self.api.passive_roots)
        self.assertNotIn(str(passive),self.api.pending)


if __name__ == '__main__':
    unittest.main()
