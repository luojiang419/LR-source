"""放弃草稿的异步确认、取消和失败恢复。"""
import unittest
from playwright.sync_api import sync_playwright
import test_photo_viewer as photo_tests
from test_photo_viewer import ORIGIN

NATIVE=r'''window.calls=[];window.qa={draft:{id:'draft',mode:'out',stage:'review'},pending:null};window.Native={call(action,body,id){calls.push(action);const reply=(data,error=null)=>queueMicrotask(()=>onNative('reply',{id,data,error}));if(action==='state')return reply({settings:{theme:'dark'},draft:qa.draft,summary:{in:4,out:1}});if(action==='cancel'){qa.pending=(cancelled,error=null)=>{if(cancelled&&!error)qa.draft={};reply({cancelled},error)};return}reply({})}};'''

class DiscardDraftTests(unittest.TestCase):
    route=photo_tests.PhotoViewerTests.route
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844},reduced_motion='reduce');self.page=self.context.new_page();self.page.route(ORIGIN+'/**',self.route);self.page.add_init_script(NATIVE);self.page.goto(ORIGIN+'/assets/index.html');self.page.wait_for_function('()=>!document.querySelector("#resume-draft").hidden');self.dialogs=[];self.page.on('dialog',lambda d:(self.dialogs.append(d.message),d.dismiss()))
    def tearDown(self):self.assertEqual(self.dialogs,[]);self.context.close()
    # 兼容升级前已打开的核对事件；新版继续旧草稿会直接保存并后台解析。
    def review(self):self.page.evaluate('onNative("recognized",{automatic:false,candidates:[],preferred:[]})');self.page.wait_for_selector('#review-dialog[open]')
    def test_discard_closes_review_and_allows_new_capture(self):
        self.review();self.page.locator('#discard-draft').click();self.assertTrue(self.page.locator('#discard-draft').is_disabled());self.page.evaluate('qa.pending(true)');self.page.wait_for_selector('#review-dialog',state='hidden');self.assertTrue(self.page.locator('#home').is_visible());self.assertTrue(self.page.locator('#resume-draft').is_hidden());self.assertEqual(self.page.locator('#count-in').inner_text(),'4件');self.assertEqual(self.page.evaluate('calls.filter(x=>x==="cancel").length'),1)
    def test_keep_preserves_review_and_can_confirm_again(self):
        self.review();self.page.locator('#discard-draft').click();self.page.evaluate('qa.pending(false)');self.page.wait_for_function('()=>!document.querySelector("#discard-draft").disabled');self.assertTrue(self.page.locator('#review-dialog').is_visible());self.page.locator('#discard-draft').click();self.page.evaluate('qa.pending(true)');self.page.wait_for_selector('#review-dialog',state='hidden')
    def test_native_failure_keeps_draft_and_reenables_button(self):
        self.review();self.page.locator('#discard-draft').click();self.page.evaluate('qa.pending(false,"正在保存，请稍后")');self.page.wait_for_function('()=>!document.querySelector("#discard-draft").disabled');self.assertTrue(self.page.locator('#review-dialog').is_visible());self.assertIn('正在保存',self.page.locator('#toast').inner_text())
    def test_home_discard_uses_same_confirmation(self):
        self.page.locator('#discard-pending').click();self.page.evaluate('qa.pending(false)');self.page.wait_for_function('()=>!document.querySelector("#discard-pending").disabled');self.assertTrue(self.page.locator('#resume-draft').is_visible());self.page.locator('#discard-pending').click();self.page.evaluate('qa.pending(true)');self.page.wait_for_selector('#resume-draft',state='hidden')

if __name__=='__main__':unittest.main()
