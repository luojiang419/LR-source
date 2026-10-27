import copy
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import test_sku_workflow as fixture


class InstantOcrTests(unittest.TestCase):
    setUp=fixture.SkuWorkflowTests.setUp
    tearDown=fixture.SkuWorkflowTests.tearDown
    rate=fixture.SkuWorkflowTests.rate

    def test_new_two_star_runs_without_advancing_clock_and_deduplicates(self):
        self.rate(2)
        with self.sku.connect() as c:job=dict(c.execute('SELECT * FROM jobs').fetchone())
        self.assertEqual(job['due'],self.clock[0]);self.assertTrue(self.sku.wake.is_set())
        self.sku.tick();self.assertEqual(len(self.calls),1)
        self.rate(2);self.sku.tick();self.assertEqual(len(self.calls),1)

    def test_first_two_star_without_imported_document_creates_context_and_runs(self):
        with self.sku.connect() as c:
            c.execute('DELETE FROM sku_rows');c.execute('DELETE FROM projects')
            c.execute("DELETE FROM settings WHERE key='active'")
        self.rate(2)
        with self.sku.connect() as c:job=dict(c.execute('SELECT * FROM jobs').fetchone())
        self.assertEqual(job['due'],self.clock[0]);self.assertTrue(self.sku.wake.is_set())
        self.sku.tick();self.assertEqual(len(self.calls),1)
        self.assertEqual(self.sku.project()['scope'],'现场记录')

    def test_two_star_survives_concurrent_xmp_fingerprint_refresh(self):
        self.rate(2)
        record=self.store.records(self.sid)[0];before=record['fingerprint']
        record['fingerprint']='rating-metadata-version'
        self.sku.metadata_changed(self.sid,record['id'],before,record['fingerprint'])
        import json
        with self.store.connect() as c:
            c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(json.dumps(record),self.sid,record['id']))
        self.sku.tick();self.assertEqual(len(self.calls),1)
        self.assertEqual(self.sku.state()['jobs'][0]['status'],'matched')

    def test_cancel_before_execution_and_during_inference(self):
        self.rate(2);self.rate(0);self.sku.tick();self.assertEqual(self.calls,[])
        self.rate(2)
        def recognize(*_):self.rate(0);return fixture.reading()
        self.sku.recognizer=recognize;self.sku.tick()
        self.assertEqual(self.sku.state()['jobs'][0]['status'],'cancelled')

    def test_new_label_precedes_bulk_parse_and_older_label(self):
        self.rate(2,self.cids[0]);self.clock[0]+=.01
        self.sku.enqueue_parses(self.sid,[self.cids[0]])
        self.rate(2,self.cids[1]);self.sku.tick()
        with self.sku.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM jobs WHERE capture_id=?',(self.cids[1],)).fetchone()[0],'matched')
            self.assertEqual(c.execute('SELECT status FROM jobs WHERE capture_id=?',(self.cids[0],)).fetchone()[0],'queued')
            self.assertEqual(c.execute('SELECT status FROM parse_jobs').fetchone()[0],'queued')

    def test_pause_and_missing_model_are_preserved(self):
        self.sku.configure_tasks(paused=True);self.rate(2);self.sku.tick();self.assertFalse(self.calls)
        with self.providers.lock:self.providers.data['selected']={}
        self.sku.configure_tasks(paused=False);self.sku.tick()
        self.assertEqual(self.sku.state()['jobs'][0]['status'],'waiting_model')
        self.assertFalse(self.calls)

    def test_full_auto_existing_and_new_label_are_ready_now(self):
        self.sku.set_full_auto(True,self.sid);self.sku._queue_full_auto()
        self.rate(2,self.cids[-1]);self.sku.tick()
        with self.sku.connect() as c:
            job=dict(c.execute('SELECT * FROM jobs WHERE capture_id=?',(self.cids[-1],)).fetchone())
            self.assertEqual(job['status'],'review');self.assertIn('资产入库服务未启动',job['reason'])
        self.assertEqual(len(self.calls),1)

    def test_full_auto_before_scan_and_ordinary_manual_mode(self):
        self.sku.configure_tasks(mode='manual');self.sku.set_full_auto(True,self.sid)
        self.rate(2,self.cids[-1]);self.sku.tick()
        with self.sku.connect() as c:
            job=dict(c.execute('SELECT * FROM jobs WHERE capture_id=?',(self.cids[-1],)).fetchone())
            self.assertEqual(job['status'],'review');self.assertIn('资产入库服务未启动',job['reason'])
        self.assertEqual(len(self.calls),1)

    def test_live_worker_runs_new_label_before_older_request_finishes(self):
        started=threading.Event();release=threading.Event();second=threading.Event();seen=[]
        def recognize(*_):
            seen.append(1)
            if len(seen)==1:started.set();release.wait(5)
            else:second.set()
            return fixture.reading()
        self.sku.recognizer=recognize;self.sku.thread.start()
        try:
            self.rate(2);self.assertTrue(started.wait(2))
            self.rate(2,self.cids[-1]);self.assertTrue(second.wait(2))
            self.assertFalse(release.is_set());self.assertEqual(len(seen),2)
        finally:release.set();self.sku.close()

    def test_browser_click_keyboard_and_remote_do_not_wait_for_save_timer(self):
        import server
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=None,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        page.locator('[data-watch-photo="'+self.cids[0]+'"]').click()
                        page.evaluate('()=>FramekeepIO.flush()')
                        page.clock.install();page.clock.pause_at(int(time.time()*1000))
                        for method in ('click','keyboard','remote'):
                            with page.expect_response(lambda r:r.url.endswith('/api/session') and r.request.method=='POST',timeout=2500) as response:
                                if method=='click':page.locator('#stars [data-rating="2"]').click(force=True)
                                elif method=='keyboard':page.keyboard.press('2')
                                else:page.evaluate('(id)=>Framekeep.assignRemoteRating(id,2)',self.cids[0])
                            self.assertEqual(response.value.status,200)
                            with self.sku.connect() as c:job=dict(c.execute('SELECT * FROM jobs WHERE capture_id=?',(self.cids[0],)).fetchone())
                            self.assertEqual(job['due'],self.clock[0]);self.assertEqual(job['status'],'queued')
                            with page.expect_response(lambda r:r.url.endswith('/api/session') and r.request.method=='POST'):
                                page.keyboard.press('0')
                        self.assertEqual(errors,[])
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
