"""归档 OCR→多维阶段、断点恢复和真实并发资源边界。"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from asset_intelligence import AssetIntelligence
import test_asset_intelligence as fixture
from test_asset_intelligence import result
from vision_scheduler import VisionScheduler


def reading(*_):return {'text':'UNIQUEOCR123','codes':[],'model':'ocr-fixture'}


class ArchivePipelineTests(unittest.TestCase):
    def setUp(self):
        fixture.IntelligenceTests.setUp(self)
        self.providers.save({'name':'OCR','category':'ocr','protocol':'chat','url':'http://127.0.0.1:1',
                             'model':'ocr-fixture','select':True,'concurrency':4})
        self.sku=SimpleNamespace(full_auto={'enabled':True},scheduler=VisionScheduler(),
            sku_rules=lambda:{'rules':[{'kind':'prefix','value':'LC'}],'length_filter':{'min_exclusive':None,'max_exclusive':None}})
        self.service.close();self.service=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=False)

    tearDown=fixture.IntelligenceTests.tearDown

    def test_archive_ocr_runs_without_live_session_switch_and_survives_restart(self):
        self.sku.full_auto={'enabled':False}
        self.service.configure({'ocr_enabled':True})
        self.service.close();self.service=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=False)
        self.assertTrue(self.service.pipeline_state()['ocr_enabled'])
        events=[]
        with patch('asset_intelligence.recognize_ocr',side_effect=lambda *args:events.append('ocr') or reading()),patch('asset_intelligence.vision',side_effect=lambda *args:events.append('vision') or json.dumps(result())):
            self.drain()
        self.assertEqual(events,['ocr']*3+['vision']*3)
        self.assertEqual(len(self.service.search(text='UNIQUEOCR123')['rows']),3)
        self.assertFalse(self.sku.full_auto['enabled'])

    def drain(self,limit=30):
        for _ in range(limit):
            self.service.discover_tick()
            if not self.service.tick():break

    def test_ocr_stage_precedes_all_vision_and_restart_reuses_ocr_checkpoint(self):
        events=[];self.service.discover_tick()
        with patch('asset_intelligence.recognize_ocr',side_effect=lambda *args:events.append('ocr') or reading()),patch('asset_intelligence.vision',side_effect=lambda *args:events.append('vision') or json.dumps(result())):
            self.assertTrue(self.service.tick())
            with self.catalog.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM intelligence_ocr').fetchone()[0],1)
            self.service.close();self.service=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=False)
            self.assertTrue(self.service.seed_done)
            self.drain()
        self.assertEqual(events,['ocr']*3+['vision']*3)
        state=self.service.pipeline_state();self.assertEqual((state['ocr_completed'],state['completed']),(3,3))
        self.assertEqual(state['phase'],'completed')
        self.assertEqual(len(self.service.search(text='UNIQUEOCR123')['rows']),3)

    def test_manual_finishes_snapshot_and_next_start_only_processes_new_item(self):
        self.service.configure({'enabled':False});self.service.control('start')
        with patch('asset_intelligence.recognize_ocr',side_effect=reading) as ocr,patch('asset_intelligence.vision',return_value=json.dumps(result())) as vision:
            self.drain();self.assertFalse(self.service.pipeline_state()['running'])
            self.assertEqual((ocr.call_count,vision.call_count),(3,3))
            Image.new('RGB',(120,160),'red').save(self.folder/'new.jpg');self.catalog.scan(self.rows[0]['root'])
            self.assertFalse(self.service.discover_tick());self.assertFalse(self.service.tick())
            self.service.control('start');self.drain()
            self.assertEqual((ocr.call_count,vision.call_count),(4,4))
            self.assertFalse(self.service.pipeline_state()['enabled'])

    def test_pause_keeps_finished_ocr_and_does_not_start_next_model(self):
        self.service.discover_tick()
        def pause(*_):self.service.control('pause');return reading()
        with patch('asset_intelligence.recognize_ocr',side_effect=pause),patch('asset_intelligence.vision') as vision:
            self.service.tick();self.assertFalse(self.service.tick());vision.assert_not_called()
        self.service.close();self.service=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=False)
        self.assertEqual(self.service.pipeline_state()['phase'],'paused')
        self.service.control('resume')
        with patch('asset_intelligence.recognize_ocr',side_effect=reading) as ocr,patch('asset_intelligence.vision',return_value=json.dumps(result())) as vision:
            self.drain();self.assertEqual((ocr.call_count,vision.call_count),(2,3))

    def test_failed_ocr_is_terminal_and_retry_preserves_successful_work(self):
        self.service.discover_tick()
        with patch('asset_intelligence.recognize_ocr',side_effect=[ValueError('识别无效'),reading(),reading()]),patch('asset_intelligence.vision',return_value=json.dumps(result())):
            self.drain()
        state=self.service.pipeline_state();self.assertEqual((state['failed'],state['completed']),(1,2));self.assertEqual(state['phase'],'attention')
        self.service.control('retry')
        with patch('asset_intelligence.recognize_ocr',side_effect=reading) as ocr,patch('asset_intelligence.vision',return_value=json.dumps(result())) as vision:
            self.drain();self.assertEqual((ocr.call_count,vision.call_count),(1,1))
        self.assertEqual(self.service.pipeline_state()['completed'],3)

    def test_source_change_rejects_late_ocr_and_requeues_new_fingerprint(self):
        self.service.discover_tick()
        def changed(*_):
            with self.catalog.connect() as c:row=c.execute("SELECT i.path FROM items i JOIN intelligence_jobs j ON j.item=i.id WHERE j.status='running'").fetchone()
            Image.new('RGB',(220,180),'green').save(row['path']);self.catalog.scan(self.rows[0]['root']);return reading()
        with patch('asset_intelligence.recognize_ocr',side_effect=changed):self.service.tick()
        with self.catalog.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM intelligence_ocr').fetchone()[0],0)
        with patch('asset_intelligence.recognize_ocr',side_effect=reading) as ocr,patch('asset_intelligence.vision',return_value=json.dumps(result())) as vision:
            self.drain();self.assertEqual((ocr.call_count,vision.call_count),(3,3))

    def test_interrupted_request_stays_queued_and_resumes_without_retry_delay(self):
        self.service.discover_tick()
        def interrupted(*_):self.service.closed.set();raise ValueError('推理任务已暂停')
        with patch('asset_intelligence.recognize_ocr',side_effect=interrupted):self.service.tick()
        with self.catalog.connect() as c:
            row=c.execute("SELECT status,due FROM intelligence_jobs WHERE reason!=''").fetchone()
            self.assertEqual(row['status'],'queued');self.assertLessEqual(row['due'],time.time())
        self.assertFalse(self.sku.scheduler.state()['ocr_pending'])
        self.service.close();self.service=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=False)
        with patch('asset_intelligence.recognize_ocr',side_effect=reading),patch('asset_intelligence.vision',return_value=json.dumps(result())):
            self.drain()
        self.assertEqual(self.service.pipeline_state()['completed'],3)

    def test_pause_while_waiting_for_provider_slot_keeps_work_resumable(self):
        self.service.discover_tick()
        with patch('asset_intelligence.recognize_ocr',side_effect=reading):
            for _ in range(3):self.service.tick()
        provider=self.providers.selected('multimodal');self.providers.save({**provider,'concurrency':1})
        started=threading.Event();release=threading.Event()
        def vision(*_):started.set();release.wait(3);return json.dumps(result())
        with patch('asset_intelligence.vision',side_effect=vision),ThreadPoolExecutor(3) as pool:
            futures=[pool.submit(self.service.tick) for _ in range(3)]
            try:
                self.assertTrue(started.wait(2));time.sleep(.15);self.service.control('pause')
            finally:release.set()
            for future in futures:future.result(timeout=3)
        counts=self.service.state()['counts'];self.assertEqual(counts,{'completed':1,'queued':2})
        self.service.control('resume')
        with patch('asset_intelligence.vision',return_value=json.dumps(result())) as call:
            self.drain();self.assertEqual(call.call_count,2)

    def test_api_workers_use_card_capacity_without_cross_phase_overlap(self):
        for n in range(5):Image.new('RGB',(120,160),'red').save(self.folder/f'extra-{n}.jpg')
        self.catalog.scan(self.rows[0]['root']);self.service.close()
        lock=threading.Lock();active={'ocr':0,'vision':0};peak={'ocr':0,'vision':0};overlap=[]
        barriers={kind:threading.Barrier(4) for kind in active}
        def infer(kind):
            with lock:
                active[kind]+=1;peak[kind]=max(peak[kind],active[kind]);overlap.append(bool(active['ocr'] and active['vision']))
            barriers[kind].wait(timeout=5)
            with lock:active[kind]-=1
            return reading() if kind=='ocr' else json.dumps(result())
        with patch('asset_intelligence.recognize_ocr',side_effect=lambda *_:infer('ocr')),patch('asset_intelligence.vision',side_effect=lambda *_:infer('vision')):
            self.service=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=True)
            until=time.monotonic()+15
            while time.monotonic()<until:
                if self.service.pipeline_state()['completed']==8:break
                time.sleep(.05)
            self.service.close()
        self.assertEqual(self.service.pipeline_state()['completed'],8)
        self.assertEqual(peak,{'ocr':4,'vision':4});self.assertFalse(any(overlap),overlap)

    def test_switching_ocr_mode_changes_queue_version_without_changing_files(self):
        old=self.service._config_key();self.sku.full_auto['enabled']=False
        self.assertNotEqual(old,self.service._config_key())
        with patch('asset_intelligence.recognize_ocr') as ocr,patch('asset_intelligence.vision',return_value=json.dumps(result())):
            self.drain();ocr.assert_not_called()
        self.assertEqual(self.service.pipeline_state()['completed'],3)

    def test_retry_failed_does_not_restart_cancelled_jobs(self):
        self.service.discover_tick()
        with self.catalog.connect() as c:
            ids=[r[0] for r in c.execute('SELECT id FROM intelligence_jobs')]
            c.execute("UPDATE intelligence_jobs SET status='failed' WHERE id=?",(ids[0],))
            c.execute("INSERT INTO intelligence_jobs SELECT 'old-profile',item,fingerprint,'obsolete','failed',0,'',due,created,updated FROM intelligence_jobs WHERE id=?",(ids[0],))
        self.service.control('cancel',ids[1]);self.service.control('retry_failed')
        with self.catalog.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM intelligence_jobs WHERE id=?',(ids[1],)).fetchone()[0],'cancelled')
            self.assertEqual(c.execute('SELECT status FROM intelligence_jobs WHERE id=?',(ids[0],)).fetchone()[0],'queued')
            self.assertEqual(c.execute("SELECT status FROM intelligence_jobs WHERE id='old-profile'").fetchone()[0],'failed')

    def test_manual_run_includes_files_still_being_imported_before_vision(self):
        self.service.configure({'enabled':False});self.service.control('start')
        with patch('asset_intelligence.recognize_ocr',side_effect=reading) as ocr,patch('asset_intelligence.vision',return_value=json.dumps(result())) as vision:
            with patch.object(self.catalog,'scanning_roots',return_value={self.rows[0]['root']}):
                self.drain();self.assertEqual(ocr.call_count,3);vision.assert_not_called()
                self.assertTrue(self.service.config['manual_active'])
            Image.new('RGB',(120,160),'red').save(self.folder/'later.jpg');self.catalog.scan(self.rows[0]['root'])
            self.drain();self.assertEqual((ocr.call_count,vision.call_count),(4,4))
            self.assertFalse(self.service.config['manual_active'])

    def test_old_scanning_marker_does_not_block_restart_and_active_scan_is_tracked(self):
        with self.catalog.connect() as c:c.execute("UPDATE roots SET status='scanning'")
        with patch('asset_intelligence.recognize_ocr',side_effect=reading),patch('asset_intelligence.vision',return_value=json.dumps(result())):
            self.drain()
        self.assertEqual(self.service.pipeline_state()['completed'],3)
        started=threading.Event();release=threading.Event();rid=self.rows[0]['root']
        def scanning(_):started.set();release.wait(2);raise ValueError('扫描取消')
        with patch.object(self.catalog,'_scan',side_effect=scanning),ThreadPoolExecutor(1) as pool:
            task=pool.submit(self.catalog.scan,rid)
            try:self.assertTrue(started.wait(1));self.assertIn(rid,self.catalog.scanning_roots())
            finally:release.set()
            with self.assertRaises(ValueError):task.result(timeout=2)
        self.assertFalse(self.catalog.scanning_roots())

    def test_missing_model_does_not_prevent_pause_or_hide_saved_progress(self):
        self.service.discover_tick();self.providers.delete(self.providers.selected('multimodal')['id'])
        self.service.control('pause');state=self.service.pipeline_state()
        self.assertEqual(state['phase'],'paused');self.assertEqual(state['total'],3)
        self.assertIn('模型',state['error'])
        with self.assertRaises(ValueError):self.service.control('resume')
        self.assertTrue(self.service.config['paused'])

    def test_large_ocr_batch_is_not_blocked_by_old_thousand_item_queue_limit(self):
        with self.catalog.connect() as c:
            original=dict(c.execute('SELECT * FROM items LIMIT 1').fetchone());columns=list(original)
            rows=[]
            for index in range(1100):
                name=f'bulk-{index}.jpg';path=str(self.folder/name)
                row={**original,'id':f'bulk-{index}','path':path,'name':name,'relative':name,'pair_key':str(self.folder/f'bulk-{index}').casefold()}
                rows.append([row[key] for key in columns])
            c.executemany('INSERT INTO items('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',rows)
        for _ in range(20):
            if not self.service.discover_tick():break
        self.assertTrue(self.service.seed_done)
        self.assertEqual(self.service.pipeline_state()['total'],1103)
        self.assertEqual(self.service.pipeline_state()['phase'],'ocr')


class StageGateTests(unittest.TestCase):
    def test_new_ocr_waits_for_existing_vision_then_blocks_new_vision(self):
        scheduler=VisionScheduler();scheduler.order_archive(True)
        vision_started=threading.Event();release_vision=threading.Event();ocr_started=threading.Event();release_ocr=threading.Event();next_vision=threading.Event()
        def vision():
            with scheduler.slot(stage='vision',independent=True):vision_started.set();release_vision.wait(3)
        def ocr():
            with scheduler.slot(stage='ocr',independent=True):ocr_started.set();release_ocr.wait(3)
        def later():
            with scheduler.slot(stage='vision',independent=True):next_vision.set()
        with ThreadPoolExecutor(3) as pool:
            a=pool.submit(vision);self.assertTrue(vision_started.wait(1));scheduler.order_archive(True,True)
            b=pool.submit(ocr);c=pool.submit(later)
            try:
                self.assertFalse(ocr_started.wait(.15));release_vision.set();self.assertTrue(ocr_started.wait(1));self.assertFalse(next_vision.is_set())
                scheduler.order_archive(True,False);self.assertFalse(next_vision.wait(.15))
            finally:release_vision.set();release_ocr.set();scheduler.order_archive(False)
            for future in (a,b,c):future.result(timeout=3)
        self.assertTrue(next_vision.is_set());self.assertEqual(scheduler.state()['active'],0)


if __name__=='__main__':unittest.main()
