"""真实页面触控回归；原生存储另由 InventoryTrashTest 覆盖。"""
from pathlib import Path
import unittest
from playwright.sync_api import sync_playwright
import test_photo_viewer as photo_tests
from test_photo_viewer import ORIGIN

NATIVE=r'''window.calls=[];window.qa={hold:false,fail:false};window.records=Array.from({length:65},(_,i)=>({id:'row-'+i,sku:'LC-'+String(i).padStart(3,'0'),mode:i%2?'out':'in',at:'2026-11-05T08:00:00+08:00',synced:0,deleted:false}));
window.Native={call(action,body,id){const b=JSON.parse(body);calls.push({action,body:b});const reply=(data,error=null)=>queueMicrotask(()=>onNative('reply',{id,data,error}));const summary=()=>{const r=records.filter(x=>!x.deleted);return {in:r.filter(x=>x.mode==='in').length,out:r.filter(x=>x.mode==='out').length,pending:r.length,total:r.length}};
if(action==='state')return reply({settings:{theme:'dark'},draft:{},summary:summary()});
if(action==='list'||action==='trashList'){const rows=records.filter(x=>x.deleted===(action==='trashList')&&(!b.mode||x.mode===b.mode)&&(!b.query||x.sku.includes(b.query.toUpperCase())));return reply({rows:rows.slice(b.offset,b.offset+60),summary:summary()})}
if(action==='deleteRecord'||action==='restoreRecord'){qa.finish=()=>{if(qa.fail)return reply(null,'测试删除失败');const r=records.find(x=>x.id===b.id);r.deleted=action==='deleteRecord';reply({summary:summary()})};if(!qa.hold)qa.finish();return}reply({})}};'''

class RecordTrashTests(unittest.TestCase):
    route=photo_tests.PhotoViewerTests.route
    @classmethod
    def setUpClass(cls):
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,reduced_motion='reduce');self.page=self.context.new_page();self.page.set_default_timeout(5000);self.errors=[];self.dialogs=[]
        self.page.on('pageerror',lambda e:self.errors.append(str(e)));self.page.on('dialog',lambda d:(self.dialogs.append(d.message),d.dismiss()))
        self.page.route(ORIGIN+'/**',self.route);self.page.add_init_script(NATIVE);self.page.goto(ORIGIN+'/assets/index.html');self.page.locator('.bottom-nav [data-nav=records]').click();self.page.wait_for_selector('.record-swipe');self.cdp=self.context.new_cdp_session(self.page)
    def tearDown(self):
        self.assertEqual(self.errors,[]);self.assertEqual(self.dialogs,[]);self.context.close()
    def row(self,index=0):return self.page.locator('.record-swipe').nth(index)
    def touch(self,event,x=None,y=None):
        self.cdp.send('Input.dispatchTouchEvent',{'type':event,'touchPoints':[] if x is None else [{'x':x,'y':y,'id':1,'radiusX':2,'radiusY':2,'force':1}]})
    def swipe(self,index=0,direction=-1,vertical=False,cancel=False,photo=False):
        row=self.row(index);row.scroll_into_view_if_needed();box=(row.locator('[data-photo]').first if photo else row).bounding_box();x=box['x']+box['width']/2;y=box['y']+box['height']/2
        self.touch('touchStart',x,y)
        for n in range(1,7):self.touch('touchMove',x+(0 if vertical else direction*n*12),y+(n*12 if vertical else 0))
        self.touch('touchCancel' if cancel else 'touchEnd')
    def visible_action(self,index=0):return self.row(index).locator('.record-action:not([hidden])')
    def test_both_directions_reveal_only_and_click_deletes_without_dialog(self):
        for direction,side in [(-1,'right'),(1,'left')]:
            with self.subTest(direction=direction):
                self.swipe(direction=direction);self.assertEqual(self.visible_action().get_attribute('data-side'),side);self.assertEqual(self.visible_action().inner_text(),'删除')
                self.assertEqual(self.page.evaluate('records.filter(r=>r.deleted).length'),0 if direction==-1 else 1)
                self.visible_action().click();self.page.wait_for_function('(n)=>records.filter(r=>r.deleted).length===n',arg=1 if direction==-1 else 2)
        self.assertEqual(self.page.locator('#count-in').inner_text(),'32件');self.assertEqual(self.page.locator('#count-out').inner_text(),'31件')
    def test_restore_both_directions_preserves_images_and_identity(self):
        self.page.evaluate('records[0].deleted=true;records[1].deleted=true');self.page.locator('[data-record-view=trash]').click()
        self.assertTrue(self.page.locator('#export').is_hidden())
        for direction in [-1,1]:
            old_id=self.row().get_attribute('data-id');self.swipe(direction=direction);self.assertEqual(self.visible_action().inner_text(),'还原');self.visible_action().click();self.page.wait_for_function('(id)=>!records.find(r=>r.id===id).deleted',arg=old_id)
        self.assertIn('回收站为空',self.page.locator('#record-rows').inner_text());self.page.locator('[data-record-view=records]').click();self.assertEqual(self.row().get_attribute('data-id'),'row-0');self.assertIn('/images/row-0/product',self.row().locator('[data-photo]').first.get_attribute('data-photo'))
    def test_swiping_photo_does_not_open_viewer_and_tap_still_works(self):
        self.swipe(photo=True);self.assertTrue(self.visible_action().is_visible());self.assertFalse(self.page.locator('#photo-dialog').is_visible())
        self.page.wait_for_timeout(450);self.row().locator('[data-photo]').first.click();self.assertFalse(self.page.locator('#photo-dialog').is_visible());self.row().locator('[data-photo]').first.click();self.assertTrue(self.page.locator('#photo-dialog').is_visible())
    def test_vertical_scroll_and_cancel_do_not_reveal_or_delete(self):
        self.swipe(vertical=True);self.assertEqual(self.page.locator('.record-action:not([hidden])').count(),0)
        self.swipe(cancel=True);self.assertEqual(self.page.locator('.record-action:not([hidden])').count(),0);self.assertFalse(self.page.locator('#photo-dialog').is_visible());self.assertEqual(self.page.evaluate('records.filter(r=>r.deleted).length'),0)
    def test_only_one_open_and_outside_tap_closes(self):
        self.swipe();self.swipe(index=1,direction=1);self.assertEqual(self.page.locator('.record-action:not([hidden])').count(),1);self.assertTrue(self.visible_action(1).is_visible());self.page.locator('#record-total').tap();self.assertEqual(self.page.locator('.record-action:not([hidden])').count(),0)
    def test_failed_request_preserves_row_and_can_retry(self):
        self.page.evaluate('qa.fail=true');self.swipe();self.visible_action().click();self.page.wait_for_function('()=>document.querySelector("#toast").textContent.includes("测试删除失败")');self.assertEqual(self.row().get_attribute('data-id'),'row-0');self.assertTrue(self.visible_action().is_enabled());self.page.evaluate('qa.fail=false');self.visible_action().click();self.page.wait_for_function('()=>records[0].deleted')
    def test_pending_request_cannot_repeat(self):
        self.page.evaluate('qa.hold=true');self.swipe();self.visible_action().click();self.assertTrue(self.visible_action().is_disabled());self.page.evaluate('document.querySelector(".record-action:not([hidden])").click()');self.assertEqual(self.page.evaluate('calls.filter(x=>x.action==="deleteRecord").length'),1);self.page.evaluate('qa.finish()');self.page.wait_for_function('()=>records[0].deleted')
    def test_pagination_reload_after_delete_has_no_duplicates(self):
        self.page.locator('#load-more').click();self.assertEqual(self.page.locator('.record-swipe').count(),65);self.swipe(index=64);self.visible_action(64).click();self.page.wait_for_function('()=>records[64].deleted');self.page.locator('#load-more').click();ids=self.page.locator('.record-swipe').evaluate_all('(rows)=>rows.map(r=>r.dataset.id)');self.assertEqual(len(ids),64);self.assertEqual(len(set(ids)),64);self.assertNotIn('row-64',ids)
    def test_trash_search_filter_and_back(self):
        self.page.evaluate('records[0].deleted=true;records[1].deleted=true');self.page.locator('[data-record-view=trash]').click();self.page.locator('[data-filter=out]').click();self.assertEqual(self.row().get_attribute('data-id'),'row-1');self.page.locator('#record-query').fill('missing');self.page.wait_for_timeout(300);self.assertEqual(self.page.locator('.record-swipe').count(),0);self.page.evaluate('onNative("back",{})');self.assertEqual(self.page.locator('[data-record-view=records]').get_attribute('aria-pressed'),'true');self.assertTrue(self.page.locator('#export').is_visible())
    def test_keyboard_reveal_and_escape(self):
        content=self.row().locator('.record-content');content.focus();content.press('ArrowLeft');self.assertTrue(self.visible_action().evaluate('(e)=>e===document.activeElement'));self.visible_action().press('Escape');self.assertEqual(self.page.locator('.record-action:not([hidden])').count(),0)
    def test_four_themes_and_small_screens(self):
        output=Path(__file__).resolve().parents[2]/'build/inventory-record-trash';output.mkdir(parents=True,exist_ok=True)
        for theme in ['dark','light','pure-white','hardcore']:
            self.page.evaluate('(theme)=>document.documentElement.dataset.framekeepTheme=theme',theme)
            for width in [320,390,600]:
                self.page.set_viewport_size({'width':width,'height':844});self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'));self.swipe();box=self.visible_action().bounding_box();self.assertGreaterEqual(box['x'],0);self.assertLessEqual(box['x']+box['width'],width)
                if width==390:self.page.screenshot(path=str(output/f'records-{theme}.png'))
                self.page.locator('#record-total').tap()

if __name__=='__main__':unittest.main()
