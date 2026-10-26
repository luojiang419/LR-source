import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from PIL import Image
from asset_archive import AssetArchive
from catalog_store import Store
import media_engine as media
from model_providers import Providers
from sku_match import match_automatic,normalize
from sku_service import SkuService
from test_sku_workflow import fixture_book,CODE,reading

ROOT=Path(__file__).resolve().parents[1]


class FullAutoMatchTests(unittest.TestCase):
    def rows(self):return [{'id':str(i),'sku':v,'normalized':normalize(v),'sheet':'模特图','excel_row':i+2} for i,v in enumerate([CODE,'LC7877496-P804'])]
    def test_unique_exact_and_fuzzy_accept_unscored_without_fake_confidence(self):
        for code in (CODE,'LC7877496-P9O5'):
            r=reading(code,confidence=0);r['unscored']=True
            result=match_automatic([r],self.rows());self.assertEqual(result['status'],'matched');self.assertEqual(result['row_ids'],['0']);self.assertEqual(r['confidence'],0)
    def test_competing_codes_near_ties_duplicates_and_unknown_are_reviewed(self):
        r=reading();r['codes']=[CODE,'LC7877496-P804']
        self.assertEqual(match_automatic([r],self.rows())['status'],'review')
        rows=self.rows()+[dict(self.rows()[0],id='near',sku='LC7877496-P9Q5',normalized=normalize('LC7877496-P9Q5'))]
        self.assertEqual(match_automatic([reading('LC7877496-P9O5')],rows)['status'],'review')
        self.assertEqual(match_automatic([reading()],self.rows()+[dict(self.rows()[0],id='duplicate')])['status'],'review')
        self.assertEqual(match_automatic([reading('UNKNOWN9999')],self.rows())['status'],'review')


class FullAutoWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos);self.store.activate(self.sid)
        self.cids=[]
        for i in range(3):
            p=self.photos/f'{i}.JPG';Image.new('RGB',(100,80),(i*50,80,100)).save(p);r=media.capture_record(self.photos,p,None,{});self.store.append_live(self.sid,[r]);self.cids.append(r['id'])
        self.ratings=dict(zip(self.cids,[0,5,-1]));self.store.save(self.sid,0,{'overrides':self.ratings})
        self.providers=Providers(self.store.data);self.providers.save({'name':'test OCR','category':'ocr','protocol':'chat','model':'test','url':'https://example.invalid/v1','select':True})
        self.archive=AssetArchive(self.store,autostart=False);self.calls=[];self.clock=[100.]
        def recognize(provider,image):
            self.calls.append(1);return reading([CODE,'LC7877496-P9O5','UNKNOWN9999'][(len(self.calls)-1)%3],confidence=0)
        self.sku=SkuService(self.store,self.providers,recognizer=recognize,clock=lambda:self.clock[0],autostart=False,archive=self.archive);self.archive.sku=self.sku
        self.pid=self.sku.import_file('plan.xlsx',fixture_book())['project']['id']
        self.sku.configure(self.pid,self.sid,'模特图')
    def tearDown(self):self.sku.close();self.archive.close();self.temp.cleanup()
    def tick(self,n=1):
        for _ in range(n):self.clock[0]+=2;self.sku.tick()
    def jobs(self):return self.sku.state()['jobs']

    def test_rating_xmp_sync_does_not_repeat_completed_ocr(self):
        self.sku.set_full_auto(True,self.sid);self.tick(3)
        self.assertEqual(len(self.calls),3)
        self.archive.ratings.seed_session(self.sid)
        self.archive.ratings.set([self.photos/'0.JPG'],4)
        for _ in range(20):
            if not self.archive.ratings.sync.process_one():break
        self.tick(3)
        self.assertEqual(len(self.calls),3)
        self.assertEqual(len(self.jobs()),3)

    def test_two_star_label_precedes_existing_background_scan_and_keeps_single_job(self):
        self.sku.set_full_auto(True);self.sku._queue_full_auto()
        before=self.store.session(self.sid)['state'];after=copy.deepcopy(before);after['overrides'][self.cids[-1]]=2
        session=self.store.session(self.sid);self.store.save(self.sid,session['revision'],after);self.sku.on_save(self.sid,before,after)
        seen=[]
        original=self.sku._valid
        def valid(job):seen.append(job['capture_id']);return original(job)
        self.sku._valid=valid;self.sku.recognizer=lambda *_:reading(CODE,confidence=0)
        self.tick();self.assertEqual(seen[0],self.cids[-1]);self.assertEqual(len(self.jobs()),3)
        self.assertEqual(next(j for j in self.jobs() if j['capture_id']==self.cids[-1])['status'],'matched')

    def test_two_star_observed_suffix_archives_whole_first_interval_without_changing_plan(self):
        from sku_library import labels,browse
        self.sku.set_full_auto(True);self.sku._queue_full_auto()
        record=self.store.records(self.sid)[-1];state=copy.deepcopy(self.store.session(self.sid)['state']);state['overrides'][record['id']]=2
        session=self.store.session(self.sid);self.store.save(self.sid,session['revision'],state)
        actual='LC7877496-P904';self.sku.recognizer=lambda *_:reading(actual,confidence=0)|{'unscored':True}
        # 老手动 OCR 比队列创建晚，但比本次完成早，不能遮住新完成的真实款号。
        with self.sku.connect() as c:c.execute('INSERT INTO parse_jobs(id,session_id,capture_id,status,created,result) VALUES (?,?,?,?,?,?)',('old-manual',self.sid,record['id'],'completed',101,json.dumps({'codes':[actual,'261009 G82']})))
        self.tick()
        job=next(j for j in self.jobs() if j['capture_id']==record['id']);self.assertEqual(job['status'],'matched');self.assertTrue(job['result']['starred_label'])
        self.assertEqual(self.archive.query(actual)['total'],3)
        self.assertEqual(self.sku.rows(self.pid,'模特图')[0]['status'],'未拍摄')
        added=[r for r in self.sku.rows(self.pid) if r['sku']==actual];self.assertEqual(len(added),1);self.assertEqual(added[0]['sheet'],'临时插拍')
        self.assertEqual(labels(self.sku,self.sid)['labels'],{cid:actual for cid in self.cids});self.assertEqual(browse(self.sku)['total'],3)
        from capture_parsing import capture_status
        self.assertEqual(capture_status(self.store,self.sku,self.archive,None,self.sid,record['id'])['ocr']['codes'],[actual])
        self.assertEqual(self.archive.query(CODE)['total'],0)
        for _ in range(4):self.archive.process_one()
        self.assertEqual(self.sku._rating(self.sid,self.store.records(self.sid)[-1]),2)

    def test_unread_two_star_label_blocks_next_interval_from_crossing_it(self):
        from sku_library import labels
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides'].update({self.cids[1]:2,self.cids[2]:2});self.store.save(self.sid,session['revision'],state)
        self.sku.set_full_auto(True);calls=[]
        def read(*_):
            calls.append(1)
            return {'codes':[],'text':'','unscored':True,'confidence':0} if len(calls)==1 else reading(CODE,confidence=0)
        self.sku.recognizer=read;self.tick(2)
        self.assertEqual(labels(self.sku,self.sid)['labels'],{self.cids[2]:CODE})
        self.assertEqual(self.archive.query(CODE)['total'],1)

    def test_removing_two_star_marker_stops_extending_its_full_auto_result(self):
        from sku_library import labels
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides'][self.cids[-1]]=2;self.store.save(self.sid,session['revision'],state)
        self.sku.recognizer=lambda *_:reading(CODE,confidence=0);self.sku.set_full_auto(True);self.tick()
        self.assertEqual(len(labels(self.sku,self.sid)['labels']),3)
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides'][self.cids[-1]]=1;self.store.save(self.sid,session['revision'],state)
        self.sku.on_save(self.sid,session['state'],state);self.sku.deliver_events(self.archive)
        self.assertEqual(labels(self.sku,self.sid)['labels'],{self.cids[-1]:CODE})

    def test_two_star_retry_of_prior_review_happens_once(self):
        self.sku.set_full_auto(True);self.sku.recognizer=lambda *_:reading('UNKNOWN9999',confidence=0);self.tick(3)
        record=self.store.records(self.sid)[-1];before=self.store.session(self.sid)['state'];after=copy.deepcopy(before);after['overrides'][record['id']]=2
        session=self.store.session(self.sid);self.store.save(self.sid,session['revision'],after);self.sku.on_save(self.sid,before,after)
        self.sku.recognizer=lambda *_:reading(CODE,confidence=0);self.tick()
        job=next(j for j in self.jobs() if j['capture_id']==record['id']);self.assertEqual(job['status'],'matched')
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs WHERE capture_id=?',(record['id'],)).fetchone()[0],1)
    def test_all_ratings_scan_fuzzy_enters_library_and_records_survive_metadata(self):
        self.sku.set_full_auto(True,self.sid);self.tick(3)
        self.assertEqual(len(self.calls),3);self.assertEqual(sorted(j['status'] for j in self.jobs()),['matched','matched','review'])
        self.assertEqual(self.archive.query(CODE)['total'],2)
        self.assertEqual(self.store.session(self.sid)['state']['overrides'],self.ratings)
        rows=self.sku.rows(self.pid,'模特图');self.assertEqual(rows[0]['status'],'已拍摄');self.assertEqual(rows[1]['status'],'未拍摄')
        for _ in range(8):self.archive.process_one()
        self.tick(2);self.assertEqual(len(self.calls),3);self.assertEqual(len(self.jobs()),3)
        with self.archive.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM metadata_jobs WHERE status='applied'").fetchone()[0],2)
        reopened=SkuService(self.store,self.providers,autostart=False,archive=self.archive)
        try:self.assertTrue(reopened.full_auto_state()['enabled'])
        finally:reopened.close()
        # 后续新增图片也扫描，不需要标二星。
        p=self.photos/'new.JPG';Image.new('RGB',(100,80),'red').save(p);self.store.append_live(self.sid,[media.capture_record(self.photos,p,None,{})]);self.tick();self.assertEqual(len(self.calls),4)
    def test_bound_document_and_internal_records_can_enable(self):
        with self.assertRaises(ValueError):self.sku.set_full_auto(True,'another-session')
        self.sku.configure(self.pid,self.sid,'模特图',False);self.assertTrue(self.sku.set_full_auto(True)['enabled'])
        self.sku.set_full_auto(False)
        self.sku._automatic_project(self.sid)
        with self.sku.connect() as c:c.execute('UPDATE projects SET metadata=? WHERE id=?',(json.dumps({'automatic_day':'2026-10-21','sheets':[]}),self.pid))
        self.assertTrue(self.sku.set_full_auto(True)['enabled'])

    def test_enabled_queue_explains_missing_model_without_changing_user_selection(self):
        self.sku.set_full_auto(True)
        self.providers.delete(self.providers.selected('ocr')['id'])
        state=self.sku.full_auto_state()
        self.assertTrue(state['enabled']);self.assertFalse(state['model_ready']);self.assertIn('使用内置模型',state['model_message'])
        self.tick();self.assertEqual(self.jobs()[0]['status'],'waiting_model');self.assertEqual(self.calls,[])
    def test_stale_document_or_scope_request_cannot_enable_new_context(self):
        state=self.sku.full_auto_state();self.assertEqual(state['project_id'],self.pid)
        self.sku.configure(self.pid,self.sid,'细节图')
        with self.assertRaisesRegex(ValueError,'工作表已变化'):self.sku.set_full_auto(True,self.sid,self.pid,'模特图')
        self.assertFalse(self.sku.full_auto_state()['enabled'])
        self.sku.import_file('new.xlsx',fixture_book())
        new=self.sku.project()['id'];self.sku.configure(new,self.sid,'模特图')
        with self.assertRaisesRegex(ValueError,'文档已变化'):self.sku.set_full_auto(True,self.sid,self.pid,'模特图')
        self.assertFalse(self.sku.full_auto_state()['enabled'])
        self.assertTrue(self.sku.set_full_auto(True,self.sid,new,'模特图')['enabled'])
        with self.assertRaisesRegex(ValueError,'文档已变化'):self.sku.set_full_auto(False,self.sid,self.pid,'模特图')
        self.assertTrue(self.sku.full_auto_state()['enabled'])
    def test_switch_off_during_request_never_enters_library(self):
        entered=threading.Event();release=threading.Event()
        def recognize(*_):entered.set();release.wait(5);return reading(CODE,confidence=0)
        self.sku.recognizer=recognize;self.sku.set_full_auto(True)
        thread=threading.Thread(target=self.sku.tick);thread.start();self.assertTrue(entered.wait(3))
        self.sku.set_full_auto(False);release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(self.archive.query(CODE)['total'],0)
        self.assertTrue(all(j['status']=='cancelled' for j in self.jobs()))
    def test_document_switch_and_general_manual_mode_disable_full_auto(self):
        self.sku.set_full_auto(True);self.sku.configure(self.pid,self.sid,'细节图');self.assertFalse(self.sku.full_auto_state()['enabled'])
        self.sku.set_full_auto(True);self.sku.configure_tasks(mode='manual');self.assertFalse(self.sku.full_auto_state()['enabled'])
    def test_enabling_full_auto_preserves_ordinary_manual_mode(self):
        self.sku.configure_tasks(mode='manual');self.sku.set_full_auto(True);self.tick()
        self.assertEqual(self.sku.task_mode,'manual');self.assertTrue(self.sku.full_auto_state()['enabled'])
        self.assertEqual(self.jobs()[0]['status'],'matched')
    def test_existing_manual_assignment_is_preserved(self):
        self.archive.apply(self.archive.preview(self.sid,[self.cids[0]],'MANUAL-SKU','模特图',self.pid)['preview_id'])
        self.sku.recognizer=lambda *_:reading(CODE,confidence=0);self.sku.set_full_auto(True);self.tick()
        self.assertEqual(self.jobs()[0]['status'],'review')
        self.assertEqual(self.archive.query('MANUAL-SKU')['total'],1)
    def test_ignore_is_not_immediately_requeued_and_star_changes_do_not_cancel(self):
        self.sku.recognizer=lambda *_:reading(CODE,confidence=0);self.sku.set_full_auto(True);self.tick()
        job=self.jobs()[0];self.sku.review(job['id'],'ignore');self.tick(3)
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()[0],'ignored')
        other=next(j for j in self.jobs() if j['status']=='matched');session=self.store.session(self.sid);before=copy.deepcopy(session['state']);after=copy.deepcopy(before);before['overrides'][other['capture_id']]=2;after['overrides'][other['capture_id']]=1
        self.sku.on_save(self.sid,before,after)
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(other['id'],)).fetchone()[0],'matched')


if __name__=='__main__':unittest.main()
