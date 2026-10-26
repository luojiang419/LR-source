import copy
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from sku_rules import DEFAULT_RULES,match_rules,validate_rules
from sku_service import SkuService
from sku_library import labels
from test_ocr_metadata import OcrMetadataTests
from test_sku_workflow import CODE


class RuleTests(unittest.TestCase):
    def test_tokens_and_conflicts(self):
        result=match_rules([{'text':'款号：ｌｃ１２３－Ａ\nSIZE S\nLC456 / LC123-A'}],DEFAULT_RULES)
        self.assertEqual(result['exact'],['LC123-A','LC456'])
        self.assertFalse(match_rules([{'text':'XLC123 LC COTTON'}],DEFAULT_RULES)['exact'])
        self.assertEqual(match_rules([{'text':'AB123-P905'}],[{'kind':'suffix','value':'P905'}])['exact'],['AB123-P905'])
        self.assertEqual(match_rules([{'text':'XLC123'}],[{'kind':'contains','value':'LC'}]),{'exact':[],'suggested':['XLC123']})

    def test_validation(self):
        self.assertEqual(validate_rules([{'kind':'prefix','value':' lc '}]),DEFAULT_RULES)
        for value in [None,{},[{'kind':'regex','value':'LC'}],[{'kind':'prefix','value':''}],[{'kind':'suffix','value':'*'}]]:
            with self.assertRaises(ValueError):validate_rules(value)


class RuleIntegrationTests(unittest.TestCase):
    def setUp(self):
        OcrMetadataTests.setUp(self)
        with self.sku.connect() as c:c.execute("DELETE FROM settings WHERE key='sku_match_rules'")
    tearDown=OcrMetadataTests.tearDown
    parse=OcrMetadataTests.parse
    choose=OcrMetadataTests.choose

    def test_default_applies_interval_and_preserves_manual_correction(self):
        result=self.parse();self.assertEqual(result['auto_applied']['sku'],CODE)
        self.assertEqual(labels(self.sku,self.sid)['labels'],{self.ids[0]:CODE,self.ids[1]:CODE})
        for _ in range(30):
            with self.archive.connect() as c:pending=c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued'").fetchone()
            if not pending:break
            self.archive.process_one()
        for i in range(2):self.assertEqual(self.archive.writer.read(self.photos/f'{i}.JPG')['XMP-fk:SKU'],CODE)
        self.choose('CORRECT-100');self.assertIsNone(self.parse()['auto_applied'])
        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['selected_sku'],'CORRECT-100')

    def test_conflicts_fuzzy_and_no_match_require_choice(self):
        self.reading['codes'].append('LC999');self.assertIsNone(self.parse()['auto_applied'])
        self.assertEqual(labels(self.sku,self.sid)['labels'],{})
        self.sku.set_sku_rules([{'kind':'contains','value':'LC'}]);self.reading['codes']=[CODE]
        self.assertIsNone(self.parse()['auto_applied'])
        self.sku.set_sku_rules([]);self.assertIsNone(self.parse()['auto_applied'])

    def test_suffix_persists_and_single_photo_without_document(self):
        rules=[{'kind':'suffix','value':'P905'}];self.sku.set_sku_rules(rules)
        reopened=SkuService(self.store,self.providers,autostart=False)
        try:self.assertEqual(reopened.sku_rules()['rules'],rules)
        finally:reopened.close()
        with self.sku.connect() as c:c.execute("DELETE FROM settings WHERE key='active'")
        session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides']={};self.store.save(self.sid,session['revision'],state)
        result=self.sku.parse_capture(self.sid,self.ids[0])
        self.assertEqual(result['auto_applied']['capture_count'],1)
        self.assertEqual(self.archive.query(CODE)['total'],1)

    def test_background_and_manual_assignment_protection(self):
        self.archive.apply(self.archive.preview(self.sid,[self.ids[0]],'MANUAL-900','模特图',self.pid)['preview_id'])
        self.sku.set_full_auto(True,self.sid);self.sku.tick()
        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['selected_sku'],CODE)
        self.assertEqual(self.archive.query('MANUAL-900')['total'],1)
        self.assertEqual(self.archive.query(CODE)['total'],1)
        self.assertIsNone(self.sku.parse_capture(self.sid,self.ids[0])['auto_applied'])

    def test_browser_settings_and_auto_apply(self):
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
                        page.locator('#app-settings-open').click();page.locator('[data-settings-section="sku-rules"]').click()
                        page.wait_for_function('()=>!document.getElementById("sku-rule-save").disabled')
                        self.assertEqual(page.get_by_label('款号匹配文字',exact=True).input_value(),'LC')
                        page.locator('#sku-rule-add').click();page.get_by_label('款号匹配方式',exact=True).nth(1).select_option('suffix');page.get_by_label('款号匹配文字',exact=True).nth(1).fill('P905')
                        page.locator('#sku-rule-save').click();page.wait_for_function('()=>document.getElementById("sku-rule-message").textContent.startsWith("已保存")')
                        self.assertEqual(len(self.sku.sku_rules()['rules']),2)
                        output=Path(__file__).resolve().parents[1]/'build'/'sku-rules';output.mkdir(parents=True,exist_ok=True)
                        for width in (1440,1024,760):
                            page.set_viewport_size({'width':width,'height':1000})
                            self.assertFalse(page.locator('#settings-sku-rules').evaluate('(e)=>e.scrollWidth>e.clientWidth'))
                        page.set_viewport_size({'width':1440,'height':1000})
                        page.screenshot(path=str(output/'settings.png'))
                        page.locator('#app-settings-close').click();page.locator('[data-watch-photo="'+self.ids[1]+'"]').click();page.locator('.asset-panel').evaluate('e=>e.open=true')
                        page.locator('#asset-parse-sku').click();page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("规则已自动应用")')
                        self.assertEqual(page.locator('#asset-sku').input_value(),CODE)
                        self.assertEqual(page.locator('#asset-ocr-codes [data-ocr-code="'+CODE+'"]').get_attribute('aria-pressed'),'true')
                        page.screenshot(path=str(output/'auto-applied.png'));self.assertEqual(errors,[])
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
