"""隔离照片验证侧栏拖拽、持久化及不等高缩略图虚拟列表。"""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from folder_watch import FolderWatch
from model_providers import Providers
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class SidebarResizeTests(unittest.TestCase):
    def test_drag_adaptive_photos_limits_restore_and_virtual_scroll(self):
        import server
        out=ROOT/'build/sidebar-resize';out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT)
            sid=store.ensure_live_session(photos);ids=[]
            for i in range(48):
                path=photos/f'{i:03}.JPG';w,h=(600,400) if i%2==0 else (400,600)
                Image.new('RGB',(w,h),(80+i*2,125,160)).save(path)
                record=media.capture_record(photos,path,None,{'ImageWidth':w,'ImageHeight':h})
                store.append_live(sid,[record]);ids.append(record['id'])
            store.activate(sid);store.save(sid,0,{'overrides':{ids[0]:4}})
            watch=FolderWatch(store,autostart=False);watch.start(str(photos))
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('WATCH',watch),('MODELS',Providers(store.data)),('MOUSE',None),('SKU',None),('ARCHIVE',None),('ANALYSIS',None),('RAWLINK',None),('FILESEARCH',None)]]
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050},reduced_motion='reduce');errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        page.wait_for_function('()=>window.FramekeepLayout?.isMonitoring()')
                        def size(side):return page.locator('.'+side+'-panel').bounding_box()['width']
                        def drag(side,delta,release=True):
                            box=page.locator('#'+side+'-sidebar-resizer').bounding_box();x=box['x']+box['width']/2;y=box['y']+box['height']/2
                            page.mouse.move(x,y);page.mouse.down();page.mouse.move(x+delta,y,steps=6);page.wait_for_timeout(100)
                            if release:page.mouse.up()
                        def photo():return page.locator('.watch-thumb.active img')
                        checks=[]
                        for theme in ['light','dark','pure-white','hardcore']:
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            for side in ['left','right']:page.locator('#'+side+'-sidebar-resizer').press('Home')
                            page.wait_for_timeout(100)
                            page.locator('#watch-thumbnails').evaluate('(e)=>e.scrollTop=0')
                            page.locator(f'[data-watch-photo="{ids[0]}"]').click()
                            page.wait_for_function('()=>document.querySelector(".watch-thumb.active img")?.naturalWidth>0')
                            before=size('left');image_before=photo().bounding_box()['width']
                            drag('left',70,release=False)
                            self.assertGreater(size('left'),before+60);self.assertGreater(photo().bounding_box()['width'],image_before+60)
                            page.mouse.up();wide=size('left')
                            available=page.locator('#watch-thumbnails').evaluate('e=>e.clientWidth')
                            self.assertAlmostEqual(photo().bounding_box()['width'],available-20,delta=2)
                            self.assertAlmostEqual(photo().bounding_box()['width']/photo().bounding_box()['height'],1.5,delta=.02)
                            right=size('right');drag('right',-60);self.assertGreater(size('right'),right+50)
                            self.assertGreaterEqual(page.locator('.library-main').bounding_box()['width'],319)
                            # 横图和竖图各自按真实比例铺开，不产生固定小图的大块留白。
                            page.locator(f'[data-watch-photo="{ids[1]}"]').click()
                            page.wait_for_function('()=>document.querySelector(".watch-thumb.active img")?.naturalHeight>0')
                            page.wait_for_timeout(100)
                            self.assertAlmostEqual(photo().bounding_box()['width']/photo().bounding_box()['height'],2/3,delta=.02)
                            # 拖拽取消还原、键盘微调、折叠和再展开。
                            drag('left',30,release=False);page.keyboard.press('Escape');page.mouse.up()
                            self.assertAlmostEqual(size('left'),wide,delta=1)
                            page.locator('#left-sidebar-resizer').press('ArrowRight');self.assertAlmostEqual(size('left'),wide+12,delta=1)
                            page.locator('#left-panel-toggle').click();self.assertTrue(page.locator('#left-sidebar-resizer').is_hidden())
                            page.locator('#left-panel-toggle').click();page.wait_for_timeout(100)
                            self.assertAlmostEqual(size('left'),wide+12,delta=1)
                            # 两边都有最小值，并给中央图片区保留空间。
                            drag('left',-1200);self.assertAlmostEqual(size('left'),160,delta=1)
                            drag('right',1200);self.assertAlmostEqual(size('right'),220,delta=1)
                            drag('left',140);drag('right',-100)
                            page.set_viewport_size({'width':1024,'height':1050});page.wait_for_timeout(150)
                            self.assertGreaterEqual(page.locator('.library-main').bounding_box()['width'],319)
                            self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'))
                            page.set_viewport_size({'width':1680,'height':1050});page.wait_for_timeout(100)
                            # 长列表滚动至尾部，选中末张后改宽，位置和虚拟化仍正常。
                            page.locator('#watch-thumbnails').evaluate('(e)=>e.scrollTop=e.scrollHeight')
                            page.locator(f'[data-watch-photo="{ids[-1]}"]').click()
                            drag('left',25);page.wait_for_timeout(100)
                            self.assertIn('047.JPG',page.locator('.watch-thumb.active').get_attribute('aria-label'))
                            self.assertLess(page.locator('#watch-thumbnails img').count(),25)
                            self.assertTrue(photo().is_visible())
                            strip=page.locator('#watch-thumbnails').bounding_box();selected=page.locator('.watch-thumb.active').bounding_box()
                            self.assertGreaterEqual(selected['y'],strip['y']-2)
                            self.assertLessEqual(selected['y']+selected['height'],strip['y']+strip['height']+2)
                            page.screenshot(path=str(out/f'{theme}.png'))
                            # 刷新、主题切换和窄屏只改变显示，不丢失保存的宽度。
                            saved=(size('left'),size('right'))
                            page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading');page.wait_for_function('()=>FramekeepLayout.isMonitoring()')
                            self.assertAlmostEqual(size('left'),saved[0],delta=1);self.assertAlmostEqual(size('right'),saved[1],delta=1)
                            page.set_viewport_size({'width':375,'height':1000});page.wait_for_timeout(100)
                            self.assertTrue(page.locator('#left-sidebar-resizer').is_hidden());self.assertTrue(page.locator('#right-sidebar-resizer').is_hidden())
                            self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'))
                            page.set_viewport_size({'width':1680,'height':1050});page.wait_for_timeout(100)
                            page.locator('#left-sidebar-resizer').dblclick();page.wait_for_timeout(100)
                            self.assertNotIn('left',page.evaluate('()=>JSON.parse(localStorage.getItem("framekeep-sidebar-widths-v1"))'))
                            checks.append({'theme':theme,'saved':saved,'responsive':True})
                        # 正常动效模式拖拽仍须即时跟手，不能被原布局过渡拖慢。
                        page.emulate_media(reduced_motion='no-preference');page.wait_for_timeout(300)
                        before=size('left');drag('left',48,release=False)
                        self.assertEqual(page.locator('.library-layout').evaluate('(e)=>getComputedStyle(e).transitionDuration'),'0s')
                        self.assertAlmostEqual(size('left'),before+48,delta=1);page.mouse.up()
                        page.evaluate('()=>FramekeepIO.flush()')
                        self.assertEqual(store.session(sid)['state']['overrides'],{ids[0]:4})
                        self.assertFalse(errors,errors)
                        (out/'verification.json').write_text(json.dumps({'checks':checks,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
                    finally:browser.close()
            finally:
                watch.close();http.shutdown();http.server_close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
