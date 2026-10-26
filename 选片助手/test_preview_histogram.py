"""预览直方图的真实像素、控件避让与异步切图验证（隔离业务资料）。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import io
from itertools import combinations
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from folder_watch import FolderWatch
import media_engine as media

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/preview-histogram'


def png(color):
    data = io.BytesIO()
    Image.new('RGB', (320, 200), color).save(data, format='PNG')
    return data.getvalue()


class PreviewHistogramTests(unittest.TestCase):
    def ready(self, page):
        page.wait_for_function('()=>document.querySelector("#preview-histogram")?.dataset.state==="ready"')

    def settle(self, page):
        page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')

    def peak(self, page, channel):
        return int(page.locator(f'#preview-histogram path[data-channel="{channel}"]').get_attribute('aria-label').split()[-1])

    def test_real_application_pixels_layout_themes_and_gestures(self):
        import server
        OUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); photos = base/'photos'; photos.mkdir()
            store = Store(base/'data', ROOT); sid = store.ensure_live_session(photos)
            for i, color in enumerate([(220, 20, 30), (20, 190, 50), (90, 90, 90)]):
                path = photos/f'{i:03}.JPG'
                Image.new('RGB', (640, 400), color).save(path, quality=100)
                store.append_live(sid, [media.capture_record(photos, path, None, {
                    'ImageWidth':640, 'ImageHeight':400, 'DateTimeOriginal':f'2026:10:24 10:00:0{i}'})])
            originals = {p.name:p.read_bytes() for p in photos.iterdir()}
            store.activate(sid); watch = FolderWatch(store, autostart=False)
            patches = [patch.object(server, key, value) for key, value in [
                ('STORE',store), ('DATA',store.data), ('WATCH',watch), ('MODELS',None), ('MOUSE',None),
                ('SKU',None), ('ARCHIVE',None), ('ANALYSIS',None), ('RAWLINK',None), ('FILESEARCH',None)]]
            for item in patches:item.start()
            class TestHTTPServer(ThreadingHTTPServer):
                request_queue_size = 128
            http = TestHTTPServer(('127.0.0.1',0), server.Handler); http.daemon_threads = True
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(channel='msedge', headless=True)
                    try:
                        page = browser.new_page(viewport={'width':1680,'height':1050}, reduced_motion='reduce')
                        errors = []; page.on('pageerror', lambda e:errors.append(str(e)))
                        page.add_init_script('window.histogramReads=0;const original=CanvasRenderingContext2D.prototype.getImageData;CanvasRenderingContext2D.prototype.getImageData=function(...args){window.histogramReads++;return original.apply(this,args)}')
                        page.goto(f'http://127.0.0.1:{http.server_port}', wait_until='domcontentloaded')
                        self.ready(page)
                        panel = page.locator('#preview-histogram')
                        self.assertEqual(panel.get_attribute('data-photo-id'), page.evaluate('Framekeep.remoteTarget().photoId'))
                        self.assertGreater(int(panel.get_attribute('data-samples')), 10000)
                        self.assertLessEqual(int(panel.get_attribute('data-samples')), 65536)
                        # 已知色块的通道峰值，验证曲线确实取自当前图片。
                        self.assertAlmostEqual(self.peak(page,'red'), 220, delta=2)
                        self.assertAlmostEqual(self.peak(page,'green'), 20, delta=2)
                        self.assertAlmostEqual(self.peak(page,'blue'), 30, delta=2)
                        self.assertAlmostEqual(self.peak(page,'luma'), 63, delta=2)
                        page.locator('[data-view="single"]').click(); self.ready(page); self.settle(page)
                        box = panel.bounding_box(); stars = page.locator('#stars').bounding_box()
                        self.assertAlmostEqual(box['width'], stars['width'], delta=10)
                        self.assertGreater(box['height'], stars['height'])
                        # 面板不吃掉滚轮；照片缩放与切图仍然工作。
                        self.assertEqual(panel.evaluate('e=>getComputedStyle(e).pointerEvents'), 'none')
                        page.mouse.move(box['x']+box['width']/2, box['y']+box['height']/2)
                        page.mouse.wheel(0,-100)
                        page.wait_for_function('()=>Number(document.getElementById("thumb-size").value)>100')
                        page.locator('#next').click()
                        page.wait_for_function('()=>document.querySelector("#preview-histogram")?.dataset.photoId===Framekeep.remoteTarget().photoId&&document.querySelector("#preview-histogram")?.dataset.state==="ready"')
                        self.assertAlmostEqual(self.peak(page,'green'), 190, delta=2)
                        reads = page.evaluate('histogramReads')
                        page.evaluate('()=>{for(let i=0;i<100;i++)document.dispatchEvent(new Event("framekeep:selection"))}')
                        self.settle(page); self.assertEqual(page.evaluate('histogramReads'), reads)
                        def position(kind, value):
                            page.locator(f'#preview-{kind}-position').evaluate('(e,value)=>{e.value=value;e.dispatchEvent(new Event("change",{bubbles:true}))}', value)
                            self.settle(page)
                        # 两项选同一右下角时必须形成两排，靠右对齐且留有间距。
                        position('ocr','hidden')
                        for pair in combinations(['rating','zoom','histogram'],2):
                            for kind in ['rating','zoom','histogram']:position(kind,'bottom-right' if kind in pair else 'hidden')
                            self.settle(page)
                            nodes={'rating':'#stars','zoom':'.thumbnail-control','histogram':'#preview-histogram'}
                            first,second=[page.locator(nodes[k]).bounding_box() for k in pair]
                            self.assertIsNotNone(first);self.assertIsNotNone(second)
                            self.assertAlmostEqual(first['x']+first['width'],second['x']+second['width'],delta=1)
                            upper,lower=sorted([first,second],key=lambda b:b['y'])
                            self.assertGreaterEqual(lower['y']-upper['y']-upper['height'],7)
                            page.screenshot(path=str(OUT/('same-corner-'+'-'.join(pair)+'.png')))
                        position('histogram','bottom-right')
                        position('rating','bottom-right'); position('zoom','bottom-right')
                        position('ocr','bottom-right')
                        page.evaluate('()=>FramekeepOcrFeedback.begin({session_id:FRAMEKEEP_SESSION.id,capture_id:Framekeep.remoteTarget().photoId,running:true})')
                        for theme in ['dark','light','pure-white','hardcore']:
                            page.evaluate('(t)=>FramekeepTheme.set(t)', theme)
                            for width in [1680,1024,600]:
                                page.set_viewport_size({'width':width,'height':1050}); self.settle(page)
                                self.assertTrue(panel.is_visible())
                                bounds = panel.bounding_box(); surface = page.locator('.preview-surface').bounding_box()
                                self.assertGreaterEqual(bounds['y'], surface['y'])
                                self.assertLessEqual(bounds['x']+bounds['width'], surface['x']+surface['width'])
                                for selector in ['#stars','.thumbnail-control','#preview-ocr-feedback']:
                                    obstacle = page.locator(selector).bounding_box()
                                    if obstacle:
                                        self.assertTrue(bounds['x']+bounds['width']<=obstacle['x'] or obstacle['x']+obstacle['width']<=bounds['x'] or bounds['y']+bounds['height']<=obstacle['y'] or obstacle['y']+obstacle['height']<=bounds['y'], selector)
                                controls=[page.locator(s).bounding_box() for s in ['#stars','.thumbnail-control','#preview-ocr-feedback','#preview-histogram']]
                                for a,b in combinations([b for b in controls if b],2):
                                    self.assertTrue(a['x']+a['width']<=b['x'] or b['x']+b['width']<=a['x'] or a['y']+a['height']<=b['y'] or b['y']+b['height']<=a['y'])
                                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                            page.set_viewport_size({'width':1680,'height':1050}); self.settle(page)
                            page.screenshot(path=str(OUT/f'{theme}.png'))
                        position('rating','bottom-left'); position('zoom','hidden'); position('ocr','top-right')
                        # 使用真实设置入口、下拉框和滑杆，确认四角/隐藏及百分比保存。
                        self.assertEqual(page.locator('#preview-histogram-position').input_value(), 'bottom-right')
                        def histogram_settings(location, transparency):
                            page.locator('#app-settings-open').click()
                            page.locator('[data-settings-section="preview"]').click()
                            select = page.locator('#preview-histogram-position')
                            select.select_option(location)
                            slider = page.locator('#preview-histogram-transparency')
                            slider.fill(str(transparency)); slider.dispatch_event('input')
                            self.assertEqual(page.locator('#preview-histogram-transparency-value').inner_text(), f'{transparency}%')
                            page.locator('#app-settings-close').click(); self.settle(page)
                        for location in ['top-left','top-right','bottom-left','bottom-right']:
                            histogram_settings(location, 35)
                            self.ready(page)
                            # 在直方图所选角落放置其他三个控件，验证上下两方向避让。
                            for kind in ['rating','zoom','ocr']:position(kind,location)
                            page.evaluate('()=>FramekeepOcrFeedback.begin({session_id:FRAMEKEEP_SESSION.id,capture_id:Framekeep.remoteTarget().photoId,running:true})')
                            for theme in ['dark','light','pure-white','hardcore']:
                                page.evaluate('(t)=>FramekeepTheme.set(t)', theme)
                                for width in [1680,1024,600]:
                                    page.set_viewport_size({'width':width,'height':1050}); self.settle(page)
                                    self.assertTrue(panel.is_visible())
                                    self.assertAlmostEqual(float(panel.evaluate('e=>getComputedStyle(e).opacity')), .65)
                                    bounds=panel.bounding_box(); surface=page.locator('.preview-surface').bounding_box()
                                    self.assertGreaterEqual(bounds['y'],surface['y'])
                                    self.assertLessEqual(bounds['y']+bounds['height'],surface['y']+surface['height'])
                                    edge=bounds['x']-surface['x'] if location.endswith('left') else surface['x']+surface['width']-bounds['x']-bounds['width']
                                    self.assertAlmostEqual(edge,12,delta=1)
                                    for selector in ['#stars','.thumbnail-control','#preview-ocr-feedback']:
                                        obstacle=page.locator(selector).bounding_box()
                                        if obstacle:self.assertTrue(bounds['y']+bounds['height']<=obstacle['y'] or obstacle['y']+obstacle['height']<=bounds['y'],(location,selector))
                                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                            page.set_viewport_size({'width':1680,'height':1050});self.settle(page)
                        histogram_settings('hidden',0);self.assertTrue(panel.is_hidden())
                        histogram_settings('top-left',100);self.assertTrue(panel.is_hidden())
                        histogram_settings('top-left',42);self.ready(page)
                        page.reload(wait_until='domcontentloaded');self.ready(page)
                        self.assertEqual(page.locator('#preview-histogram-position').input_value(),'top-left')
                        self.assertEqual(page.locator('#preview-histogram-transparency').input_value(),'42')
                        self.assertAlmostEqual(float(panel.evaluate('e=>getComputedStyle(e).opacity')),.58)
                        page.locator('#app-settings-open').click();page.locator('[data-settings-section="preview"]').click()
                        slider=page.locator('#preview-histogram-transparency');slider.press('Home');slider.press('ArrowRight')
                        self.assertEqual(slider.input_value(),'1')
                        page.locator('#preview-histogram-position').scroll_into_view_if_needed()
                        page.screenshot(path=str(OUT/'histogram-settings.png'))
                        for theme in ['dark','light','pure-white','hardcore']:
                            page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                            for width in [1920,1680,1024,600,375,320]:
                                page.set_viewport_size({'width':width,'height':1050});self.settle(page)
                                cards=[page.locator('.workspace-card').filter(has=page.locator(f'#preview-{kind}-position')).bounding_box() for kind in ['rating','zoom','histogram','ocr']]
                                rating,zoom,histogram,ocr=cards
                                if width>1000:
                                    self.assertAlmostEqual(rating['y'],zoom['y'],delta=1)
                                    self.assertAlmostEqual(histogram['y'],ocr['y'],delta=1)
                                    self.assertGreater(zoom['x'],rating['x']+rating['width'])
                                    self.assertGreater(ocr['x'],histogram['x']+histogram['width'])
                                    self.assertAlmostEqual(histogram['width'],ocr['width'],delta=1)
                                    area=page.locator('#settings-preview').bounding_box()
                                    self.assertAlmostEqual(ocr['x']+ocr['width'],area['x']+area['width'],delta=2)
                                else:
                                    for card in cards:self.assertAlmostEqual(card['x'],rating['x'],delta=1)
                                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                            page.set_viewport_size({'width':1680,'height':1050})
                            page.locator('#preview-rating-position').scroll_into_view_if_needed();self.settle(page)
                            page.screenshot(path=str(OUT/f'settings-columns-{theme}.png'))
                        page.locator('#app-settings-close').click()
                        # 非法旧偏好回退默认位置和原始半透明外观。
                        page.evaluate('()=>{localStorage.setItem("framekeep-preview-histogram-position-v1","invalid");localStorage.setItem("framekeep-preview-histogram-transparency-v1","101")}')
                        page.reload(wait_until='domcontentloaded'); self.ready(page)
                        self.assertEqual(page.locator('#preview-histogram-position').input_value(),'bottom-right')
                        self.assertEqual(page.locator('#preview-histogram-transparency').input_value(),'0')
                        page.screenshot(path=str(OUT/'application.png'))
                        self.assertFalse(errors, errors)
                    finally:browser.close()
                self.assertEqual(originals, {p.name:p.read_bytes() for p in photos.iterdir()})
            finally:
                http.shutdown(); http.server_close()
                for item in reversed(patches):item.stop()

    def test_delayed_switch_error_empty_and_cache(self):
        """受控图片请求验证快速切图，旧请求不能覆盖新照片统计。"""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page(viewport={'width':800,'height':600})
                errors = []; page.on('pageerror', lambda e:errors.append(str(e)))
                pending = {}
                page.route('http://histogram.test/**', lambda route:route.fulfill(body='<html></html>', content_type='text/html') if route.request.url=='http://histogram.test/' else pending.update({route.request.url:route}))
                page.goto('http://histogram.test/', wait_until='domcontentloaded')
                page.set_content('<style>*{box-sizing:border-box}:root{--theme-panel:#222;--theme-text:#eee;--theme-text-soft:#ddd;--theme-muted:#999;--theme-border:#444;--theme-shadow:#0008}.preview-surface{position:relative;width:760px;height:550px}</style><div class="preview-surface"><div id="loupe-view" class="hidden"></div></div>')
                page.add_style_tag(path=str(ROOT/'演示方案/preview-histogram.css'))
                page.evaluate('''()=>{
                    window.current='a';window.FRAMEKEEP_SESSION={id:'fixture'};
                    window.DEMO_PHOTOS=['a','b','error'].map(id=>({id,preview:`http://histogram.test/${id}.png`}));
                    window.Framekeep={remoteTarget:()=>({photoId:current,paused:false})};
                    window.histogramReads=0;const original=CanvasRenderingContext2D.prototype.getImageData;
                    CanvasRenderingContext2D.prototype.getImageData=function(...args){histogramReads++;return original.apply(this,args)};
                }''')
                page.add_script_tag(path=str(ROOT/'演示方案/preview-histogram.js'))
                page.wait_for_function('()=>document.querySelector("#preview-histogram")?.dataset.state==="loading"')
                page.wait_for_timeout(50)
                def switch(photo):
                    page.evaluate('(id)=>{current=id;document.dispatchEvent(new Event("framekeep:selection"))}', photo)
                    self.settle(page)
                switch('b'); page.wait_for_timeout(50)
                pending['http://histogram.test/b.png'].fulfill(body=png((10,180,40)), content_type='image/png', headers={'Access-Control-Allow-Origin':'*'})
                self.ready(page); self.assertEqual(self.peak(page,'green'),180)
                # 之前的 Image 已取消请求。服务端晚返回也不得回填。
                try:pending['http://histogram.test/a.png'].fulfill(body=png((240,10,20)), content_type='image/png')
                except Exception:pass
                self.settle(page); self.assertEqual(self.peak(page,'green'),180)
                self.assertEqual(page.locator('#preview-histogram').get_attribute('data-photo-id'),'b')
                switch('error'); page.wait_for_timeout(50)
                pending['http://histogram.test/error.png'].fulfill(status=404, body='missing')
                page.wait_for_function('()=>document.querySelector("#preview-histogram")?.dataset.state==="error"')
                self.assertEqual(page.locator('#preview-histogram path[d][data-channel]').count(),0)
                reads = page.evaluate('histogramReads'); switch('b'); self.ready(page)
                self.assertEqual(page.evaluate('histogramReads'),reads)
                # 极矮区域隐藏面板，恢复尺寸后重新出现，不能发生布局循环。
                page.locator('.preview-surface').evaluate('e=>e.style.height="90px"')
                self.settle(page); self.assertFalse(page.locator('#preview-histogram').is_visible())
                page.locator('.preview-surface').evaluate('e=>e.style.height="550px"')
                self.settle(page); self.assertTrue(page.locator('#preview-histogram').is_visible())
                switch(None); self.assertTrue(page.locator('#preview-histogram').is_hidden())
                self.assertFalse(errors, errors)
            finally:browser.close()


if __name__ == '__main__':unittest.main()
