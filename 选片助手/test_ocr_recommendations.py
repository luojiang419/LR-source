import copy
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from sku_rules import validate_length_filter,length_allowed
from sku_service import SkuService
from test_ocr_metadata import OcrMetadataTests


class LengthFilterTests(unittest.TestCase):
    def test_strict_bounds_and_unicode(self):
        rule=validate_length_filter({'min_exclusive':5,'max_exclusive':8})
        for code in ('7877538','ＬＣ１２３４','AB-123'):
            self.assertTrue(length_allowed(code,rule),code)
        for code in ('00413','341D','12345678'):
            self.assertFalse(length_allowed(code,rule),code)
        self.assertTrue(length_allowed('  00413  ',validate_length_filter({'max_exclusive':6})))
        for value in (None,[],{'min_exclusive':True},{'min_exclusive':5.1},{'min_exclusive':-1},
                      {'min_exclusive':80},{'max_exclusive':1},{'min_exclusive':5,'max_exclusive':6},{'other':5}):
            with self.assertRaises(ValueError):validate_length_filter(value)


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        OcrMetadataTests.setUp(self)
        self.reading.update(text='7877538\n341D\n00413',codes=['341D'],ocr_candidates=['7877538','341D','00413'],model='glm-ocr')
    tearDown=OcrMetadataTests.tearDown
    parse=OcrMetadataTests.parse
    choose=OcrMetadataTests.choose

    def test_filtered_recommendations_raw_manual_override_and_persistence(self):
        self.sku.set_sku_rules([],{'min_exclusive':5,'max_exclusive':8})
        result=self.parse();self.assertEqual(result['recommended'],['7877538'])
        details=self.sku.ocr_details(self.sid,self.ids[1]);self.assertEqual(details['recommended'],['7877538'])
        self.assertIn('00413',details['text']);self.assertIn('00413',details['candidates'])
        self.choose('00413');self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['selected_sku'],'00413')
        reopened=SkuService(self.store,self.providers,autostart=False)
        try:self.assertEqual(reopened.sku_rules()['length_filter'],{'min_exclusive':5,'max_exclusive':8})
        finally:reopened.close()
        self.sku.set_sku_rules([])  # 旧客户端不传长度时保留设置。
        self.assertEqual(self.sku.sku_rules()['length_filter']['min_exclusive'],5)
        with self.assertRaises(ValueError):self.sku.set_sku_rules([{'kind':'prefix','value':'X'}],{'min_exclusive':9,'max_exclusive':5})
        self.assertEqual(self.sku.sku_rules()['rules'],[])
        self.sku.set_sku_rules([],{'min_exclusive':None,'max_exclusive':None})
        self.assertEqual(self.parse()['recommended'],['341D'])

    def test_length_blocks_automatic_prefix_in_manual_and_background(self):
        self.reading.update(text='LC341D',codes=['LC341D'],ocr_candidates=[])
        self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}],{'min_exclusive':6})
        self.assertIsNone(self.parse()['auto_applied'])
        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['recommended'],[])
        self.sku.set_full_auto(True,self.sid);self.sku.tick()
        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['selected_sku'],'')
        self.sku.set_sku_rules([{'kind':'prefix','value':'LC'}],{'max_exclusive':7})
        self.assertEqual(self.parse()['auto_applied']['sku'],'LC341D')

    def test_latest_recommendations_do_not_reintroduce_history(self):
        self.parse()
        self.reading.update(text='9876543',codes=['9876543'],ocr_candidates=['9876543'])
        self.parse()
        self.sku.set_sku_rules([],{'min_exclusive':5})
        details=self.sku.ocr_details(self.sid,self.ids[1])
        self.assertEqual(details['latest_recommended'],['9876543'])
        self.assertIn('7877538',details['text'])

    def test_browser_original_buttons_and_settings(self):
        import server
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.locator('[data-watch-photo="'+self.ids[1]+'"]').click();page.locator('.asset-panel').evaluate('e=>e.open=true')
                        page.locator('#asset-parse-sku').click();page.wait_for_selector('#asset-ocr-text [data-ocr-code="00413"]')
                        self.assertEqual(page.locator('#asset-ocr-codes [data-ocr-code]').count(),1)
                        self.assertEqual(page.locator('#asset-ocr-text [data-ocr-code]').count(),3)
                        page.locator('#app-settings-open').click();page.locator('[data-settings-section="sku-rules"]').click()
                        page.wait_for_function('()=>!document.getElementById("sku-rule-save").disabled')
                        page.get_by_label('字符数大于',exact=True).fill('5');page.get_by_label('字符数小于',exact=True).fill('8');page.locator('#sku-rule-save').click()
                        page.wait_for_function('()=>document.getElementById("sku-rule-message").textContent.startsWith("已保存")')
                        page.wait_for_selector('#asset-ocr-codes [data-ocr-code="7877538"]',state='attached')
                        self.assertEqual(page.locator('#asset-ocr-codes [data-ocr-code="341D"]').count(),0)
                        out=Path(__file__).resolve().parents[1]/'build/ocr-recommendations';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'settings.png'))
                        page.locator('#app-settings-close').click()
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(t)=>{document.documentElement.dataset.framekeepTheme=t;document.documentElement.dataset.hardcoreLayout=String(t==="hardcore")}',theme)
                            for width in (1440,1024,760):
                                page.set_viewport_size({'width':width,'height':1000})
                                for selector in ('#asset-ocr-text','#asset-ocr-codes'):
                                    self.assertFalse(page.locator(selector).evaluate('(e)=>e.scrollWidth>e.clientWidth'),(theme,width,selector))
                            page.set_viewport_size({'width':1440,'height':1000})
                            page.locator('#sku-asset-panel').screenshot(path=str(out/f'raw-{theme}.png'))
                        raw=page.locator('#asset-ocr-text [data-ocr-code="00413"]')
                        self.assertLess(raw.bounding_box()['height'],page.locator('#asset-ocr-codes button').bounding_box()['height'])
                        raw.focus();page.keyboard.press('Enter')
                        page.wait_for_function('()=>document.querySelector("#asset-ocr-text [data-ocr-code=\\"00413\\"]").getAttribute("aria-pressed")==="true"')
                        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['selected_sku'],'00413')
                        self.assertEqual(page.locator('#asset-sku').input_value(),'00413')
                        page.locator('#asset-ocr-details').click();page.wait_for_selector('#ocr-metadata-text [data-ocr-pick="341D"]')
                        self.assertEqual(page.locator('#ocr-metadata-candidates [data-ocr-pick="341D"]').count(),0)
                        page.locator('#ocr-metadata-text [data-ocr-pick="341D"]').click()
                        self.assertEqual(page.locator('#ocr-metadata-sku').input_value(),'341D')
                        self.assertEqual(errors,[])
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
