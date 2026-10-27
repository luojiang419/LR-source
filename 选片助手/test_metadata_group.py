"""整款编辑与真实元数据写入，不使用用户照片。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import threading
import json
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from metadata_group import preview_group,apply_group
from metadata_writer import sha
import test_ocr_metadata as fixtures
import media_engine as media
from sku_library import labels
ROOT,CODE=fixtures.ROOT,fixtures.CODE


class MetadataGroupTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.OcrMetadataTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def drain(self):
        for _ in range(100):
            with self.f.archive.connect() as c:
                if not c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued'").fetchone():return
            self.f.archive.process_one()
        self.fail('写入队列未结束')
    def preview(self,cid=None,code=CODE):
        f=self.f;cid=cid or f.ids[0]
        return preview_group(f.archive,f.sku,f.sid,cid,[cid],code,'模特图',f.pid)
    def test_edit_from_any_member_updates_all_and_keeps_next_same_sku_interval(self):
        f=self.f;f.parse();f.choose();f.sku.parse_capture(f.sid,f.ids[3]);f.choose(CODE,f.ids[3]);self.drain()
        untouched=[sha(f.photos/f'{i}.JPG') for i in (2,3)]
        for cid in f.ids[:2]:self.assertEqual(self.preview(cid)['group']['capture_ids'],f.ids[:2])
        p=self.preview();result=apply_group(f.archive,f.sku,p['preview_id'],'CORRECT-100')
        self.assertEqual(result['capture_count'],2);self.assertEqual(result['sku'],'CORRECT-100');self.drain()
        for i in range(2):self.assertEqual(f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'],'CORRECT-100')
        self.assertEqual(untouched,[sha(f.photos/f'{i}.JPG') for i in (2,3)])
        self.assertEqual(labels(f.sku,f.sid)['labels'],dict(zip(f.ids,['CORRECT-100','CORRECT-100',CODE,CODE])))
        with self.assertRaisesRegex(ValueError,'失效'):apply_group(f.archive,f.sku,p['preview_id'],'AGAIN-100')
        # 修改后继续点击推荐款号仍更新整款，不因编辑变成单图人工锁定。
        f.choose('AGAIN-100');self.drain()
        for i in range(2):self.assertEqual(f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'],'AGAIN-100')
    def test_explicit_edit_repairs_old_single_manual_assignment_and_no_ocr_label(self):
        f=self.f
        f.archive.apply(f.archive.preview(f.sid,[f.ids[1]],'OLD-100','模特图',f.pid)['preview_id']);self.drain()
        p=self.preview(code='')
        result=apply_group(f.archive,f.sku,p['preview_id'],'MANUAL-200');self.drain()
        self.assertEqual(result['capture_count'],2)
        for i in range(2):self.assertEqual(f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'],'MANUAL-200')
        self.assertEqual(labels(f.sku,f.sid)['labels'],dict.fromkeys(f.ids[:2],'MANUAL-200'))
    def test_changed_boundary_and_invalid_sku_write_nothing(self):
        f=self.f;p=self.preview();before=[sha(f.photos/f'{i}.JPG') for i in range(4)]
        with self.assertRaisesRegex(ValueError,'有效 SKU'):apply_group(f.archive,f.sku,p['preview_id'],'')
        session=f.store.session(f.sid);state=session['state'];state['overrides'][f.ids[0]]=2;f.store.save(f.sid,session['revision'],state)
        with self.assertRaisesRegex(ValueError,'范围或归属已变化'):apply_group(f.archive,f.sku,p['preview_id'],'WRONG-100')
        self.assertEqual(before,[sha(f.photos/f'{i}.JPG') for i in range(4)])
        with f.archive.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM operations').fetchone()[0],0)
    def test_empty_draft_can_be_edited_and_legacy_apply_cannot_bypass_editor(self):
        f=self.f;p=self.preview(code='');self.assertEqual(p['sku'],'')
        with self.assertRaisesRegex(ValueError,'整款编辑'):f.archive.apply(p['preview_id'])
        result=apply_group(f.archive,f.sku,p['preview_id'],'  EDIT-100  ');self.drain()
        self.assertEqual(result['sku'],'EDIT-100')
        for i in range(2):self.assertEqual(f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'],'EDIT-100')
    def test_group_write_includes_paired_raw_and_jpg_components(self):
        # RAW 占位仅验证成员展开与队列，不将其当作真实 RAW 写入样片。
        f=self.f;f.parse();f.choose();self.drain()
        raw=f.photos/'1.ARW';raw.write_bytes(b'isolated RAW pairing fixture')
        record=media.capture_record(f.photos,raw,f.photos/'1.JPG',{'DateTimeOriginal':'2026:10:21 10:00:01'})
        f.store.append_live(f.sid,[record]);original=f.archive.writer.read
        with patch.object(f.archive.writer,'read',side_effect=lambda path,**kwargs:{} if str(path).endswith('.ARW') else original(path,**kwargs)):
            f.archive.ensure(f.sid,f.ids[1]);p=self.preview()
            result=apply_group(f.archive,f.sku,p['preview_id'],'PAIR-200')
        self.assertEqual(result['capture_count'],2)
        self.assertEqual({job['path'].replace('\\','/').rsplit('/',1)[-1] for job in result['jobs']},{'0.JPG','1.JPG','1.ARW'})
        with f.archive.connect() as c:
            jobs=c.execute('SELECT fields FROM metadata_jobs WHERE operation=?',(result['operation']['id'],)).fetchall()
        self.assertTrue(all(json.loads(row['fields'])['SKU']=='PAIR-200' for row in jobs))
    def test_browser_editor_changes_whole_interval_with_one_submit(self):
        import server
        f=self.f;f.parse();f.choose();self.drain()
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=f.store,DATA=f.store.data,MODELS=f.providers,SKU=f.sku,ARCHIVE=f.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce');errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        page.locator('[data-watch-photo="'+f.ids[0]+'"]').click();page.locator('.asset-panel').evaluate('(el)=>el.open=true')
                        page.wait_for_function('(code)=>document.getElementById("asset-sku").value===code',arg=CODE)
                        page.locator('#asset-preview').click();page.wait_for_selector('#asset-preview-dialog[open]')
                        self.assertEqual(page.locator('#asset-preview-content tbody tr').count(),2)
                        page.locator('#asset-group-sku').fill('EDITOR-300')
                        out=ROOT/'build/sku-group-acceptance';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'whole-group-editor.png'))
                        page.locator('#asset-apply').click();page.wait_for_function('()=>!document.getElementById("asset-preview-dialog").open')
                        self.drain()
                        for i in range(2):self.assertEqual(f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'],'EDITOR-300')
                        for i in (2,3):self.assertFalse(f.archive.writer.read(f.photos/f'{i}.JPG').get('XMP-fk:SKU'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
