"""导出目的地、范围、异步防重入和反馈的真实页面回归。"""
import unittest
from playwright.sync_api import sync_playwright
import test_photo_viewer as photo_tests
from test_photo_viewer import ORIGIN

NATIVE=r'''window.calls=[];window.qa={hold:false,fail:false};window.Native={call(action,body,id){const b=JSON.parse(body);calls.push({action,body:b});const reply=(data,error=null)=>queueMicrotask(()=>onNative('reply',{id,data,error}));if(action==='state')return reply({settings:{theme:'dark'},draft:{},summary:{in:1,out:0}});if(action==='list')return reply({rows:[]});if(action==='export'){qa.finish=()=>reply({destination:b.destination},qa.fail?'导出失败：测试错误':null);if(!qa.hold)qa.finish();return}reply({})}};'''

class ExportDestinationTests(unittest.TestCase):
    route=photo_tests.PhotoViewerTests.route
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844},reduced_motion='reduce');self.page=self.context.new_page();self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)));self.page.route(ORIGIN+'/**',self.route);self.page.add_init_script(NATIVE);self.page.goto(ORIGIN+'/assets/index.html');self.page.locator('[data-nav=records]').click();self.page.locator('#export').click()
    def tearDown(self):self.assertEqual(self.errors,[]);self.context.close()
    def exports(self):return self.page.evaluate('calls.filter(x=>x.action==="export")')
    def test_local_all_is_explicit_and_does_not_apply_record_filters(self):
        self.assertEqual(self.page.locator('#export-confirm').inner_text(),'保存到本地');self.page.locator('#export-confirm').click();self.page.wait_for_selector('#export-dialog',state='hidden');self.assertEqual(self.exports()[0]['body']['destination'],'local');self.assertEqual(self.exports()[0]['body']['scope'],'all');self.assertIn('请选择保存目录',self.page.locator('#toast').inner_text())
    def test_share_is_separate(self):
        self.page.locator('#export-share').click();self.page.wait_for_selector('#export-dialog',state='hidden');self.assertEqual(self.exports()[0]['body']['destination'],'share');self.assertIn('分享窗口',self.page.locator('#toast').inner_text())
    def test_date_range_and_mode_forwarded_for_both_destinations(self):
        for button,destination in [('#export-confirm','local'),('#export-share','share')]:
            if not self.page.locator('#export-dialog').is_visible():self.page.locator('#export').click()
            self.page.locator('[data-export-scope=dates]').click();self.page.locator('#export-start').fill('2026-11-01');self.page.locator('#export-end').fill('2026-11-05');self.page.locator('#export-mode').select_option('out');self.page.locator(button).click();self.page.wait_for_selector('#export-dialog',state='hidden');body=self.exports()[-1]['body'];self.assertEqual(body,{'destination':destination,'scope':'dates','start':'2026-11-01','end':'2026-11-05','mode':'out'})
    def test_invalid_dates_do_not_open_system_window(self):
        self.page.locator('[data-export-scope=dates]').click();self.page.locator('#export-start').fill('2026-11-05');self.page.locator('#export-end').fill('2026-11-01');self.page.locator('#export-confirm').click();self.assertEqual(self.exports(),[]);self.assertIn('结束日期不能早于',self.page.locator('#toast').inner_text());self.assertTrue(self.page.locator('#export-share').is_enabled())
    def test_generating_disables_both_actions_and_failure_can_retry(self):
        self.page.evaluate('qa.hold=true;qa.fail=true');self.page.locator('#export-confirm').click();self.assertTrue(self.page.locator('#export-confirm').is_disabled());self.assertTrue(self.page.locator('#export-share').is_disabled());self.page.evaluate('document.querySelector("#export-share").click()');self.assertEqual(len(self.exports()),1);self.page.evaluate('qa.finish()');self.page.wait_for_function('()=>!document.querySelector("#export-confirm").disabled');self.assertTrue(self.page.locator('#export-dialog').is_visible());self.assertIn('测试错误',self.page.locator('#toast').inner_text());self.page.evaluate('qa.hold=false;qa.fail=false');self.page.locator('#export-confirm').click();self.page.wait_for_selector('#export-dialog',state='hidden')
    def test_saved_cancelled_and_error_messages_are_distinct(self):
        for status,message in [('saved','表格已保存到所选位置'),('cancelled','已取消本地保存'),('error','本地保存失败，请重新导出')]:
            self.page.evaluate('(d)=>onNative("exportResult",d)',{'status':status,'message':message});self.assertEqual(self.page.locator('#toast').inner_text(),message)
    def test_both_buttons_visible_on_small_screen(self):
        self.page.set_viewport_size({'width':320,'height':568});self.page.locator('#export-share').scroll_into_view_if_needed();self.assertTrue(self.page.locator('#export-share').is_visible());self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))

if __name__=='__main__':unittest.main()
