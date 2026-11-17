"""现场二星入队异常必须保留星级、记录原因并结束提交动画。"""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import server
import test_sku_workflow as fixture


class LiveEnqueueFailureTests(unittest.TestCase):
    setUp=fixture.SkuWorkflowTests.setUp
    tearDown=fixture.SkuWorkflowTests.tearDown

    def globals(self,stack):
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,
                    ARCHIVE=None,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,RAWLINK=None,
                    FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CAPTURESYNC=None)
        for key,value in values.items():stack.enter_context(patch.object(server,key,value))

    def save(self):
        session=self.store.session(self.sid);state=copy.deepcopy(session['state'])
        state.setdefault('overrides',{})[self.cids[0]]=2
        return server.save_session_state({'session_id':self.sid,'revision':session['revision'],'state':state})

    def test_callback_failure_keeps_rating_and_records_original_exception(self):
        with ExitStack() as stack:
            self.globals(stack)
            stack.enter_context(patch.object(self.sku,'on_save',side_effect=PermissionError('共享目录没有写入权限')))
            result=self.save()
        self.assertEqual(self.store.session(self.sid)['state']['overrides'][self.cids[0]],2)
        self.assertEqual(result['revision'],self.store.session(self.sid)['revision'])
        self.assertIn('共享目录没有写入权限',result['sku_error'])
        report=json.loads((self.store.data/'last-operation-error.json').read_text('utf8'))
        self.assertEqual(result['sku_error_id'],report['id'])
        self.assertIn(report['id'],result['sku_error'])
        self.assertEqual(report['operation'],'/api/session/sku-enqueue')
        self.assertEqual(report['type'],'PermissionError')
        self.assertIn('PermissionError: 共享目录没有写入权限',report['traceback'])

    def test_unexpected_exception_is_logged_without_exposing_its_message(self):
        with ExitStack() as stack:
            self.globals(stack)
            stack.enter_context(patch.object(self.sku,'on_save',side_effect=RuntimeError('internal diagnostic detail')))
            result=self.save()
        self.assertNotIn('internal diagnostic detail',result['sku_error'])
        report=json.loads((self.store.data/'last-operation-error.json').read_text('utf8'))
        self.assertIn('internal diagnostic detail',report['traceback'])

    def test_browser_stops_submitting_preserves_rating_and_recovers_on_new_job(self):
        from playwright.sync_api import sync_playwright
        with ExitStack() as stack:
            self.globals(stack)
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.wait_for_function('()=>!window.FramekeepBoot?.loading&&window.FramekeepOcrFeedback')
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',self.cids[0]);page.evaluate('FramekeepIO.flush()')
                        page.clock.install()
                        with patch.object(self.sku,'on_save',side_effect=PermissionError('共享目录没有写入权限')):
                            with page.expect_response(lambda r:r.url.endswith('/api/session') and r.request.method=='POST') as response:
                                page.keyboard.press('2')
                            result=response.value.json()
                        self.assertEqual(response.value.status,200)
                        self.assertIn('sku_error_id',result)
                        page.wait_for_function('()=>document.getElementById("asset-ocr-feedback").dataset.status==="retry"')
                        self.assertNotIn('未能排队',page.locator('#toast').inner_text())
                        self.assertEqual(self.store.session(self.sid)['state']['overrides'][self.cids[0]],2)
                        page.evaluate('FramekeepIO.flush()')
                        page.evaluate('''()=>document.dispatchEvent(new CustomEvent('framekeep:capture-status',{detail:{session_id:FRAMEKEEP_SESSION.id,capture_id:Framekeep.remoteTarget().photoId,ocr:{}}}))''')
                        self.assertEqual(page.locator('#asset-ocr-feedback').get_attribute('data-status'),'retry')
                        page.clock.fast_forward(15000)
                        self.assertEqual(page.locator('#asset-ocr-feedback').get_attribute('data-status'),'failed')
                        self.assertIn('共享目录没有写入权限',page.locator('#asset-ocr-feedback').inner_text())
                        with page.expect_response(lambda r:r.url.endswith('/api/session') and r.request.method=='POST'):
                            page.keyboard.press('0')
                        with page.expect_response(lambda r:r.url.endswith('/api/session') and r.request.method=='POST') as response:
                            page.keyboard.press('2')
                        self.assertNotIn('sku_error',response.value.json())
                        self.sku.tick()
                        page.evaluate("document.dispatchEvent(new CustomEvent('framekeep:ocr-refresh'))")
                        page.wait_for_function('()=>document.getElementById("asset-ocr-feedback").dataset.status==="completed"')
                        # 回调在真实入队后才报错，不能盖住已有任务或先弹失败再显示成功。
                        with page.expect_response(lambda r:r.url.endswith('/api/session') and r.request.method=='POST'):
                            page.keyboard.press('0')
                        on_save=self.sku.on_save
                        def queued_then_error(*args):
                            on_save(*args)
                            raise OSError('模拟入队后的瞬时故障')
                        with patch.object(self.sku,'on_save',side_effect=queued_then_error):
                            with page.expect_response(lambda r:r.url.endswith('/api/session') and r.request.method=='POST'):
                                page.keyboard.press('2')
                        page.evaluate("document.dispatchEvent(new CustomEvent('framekeep:ocr-refresh'))")
                        page.wait_for_function('()=>document.getElementById("asset-ocr-feedback").dataset.status==="queued"')
                        self.assertNotIn('未能排队',page.locator('#toast').inner_text())
                        output=Path(__file__).resolve().parents[1]/'build/task241-enqueue-diagnostics'
                        output.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(output/'recovered-queued.png'))
                        self.sku.tick();page.evaluate("document.dispatchEvent(new CustomEvent('framekeep:ocr-refresh'))")
                        page.wait_for_function('()=>document.getElementById("asset-ocr-feedback").dataset.status==="completed"')
                        page.clock.fast_forward(15000)
                        self.assertEqual(page.locator('#asset-ocr-feedback').get_attribute('data-status'),'completed')
                        page.screenshot(path=str(output/'recovered-completed.png'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
