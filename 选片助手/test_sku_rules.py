import copy
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import sqlite3
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
        self.assertEqual(match_rules([{'text':'XLC123 LC COTTON'}],DEFAULT_RULES)['exact'],['XLC123'])
        self.assertEqual(match_rules([{'text':'AB123-P905'}],[{'kind':'suffix','value':'P905'}])['exact'],['AB123-P905'])
        self.assertEqual(match_rules([{'text':'XLC123'}],[{'kind':'contains','value':'LC'}]),{'exact':['XLC123'],'suggested':['XLC123']})

    def test_customer_label_raw_text_contains_rule_even_when_model_codes_miss_it(self):
        code='LC7877529-P105'
        reading={'text':'SIZE:S\n'+code+'\n261020 G93\n71% COTTON\n27.5% POLYESTER\n1.5% ELASTANE\nMADE IN CHINA','codes':['261020','G93']}
        for kind,value in [('prefix','LC'),('suffix','P105'),('suffix','-P105'),('contains','7877529'),('prefix',code)]:
            with self.subTest(kind=kind,value=value):
                self.assertEqual(match_rules([reading],validate_rules([{'kind':kind,'value':value}]))['exact'],[code])
        screenshot={'text':'SIZE:s\nLC7877847-P520\n261016 G123\n98% COTTON\n2% ELASTANE\nMADE IN CHINA'}
        self.assertEqual(match_rules([screenshot],DEFAULT_RULES)['exact'],['LC7877847-P520'])

    def test_ocr_spaces_unicode_hyphens_and_repeated_candidates_collapse(self):
        reading={'text':'SIZE:S\nｌｃ ７８７７５２９ — Ｐ １０５\n261020 G93','codes':['LC7877529-P105'],'ocr_candidates':['LC7877529－P105']}
        self.assertEqual(match_rules([reading],DEFAULT_RULES)['exact'],['LC7877529-P105'])
        self.assertEqual(match_rules([{'text':'LC COTTON'}],DEFAULT_RULES)['exact'],[])

    def test_clear_raw_text_beats_truncated_model_codes_but_two_raw_skus_need_choice(self):
        code='LC7877529-P105'
        self.assertEqual(match_rules([{'text':code,'codes':['LC7877529','LC0000000-P000']}],DEFAULT_RULES)['exact'],[code])
        self.assertEqual(match_rules([{'text':code+'\nLC7877847-P520','codes':[code]}],DEFAULT_RULES)['exact'],[code,'LC7877847-P520'])
        self.assertEqual(match_rules([{'codes':[code]}],DEFAULT_RULES)['exact'],[code])

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

    def test_conflicts_require_choice_but_contains_unique_match_applies(self):
        self.reading['codes'].append('LC999');self.reading['text']+='\nLC999';self.assertIsNone(self.parse()['auto_applied'])
        self.assertEqual(labels(self.sku,self.sid)['labels'],{})
        self.sku.set_sku_rules([{'kind':'contains','value':'LC'}]);self.reading['codes']=[CODE];self.reading['text']=self.reading['text'].removesuffix('\nLC999')
        self.assertEqual(self.parse()['auto_applied']['sku'],CODE)
        self.sku.set_sku_rules([]);self.assertIsNone(self.parse()['auto_applied'])

    def test_manual_parse_with_full_auto_off_applies_customer_rule_and_confirms_feedback(self):
        code='LC7877529-P105'
        self.sku.set_sku_rules([{'kind':'contains','value':'LC'}])
        self.sku.configure_tasks(mode='manual');self.assertFalse(self.sku.full_auto_state()['enabled'])
        self.reading.update(text='SIZE:S\n'+code+'\n261020 G93\n71% COTTON',codes=['G93'],ocr_candidates=[])
        result=self.parse();self.assertEqual(result['auto_applied']['sku'],code)
        self.assertEqual(self.archive.query(code)['total'],2)
        from capture_parsing import capture_status
        feedback=capture_status(self.store,self.sku,self.archive,None,self.sid,self.ids[1])['ocr']
        self.assertEqual(feedback['status'],'confirmed');self.assertEqual(feedback['codes'],[code])
        row=next(r for r in self.sku.state()['rows'] if r['sku']==code)
        self.assertEqual(row['status'],'已拍摄');self.assertTrue(row['sku_image']);self.assertTrue(row['completed_at'])

    def test_length_filter_runs_before_counting_unique_rule_hits(self):
        code='LC7877529-P105';self.sku.set_sku_rules([{'kind':'contains','value':'LC'}],{'min_exclusive':10,'max_exclusive':20})
        self.reading.update(text=code+'\nLC99',codes=[code,'LC99'],ocr_candidates=[])
        self.assertEqual(self.parse()['auto_applied']['sku'],code)

    def background_rule_with_postcommit_failure(self,method,error):
        code='LC7877499-P905'
        self.reading.update(text='SIZE:S\n'+code+'\n261009 G264-1',codes=[code],ocr_candidates=[code])
        self.sku.enqueue(self.sid,self.ids[1])
        with patch.object(self.sku,method,side_effect=error) as failed:
            self.sku.tick()
            self.assertGreaterEqual(failed.call_count,2)  # 结果保存和自动确认均尝试后续任务。
        from capture_parsing import capture_status
        feedback=capture_status(self.store,self.sku,self.archive,None,self.sid,self.ids[1])['ocr']
        self.assertEqual(feedback['status'],'confirmed');self.assertEqual(feedback['codes'],[code])
        row=next(r for r in self.sku.state()['rows'] if r['sku']==code)
        self.assertEqual(row['status'],'已拍摄');self.assertTrue(row['completed_at']);self.assertTrue(row['sku_image'])
        self.sku.deliver_events(self.archive)
        self.assertEqual(self.archive.query(code)['total'],2)
        return code

    def test_background_rule_confirms_even_when_history_archive_delivery_fails(self):
        self.background_rule_with_postcommit_failure('deliver_events',sqlite3.IntegrityError('UNIQUE constraint failed: segments.job'))

    def test_background_rule_confirms_even_when_workbook_export_fails(self):
        self.background_rule_with_postcommit_failure('export',PermissionError('workbook is open'))
        self.assertIn(self.pid,self.sku.dirty)

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
