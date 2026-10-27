"""选片右栏当前拍摄页：原控件、去重表格、后台状态与独立滚动。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile,threading,unittest,uuid
from unittest.mock import patch
from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from asset_archive import AssetArchive
from model_providers import Providers
from sku_service import SkuService
from sku_match import normalize
from test_sku_workflow import fixture_book,CODE
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]

class CurrentShootingSidebarTests(unittest.TestCase):
    def test_controls_unique_rows_live_status_scroll_tabs_and_empty_state(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos)
            for i in range(2):
                p=photos/f'{i}.JPG';Image.new('RGB',(100,80),(80+i*20,120,150)).save(p);store.append_live(sid,[media.capture_record(photos,p,None,{})])
            store.activate(sid);providers=Providers(store.data);sku=SkuService(store,providers,autostart=False);archive=AssetArchive(store,autostart=False)
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            values={'STORE':store,'DATA':store.data,'SKU':sku,'ARCHIVE':archive,'MODELS':providers,'ANALYSIS':None,'WATCH':None,'MOUSE':None,'RAWLINK':None,'FILESEARCH':None}
            patches=[patch.object(server,k,v) for k,v in values.items()]
            for p in patches:p.start()
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    page=browser.new_page(viewport={'width':1680,'height':1050},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                    page.locator('#current-shooting-tab').click();page.wait_for_function('()=>document.querySelector("#sku-current-table").textContent.includes("尚未导入")')
                    self.assertEqual(page.locator('.workspace-tool-content #sku-live-bar').count(),0)
                    self.assertEqual(page.locator('#sku-live-scope').count(),1)
                    page.locator('#sku-open').click();page.locator('#sku-file').set_input_files({'name':'计划.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':fixture_book()})
                    page.wait_for_function('()=>document.querySelectorAll("#sku-panel-scope option").length===4');page.locator('#sku-panel-scope').select_option('模特图')
                    page.wait_for_function('()=>document.getElementById("progress-total")?.textContent==="2"');page.locator('#sku-close').click()
                    page.wait_for_function('()=>document.querySelectorAll("#sku-current-table tbody tr").length===2')
                    self.assertEqual(page.locator('#sku-current-table th').all_text_contents(),['序号','款号','拍摄状态','款号图片'])
                    self.assertEqual(page.locator('#sku-current-table tbody td:first-child').all_text_contents(),['1','2'])
                    self.assertEqual(page.locator('#sku-current-table tbody td:nth-child(3)').all_text_contents(),['未拍摄','未拍摄'])
                    page.locator('#sku-bind-current').click();page.wait_for_function('()=>document.getElementById("sku-bind-current").textContent==="已绑定当前目录"')
                    pid=sku.project()['id']
                    with sku.connect() as c:
                        c.execute('INSERT INTO sku_rows(id,project,sheet,excel_row,sku,normalized,baseline,status) VALUES (?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,pid,'模特图',5,CODE,normalize(CODE),'','已拍摄'))
                    page.wait_for_function('()=>document.querySelector("#sku-current-table tbody tr td:nth-child(3)").textContent==="已拍摄"')
                    self.assertEqual(page.locator('#sku-current-table tbody tr').count(),2);self.assertIn('1 / 2',page.locator('#sku-live-count').inner_text())
                    page.locator('#sku-live-scope').select_option('细节图');page.wait_for_function('()=>document.getElementById("sku-live-scope").value==="细节图"')
                    page.wait_for_function('()=>[...document.querySelectorAll("#sku-current-table tbody tr td:nth-child(3)")].every(e=>e.textContent==="未拍摄")')
                    page.locator('#sku-review-open').click();self.assertTrue(page.locator('#sku-dialog').is_visible());self.assertTrue(page.locator('#sku-records').evaluate('(e)=>e.open'))
                    page.locator('#sku-close').click();page.locator('#sku-live-scope').select_option('模特图')
                    with sku.connect() as c:
                        for i in range(60):
                            code=f'LONG-SKU-{i:03d}-ABCDEF1234567890'
                            c.execute('INSERT INTO sku_rows(id,project,sheet,excel_row,sku,normalized,baseline,status) VALUES (?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,pid,'模特图',10+i,code,normalize(code),'','未拍摄'))
                    page.wait_for_function('()=>document.querySelectorAll("#sku-current-table tbody tr").length===62')
                    table=page.locator('#sku-current-table');before=page.locator('#sku-bind-current').bounding_box();head=page.locator('#sku-current-table th').first.bounding_box()
                    table.hover();page.mouse.wheel(0,700);page.wait_for_function('()=>document.getElementById("sku-current-table").scrollTop>100')
                    self.assertEqual(page.locator('#sku-bind-current').bounding_box(),before);self.assertAlmostEqual(page.locator('#sku-current-table th').first.bounding_box()['y'],head['y'],delta=1)
                    for theme in ('dark','light','pure-white','hardcore'):
                        page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                        for width in (1680,1024,375,320):
                            page.set_viewport_size({'width':width,'height':1050});self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                            self.assertLessEqual(table.evaluate('(e)=>e.scrollWidth-e.clientWidth'),1)
                        page.set_viewport_size({'width':1680,'height':1050});out=ROOT/'build/current-shooting-sidebar';out.mkdir(exist_ok=True)
                        table.evaluate('(e)=>e.scrollTop=0');page.screenshot(path=str(out/f'{theme}.png'))
                    selected=page.evaluate('Framekeep.remoteTarget().photoId')
                    page.locator('#current-shooting-tab').press('Home');self.assertTrue(page.locator('#photo-settings-panel').is_visible());self.assertTrue(page.locator('#stars').is_visible())
                    page.keyboard.press('3');self.assertEqual(page.locator('#stars .on').count(),3);page.keyboard.press('Control+z');self.assertEqual(page.evaluate('Framekeep.persistedState().overrides'),{});page.evaluate('FramekeepIO.flush()')
                    page.locator('#photo-settings-tab').press('End');self.assertTrue(page.locator('#current-shooting-panel').is_visible());self.assertEqual(page.evaluate('Framekeep.remoteTarget().photoId'),selected)
                    self.assertTrue(page.locator('#send-lr').is_visible())
                    page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading');page.wait_for_selector('#current-shooting-panel:not(.hidden)')
                    page.wait_for_function('()=>document.querySelectorAll("#sku-current-table tbody tr").length===62')
                    self.assertEqual(page.locator('.selection-sidebar-tabs [aria-selected=true]').count(),1)
                    self.assertEqual(store.session(sid)['state'].get('overrides',{}),{});self.assertFalse(errors,errors)
                    browser.close()
            finally:
                http.shutdown();http.server_close();sku.close();archive.close()
                for p in reversed(patches):p.stop()
