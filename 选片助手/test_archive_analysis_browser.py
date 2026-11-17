"""真实浏览器操作新增开关与归档按钮；复用真实持久队列。"""
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from PIL import Image
from playwright.sync_api import sync_playwright
import test_asset_retrieval_browser as fixture
from test_asset_intelligence import result
from vision_scheduler import VisionScheduler
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class ArchiveAnalysisBrowserTests(unittest.TestCase):
    setUp=fixture.RetrievalBrowserTests.setUp
    tearDown=fixture.RetrievalBrowserTests.tearDown

    def test_settings_switch_toolbar_pause_resume_and_manual_new_files(self):
        self.providers.save({'name':'视觉测试','category':'multimodal','protocol':'chat','model':'vision-fixture','url':'http://127.0.0.1:1','select':True})
        self.providers.save({'name':'OCR测试','category':'ocr','protocol':'chat','model':'ocr-fixture','url':'http://127.0.0.1:1','select':True})
        folder=Path(self.rows[0]['path']).parent;sid=self.store.ensure_live_session(folder)
        for path in sorted(folder.glob('*.jpg')):self.store.append_live(sid,[media.capture_record(folder,path,None,{})])
        self.store.activate(sid)
        self.sku.scheduler=VisionScheduler();self.service.scheduler=self.sku.scheduler
        self.service.configure({'auto_group':False})
        started=threading.Event();release=threading.Event();calls=[]
        def ocr(*_):calls.append('ocr');started.set();release.wait(10);return {'text':'LC1234','codes':['LC1234'],'model':'ocr-fixture'}
        def vision(*_):calls.append('vision');return json.dumps(result())
        out=ROOT/'build/archive-analysis-pipeline';out.mkdir(exist_ok=True)
        with patch('asset_intelligence.recognize_ocr',side_effect=ocr),patch('asset_intelligence.vision',side_effect=vision),sync_playwright() as pw:
            for worker in tuple(self.service.threads):worker.start()
            self.service.discovery.start()
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#app-settings-open').click();page.locator('[data-settings-section=models]').click()
                page.wait_for_function('()=>!document.getElementById("full-auto-archive").disabled')
                self.assertEqual(page.locator('#full-auto-archive').evaluate('e=>[e.offsetWidth,e.offsetHeight]'),[52,30])
                page.locator('#full-auto-archive-manage').click();page.locator('#catalog-analysis-enable-ocr').check()
                page.wait_for_function('()=>!document.getElementById("catalog-analysis-enable-ocr").disabled')
                self.assertTrue(self.service.config['ocr_enabled']);self.assertFalse(self.sku.full_auto.get('enabled'))
                page.locator('#catalog-analysis-close').click()
                page.locator('#full-auto-archive').check();self.assertTrue(started.wait(5))
                page.locator('#full-auto-archive-manage').click()
                page.wait_for_function('()=>document.getElementById("catalog-analysis-phase").textContent.includes("OCR")')
                self.assertIn('OCR → 多维度',page.locator('#catalog-analysis-order').inner_text())
                page.screenshot(path=str(out/'ocr-running.png'))
                page.locator('#catalog-analysis-pause').click();release.set()
                page.wait_for_function('()=>document.getElementById("catalog-analysis-phase").textContent.includes("暂停")')
                self.assertNotIn('vision',calls)
                page.locator('#catalog-analysis-close').click();page.locator('#app-settings-close').click();page.locator('#asset-archive-open').click()
                self.assertTrue(page.locator('#catalog-analysis').evaluate("e=>e.previousElementSibling.id==='catalog-image-search'"))
                page.locator('#catalog-analysis').click();self.assertEqual(page.locator('#catalog-analysis-start').inner_text(),'继续解析')
                page.locator('#catalog-analysis-start').click()
                page.wait_for_function('()=>document.getElementById("catalog-analysis-done").textContent==="3"',timeout=15000)
                self.assertEqual(calls.count('ocr'),3);self.assertEqual(calls.count('vision'),3)
                for theme in ('dark','light','pure-white','hardcore'):
                    page.evaluate('theme=>FramekeepTheme.set(theme)',theme);page.wait_for_timeout(250)
                    page.screenshot(path=str(out/('dialog-'+theme+'.png')))
                page.set_viewport_size({'width':375,'height':850});self.assertFalse(page.evaluate('document.getElementById("catalog-analysis-dialog").scrollWidth>document.getElementById("catalog-analysis-dialog").clientWidth+1'))
                page.set_viewport_size({'width':1440,'height':960});page.locator('#catalog-analysis-close').click()
                page.locator('#app-settings-open').click();page.locator('[data-settings-section=models]').click();page.locator('#full-auto-archive').uncheck()
                page.wait_for_function('()=>!document.getElementById("full-auto-archive").disabled');self.assertFalse(self.service.config['enabled'])
                page.wait_for_timeout(300);page.screenshot(path=str(out/'settings.png'))
                page.locator('#app-settings-close').click();page.locator('#asset-archive-open').click()
                Image.new('RGB',(220,300),'red').save(folder/'新增.jpg');self.catalog.scan(self.rows[0]['root'])
                page.locator('#catalog-analysis').click();page.locator('#catalog-analysis-start').click()
                page.wait_for_function('()=>document.getElementById("catalog-analysis-done").textContent==="4"',timeout=15000)
                self.assertFalse(self.service.config['enabled']);self.assertFalse(self.service.config['manual_active'])
                self.assertEqual((calls.count('ocr'),calls.count('vision')),(4,4));self.assertFalse(errors,errors)
            finally:release.set();self.service.close();browser.close()

    def test_missing_model_reverts_switch_and_explains_without_starting(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded')
                page.locator('#app-settings-open').click();page.locator('[data-settings-section=models]').click()
                page.wait_for_function('()=>!document.getElementById("full-auto-archive").disabled')
                page.locator('#full-auto-archive').click()
                page.wait_for_function('()=>!document.getElementById("full-auto-archive").checked&&!document.getElementById("full-auto-archive").disabled')
                self.assertFalse(self.service.config['enabled'])
                self.assertIn('模型',page.locator('#full-auto-archive-status').inner_text()+page.locator('#catalog-analysis-message').text_content())
            finally:browser.close()

    def test_slow_pipeline_poll_is_not_invalidated_by_next_timer(self):
        self.service.configure({'ocr_enabled':True})
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page()
                page.add_init_script("""const originalFetch=window.fetch;
                    window.fetch=async(url,...args)=>{const response=await originalFetch(url,...args);
                    if(String(url)==='/api/intelligence/pipeline')await new Promise(resolve=>setTimeout(resolve,2600));return response};""")
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                page.locator('#catalog-analysis').click()
                page.wait_for_function("()=>document.getElementById('catalog-analysis-enable-ocr').checked",timeout=12000)
                self.assertIn('OCR',page.locator('#catalog-analysis-order').inner_text())
            finally:browser.close()

    def test_manual_stage_buttons_wait_for_cache_and_ocr_does_not_start_vision(self):
        self.providers.save({'name':'视觉测试','category':'multimodal','protocol':'chat','model':'vision-fixture','url':'http://127.0.0.1:1','select':True})
        self.providers.save({'name':'OCR测试','category':'ocr','protocol':'chat','model':'ocr-fixture','url':'http://127.0.0.1:1','select':True})
        phase={'ready':False,'checking':False,'pending':3,'failed':0}
        self.catalog.preview_warmer=SimpleNamespace(phase=phase,active=set(),refresh_phase=lambda:dict(phase))
        self.service.configure({'enabled':False,'manual_active':False,'paused':False,'auto_group':False})
        with patch('asset_intelligence.recognize_ocr',return_value={'text':'MANUALOCR','codes':[]}) as ocr,patch('asset_intelligence.vision',return_value=json.dumps(result())) as vision,sync_playwright() as pw:
            for worker in self.service.threads:worker.start()
            self.service.discovery.start()
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click();page.locator('#catalog-analysis').click()
                page.wait_for_function("()=>!document.getElementById('catalog-analysis-start-ocr').hidden")
                self.assertTrue(page.locator('#catalog-analysis-start-ocr').is_disabled())
                self.assertTrue(page.locator('#catalog-analysis-start-vision').is_disabled())
                self.assertTrue(page.locator('#full-auto-archive').is_hidden())
                phase.update(ready=True,pending=0)
                page.wait_for_function("()=>!document.getElementById('catalog-analysis-start-ocr').disabled")
                page.locator('#catalog-analysis-start-ocr').click()
                page.wait_for_function("()=>document.getElementById('catalog-analysis-phase').textContent.includes('OCR已完成')")
                self.assertEqual(ocr.call_count,3);vision.assert_not_called()
                page.locator('#catalog-analysis-start-vision').click()
                page.wait_for_function("()=>document.getElementById('catalog-analysis-phase').textContent.includes('视觉解析已完成')")
                self.assertEqual(vision.call_count,3);self.assertEqual(ocr.call_count,3)
                self.assertFalse(self.service.pipeline_state()['running']);self.assertFalse(errors,errors)
                out=ROOT/'build/archive-staged-cache';out.mkdir(exist_ok=True);page.screenshot(path=str(out/'manual-stages.png'))
            finally:self.service.close();browser.close()


if __name__=='__main__':unittest.main()
