import copy
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from model_providers import Providers
from sku_service import SkuService
from sku_library import browse,labels
from test_sku_workflow import fixture_book,CODE
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class OcrMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos);self.ids=[]
        for i in range(4):
            p=self.photos/f'{i}.JPG';Image.new('RGB',(160,120),(40+i*20,80,100)).save(p)
            r=media.capture_record(self.photos,p,None,{'DateTimeOriginal':f'2026:10:21 10:00:0{i}'});self.store.append_live(self.sid,[r]);self.ids.append(r['id'])
        self.store.activate(self.sid);session=self.store.session(self.sid);state=copy.deepcopy(session['state']);state['overrides']={self.ids[1]:2,self.ids[3]:2};self.store.save(self.sid,session['revision'],state)
        self.providers=Providers(self.store.data);self.providers.save({'name':'OCR','category':'ocr','protocol':'chat','url':'http://127.0.0.1:1','model':'test','select':True})
        self.archive=AssetArchive(self.store,autostart=False)
        self.reading={'text':CODE+'\n261009 G82\nSIZE:S\n70%COTTON\n红色棉布','codes':[CODE],'ocr_candidates':[CODE,'261009 G82'],'auxiliary':{'颜色':'红色','size':'S','material':'70%COTTON'},'confidence':0,'unscored':True}
        self.sku=SkuService(self.store,self.providers,autostart=False,archive=self.archive,require_sku_choice=True,recognizer=lambda *_:copy.deepcopy(self.reading));self.archive.sku=self.sku
        self.sku.set_sku_rules([])  # 本组覆盖关闭规则后的手动确认流程；自动规则见 test_sku_rules。
        self.pid=self.sku.import_file('plan.xlsx',fixture_book())['project']['id'];self.sku.configure(self.pid,self.sid,'模特图')

    def tearDown(self):self.sku.close();self.archive.close();self.temp.cleanup()
    def parse(self):return self.sku.parse_capture(self.sid,self.ids[1])
    def choose(self,code=CODE,cid=None):
        cid=cid or self.ids[1];d=self.sku.ocr_details(self.sid,cid)
        return self.sku.choose_ocr_sku(self.sid,cid,code,d['reading_key'],d['image_key'])

    def test_raw_fields_persist_and_search_before_choice_without_becoming_skus(self):
        self.parse();self.assertEqual(labels(self.sku,self.sid)['labels'],{})
        for query in ['G82','COTTON','红色','棉','70%']:
            result=browse(self.sku,search=query);self.assertEqual(result['total'],1,query);self.assertEqual(result['groups'],[])
        d=self.sku.ocr_details(self.sid,self.ids[1]);self.assertIn('261009 G82',d['candidates']);self.assertEqual(d['readings'][0]['auxiliary']['size'],'S')
        self.assertEqual(browse(self.sku,search='" OR anything*')['total'],0)
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM ocr_documents').fetchone()[0],1)

    def test_user_choice_labels_interval_and_auxiliary_search_keeps_correct_primary(self):
        self.parse();self.choose();mapping=labels(self.sku,self.sid)['labels']
        self.assertEqual(mapping,{self.ids[0]:CODE,self.ids[1]:CODE})
        result=browse(self.sku,search='261009 G82');self.assertEqual(result['total'],2);self.assertEqual([r['sku'] for r in result['groups']],[CODE])
        inherited=self.sku.ocr_details(self.sid,self.ids[0]);self.assertEqual(inherited['target_capture_id'],self.ids[1]);self.assertIn('COTTON',inherited['text'])
        self.reading['text']='新辅助资料 BLUE';self.parse()
        self.assertEqual(labels(self.sku,self.sid)['labels'],mapping)
        self.assertEqual(browse(self.sku,search='COTTON')['total'],2);self.assertEqual(browse(self.sku,search='BLUE')['total'],2)
        self.choose('CORRECT-900',self.ids[0]);self.assertEqual(labels(self.sku,self.sid)['labels'][self.ids[1]],'CORRECT-900')
        self.assertEqual(browse(self.sku,search='G82')['groups'][0]['sku'],'CORRECT-900')
        from capture_parsing import capture_status
        self.assertEqual(capture_status(self.store,self.sku,self.archive,None,self.sid,self.ids[1])['ocr']['codes'],['CORRECT-900'])

    def test_old_reading_or_changed_image_cannot_confirm(self):
        self.parse();old=self.sku.ocr_details(self.sid,self.ids[1]);self.reading['text']+='\n更新';self.parse()
        with self.assertRaisesRegex(ValueError,'已更新'):self.sku.choose_ocr_sku(self.sid,self.ids[1],CODE,old['reading_key'],old['image_key'])
        d=self.sku.ocr_details(self.sid,self.ids[1]);Image.new('RGB',(160,120),'red').save(self.photos/'1.JPG')
        with self.assertRaisesRegex(ValueError,'已更新'):self.sku.choose_ocr_sku(self.sid,self.ids[1],CODE,d['reading_key'],d['image_key'])

    def test_full_auto_saves_ocr_without_committing_until_user_chooses(self):
        self.sku.set_full_auto(True,self.sid);self.sku.tick()
        job=next(j for j in self.sku.state()['jobs'] if j['capture_id']==self.ids[1]);self.assertEqual(job['status'],'review');self.assertTrue(job['result']['requires_sku_choice'])
        self.assertEqual(self.archive.query(CODE)['total'],0);self.assertEqual(labels(self.sku,self.sid)['labels'],{})
        self.choose();self.assertEqual(self.archive.query(CODE)['total'],2)
        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['selected_sku'],CODE)

    def test_first_choice_archives_products_before_label_then_next_interval_and_keeps_tail(self):
        self.parse();self.choose()
        self.assertEqual(self.archive.query(CODE)['total'],2)
        self.assertEqual(labels(self.sku,self.sid)['labels'],{self.ids[0]:CODE,self.ids[1]:CODE})
        with self.archive.connect() as c:
            first=[dict(r) for r in c.execute('SELECT * FROM assets WHERE sku=?',(CODE,))]
            self.assertEqual(len(first),2)
            self.assertTrue(all(r['segment'] and r['source']=='segment' for r in first))
            initial=c.execute('SELECT count(*) FROM operations').fetchone()[0]
        self.choose()
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],initial)
        self.sku.parse_capture(self.sid,self.ids[3]);self.choose('NEXT-200',self.ids[3])
        self.assertEqual(self.archive.query(CODE)['total'],2);self.assertEqual(self.archive.query('NEXT-200')['total'],2)
        self.assertEqual(labels(self.sku,self.sid)['labels'],dict(zip(self.ids,[CODE,CODE,'NEXT-200','NEXT-200'])))
        path=self.photos/'4.JPG';Image.new('RGB',(160,120),'navy').save(path)
        record=media.capture_record(self.photos,path,None,{'DateTimeOriginal':'2026:10:21 10:00:04'});self.store.append_live(self.sid,[record]);self.archive.sync_stream()
        self.assertNotIn(record['id'],labels(self.sku,self.sid)['labels'])
        self.assertEqual(self.archive.asset(self.archive.ensure(self.sid,record['id']))['sku'],'')
        # 修正整款时复用区间和永久资产 ID，未确认的尾部不跟随。
        self.choose('CORRECT-100',self.ids[0]);self.assertEqual(self.archive.query('CORRECT-100')['total'],2)
        self.assertEqual(self.archive.query(CODE)['total'],0)

    def test_choice_keeps_manual_product_assignment(self):
        self.archive.apply(self.archive.preview(self.sid,[self.ids[0]],'MANUAL-900','模特图',self.pid)['preview_id'])
        self.parse();self.choose()
        self.assertEqual(self.archive.query('MANUAL-900')['total'],1)
        self.assertEqual(self.archive.query(CODE)['total'],1)
        self.sku.parse_capture(self.sid,self.ids[3]);self.choose('NEXT-200',self.ids[3])
        self.assertEqual(self.archive.query('NEXT-200')['total'],2)
        self.assertEqual(self.archive.query('MANUAL-900')['total'],1)

    def test_unknown_first_label_is_not_absorbed_by_next_choice(self):
        self.sku.parse_capture(self.sid,self.ids[3]);self.choose('NEXT-200',self.ids[3])
        self.assertEqual(self.archive.query('NEXT-200')['total'],2)
        self.assertEqual(labels(self.sku,self.sid)['labels'],{self.ids[2]:'NEXT-200',self.ids[3]:'NEXT-200'})
        for cid in self.ids[:2]:self.assertEqual(self.archive.asset(self.archive.ensure(self.sid,cid))['sku'],'')
        self.parse();self.choose()
        self.assertEqual(self.archive.query(CODE)['total'],2);self.assertEqual(self.archive.query('NEXT-200')['total'],2)

    def test_reconfirm_old_single_tag_assignment_fills_products_and_allows_correcting_tag(self):
        self.archive.apply(self.archive.preview(self.sid,[self.ids[1]],CODE,'模特图',self.pid)['preview_id'])
        self.parse();self.choose()
        self.assertEqual(self.archive.query(CODE)['total'],2)
        self.choose('CORRECT-100')
        self.assertEqual(self.archive.query('CORRECT-100')['total'],2)
        self.assertEqual(self.archive.query(CODE)['total'],0)

    def test_persisted_choice_event_recovers_after_interrupted_delivery(self):
        self.parse()
        with patch.object(self.sku,'deliver_events',side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError,'interrupted'):self.choose()
        self.assertEqual(self.archive.query(CODE)['total'],0)
        reopened=SkuService(self.store,self.providers,autostart=False,archive=self.archive,require_sku_choice=True)
        try:
            self.archive.sku=reopened;reopened.deliver_events(self.archive)
            self.assertEqual(self.archive.query(CODE)['total'],2)
            with self.archive.connect() as c:count=c.execute('SELECT count(*) FROM operations').fetchone()[0]
            reopened.deliver_events(self.archive)
            with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],count)
        finally:self.archive.sku=self.sku;reopened.close()

    def test_inflight_ocr_keeps_auxiliary_after_user_has_confirmed_primary(self):
        self.parse();self.sku.set_full_auto(True,self.sid);entered=threading.Event();release=threading.Event()
        self.sku._queue_full_auto()
        with self.sku.connect() as c:c.execute('UPDATE jobs SET priority=? WHERE capture_id=?',(self.sku.clock()+1,self.ids[1]))
        def read(*_):
            entered.set();release.wait(5);return {'text':'新增辅助 BLUE','codes':['WRONG-900'],'confidence':0,'unscored':True}
        self.sku.recognizer=read;worker=threading.Thread(target=self.sku.tick);worker.start()
        try:
            self.assertTrue(entered.wait(3));self.choose()
        finally:release.set();worker.join(8)
        self.assertFalse(worker.is_alive());self.assertEqual(labels(self.sku,self.sid)['labels'][self.ids[1]],CODE)
        self.assertEqual(browse(self.sku,search='BLUE')['total'],2)

    def test_browser_recommendations_apply_directly_and_protect_stale_context(self):
        import server
        alternative='LC7877496-P804';self.reading['codes']=[CODE,alternative]
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
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepOcrDetails')
                        page.locator('[data-watch-photo="'+self.ids[1]+'"]').click()
                        page.locator('.asset-panel').evaluate('e=>e.open=true')
                        def parse():
                            page.locator('#asset-parse-sku').click()
                            page.wait_for_function('()=>!document.getElementById("asset-parse-sku").disabled&&document.querySelectorAll("#asset-ocr-codes [data-ocr-code]").length===2&&!document.getElementById("asset-ocr-result").hidden')
                        def chosen(code):
                            page.wait_for_function('(code)=>document.querySelector("#asset-ocr-codes [data-ocr-code=\\""+code+"\\"]").getAttribute("aria-pressed")==="true"',arg=code)
                        parse();self.assertTrue(page.locator('#asset-ocr-result details').evaluate('e=>e.open'))
                        self.assertEqual(labels(self.sku,self.sid)['labels'],{})
                        routes=[];page.route('**/api/sku/ocr/choose',lambda route:routes.append(route))
                        button=page.locator('#asset-ocr-codes [data-ocr-code="'+CODE+'"]');button.click();page.wait_for_function('()=>document.getElementById("asset-ocr-result").getAttribute("aria-busy")==="true"')
                        self.assertTrue(button.is_disabled());self.assertTrue(page.locator('#asset-ocr-codes [data-ocr-code="'+alternative+'"]').is_disabled())
                        button.evaluate('e=>e.click()');self.assertEqual(len(routes),1)
                        routes.pop().continue_();chosen(CODE)
                        self.assertEqual(labels(self.sku,self.sid)['labels'],{self.ids[0]:CODE,self.ids[1]:CODE})
                        self.assertFalse(page.locator('#ocr-metadata-dialog').evaluate('e=>e.open'))
                        self.assertEqual(page.locator('#asset-sku').input_value(),CODE)
                        self.assertIn('已应用 SKU',page.locator('#asset-message').inner_text())
                        self.assertIn('COTTON',self.sku.ocr_details(self.sid,self.ids[1])['text'])
                        page.locator('#asset-ocr-result summary').click();parse()
                        self.assertTrue(page.locator('#asset-ocr-result details').evaluate('e=>e.open'))
                        # 后台 OCR 更新后，不能拿旧推荐快照提交。
                        self.reading['text']+='\n更新资料';self.parse()
                        page.locator('#asset-ocr-codes [data-ocr-code="'+alternative+'"]').click();routes.pop().continue_()
                        page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("已更新")')
                        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['selected_sku'],CODE)
                        self.assertFalse(button.is_disabled())
                        # 明确点击仍应用到原图，但不覆盖请求期间新输入的草稿。
                        parse();page.locator('#asset-ocr-codes [data-ocr-code="'+alternative+'"]').click()
                        page.locator('#asset-sku').fill('MANUAL-900');routes.pop().continue_();chosen(alternative)
                        self.assertEqual(page.locator('#asset-sku').input_value(),'MANUAL-900')
                        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['selected_sku'],alternative)
                        # 途中切图，不将原图的结果填入新图或打开弹窗。
                        button.click();page.locator('[data-watch-photo="'+self.ids[2]+'"]').click();routes.pop().continue_()
                        page.wait_for_function('()=>document.getElementById("asset-ocr-result").getAttribute("aria-busy")===null')
                        self.assertTrue(page.locator('#asset-ocr-result').is_hidden());self.assertEqual(page.locator('#asset-sku').input_value(),'')
                        self.assertNotIn(self.ids[2],labels(self.sku,self.sid)['labels'])
                        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['selected_sku'],CODE)
                        page.locator('[data-watch-photo="'+self.ids[1]+'"]').click();parse();page.unroute('**/api/sku/ocr/choose')
                        out=ROOT/'build/ocr-quick-apply';out.mkdir(parents=True,exist_ok=True)
                        for theme in ['dark','light','pure-white','hardcore']:
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            for width in [1440,1024,375]:
                                page.set_viewport_size({'width':width,'height':1000})
                                self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'),(theme,width))
                                self.assertTrue(button.evaluate('e=>e.scrollWidth<=e.clientWidth'))
                                page.locator('.asset-panel').screenshot(path=str(out/f'{theme}-{width}.png'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()

    def test_browser_chooses_primary_and_searches_auxiliary_text(self):
        import server
        self.parse();catalog=AssetCatalog(self.store.data);http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=catalog,ANALYSIS=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepOcrDetails')
                        page.locator('[data-watch-photo="'+self.ids[1]+'"]').click()
                        page.locator('#asset-ocr-details').evaluate("e=>{for(let p=e.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS')p.open=true}")
                        page.locator('#asset-ocr-details').click();page.wait_for_selector('[data-ocr-pick="261009 G82"]')
                        page.locator('#ocr-metadata-candidates [data-ocr-pick="'+CODE+'"]').click();page.locator('#ocr-metadata-save').click();page.wait_for_function('()=>!document.getElementById("ocr-metadata-dialog").open')
                        page.wait_for_function('()=>document.querySelectorAll(".watch-sku-label").length===2')
                        page.locator('#sku-database-open').click();page.locator('#sku-database-search').fill('G82');page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===2')
                        page.locator('#sku-database-grid [data-ocr-capture]').first.click();page.wait_for_function('()=>document.getElementById("ocr-metadata-text").textContent.includes("COTTON")')
                        self.assertEqual(page.locator('#ocr-metadata-sku').input_value(),CODE)
                        out=ROOT/'build/ocr-repair/interval-labels';out.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(out/'ocr-search-choice.png'),full_page=True)
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
