from http.server import ThreadingHTTPServer
from datetime import datetime,timedelta
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from folder_watch import FolderWatch

ROOT=Path(__file__).resolve().parents[1]


class LiveLayoutBrowserTests(unittest.TestCase):
    def test_collapsible_monitor_thumbnails_arrows_reveal_and_follow(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'现场拍摄';photos.mkdir();store=Store(base/'data',ROOT);watch=FolderWatch(store,autostart=False);watch.STABLE_SECONDS=.15;watch.INTERVAL=.08
            seen=[];patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('WATCH',watch),('MOUSE',None),('MODELS',None),('SKU',None),('ARCHIVE',None),('ANALYSIS',None),('RAWLINK',None),('FILESEARCH',None)]]
            patches.append(patch('file_reveal.reveal_path',side_effect=lambda path:seen.append(str(path))))
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start();watch.thread.start()
            def image(name,orientation=1,dated=True):
                exif=Image.Exif();exif[274]=orientation
                if dated:exif[36867]=(datetime(2026,10,20,10)+timedelta(seconds=int(Path(name).stem))).strftime('%Y:%m:%d %H:%M:%S')
                Image.new('RGB',(600,400),(80,130,100)).save(photos/name,exif=exif)
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                        page.locator('#folder-watch').click();page.locator('#watch-path').fill(str(photos));page.locator('#watch-start').click();page.wait_for_function('()=>window.FramekeepLayout?.isMonitoring()')
                        self.assertEqual(page.locator('.film-panel,#filmstrip,#layout-film').count(),0);self.assertFalse(page.locator('#selection-tools').evaluate('(e)=>e.open'))
                        def current(name):page.wait_for_function('(n)=>document.getElementById("filename").textContent===n',arg=name)
                        image('001.JPG');current('001.JPG');page.locator('#stars [data-rating="1"]').click()
                        image('002.JPG',6);current('002.JPG');page.wait_for_function('()=>document.querySelector("#photos img")?.naturalHeight>document.querySelector("#photos img")?.naturalWidth')
                        page.locator('#stars [data-rating="5"]').click();image('003.JPG',dated=False);current('003.JPG')
                        page.wait_for_function('()=>document.querySelectorAll("[data-watch-photo]").length===3');self.assertEqual(page.locator('#filmstrip img').count(),0)
                        for key,name in [('ArrowUp','002.JPG'),('ArrowLeft','001.JPG'),('ArrowDown','002.JPG'),('ArrowRight','003.JPG')]:page.keyboard.press(key);current(name)
                        second=next(r for r in store.records(watch.state()['session_id']) if r['jpg']=='002.JPG')
                        page.locator('[data-watch-photo="'+second['id']+'"]').click();current('002.JPG')
                        page.locator('#watch-follow-toggle').click();page.locator('#sort').select_option('time-desc')
                        image('004.JPG');page.wait_for_function('()=>DEMO_PHOTOS.length===4');current('002.JPG')
                        page.locator('#watch-follow-toggle').click();current('004.JPG')
                        # 监控缩略图、按钮、键盘和遥控导航必须使用同一排序；新图跟随不重置排序。
                        def order(names):
                            self.assertEqual(page.evaluate('()=>Framekeep.photoItems().map(i=>i.photo.jpg)'),names)
                            self.assertEqual(page.locator('[data-watch-photo]').evaluate_all('(nodes)=>nodes.map(n=>n.title)'),names)
                            self.assertEqual(page.evaluate('()=>Framekeep.navigationOrder()'),page.evaluate('()=>Framekeep.photoItems().map(i=>i.photo.id)'))
                        order(['004.JPG','002.JPG','001.JPG','003.JPG'])
                        self.assertEqual(page.locator('#photo-position').inner_text(),'1 / 4');self.assertTrue(page.locator('#previous').is_disabled())
                        page.locator('#next').click();current('002.JPG');page.keyboard.press('ArrowUp');current('004.JPG')
                        page.locator('#sort').select_option('rating-desc');order(['002.JPG','001.JPG','004.JPG','003.JPG'])
                        self.assertEqual(page.locator('#photo-position').inner_text(),'3 / 4');self.assertFalse(page.locator('#next').is_disabled())
                        page.locator('#stars [data-rating="3"]').click();current('004.JPG');order(['002.JPG','004.JPG','001.JPG','003.JPG'])
                        self.assertEqual(page.locator('#photo-position').inner_text(),'2 / 4')
                        page.keyboard.press('ArrowDown');current('001.JPG');page.keyboard.press('ArrowLeft');current('004.JPG')
                        page.locator('#clear-rating').click();page.locator('#sort').select_option('time-desc')
                        page.evaluate('()=>FramekeepIO.flush()');page.reload(wait_until='domcontentloaded')
                        page.wait_for_function('()=>FramekeepLayout.isMonitoring()');current('004.JPG')
                        self.assertEqual(page.locator('#sort').input_value(),'time-desc');order(['004.JPG','002.JPG','001.JPG','003.JPG'])
                        page.locator('#sort').select_option('time-asc');order(['001.JPG','002.JPG','004.JPG','003.JPG'])
                        page.locator('[data-watch-photo="'+second['id']+'"]').click(button='right');self.assertTrue(page.evaluate('()=>Framekeep.remoteTarget().paused'))
                        page.locator('#photo-context-menu [data-reveal]').first.click();page.wait_for_function('()=>!Framekeep.remoteTarget().paused');page.wait_for_timeout(100)
                        self.assertEqual(seen,[str((photos/'002.JPG').resolve())])
                        page.locator('#view-open').hover();page.locator('#layout-header').click();page.wait_for_function('()=>getComputedStyle(document.querySelector(".app-header")).display==="none"');self.assertFalse(page.locator('.app-header').is_visible());page.locator('#view-open').hover();page.locator('#layout-header').click();page.wait_for_function('()=>getComputedStyle(document.querySelector(".app-header")).opacity==="1"');self.assertTrue(page.locator('.app-header').is_visible())
                        before=page.locator('.library-main').bounding_box()['width'];page.locator('#view-open').hover();page.locator('#layout-left').click();page.wait_for_function('()=>document.getElementById("selection-page").classList.contains("left-collapsed")');page.wait_for_function('(width)=>document.querySelector(".library-main").getBoundingClientRect().width>width+150',arg=before);self.assertGreater(page.locator('.library-main').bounding_box()['width'],before+150)
                        page.locator('#left-panel-toggle').click();page.locator('#view-open').hover();page.locator('#layout-focus').click();self.assertTrue(page.locator('#selection-page').evaluate('(e)=>e.classList.contains("right-collapsed")'));page.locator('#view-open').hover();page.locator('#layout-focus').click()
                        page.evaluate('()=>FramekeepIO.flush()');sid=watch.state()['session_id'];ratings={r['relativePaths'][0]:r['rating'] for r in store.manifest(False,sid)['captures']};self.assertEqual(ratings['001.JPG'],1);self.assertEqual(ratings['002.JPG'],5)
                        for i in range(5,185):image(f'{i:03d}.JPG')
                        page.wait_for_function('()=>DEMO_PHOTOS.length===184',timeout=30000);page.wait_for_function('()=>document.getElementById("filename").textContent==="184.JPG"')
                        self.assertLess(page.locator('#watch-thumbnails img').count(),25)
                        self.assertIn('184.JPG',page.locator('.watch-thumb.active').get_attribute('aria-label'))
                        self.assertEqual(page.locator('.watch-thumb.active').inner_text(),'')
                        self.assertEqual(page.locator('.watch-thumb span,.watch-thumb small').count(),0)
                        page.locator('#view-open').hover();self.assertEqual(page.locator('#view-options #layout-film').count(),0)
                        page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>FramekeepLayout.isMonitoring()');self.assertEqual(page.locator('.film-panel,#filmstrip,#layout-film').count(),0)
                        out=ROOT/'build/layout-acceptance';out.mkdir(exist_ok=True);page.screenshot(path=str(out/'live-layout-browser.png'));self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                watch.close();http.shutdown();http.server_close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
