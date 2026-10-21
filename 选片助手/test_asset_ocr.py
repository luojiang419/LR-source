import copy
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
from catalog_store import Store
import media_engine as media
from metadata_writer import sha
from model_providers import Providers
from sku_service import SkuService
from test_sku_browser import Upstream

ROOT=Path(__file__).resolve().parents[1]


class ManualOcrTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos);self.store.activate(self.sid)
        for i in range(2):
            path=self.photos/f'{i}.JPG';Image.new('RGB',(100,80),(80+i*20,100,120)).save(path)
            self.store.append_live(self.sid,[media.capture_record(self.photos,path,None,{})])
        self.ids=[r['id'] for r in self.store.records(self.sid)];self.providers=Providers(self.store.data)
        self.provider=self.providers.save({'name':'OCR','category':'ocr','protocol':'chat','url':'http://127.0.0.1:1','model':'ocr','select':True})['providers'][0]['id']
        self.calls=[]
        def recognize(p,image):
            self.calls.append(p['id']);return {'text':'LCJ81166','codes':['LCJ81166'],'confidence':0,'unscored':True,'model':'ocr'}
        self.sku=SkuService(self.store,self.providers,recognizer=recognize,autostart=False)
    def tearDown(self):self.sku.close();self.temp.cleanup()
    def test_manual_read_needs_no_stars_or_workbook_and_changes_no_business_state(self):
        before=copy.deepcopy(self.store.session(self.sid));hashes=[sha(p) for p in self.photos.iterdir()]
        result=self.sku.parse_capture(self.sid,self.ids[0]);self.assertEqual(result['reading']['codes'],['LCJ81166'])
        self.assertEqual(self.calls,[self.provider]);self.assertEqual(self.store.session(self.sid),before)
        self.assertEqual(hashes,[sha(p) for p in self.photos.iterdir()]);self.assertEqual(self.sku.state()['jobs'],[]);self.assertEqual(self.sku.state()['projects'],[])
    def test_missing_model_invalid_capture_and_changed_source_do_not_return_a_draft(self):
        with self.assertRaisesRegex(ValueError,'当前照片'):self.sku.parse_capture(self.sid,'missing')
        self.providers.delete(self.provider)
        with self.assertRaisesRegex(ValueError,'选用 OCR'):self.sku.parse_capture(self.sid,self.ids[0])
        self.providers.save({'name':'vision','category':'multimodal','protocol':'chat','url':'http://127.0.0.1:1','model':'test','select':True})
        def changed(p,image):
            Image.new('RGB',(200,100),'red').save(self.photos/'0.JPG');return {'codes':['WRONG123']}
        self.sku.recognizer=changed
        with self.assertRaisesRegex(ValueError,'照片发生变化'):self.sku.parse_capture(self.sid,self.ids[0])
    def test_asset_panel_fill_candidates_edit_and_photo_switch_races(self):
        import server
        self.providers.delete(self.provider)
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream);Upstream.calls=[];Upstream.codes=['LCJ81166'];Upstream.confidence=.6;Upstream.gate=None
        threading.Thread(target=upstream.serve_forever,daemon=True).start()
        self.sku.recognizer=__import__('sku_vision').recognize;archive=AssetArchive(self.store,autostart=False)
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        patches=[patch.object(server,k,v) for k,v in [('STORE',self.store),('DATA',self.store.data),('MODELS',self.providers),('SKU',self.sku),('ARCHIVE',archive),('ANALYSIS',None),('WATCH',None),('MOUSE',None),('RAWLINK',None),('FILESEARCH',None)]]
        for item in patches:item.start()
        threading.Thread(target=http.serve_forever,daemon=True).start();hashes=[sha(p) for p in self.photos.iterdir()]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepAssets&&window.FramekeepLayout')
                    page.locator('.asset-panel').evaluate('(e)=>e.open=true');page.locator('#asset-parse-sku').click()
                    page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("选用 OCR")')
                    self.providers.save({'name':'OCR','category':'ocr','protocol':'chat','url':f'http://127.0.0.1:{upstream.server_port}/v1','model':'ocr','select':True})
                    page.locator('#asset-parse-sku').click();page.wait_for_function('()=>document.getElementById("asset-sku").value==="LCJ81166"')
                    self.assertTrue(page.locator('#asset-ocr-result').is_visible());self.assertEqual(self.store.session(self.sid)['state'].get('overrides',{}),{})
                    page.wait_for_timeout(1900);self.assertEqual(page.locator('#asset-sku').input_value(),'LCJ81166')
                    Upstream.codes=['SKU1111','SKU2222'];page.locator('#asset-parse-sku').click()
                    page.wait_for_function('()=>document.querySelectorAll("[data-ocr-code]").length===2');self.assertEqual(page.locator('#asset-sku').input_value(),'LCJ81166')
                    page.locator('[data-ocr-code="SKU2222"]').click();self.assertEqual(page.locator('#asset-sku').input_value(),'SKU2222')
                    Upstream.codes=[];page.locator('#asset-parse-sku').click();page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("未读出")')
                    self.assertEqual(page.locator('#asset-sku').input_value(),'SKU2222')
                    Upstream.codes=['LATE1234'];Upstream.gate=threading.Event();page.locator('#asset-parse-sku').click()
                    page.wait_for_function('()=>document.getElementById("asset-parse-sku").textContent.includes("解析中")');page.locator('#asset-sku').fill('MANUAL123');Upstream.gate.set()
                    page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("手动修改")');self.assertEqual(page.locator('#asset-sku').input_value(),'MANUAL123')
                    Upstream.gate=threading.Event();page.locator('#asset-parse-sku').click();page.wait_for_function('()=>document.getElementById("asset-parse-sku").textContent.includes("解析中")')
                    page.locator('#photo-grid [data-focus]').nth(1).click();Upstream.gate.set()
                    page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("图片选择已变化")');self.assertTrue(page.locator('#asset-ocr-result').is_hidden());self.assertEqual(page.locator('#asset-sku').input_value(),'')
                    Upstream.gate=None;page.locator('#asset-parse-sku').click();page.wait_for_function('()=>document.getElementById("asset-sku").value==="LATE1234"')
                    out=ROOT/'build/ocr-validation';out.mkdir(exist_ok=True)
                    for theme in ['dark','light','pure-white','hardcore']:
                        page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                        for width in [1440,1024,375]:
                            page.set_viewport_size({'width':width,'height':1000});page.locator('.asset-panel').evaluate('(e)=>e.open=true')
                            page.locator('#asset-parse-sku').scroll_into_view_if_needed();self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'),(theme,width))
                            self.assertTrue(page.locator('#asset-parse-sku').is_visible());page.locator('.asset-panel').screenshot(path=str(out/f'asset-ocr-{theme}-{width}.png'))
                    self.assertFalse(errors,errors);self.assertEqual(self.sku.state()['jobs'],[]);self.assertEqual(self.sku.state()['projects'],[]);self.assertEqual(hashes,[sha(p) for p in self.photos.iterdir()])
                    (out/'asset-ocr-browser.json').write_text(json.dumps({'passed':True,'errors':errors,'model_calls':len(Upstream.calls),'checks':['no stars or workbook','missing model','single draft fill','multiple candidate choice','empty result keeps text','manual edit protected','photo switch protected','four themes three widths','photos and rating unchanged']},ensure_ascii=False,indent=2),encoding='utf8')
                finally:browser.close()
        finally:
            if Upstream.gate:Upstream.gate.set()
            archive.close();http.shutdown();http.server_close();upstream.shutdown();upstream.server_close()
            for item in reversed(patches):item.stop()


if __name__=='__main__':unittest.main()
