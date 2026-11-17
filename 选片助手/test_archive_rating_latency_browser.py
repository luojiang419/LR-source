"""隔离页面验证：慢资料轮询不能延长星级保存的忙碌状态。"""
from contextlib import ExitStack
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from playwright.sync_api import sync_playwright
import server
from test_archive_capture_service import ArchiveCaptureTests

class ArchiveRatingBrowserTests(unittest.TestCase):
    setUp=ArchiveCaptureTests.setUp
    tearDown=ArchiveCaptureTests.tearDown
    client=ArchiveCaptureTests.client
    item=ArchiveCaptureTests.item
    drain=ArchiveCaptureTests.drain

    def test_slow_poll_does_not_block_next_rating(self):
        with ExitStack() as stack:
            values=dict(STORE=self.store,CATALOG=self.catalog,ARCHIVE=self.archive,SKU=self.sku,MODELS=self.sku.providers,DATA=self.store.data,WATCH=None,MOUSE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=None)
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000});errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepArchiveOpenItem&&window.FramekeepArchiveCapture')
                        page.evaluate('(id)=>FramekeepArchiveOpenItem(id)',self.item(2))
                        page.wait_for_function("()=>![...document.querySelectorAll('[data-catalog-rating]')].find(b=>b.dataset.catalogRating==='2').disabled")
                        self.assertEqual(page.locator('#catalog-sort').input_value(),'original')
                        with self.catalog.connect() as c:c.execute('UPDATE items SET modified=9999999999 WHERE id=?',(self.item(4),))
                        page.select_option('#catalog-sort','modified')
                        page.wait_for_function("()=>document.querySelector('#catalog-grid .catalog-card-name')?.textContent==='IMG_0004.JPG'")
                        page.select_option('#catalog-sort','original')
                        page.wait_for_function("()=>document.querySelector('#catalog-grid .catalog-card-name')?.textContent==='IMG_0001.JPG'")
                        self.api.rate(self.item(2),2,correction=True)
                        page.evaluate('()=>FramekeepArchiveCapture.refresh()')
                        page.wait_for_function("()=>!document.getElementById('capture-ocr').disabled")
                        page.locator('#capture-ocr').click()
                        page.wait_for_function("()=>document.getElementById('capture-ocr').textContent.includes('正在解析')")
                        self.api.tick()
                        page.wait_for_function("()=>document.getElementById('capture-ocr-text').textContent.includes('LC123456')")
                        self.api.tick()
                        page.wait_for_function("()=>[...document.querySelectorAll('#catalog-grid .catalog-card-sku')].filter(n=>n.textContent==='SKU：LC123456').length===2")
                        page.wait_for_timeout(300)
                        page.evaluate("""(id)=>{const api=FramekeepIO.api;window.__pollStarted=false;FramekeepIO.api=(path,...args)=>{
                            if(String(path).startsWith('/api/archive-capture/state')){window.__pollStarted=true;return new Promise(()=>{})}
                            return api(path,...args)};window.__rateDone=false;
                            FramekeepArchiveCapture.rate(id,2).then(()=>window.__rateDone=true)}""",self.item(2))
                        page.wait_for_function('()=>window.__rateDone',timeout=4000)
                        self.assertTrue(page.evaluate('()=>window.__pollStarted'))
                        self.assertFalse(page.locator('[data-catalog-rating="1"]').is_disabled())
                        page.locator('[data-catalog-rating="1"]').click()
                        page.wait_for_function('()=>document.getElementById("catalog-rating-status").textContent==="已保存 1 星"',timeout=4000)
                        self.assertEqual(self.dataset.record(path='IMG_0002.JPG')['rating'],1)
                        out=Path(__file__).resolve().parents[1]/'build/archive-rating-latency';out.mkdir(exist_ok=True)
                        page.screenshot(path=str(out/'verified.png'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()

    def test_continuous_folder_scroll_shows_all_assets_and_keeps_selection(self):
        import time
        from PIL import Image
        for n in range(260):Image.new("RGB",(32,32),"teal").save(self.photos/f"SCROLL_{n:04d}.JPG")
        self.catalog.scan(self.rid)
        until=time.monotonic()+10
        while time.monotonic()<until:
            with self.catalog.connect() as c:
                if c.execute("SELECT count(*) FROM items").fetchone()[0]==264:break
            time.sleep(.02)
        with ExitStack() as stack:
            values=dict(STORE=self.store,CATALOG=self.catalog,ARCHIVE=self.archive,SKU=self.sku,MODELS=self.sku.providers,DATA=self.store.data,WATCH=None,MOUSE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=None)
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000});errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepArchiveOpenItem&&window.FramekeepArchiveCapture')
                        page.evaluate('(id)=>FramekeepArchiveOpenItem(id)',self.item(2))
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===120")
                        self.assertEqual(page.locator('#catalog-prev,#catalog-next').count(),0)
                        page.locator('#catalog-grid .catalog-card').first.click()
                        page.wait_for_function("()=>!!document.getElementById('catalog-selected-name').textContent")
                        name=page.locator('#catalog-selected-name').inner_text()
                        self.assertTrue(name)
                        page.evaluate("()=>{const g=document.getElementById('catalog-grid');g.scrollTop=g.scrollHeight;g.dispatchEvent(new Event('scroll'))}")
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===240")
                        page.evaluate("()=>{const g=document.getElementById('catalog-grid');g.scrollTop=g.scrollHeight;g.dispatchEvent(new Event('scroll'))}")
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===264")
                        self.assertEqual(page.locator('#catalog-selected-name').inner_text(),name)
                        page.evaluate("()=>document.dispatchEvent(new CustomEvent('framekeep:metadata-imported'))")
                        page.wait_for_timeout(500)
                        self.assertEqual(page.locator('#catalog-grid .catalog-card').count(),264)
                        ids=page.locator('#catalog-grid .catalog-card').evaluate_all('(cards)=>cards.map(n=>n.dataset.catalogItem)')
                        with self.catalog.connect() as c:
                            c.executemany("UPDATE items SET sku='GROUP-TAIL',source='portable' WHERE id=?",[(id,) for id in ids[130:230]])
                        page.evaluate("()=>document.dispatchEvent(new CustomEvent('framekeep:archive-groups'))")
                        page.wait_for_function("()=>[...document.querySelectorAll('#catalog-grid .catalog-card-sku')].filter(n=>n.textContent==='SKU：GROUP-TAIL').length===100")

                        self.assertEqual(page.evaluate("()=>new Set([...document.querySelectorAll('#catalog-grid .catalog-card')].map(n=>n.dataset.catalogItem)).size"),264)
                        out=Path(__file__).resolve().parents[1]/'build/archive-rating-latency';out.mkdir(exist_ok=True)
                        page.screenshot(path=str(out/'continuous.png'))
                        self.assertEqual(page.locator('#catalog-selected-name').inner_text(),name)
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()

if __name__=='__main__':unittest.main()
