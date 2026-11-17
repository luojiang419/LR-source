"""验证真实页面的入库接续、用户退出与成功弹层兼容。"""
import unittest
from playwright.sync_api import sync_playwright
import test_photo_viewer as photo_tests
from test_photo_viewer import ORIGIN

NATIVE=r'''window.calls=[];window.Native={call(action,body,id){const b=JSON.parse(body);calls.push({action,body:b});const data=action==='state'?{settings:{theme:'dark',rules:'LC',sound:true},draft:{},summary:{in:1,out:0,total:1,pending:1}}:action==='list'?{rows:[],summary:{in:1,out:0,total:1,pending:1}}:action==='start'?{id:'manual',mode:b.mode,stage:'product'}:{};queueMicrotask(()=>window.onNative('reply',{id,data,error:null}))}};'''

class AutoNextTests(unittest.TestCase):
    route=photo_tests.PhotoViewerTests.route
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844},reduced_motion='reduce');self.page=self.context.new_page();self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)));self.page.route(ORIGIN+'/**',self.route);self.page.add_init_script(NATIVE);self.page.goto(ORIGIN+'/assets/index.html');self.page.wait_for_function('()=>document.querySelector("#count-in").textContent.startsWith("1")')
    def tearDown(self):self.assertEqual(self.errors,[]);self.context.close()
    def saved(self,mode='in'):self.page.evaluate('(mode)=>onNative("saved",{id:"saved-record",mode,sku:"LC7877529-P105"})',mode)
    def next(self):self.page.evaluate('()=>onNative("nextCapture",{id:"new-draft",mode:"in",stage:"product"})')
    def test_inbound_success_waits_then_shows_product_camera_without_modal(self):
        self.saved();self.assertTrue(self.page.locator('#camera-screen').is_visible());self.assertEqual(self.page.locator('#camera-title').inner_text(),'入库成功');self.assertTrue(self.page.locator('#shutter').is_disabled());self.assertFalse(self.page.locator('#success-dialog').is_visible());self.assertEqual(self.page.evaluate('calls.filter(x=>x.action==="stopCamera").length'),0);self.next();self.assertEqual(self.page.locator('#camera-title').inner_text(),'先拍产品外观');self.assertTrue(self.page.locator('#shutter').is_disabled());self.page.evaluate('()=>onNative("camera",{ready:true,flash:true})');self.assertFalse(self.page.locator('#shutter').is_disabled());self.assertEqual(self.page.evaluate('calls.filter(x=>x.action==="start").length'),0)
    def test_exit_before_voice_finishes_does_not_reopen_camera(self):
        self.saved();self.page.locator('#camera-back').click();self.next();self.assertFalse(self.page.locator('#camera-screen').is_visible());self.assertTrue(self.page.locator('#home').is_visible());self.assertGreater(self.page.evaluate('calls.filter(x=>x.action==="cancelNextCapture").length'),0)
    def test_hardware_back_while_review_is_closing_exits_in_one_step(self):
        self.page.locator('#review-dialog').evaluate('n=>n.showModal()');self.saved();self.page.evaluate('()=>onNative("back",{})');self.next();self.assertFalse(self.page.locator('#review-dialog').is_visible());self.assertFalse(self.page.locator('#camera-screen').is_visible())
    def test_open_photo_does_not_block_next_inbound_camera(self):
        self.page.locator('#photo-dialog').evaluate('n=>n.showModal()');self.saved();self.page.wait_for_selector('#photo-dialog',state='hidden');self.next();self.assertTrue(self.page.locator('#camera-screen').is_visible());self.assertEqual(self.page.locator('#camera-title').inner_text(),'先拍产品外观')
    def test_outbound_manual_next_flow_still_works(self):
        self.saved('out');self.page.wait_for_selector('#success-dialog[open]');self.assertEqual(self.page.evaluate('calls.filter(x=>x.action==="start").length'),0);self.page.locator('#scan-next').click();self.page.wait_for_function('()=>document.querySelector("#camera-screen").hidden===false');self.assertEqual(self.page.evaluate('calls.find(x=>x.action==="start").body.mode'),'out')

if __name__=='__main__':unittest.main()
