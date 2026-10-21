from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from asset_archive import AssetArchive
from asset_analysis import AssetAnalysis
from model_providers import Providers
from vision_scheduler import VisionScheduler
from everything_search import EverythingSearch
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class SearchBrowserTests(unittest.TestCase):
    def test_cursor_navigation_filters_and_everything_fallback(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT)
            sid=store.ensure_live_session(photos);store.activate(sid)
            image=photos/'IMG001.JPG';Image.new('RGB',(80,60),'orange').save(image)
            store.append_live(sid,[media.capture_record(photos,image,None,{})])
            archive=AssetArchive(store,autostart=False);providers=Providers(store.data);analysis=AssetAnalysis(archive,providers,VisionScheduler(),autostart=False)
            with archive.connect() as c:
                c.executemany('INSERT INTO assets(id,created,sku,normalized,scope) VALUES (?,?,?,?,?)',[(f'a{i:03d}',float(i),'SKU1234','SKU1234','细节图') for i in range(145)])
                c.execute("INSERT INTO files(id,asset,component) VALUES ('f','a144','jpg')")
                c.execute('INSERT INTO locations VALUES (?,?,?,?)',(str(image),'f',1.,'online'))
            service=EverythingSearch(archive)
            response={'available':True,'version':'1.4.1.test','paths':[str(image),str(photos/'unregistered.JPG')],'has_more':False}
            service.cached={'available':True,'version':'1.4.1.test'}
            import time
            service.checked=time.monotonic()
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('MODELS',providers),('ARCHIVE',archive),('ANALYSIS',analysis),('SKU',None),('WATCH',None),('MOUSE',None),('RAWLINK',None),('FILESEARCH',service)]]
            patches.append(patch.object(service,'_request',side_effect=lambda payload:response.copy()))
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle');page.locator('#asset-archive-open').click()
                        page.wait_for_function('()=>document.querySelectorAll("#archive-results tbody tr").length===60')
                        self.assertTrue(page.locator('#archive-prev').is_disabled());page.locator('#archive-next').click();page.wait_for_function('()=>document.getElementById("archive-page").textContent.includes("第 2 页")')
                        page.locator('#archive-next').click();page.wait_for_function('()=>document.querySelectorAll("#archive-results tbody tr").length===25');self.assertTrue(page.locator('#archive-next').is_disabled())
                        page.locator('#archive-prev').click();page.wait_for_function('()=>document.getElementById("archive-page").textContent.includes("第 2 页")')
                        page.locator('#archive-scope').fill('模特图');page.locator('#archive-search').click();page.wait_for_function('()=>document.querySelectorAll("#archive-results tbody tr").length===0')
                        page.locator('#archive-scope').fill('细节图');page.locator('#archive-match').select_option('exact');page.locator('#archive-sku').fill('sku-1234');page.locator('#archive-search').click();page.wait_for_function('()=>document.querySelectorAll("#archive-results tbody tr").length===60')
                        page.locator('summary',has_text='文件位置搜索').click();page.locator('#file-query').fill('IMG001');page.locator('#file-search').click();page.wait_for_function('()=>document.querySelectorAll("#file-search-results tbody tr").length===2')
                        self.assertIn('未备案候选',page.locator('#file-search-results').inner_text())
                        out=ROOT/'build/search-acceptance';out.mkdir(exist_ok=True);page.screenshot(path=str(out/'search-browser.png'))
                        response.clear();response.update(available=False,message='Everything 已退出')
                        page.locator('#file-search').click();page.wait_for_function('()=>document.getElementById("file-search-message").textContent.includes("备案库回退")');self.assertEqual(page.locator('#file-search-results tbody tr').count(),1)
                        page.locator('#file-search-results [data-asset-detail]').click();page.wait_for_function('()=>document.getElementById("archive-detail").textContent.includes("SKU1234")')
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close();analysis.close();archive.close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
