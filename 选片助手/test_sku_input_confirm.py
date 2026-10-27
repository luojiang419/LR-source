"""侧栏手动确定：未识别、已确认与在途旧 OCR 均按最新输入更新完整区间。"""
from contextlib import ExitStack,contextmanager
from http.server import ThreadingHTTPServer
import copy
import hashlib
import threading
import unittest

from PIL import Image
from playwright.sync_api import sync_playwright
from metadata_group import confirm_group
from sku_library import labels
from sku_match import normalize
from capture_parsing import capture_status
import test_ocr_metadata as fixtures
from unittest.mock import patch
import server


class InputConfirmTests(unittest.TestCase):
    setUp=fixtures.OcrMetadataTests.setUp
    tearDown=fixtures.OcrMetadataTests.tearDown

    def confirm(self,code,cid=None):
        return confirm_group(self.archive,self.sku,self.sid,cid or self.ids[1],[cid or self.ids[1]],code,'模特图',self.pid)

    def drain(self):
        for _ in range(150):
            with self.archive.connect() as c:pending=c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued'").fetchone()
            if not pending:break
            self.archive.process_one()
        with self.archive.connect() as c:self.assertEqual(c.execute("SELECT path,reason FROM metadata_jobs WHERE status='failed'").fetchall(),[])

    @contextmanager
    def http(self):
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:yield f'http://127.0.0.1:{http.server_port}'
            finally:http.shutdown();http.server_close()

    def test_no_ocr_and_missing_model_can_confirm_entire_interval(self):
        with patch.object(self.providers,'check_inference',side_effect=AssertionError('手动确认不能请求模型')):
            result=self.confirm(' MANUAL-100 ')
        self.assertEqual(result['sku'],'MANUAL-100');self.assertEqual(result['capture_count'],2)
        self.assertEqual(labels(self.sku,self.sid)['labels'],dict.fromkeys(self.ids[:2],'MANUAL-100'))
        status=capture_status(self.store,self.sku,self.archive,None,self.sid,self.ids[1])['ocr']
        self.assertEqual(status['status'],'confirmed');self.assertEqual(status['codes'],['MANUAL-100'])
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM ocr_documents').fetchone()[0],0)

    def test_no_active_document_can_confirm_without_model_or_prior_ocr(self):
        with self.sku.connect() as c:c.execute("DELETE FROM settings WHERE key='active'")
        result=confirm_group(self.archive,self.sku,self.sid,self.ids[1],[self.ids[1]],'NO-PLAN-100')
        self.assertEqual(result['sku'],'NO-PLAN-100');self.assertEqual(result['capture_count'],2)
        self.assertNotEqual(self.sku.project()['id'],self.pid)

    def test_last_input_wins_before_pending_writes_and_does_not_change_neighbor_or_pixels(self):
        def pixels(path):
            with Image.open(path) as image:return hashlib.sha256(image.tobytes()).hexdigest()
        before=[pixels(self.photos/f'{i}.JPG') for i in range(4)]
        self.confirm('NEXT-200',self.ids[3]);self.drain()
        neighbor=[hashlib.sha256((self.photos/f'{i}.JPG').read_bytes()).hexdigest() for i in (2,3)]
        for code in ('OLD-100','NEW-100','OLD-100'):
            self.assertEqual(self.confirm(code,self.ids[0])['sku'],code)
        self.drain()
        for i in range(2):self.assertEqual(self.archive.writer.read(self.photos/f'{i}.JPG')['XMP-fk:SKU'],'OLD-100')
        self.assertEqual(neighbor,[hashlib.sha256((self.photos/f'{i}.JPG').read_bytes()).hexdigest() for i in (2,3)])
        self.assertEqual(before,[pixels(self.photos/f'{i}.JPG') for i in range(4)])
        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['selected_sku'],'OLD-100')

    def test_explicit_input_overrides_old_letter_version_without_automatic_suffix(self):
        self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}])
        self.sku.parse_capture(self.sid,self.ids[1]);self.sku.parse_capture(self.sid,self.ids[3])
        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[3])['selected_sku'],fixtures.CODE+'-A')
        self.confirm(fixtures.CODE,self.ids[3]);self.confirm('CORRECT-100',self.ids[3]);self.confirm(fixtures.CODE,self.ids[3])
        details=self.sku.ocr_details(self.sid,self.ids[3])
        self.assertEqual(details['selected_sku'],fixtures.CODE);self.assertEqual(details['shoot_version'],'')
        self.assertTrue(details['has_ocr'])
        with self.archive.connect() as c:asset=c.execute('SELECT asset FROM catalog_links WHERE session=? AND capture=?',(self.sid,self.ids[3])).fetchone()[0]
        with self.sku.connect() as c:row=c.execute('SELECT sku,version FROM sku_shoot_versions WHERE asset=? AND normalized=?',(asset,normalize(fixtures.CODE))).fetchone()
        self.assertEqual(tuple(row),(fixtures.CODE,0))
        self.assertEqual(labels(self.sku,self.sid)['labels'][self.ids[3]],fixtures.CODE)

    def test_inflight_old_ocr_cannot_replace_manual_confirmation_or_show_failure(self):
        entered=threading.Event();release=threading.Event();errors=[]
        def recognize(*args):entered.set();release.wait(8);return {'text':'WRONG-999','codes':['WRONG-999'],'unscored':True}
        self.sku.recognizer=recognize
        def read():
            try:self.sku.parse_capture(self.sid,self.ids[1])
            except Exception as e:errors.append(str(e))
        worker=threading.Thread(target=read);worker.start()
        try:
            self.assertTrue(entered.wait(3));self.confirm('MANUAL-300')
        finally:release.set();worker.join(10)
        self.assertFalse(worker.is_alive())
        status=capture_status(self.store,self.sku,self.archive,None,self.sid,self.ids[1])['ocr']
        self.assertEqual(status['status'],'confirmed');self.assertEqual(status['codes'],['MANUAL-300'])
        self.assertEqual(labels(self.sku,self.sid)['labels'],dict.fromkeys(self.ids[:2],'MANUAL-300'))

    def test_browser_button_enter_latest_value_and_model_free_confirmation(self):
        calls=[];self.sku.recognizer=lambda *_:calls.append(1) or copy.deepcopy(self.reading)
        with self.http() as url,sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1580,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                page.evaluate('(cid)=>Framekeep.browsePhoto(cid)',self.ids[1]);page.locator('#sku-asset-panel').evaluate('e=>e.open=true')
                field=page.locator('#asset-sku');button=page.locator('#asset-parse-sku')
                self.assertEqual(button.inner_text(),'OCR 解析当前图')
                field.fill('MANUAL-100');self.assertEqual(button.inner_text(),'确定')
                out=fixtures.ROOT/'build/sku-input-confirm';out.mkdir(parents=True,exist_ok=True)
                page.locator('#sku-asset-panel').screenshot(path=str(out/'input-confirm-button.png'))
                with page.expect_response('**/api/metadata/group/confirm') as response:button.click()
                self.assertEqual(response.value.status,200)
                page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("已确认 SKU MANUAL-100")')
                field.fill('MANUAL-200');field.press('Enter')
                page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("已确认 SKU MANUAL-200")')
                field.fill('MANUAL-100');button.click()
                page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("已确认 SKU MANUAL-100")')
                self.assertEqual(field.input_value(),'MANUAL-100')
                self.assertEqual(labels(self.sku,self.sid)['labels'],dict.fromkeys(self.ids[:2],'MANUAL-100'))
                field.fill('');self.assertEqual(button.inner_text(),'OCR 解析当前图')
                self.assertEqual(calls,[]);self.assertEqual(errors,[])
            finally:browser.close()

    def test_browser_manual_confirm_during_ocr_ignores_late_old_response(self):
        entered=threading.Event();release=threading.Event()
        def recognize(*args):entered.set();release.wait(15);return {'text':'WRONG-999','codes':['WRONG-999'],'unscored':True}
        self.sku.recognizer=recognize
        with self.http() as url,sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1580,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                page.evaluate('(cid)=>Framekeep.browsePhoto(cid)',self.ids[1]);page.locator('#sku-asset-panel').evaluate('e=>e.open=true')
                page.locator('#asset-parse-sku').click();self.assertTrue(entered.wait(3))
                page.locator('#asset-sku').fill('MANUAL-400')
                self.assertTrue(page.locator('#asset-parse-sku').is_enabled());self.assertEqual(page.locator('#asset-parse-sku').inner_text(),'确定')
                page.locator('#asset-parse-sku').click()
                page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("已确认 SKU MANUAL-400")')
                release.set()
                page.wait_for_function('()=>document.getElementById("asset-parse-sku").textContent==="OCR 解析当前图"')
                self.assertEqual(page.locator('#asset-sku').input_value(),'MANUAL-400')
                page.wait_for_function('()=>document.getElementById("asset-ocr-feedback").textContent.includes("MANUAL-400")&&document.getElementById("asset-ocr-feedback").dataset.status==="completed"')
                self.assertNotIn('WRONG-999',page.locator('#asset-message').inner_text());self.assertEqual(errors,[])
            finally:release.set();browser.close()

    def test_browser_switching_photo_during_confirmation_keeps_new_photo_draft(self):
        import metadata_group
        original=metadata_group.confirm_group;entered=threading.Event();release=threading.Event()
        def delayed(*args,**kwargs):entered.set();release.wait(10);return original(*args,**kwargs)
        with patch.object(metadata_group,'confirm_group',side_effect=delayed),self.http() as url,sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1580,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                page.evaluate('(cid)=>Framekeep.browsePhoto(cid)',self.ids[1]);page.locator('#sku-asset-panel').evaluate('e=>e.open=true')
                page.locator('#asset-sku').fill('MANUAL-500');page.locator('#asset-parse-sku').click();self.assertTrue(entered.wait(3))
                page.evaluate('(cid)=>Framekeep.browsePhoto(cid)',self.ids[3]);page.locator('#asset-sku').fill('DRAFT-NEIGHBOR')
                with page.expect_response('**/api/metadata/group/confirm'):release.set()
                page.wait_for_function('()=>document.getElementById("asset-parse-sku").textContent==="确定"')
                self.assertEqual(page.locator('#asset-sku').input_value(),'DRAFT-NEIGHBOR')
                self.assertEqual(labels(self.sku,self.sid)['labels'],dict.fromkeys(self.ids[:2],'MANUAL-500'))
                self.assertEqual(errors,[])
            finally:release.set();browser.close()


if __name__=='__main__':unittest.main()
