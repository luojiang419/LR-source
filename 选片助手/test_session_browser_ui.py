"""现场全目录虚拟滚动：无分页、全局筛选及评级保存不丢记录。"""
from contextlib import contextmanager,ExitStack
from http.server import ThreadingHTTPServer
from io import BytesIO
import hashlib
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import stable_json
import test_session_browser as fixtures

ROOT=Path(__file__).resolve().parents[1]


class SessionBrowserUiTests(unittest.TestCase):
    @contextmanager
    def application(self,all_rated=False):
        import server
        fixture=fixtures.SessionBrowserTests();fixture.setUp()
        try:
            store=fixture.store;store.activate(fixture.sid)
            if all_rated:
                with store.connect() as db:db.execute('UPDATE sessions SET state=? WHERE id=?',(stable_json({'overrides':{r['id']:3 for r in fixture.records}}),fixture.sid))
            image=BytesIO();Image.new('RGB',(120,80),'teal').save(image,'JPEG')
            for record in fixture.records:
                path=fixture.photos/record['jpg'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(image.getvalue())
            with ExitStack() as stack:
                values=dict(STORE=store,DATA=store.data,MODELS=None,ARCHIVE=None,CATALOG=None,ANALYSIS=None,SKU=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,REFERENCES=None,PREVIEWS=None,DIRECT=None)
                for key,value in values.items():stack.enter_context(patch.object(server,key,value))
                stack.enter_context(patch.object(server,'workbench_views',return_value=None))
                http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
                stack.callback(http.server_close);stack.callback(http.shutdown)
                with sync_playwright() as pw,pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True) as browser:
                    page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>FramekeepBoot.loading===false&&DEMO_PHOTOS.length===2500')
                    page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                    yield fixture,page,errors
                    self.assertFalse(errors,errors)
        finally:fixture.doCleanups()

    def test_full_directory_scroll_filter_ratings_and_navigation(self):
        with self.application() as (fixture,page,errors):
            self.assertTrue(page.evaluate('FRAMEKEEP_SESSION.browser.continuous'));self.assertFalse(page.evaluate('!!FRAMEKEEP_SESSION.browser.paged'))
            self.assertEqual(page.locator('.session-batchbar,[data-batch-next],[data-batch-jump]').count(),0)
            self.assertEqual(page.locator('#filter-all-count').inner_text(),'2500');self.assertLess(page.locator('#photo-grid [data-photo]').count(),100)
            page.locator('#catalog-view').evaluate('(el)=>el.scrollTop=el.scrollHeight');page.wait_for_selector('#photo-grid [data-photo="P02499"]')
            self.assertLess(page.locator('#photo-grid [data-photo]').count(),100)
            page.locator('#watch-thumbnails').evaluate('(el)=>el.scrollTop=el.scrollHeight');page.wait_for_selector('[data-watch-photo="P02499"]')
            self.assertLess(page.locator('#watch-thumbnails img').count(),20)
            page.evaluate('Framekeep.browsePhoto("P01900")')
            for view in ('grid','list'):
                page.locator('[data-view="'+view+'"]').click()
                page.wait_for_function("()=>{const el=document.querySelector('#'+(Framekeep.persistedState().view==='grid'?'photo-grid':'photo-list')+' .focused'),b=document.getElementById('catalog-view').getBoundingClientRect();if(!el)return false;const r=el.getBoundingClientRect();return el.dataset.photo==='P01900'&&r.top>=b.top-1&&r.bottom<=b.bottom+1}")
            page.evaluate('()=>{Framekeep.assignRemoteRating("P00003",4);Framekeep.assignRemoteRating("P02490",5);return FramekeepIO.flush()}')
            self.assertEqual(fixture.store.session(fixture.sid)['state']['overrides'],{'P00003':4,'P02490':5})
            page.locator('[data-filter="picked"]').click();self.assertEqual(page.evaluate('Framekeep.photoItems(true).length'),3)
            page.locator('#sort').select_option('rating-desc');self.assertEqual(page.evaluate('Framekeep.photoItems(true)[0].photo.id'),'P02490')
            page.locator('[data-filter="all"]').click();page.locator('#search').fill('P02490')
            self.assertEqual(page.evaluate('Framekeep.photoItems(true).map(row=>row.photo.id)'),['P02490']);self.assertEqual(page.evaluate('DEMO_PHOTOS.length'),2500)
            page.locator('#search').fill('NOT-FOUND-ANYWHERE');self.assertIn('没有符合条件',page.locator('#catalog-empty').inner_text())
            page.locator('#search').fill('');page.locator('#sort').select_option('time-asc')
            page.evaluate('Framekeep.browsePhoto("P02499")');self.assertEqual(page.locator('#photo-position').inner_text(),'2500 / 2500');self.assertTrue(page.locator('#next').is_disabled())
            page.evaluate('Framekeep.browsePhoto("P00000")');self.assertTrue(page.locator('#previous').is_disabled())
            page.evaluate('Framekeep.browsePhoto("P01900")');page.locator('#photos [data-loupe]').first.focus();page.keyboard.press('c')
            page.wait_for_function('()=>document.querySelectorAll("#photos [data-loupe]").length===2')
            self.assertEqual(page.locator('#photos [data-loupe]').evaluate_all('(nodes)=>nodes.map(node=>node.dataset.loupe)'),['P02498','P02499'])
            out=ROOT/'build/continuous-scroll';out.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(out/'full-directory.png'))

    def test_full_directory_view_saves_and_undo_keep_all_ratings(self):
        with self.application(all_rated=True) as (fixture,page,errors):
            for target in ('P00240','P01000','P02499'):
                page.evaluate('(id)=>Framekeep.browsePhoto(id)',target);page.locator('[data-view="list"]').click();page.evaluate('()=>FramekeepIO.flush()')
                self.assertEqual(len(fixture.store.session(fixture.sid)['state']['overrides']),2500)
            page.evaluate('()=>{Framekeep.assignRemoteRating("P01199",2);return FramekeepIO.flush()}')
            self.assertIn('P01199',fixture.store.session(fixture.sid)['state']['skuMarkers'])
            page.locator('#catalog-view').focus();page.keyboard.press('Control+z');page.evaluate('()=>FramekeepIO.flush()')
            state=fixture.store.session(fixture.sid)['state'];self.assertEqual(state['overrides']['P01199'],3);self.assertNotIn('P01199',state['skuMarkers']);self.assertEqual(len(state['overrides']),2500)
            page.locator('[data-filter="unpicked"]').click();self.assertEqual(page.evaluate('Framekeep.photoItems(true).length'),0)
            page.locator('[data-filter="all"]').click();page.evaluate('Framekeep.browsePhoto("P01900")');page.locator('[data-view="grid"]').click();page.evaluate('()=>FramekeepIO.flush()')
            page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>FramekeepBoot.loading===false&&DEMO_PHOTOS.length===2500')
            self.assertEqual(page.evaluate('Object.keys(Framekeep.persistedState().overrides).length'),2500)
            self.assertEqual(page.locator('.session-batchbar,[data-batch-next]').count(),0)
            page.wait_for_function("()=>{const el=document.querySelector('#photo-grid .focused'),b=document.getElementById('catalog-view').getBoundingClientRect();if(!el)return false;const r=el.getBoundingClientRect();return el.dataset.photo==='P01900'&&r.top>=b.top-1&&r.bottom<=b.bottom+1}")


if __name__=='__main__':unittest.main()
