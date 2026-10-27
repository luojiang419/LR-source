"""真实浏览器验证有界节点、全目录查询、跨批评级和导航。"""
import hashlib
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
import test_session_browser as fixtures

ROOT=Path(__file__).resolve().parents[1]


class SessionBrowserUiTests(unittest.TestCase):
    def test_bounded_grid_and_whole_directory_search_ratings_and_navigation(self):
        fixture=fixtures.SessionBrowserTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        store=fixture.store;store.activate(fixture.sid)
        image=io.BytesIO();Image.new('RGB',(120,80),'teal').save(image,'JPEG')
        for record in fixture.records[:500]+fixture.records[2400:]:
            path=fixture.photos/record['jpg'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(image.getvalue())
        source=fixture.photos/'P00003.JPG';before=hashlib.sha256(source.read_bytes()).hexdigest()
        import server
        patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('MODELS',None),('ARCHIVE',None),('CATALOG',None),('ANALYSIS',None),('SKU',None),('WATCH',None),('MOUSE',None),('CANON',None),('RAWLINK',None),('FILESEARCH',None),('REFERENCES',None),('PREVIEWS',None)]]
        original_browse=server.browse_state;delayed=threading.Event()
        def slow_page(sid=None,query=None):
            if query and str(query.get('offset'))=='240' and not delayed.is_set():delayed.set();time.sleep(.7)
            return original_browse(sid,query)
        patches.append(patch.object(server,'browse_state',side_effect=slow_page))
        for item in patches:item.start()
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='msedge',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepBoot?.loading===false')
                    page.wait_for_function('()=>window.FramekeepBoot?.loading===false&&window.FramekeepIO&&FRAMEKEEP_SESSION.browser?.total===2500')
                    self.assertEqual(page.evaluate('DEMO_PHOTOS.length'),240)
                    self.assertEqual(page.locator('#filter-all-count').inner_text(),'2500')
                    self.assertLess(page.locator('#photo-grid [data-photo]').count(),100)
                    page.evaluate('document.getElementById("catalog-view").scrollTop=1e8')
                    page.wait_for_selector('#photo-grid [data-photo="P00239"]')
                    self.assertLess(page.locator('#photo-grid [data-photo]').count(),100)
                    page.evaluate('()=>{Framekeep.assignRemoteRating("P00003",4);return FramekeepIO.flush()}')
                    page.locator('[data-batch-next]').click();self.assertFalse(page.evaluate('Framekeep.assignRemoteRating("P00003",2)'));page.wait_for_function('()=>DEMO_PHOTOS[0]?.id==="P00240"&&!FramekeepPaging.busy')
                    page.evaluate('()=>{Framekeep.assignRemoteRating("P00240",5);return FramekeepIO.flush()}')
                    self.assertEqual(store.session(fixture.sid)['state']['overrides'],{'P00003':4,'P00240':5})
                    page.locator('[data-filter="picked"]').click()
                    page.wait_for_function('()=>FRAMEKEEP_SESSION.browser.matched===3&&DEMO_PHOTOS.length===3&&!FramekeepPaging.busy')
                    page.locator('#sort').select_option('rating-desc')
                    page.wait_for_function('()=>DEMO_PHOTOS[0]?.id==="P00240"&&!FramekeepPaging.busy')
                    page.evaluate('()=>{Framekeep.assignRemoteRating("P00240",0);return FramekeepIO.flush()}')
                    page.wait_for_function('()=>FRAMEKEEP_SESSION.browser.matched===2&&DEMO_PHOTOS.length===2&&!FramekeepPaging.busy')
                    self.assertEqual(page.locator('#filter-picked-count').inner_text(),'2')
                    page.evaluate('()=>FramekeepPaging.goto("P00240")')
                    page.evaluate('()=>{Framekeep.assignRemoteRating("P00240",5);return FramekeepIO.flush()}')
                    page.wait_for_function('()=>DEMO_PHOTOS[0]?.id==="P00240"&&!FramekeepPaging.busy')
                    page.locator('[data-filter="all"]').click();page.locator('#search').fill('P02490')
                    page.wait_for_function('()=>DEMO_PHOTOS.length===1&&DEMO_PHOTOS[0].id==="P02490"&&!FramekeepPaging.busy')
                    self.assertEqual(page.evaluate('FRAMEKEEP_SESSION.browser.total'),2500)
                    self.assertEqual(page.evaluate('async()=>{const r=await FramekeepIO.api("/api/state?session_id="+FRAMEKEEP_SESSION.id+"&q=&filter=all&offset=0");return r.catalog.length}'),240)
                    page.locator('#search').fill('NOT-FOUND-ANYWHERE')
                    page.wait_for_function('()=>FRAMEKEEP_SESSION.browser.matched===0&&!FramekeepPaging.busy')
                    self.assertIn('没有符合条件',page.locator('#catalog-empty').inner_text())
                    page.locator('#search').fill('');page.locator('#sort').select_option('time-asc')
                    page.wait_for_function('()=>DEMO_PHOTOS.length===240&&DEMO_PHOTOS[0].id==="P00000"&&!FramekeepPaging.busy')
                    page.locator('.session-batchbar input').fill('11');page.locator('[data-batch-jump]').click()
                    page.wait_for_function('()=>DEMO_PHOTOS[0]?.id==="P02400"&&!FramekeepPaging.busy')
                    page.evaluate('Framekeep.browsePhoto("P02499")')
                    self.assertEqual(page.locator('#photo-position').inner_text(),'2500 / 2500')
                    self.assertTrue(page.locator('#next').is_disabled())
                    page.evaluate('Framekeep.browsePhoto("P02400")');page.locator('#previous').click()
                    page.wait_for_function('()=>Framekeep.remoteTarget().photoId==="P02399"&&!FramekeepPaging.busy')
                    page.locator('#next').click();page.wait_for_function('()=>Framekeep.remoteTarget().photoId==="P02400"&&!FramekeepPaging.busy')
                    page.keyboard.press('Control+z')
                    page.wait_for_function('()=>Framekeep.remoteTarget().photoId==="P00240"&&!FramekeepPaging.busy')
                    page.evaluate('()=>FramekeepIO.flush()')
                    self.assertEqual(store.session(fixture.sid)['state']['overrides']['P00240'],0)
                    self.assertEqual(store.session(fixture.sid)['state']['overrides']['P00003'],4)
                    # 撤销会触发延迟的全目录重排；等待页面完成，而非仅等保存请求返回。
                    page.wait_for_function('()=>!FramekeepPaging.busy&&!FramekeepPaging.pending')
                    page.locator('#photos [data-loupe]').first.focus();page.keyboard.press('c')
                    page.wait_for_function('()=>document.querySelectorAll("#photos [data-loupe]").length===2')
                    self.assertEqual(page.locator('#photos [data-loupe]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.loupe)'),['P02498','P02499'])
                    output=ROOT/'build/session-browser';output.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(output/'paged-browser.png'))
                    self.assertFalse(errors,errors);self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
                finally:browser.close()
        finally:
            http.shutdown();http.server_close()
            for item in reversed(patches):item.stop()

    def test_loading_rated_pages_keeps_only_current_overlays_without_erasing_unseen_ratings(self):
        fixture=fixtures.SessionBrowserTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        store=fixture.store;store.activate(fixture.sid)
        from catalog_store import stable_json
        with store.connect() as db:db.execute('UPDATE sessions SET state=? WHERE id=?',(stable_json({'overrides':{r['id']:3 for r in fixture.records}}),fixture.sid))
        import server
        values={'STORE':store,'DATA':store.data,'MODELS':None,'ARCHIVE':None,'CATALOG':None,'ANALYSIS':None,'SKU':None,'WATCH':None,'MOUSE':None,'CANON':None,'RAWLINK':None,'FILESEARCH':None,'REFERENCES':None,'PREVIEWS':None}
        patches=[patch.object(server,k,v) for k,v in values.items()]
        for item in patches:item.start()
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='msedge',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepBoot?.loading===false')
                    for offset in (240,480,720):
                        page.locator('[data-batch-next]').click()
                        page.wait_for_function('(offset)=>FRAMEKEEP_SESSION.browser.offset===offset&&!FramekeepPaging.busy',arg=offset)
                        self.assertEqual(page.evaluate('Object.keys(Framekeep.persistedState().overrides).length'),240)
                    page.evaluate('()=>FramekeepIO.flush()')
                    self.assertEqual(len(store.session(fixture.sid)['state']['overrides']),2500)
                    page.locator('#sort').select_option('rating-desc')
                    page.wait_for_function('()=>FRAMEKEEP_SESSION.browser.sort==="rating-desc"&&!FramekeepPaging.busy')
                    page.evaluate('()=>FramekeepPaging.goto("P01000")')
                    page.evaluate('()=>{Framekeep.assignRemoteRating("P01199",2);return FramekeepIO.flush()}')
                    page.wait_for_function('()=>!DEMO_PHOTOS.some(p=>p.id==="P01199")&&!FramekeepPaging.busy')
                    self.assertIn('P01199',store.session(fixture.sid)['state']['skuMarkers'])
                    page.evaluate('document.activeElement?.blur()');page.keyboard.press('Control+z')
                    page.wait_for_function('()=>DEMO_PHOTOS.some(p=>p.id==="P01199")&&!FramekeepPaging.busy')
                    page.evaluate('()=>FramekeepIO.flush()')
                    self.assertEqual(store.session(fixture.sid)['state']['overrides']['P01199'],3)
                    self.assertNotIn('P01199',store.session(fixture.sid)['state']['skuMarkers'])
                    self.assertEqual(len(store.session(fixture.sid)['state']['overrides']),2500)
                    self.assertFalse(errors,errors)
                finally:browser.close()
        finally:
            http.shutdown();http.server_close()
            for item in reversed(patches):item.stop()


if __name__=='__main__':unittest.main()
