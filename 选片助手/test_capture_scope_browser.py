"""目录切换时立即清理旧侧栏，并隔离迟到/失败的读取；只用隔离生成照片。"""
from contextlib import contextmanager,ExitStack
import json
import threading
import unittest
from unittest.mock import patch

import test_manual_metadata_backfill as capture_fixture


class CaptureScopeBrowserTests(unittest.TestCase):
    def setUp(self):
        from folder_archive_catalog_sync import synchronize_catalog_paths
        self.fixture=capture_fixture.ManualBackfillTests();self.fixture.setUp();f=self.fixture
        moves=[]
        for n in range(1,5):
            source=f.photos/f'IMG_{n:04d}.JPG';target=f.photos/('A' if n==1 else 'B')/source.name
            target.parent.mkdir(exist_ok=True);source.rename(target)
            moves.append({'before':str(source),'after':str(target)})
        with f.catalog.connect() as c:synchronize_catalog_paths(c,moves)
        f.api.relocate(moves)
        self.states={folder:f.api.state(f.rid,folder) for folder in ('A','B')}
        for folder,data in self.states.items():
            data['groups']=[{'id':'group-'+folder,'sku':'FOLDER-'+folder,'status':'confirmed',
                             'members':[r['asset_id'] for r in data['records']]}]
        self.fail_b=False

    def tearDown(self):self.fixture.tearDown()

    @contextmanager
    def browser(self):
        import server
        from playwright.sync_api import sync_playwright
        f=self.fixture
        with ExitStack() as stack:
            values=dict(STORE=f.store,CATALOG=f.catalog,ARCHIVE=f.archive,SKU=f.sku,MODELS=f.sku.providers,DATA=f.store.data,WATCH=None,MOUSE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=None)
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000});page.set_default_timeout(8000);errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        def state(route):
                            from urllib.parse import urlsplit,parse_qs
                            folder=parse_qs(urlsplit(route.request.url).query).get('folder',[''])[0]
                            if folder not in self.states:return route.continue_()
                            failed=self.fail_b and folder=='B'
                            route.fulfill(status=503 if failed else 200,content_type='application/json',body=json.dumps({'error':'模拟B目录读取失败'} if failed else self.states[folder]))
                        page.route('**/api/archive-capture/state?*',state)
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepArchiveBrowser&&window.FramekeepArchiveCapture')
                        page.locator('#asset-archive-open').click();self.navigate(page,'A')
                        page.locator('#capture-folder-tab').click();self.wait_folder(page,'A')
                        yield page
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()

    def navigate(self,page,folder):
        page.evaluate('([root,folder])=>{FramekeepArchiveBrowser.navigate(root,folder)}',[self.fixture.rid if folder is not None else '',folder or ''])

    def wait_folder(self,page,folder):
        page.wait_for_function('(folder)=>document.getElementById("capture-groups").textContent.includes("FOLDER-"+folder)',arg=folder)
        self.assertIn(str(1 if folder=='A' else 3),page.locator('#capture-summary').inner_text())

    def hold_requests(self,page):
        page.evaluate('''()=>{
          const original=FramekeepIO.api;window.heldCapture=[];window.holdCapture=[];
          FramekeepIO.api=(path,body,...args)=>{
            const url=new URL(path,location.href),folder=url.searchParams.get('folder')||body?.folder||'',kind=url.pathname.split('/').pop();
            if(url.pathname.startsWith('/api/archive-capture/')&&holdCapture.includes(kind+':'+folder))
              return new Promise((resolve,reject)=>heldCapture.push({kind,folder,body,resolve,reject}));
            return original(path,body,...args);
          };
        }''')

    def release(self,page,kind,folder,data):
        page.evaluate('([kind,folder,data])=>{const job=heldCapture.find(j=>j.kind===kind&&j.folder===folder&&!j.done);if(!job)throw new Error("缺少挂起请求");job.done=true;job.resolve(data)}',[kind,folder,data])

    def test_tree_and_programmatic_folder_navigation_refresh_counts_and_groups(self):
        with self.browser() as page:
            page.locator('#catalog-tree [data-folder="B"]').first.click();self.wait_folder(page,'B')
            self.assertNotIn('FOLDER-A',page.locator('#capture-groups').inner_text())
            self.navigate(page,'A');self.wait_folder(page,'A')
            page.locator('#catalog-grid [data-catalog-item]').first.click()
            page.wait_for_function('()=>document.getElementById("capture-item-status").textContent.includes("不自动识别款号")')

    def test_slow_new_folder_clears_old_state_and_discards_late_reads(self):
        with self.browser() as page:
            self.hold_requests(page)
            page.evaluate('()=>{holdCapture=["state:A","state:B","metadata-write-state:A"];FramekeepArchiveCapture.refresh(true)}')
            page.wait_for_function('()=>heldCapture.some(j=>j.kind==="state"&&j.folder==="A")&&heldCapture.some(j=>j.kind==="metadata-write-state"&&j.folder==="A")')
            self.navigate(page,'B')
            page.wait_for_function('()=>heldCapture.some(j=>j.kind==="state"&&j.folder==="B")')
            self.assertEqual(page.locator('#capture-summary').inner_text(),'')
            self.assertEqual(page.locator('#capture-groups').inner_text(),'')
            self.assertIn('正在读取当前',page.locator('#capture-sync').inner_text())
            self.assertTrue(page.locator('#capture-edit').is_disabled())
            self.release(page,'state','B',self.states['B']);self.wait_folder(page,'B')
            self.release(page,'state','A',self.states['A'])
            self.release(page,'metadata-write-state','A',{'status':'failed','reason':'旧A目录失败不应出现'})
            page.wait_for_timeout(100)
            self.assertIn('FOLDER-B',page.locator('#capture-groups').inner_text())
            self.assertTrue(page.locator('#capture-write-progress').is_hidden())
            self.assertNotIn('旧A目录',page.locator('.catalog-inspector').inner_text())

    def test_failed_folder_read_stays_clear_refresh_recovers_and_empty_scope_clears(self):
        with self.browser() as page:
            self.fail_b=True;self.navigate(page,'B')
            page.wait_for_function('()=>document.getElementById("capture-access").textContent.includes("模拟B目录读取失败")')
            self.assertEqual(page.locator('#capture-summary').inner_text(),'')
            self.assertEqual(page.locator('#capture-groups').inner_text(),'')
            self.fail_b=False;page.locator('#capture-refresh').click();self.wait_folder(page,'B')
            self.navigate(page,None)
            page.wait_for_function('()=>document.getElementById("capture-access").textContent==="选择左侧实际文件夹"')
            self.assertEqual(page.locator('#capture-summary').inner_text(),'')
            self.assertEqual(page.locator('#capture-groups').inner_text(),'')
            self.assertTrue(page.locator('#capture-write-metadata').is_disabled())

    def test_navigation_during_write_submission_clears_then_loads_current_folder(self):
        with self.browser() as page:
            self.hold_requests(page);page.evaluate('()=>{holdCapture=["write-metadata:A"]}')
            page.locator('#capture-write-metadata').click()
            page.wait_for_function('()=>heldCapture.some(j=>j.kind==="write-metadata")')
            self.navigate(page,'B')
            page.wait_for_function('()=>FramekeepArchiveSelection().folder==="B"')
            self.assertEqual(page.locator('#capture-summary').inner_text(),'')
            self.assertEqual(page.locator('#capture-groups').inner_text(),'')
            self.assertEqual(page.evaluate('()=>heldCapture.find(j=>j.kind==="write-metadata").body.folder'),'A')
            self.release(page,'write-metadata','A',{'status':'queued','reason':'A目录排队'})
            self.wait_folder(page,'B')
            self.assertNotIn('A目录排队',page.locator('.catalog-inspector').inner_text())


if __name__=='__main__':unittest.main()
