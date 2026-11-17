"""先保存、记录状态、后台回写和存疑核对的真实页面回归。"""
from pathlib import Path
import unittest
from playwright.sync_api import sync_playwright
import test_photo_viewer as photo_tests
from test_photo_viewer import ORIGIN

NATIVE=r'''window.calls=[];window.qa={fail:false,hold:false};window.records=[{id:'pending',sku:'',mode:'in',ocr_status:'pending',ocr_revision:0},{id:'suspect',sku:'',mode:'out',ocr_status:'review',ocr_revision:2,recognition:{text:'LC1234-P10\nLC5678-P20',preferred:['LC1234-P10','LC5678-P20'],candidates:['LC1234-P10','LC5678-P20','160']}},{id:'ready',sku:'LC9999-P30',mode:'in',ocr_status:'ready',ocr_revision:0}].map(r=>({...r,at:'2026-11-06T10:00:00+08:00',synced:0}));window.Native={call(action,body,id){const b=JSON.parse(body);calls.push({action,body:b});const reply=(data,error=null)=>queueMicrotask(()=>onNative('reply',{id,data,error}));const summary={in:2,out:1,total:3,pending:1};if(action==='state')return reply({settings:{theme:'dark'},draft:{},summary});if(action==='list')return reply({rows:records,summary});if(action==='record')return reply(records.find(r=>r.id===b.id));if(action==='confirmRecord'||action==='retryRecord'){qa.finish=()=>{if(qa.fail)return reply(null,'记录已变化，请重新核对');const r=records.find(r=>r.id===b.id);r.ocr_status=action==='confirmRecord'?'ready':'pending';r.sku=b.sku||'';r.ocr_revision++;reply(r)};if(!qa.hold)qa.finish();return}if(action==='enqueueDraft')return reply({});reply({})}};'''

class BackgroundOcrTests(unittest.TestCase):
    route=photo_tests.PhotoViewerTests.route
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,reduced_motion='reduce');self.page=self.context.new_page();self.page.set_default_timeout(5000);self.errors=[];self.dialogs=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)));self.page.on('dialog',lambda d:(self.dialogs.append(d.message),d.dismiss()));self.page.route(ORIGIN+'/**',self.route);self.page.add_init_script(NATIVE);self.page.goto(ORIGIN+'/assets/index.html');self.page.locator('.bottom-nav [data-nav=records]').click();self.page.wait_for_selector('.record-swipe')
    def tearDown(self):self.assertEqual(self.errors,[]);self.assertEqual(self.dialogs,[]);self.context.close()
    def row(self,id):return self.page.locator(f'.record-swipe[data-id="{id}"]')
    def review(self):self.row('suspect').locator('strong').click();self.page.wait_for_selector('#record-review-dialog[open]')
    def test_pending_and_suspect_visible_without_forced_review(self):
        self.assertEqual(self.row('pending').locator('strong').inner_text(),'解析中');self.assertEqual(self.row('suspect').locator('strong').inner_text(),'存疑');self.assertFalse(self.page.locator('dialog[open]').count());self.assertEqual(self.page.locator('#count-in').inner_text(),'2件')
    def test_background_result_updates_original_row_without_new_save(self):
        self.page.evaluate('()=>{const r=records[0];r.sku="LC7877529-P105";r.ocr_status="ready";onNative("recordUpdated",{record:r,summary:{in:2,out:1,total:3,pending:2}})}');self.assertEqual(self.row('pending').locator('strong').inner_text(),'LC7877529-P105');self.assertEqual(self.page.locator('.record-swipe').count(),3);self.assertEqual(self.page.evaluate('calls.filter(c=>["save","start"].includes(c.action)).length'),0)
    def test_suspect_opens_both_photos_and_preferred_candidates(self):
        self.review();self.assertEqual(self.page.locator('#record-candidates .preferred').count(),2);self.assertIn('/suspect/product',self.page.locator('#record-review-product').get_attribute('src'));self.page.locator('[data-record-choice="LC5678-P20"]').click();self.assertEqual(self.page.locator('#record-sku').input_value(),'LC5678-P20');self.page.locator('#record-sku').fill('LC5678-P21');self.page.locator('#record-confirm').click();self.page.wait_for_selector('#record-review-dialog',state='hidden');self.assertEqual(self.row('suspect').locator('strong').inner_text(),'LC5678-P21');self.assertEqual(self.page.evaluate('calls.find(c=>c.action==="confirmRecord").body'),{'id':'suspect','revision':2,'sku':'LC5678-P21'});self.assertEqual(self.page.locator('#count-out').inner_text(),'1件')
    def test_failure_preserves_manual_text_and_reenables_save(self):
        self.review();self.page.evaluate('qa.fail=true');self.page.locator('#record-sku').fill('LC-MANUAL-10');self.page.locator('#record-confirm').click();self.page.wait_for_function('()=>!document.querySelector("#record-confirm").disabled');self.assertTrue(self.page.locator('#record-review-dialog').is_visible());self.assertEqual(self.page.locator('#record-sku').input_value(),'LC-MANUAL-10');self.assertIn('记录已变化',self.page.locator('#toast').inner_text())
    def test_retry_returns_row_to_pending(self):
        self.review();self.page.locator('#record-retry').click();self.page.wait_for_selector('#record-review-dialog',state='hidden');self.assertEqual(self.row('suspect').locator('strong').inner_text(),'解析中');self.assertEqual(self.page.evaluate('calls.find(c=>c.action==="retryRecord").body'),{'id':'suspect','revision':2})
    def test_pending_success_does_not_wait_for_ocr_or_show_blank_sku(self):
        self.page.evaluate('onNative("saved",{id:"new",mode:"in",sku:"",ocr_status:"pending"})');self.assertTrue(self.page.locator('#camera-screen').is_visible());self.assertFalse(self.page.locator('#review-dialog').is_visible());self.assertEqual(self.page.locator('#toast').inner_text(),'入库成功');self.assertEqual(self.page.locator('#camera-copy').inner_text(),'双图已保存');self.page.evaluate('onNative("nextCapture",{mode:"in",stage:"product"})');self.assertEqual(self.page.locator('#camera-title').inner_text(),'先拍产品外观')
    def test_review_photo_and_hardware_back_preserve_edit(self):
        self.review();self.page.locator('#record-product-photo').click();self.assertTrue(self.page.locator('#photo-dialog').is_visible());self.page.evaluate('onNative("back",{})');self.page.wait_for_selector('#photo-dialog',state='hidden');self.assertTrue(self.page.locator('#record-review-dialog').is_visible());self.page.evaluate('onNative("back",{})');self.page.wait_for_selector('#record-review-dialog',state='hidden');self.assertTrue(self.page.locator('#records').is_visible())
    def test_pending_confirm_prevents_double_submission(self):
        self.review();self.page.evaluate('qa.hold=true');self.page.locator('#record-confirm').click();self.assertTrue(self.page.locator('#record-confirm').is_disabled());self.page.evaluate('document.querySelector("#record-confirm").click()');self.assertEqual(self.page.evaluate('calls.filter(c=>c.action==="confirmRecord").length'),1);self.page.evaluate('qa.finish()');self.page.wait_for_selector('#record-review-dialog',state='hidden')
    def test_legacy_completed_draft_is_enqueued_without_ocr_dialog(self):
        self.page.evaluate('()=>{const original=Native.call;Native.call=(action,body,id)=>action==="state"?queueMicrotask(()=>onNative("reply",{id,data:{settings:{},summary:{},draft:{id:"old",stage:"review"}}})):original(action,body,id);onNative("state",{draft:{id:"old",stage:"review"}})}');self.page.locator('.bottom-nav [data-nav=home]').click();self.page.locator('#resume-draft').click();self.page.wait_for_function('()=>calls.some(c=>c.action==="enqueueDraft")');self.assertFalse(self.page.locator('#review-dialog').is_visible())
    def test_small_screen_and_four_themes(self):
        output=Path(__file__).resolve().parents[2]/'build/inventory-background-ocr';output.mkdir(parents=True,exist_ok=True);self.review()
        for theme in ['dark','light','pure-white','hardcore']:
            self.page.evaluate('(v)=>document.documentElement.dataset.framekeepTheme=v',theme)
            for width in [320,390,600]:
                self.page.set_viewport_size({'width':width,'height':844});self.assertTrue(self.page.locator('#record-confirm').is_visible());self.assertFalse(self.page.locator('#record-review-dialog').evaluate('(d)=>d.scrollWidth>d.clientWidth'))
                if width==390:self.page.screenshot(path=str(output/f'review-{theme}.png'))

if __name__=='__main__':unittest.main()
