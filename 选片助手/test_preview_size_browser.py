"""隔离图片验证浮动滑杆控制缩略图、大图及对比图的实际显示尺寸。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright
from catalog_store import Store
from folder_watch import FolderWatch
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]


class PreviewSizeTests(unittest.TestCase):
    def test_slider_dimensions_sync_reset_compare_and_monitor(self):
        import server
        out=ROOT/'build/preview-size';out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT)
            sid=store.ensure_live_session(photos)
            for i in range(4):
                path=photos/f'{i:03}.JPG';w,h=(1200,800) if i%2==0 else (800,1200)
                image=Image.new('RGB',(w,h),'#466879');draw=ImageDraw.Draw(image)
                for x in range(0,w,100):draw.line((x,0,x,h),fill='#9cc8b7',width=3)
                for y in range(0,h,100):draw.line((0,y,w,y),fill='#9cc8b7',width=3)
                draw.ellipse((w//3,h//3,w*2//3,h*2//3),fill='#d8b06c');image.save(path)
                store.append_live(sid,[media.capture_record(photos,path,None,{'ImageWidth':w,'ImageHeight':h,'DateTimeOriginal':f'2026:10:20 10:00:0{i}'})])
            store.activate(sid);watch=FolderWatch(store,autostart=False)
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('WATCH',watch),('MODELS',None),('MOUSE',None),('SKU',None),('ARCHIVE',None),('ANALYSIS',None),('RAWLINK',None),('FILESEARCH',None)]]
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050},reduced_motion='reduce');errors=[]
                        preview_requests=[]
                        page.on('request',lambda request: preview_requests.append(request.url) if '/api/preview/' in request.url else None)
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                        slider=page.locator('#thumb-size');output=page.locator('#thumb-size-value')
                        def set_size(value):slider.fill(str(value));slider.dispatch_event('input')
                        def dimensions():return page.locator('#photos img').first.bounding_box()
                        def wait_image():page.wait_for_function('()=>[...document.querySelectorAll("#photos img")].every(i=>i.naturalWidth>0&&i.style.width)')
                        def expect_percent(value):self.assertEqual(output.inner_text(),f'{value}%')
                        # 原网格/列表尺寸仍然生效，并保留单独的缩略图偏好。
                        card=page.locator('.photo-card').first.bounding_box()['width'];set_size(300)
                        self.assertGreater(page.locator('.photo-card').first.bounding_box()['width'],card+50)
                        page.locator('[data-view="list"]').click();list_width=page.locator('.list-preview').first.bounding_box()['width']
                        set_size(110);self.assertLess(page.locator('.list-preview').first.bounding_box()['width'],list_width)
                        set_size(230);page.locator('[data-view="single"]').click();wait_image();expect_percent(100)
                        fit=dimensions();set_size(200);expect_percent(200)
                        self.assertAlmostEqual(dimensions()['width'],fit['width']*2,delta=1)
                        self.assertAlmostEqual(dimensions()['height'],fit['height']*2,delta=1)
                        page.locator('#photo-stage').hover();page.locator('#thumb-larger').click();expect_percent(220)
                        page.locator('#thumb-smaller').click();expect_percent(200)
                        slider.press('End');expect_percent(1600);slider.press('Home');expect_percent(100)
                        slider.press('ArrowRight');expect_percent(101)
                        # 实际拖动滑杆；滚轮缩放、平移及点击复位与控件双向同步。
                        box=slider.bounding_box();page.mouse.move(box['x']+8,box['y']+box['height']/2);page.mouse.down()
                        page.mouse.move(box['x']+box['width']*.35,box['y']+box['height']/2,steps=5);page.mouse.up()
                        self.assertGreater(dimensions()['width'],fit['width']*2)
                        set_size(200);button=page.locator('[data-loupe]').first;box=button.bounding_box();x=box['x']+box['width']/2;y=box['y']+box['height']/2
                        page.mouse.move(x,y);page.mouse.wheel(0,-100)
                        page.wait_for_function('()=>Number(document.getElementById("thumb-size").value)>200')
                        self.assertEqual(int(slider.input_value()),round(dimensions()['width']/fit['width']*100))
                        before=dimensions();page.mouse.down();page.mouse.move(x+70,y+40,steps=5);page.mouse.up()
                        self.assertGreater(dimensions()['x'],before['x']+50)
                        button.click();expect_percent(100)
                        # 切图复位；对比图统一缩放，切回网格恢复原尺寸而非百分比。
                        set_size(250);page.locator('#next').click();wait_image();expect_percent(100)
                        page.keyboard.press('c');wait_image();self.assertEqual(page.locator('[data-loupe]').count(),2)
                        fits=[i.bounding_box()['width'] for i in page.locator('#photos img').all()];set_size(300)
                        for i,width in zip(page.locator('#photos img').all(),fits):self.assertAlmostEqual(i.bounding_box()['width'],width*3,delta=1)
                        page.locator('[data-view="grid"]').click();self.assertEqual(slider.input_value(),'230');self.assertEqual(output.inner_text(),'230')
                        # 监控自动进入的大图及四主题使用同一控件，窄窗口不溢出。
                        watch.start(str(photos));page.wait_for_function('()=>FramekeepLayout.isMonitoring()');wait_image();expect_percent(100)
                        for theme in ['dark','light','pure-white','hardcore']:
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme);set_size(100);fit=dimensions();set_size(180)
                            self.assertAlmostEqual(dimensions()['width'],fit['width']*1.8,delta=1)
                        page.mouse.move(x,y);page.screenshot(path=str(out/'large-180.png'))
                        page.set_viewport_size({'width':1024,'height':768});page.locator('.preview-surface').hover()
                        self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'))
                        self.assertTrue(slider.is_visible());self.assertFalse(errors,errors)
                        self.assertTrue(preview_requests)
                        self.assertTrue(any('size=1800' in url for url in preview_requests), preview_requests)
                        self.assertTrue(all('decode=1' not in url for url in preview_requests), preview_requests)
                        self.assertGreater(page.locator('#photos img').first.evaluate('(img)=>Math.max(img.naturalWidth,img.naturalHeight)'),480)
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
