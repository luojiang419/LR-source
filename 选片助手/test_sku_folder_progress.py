"""同一目录跨清单的有效拍摄证据恢复，保持工作表范围和原始任务。"""
import copy
import io
import json
import re
from zipfile import ZipFile,ZIP_DEFLATED
import unittest
from unittest.mock import patch

import test_ocr_metadata as fixture
from test_sku_workflow import fixture_book,CODE


class FolderProgressTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose

    def new_plan(self,content=None):
        old=self.pid
        with self.sku.connect() as c:c.execute("UPDATE projects SET metadata=json_set(metadata,'$.removed_at','test-removed'),enabled=0 WHERE id=?",(old,))
        state=self.sku.import_file('重新导入.xlsx',content or fixture_book(),self.sid,'模特图')
        self.pid=state['project']['id'];self.assertNotEqual(self.pid,old)
        return old,state

    def test_reimport_projects_restores_known_sheet_and_original_evidence_without_moving_job(self):
        self.parse();self.choose();before=self.sku.state()['rows'][0]
        old,state=self.new_plan()
        shot=[r for r in state['rows'] if r['sku']==CODE and r['status']=='已拍摄']
        self.assertEqual([r['sheet'] for r in shot],['模特图'])
        self.assertEqual(shot[0]['completion']['session_id'],self.sid)
        self.assertEqual(shot[0]['completion']['capture_id'],self.ids[1])
        with self.sku.connect() as c:
            self.assertEqual(c.execute('SELECT project FROM jobs WHERE id=?',(shot[0]['completion_job'],)).fetchone()[0],old)
        self.assertEqual(state['folder_progress']['confirmed_labels'],1)
        self.assertEqual(state['folder_progress']['two_star'],2)
        self.assertEqual(state['folder_progress']['pending_labels'],1)
        self.assertTrue(self.sku.project(old)['metadata']['removed_at'])
        count=len(state['rows']);self.assertEqual(len(self.sku.state()['rows']),count)

    def test_repeated_versioned_shoots_create_one_temporary_base_row(self):
        self.parse();self.choose('OTHER-100')
        self.sku.parse_capture(self.sid,self.ids[3]);self.choose('OTHER-100',self.ids[3])
        _,state=self.new_plan()
        temp=[r for r in state['rows'] if r['sheet']=='临时插拍']
        self.assertEqual(len(temp),1);self.assertEqual(temp[0]['sku'],'OTHER-100')
        self.assertEqual(temp[0]['status'],'已拍摄')
        self.assertEqual(state['folder_progress']['confirmed_labels'],2)
        self.assertEqual(state['folder_progress']['confirmed_skus'],1)
        self.assertEqual(len(self.sku.state()['rows']),len(state['rows']))

    def test_unknown_type_does_not_mark_two_matching_sheets_complete(self):
        self.parse();self.choose()
        with self.sku.connect() as c:c.execute("UPDATE jobs SET scope='现场记录' WHERE session_id=? AND capture_id=?",(self.sid,self.ids[1]))
        _,state=self.new_plan()
        selected=[r for r in state['rows'] if r['sku']==CODE]
        self.assertEqual({r['sheet']:r['status'] for r in selected},{'模特图':'未拍摄','细节图':'未拍摄','临时插拍':'已拍摄'})

    def test_unknown_type_with_only_one_exact_sheet_match_recovers_that_sheet(self):
        self.parse();self.choose()
        with self.sku.connect() as c:c.execute("UPDATE jobs SET scope='现场记录' WHERE session_id=? AND capture_id=?",(self.sid,self.ids[1]))
        source=io.BytesIO(fixture_book());target=io.BytesIO()
        with ZipFile(source) as src,ZipFile(target,'w',ZIP_DEFLATED) as dst:
            for part in src.infolist():
                content=src.read(part.filename)
                if part.filename=='xl/worksheets/sheet2.xml':content=content.replace(CODE.encode(),b'DIFFERENT-200')
                dst.writestr(part,content)
        _,state=self.new_plan(target.getvalue())
        self.assertEqual([r['sheet'] for r in state['rows'] if r['sku']==CODE and r['status']=='已拍摄'],['模特图'])

    def test_clearing_two_star_removes_derived_progress(self):
        self.parse();self.choose();_,state=self.new_plan()
        self.assertEqual(sum(s['shot'] for s in state['progress']),1)
        session=self.store.session(self.sid);updated=copy.deepcopy(session['state']);updated['overrides'][self.ids[1]]=0
        self.store.save(self.sid,session['revision'],updated);self.sku.on_save(self.sid,session['state'],updated)
        state=self.sku.state();self.assertEqual(sum(s['shot'] for s in state['progress']),0)
        self.assertEqual(state['folder_progress']['confirmed_labels'],0)

    def test_replaced_photo_does_not_keep_old_confirmation_in_progress(self):
        self.parse();self.choose();_,state=self.new_plan()
        record=next(r for r in self.store.records(self.sid) if r['id']==self.ids[1])
        record['fingerprint']='replaced-photo';self.store.append_live(self.sid,[record])
        state=self.sku.state()
        self.assertEqual(sum(s['shot'] for s in state['progress']),0)
        self.assertEqual(state['folder_progress']['confirmed_labels'],0)

    def test_other_folder_does_not_get_new_completed_rows(self):
        self.parse();self.choose()
        folder=self.base/'另一拍摄目录';folder.mkdir();other=self.store.ensure_live_session(folder)
        old,_=self.new_plan();self.sku.configure(self.pid,other,'模特图')
        # 已经关联的旧目录证据保留；另一个全新清单不能凭同款号算已拍摄。
        with self.sku.connect() as c:c.execute("UPDATE projects SET metadata=json_set(metadata,'$.removed_at','test-removed') WHERE id=?",(self.pid,))
        state=self.sku.import_file('另一目录.xlsx',fixture_book(),other,'模特图')
        self.assertEqual(sum(s['shot'] for s in state['progress']),0)
        self.assertEqual(state['folder_progress']['two_star'],0)

    def test_browser_reimport_shows_folder_summary_and_old_capture_completion(self):
        from contextlib import ExitStack
        from http.server import ThreadingHTTPServer
        import threading
        from playwright.sync_api import expect,sync_playwright
        import server
        self.parse();self.choose();_,state=self.new_plan()
        row=next(r for r in state['rows'] if r['status']=='已拍摄')
        values=dict(STORE=self.store,DATA=self.store.data,SKU=self.sku,MODELS=self.providers,ARCHIVE=self.archive,
                    CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,
                    UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1500,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                        page.locator('#sku-open').click()
                        summary=page.locator('.progress-folder-summary')
                        expect(summary).to_contain_text('二星标签 2 张')
                        expect(summary).to_contain_text('已确认 1 张 / 1 款')
                        expect(summary).to_contain_text('待确认 1 张')
                        expect(page.locator('#progress-shot')).to_have_text('1')
                        node=page.locator('[data-sku-row="'+row['id']+'"]')
                        node.locator('[data-completion]').click()
                        expect(page.locator('#sku-image-stage img')).to_have_attribute('src',re.compile(self.ids[1]))
                        page.keyboard.press('Escape')
                        out=fixture.ROOT/'build/folder-shooting-progress';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'source-restored-progress.png'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
