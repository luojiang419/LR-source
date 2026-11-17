"""拍摄任意位置点击、反馈、防连拍和首页操作优先级。"""
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
import test_photo_viewer as photo_tests
from test_photo_viewer import ORIGIN

NATIVE=r'''window.calls=[];window.qa={draft:{},accepted:0,failNext:false};window.Native={call(action,body,id){const b=JSON.parse(body);calls.push({action,body:b});const reply=(data,error=null)=>queueMicrotask(()=>onNative('reply',{id,data,error}));if(action==='state')return reply({settings:{theme:'dark',rules:'LC',sound:true},draft:qa.draft,summary:{in:4,out:1,total:5,pending:2}});if(action==='list')return reply({rows:[],summary:{in:4,out:1,total:5,pending:2}});if(action==='start'||action==='resume'||action==='retake'){qa.draft={id:'qa-draft',mode:b.mode||'in',stage:'product'};reply(qa.draft);return queueMicrotask(()=>onNative('camera',{ready:true,flash:true}))}if(action==='torch')return reply({on:true});if(action==='capture'){if(qa.failNext){qa.failNext=false;return reply(null,'拍摄失败，请重试')}qa.accepted++;const kind=qa.draft.stage;queueMicrotask(()=>onNative('capturing',{kind}));window.finishShot=()=>{qa.draft={...qa.draft,stage:kind==='product'?'label':'review'};onNative('draft',qa.draft);reply(qa.draft)};return}reply({})}};'''

class CaptureLayoutTests(unittest.TestCase):
    route=photo_tests.PhotoViewerTests.route
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844},has_touch=True,is_mobile=True,reduced_motion='reduce');self.page=self.context.new_page();self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)));self.page.route(ORIGIN+'/**',self.route);self.page.add_init_script(NATIVE);self.page.goto(ORIGIN+'/assets/index.html');self.page.wait_for_function('()=>document.querySelector("#count-in").textContent.startsWith("4")')
    def tearDown(self):self.assertEqual(self.errors,[]);self.context.close()
    def start(self):self.page.locator('[data-start=in]').click();self.page.wait_for_function('()=>!document.querySelector("#shutter").disabled')
    def test_scan_card_first_large_buttons_and_compact_counts_all_themes(self):
        out=Path(__file__).resolve().parents[2]/'build/inventory-capture-feedback';out.mkdir(parents=True,exist_ok=True)
        for theme in ['dark','light','pure-white','hardcore']:
            self.page.evaluate('(t)=>document.documentElement.dataset.framekeepTheme=t',theme)
            for width,height in [(320,568),(390,844),(600,800)]:
                self.page.set_viewport_size({'width':width,'height':height});card=self.page.locator('#home>.capture-card').bounding_box();summary=self.page.locator('#home>.summary').bounding_box();self.assertLess(card['y'],summary['y']);self.assertGreater(card['height'],summary['height']);nav=self.page.locator('.bottom-nav').bounding_box()
                for mode in ['in','out']:
                    button=self.page.locator('[data-start='+mode+']').bounding_box();self.assertGreaterEqual(button['height'],140);self.assertLess(button['y']+button['height'],nav['y'])
                if height>=700:
                    header=self.page.locator('.brand').bounding_box();top_gap=card['y']-header['y']-header['height'];bottom_gap=nav['y']-summary['y']-summary['height'];self.assertAlmostEqual(top_gap,bottom_gap,delta=3,msg=(theme,width,height,top_gap,bottom_gap))
                else:
                    self.page.evaluate('window.scrollTo(0,document.body.scrollHeight)');summary_end=self.page.locator('#home>.summary').bounding_box();self.assertLessEqual(summary_end['y']+summary_end['height'],nav['y']+1);self.page.evaluate('window.scrollTo(0,0)')
                self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                if width==390:self.page.screenshot(path=str(out/('home-'+theme+'.png')))
        self.assertEqual(self.page.locator('.page-title,.eyebrow').count(),0);self.assertEqual(self.page.locator('#recent').count(),0);self.assertEqual(self.page.locator('#home > *').count(),2)
        for text in ['每一件，都有迹可循','出入有据','手机采集，电脑归档','你的工作节奏']:
            self.assertNotIn(text,self.page.locator('body').inner_text())
    def test_viewfinder_tap_triggers_one_shot_and_visible_flash(self):
        self.start();self.page.touchscreen.tap(180,330);self.page.wait_for_function('()=>qa.accepted===1');self.assertTrue(self.page.locator('#capture-flash').is_visible());self.assertGreater(float(self.page.locator('#capture-flash').evaluate('n=>getComputedStyle(n).opacity')),0);self.assertTrue(self.page.locator('#shutter').is_disabled());self.page.evaluate('finishShot()');self.page.wait_for_function('()=>document.querySelector("#camera-title").textContent==="再拍 SKU 标签"');self.page.wait_for_selector('#capture-flash',state='hidden');self.page.wait_for_function('()=>!document.querySelector("#shutter").disabled')
    def test_controls_do_not_take_photos(self):
        self.start();self.page.locator('#flash').click();self.page.locator('#import-photo').click();self.assertEqual(self.page.evaluate('qa.accepted'),0);self.page.locator('#camera-back').click();self.assertEqual(self.page.evaluate('qa.accepted'),0)
    def test_shutter_does_not_bubble_into_second_capture_and_double_taps_debounce(self):
        self.start();self.page.locator('#shutter').click();self.assertEqual(self.page.evaluate('qa.accepted'),1);self.page.touchscreen.tap(180,330);self.assertEqual(self.page.evaluate('qa.accepted'),1);self.page.evaluate('finishShot()');self.page.touchscreen.tap(180,330);self.assertEqual(self.page.evaluate('qa.accepted'),1);self.page.wait_for_function('()=>!document.querySelector("#shutter").disabled');self.page.touchscreen.tap(180,330);self.assertEqual(self.page.evaluate('qa.accepted'),2)
    def test_dragging_camera_background_does_not_shoot(self):
        self.start();self.page.mouse.move(130,320);self.page.mouse.down();self.page.mouse.move(230,400,steps=4);self.page.mouse.up();self.assertEqual(self.page.evaluate('qa.accepted'),0)
    def test_rejected_capture_has_no_flash_and_can_retry(self):
        self.start();self.page.evaluate('qa.failNext=true');self.page.touchscreen.tap(180,330);self.page.wait_for_function('()=>document.querySelector("#toast").textContent.includes("拍摄失败")');self.assertTrue(self.page.locator('#capture-flash').is_hidden());self.assertFalse(self.page.locator('#shutter').is_disabled());self.assertEqual(self.page.evaluate('qa.accepted'),0)

if __name__=='__main__':unittest.main()
