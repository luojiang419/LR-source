from http.server import ThreadingHTTPServer
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from catalog_store import Store
from metadata_writer import sha
from model_providers import Providers
from sku_service import SkuService
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class NoDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos);self.store.activate(self.sid)
        for i in range(3):
            path=self.photos/f'{i}.JPG';Image.new('RGB',(120,80),(40+i*30,90,120)).save(path)
            self.store.append_live(self.sid,[media.capture_record(self.photos,path,None,{})])
        self.providers=Providers(self.store.data);self.providers.save({'name':'local fixture','category':'ocr','protocol':'chat','url':'https://example.invalid/v1','model':'fixture','select':True})
        self.archive=AssetArchive(self.store,autostart=False);self.clock=[100.];self.calls=[]
        def read(*_):self.calls.append(1);return {'codes':['FREE-100'],'text':'FREE-100\n蓝色棉布','unscored':True,'confidence':0}
        self.sku=SkuService(self.store,self.providers,archive=self.archive,clock=lambda:self.clock[0],recognizer=read,autostart=False,require_sku_choice=True);self.archive.sku=self.sku
    def tearDown(self):self.sku.close();self.archive.close();self.temp.cleanup()
    def tick(self,n):
        for _ in range(n):self.clock[0]+=3;self.sku.tick()
    def test_no_document_can_enable_scan_and_restart_without_rereading(self):
        self.assertIsNone(self.sku.project());state=self.sku.full_auto_state();self.assertTrue(state['can_enable']);self.assertTrue(state['without_document'])
        before=[sha(p) for p in sorted(self.photos.glob('*.JPG'))]
        self.assertTrue(self.sku.set_full_auto(True,self.sid)['enabled']);self.tick(3)
        self.assertEqual(len(self.calls),3);self.assertTrue(self.sku.project()['metadata']['automatic_day'])
        self.assertTrue(all(job['status']=='review' for job in self.sku.state()['jobs']))
        for record in self.store.records(self.sid):self.assertIn('蓝色棉布',self.sku.ocr_details(self.sid,record['id'])['text'])
        self.assertEqual(self.archive.query('FREE-100')['total'],0);self.assertEqual(before,[sha(p) for p in sorted(self.photos.glob('*.JPG'))])
        reopened=SkuService(self.store,self.providers,archive=self.archive,recognizer=lambda *_:self.fail('不应重新扫描'),clock=lambda:self.clock[0],autostart=False,require_sku_choice=True)
        try:self.assertTrue(reopened.full_auto_state()['enabled']);reopened.tick()
        finally:reopened.close()
        path=self.photos/'new.JPG';Image.new('RGB',(120,80),'red').save(path);self.store.append_live(self.sid,[media.capture_record(self.photos,path,None,{})]);self.tick(1);self.assertEqual(len(self.calls),4)
    def test_missing_folder_cannot_create_or_enable_context(self):
        with self.assertRaisesRegex(ValueError,'照片目录'):self.sku.set_full_auto(True,'missing')
        self.assertIsNone(self.sku.project())
    def test_browser_enables_without_import_from_both_synced_switches(self):
        import server
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        self.assertTrue(page.locator('#shooting-full-auto').is_enabled());page.locator('#shooting-full-auto').check()
                        page.wait_for_function('()=>document.getElementById("shooting-auto-status").textContent.includes("全自动已开启")')
                        self.tick(3);self.assertEqual(len(self.calls),3)
                        page.locator('#app-settings-open').click();self.assertTrue(page.locator('#full-auto-ocr').is_checked())
                        self.assertIn('无需导入文档',page.locator('#full-auto-ocr-help').inner_text());page.locator('#full-auto-ocr').uncheck()
                        page.wait_for_function('()=>!FramekeepAutomation.isSaving()&&!document.getElementById("shooting-full-auto").checked')
                        self.assertFalse(self.sku.full_auto_state()['enabled']);self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
