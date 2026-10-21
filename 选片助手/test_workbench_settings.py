"""设置页迁移、唯一拍摄类型、历史备案读取和写入预览的隔离验证。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import hashlib,tempfile,threading,unittest
from unittest.mock import patch
from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from asset_archive import AssetArchive
from sku_service import SkuService
from model_providers import Providers
from test_sku_workflow import fixture_book
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]

class WorkbenchSettingsTests(unittest.TestCase):
    def test_settings_single_type_history_preview_navigation_and_responsive(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();photo=photos/'one.JPG';Image.new('RGB',(160,120),'steelblue').save(photo)
            digest=hashlib.sha256(photo.read_bytes()).hexdigest();store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos)
            record=media.capture_record(photos,photo,None,{});store.append_live(sid,[record]);store.activate(sid)
            providers=Providers(store.data);archive=AssetArchive(store,autostart=False);sku=SkuService(store,providers,autostart=False)
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            patches=[patch.object(server,k,v) for k,v in {'STORE':store,'DATA':store.data,'MODELS':providers,'ARCHIVE':archive,'SKU':sku,'ANALYSIS':None,'WATCH':None,'MOUSE':None,'RAWLINK':None,'FILESEARCH':None}.items()]
            for p in patches:p.start()
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    page=browser.new_page(viewport={'width':1680,'height':1050},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!!window.FramekeepSidebar')
                    self.assertEqual(page.locator('#asset-scope,#asset-custom-scope').count(),0)
                    page.locator('#current-shooting-tab').click();page.locator('#shooting-manual-type').fill('棚拍');page.locator('#photo-settings-tab').click()
                    page.locator('#asset-sku').fill('DRAFT123');selected=page.evaluate('FramekeepAssets.selection()')
                    page.locator('#app-settings-open').click();self.assertTrue(page.locator('#app-settings-page').is_visible())
                    self.assertEqual(page.locator('#app-settings-page #assistant-settings,#app-settings-page #mouse-remote').count(),2)
                    self.assertEqual(page.locator('.app-header #assistant-settings,.app-header #mouse-remote').count(),0)
                    self.assertEqual(page.locator('.app-settings-nav button').all_text_contents(),['01AI 与模型','02鼠标遥控','03文件与备案','04SKU 自动区间'])
                    for section in ('models','remote','files','streams'):
                        page.locator(f'[data-settings-section="{section}"]').click()
                        self.assertEqual(page.locator(f'[data-settings-section="{section}"]').get_attribute('aria-current'),'location')
                    for control in ('asset-mode','asset-overwrite','asset-read','asset-start','asset-close-stream'):
                        self.assertEqual(page.locator('#app-settings-page #'+control).count(),1);self.assertEqual(page.locator('.right-panel #'+control).count(),0)
                    self.assertEqual(page.locator('#asset-mode').input_value(),'embedded');self.assertEqual(page.locator('#asset-mode option').count(),1)
                    page.locator('#asset-overwrite').check();page.locator('#app-settings-title').click();page.keyboard.press('5')
                    self.assertEqual(page.evaluate('Framekeep.persistedState().overrides'),{})
                    page.locator('#app-settings-close').click();self.assertEqual(page.locator('#asset-sku').input_value(),'DRAFT123');self.assertEqual(page.evaluate('FramekeepAssets.selection()'),selected)
                    aid=archive.ensure(sid,record['id'])
                    with archive.connect() as c:c.execute("UPDATE assets SET sku='OLD123',scope='历史类型' WHERE id=?",(aid,))
                    page.locator('#app-settings-open').click();page.locator('#asset-read').click()
                    page.wait_for_function('()=>document.getElementById("app-settings-message").textContent.includes("OLD123")')
                    self.assertEqual(page.evaluate('FramekeepSku.currentType()'),'棚拍')
                    page.locator('#app-settings-close').click()
                    with page.expect_response(lambda r:'/api/metadata/preview' in r.url) as response:page.locator('#asset-preview').click()
                    self.assertEqual(response.value.json()['scope'],'棚拍');page.wait_for_selector('#asset-preview-dialog[open]');page.locator('#asset-preview-dialog [data-close]').click()
                    page.locator('#sku-open').click();page.locator('#sku-file').set_input_files({'name':'plan.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':fixture_book()})
                    page.wait_for_function('()=>document.querySelectorAll("#sku-panel-scope option").length===3');page.locator('#sku-panel-scope').select_option('模特图')
                    page.wait_for_function('()=>FramekeepSku.currentType()==="模特图"');page.locator('#sku-close').click()
                    page.locator('#app-settings-open').click();page.locator('#settings-current-shooting').click()
                    self.assertTrue(page.locator('#current-shooting-panel').is_visible());self.assertTrue(page.locator('#shooting-manual-field').is_hidden())
                    page.locator('#sku-live-scope').select_option('细节图');page.wait_for_function('()=>FramekeepSku.currentType()==="细节图"');page.locator('#photo-settings-tab').click()
                    page.locator('#asset-sku').fill('NEW123')
                    with page.expect_response(lambda r:'/api/metadata/preview' in r.url) as response:page.locator('#asset-preview').click()
                    self.assertEqual(response.value.json()['scope'],'细节图');page.wait_for_selector('#asset-preview-dialog[open]')
                    sku.configure(sku.project()['id'],sid,'模特图')
                    page.wait_for_function('()=>!document.getElementById("asset-preview-dialog").open')
                    self.assertEqual(page.evaluate('FramekeepSku.currentType()'),'模特图')
                    def switch_during_preview(route):
                        response=route.fetch();sku.configure(sku.project()['id'],sid,'细节图')
                        page.wait_for_function('()=>FramekeepSku.currentType()==="细节图"');route.fulfill(response=response)
                    page.route('**/api/metadata/preview',switch_during_preview);page.locator('#asset-preview').click()
                    page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("请重新预览")')
                    self.assertFalse(page.locator('#asset-preview-dialog').evaluate('(e)=>e.open'));page.unroute('**/api/metadata/preview',switch_during_preview)
                    page.locator('#app-settings-open').click();page.locator('#asset-stream-sku').fill('CLOSE123');page.locator('#asset-start').click()
                    page.wait_for_function('()=>document.getElementById("app-settings-message").textContent.includes("已接续")')
                    self.assertTrue(archive.stream_state()['streams']);page.locator('#asset-close-stream').click()
                    page.wait_for_function('()=>document.getElementById("app-settings-message").textContent.includes("已封口")')
                    for theme in ('dark','light','pure-white','hardcore'):
                        page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                        for width in (1680,1024,375,320):
                            page.set_viewport_size({'width':width,'height':1050});self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                        page.set_viewport_size({'width':1680,'height':1050});out=ROOT/'build/workbench-settings';out.mkdir(exist_ok=True);page.screenshot(path=str(out/f'{theme}.png'))
                    page.locator('#app-settings-close').click();page.locator('#selection-tab').press('End');self.assertTrue(page.locator('#app-settings-page').is_visible());page.locator('#app-settings-open').press('ArrowLeft');self.assertTrue(page.locator('#analysis-dialog').is_visible())
                    page.locator('#app-settings-open').click();page.locator('#app-settings-close').click();self.assertTrue(page.locator('#analysis-dialog').is_visible())
                    self.assertEqual(page.locator('.workspace-tabs [role=tab]').count(),6)
                    self.assertEqual(hashlib.sha256(photo.read_bytes()).hexdigest(),digest)
                    self.assertFalse(errors,errors);browser.close()
            finally:
                http.shutdown();http.server_close();sku.close();archive.close()
                for p in reversed(patches):p.stop()
