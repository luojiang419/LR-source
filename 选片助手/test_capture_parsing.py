"""底栏解析摘要：真实持久化状态、切图隔离、只读查询和布局。"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from asset_analysis import AssetAnalysis
from catalog_store import Store
from capture_parsing import capture_status
from model_providers import Providers
from sku_service import SkuService
from vision_scheduler import VisionScheduler
import media_engine as media
import server

ROOT=Path(__file__).resolve().parents[1]


class CaptureParsingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        photos=self.base/'photos';photos.mkdir();self.store=Store(self.base/'data',ROOT)
        self.sid=self.store.ensure_live_session(photos);self.ids=[]
        for i in range(2):
            path=photos/f'{i}.JPG';Image.new('RGB',(120,180),(60+i*50,90,120)).save(path)
            record=media.capture_record(photos,path,None,{'DateTimeOriginal':f'2026:10:21 10:00:0{i}'})
            self.store.append_live(self.sid,[record]);self.ids.append(record['id'])
        self.store.activate(self.sid);self.providers=Providers(self.store.data)
        self.archive=AssetArchive(self.store,autostart=False);self.scheduler=VisionScheduler()
        self.sku=SkuService(self.store,self.providers,autostart=False)
        self.analysis=AssetAnalysis(self.archive,self.providers,self.scheduler,autostart=False)

    def tearDown(self):
        self.sku.close();self.analysis.close();self.archive.close();self.temp.cleanup()

    def status(self,cid):return capture_status(self.store,self.sku,self.archive,self.analysis,self.sid,cid)

    def complete(self):
        with self.sku.connect() as c:
            c.execute('INSERT INTO parse_jobs(id,session_id,capture_id,status,created,result) VALUES (?,?,?,?,?,?)',('ocr-1',self.sid,self.ids[0],'completed',1,json.dumps({'codes':['XX-XX-154']})))
        with self.archive.connect() as c:
            c.execute('INSERT INTO assets(id,created) VALUES (?,?)',('asset-1',1))
            c.execute('INSERT INTO catalog_links VALUES (?,?,?)',(self.sid,self.ids[0],'asset-1'))
        with self.analysis.connect() as c:
            c.execute('INSERT INTO jobs(id,asset,status,created) VALUES (?,?,?,?)',('vision-1','asset-1','completed',1))

    def test_status_reads_older_results_without_creating_assets_or_running_models(self):
        self.assertEqual(self.status(self.ids[0])['ocr']['status'],'unparsed')
        with self.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM assets').fetchone()[0],0)
        self.complete()
        with self.sku.connect() as c:
            c.executemany('INSERT INTO parse_jobs(id,session_id,capture_id,status,created) VALUES (?,?,?,?,?)',[(f'other-{i}',self.sid,self.ids[1],'completed',100+i) for i in range(105)])
        with patch.object(self.providers,'check_inference',side_effect=AssertionError('状态查询不能发起模型请求')):
            result=self.status(self.ids[0])
        self.assertEqual(result['ocr']['codes'],['XX-XX-154'])
        self.assertEqual(result['multimodal']['status'],'completed')
        with self.sku.connect() as c:c.execute("UPDATE parse_jobs SET status='failed',reason='测试错误' WHERE id='ocr-1'")
        self.assertEqual(self.status(self.ids[0])['ocr']['status'],'failed')
        self.assertEqual(self.status(self.ids[1])['multimodal']['status'],'unparsed')
        with self.assertRaises(ValueError):self.status('missing')

    def test_footer_follows_current_photo_polling_states_and_four_themes(self):
        self.complete()
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values={'STORE':self.store,'DATA':self.store.data,'SKU':self.sku,'ARCHIVE':self.archive,'ANALYSIS':self.analysis,'MODELS':self.providers,'WATCH':None,'MOUSE':None,'RAWLINK':None,'FILESEARCH':None,'UPDATES':None,'CATALOG':None}
        patches=[patch.object(server,k,v) for k,v in values.items()]
        for item in patches:item.start()
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                    page.locator('[data-focus="'+self.ids[0]+'"]').first.click();page.locator('[data-view="single"]').click()
                    page.wait_for_function('()=>document.getElementById("capture-sku-result").textContent==="SKU:XX-XX-154"')
                    self.assertEqual(page.locator('#capture-ocr-status').inner_text(),'OCR 已解析')
                    self.assertEqual(page.locator('#capture-multimodal-status').get_attribute('data-status'),'completed')
                    boxes=[page.locator(s).bounding_box() for s in ('#loupe-name','#capture-parsing-status')]
                    self.assertGreater(boxes[1]['x'],boxes[0]['x'])
                    page.locator('#next').click()
                    page.wait_for_function('()=>document.getElementById("capture-multimodal-status").dataset.status==="unparsed"')
                    self.assertEqual(page.locator('#capture-sku-result').inner_text(),'SKU:—')
                    page.locator('#previous').click()
                    page.wait_for_function('()=>document.getElementById("capture-sku-result").textContent==="SKU:XX-XX-154"')
                    with self.analysis.connect() as c:c.execute("UPDATE jobs SET status='running' WHERE id='vision-1'")
                    page.wait_for_function('()=>document.getElementById("capture-multimodal-status").dataset.status==="running"')
                    for theme in ('dark','light','pure-white','hardcore'):
                        page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                        for width in (1680,1024,375):
                            page.set_viewport_size({'width':width,'height':900})
                            self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width))
                    page.set_viewport_size({'width':1680,'height':1050})
                    out=ROOT/'build/capture-parsing';out.mkdir(exist_ok=True,parents=True);page.screenshot(path=str(out/'footer.png'))
                    page.route('**/api/parsing/capture?*',lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"测试断连"}'))
                    page.wait_for_function('()=>document.getElementById("capture-ocr-status").textContent==="OCR 状态不可用"')
                    self.assertEqual(page.locator('#capture-sku-result').inner_text(),'SKU:—')
                    self.assertFalse(errors,errors)
                finally:browser.close()
        finally:
            http.shutdown();http.server_close()
            for item in reversed(patches):item.stop()
