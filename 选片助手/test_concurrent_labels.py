"""阻塞旧推理的并发验收；覆盖 API 容量、代次、取消和全自动未扫描标签。"""
import json
import threading
import time
import unittest

from PIL import Image
import media_engine as media
import test_sku_workflow as fixture
import test_ocr_metadata as metadata_fixture
from vision_scheduler import VisionScheduler


class ConcurrentLabelTests(unittest.TestCase):
    setUp=fixture.SkuWorkflowTests.setUp
    tearDown=fixture.SkuWorkflowTests.tearDown
    rate=fixture.SkuWorkflowTests.rate

    def wait(self,predicate,timeout=3):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            if predicate():return
            time.sleep(.01)
        self.fail('并发任务未在超时前达到预期状态')

    def jobs(self):
        with self.sku.connect() as c:return [dict(r) for r in c.execute('SELECT * FROM jobs')]

    def test_selected_visual_model_does_not_delay_ocr_unless_explicitly_bound(self):
        visual=self.providers.selected('multimodal')['id']
        self.providers.save({'name':'fast OCR','category':'ocr','protocol':'chat','url':'https://example.invalid','model':'ocr'})
        ocr=next(p['id'] for p in self.providers.public()['providers'] if p['category']=='ocr')
        self.providers.select(ocr)
        self.rate(2);self.sku.tick()
        self.assertEqual(self.calls,[ocr])
        self.assertEqual(self.providers.selected('multimodal')['id'],visual)
        self.providers.bind('sku_label',visual)
        self.assertEqual([p['id'] for p in self.sku._label_providers()],[ocr,visual])
        self.assertEqual([p['id'] for p in self.sku._label_providers({'trigger':'full_auto'})],[ocr])

    def test_star_save_does_not_build_whole_document_state(self):
        from unittest.mock import patch
        with patch.object(self.sku,'state',side_effect=AssertionError('标星不能读取整份清单')):
            self.rate(2)
        self.assertEqual(self.jobs()[0]['status'],'queued')

    def test_label_input_never_waits_for_browser_preview_generation(self):
        from unittest.mock import patch
        self.rate(2)
        with patch('media_engine.preview_bytes',side_effect=AssertionError('OCR不能等待浏览预览生成')):
            self.sku.tick()
        self.assertEqual(self.jobs()[0]['status'],'matched')
        self.assertEqual(len(self.calls),1)

    def test_bulk_ocr_reserves_one_slot_and_dispatches_new_star_without_waiting(self):
        self.providers.save({'name':'OCR','category':'ocr','protocol':'chat','url':'http://127.0.0.1:1','model':'ocr','select':True})
        for i in range(2,5):
            path=self.photos/f'label{i}.JPG';Image.new('RGB',(100,80),'white').save(path)
            record=media.capture_record(self.photos,path,None,{})
            self.store.append_live(self.sid,[record]);self.cids.append(record['id'])
        release=threading.Event();calls=[];latest=threading.Event()
        def recognize(*_):
            calls.append(1)
            if len(calls)<=3:release.wait(8)
            else:latest.set()
            return fixture.reading()
        self.sku.recognizer=recognize
        self.sku.enqueue_parses(self.sid,self.cids[:4]);self.sku.thread.start()
        try:
            self.wait(lambda:len(calls)==3)
            self.rate(2,self.cids[4]);self.assertTrue(latest.wait(2),'二星没有使用预留的OCR名额')
            self.assertFalse(release.is_set())
            self.wait(lambda:any(j['status']=='matched' for j in self.jobs()))
        finally:release.set();self.sku.close()

    def test_new_star_is_durable_while_previous_result_holds_business_lock(self):
        from asset_archive import AssetArchive
        archive=AssetArchive(self.store,autostart=False);self.sku.archive=archive
        held=threading.Event();release=threading.Event();done=threading.Event();errors=[]
        def previous():
            with self.sku.lock:held.set();release.wait(8)
        def mark():
            try:self.rate(2)
            except Exception as exc:errors.append(exc)
            finally:done.set()
        owner=threading.Thread(target=previous);owner.start();worker=threading.Thread(target=mark)
        try:
            self.assertTrue(held.wait(2));worker.start()
            self.assertTrue(done.wait(2),'新二星保存仍排在旧款归档业务锁后')
            self.assertFalse(errors,errors);self.assertEqual(self.jobs()[0]['status'],'queued')
        finally:release.set();owner.join(5);worker.join(5);self.sku.archive=None;archive.close()

    def test_latest_api_label_bypasses_occupied_shared_capacity_and_bulk_read(self):
        first=threading.Event();second=threading.Event();release=threading.Event();calls=[]
        self.sku.scheduler=VisionScheduler(limit=1)
        def recognize(*_):
            calls.append(1)
            if len(calls)==1:first.set();release.wait(5)
            else:second.set()
            return fixture.reading()
        self.sku.recognizer=recognize
        self.sku.enqueue_parses(self.sid,[self.cids[0]]);self.sku.thread.start()
        try:
            self.assertTrue(first.wait(2));self.rate(2,self.cids[-1])
            self.assertTrue(second.wait(2));self.assertFalse(release.is_set())
            self.wait(lambda:any(j['status']=='matched' for j in self.jobs()))
        finally:release.set();self.sku.close()

    def test_four_api_labels_are_running_and_repeated_mark_does_not_duplicate(self):
        for i in range(2,4):
            path=self.photos/f'label{i}.JPG';Image.new('RGB',(100,80),'white').save(path)
            record=media.capture_record(self.photos,path,None,{})
            self.store.append_live(self.sid,[record]);self.cids.append(record['id'])
        release=threading.Event();four=threading.Event();calls=[];lock=threading.Lock()
        self.sku.scheduler=VisionScheduler(limit=1)
        def recognize(*_):
            with lock:
                calls.append(1)
                if len(calls)==4:four.set()
            release.wait(5);return fixture.reading()
        self.sku.recognizer=recognize;self.sku.thread.start()
        try:
            for cid in self.cids:self.rate(2,cid)
            self.assertTrue(four.wait(2));self.assertEqual(self.sku.scheduler.state()['active'],4)
            self.rate(2,self.cids[-1]);self.assertEqual(len(calls),4)
            self.assertEqual([j['status'] for j in self.jobs()],['running']*4)
            release.set();self.wait(lambda:all(j['status']=='matched' for j in self.jobs()))
        finally:release.set();self.sku.close()

    def test_full_auto_unscanned_label_starts_while_bulk_request_is_running(self):
        started=threading.Event();latest=threading.Event();release=threading.Event();calls=[]
        self.sku.scheduler=VisionScheduler(limit=1)
        def recognize(*_):
            calls.append(1)
            if len(calls)==1:started.set();release.wait(5)
            else:latest.set()
            return fixture.reading()
        self.sku.recognizer=recognize;self.sku.set_full_auto(True,self.sid)
        # 模拟全自动大目录尚未扫描到新标签；普通 OCR 已占用唯一共享容量。
        self.sku.full_scan_at=self.clock[0]+100
        self.sku.enqueue_parses(self.sid,[self.cids[0]]);self.sku.thread.start()
        try:
            self.assertTrue(started.wait(2));self.rate(2,self.cids[-1])
            self.assertTrue(latest.wait(2));self.assertFalse(release.is_set())
            job=next(j for j in self.jobs() if j['capture_id']==self.cids[-1])
            self.assertEqual(job['trigger'],'full_auto')
        finally:release.set();self.sku.close()

    def test_cancel_and_restar_discards_late_previous_attempt(self):
        started=threading.Event();latest=threading.Event();release=threading.Event();calls=[]
        self.sku.require_sku_choice=True
        def recognize(*_):
            calls.append(1)
            if len(calls)==1:
                started.set();release.wait(5);return fixture.reading('OLD-111')
            latest.set();return fixture.reading('NEW-222')
        self.sku.recognizer=recognize;self.sku.thread.start()
        try:
            self.rate(2);self.assertTrue(started.wait(2));token=self.jobs()[0]['run_token']
            self.rate(0);self.rate(2);self.assertTrue(latest.wait(2))
            self.wait(lambda:self.jobs()[0]['status']=='review')
            self.assertNotEqual(self.jobs()[0]['run_token'],token)
            release.set();self.wait(lambda:not self.sku.label_workers)
            result=json.loads(self.jobs()[0]['result'])
            self.assertEqual(result['readings'][0]['codes'],['NEW-222'])
            self.assertNotIn('OLD-111',self.sku.ocr_details(self.sid,self.cids[0])['text'])
        finally:release.set();self.sku.close()

    def test_cancel_inflight_and_pause_block_new_independent_requests(self):
        started=threading.Event();release=threading.Event();calls=[]
        def recognize(*_):calls.append(1);started.set();release.wait(5);return fixture.reading()
        self.sku.recognizer=recognize;self.sku.thread.start()
        try:
            self.rate(2);self.assertTrue(started.wait(2));self.rate(0)
            self.sku.configure_tasks(paused=True);self.rate(2,self.cids[-1]);release.set()
            self.wait(lambda:not self.sku.label_workers)
            self.assertEqual(len(calls),1)
            states={j['capture_id']:j['status'] for j in self.jobs()}
            self.assertEqual(states[self.cids[0]],'cancelled');self.assertEqual(states[self.cids[-1]],'queued')
        finally:release.set();self.sku.close()

    def test_local_models_keep_the_shared_capacity_limit(self):
        from unittest.mock import patch
        self.providers.set_builtin(True)
        self.assertTrue(self.sku._parallel_labels_enabled())
        entered=threading.Event();release=threading.Event()
        def run(job,**kwargs):entered.set();release.wait(5)
        with patch.object(self.sku,'_process_label',side_effect=run):
            try:
                self.rate(2);self.rate(2,self.cids[-1]);self.sku._dispatch_labels();self.assertTrue(entered.wait(2))
                self.sku._dispatch_labels();self.assertEqual(len(self.sku.label_workers),1)
                self.assertEqual(sum(j['status']=='queued' for j in self.jobs()),1)
            finally:release.set();self.sku.close()

    def test_parallel_api_request_is_sent_before_slow_archival_reservation(self):
        from asset_archive import AssetArchive
        model=threading.Event();reserved=threading.Event();release=threading.Event()
        def reserve(_):reserved.set();release.wait(5)
        archive=AssetArchive(self.store,autostart=False);archive.reserve=reserve;self.sku.archive=archive
        self.sku.require_sku_choice=True;self.sku.set_sku_rules([])
        def recognize(*_):model.set();return fixture.reading()
        self.sku.recognizer=recognize;self.sku.thread.start()
        try:
            self.rate(2);self.assertTrue(model.wait(2));self.wait(lambda:self.jobs()[0]['status']=='review')
            self.assertFalse(reserved.is_set(),'API结果不应等待同步区间扫描')
            with self.sku.connect() as c:self.assertTrue(c.execute('SELECT 1 FROM archive_events WHERE delivered=0').fetchone())
            self.assertFalse(release.is_set())
        finally:release.set();self.sku.close();archive.close()


class ConcurrentArchiveTests(unittest.TestCase):
    setUp=metadata_fixture.OcrMetadataTests.setUp
    tearDown=metadata_fixture.OcrMetadataTests.tearDown
    rate=fixture.SkuWorkflowTests.rate
    choose=metadata_fixture.OcrMetadataTests.choose
    wait=ConcurrentLabelTests.wait

    def test_out_of_order_results_keep_unfinished_label_as_interval_boundary(self):
        import copy,io
        from sku_library import labels
        session=self.store.session(self.sid);state=copy.deepcopy(session['state'])
        state['overrides']={cid:0 for cid in self.ids};self.store.save(self.sid,session['revision'],state)
        old=threading.Event();release=threading.Event();first='LC1111111';second='LC2222222'
        def recognize(_,image):
            with Image.open(io.BytesIO(image)) as picture:red=picture.getpixel((0,0))[0]
            if red<80:old.set();release.wait(20);return fixture.reading(first)
            return fixture.reading(second)
        self.sku.recognizer=recognize;self.sku.thread.start()
        def ready(cid):
            with self.sku.connect() as c:return bool(c.execute("SELECT 1 FROM jobs WHERE capture_id=? AND status='review'",(cid,)).fetchone())
        try:
            self.rate(2,self.ids[1]);self.assertTrue(old.wait(3))
            self.rate(2,self.ids[3]);self.wait(lambda:ready(self.ids[3]),timeout=10)
            self.choose(second,self.ids[3]);mapping=labels(self.sku,self.sid)['labels']
            self.assertEqual(mapping,{self.ids[2]:second,self.ids[3]:second})
            self.assertFalse(release.is_set())
            release.set();self.wait(lambda:ready(self.ids[1]),timeout=10)
            self.choose(first,self.ids[1]);mapping=labels(self.sku,self.sid)['labels']
            self.assertEqual(mapping,{self.ids[0]:first,self.ids[1]:first,self.ids[2]:second,self.ids[3]:second})
        finally:release.set();self.sku.close()


if __name__=='__main__':unittest.main()
