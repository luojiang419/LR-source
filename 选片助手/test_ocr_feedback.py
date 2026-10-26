"""双位置 OCR 反馈：真实队列、重试状态、切图隔离和显示设置。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from capture_parsing import capture_status
import test_sku_workflow as fixture


class OcrFeedbackTests(unittest.TestCase):
    setUp=fixture.SkuWorkflowTests.setUp
    tearDown=fixture.SkuWorkflowTests.tearDown
    rate=fixture.SkuWorkflowTests.rate

    def status(self):
        return capture_status(self.store,self.sku,None,None,self.sid,self.cids[0])['ocr']

    def test_paused_manual_retry_and_failure_do_not_reuse_old_confirmed_choice(self):
        self.rate(2)
        with self.sku.connect() as c:
            c.execute('INSERT INTO ocr_choices VALUES (?,?,?,?,?)',(self.sid,self.cids[0],'OLD-SKU','old',1))
            c.execute('UPDATE jobs SET result=?',(json.dumps({'requires_sku_choice':True}),))
        self.assertEqual(self.status()['status'],'queued')
        self.sku.configure_tasks(paused=True)
        self.assertEqual(self.status()['status'],'paused')
        self.sku.configure_tasks(paused=False,mode='manual')
        self.assertEqual(self.status()['status'],'manual')

        self.sku.configure_tasks(mode='auto')
        for state in ('running','failed','cancelled','waiting_model'):
            with self.sku.connect() as c:c.execute('UPDATE jobs SET status=?',(state,))
            self.assertEqual(self.status()['status'],state)
        with self.sku.connect() as c:
            c.execute('INSERT INTO parse_jobs(id,session_id,capture_id,status,created) VALUES (?,?,?,?,?)',('new-parse',self.sid,self.cids[0],'queued',1001))
        self.sku.configure_tasks(mode='manual')
        self.assertEqual(self.status()['status'],'manual')

    def test_review_exposes_candidate_text_without_confirming_sku(self):
        self.rate(2)
        result={'requires_sku_choice':True,'readings':[fixture.reading()]}
        with self.sku.connect() as c:c.execute("UPDATE jobs SET status='review',result=?",(json.dumps(result),))
        status=self.status()
        self.assertEqual(status['status'],'review');self.assertEqual(status['codes'],[])
        self.assertIn(fixture.CODE,status['candidates'])

    def test_live_queue_feedback_settings_and_photo_isolation(self):
        import server
        started=threading.Event();release=threading.Event()
        def recognize(*_):
            started.set()
            if not release.wait(20):raise TimeoutError('测试等待超时')
            return fixture.reading()
        self.sku.recognizer=recognize;self.sku.thread.start()
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=None,
                    CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,
                    UPDATES=None,COMPONENTS=None,DRIVER=None)
        out=fixture.ROOT/'build/ocr-feedback';out.mkdir(parents=True,exist_ok=True)
        with ExitStack() as stack:
            for name,value in values.items():stack.enter_context(patch.object(server,name,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':950});errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',self.cids[0]);page.evaluate('()=>FramekeepIO.flush()')
                        pending=[]
                        def hold_save(route):
                            if route.request.method=='POST':pending.append(route)
                            else:route.continue_()
                        page.route('**/api/session',hold_save)
                        page.locator('#stars [data-rating="2"]').click()
                        self.assertTrue(page.locator('#asset-ocr-feedback').is_visible())
                        self.assertEqual(page.locator('#preview-ocr-feedback').get_attribute('data-status'),'queued')
                        self.assertFalse(started.is_set())
                        self.assertTrue(pending)
                        for route in pending:route.continue_()
                        page.unroute('**/api/session',hold_save)
                        self.assertTrue(started.wait(3))
                        page.wait_for_function('()=>document.getElementById("preview-ocr-feedback").dataset.status==="running"')
                        self.assertTrue(page.locator('#asset-ocr-feedback').is_visible())
                        self.assertEqual(page.locator('#preview-ocr-feedback').get_attribute('data-position'),'top-right')
                        self.assertEqual(page.locator('#asset-ocr-feedback .ocr-feedback-title').inner_text(),page.locator('#preview-ocr-feedback .ocr-feedback-title').inner_text())
                        self.assertEqual(page.locator('#preview-ocr-feedback').evaluate('e=>getComputedStyle(e).opacity'),'0.65')
                        self.assertEqual(page.locator('#preview-ocr-feedback .ocr-scanner-sweep').evaluate('e=>getComputedStyle(e).animationName'),'ocr-sweep')
                        page.screenshot(path=str(out/'running.png'))
                        release.set()
                        page.wait_for_function('(sku)=>document.querySelector("#preview-ocr-feedback .ocr-feedback-title").textContent===sku',arg=fixture.CODE)
                        self.assertEqual(page.locator('#preview-ocr-feedback').get_attribute('data-status'),'completed')
                        self.assertFalse(page.locator('#preview-ocr-feedback svg').is_visible())
                        page.screenshot(path=str(out/'completed.png'))
                        # 切到无结果照片不能保留上一张的款号；返回后从真实状态恢复。
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',self.cids[1])
                        page.wait_for_function('()=>document.getElementById("preview-ocr-feedback").hidden')
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',self.cids[0])
                        page.wait_for_function('(sku)=>!document.getElementById("preview-ocr-feedback").hidden&&document.querySelector("#preview-ocr-feedback strong").textContent===sku',arg=fixture.CODE)
                        # 旧确认不能盖住新的手动解析；解析期间切图不能串结果。
                        self.clock[0]+=1;started.clear();release.clear()
                        page.locator('#asset-parse-sku').click();self.assertTrue(started.wait(3))
                        self.assertEqual(page.locator('#preview-ocr-feedback').get_attribute('data-status'),'running')
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',self.cids[1]);release.set()
                        page.wait_for_function('()=>!document.getElementById("asset-parse-sku").disabled')
                        self.assertTrue(page.locator('#preview-ocr-feedback').evaluate('e=>e.hidden'))
                        # 暂停、撤星与缺模型使用真实后端状态。
                        self.sku.configure_tasks(paused=True)
                        page.keyboard.press('2')
                        page.wait_for_function('()=>document.querySelector("#preview-ocr-feedback strong").textContent.includes("已暂停")')
                        self.assertFalse(page.locator('#preview-ocr-feedback svg').is_visible())
                        page.keyboard.press('0')
                        page.wait_for_function('()=>document.getElementById("preview-ocr-feedback").hidden')
                        with self.providers.lock:self.providers.data['selected']={}
                        self.sku.configure_tasks(paused=False);page.keyboard.press('2')
                        page.wait_for_function('()=>document.querySelector("#preview-ocr-feedback strong").textContent.includes("等待配置")')
                        # 位置、透明度、控件避让与持久化。
                        page.locator('#app-settings-open').click();page.locator('[data-settings-section="preview"]').click()
                        page.locator('#preview-ocr-position').select_option('bottom-left');page.locator('#preview-ocr-transparency').fill('60')
                        self.assertEqual(page.locator('#preview-ocr-transparency-value').inner_text(),'60%')
                        page.screenshot(path=str(out/'settings.png'))
                        page.locator('#app-settings-close').click()
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',self.cids[1])
                        page.wait_for_function('()=>!document.getElementById("preview-ocr-feedback").hidden')
                        box=page.locator('#preview-ocr-feedback').bounding_box();stars=page.locator('#stars').bounding_box()
                        self.assertLessEqual(box['y']+box['height'],stars['y'])
                        self.assertEqual(page.locator('#preview-ocr-feedback').evaluate('e=>getComputedStyle(e).opacity'),'0.4')
                        self.assertEqual(page.locator('#asset-ocr-feedback').evaluate('e=>getComputedStyle(e).opacity'),'1')
                        page.reload(wait_until='networkidle')
                        self.assertEqual(page.locator('#preview-ocr-position').input_value(),'bottom-left')
                        self.assertEqual(page.locator('#preview-ocr-transparency').input_value(),'60')
                        page.locator('#app-settings-open').click();page.locator('[data-settings-section="preview"]').click()
                        for position in ('top-left','top-right','bottom-left','bottom-right','hidden'):
                            page.locator('#preview-ocr-position').select_option(position)
                            self.assertEqual(page.locator('#preview-ocr-feedback').get_attribute('data-position'),position)
                        page.locator('#preview-ocr-position').select_option('top-right')
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                            for width in (1440,375):
                                page.set_viewport_size({'width':width,'height':950})
                                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                                page.screenshot(path=str(out/f'settings-{theme}-{width}.png'))
                        page.set_viewport_size({'width':1440,'height':950});page.locator('#app-settings-close').click()
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',self.cids[1])
                        with self.sku.connect() as c:c.execute("UPDATE jobs SET status='failed',reason='模型连接失败 <test>' WHERE capture_id=?",(self.cids[1],))
                        page.wait_for_function('()=>document.getElementById("preview-ocr-feedback").dataset.status==="failed"')
                        self.assertIn('<test>',page.locator('#preview-ocr-feedback .ocr-feedback-note').inner_text())
                        self.assertFalse(page.locator('#preview-ocr-feedback svg').is_visible())
                        result={'requires_sku_choice':True,'readings':[fixture.reading()]}
                        with self.sku.connect() as c:c.execute("UPDATE jobs SET status='review',result=? WHERE capture_id=?",(json.dumps(result),self.cids[1]))
                        page.wait_for_function('()=>document.getElementById("preview-ocr-feedback").dataset.status==="review"')
                        self.assertEqual(page.locator('#preview-ocr-feedback strong').inner_text(),fixture.CODE)
                        self.assertIn('请核对',page.locator('#preview-ocr-feedback .ocr-feedback-note').inner_text())
                        self.assertFalse(page.locator('#preview-ocr-feedback svg').is_visible())
                        page.emulate_media(reduced_motion='reduce')
                        # CSS 动效关闭可在设置样例上验证，不改业务状态。
                        self.assertEqual(page.locator('#ocr-feedback-sample .ocr-scanner-sweep').evaluate('e=>getComputedStyle(e).animationName'),'none')
                        self.assertFalse(errors,errors)
                        (out/'result.json').write_text(json.dumps({'passed':True,'errors':errors,'themes':4,'widths':[1440,375],'checks':['actual queue','dual animation','SKU completion','manual parse','photo isolation','pause and cancel','missing model','control collision','settings persistence','reduced motion']},ensure_ascii=False,indent=2),encoding='utf8')
                    finally:release.set();browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
