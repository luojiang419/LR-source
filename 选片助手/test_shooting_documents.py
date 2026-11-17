"""自动文档、类型隔离、历史移除和真实元数据改号回归。"""
import copy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import unittest

from PIL import Image
from sku_service import SkuService
from sku_workbook import inspect_workbook
from test_ocr_without_document import NoDocumentTests
import media_engine as media


class ShootingDocumentsTests(unittest.TestCase):
    setUp=NoDocumentTests.setUp
    tearDown=NoDocumentTests.tearDown

    def prepare(self):
        self.clock[0]=datetime(2026,11,4,1,tzinfo=timezone.utc).timestamp()
        self.sku.configure_tasks(mode='manual')
        self.records=self.store.records(self.sid)
        self.sku.live_arrivals(self.sid,self.records[:2]);return self.sku.project()

    def rate(self,cid):
        session=self.store.session(self.sid);before=copy.deepcopy(session['state']);after=copy.deepcopy(before);after.setdefault('overrides',{})[cid]=2
        self.store.save(self.sid,session['revision'],after);self.sku.on_save(self.sid,before,after)

    def choose(self,cid,code):
        details=self.sku.ocr_details(self.sid,cid)
        return self.sku.choose_ocr_sku(self.sid,cid,code,details['reading_key'],details['image_key'],allow_manual=True,literal=True)

    def drain(self):
        self.sku.deliver_events(self.archive)
        for _ in range(100):
            with self.archive.connect() as c:pending=c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued' LIMIT 1").fetchone()
            if not pending:break
            self.archive.process_one()
        with self.archive.connect() as c:self.assertEqual([dict(r) for r in c.execute("SELECT path,reason FROM metadata_jobs WHERE status='failed'")],[])

    def test_first_arrival_manual_mode_no_model_duplicate_restart_and_next_day(self):
        self.assertIsNone(self.sku.project());p=self.prepare();self.assertEqual(p['name'],'2026-11-04.xlsx');self.assertEqual(self.calls,[])
        self.sku.live_arrivals(self.sid,self.records[:2]);self.assertEqual(len(self.sku.state()['projects']),1)
        reopened=SkuService(self.store,self.providers,archive=self.archive,clock=lambda:self.clock[0],autostart=False)
        try:
            reopened.live_arrivals(self.sid,self.records[:2]);self.assertEqual(reopened.project()['id'],p['id'])
            self.clock[0]+=86400;reopened.live_arrivals(self.sid,self.records[2:]);self.assertEqual(reopened.project()['name'],'2026-11-05.xlsx')
            self.assertEqual(reopened.shooting_context(self.sid,self.records[0]['id'])['project'],p['id'])
        finally:reopened.close()

    def test_two_star_live_ignores_legacy_manual_and_keeps_pause(self):
        self.prepare();self.sku.configure_tasks(paused=True);self.rate(self.records[1]['id']);self.sku.tick()
        with self.sku.connect() as c:job=dict(c.execute('SELECT * FROM jobs').fetchone())
        self.assertEqual(job['trigger'],'live');self.assertEqual(job['status'],'queued');self.assertEqual(self.calls,[])
        self.sku.configure_tasks(paused=False);self.clock[0]+=3;self.sku.tick();self.assertEqual(len(self.calls),1)

    def test_create_type_blocks_unclosed_group_then_freezes_jobs_and_exports_both_sheets(self):
        p=self.prepare()
        with self.assertRaisesRegex(ValueError,'未收尾'):self.sku.create_shooting_type(p['id'],'细节图',self.sid)
        self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'FIRST-100');self.drain()
        self.sku.create_shooting_type(p['id'],'细节图',self.sid)
        self.sku.live_arrivals(self.sid,self.records[2:]);self.rate(self.records[2]['id']);self.choose(self.records[2]['id'],'SECOND-200');self.drain()
        rows=self.sku.state()['rows'];self.assertEqual({(r['sku'],r['sheet']) for r in rows},{('FIRST-100','现场记录'),('SECOND-200','细节图')})
        self.assertEqual({r['status'] for r in rows},{'已拍摄'})
        with self.sku.connect() as c:self.assertEqual(c.execute('SELECT scope FROM jobs WHERE capture_id=?',(self.records[1]['id'],)).fetchone()[0],'现场记录')
        book=inspect_workbook(self.sku.export(p['id']),allow_empty=True);self.assertTrue({'现场记录','细节图'}<={s['name'] for s in book})
        self.assertEqual(next(s for s in book if s['name']=='细节图')['rows'][0]['sku'],'SECOND-200')

    def test_type_validation_and_case_insensitive_reuse(self):
        p=self.prepare();self.rate(self.records[1]['id'])
        for name in ('','a/b','x'*32,"'bad",'bad]'):
            with self.assertRaises(ValueError):self.sku.create_shooting_type(p['id'],name,self.sid)
        self.sku.create_shooting_type(p['id'],' Detail ',self.sid);self.sku.create_shooting_type(p['id'],'detail',self.sid)
        self.assertEqual([s['name'] for s in self.sku.project()['metadata']['sheets']],['现场记录','Detail'])

    def test_same_base_sku_two_types_and_versioned_row_rename_keep_other_type(self):
        p=self.prepare();self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'SAME-100');self.drain()
        self.sku.create_shooting_type(p['id'],'细节图',self.sid);self.sku.live_arrivals(self.sid,self.records[2:]);self.rate(self.records[2]['id'])
        details=self.sku.ocr_details(self.sid,self.records[2]['id'])
        self.sku.choose_ocr_sku(self.sid,self.records[2]['id'],'SAME-100',details['reading_key'],details['image_key'],allow_manual=True)
        self.drain();rows=self.sku.state()['rows'];self.assertEqual(len(rows),2)
        self.assertEqual({(r['sku'],r['sheet']) for r in rows},{('SAME-100','现场记录'),('SAME-100-A','细节图')})
        row=next(r for r in rows if r['sheet']=='细节图');self.assertEqual(row['shoot_sku'],'SAME-100-A')
        self.sku.rename_shooting_row(p['id'],row['id'],row['sku'],'DETAIL-200');self.drain()
        self.assertEqual(self.archive.writer.read(self.photos/self.records[1]['jpg'])['XMP-fk:SKU'],'SAME-100')
        self.assertEqual(self.archive.writer.read(self.photos/self.records[2]['jpg'])['XMP-fk:SKU'],'DETAIL-200')
        self.assertEqual({(r['sku'],r['sheet']) for r in self.sku.state()['rows']},{('SAME-100','现场记录'),('DETAIL-200','细节图')})

    def test_history_view_does_not_rebind_and_remove_does_not_resurrect(self):
        p=self.prepare();self.rate(self.records[1]['id']);old=p['id']
        self.clock[0]+=86400;self.sku.live_arrivals(self.sid,self.records[2:]);today=self.sku.project()['id'];self.sku.activate(old)
        self.assertEqual(self.sku.shooting_context(self.sid)['project'],today)
        self.assertFalse(next(r for r in self.sku.document_inventory() if r['id']==old)['today'])
        self.sku.manage_document(today,'remove');self.assertIsNone(self.sku.shooting_context(self.sid))
        image=self.photos/'new.JPG';Image.new('RGB',(100,80),'red').save(image);record=media.capture_record(self.photos,image,None,{})
        self.sku.live_arrivals(self.sid,[record]);self.assertNotEqual(self.sku.project()['id'],today);self.assertIn('（2）',self.sku.project()['name'])
        self.sku.manage_document(today,'restore');self.assertFalse(self.sku.project(today)['metadata'].get('removed_at'))
        self.assertEqual(self.sku.shooting_context(self.sid,self.records[2]['id'])['project'],today)

    def test_unbound_imported_history_never_becomes_new_capture_document(self):
        from test_sku_workflow import fixture_book
        self.clock[0]=datetime(2026,11,3,1,tzinfo=timezone.utc).timestamp()
        history=self.sku.import_file('old-plan.xlsx',fixture_book(),self.sid,preferred_scope='模特图')['project']
        with self.sku.connect() as c:c.execute('DELETE FROM shooting_bindings')
        self.clock[0]+=86400;self.sku.activate(history['id']);self.sku.live_arrivals(self.sid,self.store.records(self.sid)[:1])
        self.assertEqual(self.sku.project()['name'],'2026-11-04.xlsx')
        self.assertNotEqual(self.sku.shooting_context(self.sid)['project'],history['id'])

    def test_first_schema_upgrade_preserves_legacy_bound_import(self):
        from test_sku_workflow import fixture_book
        p=self.sku.import_file('bound-plan.xlsx',fixture_book(),self.sid,preferred_scope='模特图')['project']
        with self.sku.connect() as c:c.execute('DROP TABLE shooting_bindings')
        reopened=SkuService(self.store,self.providers,archive=self.archive,clock=lambda:self.clock[0],autostart=False)
        try:
            self.assertEqual(reopened.shooting_context(self.sid)['project'],p['id'])
            self.clock[0]+=86400;reopened.live_arrivals(self.sid,self.store.records(self.sid)[:1]);self.assertEqual(reopened.shooting_context(self.sid)['project'],p['id'])
        finally:reopened.close()

    def test_imported_binding_wins_and_custom_type_preserves_source_file(self):
        from test_sku_workflow import fixture_book
        self.clock[0]=datetime(2026,11,4,1,tzinfo=timezone.utc).timestamp()
        original=fixture_book();source=self.base/'customer.xlsx';source.write_bytes(original)
        p=self.sku.import_file(source.name,original,self.sid,preferred_scope='模特图')['project']
        self.sku.live_arrivals(self.sid,self.store.records(self.sid)[:1]);self.assertEqual(self.sku.shooting_context(self.sid)['project'],p['id'])
        self.rate(self.store.records(self.sid)[0]['id']);self.sku.create_shooting_type(p['id'],'补拍',self.sid)
        self.assertEqual(source.read_bytes(),original);self.assertIn('补拍',[s['name'] for s in inspect_workbook(self.sku.export(p['id']),allow_empty=True)])

    def test_rename_row_updates_actual_jpg_and_preserves_pixels_and_neighbor(self):
        p=self.prepare();self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'OLD-100');self.drain()
        paths=[self.photos/str(r['jpg']) for r in self.records]
        def pixels(path):
            with Image.open(path) as image:return hashlib.sha256(image.tobytes()).hexdigest()
        before=[pixels(path) for path in paths];neighbor=hashlib.sha256(paths[2].read_bytes()).hexdigest()
        row=next(r for r in self.sku.state()['rows'] if r['sku']=='OLD-100')
        result=self.sku.rename_shooting_row(p['id'],row['id'],'OLD-100','NEW-200');self.assertTrue(result['operation_ids']);self.drain()
        for path in paths[:2]:self.assertEqual(self.archive.writer.read(path)['XMP-fk:SKU'],'NEW-200')
        self.assertEqual(before,[pixels(path) for path in paths]);self.assertEqual(neighbor,hashlib.sha256(paths[2].read_bytes()).hexdigest())
        self.assertEqual([r['sku'] for r in self.sku.state()['rows']],['NEW-200'])
        self.assertEqual(next(s for s in inspect_workbook(self.sku.export(p['id'])) if s['name']=='现场记录')['rows'][0]['sku'],'NEW-200')

    def test_monitor_hook_creates_document_without_ocr_and_does_not_reassign_historical_photos(self):
        from folder_watch import FolderWatch
        self.clock[0]=datetime(2026,11,4,1,tzinfo=timezone.utc).timestamp()
        watch=FolderWatch(self.store,autostart=False);watch.on_arrivals=self.sku.live_arrivals
        try:
            watch.start(str(self.photos));watch.tick();self.assertIsNone(self.sku.project())
            path=self.photos/'fresh.JPG';Image.new('RGB',(120,80),'orange').save(path);watch.tick();self.clock[0]+=1;watch.tick()
            self.assertIsNotNone(self.sku.project());self.assertEqual(self.calls,[])
            with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM shooting_captures').fetchone()[0],1)
            with self.archive.connect() as c:
                captures={r[0] for r in c.execute('SELECT capture FROM bindings')}
            self.assertFalse(captures & {r['id'] for r in self.store.records(self.sid) if r.get('jpg')!='fresh.JPG'})
        finally:watch.close()

    def test_browser_type_creation_history_remove_restore_and_inline_rename(self):
        from test_sku_input_confirm import InputConfirmTests
        from playwright.sync_api import sync_playwright,expect
        import threading
        p=self.prepare();self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'BROWSER-100');self.drain()
        self.clock[0]+=86400;self.sku.live_arrivals(self.sid,self.records[2:]);self.rate(self.records[2]['id']);self.choose(self.records[2]['id'],'BROWSER-200');self.drain()
        today=self.sku.project()['id'];stop=threading.Event()
        def writer():
            while not stop.wait(.05):self.archive.process_one()
        thread=threading.Thread(target=writer,daemon=True);thread.start()
        out=Path(__file__).resolve().parents[1]/'build/shooting-documents';out.mkdir(parents=True,exist_ok=True)
        try:
            with InputConfirmTests.http(self) as url,sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading');page.locator('#sku-open').click()
                    expect(page.locator('#sku-projects option')).to_have_count(3)
                    expect(page.locator('#sku-rows .sku-type-cell')).to_have_text(['现场记录'])
                    page.locator('#sku-panel-scope').fill('细节图');expect(page.locator('#sku-type-action')).to_have_text('创建类型');page.locator('#sku-type-action').click()
                    page.wait_for_function('()=>document.getElementById("sku-panel-scope").value==="细节图" && document.getElementById("sku-type-action").textContent.includes("已用于")')
                    self.assertEqual(self.sku.shooting_context(self.sid)['scope'],'细节图')
                    page.locator('[data-progress-sheet="现场记录"]').click();self.assertEqual(self.sku.shooting_context(self.sid)['scope'],'细节图')
                    page.locator('[data-edit-sku]').first.dblclick();page.locator('.sku-code-input').fill('BROWSER-300');page.locator('.sku-code-input').press('Enter')
                    expect(page.locator('[data-edit-sku]').first).to_have_text('BROWSER-300')
                    expect(page.locator('#sku-row-write-dialog')).to_have_count(0)
                    expect(page.locator('#sku-rows .sku-metadata-status')).to_have_count(0,timeout=30000)
                    page.wait_for_timeout(350);page.screenshot(path=str(out/'shooting-list.png'))
                    page.locator('#sku-projects').select_option('__history__');expect(page.locator('#sku-history-dialog')).to_be_visible()
                    expect(page.locator('#sku-history-rows [data-history-project]')).to_have_count(1)
                    page.wait_for_timeout(350);page.screenshot(path=str(out/'history.png'));page.locator('[data-history-project]').first.dblclick()
                    expect(page.locator('#sku-history-hint')).to_contain_text('正在查看历史');self.assertEqual(self.sku.shooting_context(self.sid)['project'],today)
                    page.locator('#sku-remove-document').click();page.locator('#sku-remove-accept').click();page.wait_for_function('()=>!document.getElementById("sku-remove-confirm").open')
                    page.locator('#sku-projects').select_option('__history__');page.locator('#sku-history-removed').check();expect(page.locator('[data-restore-document]')).to_have_count(1);page.locator('[data-restore-document]').click();expect(page.locator('[data-restore-document]')).to_have_count(0)
                    page.locator('#sku-history-close').click();page.locator('#sku-projects').select_option(today)
                    page.locator('#sku-panel-scope').fill('新类型');expect(page.locator('#sku-type-action')).to_have_text('创建类型');page.wait_for_timeout(1200);expect(page.locator('#sku-panel-scope')).to_have_value('新类型')
                    for width in (1680,1024,600):
                        page.set_viewport_size({'width':width,'height':1050});self.assertTrue(page.evaluate('()=>document.documentElement.scrollWidth<=innerWidth+1'))
                    self.assertFalse(errors,errors)
                    (out/'browser.json').write_text(json.dumps({'passed':True,'page_errors':errors,'widths':[1680,1024,600]},ensure_ascii=False),encoding='utf8')
                finally:browser.close()
        finally:stop.set();thread.join(timeout=3)


if __name__=='__main__':unittest.main()
