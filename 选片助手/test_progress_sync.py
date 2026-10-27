"""误移除文档后显式恢复目录进度，保持原确认和原始文件。"""
from contextlib import ExitStack
import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image
from playwright.sync_api import expect,sync_playwright
from folder_watch import FolderWatch
import test_ocr_metadata as fixture
from test_sku_workflow import fixture_book,CODE


class ProgressSyncTests(unittest.TestCase):
    setUp=fixture.OcrMetadataTests.setUp
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose

    def reimport_unbound(self):
        old=self.pid
        with self.sku.connect() as c:
            c.execute("UPDATE projects SET metadata=json_set(metadata,'$.removed_at','removed'),enabled=0 WHERE id=?",(old,))
        with self.archive.connect() as c:c.execute("UPDATE streams SET state='paused' WHERE project=?",(old,))
        self.original=self.base/'用户原表.xlsx';self.original.write_bytes(fixture_book())
        result=self.sku.import_file(self.original.name,self.original.read_bytes(),preferred_scope='模特图')
        self.pid=result['project']['id'];self.assertNotEqual(self.pid,old)
        self.assertIsNone(result['project']['session_id'])
        self.assertEqual(sum(p['shot'] for p in result['progress']),0)
        return old

    def snapshot(self):
        with self.sku.connect() as c:jobs=[tuple(r) for r in c.execute('SELECT * FROM jobs ORDER BY id')]
        with self.archive.connect() as c:
            assignments=[tuple(r) for r in c.execute('SELECT * FROM assignments ORDER BY asset,revision')]
            streams=[tuple(r) for r in c.execute('SELECT * FROM streams ORDER BY id')]
        return jobs,assignments,streams

    def test_explicit_sync_restores_removed_document_and_workbook_idempotently(self):
        self.parse();self.choose();old=self.reimport_unbound()
        self.sku.configure(self.pid,None,'模特图',enabled=False)
        before=self.snapshot();hashes={p:p.read_bytes() for p in self.photos.glob('*.JPG')}
        original=self.original.read_bytes();project=self.sku.project()
        with patch.object(self.sku,'recognizer',side_effect=AssertionError('同步不能再次识别')):
            result=self.sku.sync_progress(self.pid,self.sid)
            report=result['progress_sync'];self.assertEqual(report['matched_skus'],1)
            self.assertEqual(report['restored_rows'],1);self.assertEqual(report['pending_labels'],1)
            self.assertTrue(report['workbook_updated'])
            row=next(r for r in result['rows'] if r['status']=='已拍摄')
            self.assertEqual(row['sheet'],'模特图');self.assertEqual(row['completion']['capture_id'],self.ids[1])
            self.assertEqual(result['project']['session_id'],project['session_id'])
            self.assertEqual(result['project']['scope'],project['scope'])
            self.assertEqual(result['project']['enabled'],project['enabled'])
            again=self.sku.sync_progress(self.pid,self.sid)
            self.assertEqual(again['progress_sync']['restored_rows'],0)
            self.assertEqual(len(again['rows']),len(result['rows']))
        self.assertTrue(self.sku.project(old)['metadata']['removed_at'])
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.original.read_bytes(),original)
        self.assertTrue(all(p.read_bytes()==content for p,content in hashes.items()))
        with ZipFile(result['project']['export_path']) as workbook:
            self.assertIsNone(workbook.testzip())
            xml=''.join(workbook.read(name).decode('utf8') for name in workbook.namelist() if name.endswith('.xml'))
            self.assertIn('已拍摄',xml);self.assertIn('10:00:01',xml)

    def test_sync_across_sheet_types_preserves_original_confirmation_times(self):
        self.parse();self.choose()
        self.sku.configure(self.pid,self.sid,'细节图');self.sku.parse_capture(self.sid,self.ids[3]);self.choose(CODE,self.ids[3])
        self.reimport_unbound();result=self.sku.sync_progress(self.pid,self.sid)
        rows=[r for r in result['rows'] if r['sku']==CODE]
        self.assertEqual({r['sheet']:r['status'] for r in rows},{'模特图':'已拍摄','细节图':'已拍摄'})
        self.assertEqual({r['completed_at'][-8:] for r in rows},{'10:00:01','10:00:03'})
        self.assertEqual(result['project']['scope'],'模特图')

    def test_unplanned_rows_are_deduplicated_and_export_failure_can_retry(self):
        self.parse();self.choose('UNPLANNED-100');self.reimport_unbound()
        for error in (ValueError('文件被占用'),PermissionError('工作副本被占用')):
            with self.subTest(error=type(error).__name__),patch.object(self.sku,'export',side_effect=error):
                result=self.sku.sync_progress(self.pid,self.sid)
            self.assertFalse(result['progress_sync']['workbook_updated'])
            self.assertEqual(result['progress_sync']['unplanned_skus'],1)
            self.assertEqual(len([r for r in result['rows'] if r['sheet']=='临时插拍']),1)
        retry=self.sku.sync_progress(self.pid,self.sid)
        self.assertTrue(retry['progress_sync']['workbook_updated'])
        self.assertEqual(retry['progress_sync']['restored_rows'],0)
        self.assertEqual(len([r for r in retry['rows'] if r['sheet']=='临时插拍']),1)

    def test_invalid_or_stale_document_and_missing_folder_do_not_change_progress(self):
        self.parse();self.choose();old=self.reimport_unbound();before=copy.deepcopy(self.sku.rows(self.pid))
        for pid,sid in [(old,self.sid),(self.pid,None),(self.pid,'missing')]:
            with self.assertRaises(ValueError):self.sku.sync_progress(pid,sid)
        self.assertEqual(self.sku.rows(self.pid),before)

    def test_browser_button_uses_monitor_source_and_guards_stale_requests(self):
        import server
        self.parse();self.choose();self.reimport_unbound()
        watched=FolderWatch(self.store,autostart=False);watched.start(str(self.photos))
        other=self.base/'仅浏览的目录';other.mkdir();p=other/'other.JPG';Image.new('RGB',(100,80),'blue').save(p)
        import media_engine as media
        other_sid=self.store.ensure_live_session(other);self.store.append_live(other_sid,[media.capture_record(other,p,None,{})]);self.store.activate(other_sid)
        out=fixture.ROOT/'build/document-progress-sync';out.mkdir(parents=True,exist_ok=True)
        with ExitStack() as patches:
            for name,value in dict(STORE=self.store,DATA=self.store.data,SKU=self.sku,MODELS=self.providers,ARCHIVE=self.archive,
                                   WATCH=watched,PREVIEWS=None,CATALOG=None,ANALYSIS=None,MOUSE=None,CANON=None,
                                   RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None).items():
                patches.enter_context(patch.object(server,name,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{http.server_port}'
            try:
                with urllib.request.urlopen(url+'/api/state') as response:token=json.load(response)['request_token']
                def post(sid,generation):
                    request=urllib.request.Request(url+'/api/sku/sync-progress',data=json.dumps({'project_id':self.pid,'session_id':sid,'watch_generation':generation}).encode(),headers={'Content-Type':'application/json','Origin':url,'X-Framekeep-Token':token})
                    return urllib.request.urlopen(request)
                for sid,generation in [(other_sid,watched.state()['generation']),(self.sid,'stale')]:
                    with self.assertRaises(urllib.error.HTTPError) as error:post(sid,generation)
                    self.assertEqual(error.exception.code,400)
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1500,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                        button=page.locator('#shooting-sync-progress');expect(button).to_be_enabled()
                        held=[];page.route('**/api/sku/sync-progress',lambda route:held.append(route))
                        button.click()
                        page.wait_for_function('()=>document.getElementById("shooting-sync-progress").textContent.includes("正在同步")')
                        expect(button).to_be_disabled();expect(page.locator('#sku-sync-progress')).to_be_disabled()
                        for _ in range(60):
                            if held:break
                            page.wait_for_timeout(20)
                        self.assertEqual(len(held),1);self.assertEqual(held[0].request.post_data_json['session_id'],self.sid)
                        held[0].continue_();page.unroute('**/api/sku/sync-progress')
                        expect(page.locator('#shooting-sync-status')).to_contain_text('文档匹配 1 款')
                        expect(page.locator('#shooting-sync-status')).to_contain_text('1 张标签待确认')
                        expect(button).to_be_enabled();self.assertEqual(page.evaluate('FRAMEKEEP_SESSION.id'),other_sid)
                        page.locator('#sku-open').click();expect(page.locator('#progress-shot')).to_have_text('1')
                        page.locator('#sku-sync-progress').click()
                        expect(page.locator('#sku-message')).to_contain_text('已有进度已核对')
                        page.screenshot(path=str(out/'source-synchronized.png'))
                        watched.stop()
                        with page.expect_response('**/api/sku/sync-progress') as response:page.locator('#sku-sync-progress').click()
                        paused=response.value.json()
                        self.assertEqual(paused['progress_sync']['session_id'],other_sid)
                        expect(page.locator('#progress-shot')).to_have_text('1')
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            page.set_viewport_size({'width':980,'height':800})
                            expect(page.locator('#sku-sync-progress')).to_be_visible()
                            self.assertTrue(page.locator('#sku-sync-progress').evaluate('e=>e.getBoundingClientRect().right<=innerWidth'))
                        page.set_viewport_size({'width':1500,'height':960});page.evaluate('()=>FramekeepTheme.set("dark")')
                        page.screenshot(path=str(out/'source-paused-folder.png'));self.assertFalse(errors,errors)
                        with self.sku.connect() as c:
                            c.execute("UPDATE projects SET metadata=json_set(metadata,'$.removed_at','removed') WHERE id=?",(self.pid,))
                            c.execute("DELETE FROM settings WHERE key='active'")
                        expect(page.locator('#sku-sync-progress')).to_be_disabled();expect(button).to_be_disabled()
                        (out/'browser-result.json').write_text(json.dumps({'passed':True,'monitorSourcePreferred':True,'pausedUsesOpenFolder':True,'noBrowsingSwitch':True,'disabledWithoutDocument':True,'duplicateClickDisabled':True,'pendingLabelsShown':True,'repeatIdempotent':True,'themes':4,'pageErrors':errors}),encoding='utf8')
                    finally:browser.close()
            finally:watched.close();http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
