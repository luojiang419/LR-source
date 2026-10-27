"""真实 HTTP 与浏览器操作检验检索、归档入口和快捷弹窗。"""
import base64
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright

import server
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from asset_intelligence import AssetIntelligence
from catalog_store import Store
from model_providers import Providers
from sku_service import SkuService
from test_asset_intelligence import result
from retrieval_store import save_result
from visual_features import APPEARANCE, appearance

ROOT=Path(__file__).resolve().parents[1]


class RetrievalBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.stack=ExitStack()
        self.store=Store(self.base/'data',ROOT);self.catalog=AssetCatalog(self.store.data)
        self.providers=Providers(self.store.data);self.archive=AssetArchive(self.store,autostart=False)
        self.sku=SkuService(self.store,self.providers,autostart=False)
        self.service=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=False)
        folder=self.base/'LC1234';folder.mkdir()
        for name,color in [('衬衫正面.jpg','red'),('衬衫背面.jpg','red'),('蓝色衬衫.jpg','blue')]:Image.new('RGB',(220,300),color).save(folder/name)
        self.catalog.add_root(str(folder));self.rows=self.catalog.items()['rows']
        for row in self.rows:
            color='蓝色' if '蓝' in row['name'] else '红色';value=result(color)
            payload=Path(row['path']).read_bytes()
            with self.catalog.connect() as c:save_result(c,row['id'],row['fingerprint'],'test','fixture',[{'seconds':0,'result':value}],[(0,APPEARANCE,appearance(payload))])
            self.catalog.assign(row['id'],'LC1234' if color=='红色' else 'LC5678')
        values=dict(STORE=self.store,CATALOG=self.catalog,ARCHIVE=self.archive,SKU=self.sku,MODELS=self.providers,
                    INTELLIGENCE=self.service,LANWORKSPACE=None,DATA=self.store.data,WATCH=None,MOUSE=None,ANALYSIS=None,
                    RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=None)
        for name,value in values.items():self.stack.enter_context(patch.object(server,name,value))
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.http.daemon_threads=True
        threading.Thread(target=self.http.serve_forever,daemon=True).start();self.url=f'http://127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.stack.close();self.service.close()
        self.sku.close();self.archive.close();self.catalog.close();self.tmp.cleanup()

    def test_search_facets_preview_quick_dialog_and_archive_reference(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1580,'height':1000});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click()
                panel=page.locator('#asset-retrieval-page');query=panel.locator('.retrieval-query')
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===3')
                tabs=page.locator('.workspace-tabs [role=tab]').evaluate_all('(nodes)=>nodes.map(n=>n.id)')
                self.assertLess(tabs.index('asset-retrieval-open'),tabs.index('sku-open'))
                query.fill('LC1234');query.press('Enter');page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===2')
                query.fill('');query.press('Enter');page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===3')
                panel.locator('.retrieval-facet-key').select_option('color')
                panel.locator('.retrieval-filter-chips [data-value="蓝色"]').click()
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===1')
                panel.locator('[data-action="clear-facets"]').click()
                query.fill('蓝色');query.press('Enter');page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===1')
                panel.locator('.retrieval-card').click();page.wait_for_function('()=>document.querySelector("#asset-retrieval-page .retrieval-attributes").textContent.includes("主体颜色")')
                self.assertIn('蓝色',panel.locator('.retrieval-detail').inner_text())
                # 手动搜索应取消输入防抖，不能在选中素材后再次清空详情。
                page.wait_for_timeout(350)
                self.assertTrue(panel.locator('[data-detail="similar"]').is_visible())
                panel.locator('[data-detail="preview"]').click();page.wait_for_selector('#asset-image-viewer[open]')
                page.locator('#asset-viewer-close').click()
                page.keyboard.press('Control+Shift+Space');page.wait_for_selector('#quick-search-dialog[open]')
                quick=page.locator('#quick-search-dialog');quick.locator('.retrieval-query').fill('LC1234');quick.locator('[data-action="search"]').click()
                page.wait_for_function('()=>document.querySelectorAll("#quick-search-dialog .retrieval-card").length===2')
                quick.locator('.retrieval-query').press('ArrowDown');self.assertEqual(quick.locator('[aria-selected=true]').count(),1)
                page.keyboard.press('Escape');quick.wait_for(state='hidden',timeout=3000);self.assertFalse(quick.is_visible())
                page.locator('[data-workspace-target="asset-archive-open"]').click();page.wait_for_selector('#catalog-grid .catalog-card')
                page.locator('#catalog-grid .catalog-card').first.click();page.locator('#catalog-image-search').click();panel.locator('.reference-threshold').press('Home')
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===3&&document.querySelector("#asset-retrieval-page .retrieval-result-status").textContent.includes("布局与配色")')
                self.assertIn('布局与配色',panel.locator('.retrieval-result-status').inner_text())
                panel.locator('[data-action="clear-image"]').click();query.fill('红色');query.press('Enter')
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===2')
                out=ROOT/'build/multimodal-search';out.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(out/'retrieval-workspace.png'))
                page.locator('.retrieval-actions-menu>summary').click();page.locator('[data-workspace-heading="retrieval"] [data-action="settings"]').click();page.wait_for_selector('#retrieval-settings[open]')
                self.assertIn('同款',page.locator('#retrieval-settings').inner_text())
                page.locator('[data-config="custom_dimensions"]').fill('电商卖点\n搭配建议')
                page.locator('[data-config="save"]').click();page.wait_for_function('()=>document.querySelector("#retrieval-settings .retrieval-message").textContent.includes("已保存")')
                self.assertEqual(self.service.config['custom_dimensions'],['电商卖点','搭配建议'])
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_desktop_web_layout_filters_details_navigation_and_resizing(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1366,'height':886});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click()
                panel=page.locator('#asset-retrieval-page');query=panel.locator('.retrieval-query')
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===3')
                self.assertFalse(page.locator('.workspace-bottom-bar').is_visible())
                for width in (1366,850,650):
                    page.set_viewport_size({'width':width,'height':886})
                    toolbar=panel.locator('.retrieval-searchbar').bounding_box()
                    for selector in ('.retrieval-root','.retrieval-media','.retrieval-mode'):
                        box=panel.locator(selector).bounding_box()
                        self.assertIsNotNone(box);self.assertGreaterEqual(box['y'],toolbar['y'])
                        self.assertLessEqual(box['y']+box['height'],toolbar['y']+toolbar['height']+1)
                    reference=panel.locator('.reference-dropzone').bounding_box()
                    results=panel.locator('.retrieval-results-area').bounding_box()
                    self.assertLess(reference['x']+reference['width'],results['x'])
                    self.assertFalse(panel.locator('.retrieval-detail').is_visible())
                    self.assertTrue(page.locator('[data-workspace-target="asset-archive-open"]').is_visible())
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),width)
                for theme in ('light','pure-white','hardcore','dark'):
                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                    self.assertTrue(panel.locator('.retrieval-root').is_visible())
                    self.assertFalse(panel.locator('.retrieval-detail').is_visible())
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),650)
                page.set_viewport_size({'width':1366,'height':886})
                query.fill('LC5678')
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===1')
                panel.locator('.retrieval-media').select_option('video')
                page.wait_for_function('()=>document.querySelector("#asset-retrieval-page .retrieval-grid").textContent.includes("没有找到")')
                panel.locator('.retrieval-media').select_option('image')
                page.wait_for_selector('#asset-retrieval-page .retrieval-card')
                root=self.catalog.tree()['roots'][0]['id']
                with page.expect_response(lambda response:'/api/intelligence/search?' in response.url and 'root='+root in response.url):
                    panel.locator('.retrieval-root').select_option(root)
                panel.locator('.retrieval-card').click()
                page.wait_for_function('()=>document.querySelector("#asset-retrieval-page .retrieval-attributes").textContent.includes("主体颜色")')
                self.assertTrue(panel.locator('[data-detail="assign"]').is_visible())
                panel.locator('[data-detail="close"]').click();self.assertFalse(panel.locator('.retrieval-detail').is_visible())
                query.press('ArrowDown');page.keyboard.press('Escape')
                self.assertFalse(panel.locator('.retrieval-detail').is_visible())
                out=ROOT/'build/desktop-search-layout';out.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(out/'desktop-search.png'))
                page.locator('.retrieval-actions-menu>summary').click()
                page.locator('[data-workspace-heading="retrieval"] [data-action="quick"]').click()
                page.wait_for_selector('#quick-search-dialog[open]');page.keyboard.press('Escape')
                page.locator('[data-workspace-target="asset-archive-open"]').click()
                page.wait_for_function('()=>!document.querySelector("#asset-archive-dialog").classList.contains("hidden")')
                self.assertTrue(page.locator('.workspace-bottom-bar').is_visible())
                page.locator('#asset-retrieval-open').click()
                page.locator('[data-workspace-target="sku-open"]').click()
                page.wait_for_function('()=>!document.querySelector(".workbench").classList.contains("workspace-retrieval-active")')
                self.assertTrue(page.locator('.workspace-bottom-bar').is_visible())
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_standalone_quick_page_upload_and_request_token(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':900});errors=[];writes=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda req:writes.append(req.headers.get('x-framekeep-token')) if req.method=='POST' else None)
                page.goto(self.url+'/quick-search.html',wait_until='domcontentloaded')
                page.wait_for_selector('.retrieval-card');page.locator('.retrieval-upload').set_input_files(str(self.rows[0]['path']));page.wait_for_function('()=>!document.querySelector(".reference-threshold").disabled');page.locator('.reference-threshold').press('Home')
                page.wait_for_function('()=>document.querySelector(".retrieval-result-status").textContent.includes("布局与配色相似")')
                self.assertEqual(writes[-1],server.TOKEN)
                page.locator('.retrieval-card').first.click();page.wait_for_selector('[data-detail="folder"]')
                out=ROOT/'build/multimodal-search';out.mkdir(parents=True,exist_ok=True);page.wait_for_function('()=>[...document.querySelectorAll(".retrieval-card img")].every(img=>img.naturalWidth>0)');page.wait_for_function('()=>document.querySelector(".retrieval-attributes").textContent.includes("主体颜色")');page.screenshot(path=str(out/'quick-search.png'))
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_fixed_reference_area_auto_search_threshold_drop_and_paste(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':950});requests=[];errors=[]
                page.on('request',lambda request:requests.append(request) if '/api/intelligence/image' in request.url and request.method=='POST' else None)
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click()
                page.wait_for_selector('#asset-retrieval-page .retrieval-card');panel=page.locator('#asset-retrieval-page');reference=panel.locator('.reference-search')
                self.assertTrue(reference.locator('.reference-dropzone').is_visible())
                red=next(row for row in self.rows if '正面' in row['name']);blue=next(row for row in self.rows if '蓝' in row['name'])
                panel.locator('.retrieval-upload').set_input_files(red['path'])
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===2')
                self.assertEqual(len(requests),1);self.assertIn('50%',reference.locator('output').inner_text())
                reference.locator('.reference-threshold').press('Home')
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===3')
                reference.locator('.reference-threshold').press('End')
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===2')
                self.assertEqual(len(requests),1)
                with page.expect_response('**/api/intelligence/image'):reference.locator('[data-reference="generate"]').click()
                self.assertEqual(len(requests),2)
                encoded=base64.b64encode(Path(blue['path']).read_bytes()).decode()
                page.evaluate('''encoded=>{const data=new DataTransfer();data.items.add(new File([Uint8Array.from(atob(encoded),c=>c.charCodeAt(0))],'粘贴参考.jpg',{type:'image/jpeg'}));document.querySelector('#asset-retrieval-page .reference-dropzone').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}))}''',encoded)
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===1')
                self.assertEqual(len(requests),3)
                encoded=base64.b64encode(Path(red['path']).read_bytes()).decode()
                page.evaluate('''encoded=>{const data=new DataTransfer();data.items.add(new File([Uint8Array.from(atob(encoded),c=>c.charCodeAt(0))],'拖入参考.jpg',{type:'image/jpeg'}));document.querySelector('#asset-retrieval-page .reference-dropzone').dispatchEvent(new DragEvent('drop',{dataTransfer:data,bubbles:true,cancelable:true}))}''',encoded)
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===2')
                self.assertEqual(len(requests),4)
                reference.locator('[data-reference="clear"]').click();self.assertFalse(panel.locator('.retrieval-query').is_disabled())
                page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:undefined})")
                reference.locator('[data-reference="paste"]').click();self.assertIn('Ctrl+V',reference.locator('.reference-help').inner_text())
                self.assertFalse(errors,errors)
            finally:browser.close()


if __name__=='__main__':unittest.main()
