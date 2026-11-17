"""手动更正后清单、历史派生记录、统计与工作副本自动同步。"""
import copy
import json
import sqlite3
import unittest
import uuid

from playwright.sync_api import expect,sync_playwright

from metadata_group import confirm_group
from sku_service import SkuService
from sku_workbook import inspect_workbook
import test_sku_input_confirm as input_fixture
import test_ocr_metadata as fixture


class ManagedRowsTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    http=input_fixture.InputConfirmTests.http

    def confirm(self,code,cid=None):
        return confirm_group(self.archive,self.sku,self.sid,cid or self.ids[1],[cid or self.ids[1]],code,'模特图',self.pid)

    def managed(self):
        state=self.sku.state();sheet=state['project']['metadata'].get('managed_sheet')
        return [r for r in state['rows'] if r['sheet']==sheet]

    def orphan(self,code,cid=None,*,legacy=False):
        job={'project':self.pid,'session_id':self.sid,'capture_id':cid or self.ids[1],'scope':'模特图'}
        result=self.sku._record_unplanned(job,[{'codes':[code],'unscored':True}],{'status':'review','row_ids':[]},accept_observed=True)
        rid=result['row_ids'][0]
        if legacy:
            with self.sku.connect() as c:c.execute("UPDATE sku_rows SET source_values=json_remove(source_values,'$._label') WHERE id=?",(rid,))
        return rid

    def test_repeated_manual_corrections_keep_only_current_generated_row(self):
        planned=copy.deepcopy(self.sku.rows(self.pid))
        for code in ('WRONG-100','CORRECT-200','FINAL-300','WRONG-100'):
            self.confirm(code)
            rows=self.managed();self.assertEqual([r['sku'] for r in rows],[code])
            self.assertEqual(rows[0]['status'],'已拍摄');self.assertEqual(rows[0]['excel_row'],2)
            self.assertEqual(rows[0]['source_values']['A'],code)
            self.assertTrue(rows[0]['sku_image']);self.assertEqual(rows[0]['completion']['capture_id'],self.ids[1])
            self.assertEqual([r for r in self.sku.rows(self.pid) if r['sheet']!='临时插拍'],planned)

    def test_existing_legacy_orphans_are_reconciled_on_progress_refresh(self):
        self.confirm('FINAL-300')
        first=self.orphan('WRONG-100',legacy=True);second=self.orphan('WRONG-200',legacy=True)
        unrelated=self.orphan('UNCONFIRMED-900',self.ids[3],legacy=True)
        self.assertEqual({r['sku'] for r in self.managed()},{'FINAL-300','UNCONFIRMED-900'})
        with self.sku.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM sku_rows WHERE id IN (?,?)',(first,second)).fetchone()[0],0)
            self.assertIsNotNone(c.execute('SELECT id FROM sku_rows WHERE id=?',(unrelated,)).fetchone())
        self.assertEqual([r['excel_row'] for r in self.managed()],[2,3])

    def test_old_sku_still_used_by_another_label_is_preserved(self):
        self.confirm('SHARED-100',self.ids[1]);self.confirm('SHARED-100',self.ids[3])
        self.confirm('CORRECT-200',self.ids[1])
        rows=self.managed();self.assertEqual({r['sku'] for r in rows},{'SHARED-100','CORRECT-200'})
        self.assertTrue(all(r['status']=='已拍摄' for r in rows))
        shared=next(r for r in rows if r['sku']=='SHARED-100')
        self.assertEqual(shared['completion']['capture_id'],self.ids[3])

    def test_correcting_to_existing_sku_removes_old_row_without_duplicate(self):
        self.confirm('EXISTING-100',self.ids[3]);existing=self.managed()[0]['id']
        self.confirm('WRONG-200',self.ids[1]);self.confirm('EXISTING-100',self.ids[1])
        rows=self.managed();self.assertEqual([r['sku'] for r in rows],['EXISTING-100'])
        self.assertEqual(rows[0]['id'],existing)
        progress=next(s for s in self.sku.state()['progress'] if s['sheet']=='临时插拍')
        self.assertEqual((progress['total'],progress['shot'],progress['pending']),(1,1,0))

    def test_imported_plan_row_is_preserved_after_manual_correction(self):
        self.confirm(fixture.CODE);self.confirm('CORRECT-200')
        rows=self.sku.state()['rows']
        planned=[r for r in rows if r['sku']==fixture.CODE and r['sheet']=='模特图']
        self.assertTrue(planned);self.assertTrue(all(r['status']=='未拍摄' for r in planned))
        self.assertEqual([r['sku'] for r in self.managed()],['CORRECT-200'])

    def test_explicit_other_origin_is_not_inferred_from_same_filename(self):
        self.confirm('CORRECT-200');rid=self.orphan('OTHER-900')
        with self.sku.connect() as c:
            c.execute("UPDATE sku_rows SET source_values=json_set(source_values,'$._label.session_id','another-session') WHERE id=?",(rid,))
        self.assertIn('OTHER-900',{r['sku'] for r in self.managed()})

    def test_legacy_source_collision_with_unconfirmed_other_folder_is_preserved(self):
        self.confirm('CORRECT-200');self.orphan('AMBIGUOUS-900',legacy=True)
        folder=self.base/'other'/'photos';folder.mkdir(parents=True)
        photo=folder/'1.JPG';fixture.Image.new('RGB',(160,120),'green').save(photo)
        sid=self.store.ensure_live_session(folder)
        record=fixture.media.capture_record(folder,photo,None,{'DateTimeOriginal':'2026:10:21 11:00:00'})
        self.store.append_live(sid,[record])
        with self.sku.connect() as c:
            job=dict(c.execute('SELECT * FROM jobs WHERE project=? LIMIT 1',(self.pid,)).fetchone())
            job.update(id=uuid.uuid4().hex,session_id=sid,capture_id=record['id'],fingerprint=record['fingerprint'],status='review',targets='[]')
            c.execute('INSERT INTO jobs('+','.join(job)+') VALUES ('+','.join('?' for _ in job)+')',list(job.values()))
        self.assertIn('AMBIGUOUS-900',{r['sku'] for r in self.managed()})

    def test_cleanup_failure_rolls_back_choice_rows_and_progress(self):
        self.confirm('WRONG-100');before=self.sku.rows(self.pid)
        with self.sku.connect() as c:c.execute("CREATE TRIGGER reject_row_cleanup BEFORE DELETE ON sku_rows BEGIN SELECT RAISE(ABORT,'injected cleanup failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'injected cleanup failure'):self.confirm('CORRECT-200')
        self.assertEqual(self.sku.rows(self.pid),before)
        self.assertEqual(self.sku.ocr_details(self.sid,self.ids[1])['selected_sku'],'WRONG-100')
        with self.sku.connect() as c:c.execute('DROP TRIGGER reject_row_cleanup')
        self.confirm('CORRECT-200');self.assertEqual([r['sku'] for r in self.managed()],['CORRECT-200'])

    def test_export_and_restart_do_not_restore_obsolete_rows_or_extra_status_cells(self):
        self.confirm('WRONG-100');self.confirm('CORRECT-200')
        output=self.sku.export(self.pid)
        for path in (output,self.sku.root/self.pid/'source.xlsx'):
            sheet=next(s for s in inspect_workbook(path) if s['name']=='临时插拍')
            self.assertEqual([r['sku'] for r in sheet['rows']],['CORRECT-200'])
            self.assertEqual(sheet['rows'][0]['excel_row'],2)
        self.sku.close()
        self.sku=SkuService(self.store,self.providers,autostart=False,archive=self.archive,require_sku_choice=True)
        self.archive.sku=self.sku
        self.assertEqual([r['sku'] for r in self.managed()],['CORRECT-200'])

    def test_browser_open_progress_updates_without_reopening_page(self):
        self.confirm('WRONG-100')
        self.sku.configure(self.pid,self.sid,'临时插拍')
        with self.http() as url,sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1500,'height':1000});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                page.locator('#sku-open').click()
                expect(page.locator('#sku-rows')).to_contain_text('WRONG-100')
                self.confirm('CORRECT-200')
                expect(page.locator('#sku-rows')).to_contain_text('CORRECT-200',timeout=10000)
                expect(page.locator('#sku-rows')).not_to_contain_text('WRONG-100')
                expect(page.locator('#progress-total')).to_have_text('1')
                expect(page.locator('#progress-shot')).to_have_text('1')
                expect(page.locator('#progress-pending')).to_have_text('0')
                out=fixture.ROOT/'build/sku-progress-correction';out.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(out/'updated-progress.png'))
                self.assertFalse(errors,errors)
            finally:browser.close()


if __name__=='__main__':unittest.main()
