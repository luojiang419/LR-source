"""SKU全部平铺、滚动、连续选择和迟到批次隔离。"""
from contextlib import contextmanager,ExitStack
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
import threading,unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
import test_sku_library_catalog as fixtures


class ContinuousSkuBrowserTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.CatalogLibraryTests();self.fixture.setUp();self.f=self.fixture.f;f=self.f
        for i in range(125,385):Image.new('RGB',(80,60),(i%255,70,100)).save(f.photos/'A'/f'added{i:03}.JPG')
        f.catalog.scan(f.root)
        with f.catalog.connect() as c:c.execute("UPDATE items SET sku='A-100',normalized='A100',source='shooting' WHERE folder='A'")
        f.archive.apply(f.archive.preview(f.sid,[f.ids[1]],'B-200','细节图')['preview_id'])
        self.fixture.index.refresh()

    def tearDown(self):self.fixture.tearDown()

    @contextmanager
    def browser(self):
        import server
        f=self.f;http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=f.store,DATA=f.store.data,MODELS=f.providers,SKU=f.sku,ARCHIVE=f.archive,CATALOG=f.catalog,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        page.locator('#sku-database-open').click()
                        yield page
                        # 此启动取消异常在任务231修改前已独立复现；其余脚本异常仍须失败。
                        self.assertEqual([e for e in errors if e!='signal is aborted without reason'],[])
                    finally:browser.close()
            finally:http.shutdown();http.server_close()

    def test_all_images_scroll_reload_and_shift_selection_across_batches(self):
        with self.browser() as page:
            page.locator('#sku-database-list [data-db-sku="A100"]').click()
            page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===387')
            self.assertEqual(page.locator('#sku-database-prev,#sku-database-next').count(),0)
            cards=page.locator('#sku-database-grid .catalog-card');cards.nth(230).click();cards.nth(260).click(modifiers=['Shift'])
            page.wait_for_function('()=>FramekeepSkuLibrary.selectedRows.length===31')
            cards.last.scroll_into_view_if_needed();page.wait_for_function('()=>{const i=[...document.querySelectorAll("#sku-database-grid img")].at(-1);return i.complete&&i.naturalWidth>0}')
            self.assertGreater(page.locator('#sku-database-grid').evaluate('(e)=>e.scrollTop'),0)
            page.screenshot(path=str(self.f.base/'all-images-bottom.png'))
            page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===387')
            self.assertIn('387 张',page.locator('#sku-database-title').inner_text())

    def test_late_batch_cannot_append_to_a_different_sku(self):
        with self.browser() as page:
            page.locator('#sku-database-list [data-db-sku="B200"]').click()
            page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===1')
            held=[]
            def delay(route):
                q=parse_qs(urlsplit(route.request.url).query)
                if q.get('sku')==['A100'] and q.get('offset')==['240']:held.append(route)
                else:route.continue_()
            page.route('**/api/sku/library?*',delay)
            page.evaluate('()=>FramekeepViewCache.invalidate("/api/catalog/visit")')
            page.locator('#sku-database-list [data-db-sku="A100"]').click()
            page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===240')
            page.wait_for_timeout(100);self.assertTrue(held)
            page.locator('#sku-database-list [data-db-sku="B200"]').click()
            page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===1')
            with page.expect_response(lambda r:'offset=240' in r.url):held[0].continue_()
            page.wait_for_timeout(150)
            self.assertEqual(page.locator('#sku-database-grid .catalog-card').count(),1)
            self.assertIn('B-200',page.locator('#sku-database-title').inner_text())
            page.unroute('**/api/sku/library?*',delay)
            page.locator('#sku-database-list [data-db-sku="A100"]').click()
            page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===387')

    def test_context_menu_reveals_catalog_and_portable_photos_by_real_item_id(self):
        from sku_library_index import SkuLibraryIndex
        original=SkuLibraryIndex.browse
        for source in ('catalog','portable'):
            with self.subTest(source=source):
                def listing(index,*args,**kwargs):
                    data=original(index,*args,**kwargs)
                    rows=[]
                    for r in data['rows']:
                        row=dict(r)
                        if row.get('source')=='catalog':
                            row['source']=source
                            if source=='portable':row['id']=row.pop('catalog_item_id')
                        rows.append(row)
                    return {**data,'rows':rows}
                with patch.object(SkuLibraryIndex,'browse',listing),patch('file_reveal.reveal_path') as reveal:
                    with self.browser() as page:
                        page.locator('#sku-database-list [data-db-sku="A100"]').click()
                        card=page.locator('#sku-database-grid .catalog-card').first;card.click(button='right')
                        with page.expect_response(lambda r:r.url.endswith('/api/reveal-file')) as response:
                            page.locator('[data-pool-action="reveal"]').click()
                        self.assertTrue(response.value.ok,response.value.text())
                        self.assertIn('item_id',response.value.request.post_data_json)
                        self.assertNotIn('capture_id',response.value.request.post_data_json)
                        reveal.assert_called_once_with((self.f.photos/'A'/'added000.JPG').resolve())


if __name__=='__main__':unittest.main()
