from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
import uuid
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import test_full_auto_ocr as fixture

ROOT=Path(__file__).resolve().parents[1]


class FullAutoBrowserTests(unittest.TestCase):
    setUp=fixture.FullAutoWorkflowTests.setUp
    tearDown=fixture.FullAutoWorkflowTests.tearDown
    tick=fixture.FullAutoWorkflowTests.tick

    def test_sidebar_import_shared_switch_and_common_skus_keep_real_scope(self):
        import server
        from test_sku_workflow import fixture_book,CODE
        from sku_match import normalize
        with ExitStack() as stack:
            for key,value in dict(STORE=self.store,DATA=self.store.data,SKU=self.sku,MODELS=self.providers,ARCHIVE=self.archive,MOUSE=None,WATCH=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,COMPONENTS=None,DRIVER=None).items():stack.enter_context(patch.object(server,key,value))
            with self.sku.connect() as c:c.execute("DELETE FROM settings WHERE key='active'")
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        side=page.get_by_role('switch',name='右栏全自动 OCR');self.assertTrue(side.is_disabled())
                        self.assertTrue(page.locator('#shooting-import').is_visible());self.assertTrue(page.locator('#photo-settings-panel').is_visible())
                        page.locator('#shooting-plan-file').set_input_files({'name':'右栏计划.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':fixture_book()})
                        page.wait_for_function('()=>document.getElementById("shooting-document").textContent.includes("右栏计划")&&!document.getElementById("shooting-full-auto").disabled')
                        self.assertTrue(page.locator('#selection-page').is_visible());self.assertTrue(page.locator('#current-shooting-panel').is_visible())
                        project=self.sku.project();pid=project['id'];self.assertEqual((project['scope'],project['session_id']),('模特图',self.sid))
                        pending=[]
                        def hold_toggle(route):
                            if route.request.method=='POST':pending.append(route)
                            else:route.continue_()
                        page.route('**/api/sku/full-auto',hold_toggle)
                        with page.expect_request(lambda request:request.method=='POST' and request.url.endswith('/api/sku/full-auto')):side.check()
                        page.wait_for_function('()=>FramekeepAutomation.isSaving()')
                        self.assertTrue(page.locator('#shooting-import').is_disabled());self.assertTrue(page.locator('#full-auto-ocr').is_disabled())
                        blocked=page.evaluate('async()=>{try{await FramekeepSku.importPlan(new File(["test"],"other.xlsx"),true);return "unexpected"}catch(e){return e.message}}')
                        self.assertIn('正在切换',blocked);self.assertEqual(self.sku.project()['id'],pid)
                        self.assertEqual(len(pending),1);pending[0].continue_();page.unroute('**/api/sku/full-auto',hold_toggle)
                        page.wait_for_function('()=>document.getElementById("shooting-auto-status").textContent.includes("全自动已开启")')
                        self.assertTrue(page.locator('#full-auto-ocr').is_checked());self.tick(3)
                        page.wait_for_function('()=>document.getElementById("shooting-auto-status").textContent.includes("已入库 2")')
                        # 同表重复不进入公共；公共保留每个表各自的完成状态。
                        with self.sku.connect() as c:
                            for i in range(2):c.execute('INSERT INTO sku_rows(id,project,sheet,excel_row,sku,normalized,baseline,status) VALUES (?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,pid,'模特图',10+i,'ONLY-LOCAL',normalize('ONLY-LOCAL'),'','未拍摄'))
                        page.locator('#shooting-common-view').click()
                        page.wait_for_function('()=>document.querySelectorAll("#sku-current-table [data-common-sheet]").length===4')
                        self.assertEqual(page.locator('#sku-current-table tbody tr').count(),2);self.assertNotIn('ONLY-LOCAL',page.locator('#sku-current-table').inner_text())
                        first=page.locator('#sku-current-table tbody tr').first
                        self.assertIn(CODE,first.inner_text());self.assertEqual(first.locator('.sku-state').all_text_contents(),['已拍摄','未拍摄'])
                        self.assertEqual(page.evaluate('FramekeepSku.context().scope'),'模特图');self.assertTrue(self.sku.full_auto_state()['enabled'])
                        page.locator('#sku-open').click();self.assertEqual(page.locator('#sku-common-tab').get_attribute('aria-selected'),'true')
                        self.assertEqual(page.locator('#sku-rows tbody tr').count(),2);self.assertEqual(page.locator('#progress-total').inner_text(),'2');self.assertEqual(page.locator('#progress-shot').inner_text(),'0')
                        page.locator('#sku-filter').select_option('shot');page.wait_for_function('()=>document.getElementById("sku-rows").textContent.includes("没有符合")')
                        page.locator('#sku-filter').select_option('all');page.locator('#sku-close').click()
                        out=ROOT/'build/current-shooting-sidebar';out.mkdir(exist_ok=True,parents=True)
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                            for width in (1680,1000,375,320):
                                page.set_viewport_size({'width':width,'height':1050})
                                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                                self.assertFalse(page.locator('#shooting-automation').evaluate('(e)=>e.scrollWidth>e.clientWidth'))
                                self.assertLessEqual(page.locator('#sku-current-table').evaluate('(e)=>e.scrollWidth-e.clientWidth'),1)
                                self.assertEqual(side.evaluate('(e)=>getComputedStyle(e).height'),'20px')
                                if width==320:page.screenshot(path=str(out/f'common-{theme}-narrow.png'))
                            page.set_viewport_size({'width':1680,'height':1050});page.screenshot(path=str(out/f'common-{theme}.png'))
                        page.locator('#app-settings-open').click();settings=page.get_by_role('switch',name='全自动 OCR 模式');self.assertTrue(settings.is_checked())
                        settings.uncheck();page.wait_for_function('()=>!document.getElementById("shooting-full-auto").checked&&!document.getElementById("full-auto-ocr").disabled')
                        page.locator('#app-settings-close').click();page.locator('#photo-settings-tab').click();self.assertTrue(side.is_visible())
                        side.check();page.wait_for_function('()=>document.getElementById("shooting-auto-status").textContent.includes("全自动已开启")')
                        page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>document.getElementById("shooting-full-auto").checked')
                        page.locator('#current-shooting-tab').click();page.locator('#shooting-common-view').click();page.locator('#sku-current-table [data-common-sheet="细节图"]').first.click()
                        page.wait_for_function('()=>document.getElementById("sku-live-scope").value==="细节图"&&!document.getElementById("shooting-full-auto").checked')
                        self.assertEqual(page.locator('#shooting-scope-view').get_attribute('aria-selected'),'true')
                        self.assertEqual(page.locator('#sku-current-table tbody td:last-child').all_text_contents(),['未拍摄','未拍摄'])
                        self.assertEqual(self.archive.query(CODE)['total'],2);self.assertEqual(self.store.session(self.sid)['state']['overrides'],self.ratings);self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()

    def test_import_gate_toggle_automatic_library_and_all_themes(self):
        import server
        with ExitStack() as stack:
            for key,value in dict(STORE=self.store,DATA=self.store.data,SKU=self.sku,MODELS=self.providers,ARCHIVE=self.archive,MOUSE=None,WATCH=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,COMPONENTS=None,DRIVER=None).items():stack.enter_context(patch.object(server,key,value))
            with self.sku.connect() as c:c.execute("DELETE FROM settings WHERE key='active'")
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.locator('#app-settings-open').click()
                        toggle=page.get_by_role('switch',name='全自动 OCR 模式');page.wait_for_function('()=>document.getElementById("full-auto-ocr-help").textContent.includes("导入")')
                        self.assertTrue(toggle.is_disabled())
                        denied=page.evaluate('async()=>{try{await FramekeepIO.api("/api/sku/full-auto",{enabled:true});return "unexpected"}catch(e){return e.message}}');self.assertIn('导入',denied)
                        self.sku.activate(self.pid);page.wait_for_function('()=>!document.getElementById("full-auto-ocr").disabled')
                        toggle.check();page.wait_for_function('()=>document.getElementById("full-auto-ocr-status").textContent.includes("全自动已开启")')
                        self.tick(3);page.wait_for_function('()=>document.getElementById("full-auto-ocr-status").textContent.includes("已入库 2")')
                        out=ROOT/'build/complete-install';out.mkdir(exist_ok=True,parents=True)
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                            for width in (1680,1000,375,320):
                                page.set_viewport_size({'width':width,'height':1050});toggle.scroll_into_view_if_needed()
                                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                                self.assertFalse(page.locator('#full-auto-ocr-card').evaluate('(e)=>e.scrollWidth>e.clientWidth'))
                                self.assertEqual(toggle.evaluate('(e)=>getComputedStyle(e).height'),'30px')
                            page.set_viewport_size({'width':1680,'height':1050});toggle.scroll_into_view_if_needed();page.screenshot(path=str(out/f'full-auto-{theme}.png'))
                        toggle.uncheck();page.wait_for_function('()=>document.getElementById("full-auto-ocr-status").textContent.includes("全自动已关闭")')
                        page.reload(wait_until='domcontentloaded');page.locator('#app-settings-open').click();self.assertFalse(toggle.is_checked())
                        self.assertEqual(self.archive.query('LC7877496-P905')['total'],2);self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
