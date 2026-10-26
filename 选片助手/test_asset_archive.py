import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
from PIL import Image
from catalog_store import Store
import media_engine as media
from asset_archive import AssetArchive,uid
from metadata_writer import sha
from model_providers import Providers
from sku_service import SkuService
from test_sku_workflow import fixture_book,CODE,reading

ROOT=Path(__file__).resolve().parents[1]

class AssetTests(unittest.TestCase):
    def test_initialized_ratings_does_not_take_archive_lock_inside_sqlite_transaction(self):
        import threading
        ratings=self.archive.ratings;finished=threading.Event();failures=[]
        def read():
            try:
                with self.archive.connect() as c:
                    c.execute('INSERT INTO audit VALUES (?,?,?,?)',('rating-lock-test','test','{}',0))
                    self.assertIs(self.archive.ratings,ratings)
                finished.set()
            except Exception as exc:failures.append(exc)
        worker=threading.Thread(target=read)
        try:
            with self.archive.lock:
                worker.start();self.assertTrue(finished.wait(2),'评级读取在 SQLite 事务内等待归档锁')
        finally:worker.join(3)
        self.assertFalse(failures)

    def test_worker_survives_busy_database_even_when_audit_write_is_also_busy(self):
        import sqlite3
        calls=[]
        def tick():
            calls.append(1)
            if len(calls)==1:raise sqlite3.OperationalError('database is locked')
            self.archive.closed.set()
        with patch.object(self.archive,'tick',side_effect=tick),patch.object(self.archive,'connect',side_effect=sqlite3.OperationalError('database is locked')):
            self.archive._run()
        self.assertEqual(len(calls),2);self.assertEqual(self.archive.worker_error,'database is locked')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos);self.store.activate(self.sid)
        self.archive=AssetArchive(self.store,autostart=False)
    def tearDown(self):self.archive.close();self.temp.cleanup()
    def add(self,name):
        path=self.photos/name;Image.new('RGB',(64,48),(len(name)*4,len(self.store.records(self.sid))*30,120)).save(path)
        r=media.capture_record(self.photos,path,None,{});self.store.append_live(self.sid,[r]);return r['id']
    def process(self,op):
        for _ in range(20):
            if not any(j['status']=='queued' for j in self.archive.operation(op)['jobs']):break
            self.archive.process_one()
        return self.archive.operation(op)
    def paired_copy(self,orphan=False):
        source=self.add('pair.JPG');aid=self.archive.ensure(self.sid,source)
        target=self.base/'paired';target.mkdir();shutil.copy2(self.photos/'pair.JPG',target/'pair.JPG')
        raw=target/'pair.ARW';raw.write_bytes(b'raw component of imported JPG')
        sid=self.store.ensure_live_session(target)
        if orphan:
            r=media.capture_record(target,raw,None,{})
            self.store.append_live(sid,[r])
            with patch.object(self.archive.writer,'read',return_value={}):old=self.archive.ensure(sid,r['id'])
            with self.archive.connect() as c:c.execute('DELETE FROM catalog_links WHERE session=?',(sid,))
        else:old=None
        r=media.capture_record(target,raw,target/'pair.JPG',{});self.store.append_live(sid,[r])
        return aid,sid,r['id'],old
    def test_paired_copy_uses_existing_jpg_identity_before_creating_raw(self):
        aid,sid,cid,_=self.paired_copy()
        with patch.object(self.archive.writer,'read',return_value={}):
            self.assertEqual(self.archive.ensure(sid,cid),aid);self.assertEqual(self.archive.ensure(sid,cid),aid)
        with self.archive.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM assets').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM files WHERE asset=?',(aid,)).fetchone()[0],2)
    def test_interrupted_unreferenced_raw_identity_is_recovered_with_audit(self):
        aid,sid,cid,old=self.paired_copy(True)
        with patch.object(self.archive.writer,'read',return_value={}):self.assertEqual(self.archive.ensure(sid,cid),aid)
        with self.archive.connect() as c:
            self.assertIsNone(c.execute('SELECT 1 FROM assets WHERE id=?',(old,)).fetchone())
            self.assertEqual(c.execute("SELECT count(*) FROM audit WHERE kind='recover_incomplete_pair'").fetchone()[0],1)
    def test_existing_raw_ownership_or_portable_identity_is_never_merged(self):
        aid,sid,cid,old=self.paired_copy(True)
        for used in ('linked','assigned','portable'):
            with self.subTest(used=used),self.archive.connect() as c:
                c.execute('DELETE FROM catalog_links WHERE session=?',(sid,));c.execute("UPDATE assets SET sku='',revision=0 WHERE id=?",(old,))
                if used=='linked':c.execute('INSERT INTO catalog_links VALUES (?,?,?)',(sid,cid,old))
                if used=='assigned':c.execute("UPDATE assets SET sku='ORIGINAL',revision=1 WHERE id=?",(old,))
            with patch.object(self.archive.writer,'read',return_value={'XMP-fk:AssetId':old} if used=='portable' else {}):
                with self.assertRaisesRegex(ValueError,'冲突'):self.archive.ensure(sid,cid)
            with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM files WHERE asset=?',(old,)).fetchone()[0],1)
    def test_component_read_failure_does_not_leave_partial_asset(self):
        aid,sid,cid,_=self.paired_copy()
        def read(path):
            if Path(path).suffix=='.JPG':raise ValueError('test read failure')
            return {}
        with patch.object(self.archive.writer,'read',side_effect=read):
            with self.assertRaisesRegex(ValueError,'read failure'):self.archive.ensure(sid,cid)
        with self.archive.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM assets').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM files').fetchone()[0],1)
    def test_jpeg_portable_identity_preserves_rating_and_pixels_and_reverts(self):
        cid=self.add('001.JPG');p=self.photos/'001.JPG';self.archive.writer.run('-overwrite_original','-XMP-xmp:Rating=4','-XMP-dc:Subject=original','-DateTimeOriginal=2026:10:20 13:14:15',p)
        before=Image.open(p).convert('RGB').tobytes();original_sha=sha(p)
        preview=self.archive.preview(self.sid,[cid],CODE,'细节图');op=self.archive.apply(preview['preview_id'])['operation']['id'];result=self.process(op)
        self.assertEqual([j['status'] for j in result['jobs']],['applied'],result)
        fields=self.archive.writer.read(p);self.assertEqual(fields['XMP-fk:SKU'],CODE);self.assertEqual(fields['XMP-xmp:Rating'],4);self.assertIn('original',fields['XMP-dc:Subject']);self.assertEqual(Image.open(p).convert('RGB').tobytes(),before)
        self.assertNotEqual(sha(p),original_sha)
        dest=self.base/'moved';dest.mkdir();shutil.copy2(p,dest/'different.JPG');self.archive.relocate(str(dest))
        rows=self.archive.query(CODE)['rows'];self.assertEqual(len(rows),1);self.assertEqual(len(rows[0]['locations']),2)
        revert=self.archive.revert(op)['operation']['id'];self.process(revert)
        self.assertFalse(self.archive.writer.read(p).get('XMP-fk:SKU'));self.assertEqual(self.archive.writer.read(p)['XMP-xmp:Rating'],4)
        self.assertEqual(self.archive.asset(rows[0]['id'])['sku'],'')
    def test_external_changes_and_assignment_revision_are_not_overwritten(self):
        cid=self.add('001.JPG');preview=self.archive.preview(self.sid,[cid],CODE,'模特图')
        p=self.photos/'001.JPG';Image.new('RGB',(20,20),'red').save(p)
        op=self.archive.apply(preview['preview_id'])['operation']['id'];result=self.process(op)
        self.assertEqual(result['jobs'][0]['status'],'failed');self.assertIn('修改',result['jobs'][0]['reason'])
        p1=self.archive.preview(self.sid,[cid],'SKU1234','模特图',overwrite=True);p2=self.archive.preview(self.sid,[cid],'SKU1235','模特图',overwrite=True)
        self.archive.apply(p1['preview_id'])
        with self.assertRaises(ValueError):self.archive.apply(p2['preview_id'])
    def test_manual_lock_blocks_auto_assignment_and_preview_is_read_only(self):
        cid=self.add('001.JPG');p=self.photos/'001.JPG';before=sha(p)
        p1=self.archive.preview(self.sid,[cid],CODE,'模特图');self.assertEqual(sha(p),before)
        self.archive.apply(p1['preview_id']);aid=self.archive.ensure(self.sid,cid)
        auto=self.archive.preview(None,None,'OTHER123','模特图',asset_ids=[aid],source='segment')
        self.assertFalse(auto['targets'][0]['apply'])
        with self.assertRaises(ValueError):self.archive.apply(auto['preview_id'])
    def test_recovery_after_file_commit_before_database_commit(self):
        cid=self.add('001.JPG');preview=self.archive.preview(self.sid,[cid],CODE,'细节图');op=self.archive.apply(preview['preview_id'])['operation']['id']
        with self.archive.connect() as c:job=dict(c.execute('SELECT * FROM metadata_jobs WHERE operation=?',(op,)).fetchone())
        temp,target,journal=self.archive.writer.write(job['path'],json.loads(job['fields']),job['expected_sha'])
        with self.archive.connect() as c:c.execute("UPDATE metadata_jobs SET journal=?,status='running' WHERE id=?",(json.dumps(journal),job['id']))
        temp.replace(target);self.archive.close();self.archive=AssetArchive(self.store,autostart=False)
        with patch.object(self.archive.writer,'write',side_effect=AssertionError('不得重复写入')):result=self.process(op)
        self.assertEqual(result['jobs'][0]['status'],'applied')
    def test_pending_failed_label_is_still_a_separate_boundary(self):
        # 首款可先拍产品，启动时自动纳入未归组照片。
        old=self.add('old.JPG');stream=self.archive.start_stream(self.sid,'p','模特图')['streams'][0]['id']
        a=self.add('a.JPG');label1=self.add('label1.JPG');b=self.add('b.JPG');label2=self.add('label2.JPG')
        def job(id,cid):return {'id':id,'capture_id':cid,'session_id':self.sid,'project':'p','scope':'模特图','status':'failed'}
        j1=job('one',label1);j2=job('two',label2);self.archive.reserve(j1);self.archive.reserve(j2)
        segments=self.archive.stream_state()['segments'];by_job={s['job']:s for s in segments}
        self.assertEqual(set(by_job['one']['members']),{self.archive.ensure(self.sid,old),self.archive.ensure(self.sid,a),self.archive.ensure(self.sid,label1)})
        self.assertEqual(set(by_job['two']['members']),{self.archive.ensure(self.sid,b),self.archive.ensure(self.sid,label2)})
        self.assertIn(self.archive.ensure(self.sid,old),by_job['one']['members'])
        self.archive.handle_event('confirm',dict(j2,status='matched'),CODE)
        confirmed=next(s for s in self.archive.stream_state()['segments'] if s['job']=='two')
        self.assertEqual(confirmed['status'],'confirmed');self.assertTrue(confirmed['operation'])
    def test_first_product_before_start_repeated_start_and_scope_isolation(self):
        product=self.add('first-product.JPG');label=self.add('label-a.JPG')
        first=self.archive.start_stream(self.sid,'p','模特图')['streams'][0]
        self.assertEqual(first['pending'],2)
        again=self.archive.start_stream(self.sid,'p','模特图')['streams'][0]
        self.assertEqual(again['id'],first['id']);self.assertEqual(again['pending'],2)
        job={'id':'first','capture_id':label,'session_id':self.sid,'project':'p','scope':'模特图','status':'matched'}
        self.archive.reserve(job);self.archive.handle_event('first',job,CODE)
        seg=self.archive.stream_state()['segments'][0];self.process(seg['operation'])
        self.assertEqual(self.archive.writer.read(self.photos/'first-product.JPG')['XMP-fk:SKU'],CODE)
        # 重复接续不重复归入上一款，下一类型也不接管上一段未结束产品。
        pending=self.add('unfinished.JPG');self.archive.sync_stream()
        other=self.archive.start_stream(self.sid,'p','细节图')['streams'][0]
        self.assertEqual(other['pending'],0)
        second=self.add('detail.JPG');end=self.add('label-b.JPG')
        j2=dict(job,id='second',capture_id=end,scope='细节图');self.archive.reserve(j2)
        members=next(s for s in self.archive.stream_state()['segments'] if s['job']=='second')['members']
        self.assertEqual(set(members),{self.archive.ensure(self.sid,second),self.archive.ensure(self.sid,end)})
        self.assertNotIn(self.archive.ensure(self.sid,pending),members)

    def test_legacy_stream_recovers_skipped_first_products_before_first_label(self):
        first=self.add('first.JPG')
        state=self.archive.start_stream(self.sid,'p','模特图')
        stream=state['streams'][0]['id']
        with self.archive.connect() as c:
            c.execute('DELETE FROM bindings WHERE stream=?',(stream,))
            c.execute('UPDATE streams SET seen=? WHERE id=?',(json.dumps([first]),stream))
        label=self.add('label.JPG')
        # 相机只有秒级时间时，接回首图仍须位于款末标签之前。
        with self.store.connect() as c:
            for record in self.store.records(self.sid):
                record['time']='2026-10-20T10:00:00'
                c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(json.dumps(record),self.sid,record['id']))
        self.archive.sync_stream()
        job={'id':'legacy-first','capture_id':label,'session_id':self.sid,'project':'p','scope':'模特图','status':'matched'}
        self.archive.reserve(job)
        seg=self.archive.stream_state()['segments'][0]
        self.assertEqual(set(seg['members']),{self.archive.ensure(self.sid,first),self.archive.ensure(self.sid,label)})

    def test_real_sku_outbox_metadata_write_and_cancel_compensation(self):
        providers=Providers(self.store.data);p=providers.save({'name':'mock','category':'multimodal','protocol':'chat','model':'mock','url':'https://example.invalid'});providers.select(p['providers'][0]['id'])
        sku=SkuService(self.store,providers,archive=self.archive,recognizer=lambda p,b:reading(),autostart=False);self.archive.sku=sku
        try:
            pid=sku.import_file('list.xlsx',fixture_book(),self.sid)['project']['id'];sku.configure(pid,self.sid,'模特图')
            self.assertTrue(self.archive.stream_state()['streams'][0]['auto_write'])
            product=self.add('product.JPG');cid=self.add('label.JPG');session=self.store.session(self.sid);before=copy.deepcopy(session['state']);state=copy.deepcopy(before);state.setdefault('overrides',{})[cid]=2
            self.store.save(self.sid,session['revision'],state);sku.on_save(self.sid,before,state)
            with sku.connect() as c:c.execute('UPDATE jobs SET due=0')
            sku.tick();sku.deliver_events(self.archive)
            seg=self.archive.stream_state()['segments'][0];self.assertEqual(seg['status'],'confirmed');self.process(seg['operation'])
            self.assertEqual(self.archive.writer.read(self.photos/'product.JPG')['XMP-fk:SKU'],CODE)
            session=self.store.session(self.sid);old=copy.deepcopy(session['state']);new=copy.deepcopy(old);new['overrides'][cid]=3;self.store.save(self.sid,session['revision'],new);sku.on_save(self.sid,old,new);sku.deliver_events(self.archive)
            for _ in range(5):self.archive.process_one()
            self.assertFalse(self.archive.writer.read(self.photos/'product.JPG').get('XMP-fk:SKU'))
        finally:sku.close()
    def test_switch_scope_freezes_unsynced_label_and_keeps_auto_preference(self):
        self.archive.start_stream(self.sid,'p','模特图',True,'embedded')
        product=self.add('a.JPG');label=self.add('label.JPG')
        self.archive.start_stream(self.sid,'p','细节图')
        job={'id':'late-click','capture_id':label,'session_id':self.sid,'project':'p','scope':'模特图','status':'matched'}
        self.archive.reserve(job);self.archive.handle_event('frozen',job,CODE)
        state=self.archive.stream_state();self.assertTrue(state['streams'][0]['auto_write'])
        self.assertEqual(state['segments'][0]['status'],'confirmed');self.assertEqual(len(state['segments'][0]['members']),2)
    def test_old_disabled_stream_and_ready_segment_resume_automatically(self):
        self.archive.start_stream(self.sid,'p','模特图',False)
        product=self.add('product.JPG');label=self.add('label.JPG')
        job={'id':'legacy','capture_id':label,'session_id':self.sid,'project':'p','scope':'模特图','status':'matched'}
        self.archive.reserve(job)
        with self.archive.connect() as c:
            c.execute('UPDATE streams SET auto_write=0')
            c.execute("UPDATE segments SET sku=?,status='ready'",(CODE,))
        self.archive.close();self.archive=AssetArchive(self.store,autostart=False)
        self.assertTrue(self.archive.stream_state()['streams'][0]['auto_write'])
        self.archive.tick();seg=self.archive.stream_state()['segments'][0]
        self.assertEqual(seg['status'],'confirmed');self.process(seg['operation'])
        self.assertEqual(self.archive.writer.read(self.photos/'product.JPG')['XMP-fk:SKU'],CODE)
        self.archive.configure_stream(seg['stream'],False,'sidecar')
        self.assertTrue(self.archive.stream_state()['streams'][0]['auto_write'])
    def test_manual_batch_correction_is_locked_against_later_auto_result(self):
        self.archive.start_stream(self.sid,'p','模特图')
        product=self.add('product.JPG');label=self.add('label.JPG')
        job={'id':'label','capture_id':label,'session_id':self.sid,'project':'p','scope':'模特图','status':'matched'}
        self.archive.reserve(job);self.archive.handle_event('first',job,CODE)
        seg=self.archive.stream_state()['segments'][0];self.process(seg['operation'])
        preview=self.archive.preview(self.sid,[product,label],'MANUAL123','模特图',overwrite=True)
        self.process(self.archive.apply(preview['preview_id'])['operation']['id'])
        self.archive.handle_event('changed-result',job,'OTHER123')
        self.assertEqual(self.archive.stream_state()['segments'][0]['status'],'needs_review')
        for cid in [product,label]:
            asset=self.archive.asset(self.archive.ensure(self.sid,cid))
            self.assertEqual(asset['sku'],'MANUAL123');self.assertTrue(asset['locked'])
    def test_late_raw_component_inherits_existing_assignment_and_undo(self):
        cid=self.add('pair.JPG');self.archive.start_stream(self.sid,'p','细节图',False,'sidecar',cid)
        p=self.archive.preview(self.sid,[cid],CODE,'细节图',mode='sidecar');op=self.archive.apply(p['preview_id'])['operation']['id'];self.process(op)
        raw=self.photos/'pair.ARW';raw.write_bytes(b'mock RAW bytes for pairing only')
        record=media.capture_record(self.photos,raw,self.photos/'pair.JPG',{});self.store.append_live(self.sid,[record])
        original_read=self.archive.writer.read
        with patch.object(self.archive.writer,'read',side_effect=lambda path:{} if Path(path).suffix=='.ARW' else original_read(path)):self.archive.sync_stream()
        result=self.process(op);self.assertEqual(len(result['jobs']),2);self.assertTrue(all(j['status']=='applied' for j in result['jobs']),result)
        self.assertEqual(original_read(raw.with_suffix('.xmp'))['XMP-fk:SKU'],CODE);self.assertEqual(self.archive.query(CODE)['total'],1)
        revert=self.archive.revert(op)['operation']['id'];self.process(revert);self.assertFalse(original_read(raw.with_suffix('.xmp')).get('XMP-fk:SKU'))
    def test_resuming_legacy_stream_uses_sidecar_for_new_work_and_keeps_history(self):
        cid=self.add('legacy.JPG');state=self.archive.start_stream(self.sid,'p','细节图',mode='sidecar');stream=state['streams'][0]['id']
        p=self.archive.preview(self.sid,[cid],CODE,'细节图',mode='embedded');op=self.archive.apply(p['preview_id'])['operation']['id'];self.process(op)
        state=self.archive.start_stream(self.sid,'p','细节图')
        self.assertEqual(state['streams'][0]['id'],stream);self.assertEqual(state['streams'][0]['mode'],'sidecar')
        self.archive.configure_stream(stream,True,'sidecar');self.archive.close();self.archive=AssetArchive(self.store,autostart=False)
        self.assertEqual(self.archive.stream_state()['streams'][0]['mode'],'sidecar')
        self.assertTrue(all(j['mode']=='embedded' for j in self.archive.operation(op)['jobs']))
        automatic=self.archive.preview(None,None,CODE,'细节图',mode='sidecar',asset_ids=[self.archive.ensure(self.sid,cid)],source='segment')
        self.assertEqual(automatic['mode'],'sidecar')

    def test_compact_history_undo_preserves_pixels_and_does_not_accumulate_images(self):
        cid=self.add('compact.JPG');photo=self.photos/'compact.JPG';pixels=Image.open(photo).convert('RGB').tobytes()
        preview=self.archive.preview(self.sid,[cid],CODE,'细节图')
        op=self.archive.apply(preview['preview_id'])['operation']['id'];result=self.process(op)
        self.assertTrue(all(j['status']=='applied' for j in result['jobs']),result)
        self.assertFalse((self.store.data/'metadata-backups').exists())
        self.assertFalse(list(self.photos.glob('.framekeep-recovery-*')))
        records=list((self.store.data/'metadata-history').glob('*.json'));self.assertEqual(len(records),1)
        saved=json.loads(records[0].read_text('utf8'));self.assertEqual(saved['journal']['after']['SKU'],CODE)
        self.assertLess(records[0].stat().st_size,8000)
        undo=self.archive.revert(op)['operation']['id'];result=self.process(undo)
        self.assertTrue(all(j['status']=='applied' for j in result['jobs']),result)
        self.assertFalse(self.archive.writer.read(photo).get('XMP-fk:SKU'))
        self.assertEqual(Image.open(photo).convert('RGB').tobytes(),pixels)
        self.assertFalse(list(self.photos.glob('.framekeep-recovery-*')))

    def test_default_raw_sidecar_leaves_raw_bytes_unchanged(self):
        raw=self.photos/'compact.ARW';raw.write_bytes(b'RAW payload must stay untouched'*10000);before=sha(raw)
        fields={'SKU':CODE,'AssetId':uid(),'FileId':uid(),'ShootType':'细节图'}
        temp,target,journal=self.archive.writer.write(raw,fields,before)
        self.assertFalse([p for p in self.photos.iterdir() if p.is_dir()])
        self.assertEqual(temp.parent,self.photos)
        self.assertEqual(target,raw.with_suffix('.xmp'));self.assertEqual(journal['mode'],'sidecar')
        from file_safety import replace_verified
        replace_verified(temp,target,None);self.archive.writer.discard_prepared(temp)
        self.assertEqual(sha(raw),before);self.assertLess(target.stat().st_size,8000)
        self.assertEqual(self.archive.writer.read(target)['XMP-fk:SKU'],CODE)

if __name__=='__main__':unittest.main()
