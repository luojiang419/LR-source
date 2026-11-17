"""慢随行档案发布期间，现场与归档二星必须先本机落库、返回并开始解析。"""
from contextlib import ExitStack
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import archive_capture_service as capture
import media_engine as media
import portable_capture_store as portable
import test_archive_capture_service as fixture


class TwoStarSubmitLatencyTests(unittest.TestCase):
    setUp=fixture.ArchiveCaptureTests.setUp
    tearDown=fixture.ArchiveCaptureTests.tearDown
    client=fixture.ArchiveCaptureTests.client
    item=fixture.ArchiveCaptureTests.item

    def background_service(self):
        self.api.close()
        # 只暂停目录巡检；发布线程应独立于目录巡检和OCR调度运行。
        with patch.object(capture.ArchiveCaptureService,'_run',lambda service:service.closed.wait()):
            self.api=capture.ArchiveCaptureService(self.store,self.archive,self.catalog,self.sku,autostart=True)
        self.handles[-1]=(self.api,self.sku,self.catalog,self.archive)
        self.dataset,_=self.api.scope(self.rid)
        self.api.edit(self.rid)
        self.api._scan(self.dataset)

    def delayed_publish(self,action,verify=None):
        entered=threading.Event();release=threading.Event();done=threading.Event();errors=[]
        publish=portable.publish_file
        def slow(path,content):
            if path.parent.name=='events':
                entered.set()
                if not release.wait(12):raise TimeoutError('测试发布未解除')
            return publish(path,content)
        def submit():
            try:action()
            except BaseException as exc:errors.append(exc)
            finally:done.set()
        # 确保持有发布槽的线程没有占据本机业务锁。
        with self.dataset.deferred_publish():self.dataset.change(kind='latency_probe')
        with patch.object(portable,'publish_file',side_effect=slow):
            publisher=threading.Thread(target=self.dataset.publish);publisher.start()
            request=None
            try:
                self.assertTrue(entered.wait(3))
                request=threading.Thread(target=submit);started=time.monotonic();request.start()
                self.assertTrue(done.wait(1.5),'二星提交仍在等待随行档案发布，尚未返回/入队')
                self.assertFalse(release.is_set())
                self.assertFalse(errors,errors)
                if verify:verify()
                elapsed=time.monotonic()-started
                self.assertLess(elapsed,1.5)
                return elapsed
            finally:
                release.set();publisher.join(8)
                if request:request.join(8)

    def test_archive_rating_returns_and_ocr_can_run_before_slow_publish_finishes(self):
        self.background_service()
        def verify():
            for _ in range(6):
                self.api.tick()
                if self.dataset.record(path='IMG_0002.JPG').get('ocr'):return
            self.fail('档案仍在发布时OCR没有接续执行')
        elapsed=self.delayed_publish(lambda:self.api.rate(self.item(2),2,root_id=self.rid),verify)
        self.assertEqual(self.dataset.record(path='IMG_0002.JPG')['rating'],2)
        self.assertTrue(self.dataset.record(path='IMG_0002.JPG').get('ocr'))
        self.assertLess(elapsed,1.5)

    def test_live_rating_returns_and_ocr_queues_before_slow_publish_finishes(self):
        self.background_service()
        sid=self.store.ensure_live_session(self.photos);self.store.activate(sid)
        path=self.photos/'IMG_0002.JPG'
        record=media.capture_record(self.photos,path,None,{'Model':'TestCam','SerialNumber':'1234567','DateTimeOriginal':'2026:11:09 12:00:02'})
        self.store.append_live(sid,[record]);session=self.store.session(sid)
        state={**session['state'],'overrides':{record['id']:2}}
        def save():
            self.store.save(sid,session['revision'],state)
            self.archive.ratings.saved(sid,session,state)
            self.sku.on_save(sid,session['state'],state)
        def verify():
            with self.sku.connect() as c:
                self.assertEqual(c.execute('SELECT status FROM jobs WHERE capture_id=?',(record['id'],)).fetchone()[0],'queued')
            self.sku.tick()
            with self.sku.connect() as c:
                self.assertNotEqual(c.execute('SELECT status FROM jobs WHERE capture_id=?',(record['id'],)).fetchone()[0],'queued')
        self.delayed_publish(save,verify)
        with self.sku.connect() as c:
            self.assertNotEqual(c.execute('SELECT status FROM jobs WHERE capture_id=?',(record['id'],)).fetchone()[0],'queued')

    def test_publish_failure_preserves_events_and_recovers_without_resubmitting_rating(self):
        self.background_service();publish=portable.publish_file;failed=threading.Event();recover=threading.Event()
        def unavailable(path,content):
            if path.parent.name=='events' and not recover.is_set():
                failed.set();raise OSError('隔离模拟NAS暂不可用')
            return publish(path,content)
        with patch.object(portable,'publish_file',side_effect=unavailable):
            self.api.rate(self.item(2),2,root_id=self.rid)
            self.assertTrue(failed.wait(3))
            with self.dataset.connect() as c:self.assertTrue(c.execute("SELECT 1 FROM events WHERE status='pending'").fetchone())
            self.assertEqual(self.dataset.record(path='IMG_0002.JPG')['rating'],2)
            recover.set();deadline=time.monotonic()+5
            while True:
                with self.dataset.connect() as c:pending=c.execute("SELECT 1 FROM events WHERE status='pending'").fetchone()
                if not pending:break
                self.assertLess(time.monotonic(),deadline,'发布恢复没有自动接续');time.sleep(.03)
            self.assertFalse(self.dataset.last_error)

    def test_browser_both_pages_leave_submitting_and_parse_while_publish_is_slow(self):
        import server
        from playwright.sync_api import sync_playwright
        self.background_service()
        sid=self.store.ensure_live_session(self.photos);self.store.activate(sid)
        records=[media.capture_record(self.photos,self.photos/f'IMG_{n:04d}.JPG',None,
                 {'Model':'TestCam','SerialNumber':'1234567','DateTimeOriginal':f'2026:11:09 12:00:0{n}'}) for n in range(1,5)]
        self.store.append_live(sid,records);self.archive.ratings.seed_session(sid)
        values=dict(STORE=self.store,CATALOG=self.catalog,ARCHIVE=self.archive,SKU=self.sku,MODELS=self.sku.providers,
                    DATA=self.store.data,WATCH=None,MOUSE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,
                    UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=None)
        output=Path(__file__).resolve().parents[1]/'build/task236-submit-latency';output.mkdir(parents=True,exist_ok=True)
        report={}
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000});errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.wait_for_function('()=>!window.FramekeepBoot?.loading&&window.FramekeepArchiveCapture')
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',records[0]['id']);page.evaluate('FramekeepIO.flush()')
                        for mode in ('live','archive'):
                            if mode=='archive':
                                page.evaluate('(id)=>FramekeepArchiveOpenItem(id)',self.item(2))
                                page.wait_for_function('''()=>!document.querySelector('[data-catalog-rating="2"]').disabled''')
                            entered=threading.Event();release=threading.Event();publish=portable.publish_file
                            def slow(path,content):
                                if path.parent.name=='events':
                                    entered.set()
                                    if not release.wait(15):raise TimeoutError('测试发布未解除')
                                return publish(path,content)
                            with self.dataset.deferred_publish():self.dataset.change(kind='browser_latency_probe')
                            with patch.object(portable,'publish_file',side_effect=slow):
                                publisher=threading.Thread(target=self.dataset.publish);publisher.start()
                                try:
                                    self.assertTrue(entered.wait(3));started=time.monotonic()
                                    endpoint='/api/session' if mode=='live' else '/api/archive-capture/rating'
                                    with page.expect_response(lambda r:r.url.endswith(endpoint) and r.request.method=='POST',timeout=2500) as response:
                                        if mode=='live':page.keyboard.press('2')
                                        else:page.locator('[data-catalog-rating="2"]').click()
                                    self.assertEqual(response.value.status,200)
                                    elapsed=time.monotonic()-started;self.assertLess(elapsed,2.5)
                                    if mode=='live':
                                        self.sku.tick();page.evaluate("document.dispatchEvent(new CustomEvent('framekeep:ocr-refresh'))")
                                        feedback='#preview-ocr-feedback'
                                    else:
                                        for _ in range(6):
                                            self.api.tick()
                                            if self.dataset.record(path='IMG_0002.JPG').get('sku'):break
                                        page.evaluate('FramekeepArchiveCapture.refresh(true)');feedback='#capture-ocr-feedback'
                                    page.wait_for_function('(selector)=>{const n=document.querySelector(selector);return n&&!n.hidden&&n.dataset.phase==="completed"&&n.textContent.includes("LC123456")}',arg=feedback,timeout=5000)
                                    self.assertFalse(release.is_set())
                                    page.screenshot(path=str(output/(mode+'-parsed-during-slow-publish.png')))
                                    report[mode]={'submitSeconds':round(elapsed,3),'phase':page.locator(feedback).get_attribute('data-phase'),
                                                  'publishStillBlocked':True,'feedback':page.locator(feedback).inner_text()}
                                finally:release.set();publisher.join(8)
                        self.assertFalse(errors,errors);report['pageErrors']=errors
                        (output/'browser-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
