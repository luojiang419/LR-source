"""模型下拉、即时保存、重启恢复及失败回退，运行真实设置页面。"""
import unittest
from playwright.sync_api import sync_playwright
import test_photo_viewer as photo_tests
from test_photo_viewer import ORIGIN

NATIVE=r'''window.calls=[];window.qa={fail:false,hold:false};window.Native={call(action,body,id){const b=JSON.parse(body);calls.push({action,body:b});const reply=(data,error=null)=>queueMicrotask(()=>onNative('reply',{id,data,error}));if(action==='state')return reply({settings:JSON.parse(localStorage.getItem('qa-settings')||'{"theme":"dark","ocrModel":"paddle-small"}'),draft:{},summary:{}});if(action==='settings'){qa.finish=()=>{if(qa.fail)return reply(null,'保存失败');localStorage.setItem('qa-settings',JSON.stringify(b));reply(b)};if(!qa.hold)qa.finish();return}reply({})}};'''

class OcrModelSettingsTests(unittest.TestCase):
    route=photo_tests.PhotoViewerTests.route
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844},reduced_motion='reduce');self.page=self.context.new_page();self.page.set_default_timeout(5000);self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)));self.page.route(ORIGIN+'/**',self.route);self.page.add_init_script(NATIVE);self.page.goto(ORIGIN+'/assets/index.html');self.page.locator('.bottom-nav [data-nav=settings]').click();self.select=self.page.locator('#ocr-model')
    def tearDown(self):self.assertEqual(self.errors,[]);self.context.close()
    def test_six_bundled_choices_and_small_default(self):
        self.assertEqual(self.select.input_value(),'paddle-small');self.assertEqual(self.select.locator('option').evaluate_all('(nodes)=>nodes.map(n=>n.value)'),['paddle-small','paddle-tiny','paddle-medium','paddle-v5','mlkit-chinese','mlkit-latin'])
    def test_each_choice_saves_immediately_and_survives_reload(self):
        for model in ['paddle-medium','paddle-tiny','paddle-v5','mlkit-chinese','mlkit-latin','paddle-small']:
            self.select.select_option(model);self.page.wait_for_function('()=>!document.querySelector("#ocr-model").disabled');self.assertEqual(self.page.evaluate('calls.filter(c=>c.action==="settings").at(-1).body.ocrModel'),model);self.page.reload();self.page.locator('.bottom-nav [data-nav=settings]').click();self.assertEqual(self.select.input_value(),model)
    def test_save_failure_restores_previous_choice(self):
        self.page.evaluate('qa.fail=true');self.select.select_option('paddle-medium');self.page.wait_for_function('()=>!document.querySelector("#ocr-model").disabled');self.assertEqual(self.select.input_value(),'paddle-small');self.assertIn('保存失败',self.page.locator('#toast').inner_text())
    def test_pending_save_disables_selector_and_does_not_reparse_records(self):
        self.page.evaluate('qa.hold=true');self.select.select_option('paddle-medium');self.assertTrue(self.select.is_disabled());self.assertEqual(self.page.evaluate('calls.filter(c=>["save","retryRecord","start"].includes(c.action)).length'),0);self.page.evaluate('qa.finish()');self.page.wait_for_function('()=>!document.querySelector("#ocr-model").disabled')
    def test_legacy_settings_without_model_use_small(self):
        self.page.evaluate('localStorage.setItem("qa-settings",JSON.stringify({theme:"light"}))');self.page.reload();self.page.locator('.bottom-nav [data-nav=settings]').click();self.assertEqual(self.select.input_value(),'paddle-small')
    def test_four_themes_and_small_viewports(self):
        for theme in ['dark','light','pure-white','hardcore']:
            self.page.evaluate('(t)=>document.documentElement.dataset.framekeepTheme=t',theme)
            for width in [320,390,600]:
                self.page.set_viewport_size({'width':width,'height':844});self.select.scroll_into_view_if_needed();self.assertTrue(self.select.is_visible());self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))

if __name__=='__main__':unittest.main()
