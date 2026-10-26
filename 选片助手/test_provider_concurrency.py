"""卡片容量真实调度边界：持久化、跨任务共享、动态调整与队列吞吐。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json,tempfile,threading,time,unittest
from unittest.mock import patch

from PIL import Image
from model_providers import Providers
from vision_scheduler import VisionScheduler
import media_engine as media
import test_sku_workflow as sku_fixture
import test_asset_archive as archive_fixture
import test_ocr_metadata as ocr_fixture
from asset_analysis import AssetAnalysis


def wait_for(predicate,timeout=4):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if predicate():return
        time.sleep(.01)
    raise AssertionError('等待并发状态超时')


class ProviderCapacityTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.providers=Providers(self.folder.name)
        self.body={'name':'API','category':'ocr','protocol':'chat','url':'http://127.0.0.1:1234/v1','model':'test','select':True}
        self.providers.save(self.body);self.body['id']=self.providers.selected('ocr')['id']
    def limit(self,n):self.providers.save(dict(self.body,concurrency=n))
    def test_defaults_persistence_legacy_and_strict_validation(self):
        p=self.providers;self.assertEqual(p.selected('ocr')['concurrency'],4)
        self.limit(12);self.assertEqual(Providers(self.folder.name).selected('ocr')['concurrency'],12)
        self.limit(64);self.assertEqual(p.selected('ocr')['concurrency'],64);self.limit(12)
        p.save(dict(self.body,name='renamed'));self.assertEqual(p.selected('ocr')['concurrency'],12)
        before=p.path.read_bytes()
        for value in (0,65,-1,True,1.2,'4',None):
            with self.subTest(value=value),self.assertRaises(ValueError):self.limit(value)
            self.assertEqual(p.path.read_bytes(),before)
        legacy=json.loads(before);legacy['providers'][0].pop('concurrency');p.path.write_text(json.dumps(legacy),encoding='utf8')
        self.assertEqual(Providers(self.folder.name).public()['providers'][0]['concurrency'],4)
        p.save({'name':'local','category':'ocr','protocol':'local','model':'glm-ocr-local'})
        self.assertEqual(p.public()['providers'][-1]['concurrency'],1)
    def test_same_card_shared_limit_and_different_card_independence(self):
        self.limit(2);p=self.providers;card=p.selected('ocr');release=threading.Event();entered=[];lock=threading.Lock()
        def run(provider):
            with p.inference_slot(provider):
                with lock:entered.append(provider['id'])
                release.wait(5)
        with ThreadPoolExecutor(6) as pool:
            futures=[pool.submit(run,card) for _ in range(4)]
            try:
                wait_for(lambda:len(entered)==2);self.assertEqual(p.inference_active[card['id']],2)
                other=p.save({'name':'other','category':'multimodal','protocol':'chat','url':'http://127.0.0.1:1234/v1','model':'other','concurrency':1})['providers'][-1]
                futures.append(pool.submit(run,other));wait_for(lambda:len(entered)==3)
                self.assertEqual(entered.count(card['id']),2)
            finally:release.set()
            for future in futures:future.result(timeout=5)
        self.assertEqual(p.inference_active,{})
    def test_increase_wakes_waiters_and_decrease_does_not_interrupt_active_calls(self):
        self.limit(1);p=self.providers;card=p.selected('ocr');entered=[];release=[threading.Event() for _ in range(5)]
        def run(i):
            with p.inference_slot(card):entered.append(i);release[i].wait(5)
        with ThreadPoolExecutor(5) as pool:
            futures=[pool.submit(run,i) for i in range(4)]
            try:
                wait_for(lambda:len(entered)==1);self.limit(4);wait_for(lambda:len(entered)==4)
                self.limit(2);futures.append(pool.submit(run,4))
                wait_for(lambda:len(p.inference_waiters.get(card['id'],[]))==1)
                release[0].set();wait_for(lambda:p.inference_active[card['id']]==3)
                release[1].set();wait_for(lambda:p.inference_active[card['id']]==2)
                self.assertNotIn(4,entered)
                release[2].set();wait_for(lambda:4 in entered)
            finally:
                for event in release:event.set()
            for future in futures:future.result(timeout=5)
    def test_latest_star_precedes_older_star_and_bulk_waiters(self):
        self.limit(1);p=self.providers;card=p.selected('ocr');release=threading.Event();entered=[]
        def run(name,priority=0):
            with p.inference_slot(card,priority=priority):
                entered.append(name)
                if name=='held':release.wait(5)
        with ThreadPoolExecutor(4) as pool:
            futures=[pool.submit(run,'held')];wait_for(lambda:entered==['held'])
            for name,priority in [('bulk',0),('old-star',1),('new-star',2)]:futures.append(pool.submit(run,name,priority))
            try:wait_for(lambda:len(p.inference_waiters[card['id']])==3)
            finally:release.set()
            for future in futures:future.result(timeout=5)
        self.assertEqual(entered,['held','new-star','old-star','bulk'])
    def test_cancelled_waiter_never_sends_and_exception_releases_capacity(self):
        self.limit(1);p=self.providers;card=p.selected('ocr');cancel=threading.Event();entered=[]
        def waiting():
            with self.assertRaises(ValueError),p.inference_slot(card,cancel=cancel):entered.append(1)
        with ThreadPoolExecutor(1) as pool:
            with p.inference_slot(card):
                future=pool.submit(waiting);wait_for(lambda:len(p.inference_waiters.get(card['id'],[]))==1)
                cancel.set();future.result(timeout=2);self.assertEqual(entered,[])
        with self.assertRaises(RuntimeError),p.inference_slot(card):raise RuntimeError('test')
        self.assertEqual(p.inference_active,{})


class OcrCardThroughputTests(unittest.TestCase):
    setUp=sku_fixture.SkuWorkflowTests.setUp
    tearDown=sku_fixture.SkuWorkflowTests.tearDown
    rate=sku_fixture.SkuWorkflowTests.rate
    def test_bulk_fills_four_slots_and_new_star_gets_next_free_slot(self):
        for i in range(2,6):
            path=self.photos/f'label{i}.JPG';Image.new('RGB',(100,80),'white').save(path)
            record=media.capture_record(self.photos,path,None,{});self.store.append_live(self.sid,[record]);self.cids.append(record['id'])
        self.sku.scheduler=VisionScheduler(limit=1);release=[threading.Event() for _ in range(6)];calls=[];lock=threading.Lock()
        def recognize(*_):
            with lock:index=len(calls);calls.append(index)
            release[index].wait(8);return sku_fixture.reading()
        self.sku.recognizer=recognize;self.sku.enqueue_parses(self.sid,self.cids[:5]);self.sku.thread.start()
        try:
            wait_for(lambda:len(calls)==4)
            self.assertEqual(self.sku.scheduler.state()['active'],4)
            self.rate(2,self.cids[5]);release[0].set();wait_for(lambda:len(calls)==5)
            with self.sku.connect() as c:
                self.assertEqual(c.execute('SELECT status FROM jobs WHERE capture_id=?',(self.cids[5],)).fetchone()[0],'running')
                self.assertEqual(c.execute('SELECT status FROM parse_jobs WHERE capture_id=?',(self.cids[4],)).fetchone()[0],'queued')
        finally:
            for event in release:event.set()
            self.sku.close()

    def test_cancel_retry_read_drops_previous_attempt_when_response_arrives_late(self):
        started=threading.Event();release=threading.Event();calls=[];lock=threading.Lock()
        self.sku.require_sku_choice=True
        def recognize(*_):
            with lock:index=len(calls);calls.append(index)
            if index==0:started.set();release.wait(8);return sku_fixture.reading('OLD-111')
            return sku_fixture.reading('NEW-222')
        self.sku.recognizer=recognize;self.sku.enqueue_parses(self.sid,[self.cids[0]]);self.sku.thread.start()
        def job():
            with self.sku.connect() as c:return dict(c.execute('SELECT * FROM parse_jobs').fetchone())
        try:
            self.assertTrue(started.wait(2));old=job();jid='read:'+old['id']
            self.sku.configure_tasks(action='cancel',job_id=jid);self.sku.configure_tasks(action='retry',job_id=jid)
            wait_for(lambda:job()['status']=='completed');self.assertNotEqual(job()['run_token'],old['run_token'])
            release.set();wait_for(lambda:not self.sku.label_workers)
            self.assertIn('NEW-222',self.sku.ocr_details(self.sid,self.cids[0])['text'])
            self.assertNotIn('OLD-111',self.sku.ocr_details(self.sid,self.cids[0])['text'])
        finally:release.set();self.sku.close()


class AssetCardThroughputTests(unittest.TestCase):
    setUp=archive_fixture.AssetTests.setUp
    tearDown=archive_fixture.AssetTests.tearDown
    add=archive_fixture.AssetTests.add
    def test_multimodal_uses_card_four_even_with_global_local_limit_one(self):
        p=Providers(self.store.data);pid=p.save({'name':'vision','category':'multimodal','protocol':'chat','url':'http://127.0.0.1:1234/v1','model':'test','concurrency':4})['providers'][0]['id'];p.bind('asset_analysis',pid)
        analysis=AssetAnalysis(self.archive,p,VisionScheduler(limit=1),autostart=False)
        assets=[self.archive.ensure(self.sid,self.add(str(i)+'.JPG')) for i in range(4)]
        schema=analysis.save_schema('color','return color',{'type':'object','properties':{'color':{'type':'string'}},'required':['color']})['schemas'][0]
        analysis.create_batch(assets,schema['id'],schema['version']);analysis.scheduler.configure(1)
        release=threading.Event();calls=[];lock=threading.Lock()
        def vision(*_):
            with lock:calls.append(1)
            release.wait(8);return '{"color":"blue"}'
        with patch('asset_analysis.vision',side_effect=vision):
            for thread in tuple(analysis.threads):thread.start()
            try:
                wait_for(lambda:len(calls)==4,timeout=6);self.assertEqual(p.inference_active[pid],4)
                release.set();wait_for(lambda:analysis.state()['counts'].get('completed')==4,timeout=6)
                p.save(dict(p.by_id(pid),concurrency=2));analysis.create_batch(assets,schema['id'],schema['version'])
                self.assertEqual(len(analysis.state()['jobs']),4,'调整并发数不能使相同图片重新解析')
            finally:release.set();analysis.close()


class OcrMetadataConcurrencyTests(unittest.TestCase):
    setUp=ocr_fixture.OcrMetadataTests.setUp
    tearDown=ocr_fixture.OcrMetadataTests.tearDown
    def test_managed_metadata_versions_support_unicode_photo_paths(self):
        path=self.photos/'中文标签.JPG';Image.new('RGB',(160,120),'white').save(path)
        record=media.capture_record(self.photos,path,None,{});self.store.append_live(self.sid,[record])
        self.archive.ratings.seed_session(self.sid)
        def recognize(*_):
            self.archive.ratings.set([str(path)],2)
            while self.archive.ratings.sync.process_one():pass
            return self.reading
        self.sku.recognizer=recognize
        self.assertEqual(self.sku.parse_capture(self.sid,record['id'])['reading']['text'],self.reading['text'])
    def test_rating_metadata_write_during_api_call_keeps_reading_but_pixel_change_does_not(self):
        cid=self.ids[1];path=self.photos/'1.JPG'
        self.archive.ratings.seed_session(self.sid)  # 与真实引擎启动一致，登记评级与照片身份的关联。
        def recognize(*_):
            self.archive.ratings.set([str(path)],2)
            while self.archive.ratings.sync.process_one():pass
            return self.reading
        self.sku.recognizer=recognize
        self.assertEqual(self.sku.parse_capture(self.sid,cid)['reading']['text'],self.reading['text'])
        def replace_pixels(*_):Image.new('RGB',(160,120),'red').save(path);return self.reading
        self.sku.recognizer=replace_pixels
        with self.assertRaisesRegex(ValueError,'照片发生变化'):self.sku.parse_capture(self.sid,cid)


if __name__=='__main__':unittest.main()
