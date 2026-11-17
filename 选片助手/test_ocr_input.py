"""OCR 取图只读：缓存命中不访问 NAS，未命中不生成浏览缓存。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import io
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from preview_cache import cache_for
from ocr_input import catalog_input,record_input
import test_analysis_workflow as fixture


class OcrInputTests(unittest.TestCase):
    setUp=fixture.AnalysisWorkflowTests.setUp
    tearDown=fixture.AnalysisWorkflowTests.tearDown

    def row(self):
        with self.catalog.connect() as c:return dict(c.execute('SELECT * FROM items ORDER BY id LIMIT 1').fetchone())

    def enqueue(self,row):
        run=self.workflow.execute('ocr',self.sid)['runs']['ocr']['id']
        self.workflow.enqueue('ocr','catalog',[row],run=run)

    def test_clear_cached_input_uses_no_source_stat_read_generation_or_cache_writer(self):
        row=self.row();self.catalog.preview(row['id'],2048,read=True)
        index=cache_for(self.catalog.previews);self.enqueue(row);original=Path.stat
        def stat(path,*args,**kwargs):
            if str(path).startswith(str(self.archive_folder)):raise AssertionError('缓存 OCR 不得访问 NAS 文件信息')
            return original(path,*args,**kwargs)
        with index.lock,index.connect() as writer,ThreadPoolExecutor(1) as pool,ExitStack() as stack:
            writer.execute('BEGIN IMMEDIATE')
            stack.enter_context(patch('ocr_input.Path.stat',new=stat))
            stack.enter_context(patch.object(self.catalog,'preview',side_effect=AssertionError('不得生成浏览预览')))
            stack.enter_context(patch('media_engine.warm_previews',side_effect=AssertionError('不得批量生成缩略图')))
            self.assertTrue(pool.submit(self.workflow.tick).result(3))
        self.assertEqual(self.ocr.call_count,1)
        self.assertEqual(self.workflow.state()['timings']['cached_input']['count'],1)
        with self.catalog.connect() as c:self.assertTrue(c.execute('SELECT 1 FROM intelligence_ocr WHERE item=?',(row['id'],)).fetchone())

    def test_cache_miss_reads_jpg_once_in_memory_without_publishing_a_preview(self):
        row=self.row();index=cache_for(self.catalog.previews);original=Path.read_bytes;reads=[]
        def read(path):
            if str(path)==row['path']:reads.append(str(path))
            return original(path)
        with index.read_connection() as c:before=c.execute('SELECT count(*) FROM previews').fetchone()[0]
        with patch('ocr_input.Path.read_bytes',new=read),patch.object(index,'publish',side_effect=AssertionError('OCR 不应生成缓存文件')),patch('media_engine.warm_previews',side_effect=AssertionError('OCR 不应生成缩略图')):
            prepared=catalog_input(self.catalog,row);prepared.validate(self.catalog)
        self.assertFalse(prepared.cached);self.assertEqual(reads,[row['path']])
        with Image.open(io.BytesIO(prepared.payload)) as image:self.assertGreater(min(image.size),0)
        with index.read_connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM previews').fetchone()[0],before)

    def test_slow_file_metadata_does_not_serialize_reads_of_other_cached_images(self):
        with self.catalog.connect() as c:rows=[dict(row) for row in c.execute('SELECT * FROM items')]
        for row in rows:self.catalog.preview(row['id'],2048,read=True)
        index=cache_for(self.catalog.previews)
        with index.read_connection() as c:names=[row[0] for row in c.execute('SELECT name FROM previews ORDER BY name')]
        entered=threading.Event();release=threading.Event();original=Path.stat
        def stat(path,*args,**kwargs):
            if path==index.folder/names[0]:entered.set();release.wait(5)
            return original(path,*args,**kwargs)
        with patch('preview_cache.Path.stat',new=stat),ThreadPoolExecutor(2) as pool:
            first=pool.submit(index.get,names[0],read=True)
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(pool.submit(index.get,names[1],read=True).result(1))
            finally:release.set()
            self.assertTrue(first.result(2))

    def test_small_thumbnail_is_not_used_as_clear_ocr_input(self):
        row=self.row();self.catalog.preview(row['id'],420,read=True)
        prepared=catalog_input(self.catalog,row)
        self.assertFalse(prepared.cached)

    def test_cached_input_respects_model_resolution_without_writing_an_intermediate_size(self):
        path=self.archive_folder/'large.jpg';Image.new('RGB',(2400,1800),'green').save(path)
        self.catalog.scan(self.root)
        with self.catalog.connect() as c:row=dict(c.execute('SELECT * FROM items WHERE name=?',('large.jpg',)).fetchone())
        self.catalog.preview(row['id'],2048,read=True);index=cache_for(self.catalog.previews)
        with index.read_connection() as c:before=c.execute('SELECT count(*) FROM previews').fetchone()[0]
        prepared=catalog_input(self.catalog,row,1600)
        self.assertTrue(prepared.cached)
        with Image.open(io.BytesIO(prepared.payload)) as image:self.assertEqual(max(image.size),1600)
        with index.read_connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM previews').fetchone()[0],before)

    def test_changed_index_invalidates_cached_input_during_inference(self):
        row=self.row();self.catalog.preview(row['id'],2048,read=True);self.enqueue(row)
        def changed(*args):
            with self.catalog.connect() as c:c.execute("UPDATE items SET fingerprint='new-version' WHERE id=?",(row['id'],))
            return {'text':'OLD-RESULT','codes':[]}
        self.ocr.side_effect=changed;self.workflow.tick()
        with self.analysis.connect() as c:
            job=c.execute('SELECT * FROM workflow_jobs').fetchone()
            self.assertEqual(job['status'],'failed');self.assertIn('版本已变化',job['reason'])
        with self.catalog.connect() as c:self.assertFalse(c.execute('SELECT 1 FROM intelligence_ocr WHERE item=?',(row['id'],)).fetchone())

    def test_existing_model_result_is_saved_without_any_input_read_or_second_inference(self):
        row=self.row();self.enqueue(row)
        with self.analysis.connect() as c:c.execute("UPDATE workflow_jobs SET result=?,input_sha=?",('{"text":"SAVED-OCR","codes":[]}','b'*64))
        with patch('ocr_input.cache_for',side_effect=AssertionError('已有结果不必读取缓存')),patch.object(self.catalog,'preview',side_effect=AssertionError('已有结果不应读原图')):
            self.assertTrue(self.workflow.tick())
        self.ocr.assert_not_called();self.assertEqual(self.workflow.state()['timings']['reused_result']['count'],1)

    def test_batch_reserves_background_before_input_and_releases_on_pause_or_close(self):
        run=self.workflow.execute('ocr',self.sid)['runs']['ocr'];started=threading.Event()
        self.assertTrue(self.scheduler.state()['workflow_priority'])
        def background():
            with self.scheduler.preview_slot():started.set()
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(background);self.assertFalse(started.wait(.1))
            self.workflow.configure({'ocr_paused':True});self.assertTrue(started.wait(1));future.result(1)
        self.workflow.configure({'ocr_paused':False});self.assertTrue(self.scheduler.state()['workflow_priority'])
        self.workflow.close();self.assertFalse(self.scheduler.state()['workflow_priority'])
        from analysis_workflow import AnalysisWorkflow
        self.workflow=AnalysisWorkflow(self.analysis,self.intelligence,self.sku,lambda:self.monitor)
        self.assertEqual(self.workflow.state()['runs']['ocr']['id'],run['id'])
        self.assertTrue(self.scheduler.state()['workflow_priority'])

    def test_jpg_is_read_without_touching_its_paired_raw_file(self):
        jpg=self.photos/'paired.jpg';raw=self.photos/'paired.CR3';Image.new('RGB',(200,300),'green').save(jpg);raw.write_bytes(b'not-a-real-raw')
        record={'id':'pair','fingerprint':'pair-current','jpg':jpg.name,'raw':raw.name,'_fallback_raw':str(raw)}
        original=Path.stat
        def stat(path,*args,**kwargs):
            if path==raw:raise AssertionError('有 JPG 时不应先读取 RAW')
            return original(path,*args,**kwargs)
        with patch('ocr_input.Path.stat',new=stat),patch('media_engine._raw_preview',side_effect=AssertionError('不提取 RAW')):
            prepared=record_input(record,self.photos,self.store.data/'previews','unused')
        self.assertFalse(prepared.cached)

    def test_ocr_defers_bulk_metadata_but_still_allows_explicit_details_requests(self):
        metadata=self.catalog.metadata;row=self.row();self.workflow.execute('ocr',self.sid)
        with patch.object(metadata,'_extract',return_value=({},'explicit details')) as extract:
            self.assertFalse(metadata.process_one());extract.assert_not_called()
            metadata.request(row['id']);self.assertTrue(metadata.process_one());self.assertEqual(extract.call_count,1)
            self.workflow.configure({'ocr_paused':True})
            self.assertTrue(metadata.process_one());self.assertEqual(extract.call_count,2)

    def test_restart_does_not_requeue_all_ready_metadata_during_an_ocr_batch(self):
        metadata=self.catalog.metadata;self.workflow.execute('ocr',self.sid)
        with self.catalog.connect() as c:c.execute("UPDATE catalog_metadata SET status='ready'")
        def stop_loop():metadata.stop.set();return False
        with patch.object(metadata,'process_one',side_effect=stop_loop):metadata._run()
        with self.catalog.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM catalog_metadata WHERE status!='ready'").fetchone()[0],0)

    def test_background_archive_discovery_yields_to_a_manual_model_batch(self):
        from archive_pipeline import ArchivePipeline
        from types import SimpleNamespace
        self.sku.scheduler=self.scheduler
        pipeline=ArchivePipeline(self.catalog,self.archive,self.sku,SimpleNamespace(),autostart=False)
        try:
            self.workflow.execute('ocr',self.sid)
            with patch.object(self.catalog,'roots',side_effect=AssertionError('OCR 期间不启动归档重扫')):pipeline.tick()
            self.workflow.stop('ocr')
            with patch.object(self.catalog,'roots',return_value=[]) as roots:pipeline.tick();roots.assert_called_once()
        finally:pipeline.close()


if __name__=='__main__':unittest.main()
