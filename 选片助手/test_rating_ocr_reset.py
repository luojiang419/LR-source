"""二星清零、重标的持久资料、整款归档及界面保存顺序回归。"""
import copy
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from ocr_metadata import matching_ocr
from sku_service import SkuService
import test_ocr_metadata as fixture
import server

ROOT=Path(__file__).resolve().parents[1]


class RatingOcrResetTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose

    def rate(self,value,cid=None):
        cid=cid or self.ids[1];session=self.store.session(self.sid)
        before=copy.deepcopy(session['state']);state=copy.deepcopy(before);state['overrides'][cid]=value
        self.store.save(self.sid,session['revision'],state);self.sku.on_save(self.sid,before,state)

    def own(self):
        return next(r for r in self.sku.state()['rows'] if r['sheet']=='模特图' and r['excel_row']==2)

    def test_clear_choice_search_and_progress_then_restar_restores_image_time_and_archive(self):
        self.parse();self.choose();before=self.own()
        self.assertEqual(before['status'],'已拍摄');self.assertTrue(before['sku_image']);self.assertTrue(before['completed_at'])
        self.assertEqual(self.archive.query(fixture.CODE)['total'],2)
        self.rate(0)
        row=self.own();self.assertEqual(row['status'],'未拍摄');self.assertEqual(row['completed_at'],'');self.assertIsNone(row['completion'])
        with self.sku.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM ocr_choices').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM ocr_documents').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM ocr_document_search').fetchone()[0],0)
        self.assertEqual(matching_ocr(self.sku,'COTTON'),[])
        self.sku.deliver_events(self.archive);self.assertEqual(self.archive.query(fixture.CODE)['total'],0)
        self.assertFalse(self.sku.ocr_details(self.sid,self.ids[1])['has_ocr'])
        calls=[];self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}])
        self.sku.recognizer=lambda *_:calls.append(1) or copy.deepcopy(self.reading)
        self.rate(2);self.sku.tick();row=self.own()
        self.assertEqual(len(calls),1);self.assertEqual(row['status'],'已拍摄')
        self.assertEqual(row['completed_at'],before['completed_at'])
        self.assertEqual(row['sku_image']['capture_id'],before['sku_image']['capture_id'])
        self.assertEqual(self.archive.query(fixture.CODE)['total'],2)

    def test_reset_is_not_rescanned_by_full_auto_after_restart(self):
        self.parse();self.choose();self.sku.set_full_auto(True,self.sid)
        self.rate(0);self.sku.deliver_events(self.archive)
        self.sku.close();self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False,require_sku_choice=True)
        self.archive.sku=self.sku;self.sku._queue_full_auto()
        with self.sku.connect() as c:
            self.assertTrue(all(r[0]=='cancelled' for r in c.execute('SELECT status FROM jobs WHERE capture_id=?',(self.ids[1],))))
        self.assertFalse(self.sku.ocr_details(self.sid,self.ids[1])['has_ocr'])
        self.rate(2)
        with self.sku.connect() as c:
            job=c.execute('SELECT * FROM jobs WHERE capture_id=? AND status=\'queued\'',(self.ids[1],)).fetchone()
            self.assertIsNotNone(job);self.assertEqual(json.loads(job['result']),{});self.assertEqual(json.loads(job['targets']),[])

    def test_restar_before_archive_worker_runs_does_not_reuse_old_choice(self):
        self.parse();self.choose();self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}])
        self.rate(0);self.rate(2);self.sku.tick()
        self.assertEqual(self.own()['status'],'已拍摄')
        self.assertEqual(self.archive.query(fixture.CODE)['total'],2)
        self.assertTrue(self.sku.ocr_details(self.sid,self.ids[1])['has_ocr'])

    def test_full_auto_restar_reads_changed_code_and_discards_previous_ocr(self):
        self.rate(0,self.ids[3])
        self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}]);self.sku.set_full_auto(True,self.sid)
        calls=[]
        def recognize(*_):calls.append(1);return copy.deepcopy(self.reading)
        self.sku.recognizer=recognize;self.rate(0);self.rate(2);self.sku.tick()
        self.assertEqual(len(calls),1);self.assertEqual(self.archive.query(fixture.CODE)['total'],2)
        self.rate(0);self.sku.deliver_events(self.archive)
        self.assertEqual(self.archive.query(fixture.CODE)['total'],0)
        self.reading.update(text='LC9999999-P123',codes=['LC9999999-P123'],ocr_candidates=['LC9999999-P123'])
        self.rate(2);self.sku.tick()
        self.assertEqual(len(calls),2);self.assertEqual(self.archive.query('LC9999999-P123')['total'],2)
        self.assertEqual(self.archive.query(fixture.CODE)['total'],0)
        details=self.sku.ocr_details(self.sid,self.ids[1])
        self.assertEqual(details['selected_sku'],'LC9999999-P123');self.assertNotIn(fixture.CODE,details['text'])

    def test_without_document_keeps_list_row_and_restores_photo_and_capture_time(self):
        with self.sku.connect() as c:
            c.execute('DELETE FROM sku_rows');c.execute('DELETE FROM projects');c.execute("DELETE FROM settings WHERE key='active'")
        self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}]);self.rate(0);self.rate(2);self.sku.tick()
        def row():return next(r for r in self.sku.state()['rows'] if r['sku']==fixture.CODE)
        before=row();self.assertTrue(before['sku_image']);self.assertEqual(before['status'],'已拍摄')
        self.rate(0);cleared=row()
        self.assertEqual(cleared['id'],before['id']);self.assertIsNone(cleared['sku_image']);self.assertEqual(cleared['completed_at'],'')
        self.rate(2);self.sku.tick();restored=row()
        self.assertEqual(restored['id'],before['id']);self.assertEqual(restored['sku_image']['capture_id'],before['sku_image']['capture_id'])
        self.assertEqual(restored['completed_at'],before['completed_at']);self.assertEqual(restored['status'],'已拍摄')

    def test_clear_only_own_evidence_and_cancelled_manual_read_cannot_return(self):
        self.parse();self.choose();self.sku.parse_capture(self.sid,self.ids[3]);self.choose(cid=self.ids[3])
        self.rate(0);self.assertEqual(self.own()['status'],'已拍摄')
        self.assertEqual(self.own()['completion']['capture_id'],self.ids[3])
        self.sku.deliver_events(self.archive)
        self.assertTrue(self.sku.ocr_details(self.sid,self.ids[3])['has_ocr'])
        self.rate(2);started=threading.Event();release=threading.Event()
        def recognize(*_):started.set();release.wait(5);return self.reading
        self.sku.recognizer=recognize
        thread=threading.Thread(target=lambda:self.sku.parse_capture(self.sid,self.ids[1]))
        thread.start()
        try:
            self.assertTrue(started.wait(2));self.rate(0);release.set();thread.join(5)
            with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM ocr_documents WHERE capture_id=?',(self.ids[1],)).fetchone()[0],0)
        finally:release.set();thread.join(5)

    def test_browser_preserves_fast_zero_two_and_refreshes_without_poll(self):
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',self.ids[1]);page.evaluate('()=>FramekeepIO.flush()')
                        self.parse();self.choose()
                        page.locator('#sku-open').click();page.wait_for_function('()=>document.querySelector("#sku-rows tbody tr")?.textContent.includes("已拍摄")')
                        page.locator('#selection-tab').click();page.clock.install();page.clock.pause_at(int(time.time()*1000))
                        seen=[]
                        def respond(route):
                            body=route.request.post_data_json
                            seen.append(body['state']['overrides'][self.ids[1]])
                            route.continue_()
                        page.route('**/api/session',respond)
                        # 同一 JS 事件循环快速操作；冻结轮询计时器验证保存完成事件触发刷新。
                        page.evaluate('''async(id)=>{Framekeep.assignRemoteRating(id,0);Framekeep.assignRemoteRating(id,2);await FramekeepIO.flush()}''',self.ids[1])
                        self.assertEqual(seen,[0,2])
                        page.locator('#sku-open').click();page.wait_for_function('()=>document.querySelector("#sku-rows tbody tr")?.textContent.includes("未拍摄")')
                        self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}]);self.sku.tick()
                        # 再次保存一个星级，主动刷新已恢复的图片与真实时间，不等待 1 秒轮询。
                        page.evaluate('''async(id)=>{Framekeep.assignRemoteRating(id,1);await FramekeepIO.flush()}''',self.ids[0])
                        page.wait_for_function('()=>document.querySelector("#sku-rows tbody tr")?.textContent.includes("已拍摄")&&document.querySelector("#sku-rows tbody [data-sku-cover] img")')
                        out=ROOT/'build/rating-ocr-reset';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'restored-progress.png'))
                        self.assertEqual(errors,[])
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
