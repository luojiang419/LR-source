"""解析任务模式、持久化、目录自动触发及统一页面的隔离验收。"""
import copy
import hashlib
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from asset_analysis import AssetAnalysis
from catalog_store import Store
from model_providers import Providers
from sku_service import SkuService
from vision_scheduler import VisionScheduler
from test_sku_workflow import fixture_book,reading
from test_asset_analysis import LocalVision
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]

class ParsingTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos);self.store.activate(self.sid)
        self.providers=Providers(self.store.data);self.calls=[]
        self.pid=self.providers.save({'name':'local vision','category':'multimodal','protocol':'chat','url':'http://127.0.0.1:1234/v1','model':'test'})['providers'][0]['id']
        self.providers.select(self.pid);self.providers.bind('asset_analysis',self.pid)
        self.archive=AssetArchive(self.store,autostart=False);self.scheduler=VisionScheduler()
        self.sku=SkuService(self.store,self.providers,recognizer=self.recognize,scheduler=self.scheduler,autostart=False);self.sku.DEBOUNCE=0
        self.analysis=AssetAnalysis(self.archive,self.providers,self.scheduler,autostart=False)
        self.store.on_catalog=self.analysis.request_auto
        self.schema=self.analysis.save_schema('颜色','返回 color',{'type':'object','properties':{'color':{'type':'string'}},'required':['color']})['schemas'][0]
    def recognize(self,provider,image):self.calls.append(provider['id']);return reading()
    def add(self,name):
        p=self.photos/name;Image.new('RGB',(160,120),(70,120,len(name)*10)).save(p)
        record=media.capture_record(self.photos,p,None,{})
        self.store.append_live(self.sid,[record]);return record['id']
    def tearDown(self):
        self.sku.close();self.analysis.close();self.archive.close();self.temp.cleanup()
    def rate(self,cid,value):
        s=self.store.session(self.sid);state=copy.deepcopy(s['state']);state.setdefault('overrides',{})[cid]=value
        self.store.save(self.sid,s['revision'],state);self.sku.on_save(self.sid,s['state'],state)

    def test_ocr_modes_pending_manual_reads_restart_and_no_photo_writes(self):
        cid=self.add('label.JPG');before=hashlib.sha256((self.photos/'label.JPG').read_bytes()).hexdigest()
        pid=self.sku.import_file('plan.xlsx',fixture_book(),self.sid)['project']['id'];self.sku.configure(pid,self.sid,'模特图')
        self.rate(cid,2);self.sku.configure_tasks(mode='manual');self.sku.tick();self.assertFalse(self.calls)
        self.sku.enqueue_parses(self.sid,[cid,cid]);self.sku.tick();self.assertEqual(len(self.calls),1)
        state=self.sku.task_state();self.assertEqual(state['counts']['completed'],1);self.assertEqual(state['counts']['queued'],1)
        self.assertEqual(self.store.session(self.sid)['state']['overrides'][cid],2)
        self.sku.close();self.sku=SkuService(self.store,self.providers,recognizer=self.recognize,autostart=False)
        self.assertEqual(self.sku.task_mode,'manual');self.sku.tick();self.assertEqual(len(self.calls),1)
        self.sku.configure_tasks(mode='auto',paused=True);self.sku.tick();self.assertEqual(len(self.calls),1)
        self.sku.configure_tasks(paused=False);self.sku.tick();self.assertEqual(len(self.calls),2)
        self.sku.configure_tasks(mode='manual');other=self.add('other.JPG');self.rate(other,2)
        self.assertEqual(len(self.sku.task_state()['jobs']),2)
        self.sku.enqueue(self.sid,other);self.sku.DEBOUNCE=0
        with self.sku.connect() as c:c.execute('UPDATE jobs SET due=0')
        self.sku.tick();self.assertEqual(len(self.calls),3)
        self.assertEqual(hashlib.sha256((self.photos/'label.JPG').read_bytes()).hexdigest(),before)

    def test_manual_ocr_failure_cancel_retry_and_existing_entry_tracking(self):
        cid=self.add('label.JPG');self.sku.enqueue_parses(self.sid,[cid]);jid=self.sku.task_state()['jobs'][0]['id']
        self.sku.configure_tasks(action='cancel',job_id=jid);self.sku.tick();self.assertFalse(self.calls)
        self.sku.configure_tasks(action='retry',job_id=jid)
        with patch.object(self.sku,'recognizer',side_effect=ValueError('模拟 OCR 故障')):self.sku.tick()
        self.assertEqual(self.sku.task_state()['jobs'][0]['status'],'failed')
        self.sku.configure_tasks(action='retry',job_id=jid);self.sku.tick()
        self.assertEqual(self.sku.task_state()['counts']['completed'],1)
        self.sku.parse_capture(self.sid,cid);self.assertEqual(self.sku.task_state()['counts']['completed'],2)
        self.assertEqual(self.store.session(self.sid)['state'].get('overrides',{}),{})

    def test_multimodal_future_only_dedupe_mode_and_restart(self):
        old=self.add('old.JPG');self.analysis.configure_auto(True,self.sid,self.schema['id'],self.schema['version'])
        self.analysis.request_auto(self.sid);self.analysis.auto_tick();self.assertFalse(self.analysis.state()['jobs'])
        fresh=self.add('new.JPG');self.analysis.auto_tick();self.assertEqual(len(self.analysis.state()['jobs']),1)
        self.analysis.request_auto(self.sid);self.analysis.auto_tick();self.assertEqual(len(self.analysis.state()['jobs']),1)
        self.analysis.configure_auto(False)
        with patch('asset_analysis.vision',return_value='{"color":"blue"}') as model:
            self.analysis.tick();model.assert_not_called()
        offline=self.add('while-manual.JPG');self.analysis.close();self.analysis=AssetAnalysis(self.archive,self.providers,self.scheduler,autostart=False)
        self.assertFalse(self.analysis.auto['enabled'])
        self.analysis.configure_auto(True,self.sid,self.schema['id'],1);self.analysis.auto_tick()
        with patch('asset_analysis.vision',return_value='{"color":"blue"}'):self.analysis.tick()
        self.assertEqual(self.analysis.state()['counts']['completed'],1)
        self.assertEqual(len(self.analysis.state()['jobs']),1)
        self.analysis.close();self.analysis=AssetAnalysis(self.archive,self.providers,self.scheduler,autostart=False)
        self.assertTrue(self.analysis.auto['enabled']);self.add('after-restart.JPG');self.analysis.auto_tick()
        self.assertEqual(len(self.analysis.state()['jobs']),2)
        self.assertFalse(list(self.photos.glob('*.xmp')))

    def test_auto_preparation_errors_and_validation_are_visible(self):
        with self.assertRaisesRegex(ValueError,'目录'):self.analysis.configure_auto(True,None,self.schema['id'],1)
        self.analysis.configure_auto(True,self.sid,self.schema['id'],1);cid=self.add('offline.JPG');(self.photos/'offline.JPG').unlink()
        self.analysis.auto_tick();state=self.analysis.state();self.assertEqual(state['counts']['failed'],1)
        self.assertTrue(state['jobs'][0]['id'].startswith('auto:'));self.assertTrue(state['jobs'][0]['reason'])
        self.analysis.control(action='cancel',job_id=state['jobs'][0]['id']);self.assertFalse(self.analysis.state()['jobs'])

    def test_stop_or_cancel_during_preparation_does_not_send_model_request(self):
        self.analysis.configure_auto(True,self.sid,self.schema['id'],1);cid=self.add('cancel.JPG')
        original=self.analysis.image
        def cancel(aid,size):
            self.analysis.control(action='cancel',job_id=f'auto:{self.sid}:{cid}')
            return original(aid,size)
        with patch.object(self.analysis,'image',side_effect=cancel):self.analysis.auto_tick()
        self.assertFalse(self.analysis.state()['jobs'])
        self.add('held.JPG');self.analysis.auto_tick()
        def stop(aid,size):
            self.analysis.configure_auto(False)
            return original(aid,size)
        with patch.object(self.analysis,'image',side_effect=stop),patch('asset_analysis.vision') as model:
            self.analysis.tick();model.assert_not_called()
        self.assertEqual(self.analysis.state()['counts']['queued'],1)

    def test_unified_task_page_manual_auto_progress_filters_and_reload(self):
        import server
        upstream=ThreadingHTTPServer(('127.0.0.1',0),LocalVision);LocalVision.calls=[];LocalVision.invalid=False
        threading.Thread(target=upstream.serve_forever,daemon=True).start()
        self.providers.save({'id':self.pid,'name':'local vision','category':'multimodal','protocol':'chat','url':f'http://127.0.0.1:{upstream.server_port}/v1','model':'test'})
        self.add('first.JPG');self.analysis.threads[0].start();self.analysis.auto_thread.start();self.sku.thread.start()
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values={'STORE':self.store,'DATA':self.store.data,'MODELS':self.providers,'ARCHIVE':self.archive,'ANALYSIS':self.analysis,'SKU':self.sku,'WATCH':None,'MOUSE':None,'RAWLINK':None,'FILESEARCH':None}
        patches=[patch.object(server,k,v) for k,v in values.items()]
        for p in patches:p.start()
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#asset-analysis-open').click()
                self.assertEqual(page.locator('#asset-analysis-open').inner_text(),'解析任务')
                page.wait_for_function('()=>document.querySelectorAll("#analysis-schema option").length===2');page.locator('#analysis-schema').select_option(self.schema['id']+':1')
                page.locator('#parsing-ocr-mode').select_option('manual');page.wait_for_function('()=>document.getElementById("analysis-message").textContent.includes("已保存")')
                page.locator('#parsing-ocr-pause').click();page.wait_for_function('()=>document.getElementById("parsing-ocr-state").textContent==="已暂停"')
                page.locator('#parsing-ocr-run').click();page.wait_for_selector('[data-parsing-channel="ocr"]')
                self.assertFalse(self.calls);page.locator('#parsing-ocr-resume').click()
                page.wait_for_function('()=>document.querySelector("#analysis-jobs").textContent.includes("已完成")')
                page.locator('#parsing-multimodal-mode').select_option('auto');page.wait_for_function('()=>document.getElementById("parsing-auto-scope").textContent.includes("自动目录")')
                self.add('second.JPG');page.wait_for_selector('[data-parsing-channel="multimodal"]')
                page.wait_for_function('()=>document.getElementById("analysis-stats").textContent.includes("已完成 1")')
                page.locator('#parsing-kind').select_option('ocr');self.assertEqual(page.locator('[data-parsing-channel="multimodal"]').count(),0)
                page.locator('#parsing-kind').select_option('all');page.locator('#analysis-jobs details summary').first.click()
                page.wait_for_timeout(1350);self.assertTrue(page.locator('#analysis-jobs details').first.evaluate('(e)=>e.open'))
                for theme in ('dark','light','pure-white','hardcore'):
                    page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                    for width in (1680,1024,375,320):
                        page.set_viewport_size({'width':width,'height':1050});self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    page.set_viewport_size({'width':1680,'height':1050})
                    out=ROOT/'build/parsing-tasks';out.mkdir(exist_ok=True);page.screenshot(path=str(out/f'{theme}.png'))
                page.reload(wait_until='domcontentloaded');page.locator('#asset-analysis-open').click()
                page.wait_for_function('()=>document.getElementById("parsing-ocr-mode").value==="manual" && document.getElementById("parsing-multimodal-mode").value==="auto"')
                self.assertEqual(self.store.session(self.sid)['state'].get('overrides',{}),{});self.assertFalse(errors,errors)
                browser.close()
        finally:
            http.shutdown();http.server_close();upstream.shutdown();upstream.server_close()
            for p in reversed(patches):p.stop()

    def test_shared_task_list_compact_progress_and_auto_switch(self):
        """实际解析队列配合只读元数据快照，验证共享列表及切换，不写照片。"""
        import server
        cid=self.add('shared.JPG')
        batch={'id':'ui-batch','folder':str(self.base/'archive'),'updated':1,'status':'completed','total':2,'finished':2,
               'counts':{'applied':2},'verification':{'status':'completed','checked':2,'valid':2,'source_assets':1},
               'files':[{'name':'shared.'+ext,'path':str(self.base/'archive'/('shared.'+ext)),
                         'source_name':'shared.JPG','source_path':str(self.photos/'shared.JPG'),
                         'kind':kind,'format':fmt,'status':'applied','rating':2,'sku':'测试SKU','reason':''}
                        for ext,kind,fmt in [('JPG','jpg','JPG'),('CR3','raw','RAW')]]}
        snapshot={'batches':[batch]}
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values={'STORE':self.store,'DATA':self.store.data,'MODELS':self.providers,'ARCHIVE':self.archive,
                'ANALYSIS':self.analysis,'SKU':self.sku,'WATCH':None,'MOUSE':None,'FILESEARCH':None,
                'RAWLINK':SimpleNamespace(task_state=lambda:copy.deepcopy(snapshot))}
        patches=[patch.object(server,k,v) for k,v in values.items()]
        for p in patches:p.start()
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                page=browser.new_page(viewport={'width':1500,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#asset-analysis-open').click()
                def shown(kind):
                    page.wait_for_function('(kind)=>document.getElementById("metadata-tasks-card").dataset.kind===kind',arg=kind)
                    self.assertEqual(page.locator('#metadata-files').is_visible(),kind=='metadata')
                    self.assertEqual(page.locator('#analysis-jobs').is_visible(),kind!='metadata')
                shown('metadata')
                self.assertEqual(page.locator('#metadata-files tbody tr').count(),1)
                self.assertEqual(page.locator('.metadata-format-tag').inner_text(),'JPG/RAW')
                self.assertEqual(page.locator('#metadata-percent').inner_text(),'100%')
                panel=page.locator('#task-progress-panel').bounding_box();header=page.locator('#metadata-tasks-card>header').bounding_box()
                self.assertLessEqual(panel['height'],header['height']+10)
                self.assertEqual(page.locator('#task-progress-panel table, #task-progress-panel .workspace-empty').count(),0)
                self.sku.enqueue_parses(self.sid,[cid]);shown('ocr')
                self.assertEqual(page.locator('#metadata-percent').inner_text(),'0%')
                page.locator('#parsing-kind').select_option('metadata');shown('metadata');page.wait_for_timeout(1350);shown('metadata')
                page.locator('#parsing-kind').select_option('auto');shown('ocr')
                batch.update(status='writing',finished=1,counts={'applied':1,'running':1},updated=time.time())
                batch['verification']={};batch['files'][1]['status']='running'
                page.evaluate('FramekeepMetadataTasks.submitted("ui-batch")');shown('metadata')
                self.assertEqual(page.locator('#metadata-percent').inner_text(),'25%')
                self.assertEqual(page.locator('#metadata-progress').get_attribute('data-active'),'true')
                batch.update(status='completed',finished=2,counts={'applied':2})
                batch['verification']={'status':'completed','checked':2,'valid':2,'source_assets':1};batch['files'][1]['status']='applied'
                shown('ocr');self.sku.tick()
                page.wait_for_function('()=>document.getElementById("metadata-percent").textContent==="100%"')
                self.analysis.configure_auto(True,self.sid,self.schema['id'],1);self.add('background.JPG');self.analysis.auto_tick();shown('multimodal')
                page.locator('#parsing-kind').select_option('metadata');shown('metadata');page.wait_for_timeout(1350);shown('metadata')
                out=ROOT/'build/shared-task-progress';out.mkdir(exist_ok=True)
                for theme in ('dark','light','pure-white','hardcore'):
                    page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                    for width in (1500,1024,375,320):
                        page.set_viewport_size({'width':width,'height':1000})
                        self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    page.set_viewport_size({'width':1500,'height':1000});page.screenshot(path=str(out/f'{theme}.png'))
                empty={'analysis':{'schemas':[],'jobs':[],'counts':{}},'ocr':{'mode':'auto','jobs':[],'counts':{}},'metadata':{'batches':[]}}
                page.route('**/api/parsing',lambda route:route.fulfill(json=empty))
                page.reload(wait_until='domcontentloaded');page.locator('#asset-analysis-open').click()
                shown('all');self.assertEqual(page.locator('#analysis-jobs').inner_text(),'')
                self.assertNotIn('暂无符合条件',page.locator('#analysis-dialog').inner_text())
                self.assertEqual(page.locator('#metadata-percent').inner_text(),'0%')
                self.assertFalse(errors,errors);browser.close()
        finally:
            http.shutdown();http.server_close()
            for p in reversed(patches):p.stop()
