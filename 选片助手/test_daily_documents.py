"""客户旧版文档、并发建档、分页款号及跨日期总览的升级回归。"""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import shutil
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
import media_engine as media
from sku_service import SkuService
from sku_library import labels
from test_shooting_documents import ShootingDocumentsTests


class DailyDocumentsTests(unittest.TestCase):
    setUp=ShootingDocumentsTests.setUp
    tearDown=ShootingDocumentsTests.tearDown
    prepare=ShootingDocumentsTests.prepare
    rate=ShootingDocumentsTests.rate
    choose=ShootingDocumentsTests.choose
    drain=ShootingDocumentsTests.drain

    def legacy_duplicate(self):
        original=self.sku.project();pid='legacy-second-document'
        folder=self.sku.root/pid;folder.mkdir();shutil.copy2(self.sku.root/original['id']/'source.xlsx',folder/'source.xlsx')
        with self.sku.connect() as c:
            c.execute('INSERT INTO projects(id,name,metadata,session_id,scope,enabled,created) VALUES (?,?,?,?,?,?,?)',
                      (pid,original['name'],json.dumps(original['metadata'],ensure_ascii=False),self.sid,'现场记录',1,self.clock[0]+1))
            c.execute('INSERT INTO shooting_captures VALUES (?,?,?,?,?)',(self.sid,self.records[2]['id'],pid,'现场记录',self.clock[0]))
        self.sku._bind_document(self.sid,pid,'现场记录')
        return pid

    def test_legacy_same_day_documents_read_as_one_without_rewriting_evidence(self):
        p=self.prepare();self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'FIRST-100');self.drain()
        duplicate=self.legacy_duplicate();self.rate(self.records[2]['id']);self.choose(self.records[2]['id'],'SECOND-200');self.drain()
        before={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in self.photos.glob('*.JPG')}
        with self.sku.connect() as c:jobs=[tuple(r) for r in c.execute('SELECT id,project,targets FROM jobs ORDER BY id')]
        state=self.sku.activate(duplicate)
        self.assertEqual(len(state['projects']),1);self.assertEqual(state['project']['id'],p['id'])
        self.assertEqual({r['sku'] for r in state['rows']},{'FIRST-100','SECOND-200'})
        self.assertEqual(state['progress'][0]['shot'],2)
        self.assertTrue(all(r['sku_image'] and r['label_image'] for r in state['rows']))
        from sku_workbook import inspect_workbook
        book=inspect_workbook(self.sku.export(p['id']))
        self.assertEqual({r['sku'] for s in book if s['name']=='现场记录' for r in s['rows']},{'FIRST-100','SECOND-200'})
        with self.sku.connect() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT id,project,targets FROM jobs ORDER BY id')],jobs)
            self.assertEqual(c.execute('SELECT count(*) FROM projects').fetchone()[0],2)
        self.assertEqual({path:hashlib.sha256(path.read_bytes()).hexdigest() for path in before},before)
        reopened=SkuService(self.store,self.providers,archive=self.archive,clock=lambda:self.clock[0],autostart=False)
        try:self.assertEqual(reopened.state()['progress'][0]['shot'],2)
        finally:reopened.close()
        self.sku.manage_document(p['id'],'remove');self.assertEqual(self.sku.state()['projects'],[])
        self.sku.manage_document(p['id'],'restore');self.assertEqual(len(self.sku.state()['projects']),1)
        self.assertEqual(self.sku.activate(p['id'])['progress'][0]['shot'],2)

    def test_two_engines_atomically_share_day_document_across_folders(self):
        self.clock[0]=1793754000
        other=self.base/'other';other.mkdir();sid=self.store.ensure_live_session(other)
        reopened=SkuService(self.store,self.providers,clock=lambda:self.clock[0],autostart=False)
        barrier=threading.Barrier(2)
        from sku_workbook_auto import create_book
        def slow_book():time.sleep(.08);return create_book()
        def create(service,session):barrier.wait(timeout=5);return service._automatic_project(session,start_stream=False)['id']
        try:
            with patch('sku_workbook_auto.create_book',slow_book),ThreadPoolExecutor(2) as pool:
                first=pool.submit(create,self.sku,self.sid);second=pool.submit(create,reopened,sid)
                self.assertEqual(first.result(timeout=10),second.result(timeout=10))
            with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM projects').fetchone()[0],1)
            # 第二个目录成为同日绑定后，第一个目录的二星入口仍可正常排队。
            self.rate(self.store.records(self.sid)[0]['id'])
        finally:reopened.close()

    def test_overview_includes_all_documents_and_types_by_day_without_rebinding(self):
        p=self.prepare();self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'YESTERDAY-100');self.drain()
        self.clock[0]+=86400;self.sku.live_arrivals(self.sid,self.records[2:]);self.rate(self.records[2]['id']);self.choose(self.records[2]['id'],'TODAY-200');self.drain()
        from test_sku_workflow import fixture_book
        self.sku.import_file('2026-11-05.xlsx',fixture_book())
        context=self.sku.shooting_context(self.sid);today=self.sku.document_overview()
        self.assertEqual(len(today['documents']),2)
        self.assertIn('TODAY-200',{r['sku'] for r in today['rows']});self.assertNotIn('YESTERDAY-100',{r['sku'] for r in today['rows']})
        history=self.sku.document_overview('history')
        self.assertEqual({r['document_day'] for r in history['rows']},{'2026-11-04','2026-11-05'})
        self.assertIn('YESTERDAY-100',{r['sku'] for r in history['rows']})
        self.assertEqual(self.sku.shooting_context(self.sid),context)
        with self.assertRaises(ValueError):self.sku.document_overview('invalid')

    def test_readonly_sku_index_uses_shared_rating_to_restore_interval(self):
        self.prepare();self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'INDEX-100');self.drain()
        record=self.records[1];self.archive.ratings.set(self.archive.ratings.paths(self.sid,record),2)
        session=self.store.session(self.sid);updated=copy.deepcopy(session['state']);updated['overrides'].pop(record['id'])
        self.store.save(self.sid,session['revision'],updated)
        from sku_library_index import SkuLibraryIndex
        index=SkuLibraryIndex(self.sku)
        try:
            index.refresh();data=index.browse(code='INDEX-100',fresh=True)
            self.assertEqual(data['total'],2);self.assertEqual(index.error,'')
        finally:index.close()

    def test_paged_labels_recover_legacy_interval_with_persisted_shared_rating(self):
        p=self.prepare();self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'LEGACY-100');self.drain()
        record=self.records[1];self.archive.ratings.set(self.archive.ratings.paths(self.sid,record),2)
        session=self.store.session(self.sid);updated=copy.deepcopy(session['state']);updated['overrides'].pop(record['id'])
        self.store.save(self.sid,session['revision'],updated)
        with self.archive.connect() as c:c.execute('DELETE FROM catalog_links')
        expected=dict.fromkeys([r['id'] for r in self.records[:2]],'LEGACY-100')
        self.assertEqual(labels(self.sku,self.sid,[r['id'] for r in self.records])['labels'],expected)
        self.assertEqual(labels(self.sku,self.sid,[self.records[0]['id']])['labels'],{self.records[0]['id']:'LEGACY-100'})
        self.assertEqual(self.sku.state()['progress'][0]['shot'],1)
        self.archive.ratings.set(self.archive.ratings.paths(self.sid,record),0)
        self.assertEqual(self.sku.state()['progress'][0]['shot'],0)

    def test_browser_overview_range_dates_sources_and_default_today(self):
        from test_sku_input_confirm import InputConfirmTests
        from playwright.sync_api import sync_playwright,expect
        self.prepare();self.rate(self.records[1]['id']);self.choose(self.records[1]['id'],'DAY-ONE');self.drain()
        self.clock[0]+=86400;self.sku.live_arrivals(self.sid,self.records[2:]);self.rate(self.records[2]['id']);self.choose(self.records[2]['id'],'DAY-TWO');self.drain()
        binding=self.sku.shooting_context(self.sid)
        out=Path(__file__).resolve().parents[1]/'build/daily-documents';out.mkdir(parents=True,exist_ok=True)
        with InputConfirmTests.http(self) as url,sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading');page.locator('#sku-open').click()
                page.locator('#sku-all-tab').click();expect(page.locator('#sku-display-range')).to_be_visible()
                expect(page.locator('#sku-display-range')).to_have_value('today');expect(page.locator('#sku-projects')).not_to_be_visible()
                expect(page.locator('.sku-day-divider')).to_have_count(1);expect(page.locator('#sku-rows')).to_contain_text('DAY-TWO')
                expect(page.locator('#sku-rows')).not_to_contain_text('DAY-ONE')
                page.locator('#sku-display-range').select_option('history');expect(page.locator('.sku-day-divider')).to_have_count(2)
                expect(page.locator('#sku-rows')).to_contain_text('DAY-ONE');expect(page.locator('#progress-shot')).to_have_text('2')
                expect(page.locator('#sku-rows [data-source-project]')).to_have_count(2)
                page.wait_for_function('()=>[...document.querySelectorAll("#sku-rows img")].every(im=>im.complete&&im.naturalWidth>0)')
                page.screenshot(path=str(out/'history-overview.png'))
                page.locator('#sku-search').fill('DAY-ONE');expect(page.locator('#sku-rows [data-source-project]')).to_have_count(1)
                page.locator('#sku-search').fill('');page.locator('#sku-filter').select_option('pending');expect(page.locator('#sku-rows [data-source-project]')).to_have_count(0)
                page.locator('#sku-filter').select_option('all')
                for width in (1024,600):
                    page.set_viewport_size({'width':width,'height':1000});self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth+1'))
                page.screenshot(path=str(out/'history-narrow.png'))
                self.assertEqual(self.sku.shooting_context(self.sid),binding)
                page.locator('[data-overview-document]').first.click();expect(page.locator('#sku-display-range')).not_to_be_visible()
                toggle=page.locator('#progress-sidebar-toggle')
                if toggle.get_attribute('aria-expanded')=='false':toggle.click()
                expect(page.locator('#sku-projects')).to_be_visible()
                self.assertFalse(errors,errors)
            finally:browser.close()


if __name__=='__main__':unittest.main()
