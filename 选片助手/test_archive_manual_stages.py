"""缩略图先完成、单阶段手动启动、重启不自动调用模型。"""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch

from asset_intelligence import AssetIntelligence
from preview_cache import PreviewWarmer,cache_for
import test_archive_analysis_pipeline as pipeline_fixture
from test_archive_analysis_pipeline import reading
from test_asset_intelligence import result
from vision_scheduler import VisionScheduler


class ManualStageTests(unittest.TestCase):
    def setUp(self):
        pipeline_fixture.ArchivePipelineTests.setUp(self)
        self.worker=PreviewWarmer(cache_for(self.base/'data/previews'),'unused',catalog=self.catalog,autostart=False)
        self.worker.attach_scheduler(self.sku.scheduler)
        self.worker.reconcile_requested.clear()
        self.catalog.preview_warmer=self.worker;self.catalog.on_previews=self.worker.enqueue
        self.catalog.queue_all_previews();self.worker.refresh_phase()
        self.service.close();self.service=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=False)

    def tearDown(self):
        self.worker.close();pipeline_fixture.ArchivePipelineTests.tearDown(self)

    drain=pipeline_fixture.ArchivePipelineTests.drain

    def cache_all(self):
        while self.worker.process_one():pass
        self.assertTrue(self.worker.refresh_phase()['ready'])

    def test_cache_gate_then_manual_ocr_stops_before_vision(self):
        with self.assertRaisesRegex(ValueError,'缩略图'):self.service.control('start_ocr')
        with self.assertRaisesRegex(ValueError,'手动启动'):self.service.configure({'enabled':True})
        self.assertFalse(self.service.pipeline_state()['running'])
        self.cache_all();self.service.control('start_ocr')
        with patch('asset_intelligence.recognize_ocr',side_effect=reading) as ocr,patch('asset_intelligence.vision') as vision:
            self.drain();self.drain()
            self.assertEqual(ocr.call_count,3);vision.assert_not_called()
        state=self.service.pipeline_state()
        self.assertEqual((state['ocr_completed'],state['completed']),(3,0));self.assertFalse(state['running'])
        self.service.control('start_vision')
        with patch('asset_intelligence.recognize_ocr') as ocr,patch('asset_intelligence.vision',return_value=json.dumps(result())) as vision:
            self.drain();ocr.assert_not_called();self.assertEqual(vision.call_count,3)
            self.assertTrue(all('UNIQUEOCR123' in call.args[2] for call in vision.call_args_list))
        self.assertFalse(self.service.pipeline_state()['running'])
        self.assertEqual(self.service.pipeline_state()['completed'],3)

    def test_restart_keeps_ocr_checkpoint_but_requires_manual_continue(self):
        self.cache_all();self.service.control('start_ocr');self.service.discover_tick()
        with patch('asset_intelligence.recognize_ocr',side_effect=reading):self.assertTrue(self.service.tick())
        self.service.close();self.service=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=False)
        self.assertFalse(self.service.tick());self.assertFalse(self.service.pipeline_state()['running'])
        self.service.control('start_ocr')
        with patch('asset_intelligence.recognize_ocr',side_effect=reading) as ocr,patch('asset_intelligence.vision') as vision:
            self.drain();self.assertEqual(ocr.call_count,2);vision.assert_not_called()
        self.assertEqual(self.service.pipeline_state()['ocr_completed'],3)

    def test_failed_previews_do_not_allow_model_start(self):
        with patch.object(self.catalog,'render_preview_batch',side_effect=RuntimeError('损坏图片')):
            while self.worker.process_one():pass
        self.assertEqual(self.worker.refresh_phase()['failed'],3)
        with self.assertRaisesRegex(ValueError,'失败项'):self.service.control('start_vision')
        self.catalog.remove(self.rows[0]['root'])
        self.assertTrue(self.worker.refresh_phase()['ready'])

    def test_ocr_stage_does_not_require_a_vision_model(self):
        self.cache_all();selected=self.providers.selected
        with patch.object(self.providers,'purpose',return_value=None),patch.object(self.providers,'selected',side_effect=lambda category:selected(category) if category=='ocr' else None):
            self.service.control('start_ocr')
            with patch('asset_intelligence.recognize_ocr',side_effect=reading):self.drain()
            self.assertEqual(self.service.pipeline_state()['ocr_completed'],3)
            with self.assertRaisesRegex(ValueError,'多模态'):self.service.control('start_vision')

    def test_realtime_completed_count_keeps_up_while_full_statistics_are_blocked(self):
        entered=threading.Event();release=threading.Event();original=self.catalog.state
        def slow():
            entered.set();release.wait(3);return original()
        with patch.object(self.catalog,'state',side_effect=slow):
            initial=self.catalog.progress.snapshot(self.worker)
            self.assertFalse(initial['initialized']);self.assertTrue(entered.wait(1))
            try:
                self.assertTrue(self.worker.process_one())
                live=self.catalog.progress.snapshot(self.worker)
                self.assertFalse(live['initialized'])
                self.assertEqual(live['previews']['completed_this_run'],1)
                self.assertEqual(live['previews']['ready'],0,'未核对累计数不能冒充精确总数')
            finally:release.set();self.catalog.progress.worker.join(3)

    def test_stopped_models_do_not_scan_database_during_decode(self):
        with patch.object(self.catalog,'connect',side_effect=AssertionError('缓存阶段不应查询整库模型进度')):
            state=self.service.pipeline_state()
        self.assertEqual(state['phase'],'decoding');self.assertFalse(state['running'])
        self.assertFalse(state['counts_ready']);self.assertIsNone(state['total'])


class DecodeModelExclusionTests(unittest.TestCase):
    def test_decode_waits_for_inflight_model_and_pending_decode_blocks_both_models(self):
        scheduler=VisionScheduler();model=threading.Event();release=threading.Event();decoded=threading.Event();finish=threading.Event();ocr=threading.Event()
        def first():
            with scheduler.slot(stage='vision',independent=True):model.set();release.wait(5)
        def decode():
            with scheduler.preview_slot():decoded.set();finish.wait(5)
        def next_model():
            with scheduler.slot(stage='ocr',independent=True):ocr.set()
        with ThreadPoolExecutor(3) as pool:
            a=pool.submit(first);self.assertTrue(model.wait(1));scheduler.order_previews(True)
            b=pool.submit(decode);c=pool.submit(next_model)
            try:
                self.assertFalse(decoded.wait(.1));self.assertFalse(ocr.is_set())
                release.set();self.assertTrue(decoded.wait(1));scheduler.order_previews(False)
                self.assertFalse(ocr.wait(.1))
            finally:release.set();finish.set();scheduler.order_previews(False)
            for future in (a,b,c):future.result(3)
        self.assertTrue(ocr.is_set());self.assertEqual(scheduler.state()['preview_active'],0)

    def test_ocr_and_vision_never_overlap_even_without_archive_ordering(self):
        scheduler=VisionScheduler();started=threading.Event();release=threading.Event();vision=threading.Event()
        def run_ocr():
            with scheduler.slot(stage='ocr',independent=True):started.set();release.wait(3)
        def run_vision():
            with scheduler.slot(stage='vision',independent=True):vision.set()
        with ThreadPoolExecutor(2) as pool:
            a=pool.submit(run_ocr);self.assertTrue(started.wait(1));b=pool.submit(run_vision)
            try:self.assertFalse(vision.wait(.1))
            finally:release.set()
            a.result(3);b.result(3)
        self.assertTrue(vision.is_set())


if __name__=='__main__':unittest.main()
