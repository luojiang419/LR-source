from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from test_raw_reconcile import RawWorkflowTests as _RawFixture

ROOT=Path(__file__).resolve().parents[1]

class RawBrowserTests(_RawFixture):
    def test_archive_raw_folder_sync_uses_deleted_jpg_record(self):
        import server
        self.archive.thread.start();self.link.thread.start()
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        patches=[patch.object(server,key,value) for key,value in [('STORE',self.store),('DATA',self.store.data),('ARCHIVE',self.archive),('RAWLINK',self.link),('SKU',None),('MODELS',None),('ANALYSIS',None),('WATCH',None),('MOUSE',None)]]
        for p in patches:p.start()
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                    (self.jpg/'IMG_0346.JPG').unlink()
                    page.once('dialog',lambda d:d.accept(str(self.raw)));page.locator('#raw-sync-browse').click()
                    page.wait_for_function('()=>!document.getElementById("raw-sync-start").disabled');page.locator('#raw-sync-start').click()
                    page.wait_for_function('()=>document.getElementById("raw-sync-summary").textContent==="已同步 1"')
                    self.assertNotIn('后拷RAW',page.evaluate('FRAMEKEEP_SESSION.root'))
                    fields=self.archive.writer.read(self.target);self.assertEqual(fields['XMP-fk:SKU'],'SKU-远程123');self.assertEqual(fields['XMP-xmp:Rating'],4)
                    self.assertTrue(page.locator('#raw-sync-open').is_hidden())
                    out=ROOT/'build/raw-reconcile';page.screenshot(path=str(out/'raw-sync-browser.png'));self.assertFalse(errors,errors)
                    page.reload(wait_until='networkidle');page.wait_for_function('()=>document.getElementById("raw-sync-summary").textContent==="已同步 1"')
                finally:browser.close()
        finally:
            http.shutdown();http.server_close()
            for p in reversed(patches):p.stop()

for _name in list(_RawFixture.__dict__):
    if _name.startswith('test_'):setattr(RawBrowserTests,_name,None)
del _RawFixture
