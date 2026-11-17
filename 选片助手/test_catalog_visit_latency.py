from io import BytesIO
from contextlib import ExitStack
from pathlib import Path
from http.server import ThreadingHTTPServer
import threading,unittest
from unittest.mock import patch,Mock
import test_archive_visits as fixtures


class VisitReplyTests(unittest.TestCase):
    def test_visit_and_thumbnail_preferences_do_not_fsync_business_view_cache(self):
        import server
        for path in ('/api/catalog/visit','/api/catalog/thumbnail-settings'):
            handler=object.__new__(server.Handler);handler.command='POST';handler.path=path;handler.wfile=BytesIO()
            handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock();views=Mock()
            with patch.object(server,'workbench_views',return_value=views):handler.reply({'ok':True})
            views.invalidate.assert_not_called()


class SlowBrowseTests(unittest.TestCase):
    setUp=fixtures.ArchiveVisitTests.setUp
    tearDown=fixtures.ArchiveVisitTests.tearDown
    wait=fixtures.ArchiveVisitTests.wait
    rows=fixtures.ArchiveVisitTests.rows

    def test_photo_list_and_images_do_not_wait_for_file_browse(self):
        import server
        from catalog_store import Store
        from asset_archive import AssetArchive
        from model_providers import Providers
        from playwright.sync_api import sync_playwright,Error
        self.visits.touch(self.rid,'A');self.wait();store=Store(self.catalog.data,Path(__file__).resolve().parents[1]);archive=AssetArchive(store,autostart=False)
        with ExitStack() as stack:
            for name,value in dict(STORE=store,DATA=store.data,CATALOG=self.catalog,ARCHIVE=archive,SKU=None,MODELS=Providers(store.data),WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,ANALYSIS=None,REFERENCES=None,PREVIEWS=None).items():stack.enter_context(patch.object(server,name,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            held=[]
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page();page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                        page.wait_for_function('()=>FramekeepArchiveBrowser.enabled()')
                        page.route('**/api/catalog/browse*',lambda route:held.append(route))
                        page.evaluate('(root)=>{void FramekeepArchiveBrowser.navigate(root,"A")}',self.rid)
                        page.wait_for_function('()=>document.querySelector("#catalog-grid img")?.naturalWidth>0',timeout=3000)
                        self.assertTrue(held,'文件枚举仍未返回时照片应已显示')
                    finally:
                        for route in held:
                            try:route.abort()
                            except Error:pass
                        browser.close()
            finally:http.shutdown();http.server_close();archive.close()


if __name__=='__main__':unittest.main()
