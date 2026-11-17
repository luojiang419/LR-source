"""记录页款号编辑的交互回归，使用现有页面和原生桥模拟。"""
import unittest
from pathlib import Path
import test_background_ocr as background


class RecordEditTests(unittest.TestCase):
    setUpClass = background.BackgroundOcrTests.__dict__['setUpClass']
    tearDownClass = background.BackgroundOcrTests.__dict__['tearDownClass']
    tearDown = background.BackgroundOcrTests.tearDown
    route = background.BackgroundOcrTests.route
    row = background.BackgroundOcrTests.row

    def setUp(self):
        background.BackgroundOcrTests.setUp(self)
        self.page.evaluate('''()=>{
          records.find(r=>r.id==='ready').synced=1;
          const original=Native.call;
          Native.call=(action,body,id)=>{
            if(action!=='updateRecordSku')return original(action,body,id);
            const b=JSON.parse(body);calls.push({action,body:b});
            qa.finish=()=>{const r=records.find(r=>r.id===b.id);const error=qa.fail?'保存失败，请重试':null;
              if(!error)Object.assign(r,{sku:b.sku,ocr_status:'ready',ocr_revision:r.ocr_revision+1,synced:0});
              queueMicrotask(()=>onNative('reply',{id,data:r,error}));};
            if(!qa.hold)qa.finish();
          };
        }''')

    def edit(self, id='ready'):
        self.row(id).locator('[data-record-edit]').click()
        self.page.wait_for_selector('#record-review-dialog[open]')

    def test_ready_edit_prefills_saves_original_record_and_reopens(self):
        self.edit()
        self.assertEqual(self.page.locator('#record-review-title').inner_text(), '编辑款号')
        self.assertEqual(self.page.locator('#record-sku').input_value(), 'LC9999-P30')
        self.assertTrue(self.page.locator('#record-retry').is_hidden())
        self.page.locator('#record-sku').fill(' lc9999-p31 ')
        self.page.locator('#record-confirm').click()
        self.page.wait_for_selector('#record-review-dialog', state='hidden')
        self.assertEqual(self.row('ready').locator('strong').inner_text(), 'LC9999-P31')
        self.assertEqual(self.page.locator('.record-swipe').count(), 3)
        self.assertEqual(self.page.evaluate('calls.find(c=>c.action==="updateRecordSku").body'),
                         {'id': 'ready', 'revision': 0, 'sku': 'LC9999-P31'})
        self.edit()
        self.assertEqual(self.page.locator('#record-sku').input_value(), 'LC9999-P31')

    def test_cancel_does_not_write_and_keyboard_opens_editor(self):
        button = self.row('ready').locator('[data-record-edit]')
        button.focus(); button.press('Enter')
        self.page.wait_for_selector('#record-review-dialog[open]')
        self.page.locator('#record-sku').fill('LC-CANCEL')
        self.page.locator('#record-review-close').click()
        self.page.wait_for_selector('#record-review-dialog', state='hidden')
        self.assertEqual(self.row('ready').locator('strong').inner_text(), 'LC9999-P30')
        self.assertEqual(self.page.evaluate('calls.filter(c=>c.action==="updateRecordSku").length'), 0)

    def test_empty_or_invalid_sku_never_calls_native_save(self):
        self.edit()
        for sku in ['', '   ', '款号123', '=FORMULA', 'LC 123']:
            self.page.locator('#record-sku').fill(sku)
            self.page.locator('#record-confirm').click()
            self.assertTrue(self.page.locator('#record-review-dialog').is_visible())
            self.assertIn('款号需为', self.page.locator('#toast').inner_text())
        self.assertEqual(self.page.evaluate('calls.filter(c=>c.action==="updateRecordSku").length'), 0)

    def test_save_failure_retains_input_and_allows_retry(self):
        self.edit(); self.page.evaluate('qa.fail=true')
        self.page.locator('#record-sku').fill('LC-RETRY')
        self.page.locator('#record-confirm').click()
        self.page.wait_for_function('()=>!document.querySelector("#record-confirm").disabled')
        self.assertEqual(self.page.locator('#record-sku').input_value(), 'LC-RETRY')
        self.assertTrue(self.page.locator('#record-review-dialog').is_visible())
        self.page.evaluate('qa.fail=false')
        self.page.locator('#record-confirm').click()
        self.page.wait_for_selector('#record-review-dialog', state='hidden')
        self.assertEqual(self.row('ready').locator('strong').inner_text(), 'LC-RETRY')

    def test_pending_can_be_manually_completed_and_save_cannot_repeat(self):
        self.edit('pending'); self.page.evaluate('qa.hold=true')
        self.page.locator('#record-sku').fill('LC-MANUAL')
        self.page.locator('#record-confirm').click()
        self.assertTrue(self.page.locator('#record-sku').is_disabled())
        self.page.evaluate('document.querySelector("#record-confirm").click();onNative("back",{})')
        self.assertTrue(self.page.locator('#record-review-dialog').is_visible())
        self.assertEqual(self.page.evaluate('calls.filter(c=>c.action==="updateRecordSku").length'), 1)
        self.page.evaluate('qa.finish()')
        self.page.wait_for_selector('#record-review-dialog', state='hidden')
        self.assertEqual(self.row('pending').locator('strong').inner_text(), 'LC-MANUAL')

    def test_photo_preview_and_back_keep_unsaved_edit(self):
        self.edit(); self.page.locator('#record-sku').fill('LC-KEEP')
        self.page.locator('#record-label-photo').click()
        self.assertTrue(self.page.locator('#photo-dialog').is_visible())
        self.page.evaluate('onNative("back",{})')
        self.page.wait_for_selector('#photo-dialog', state='hidden')
        self.assertEqual(self.page.locator('#record-sku').input_value(), 'LC-KEEP')

    def test_small_screen_and_themes_keep_edit_controls_visible(self):
        self.edit()
        output = Path(__file__).resolve().parents[2] / 'build/inventory-record-edit'
        output.mkdir(parents=True, exist_ok=True)
        for theme in ['dark', 'light', 'pure-white', 'hardcore']:
            self.page.evaluate('(v)=>document.documentElement.dataset.framekeepTheme=v', theme)
            for width in [320, 390, 600]:
                self.page.set_viewport_size({'width': width, 'height': 844})
                self.assertFalse(self.page.locator('#record-review-dialog').evaluate('(d)=>d.scrollWidth>d.clientWidth'))
                self.assertTrue(self.page.locator('#record-confirm').is_visible())
                if width == 390: self.page.screenshot(path=str(output / f'edit-{theme}.png'))


if __name__ == '__main__': unittest.main()
