"""独立开关范围、单项手动任务、元数据写回、断点和真实进度。"""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import time
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from asset_catalog import AssetCatalog
from asset_intelligence import AssetIntelligence
from asset_analysis import AssetAnalysis
from analysis_workflow import AnalysisWorkflow
from vision_scheduler import VisionScheduler
from test_asset_intelligence import result
import test_ocr_metadata as fixture


class AnalysisWorkflowTests(unittest.TestCase):
    def setUp(self):
        fixture.OcrMetadataTests.setUp(self)
        self.providers.save({'name':'视觉','category':'multimodal','protocol':'chat','url':'http://127.0.0.1:1','model':'vision-fixture','select':True,'concurrency':2})
        self.scheduler=VisionScheduler();self.analysis=AssetAnalysis(self.archive,self.providers,self.scheduler,autostart=False)
        self.catalog=AssetCatalog(self.store.data);self.intelligence=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=False)
        self.archive_folder=self.base/'archive';self.archive_folder.mkdir()
        for n,color in enumerate(('red','blue')):Image.new('RGB',(180,240),color).save(self.archive_folder/f'{n}.jpg')
        self.root=self.catalog.add_root(str(self.archive_folder))['root_id']
        self.monitor={'enabled':True,'session_id':self.sid}
        self.workflow=AnalysisWorkflow(self.analysis,self.intelligence,self.sku,lambda:self.monitor)
        self.catalog.on_media_changes=self.workflow.indexed
        self.sku.analysis=self.analysis;self.sku.intelligence=self.intelligence;self.sku.workflow=self.workflow
        self.models=ExitStack();self.addCleanup(self.models.close)
        self.vision=self.models.enter_context(patch('analysis_workflow.vision',return_value=json.dumps(result())))
        self.ocr=self.models.enter_context(patch('analysis_workflow.recognize',return_value={'text':'OCR-123','codes':['OCR-123'],'model':'ocr-fixture'}))

    def tearDown(self):
        self.workflow.close();self.analysis.close();self.intelligence.close();self.catalog.close()
        fixture.OcrMetadataTests.tearDown(self)

    def add(self,name='new.JPG',color='green'):
        path=self.photos/name;Image.new('RGB',(160,120),color).save(path)
        record=fixture.media.capture_record(self.photos,path,None,{'DateTimeOriginal':'2026:10:30 12:00:01'})
        self.store.append_live(self.sid,[record]);return record

    def drain(self,limit=40):
        for _ in range(limit):
            self.workflow.discover_tick()
            worked=self.workflow.tick()
            if not worked and all(r['status']!='running' for r in self.workflow.progress.values()):break
        self.workflow.refresh_progress()

    def test_local_switch_only_analyzes_new_monitor_images_and_survives_restart(self):
        self.workflow.configure({'live_enabled':True,'session_id':self.sid})
        self.drain();self.vision.assert_not_called()
        record=self.add();self.workflow.changed(self.sid,[record]);self.drain()
        self.assertEqual(self.vision.call_count,1);self.ocr.assert_not_called()
        from capture_parsing import capture_status
        self.assertEqual(capture_status(self.store,self.sku,self.archive,self.analysis,self.sid,record['id'])['multimodal']['status'],'completed')
        self.workflow.changed(self.sid,[record]);self.drain();self.assertEqual(self.vision.call_count,1)
        self.workflow.close();self.workflow=AnalysisWorkflow(self.analysis,self.intelligence,self.sku,lambda:self.monitor)
        self.assertTrue(self.workflow.config['live_enabled']);self.assertFalse(self.workflow.config['global_enabled'])
        self.workflow.changed(self.sid,[record]);self.drain();self.assertEqual(self.vision.call_count,1)

    def test_global_switch_backfills_monitor_and_archive_and_local_stays_independent(self):
        self.workflow.configure({'global_enabled':True,'session_id':self.sid});self.drain()
        self.assertEqual(self.vision.call_count,6);self.ocr.assert_not_called()
        with self.catalog.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM intelligence_results').fetchone()[0],2)
        self.workflow.configure({'live_enabled':True,'global_enabled':False})
        record=self.add();self.workflow.changed(self.sid,[record]);self.drain();self.assertEqual(self.vision.call_count,7)
        self.workflow.configure({'live_enabled':False});other=self.add('disabled.JPG','purple');self.workflow.changed(self.sid,[other]);self.drain()
        self.assertEqual(self.vision.call_count,7)

    def test_manual_ocr_only_records_progress_duration_and_does_not_enable_auto(self):
        first=self.workflow.execute('ocr',self.sid);self.assertEqual(first['runs']['ocr']['percent'],0)
        self.drain();self.assertEqual(self.ocr.call_count,6);self.vision.assert_not_called()
        run=self.workflow.state()['runs']['ocr'];self.assertEqual((run['status'],run['total'],run['done'],run['percent']),('completed',6,6,100))
        self.assertGreaterEqual(run['elapsed_seconds'],0)
        self.assertFalse(self.workflow.config['global_enabled']);self.assertFalse(self.workflow.config['live_enabled'])
        with self.sku.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM ocr_documents WHERE id LIKE 'workflow:%'").fetchone()[0],4)
        with self.catalog.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM intelligence_ocr').fetchone()[0],2)
        self.workflow.execute('ocr',self.sid);self.drain();self.assertEqual(self.ocr.call_count,6)
        self.workflow.close();self.workflow=AnalysisWorkflow(self.analysis,self.intelligence,self.sku,lambda:self.monitor);self.workflow.refresh_progress()
        self.assertEqual(self.workflow.progress['ocr']['status'],'completed')

    def test_manual_vision_only_saves_database_fingerprints_and_preserves_all_source_bytes(self):
        code='KEEP-100';preview=self.archive.preview(self.sid,[self.ids[0]],code,'模特图',self.pid,overwrite=True);self.archive.apply(preview['preview_id'])
        while self.archive.process_one():pass
        sources=[*self.photos.glob('*'),*self.archive_folder.glob('*')]
        before={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in sources if path.is_file()}
        with patch.object(self.archive,'ensure',side_effect=AssertionError('批量视觉不注册源文件')),patch('analysis_metadata.enqueue',side_effect=AssertionError('批量视觉不写回元数据')):
            self.workflow.execute('vision',self.sid);self.drain()
        self.assertEqual(self.vision.call_count,6);self.ocr.assert_not_called()
        with self.archive.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM operations WHERE source='multidimensional'").fetchone()[0],0)
        while self.archive.process_one():pass
        meta=self.archive.writer.read_asset(self.photos/'0.JPG')
        self.assertEqual(meta['XMP-fk:SKU'],code);self.assertNotIn('XMP-fk:Multidimensional',meta)
        after={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in sources if path.is_file()}
        self.assertEqual(before,after)
        with self.catalog.connect() as c:
            fingerprints=c.execute('SELECT * FROM intelligence_file_fingerprints').fetchall()
            self.assertEqual(len(fingerprints),2);self.assertTrue(all(len(r['content_sha'])==64 and r['stage']=='vision' for r in fingerprints))
        self.workflow.execute('vision',self.sid);self.drain();self.assertEqual(self.vision.call_count,6)

    def test_ocr_consumes_first_batch_before_discovery_finishes_without_registering_assets(self):
        self.workflow.execute('ocr',self.sid);self.workflow.discover_tick()
        self.assertFalse(self.workflow.state()['runs']['ocr']['discovered'])
        phases=[]
        def recognize(*args):
            phases.extend(self.workflow.state()['active'])
            return {'text':'DIRECT-OCR','codes':['DIRECT-OCR']}
        self.ocr.side_effect=recognize
        with patch.object(self.archive,'ensure',side_effect=AssertionError('OCR 不应注册资产')),patch('photo_metadata.ensure_file',side_effect=AssertionError('OCR 不应读取资产元数据')),patch('metadata_writer.sha',side_effect=AssertionError('OCR 不应反复读取完整 NAS 文件哈希')):
            self.assertTrue(self.workflow.tick())
            self.workflow.refresh_progress()
            run=self.workflow.state()['runs']['ocr']
            self.assertEqual(run['done'],1);self.assertFalse(run['discovered'])
            self.drain()
        self.assertEqual(self.ocr.call_count,6)
        self.assertTrue(phases);self.assertTrue(all(p['phase']=='inference' and p['kind']=='ocr' for p in phases))
        self.assertEqual(self.workflow.state()['active'],[])
        with self.catalog.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM intelligence_ocr').fetchone()[0],2)

    def test_direct_ocr_rejects_source_change_during_inference(self):
        self.workflow.execute('ocr',self.sid);self.workflow.discover_tick()
        def changed(*args):
            with self.analysis.connect() as c:job=c.execute("SELECT * FROM workflow_jobs WHERE status='running'").fetchone()
            record=self.store.records(job['session'],ids=[job['source_id']])[0]
            path=self.photos/(record.get('jpg') or record['raw'])
            with path.open('ab') as stream:stream.write(b'changed-during-ocr')
            return {'text':'STALE'}
        self.ocr.side_effect=changed
        self.assertTrue(self.workflow.tick())
        with self.analysis.connect() as c:
            failed=c.execute("SELECT reason FROM workflow_jobs WHERE status='failed'").fetchone()
            self.assertIn('源图片发生变化',failed['reason'])
        with self.sku.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM ocr_documents WHERE id LIKE 'workflow:%'").fetchone()[0],0)
        self.assertEqual(self.workflow.state()['active'],[])

    def test_one_shot_has_bounded_snapshot_and_new_photos_wait_for_next_run(self):
        self.workflow.execute('vision',self.sid);self.add()
        self.drain();self.assertEqual(self.vision.call_count,6)
        self.workflow.execute('vision',self.sid);self.drain();self.assertEqual(self.vision.call_count,7)

    def test_restart_exposes_saved_progress_before_background_backfill_or_discovery(self):
        self.workflow.execute('ocr',self.sid);self.workflow.discover_tick();self.workflow.tick();self.workflow.refresh_progress()
        saved=dict(self.workflow.state()['runs']['ocr']);self.workflow.close()
        with patch('catalog_ocr.backfill',side_effect=AssertionError('首帧不应等待历史补录')):
            self.workflow=AnalysisWorkflow(self.analysis,self.intelligence,self.sku,lambda:self.monitor)
            restored=self.workflow.state()['runs']['ocr']
        self.assertEqual((restored['id'],restored['done'],restored['total']),(saved['id'],1,saved['total']))

    def test_manual_discovery_backpressure_keeps_workers_fed_without_rewriting_full_queue(self):
        run=self.workflow.execute('ocr',self.sid)['runs']['ocr']['id']
        self.workflow.enqueue('ocr','catalog',[{'id':'pending-'+str(i),'fingerprint':'version'} for i in range(256)],run=run)
        self.workflow.discover_tick()
        with self.analysis.connect() as c:
            row=c.execute('SELECT * FROM workflow_runs WHERE id=?',(run,)).fetchone()
            self.assertEqual((row['total'],row['capture_cursor'],row['cursor']),(256,-1,0))
            c.execute("UPDATE workflow_jobs SET status='completed'")
        self.workflow.discover_tick()
        with self.analysis.connect() as c:
            row=c.execute('SELECT * FROM workflow_runs WHERE id=?',(run,)).fetchone()
            self.assertEqual((row['total'],row['done']),(260,256))

    def test_paused_manual_run_does_not_continue_bulk_discovery(self):
        self.workflow.execute('ocr',self.sid);self.workflow.configure({'ocr_paused':True});self.workflow.discover_tick()
        with self.analysis.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM workflow_jobs').fetchone()[0],0)

    def test_stop_is_independent_persistent_and_resumes_the_original_snapshot(self):
        original=self.workflow.execute('ocr',self.sid)['runs']['ocr']
        self.workflow.discover_tick();self.workflow.tick()
        self.workflow.execute('vision',self.sid)
        state=self.workflow.stop('ocr')
        self.assertEqual(state['runs']['ocr']['status'],'stopped')
        self.assertEqual(state['runs']['vision']['status'],'running')
        self.assertTrue(state['config']['ocr_paused']);self.assertFalse(state['config']['vision_paused'])
        self.workflow.stop('vision');self.workflow.close()
        self.workflow=AnalysisWorkflow(self.analysis,self.intelligence,self.sku,lambda:self.monitor)
        self.assertFalse(self.workflow.tick())
        self.add('after-stop.JPG')
        resumed=self.workflow.execute('ocr',self.sid)['runs']['ocr']
        self.assertEqual(resumed['id'],original['id'])
        self.assertEqual(resumed['capture_limit'],original['capture_limit'])
        self.drain();self.assertEqual(self.ocr.call_count,6);self.vision.assert_not_called()
        self.assertEqual(self.workflow.state()['runs']['ocr']['status'],'completed')

    def test_stop_during_inference_finishes_current_result_and_blocks_new_dispatch(self):
        run=self.workflow.execute('ocr',self.sid)['runs']['ocr']['id'];self.workflow.discover_tick()
        entered=threading.Event();release=threading.Event()
        def model(*args):
            entered.set();release.wait(5);return {'text':'STOP-SAFE','codes':[]}
        self.ocr.side_effect=model
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.workflow.tick)
            try:
                self.assertTrue(entered.wait(2));self.workflow.stop('ocr')
                with self.assertRaisesRegex(ValueError,'收尾'):self.workflow.execute('ocr',self.sid)
                self.assertTrue(self.workflow.config['ocr_paused']);self.assertFalse(self.workflow.tick())
            finally:release.set()
            self.assertTrue(future.result(3))
        with self.analysis.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM workflow_jobs WHERE status='completed'").fetchone()[0],1)
        self.assertEqual(self.ocr.call_count,1)
        self.workflow.refresh_progress()
        self.assertEqual(self.workflow.state()['runs']['ocr']['done'],1)
        resumed=self.workflow.execute('ocr',self.sid)['runs']['ocr']
        self.assertEqual((resumed['id'],resumed['done']),(run,1))
        self.drain();self.assertEqual(self.ocr.call_count,6)

    def test_stop_while_waiting_for_model_slot_does_not_send_a_request(self):
        self.workflow.execute('ocr',self.sid);self.workflow.discover_tick()
        with ThreadPoolExecutor(1) as pool:
            with self.scheduler.slot(stage='vision',priority=True):
                future=pool.submit(self.workflow.tick);deadline=time.monotonic()+2
                while not self.scheduler.state()['waiting_ocr'] and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(self.scheduler.state()['waiting_ocr'])
                self.workflow.stop('ocr');self.assertTrue(future.result(2))
        self.ocr.assert_not_called()

    def test_stop_does_not_require_an_available_model_and_rejects_stale_progress(self):
        from contextlib import contextmanager
        self.workflow.execute('ocr',self.sid)
        ready=threading.Event();release=threading.Event();original=self.analysis.connect
        @contextmanager
        def delayed_snapshot():
            with original() as c:yield c
            if threading.current_thread().name=='old-workflow-snapshot':ready.set();release.wait(3)
        with patch.object(self.analysis,'connect',side_effect=delayed_snapshot):
            thread=threading.Thread(target=self.workflow.refresh_progress,name='old-workflow-snapshot');thread.start()
            try:
                self.assertTrue(ready.wait(2))
                with patch.object(self.workflow,'_provider',side_effect=AssertionError('停止不能依赖模型配置')):self.workflow.stop('ocr')
            finally:release.set();thread.join(3)
        self.assertEqual(self.workflow.state()['runs']['ocr']['status'],'stopped')
        self.assertTrue(self.workflow.config['ocr_paused'])

    def test_continue_after_model_change_creates_current_configuration_jobs(self):
        original=self.workflow.execute('ocr',self.sid)['runs']['ocr']['id'];self.workflow.discover_tick();self.workflow.stop('ocr')
        self.providers.save({'name':'新 OCR','category':'ocr','protocol':'chat','url':'http://127.0.0.1:1','model':'new-ocr-fixture','select':True})
        resumed=self.workflow.execute('ocr',self.sid)['runs']['ocr']
        self.assertNotEqual(resumed['id'],original)
        self.drain();self.assertEqual(self.ocr.call_count,6)
        self.assertEqual(self.workflow.state()['runs']['ocr']['failed'],0)

    def test_parsing_page_continue_resumes_a_stopped_manual_workflow(self):
        import urllib.request
        import server
        import test_sku_input_confirm as input_fixture
        run=self.workflow.execute('ocr',self.sid)['runs']['ocr']['id'];self.workflow.discover_tick();self.workflow.stop('ocr')
        with input_fixture.InputConfirmTests.http(self) as url,ExitStack() as stack:
            for name,value in dict(WORKFLOW=self.workflow,ANALYSIS=self.analysis,INTELLIGENCE=self.intelligence,CATALOG=self.catalog).items():stack.enter_context(patch.object(server,name,value))
            request=urllib.request.Request(url+'/api/parsing/configure',data=json.dumps({'channel':'ocr','paused':False}).encode(),headers={'Content-Type':'application/json','X-Framekeep-Token':server.TOKEN})
            with urllib.request.urlopen(request,timeout=5) as response:state=json.load(response)
            self.assertEqual(state['workflow']['runs']['ocr']['id'],run)
            self.assertEqual(state['workflow']['runs']['ocr']['status'],'running')
        self.drain();self.assertEqual(self.ocr.call_count,6)

    def test_catalog_busy_keeps_model_result_and_retries_without_another_request(self):
        from contextlib import contextmanager
        self.workflow.execute('ocr',self.sid)
        with self.analysis.connect() as c:run=c.execute('SELECT id FROM workflow_runs').fetchone()[0]
        with self.catalog.connect() as c:row=dict(c.execute('SELECT * FROM items LIMIT 1').fetchone())
        self.workflow.enqueue('ocr','catalog',[row],run=run)
        payload=self.catalog.preview(row['id'],1600,read=True);original=self.catalog.connect
        @contextmanager
        def quick_timeout():
            with original() as c:c.execute('PRAGMA busy_timeout=20');yield c
        with original() as writer:
            writer.execute('BEGIN IMMEDIATE')
            with patch.object(self.catalog,'connect',side_effect=quick_timeout),patch.object(self.catalog,'preview',return_value=payload):self.assertTrue(self.workflow.tick())
        with self.analysis.connect() as c:
            job=c.execute('SELECT * FROM workflow_jobs').fetchone()
            self.assertEqual(job['status'],'queued');self.assertTrue(job['result'])
        self.assertTrue(self.workflow.tick());self.assertEqual(self.ocr.call_count,1)
        with self.analysis.connect() as c:self.assertEqual(c.execute('SELECT status FROM workflow_jobs').fetchone()[0],'completed')

    def test_failures_finish_with_actual_failed_count_and_duplicate_click_is_rejected(self):
        self.workflow.execute('vision',self.sid)
        with self.assertRaisesRegex(ValueError,'正在执行'):self.workflow.execute('vision',self.sid)
        self.vision.side_effect=ValueError('测试模型失败');self.drain()
        run=self.workflow.progress['vision'];self.assertEqual((run['total'],run['done'],run['failed'],run['percent']),(6,6,6,100))
        self.vision.side_effect=None;self.workflow.execute('vision',self.sid);self.drain()
        self.assertEqual(self.workflow.progress['vision']['failed'],0)

    def test_pause_and_clear_preserve_results_and_manual_duration(self):
        self.workflow.execute('vision',self.sid);self.workflow.configure({'vision_paused':True})
        self.workflow.discover_tick();self.assertFalse(self.workflow.tick());self.vision.assert_not_called()
        self.workflow.configure({'vision_paused':False});self.drain()
        self.workflow.clear_completed_tasks();jobs,counts=self.workflow.tasks('vision')
        self.assertEqual(jobs,[]);self.assertEqual(counts,{})
        with self.analysis.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM workflow_jobs WHERE status='completed'").fetchone()[0],6)
        self.assertEqual(self.workflow.progress['vision']['percent'],100)

    def test_disabled_or_deleted_model_cannot_break_photo_ingestion(self):
        self.workflow.configure({'live_enabled':True,'session_id':self.sid})
        record=self.add()
        with patch.object(self.providers,'selected',return_value=None),patch.object(self.providers,'purpose',return_value=None):
            self.workflow.changed(self.sid,[record])
            self.workflow.configure({'live_enabled':False})
        with self.analysis.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM workflow_inbox').fetchone()[0],1)

    def test_global_archive_change_is_not_lost_after_preview_callback_rebinding(self):
        self.workflow.configure({'global_enabled':True,'session_id':self.sid});self.drain()
        self.assertEqual(self.vision.call_count,6)
        self.catalog.on_previews=lambda *_:None
        Image.new('RGB',(180,240),'purple').save(self.archive_folder/'0.jpg')
        self.catalog.scan(self.root);self.drain()
        self.assertEqual(self.vision.call_count,7)

    def test_switch_off_while_waiting_for_model_slot_keeps_checkpoint_without_request(self):
        self.workflow.configure({'live_enabled':True,'session_id':self.sid})
        record=self.add();self.workflow.changed(self.sid,[record]);self.workflow.discover_tick()
        provider=self.workflow._provider('vision')
        with self.providers.inference_slot(provider),self.providers.inference_slot(provider),ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.workflow.tick)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                with self.analysis.connect() as c:claimed=c.execute("SELECT 1 FROM workflow_jobs WHERE status='running'").fetchone()
                if claimed:break
                time.sleep(.02)
            self.assertTrue(claimed);self.workflow.configure({'live_enabled':False})
            self.assertTrue(future.result(3))
        self.vision.assert_not_called()
        with self.analysis.connect() as c:self.assertEqual(c.execute('SELECT status FROM workflow_jobs').fetchone()[0],'queued')

    def test_restored_global_mode_grows_workers_without_double_start(self):
        self.workflow.configure({'global_enabled':True,'session_id':self.sid})
        with patch.object(self.providers,'concurrency',return_value=4):
            self.workflow.start();self.workflow.start()
            deadline=time.monotonic()+3
            while (len(self.workflow.workers)<8 or any(worker.ident is None for worker in self.workflow.workers)) and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(len(self.workflow.workers),8)
            self.assertTrue(all(worker.ident is not None for worker in self.workflow.workers))

    def test_next_model_inputs_are_dispatched_while_previous_results_are_being_saved(self):
        import catalog_ocr
        for index,color in enumerate(('green','purple','yellow','orange')):
            Image.new('RGB',(180,240),color).save(self.archive_folder/f'prefetch-{index}.jpg')
        self.catalog.scan(self.root);self.monitor['enabled']=False
        release=threading.Event();all_requested=threading.Event();original=catalog_ocr.save_ocr;calls=[]
        def recognize(*args):
            calls.append(1)
            if len(calls)==6:all_requested.set()
            return {'text':'PREFETCH','codes':[]}
        def slow_save(*args,**kwargs):
            release.wait(10);return original(*args,**kwargs)
        self.ocr.side_effect=recognize
        with patch('catalog_ocr.save_ocr',side_effect=slow_save):
            self.workflow.execute('ocr');self.workflow.start()
            try:
                self.assertTrue(all_requested.wait(3),'保存前一批结果时应已发送下一批模型请求')
                self.assertGreaterEqual(len(self.workflow.workers),6)
                with self.analysis.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM workflow_jobs WHERE status='completed'").fetchone()[0],0)
            finally:release.set()
            deadline=time.monotonic()+5
            while self.workflow.state()['runs']['ocr']['status']=='running' and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(self.workflow.state()['runs']['ocr']['status'],'completed')

    def test_same_content_in_two_sources_shares_one_inflight_model_request(self):
        self.workflow.configure({'live_enabled':True,'session_id':self.sid})
        records=[self.add('copy-1.JPG'),self.add('copy-2.JPG')]
        self.workflow.changed(self.sid,records);self.workflow.discover_tick()
        entered=threading.Event();release=threading.Event()
        def model(*args):entered.set();release.wait(5);return json.dumps(result())
        self.vision.side_effect=model
        with ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(self.workflow.tick) for _ in records]
            try:self.assertTrue(entered.wait(3))
            finally:release.set()
            self.assertTrue(all(future.result(5) for future in futures))
        self.assertEqual(self.vision.call_count,1)

    def test_preview_backlog_does_not_starve_priority_live_analysis(self):
        self.scheduler.order_previews(True)
        entered=threading.Event()
        def model():
            with self.scheduler.slot(stage='vision',priority=True):entered.set()
        with ThreadPoolExecutor(1) as pool:
            with self.scheduler.preview_slot():
                future=pool.submit(model);self.assertFalse(entered.wait(.05))
            self.assertTrue(entered.wait(1));future.result(1)

    def test_completed_results_legacy_vectors_and_duration_merge_roundtrip_without_machine_keys(self):
        from sku_metadata_backup import export_metadata,import_metadata,_canonical
        self.workflow.execute('vision',self.sid);self.drain()
        # 新视觉描述不再写图片向量；仍需兼容备份中保留的历史向量。
        from retrieval_store import store_vector
        from visual_features import APPEARANCE
        with self.catalog.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM intelligence_vectors').fetchone()[0],0)
            row=c.execute('SELECT item,fingerprint FROM intelligence_results LIMIT 1').fetchone()
            store_vector(c,row['item'],0,APPEARANCE,row['fingerprint'],[1.]+[0.]*191)
        output=export_metadata(self.sku);payload=output['backup']['payload']
        self.assertEqual(output['backup']['version'],2)
        self.assertEqual(len(payload['databases']['analysis']['workflow_jobs']),6)
        self.assertEqual(len(payload['databases']['retrieval']['intelligence_results']),2)
        self.assertTrue(payload['databases']['retrieval']['intelligence_vectors'][0]['vector'].startswith('base64:'))
        with self.catalog.connect() as c:
            c.execute("INSERT INTO intelligence_jobs VALUES ('unparsed-history','missing-item','fp','config','queued',0,'',0,0,0)")
        filtered=export_metadata(self.sku)['backup']['payload']
        self.assertFalse(any(row['id']=='unparsed-history' for row in filtered['databases']['retrieval']['intelligence_jobs']))
        with tempfile.TemporaryDirectory() as folder:
            store=fixture.Store(fixture.Path(folder)/'data',fixture.ROOT);providers=fixture.Providers(store.data)
            archive=fixture.AssetArchive(store,autostart=False);sku=fixture.SkuService(store,providers,archive=archive,autostart=False)
            try:
                content=_canonical(output['backup']);report=import_metadata(sku,content)
                self.assertEqual(report['mode'],'merge');self.assertGreater(report['records'],0)
                self.assertTrue(import_metadata(sku,content)['already_imported'])
                with sku.analysis.connect() as c:
                    self.assertEqual(c.execute("SELECT count(*) FROM workflow_jobs WHERE status='completed'").fetchone()[0],6)
                    self.assertEqual(c.execute('SELECT finished-started FROM workflow_runs').fetchone()[0],self.workflow.progress['vision']['elapsed_seconds'])
                with sku.intelligence.catalog.connect() as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM intelligence_results').fetchone()[0],2)
                    self.assertIsInstance(c.execute('SELECT vector FROM intelligence_vectors LIMIT 1').fetchone()[0],bytes)
                self.assertEqual(providers.public()['providers'],[])
                providers.save({'name':'另一设备的同一视觉服务','category':'multimodal','protocol':'chat','url':'http://127.0.0.1:1','model':'vision-fixture','select':True,'concurrency':2})
                other=AnalysisWorkflow(sku.analysis,sku.intelligence,sku)
                try:
                    other.execute('vision',self.sid)
                    for _ in range(20):other.discover_tick();other.tick()
                    self.assertEqual(other.progress['vision']['status'],'completed')
                    self.assertEqual(self.vision.call_count,6,'相同内容和模型不应因本机卡片 ID 不同重复付费解析')
                finally:other.close()
            finally:sku.close();archive.close()

    def test_browser_controls_scope_manual_percentage_animation_and_latest_duration(self):
        from playwright.sync_api import sync_playwright,expect
        import server
        import test_sku_input_confirm as input_fixture
        calls=[];release=threading.Event()
        def recognize(*args):
            calls.append(1)
            if len(calls)>1:release.wait(15)
            return {'text':'OCR-123','codes':['OCR-123'],'model':'ocr-fixture'}
        self.ocr.side_effect=recognize
        self.workflow.start()
        with input_fixture.InputConfirmTests.http(self) as url,ExitStack() as stack,sync_playwright() as pw:
            for name,value in dict(WORKFLOW=self.workflow,ANALYSIS=self.analysis,INTELLIGENCE=self.intelligence,CATALOG=self.catalog).items():stack.enter_context(patch.object(server,name,value))
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                page.locator('#app-settings-open').click();page.locator('[data-settings-section=models]').click()
                expect(page.locator('#full-auto-archive-title')).to_have_text('多维度解析')
                expect(page.locator('#full-auto-archive')).to_be_visible()
                page.wait_for_function("()=>!document.getElementById('app-settings-page').getAnimations({subtree:true}).some(a=>a.playState==='running'&&a.effect.getTiming().iterations!==Infinity)")
                for toggle,button in [('full-auto-ocr','workflow-ocr-execute'),('full-auto-archive','workflow-vision-execute')]:
                    a=page.locator('#'+toggle).bounding_box();b=page.locator('#'+button).bounding_box()
                    self.assertGreater(b['x'],a['x']);self.assertAlmostEqual(a['y']+a['height']/2,b['y']+b['height']/2,delta=2)
                page.locator('#workflow-ocr-execute').click()
                expect(page.locator('#workflow-ocr-execute')).to_have_text('执行中 16%',timeout=10000)
                self.assertTrue(page.locator('#workflow-ocr-execute').evaluate('e=>e.classList.contains("is-running")'))
                self.assertIn('linear-gradient',page.locator('#workflow-ocr-execute').evaluate('e=>getComputedStyle(e).backgroundImage'))
                expect(page.locator('#workflow-run-progress')).to_contain_text('OCR')
                self.assertEqual(page.locator('#asset-analysis-open, #analysis-dialog').count(),0)
                release.set()
                page.locator('#app-settings-open').click()
                expect(page.locator('#workflow-ocr-last')).to_contain_text('最近一次 OCR 完成',timeout=10000)
                expect(page.locator('#workflow-ocr-execute')).to_have_text('手动执行')
                self.vision.assert_not_called()
                page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading');page.locator('#app-settings-open').click();page.locator('[data-settings-section=models]').click()
                expect(page.locator('#workflow-ocr-last')).to_contain_text('耗时')
                out=fixture.ROOT/'build/multidimensional-workflow';out.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(out/'settings-completed.png'))
                page.locator('#selection-tab').click()
                a=page.locator('#shooting-full-auto').bounding_box();b=page.locator('#shooting-multidimensional').bounding_box()
                self.assertAlmostEqual(a['y'],b['y'],delta=2);self.assertGreater(b['x'],a['x'])
                page.screenshot(path=str(out/'selection-switches.png'))
                page.locator('#app-settings-open').click();page.locator('[data-settings-section=database]').click()
                expect(page.locator('#sku-metadata-import-mode')).to_have_value('merge')
                page.locator('#sku-metadata-import-mode').select_option('replace')
                expect(page.locator('#sku-metadata-import-help')).to_contain_text('替换业务资料')
                page.locator('[data-settings-section=models]').click()
                for theme in ('dark','light','pure-white','hardcore'):
                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                    page.set_viewport_size({'width':375,'height':900})
                    page.wait_for_function("()=>!document.getElementById('app-settings-page').getAnimations({subtree:true}).some(a=>a.playState==='running'&&a.effect.getTiming().iterations!==Infinity)")
                    card=page.locator('#full-auto-archive-card')
                    self.assertFalse(card.evaluate('e=>e.scrollWidth>e.clientWidth'))
                    toggle=page.locator('#full-auto-archive').bounding_box();button=page.locator('#workflow-vision-execute').bounding_box()
                    self.assertGreater(button['x'],toggle['x']);self.assertAlmostEqual(toggle['y']+toggle['height']/2,button['y']+button['height']/2,delta=2)
                page.screenshot(path=str(out/'settings-narrow.png'))
                page.evaluate("""()=>document.dispatchEvent(new CustomEvent('framekeep:workflow-progress',{detail:{config:{},runs:{ocr:{status:'running',done:58,total:9289,discovered:0,percent:0,elapsed_seconds:867}},active:[{kind:'ocr',name:'nas-photo.jpg',phase:'image',elapsed_seconds:90}]}}))""")
                note=page.locator('#workflow-ocr-last')
                expect(note).to_contain_text('已处理 58/9289（已入队）')
                expect(note).to_contain_text('边识别边补充队列')
                expect(note).to_contain_text('准备模型输入 1 分 30 秒')
                expect(note).not_to_contain_text('正在发现图片')
                expect(note).to_have_attribute('title','nas-photo.jpg')
                page.screenshot(path=str(out/'ocr-streaming-progress.png'))
                self.assertFalse(errors,errors)
            finally:release.set();browser.close()

    def test_browser_can_stop_and_continue_ocr_and_vision_independently(self):
        from playwright.sync_api import sync_playwright,expect
        import server
        import test_sku_input_confirm as input_fixture
        release=threading.Event();entered=threading.Event();calls=[];capacity=4
        def model(kind):
            calls.append(kind)
            if len(calls)>=capacity:entered.set()
            release.wait(20)
            return {'text':'STOP-BROWSER','codes':[]} if kind=='ocr' else json.dumps(result())
        self.ocr.side_effect=lambda *args:model('ocr')
        self.vision.side_effect=lambda *args:model('vision')
        self.workflow.start()
        with input_fixture.InputConfirmTests.http(self) as url,ExitStack() as stack,sync_playwright() as pw:
            for name,value in dict(WORKFLOW=self.workflow,ANALYSIS=self.analysis,INTELLIGENCE=self.intelligence,CATALOG=self.catalog).items():stack.enter_context(patch.object(server,name,value))
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                page.locator('#app-settings-open').click();page.locator('[data-settings-section=models]').click()
                for kind,capacity in [('ocr',4),('vision',2)]:
                    calls.clear();entered.clear();release.clear()
                    start=page.locator('#workflow-'+kind+'-execute');stop=page.locator('#workflow-'+kind+'-stop');note=page.locator('#workflow-'+kind+'-last')
                    expect(stop).to_be_disabled();start.click();self.assertTrue(entered.wait(5))
                    expect(stop).to_be_enabled();stop.click()
                    expect(note).to_contain_text('正在停止');expect(start).to_be_disabled();expect(stop).to_be_disabled()
                    before=len(calls);page.wait_for_timeout(200);self.assertEqual(len(calls),before)
                    release.set();expect(start).to_have_text('继续执行',timeout=10000)
                    expect(note).to_contain_text('已停止')
                    out=fixture.ROOT/'build/ocr-direct-input';out.mkdir(exist_ok=True)
                    page.screenshot(path=str(out/('stop-'+kind+'.png')))
                    start.click();expect(start).to_have_text('手动执行',timeout=10000);expect(stop).to_be_disabled()
                    self.assertEqual(len(calls),6)
                page.set_viewport_size({'width':375,'height':900})
                for kind,card in [('ocr','full-auto-ocr-card'),('vision','full-auto-archive-card')]:
                    self.assertFalse(page.locator('#'+card).evaluate('e=>e.scrollWidth>e.clientWidth'))
                    expect(page.locator('#workflow-'+kind+'-stop')).to_be_visible()
                self.assertFalse(errors,errors)
            finally:release.set();browser.close()


if __name__=='__main__':unittest.main()
