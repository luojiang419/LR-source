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
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        slider=page.locator('#thumb-size');output=page.locator('#thumb-size-value')
                        def set_size(value):slider.fill(str(value));slider.dispatch_event('input')
                        def dimensions():return page.locator('#photos img').first.bounding_box()
                        def visible_width(selector):
                            return page.wait_for_function('''selector=>{const r=document.querySelector(selector)?.getBoundingClientRect();return r?.width>0&&r.height>0?r.width:false}''',arg=selector).json_value()
                        def wait_image():page.wait_for_function('()=>[...document.querySelectorAll("#photos img")].every(i=>i.naturalWidth>0&&i.style.width)')
                        def expect_percent(value):self.assertEqual(output.inner_text(),f'{value}%')
                        def set_position(value,kind='zoom'):
                            page.locator('#app-settings-open').click();page.locator('[data-settings-section="preview"]').click()
                            self.assertEqual(page.locator('#settings-preview #preview-rating-position,#settings-preview #preview-zoom-position').count(),2)
                            page.locator(f'#preview-{kind}-position').select_option(value);page.locator('#app-settings-close').click()
                            page.locator('.preview-surface').hover()
                        def set_transparency(rating,zoom):
                            page.locator('#app-settings-open').click();page.locator('[data-settings-section="preview"]').click()
                            for kind,value in [('rating',rating),('zoom',zoom)]:
                                control=page.locator(f'#preview-{kind}-transparency');control.fill(str(value));control.dispatch_event('input')
                                self.assertEqual(page.locator(f'#preview-{kind}-transparency-value').inner_text(),f'{value}%')
                            page.locator('#app-settings-close').click();page.locator('.preview-surface').hover()
                        def opacity(selector):return float(page.locator(selector).evaluate('e=>getComputedStyle(e).opacity'))
                        # 新用户默认隐藏，仍可滚轮缩放；设置项通过真实设置页即时生效。
                        page.locator('.preview-surface').hover();self.assertTrue(page.locator('.thumbnail-control').is_hidden())
                        self.assertEqual(page.locator('#preview-rating-position').input_value(),'bottom-left')
                        for kind in ['rating','zoom']:self.assertEqual(page.locator(f'#preview-{kind}-transparency').input_value(),'0')
                        page.locator('[data-view="single"]').click();wait_image()
                        page.locator('[data-loupe]').first.hover();page.mouse.wheel(0,-100)
                        page.wait_for_function('()=>Number(document.getElementById("thumb-size").value)>100')
                        self.assertTrue(page.locator('.thumbnail-control').is_hidden());page.locator('[data-loupe]').first.click()
                        for theme in ['dark','light','pure-white','hardcore']:
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            set_position('bottom-right')
                            set_position('bottom-left','rating')
                            for rating,zoom in [(0,0),(35,65),(100,100)]:
                                set_transparency(rating,zoom)
                                self.assertAlmostEqual(opacity('#stars'),1-rating/100)
                                self.assertAlmostEqual(opacity('.thumbnail-control'),1-zoom/100)
                                self.assertEqual(page.locator('#stars').is_hidden(),rating==100)
                                self.assertEqual(page.locator('.thumbnail-control').is_hidden(),zoom==100)
                                if rating==100:
                                    for selector in ['#stars','.thumbnail-control']:
                                        self.assertTrue(page.locator(selector).evaluate('(e)=>{const b=e.getBoundingClientRect();return !e.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2))}'))
                                elif rating==35:
                                    page.locator('.preview-surface').screenshot(path=str(out/f'transparency-{theme}.png'))
                                    page.mouse.move(0,0);self.assertEqual(opacity('.thumbnail-control'),0)
                                    self.assertAlmostEqual(opacity('#stars'),.65)
                            set_transparency(0,0)
                            for position in ['top-left','top-right','bottom-left','bottom-right']:
                                set_position(position)
                                for rating_position in ['top-left','top-right','bottom-left','bottom-right','hidden']:
                                    set_position(rating_position,'rating')
                                    for width in [1680,1024,600]:
                                        page.set_viewport_size({'width':width,'height':1050});page.locator('.preview-surface').hover()
                                        # 等待 ResizeObserver 根据实际预览宽度完成避让。
                                        page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                                        surface=page.locator('.preview-surface').bounding_box();control=page.locator('.thumbnail-control').bounding_box();stars=page.locator('#stars').bounding_box()
                                        self.assertTrue(slider.is_visible())
                                        horizontal=control['x']-surface['x'] if position.endswith('left') else surface['x']+surface['width']-control['x']-control['width']
                                        vertical=control['y']-surface['y'] if position.startswith('top') else surface['y']+surface['height']-control['y']-control['height']
                                        self.assertAlmostEqual(horizontal,12,delta=2);self.assertAlmostEqual(vertical,12,delta=2)
                                        if rating_position=='hidden':self.assertIsNone(stars)
                                        else:
                                            horizontal=stars['x']-surface['x'] if rating_position.endswith('left') else surface['x']+surface['width']-stars['x']-stars['width']
                                            vertical=stars['y']-surface['y'] if rating_position.startswith('top') else surface['y']+surface['height']-stars['y']-stars['height']
                                            self.assertAlmostEqual(horizontal,12,delta=2)
                                            same_edge=position.split('-')[0]==rating_position.split('-')[0]
                                            horizontal_overlap=control['x']<stars['x']+stars['width']+8 and stars['x']<control['x']+control['width']+8
                                            self.assertAlmostEqual(vertical,control['height']+20 if same_edge and horizontal_overlap else 12,delta=2)
                                            self.assertTrue(control['x']+control['width']<=stars['x'] or stars['x']+stars['width']<=control['x'] or control['y']+control['height']<=stars['y'] or stars['y']+stars['height']<=control['y'])
                                            self.assertGreaterEqual(stars['y'],surface['y']);self.assertLessEqual(stars['y']+stars['height'],surface['y']+surface['height'])
                                        if width==600 and rating_position==position:page.locator('.preview-surface').screenshot(path=str(out/f'position-{theme}-{position}.png'))
                                        self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                                page.set_viewport_size({'width':1680,'height':1050})
                        set_position('top-right','rating')
                        page.locator('#stars [data-rating="4"]').click();page.evaluate('FramekeepIO.flush()')
                        page.reload(wait_until='domcontentloaded')
                        page.wait_for_function('()=>document.querySelector(".preview-surface")?.dataset.zoomPosition==="bottom-right"')
                        self.assertEqual(page.locator('#preview-rating-position').input_value(),'top-right')
                        page.wait_for_function('()=>document.querySelectorAll("#stars .on").length===4')
                        set_position('hidden','rating');page.locator('#photo-stage').click();page.keyboard.press('3');page.evaluate('FramekeepIO.flush()')
                        page.reload(wait_until='domcontentloaded')
                        page.wait_for_function('()=>document.querySelector(".preview-surface")?.dataset.ratingPosition==="hidden"')
                        self.assertTrue(page.locator('#stars').is_hidden())
                        page.wait_for_function('()=>document.querySelectorAll("#stars .on").length===3')
                        page.evaluate('localStorage.setItem("framekeep-preview-rating-position-v1","invalid")');page.reload(wait_until='domcontentloaded')
                        page.wait_for_function('()=>document.querySelector(".preview-surface")?.dataset.ratingPosition==="bottom-left"')
                        set_position('hidden');page.reload(wait_until='domcontentloaded')
                        page.wait_for_function('()=>document.getElementById("preview-zoom-position")?.value==="hidden"')
                        page.locator('.preview-surface').hover();self.assertTrue(page.locator('.thumbnail-control').is_hidden())
                        page.evaluate('localStorage.setItem("framekeep-preview-zoom-position-v1","invalid")');page.reload(wait_until='domcontentloaded')
                        page.wait_for_function('()=>document.querySelector(".preview-surface")?.dataset.zoomPosition==="hidden"')
                        set_position('bottom-right');set_transparency(27,58)
                        page.reload(wait_until='domcontentloaded')
                        page.wait_for_function('()=>document.querySelector(".preview-surface")?.dataset.zoomTransparency==="58"')
                        page.locator('.preview-surface').hover()
                        self.assertAlmostEqual(opacity('#stars'),.73);self.assertAlmostEqual(opacity('.thumbnail-control'),.42)
                        for kind,value in [('rating',27),('zoom',58)]:
                            self.assertEqual(page.locator(f'#preview-{kind}-transparency').input_value(),str(value))
                        # 在设置页用键盘调整，不能触发照片快捷键；越界或非法偏好回退原有外观。
                        page.locator('#app-settings-open').click();page.locator('[data-settings-section="preview"]').click()
                        transparency=page.locator('#preview-rating-transparency');transparency.press('End');transparency.press('ArrowLeft')
                        self.assertEqual(transparency.input_value(),'99')
                        self.assertEqual(page.locator('#preview-rating-transparency-value').inner_text(),'99%')
                        page.locator('#app-settings-close').click()
                        for invalid in ['invalid','-1','101','Infinity']:
                            page.evaluate('(value)=>{for(const kind of ["rating","zoom"])localStorage.setItem(`framekeep-preview-${kind}-transparency-v1`,value)}',invalid)
                            page.reload(wait_until='domcontentloaded')
                            page.wait_for_function('()=>document.querySelector(".preview-surface")?.dataset.zoomTransparency==="0"')
                            self.assertEqual(page.locator('#preview-rating-transparency').input_value(),'0')
                        set_transparency(0,0)
                        set_position('bottom-right');page.locator('[data-view="grid"]').click()
                        # 原网格/列表尺寸仍然生效，并保留单独的缩略图偏好。
                        # 视图会被后台状态刷新替换，在同一浏览器回调中等待并读取可见几何。
                        card=visible_width('.photo-card');set_size(300)
                        self.assertGreater(visible_width('.photo-card'),card+50)
                        page.locator('[data-view="list"]').click();list_width=visible_width('.list-preview')
                        set_size(110);self.assertLess(visible_width('.list-preview'),list_width)
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
                        self.assertTrue(any('size=2048' in url for url in preview_requests), preview_requests)
                        self.assertTrue(all('decode=1' not in url for url in preview_requests), preview_requests)
                        self.assertGreater(page.locator('#photos img').first.evaluate('(img)=>Math.max(img.naturalWidth,img.naturalHeight)'),480)
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
