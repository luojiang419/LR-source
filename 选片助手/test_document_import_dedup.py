"""相同导入内容复用任务，保留进度；移除后重新建立独立的新进度。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack,contextmanager
from http.server import ThreadingHTTPServer
import io,json,threading,unittest
from unittest.mock import patch
from zipfile import ZipFile

from playwright.sync_api import sync_playwright
from sku_service import SkuService
from metadata_group import confirm_group
import test_ocr_metadata as fixture
import server


class ImportDedupTests(unittest.TestCase):
    def setUp(self):
        fixture.OcrMetadataTests.setUp(self)
        self.content=(self.sku.root/self.pid/'source.xlsx').read_bytes()
    tearDown=fixture.OcrMetadataTests.tearDown
    parse=fixture.OcrMetadataTests.parse
    choose=fixture.OcrMetadataTests.choose

    def changed(self):
        target=io.BytesIO()
        with ZipFile(io.BytesIO(self.content)) as source,ZipFile(target,'w') as output:
            for info in source.infolist():
                raw=source.read(info)
                if info.filename=='xl/worksheets/sheet1.xml':raw=raw.replace(fixture.CODE.encode(),b'DIFFERENT-100')
                output.writestr(info,raw)
        return target.getvalue()

    def test_renamed_repeat_keeps_progress_ids_working_source_and_preferences(self):
        self.parse();self.choose('UNPLANNED-100')  # 应用会扩展工作副本，不能拿其当前哈希判重。
        metadata=self.sku.project()['metadata'];metadata['presentation']={'headers':{'sku':'商品编码'},'images':{}}
        with self.sku.connect() as c:c.execute('UPDATE projects SET metadata=? WHERE id=?',(json.dumps(metadata),self.pid))
        before=[(r['id'],r['sku'],r['status'],r['completed_at']) for r in self.sku.state()['rows']]
        source=(self.sku.root/self.pid/'source.xlsx').read_bytes();self.assertNotEqual(source,self.content)
        other=self.base/'other';other.mkdir();sid=self.store.ensure_live_session(other)
        result=self.sku.import_file('renamed.xlsx',self.content,sid,'细节图')
        self.assertTrue(result['already_imported']);self.assertEqual(result['project']['id'],self.pid)
        self.assertEqual(result['project']['scope'],'模特图');self.assertEqual(result['project']['session_id'],self.sid)
        self.assertEqual(result['project']['presentation']['headers']['sku'],'商品编码')
        self.assertEqual(before,[(r['id'],r['sku'],r['status'],r['completed_at']) for r in result['rows']])
        self.assertEqual((self.sku.root/self.pid/'source.xlsx').read_bytes(),source)
        self.assertEqual(len(result['projects']),1);self.assertEqual(len(list(self.sku.root.iterdir())),1)

    def test_same_name_with_different_content_is_an_independent_document(self):
        result=self.sku.import_file('plan.xlsx',self.changed(),self.sid)
        self.assertFalse(result['already_imported']);self.assertNotEqual(result['project']['id'],self.pid)
        self.assertEqual(len(result['projects']),2)
        self.assertTrue(any(row['sku']=='DIFFERENT-100' for row in result['rows']))

    def test_two_services_import_simultaneously_create_only_one_new_task(self):
        second=SkuService(self.store,self.providers,archive=None,autostart=False,require_sku_choice=True)
        barrier=threading.Barrier(2);content=self.changed()
        def run(sku):barrier.wait();return sku.import_file('same.xlsx',content,self.sid)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[self.sku,second]))
            self.assertEqual(results[0]['project']['id'],results[1]['project']['id'])
            self.assertEqual(sorted(r['already_imported'] for r in results),[False,True])
            with self.sku.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM projects').fetchone()[0],2)
            self.assertEqual(len(list(self.sku.root.iterdir())),2)
        finally:second.close()

    def test_restart_still_reuses_original_task(self):
        self.parse();self.choose()
        self.sku.close();self.sku=SkuService(self.store,self.providers,archive=self.archive,autostart=False,require_sku_choice=True);self.archive.sku=self.sku
        result=self.sku.import_file('copy.xlsx',self.content)
        self.assertTrue(result['already_imported']);self.assertEqual(result['project']['id'],self.pid)
        self.assertTrue(any(row['status']=='已拍摄' for row in result['rows']))

    def test_removed_document_stays_removed_and_new_task_recovers_valid_folder_progress(self):
        self.parse();self.choose()
        with self.sku.connect() as c:
            c.execute("UPDATE projects SET metadata=json_set(metadata,'$.removed_at','2026-10-27'),enabled=0 WHERE id=?",(self.pid,))
            c.execute("DELETE FROM settings WHERE key='active'")
        with self.archive.connect() as c:c.execute("UPDATE streams SET state='paused' WHERE project=?",(self.pid,))
        self.assertEqual(self.sku.state()['projects'],[])
        with self.assertRaisesRegex(ValueError,'已移除'):self.sku.activate(self.pid)
        result=self.sku.import_file('plan.xlsx',self.content,self.sid,'模特图');pid=result['project']['id']
        self.assertNotEqual(pid,self.pid);self.assertFalse(result['already_imported'])
        self.assertEqual([(r['sheet'],r['sku']) for r in result['rows'] if r['status']=='已拍摄'],[('模特图',fixture.CODE)])
        self.assertEqual(len(result['projects']),1)
        confirm_group(self.archive,self.sku,self.sid,self.ids[1],[self.ids[1]],fixture.CODE,'模特图',pid)
        state=self.sku.state();self.assertEqual(state['project']['id'],pid)
        self.assertTrue(any(row['status']=='已拍摄' for row in state['rows']))
        self.assertEqual(self.sku.import_file('same.xlsx',self.content)['project']['id'],pid)
        with self.sku.connect() as c:self.assertTrue(c.execute('SELECT 1 FROM projects WHERE id=?',(self.pid,)).fetchone())

    def test_invalid_import_leaves_no_task_or_working_folder(self):
        before=len(list(self.sku.root.iterdir()))
        with self.assertRaisesRegex(ValueError,'无法导入'):self.sku.import_file('broken.xlsx',b'invalid')
        self.assertEqual(len(list(self.sku.root.iterdir())),before)
        self.assertEqual(len(self.sku.state()['projects']),1)

    def test_browser_upload_repeat_reuses_one_select_option_and_reports_it(self):
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=self.store,DATA=self.store.data,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1580,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        page.locator('#sku-open').click()
                        for name in ('plan.xlsx','renamed.xlsx'):
                            with page.expect_response('**/api/sku/import') as response:page.locator('#sku-file').set_input_files({'name':name,'mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':self.content})
                            self.assertEqual(response.value.status,200);self.assertTrue(response.value.json()['already_imported'])
                            page.wait_for_function('()=>document.getElementById("sku-message").textContent.includes("复用")')
                        self.assertEqual(page.locator('#sku-projects option:not([value=""])').count(),1)
                        out=fixture.ROOT/'build/document-import-dedup';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'reused-document.png'),full_page=True)
                        self.assertEqual(errors,[])
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
