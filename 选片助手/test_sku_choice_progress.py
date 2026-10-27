"""候选应用的事务、故障恢复、拍摄证据与真实 HTTP/UI 验收。"""
import copy
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import server
import test_ocr_metadata as fixtures


class SkuChoiceProgressTests(unittest.TestCase):
    setUp=fixtures.OcrMetadataTests.setUp
    tearDown=fixtures.OcrMetadataTests.tearDown
    parse=fixtures.OcrMetadataTests.parse
    choose=fixtures.OcrMetadataTests.choose
    code='LC7878091'

    def row(self):return next(r for r in self.sku.state()['rows'] if r['sku']==self.code)

    def reject_confirmation(self):
        with self.sku.connect() as c:
            c.execute("CREATE TRIGGER reject_test_choice BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT,'injected confirmation failure'); END")

    def accept_confirmation(self):
        with self.sku.connect() as c:c.execute('DROP TRIGGER reject_test_choice')

    def assert_completed(self):
        row=self.row();self.assertEqual(row['status'],'已拍摄');self.assertTrue(row['sku_image'])
        self.assertIn(row['sku_image']['capture_id'],self.ids[:2])
        self.assertEqual(row['completed_at'],'2026-10-21T10:00:01')
        self.assertEqual(row['completion']['capture_id'],self.ids[1])
        return row

    def test_failed_confirmation_rolls_back_row_metadata_choice_and_event_then_retry_succeeds(self):
        self.parse();before=copy.deepcopy(self.sku.project()['metadata']);self.reject_confirmation()
        with self.sku.connect() as c:events=c.execute('SELECT count(*) FROM archive_events').fetchone()[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError,'injected'):self.choose(self.code)
        self.assertFalse([r for r in self.sku.state()['rows'] if r['sku']==self.code])
        self.assertEqual(self.sku.project()['metadata'],before)
        with self.sku.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM ocr_choices').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM archive_events').fetchone()[0],events)
        self.accept_confirmation();self.assertTrue(self.choose(self.code)['saved']);self.assert_completed()

    def test_previous_orphan_row_is_reused_and_filled_without_duplicate(self):
        self.parse()
        job={'project':self.pid,'session_id':self.sid,'capture_id':self.ids[1],'scope':'模特图'}
        result=self.sku._record_unplanned(job,[{'codes':[self.code],'unscored':True}],{'status':'review','row_ids':[]},accept_observed=True)
        rid=result['row_ids'][0];self.assertEqual(self.row()['status'],'未拍摄')
        self.choose(self.code);row=self.assert_completed();self.assertEqual(row['id'],rid)
        self.choose(self.code);self.assert_completed()
        self.assertEqual(len([r for r in self.sku.state()['rows'] if r['sku']==self.code]),1)

    def test_export_failure_keeps_committed_image_time_and_queues_retry(self):
        self.parse()
        with patch.object(self.sku,'export',side_effect=PermissionError('workbook is open')):
            result=self.choose(self.code)
        self.assertTrue(result['saved']);self.assertTrue(result['warnings']);self.assertIn(self.pid,self.sku.dirty)
        self.assert_completed()
        saved=json.loads((self.store.data/'last-operation-error.json').read_text(encoding='utf8'))
        self.assertEqual(saved['operation'],'sku-choice-export');self.assertIn('workbook is open',saved['traceback'])
        self.sku.export(self.pid);self.assert_completed()

    def test_archive_delivery_failure_keeps_progress_and_replays_durable_event(self):
        self.parse()
        with patch.object(self.sku,'deliver_events',side_effect=OSError('archive temporarily locked')):
            result=self.choose(self.code)
        self.assertTrue(result['saved']);self.assertTrue(result['warnings']);self.assert_completed()
        self.assertEqual(self.archive.query(self.code)['total'],0)
        self.sku.deliver_events(self.archive)
        self.assertEqual(self.archive.query(self.code)['total'],2);self.assert_completed()

    def test_browser_failure_is_logged_and_retry_refreshes_progress_without_polling(self):
        self.reading['codes']=[self.code];self.reading['ocr_candidates']=[self.code];self.reading['text']=self.code
        self.parse();http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepBoot?.loading===false')
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',self.ids[1]);page.evaluate('()=>FramekeepIO.flush()')
                        page.locator('.asset-panel').evaluate('e=>e.open=true')
                        page.locator('#asset-parse-sku').click()
                        page.wait_for_function('()=>!document.getElementById("asset-parse-sku").disabled&&document.querySelector("#asset-ocr-codes [data-ocr-code]")')
                        # 普通页面的候选点击应用；返回故障详情且不产生空进度行。
                        self.reject_confirmation()
                        details=self.sku.ocr_details(self.sid,self.ids[1])
                        payload=dict(session_id=self.sid,capture_id=self.ids[1],sku=self.code,reading_key=details['reading_key'],image_key=details['image_key'])
                        button=page.locator('#asset-ocr-codes [data-ocr-code="'+self.code+'"]')
                        with page.expect_response('**/api/sku/ocr/choose') as response:button.click()
                        result=response.value.json()
                        self.assertEqual(response.value.status,500);self.assertIn('error_id',result)
                        self.assertFalse([r for r in self.sku.state()['rows'] if r['sku']==self.code])
                        saved=json.loads((self.store.data/'last-operation-error.json').read_text(encoding='utf8'))
                        self.assertEqual(saved['id'],result['error_id']);self.assertIn('injected confirmation failure',saved['traceback'])
                        self.accept_confirmation()
                        # 冻结轮询后，真实候选应用仍应主动刷新拍摄进度。
                        page.clock.install();page.clock.pause_at(1_793_000_000_000)
                        with page.expect_response('**/api/sku/ocr/choose') as response:button.click()
                        result=response.value.json()
                        self.assertTrue(result['saved']);self.assert_completed()
                        page.clock.run_for(100)
                        page.wait_for_function('()=>document.getElementById("sku-panel-scope").textContent.includes("临时插拍")',timeout=5000)
                        page.locator('#sku-open').click();page.locator('#sku-search').fill(self.code)
                        page.locator('[data-progress-sheet="临时插拍"]').click()
                        page.wait_for_function('(code)=>[...document.querySelectorAll("#sku-rows tbody tr")].some(r=>r.textContent.includes(code)&&r.textContent.includes("已拍摄")&&r.querySelector("[data-sku-cover] img"))',arg=self.code)
                        out=fixtures.ROOT/'build/sku-choice-progress';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'source-progress.png'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
