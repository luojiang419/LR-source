"""浏览器运行实际 APP 资源，验证图片查看器的触控和边界行为。"""
from pathlib import Path
import json
import mimetypes
import unittest
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]
ASSETS=ROOT/'安卓出入库/app/src/main/assets'
ORIGIN='https://appassets.androidplatform.net'

STUB=r'''window.Native={call(action,body,id){const data=action==='state'?{settings:{theme:'dark',rules:'LC',sound:true,cloud:false},draft:{},summary:{in:1,out:1,total:2,pending:2}}:action==='list'?{rows:[{id:'portrait',mode:'in',sku:'LC-PORTRAIT-001',at:'2026-11-05T08:00:00+08:00',synced:0},{id:'landscape',mode:'out',sku:'LC-LANDSCAPE-002',at:'2026-11-05T09:00:00+08:00',synced:0}],summary:{in:1,out:1,total:2,pending:2}}:{};queueMicrotask(()=>window.onNative('reply',{id,data,error:null}))}};'''

class PhotoViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,reduced_motion='reduce')
        self.page=self.context.new_page();self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)))
        self.page.route(ORIGIN+'/**',self.route);self.page.add_init_script(STUB)
        self.page.goto(ORIGIN+'/assets/index.html');self.page.locator('.bottom-nav [data-nav=records]').click()
        self.page.locator('#record-rows [data-photo]').first.click();self.page.wait_for_function('()=>document.querySelector("#full-photo").naturalWidth>0&&!document.querySelector("#photo-zoom-in").disabled')
        self.cdp=self.context.new_cdp_session(self.page)
    def tearDown(self):
        self.assertEqual(self.errors,[]);self.context.close()
    def route(self,route):
        path=route.request.url.removeprefix(ORIGIN).split('?',1)[0]
        if path.startswith('/assets/'):
            file=ASSETS/path.removeprefix('/assets/')
            if file.is_file():return route.fulfill(body=file.read_bytes(),content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        if path.startswith('/images/'):
            w,h=(600,900) if '/portrait/' in path else (1200,600)
            svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"><rect width="100%" height="100%" fill="#5c7e9b"/><path d="M0 {h//2}H{w}M{w//2} 0V{h}" stroke="white" stroke-width="4"/><text x="20" y="50" fill="white">SKU test fixture</text></svg>'
            return route.fulfill(body=svg,content_type='image/svg+xml')
        route.fulfill(status=404,body='not found')
    def geometry(self):
        return self.page.evaluate('''()=>{const stage=document.querySelector('#photo-stage').getBoundingClientRect(),image=document.querySelector('#full-photo').getBoundingClientRect(),m=new DOMMatrix(getComputedStyle(document.querySelector('#full-photo')).transform);return {stage:{x:stage.x,y:stage.y,w:stage.width,h:stage.height},image:{x:image.x,y:image.y,w:image.width,h:image.height},scale:m.a,x:m.e,y:m.f,bodyWidth:innerWidth}}''')
    def touch(self,event,points):
        self.cdp.send('Input.dispatchTouchEvent',{'type':event,'touchPoints':[{'x':x,'y':y,'id':i,'radiusX':2,'radiusY':2,'force':1} for i,x,y in points]})
    def test_fullscreen_fit_and_controls(self):
        bounds=self.page.locator('#photo-dialog').bounding_box();self.assertEqual(bounds,{'x':0,'y':0,'width':390,'height':844})
        self.assertEqual(self.page.locator('#photo-scale').inner_text(),'100%');self.assertTrue(self.page.locator('#photo-zoom-out').is_disabled())
        self.page.locator('#photo-zoom-in').click();self.assertEqual(self.page.locator('#photo-scale').inner_text(),'150%')
        self.page.locator('#photo-fit').click();self.assertAlmostEqual(self.geometry()['scale'],1)
    def test_double_tap_zoom_and_restore(self):
        g=self.geometry();x=g['stage']['x']+g['stage']['w']/2;y=g['stage']['y']+g['stage']['h']/2
        for _ in range(2):self.touch('touchStart',[(1,x,y)]);self.touch('touchEnd',[])
        self.assertAlmostEqual(self.geometry()['scale'],3,delta=.01)
        self.page.wait_for_timeout(350)
        for _ in range(2):self.touch('touchStart',[(1,x,y)]);self.touch('touchEnd',[])
        self.assertAlmostEqual(self.geometry()['scale'],1,delta=.01)
    def test_pinch_preserves_anchor_then_one_finger_pan(self):
        g=self.geometry();cx=g['stage']['x']+g['stage']['w']/2;cy=g['stage']['y']+g['stage']['h']/2
        self.touch('touchStart',[(1,cx-30,cy-40),(2,cx+50,cy+40)])
        self.touch('touchMove',[(1,cx-70,cy-80),(2,cx+90,cy+80)])
        g=self.geometry();self.assertAlmostEqual(g['scale'],2,delta=.02);self.assertAlmostEqual(g['x'],-10,delta=1)
        # CDP 的 touchEnd 列出抬起的触点，保留第一根手指继续拖动。
        self.touch('touchEnd',[(2,cx+90,cy+80)]);self.touch('touchMove',[(1,cx-40,cy-40)]);self.touch('touchEnd',[])
        moved=self.geometry();self.assertAlmostEqual(moved['x']-g['x'],30,delta=1);self.assertAlmostEqual(moved['y']-g['y'],40,delta=1)
    def test_drag_cannot_lose_picture_outside_viewport(self):
        self.page.locator('#photo-zoom-in').click();self.page.locator('#photo-zoom-in').click();g=self.geometry();x=g['stage']['w']/2;y=g['stage']['y']+g['stage']['h']/2
        self.touch('touchStart',[(1,x,y)]);self.touch('touchMove',[(1,390,830)]);self.touch('touchEnd',[])
        g=self.geometry();s=g['stage'];im=g['image'];self.assertLessEqual(im['x'],s['x']+.1);self.assertGreaterEqual(im['x']+im['w'],s['x']+s['w']-.1)
        self.assertLessEqual(im['y'],s['y']+.1);self.assertGreaterEqual(im['y']+im['h'],s['y']+s['h']-.1)
    def test_scale_limits_and_next_photo_reset(self):
        for _ in range(9):
            if not self.page.locator('#photo-zoom-in').is_disabled():self.page.locator('#photo-zoom-in').click()
        self.assertAlmostEqual(self.geometry()['scale'],8);self.assertTrue(self.page.locator('#photo-zoom-in').is_disabled())
        self.page.locator('#photo-close').click();self.page.locator('#record-rows [data-photo]').nth(2).click();self.page.wait_for_function('()=>document.querySelector("#full-photo").naturalWidth===1200')
        g=self.geometry();self.assertAlmostEqual(g['scale'],1);self.assertAlmostEqual(g['x'],0);self.assertAlmostEqual(g['y'],0)
    def test_pointer_cancel_does_not_leave_drag_active(self):
        g=self.geometry();x=g['stage']['w']/2;y=g['stage']['y']+g['stage']['h']/2
        self.touch('touchStart',[(1,x,y)]);self.touch('touchCancel',[])
        self.assertFalse(self.page.locator('#photo-stage').evaluate('n=>n.classList.contains("is-interacting")'))
        self.page.locator('#photo-zoom-in').click();self.assertAlmostEqual(self.geometry()['scale'],1.5)
    def test_mouse_wheel_keyboard_and_escape(self):
        self.page.mouse.move(195,400);self.page.mouse.wheel(0,-300);self.page.wait_for_function('()=>parseInt(document.querySelector("#photo-scale").textContent)>100');self.page.locator('#photo-stage').focus();self.page.keyboard.press('0');self.assertAlmostEqual(self.geometry()['scale'],1)
        self.page.keyboard.press('Escape');self.page.wait_for_selector('#photo-dialog',state='hidden');self.assertTrue(self.page.locator('#record-rows').is_visible())
    def test_theme_changes_and_viewport_resize_keep_controls_visible(self):
        for theme in ['dark','light','pure-white','hardcore']:
            self.page.evaluate('(t)=>document.documentElement.dataset.framekeepTheme=t',theme)
            for width,height in [(320,568),(600,800)]:
                self.page.set_viewport_size({'width':width,'height':height});self.page.wait_for_function('()=>document.querySelector("#photo-stage").clientWidth===innerWidth');self.assertTrue(self.page.locator('#photo-close').is_visible());self.assertTrue(self.page.locator('#photo-fit').is_visible());self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))

if __name__=='__main__':unittest.main()
