from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from model_providers import Providers

ROOT=Path(__file__).resolve().parents[1]


class ArchiveLocationsBrowserTests(unittest.TestCase):
    def test_drive_buttons_folder_state_and_manual_network_path(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos'
            for relative in ('v1.2/child','v1.2/other'):
                target=photos/relative;target.mkdir(parents=True);Image.new('RGB',(160,120),'teal').save(target/'one.jpg')
            store=Store(base/'data',ROOT);archive=AssetArchive(store,autostart=False);catalog=AssetCatalog(store.data)
            patches=[patch.object(server,k,v) for k,v in dict(STORE=store,DATA=store.data,ARCHIVE=archive,CATALOG=catalog,MODELS=Providers(store.data),SKU=None,ANALYSIS=None,MOUSE=None,WATCH=None,RAWLINK=None,FILESEARCH=None,UPDATES=None).items()]
            for item in patches:item.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':900},reduced_motion='reduce');errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle');page.locator('#asset-archive-open').click()
                        page.locator('#catalog-add-root').click();page.locator('#catalog-location-path').fill(str(photos));page.locator('#catalog-location-submit').click()
                        page.wait_for_selector('#catalog-grid .catalog-card');self.assertFalse(page.locator('#catalog-location-dialog').evaluate('e=>e.open'))
                        button=page.locator('.catalog-drive-button').first;self.assertEqual(button.evaluate('e=>e.tagName'),'BUTTON');self.assertIn(':',button.inner_text())
                        button.press('Enter');self.assertEqual(button.get_attribute('aria-expanded'),'false');self.assertTrue(page.locator('.catalog-drive-folders').first.is_hidden())
                        button.press('Space');self.assertEqual(button.get_attribute('aria-expanded'),'true')
                        page.locator('.catalog-root>summary').first.click();page.wait_for_load_state('networkidle');self.assertFalse(page.locator('.catalog-root').first.evaluate('e=>e.open'))
                        page.locator('#catalog-search').fill('nothing');page.wait_for_load_state('networkidle');self.assertFalse(page.locator('.catalog-root').first.evaluate('e=>e.open'))
                        page.locator('#catalog-search').fill('');page.locator('.catalog-root>summary').first.click();page.wait_for_load_state('networkidle')
                        self.assertTrue(page.locator('[data-folder="v1.2"]').is_visible());self.assertEqual(page.locator('[data-folder="v1.2/child"]').count(),1)
                        long_name='用于检查长路径完整显示的文件夹名称'*3
                        roots=catalog.tree()['roots']+[
                            {'id':'mapped','path':'x:\\'+long_name,'folders':[{'folder':long_name+'/child','count':2}]},
                            {'id':'ip','path':r'\\192.168.0.181\新摄影\照片','folders':[{'folder':'nested/child','count':2}]},
                            {'id':'extended','path':r'\\?\UNC\192.168.0.181\新摄影\更多','folders':[]}]
                        page.route('**/api/catalog/tree',lambda route:route.fulfill(json={'roots':roots}))
                        page.locator('#selection-tab').click();page.locator('#asset-archive-open').click();page.wait_for_function('()=>document.querySelectorAll(".catalog-drive-button").length===3')
                        labels=page.locator('.catalog-drive-button span').all_text_contents()
                        self.assertIn('X:',labels);self.assertIn(r'\\192.168.0.181\新摄影',labels);self.assertNotIn('其他位置',labels)
                        self.assertEqual(page.locator('summary.catalog-drive').count(),0)
                        geometry=page.locator('#catalog-tree summary>span').evaluate_all('''nodes=>nodes.filter(e=>e.getBoundingClientRect().width>0).map(e=>({left:e.getBoundingClientRect().left,width:e.clientWidth,scroll:e.scrollWidth,text:e.textContent,height:e.getBoundingClientRect().height,whiteSpace:getComputedStyle(e).whiteSpace}))''')
                        self.assertLess(max(r['left'] for r in geometry)-min(r['left'] for r in geometry),1,geometry)
                        long_row=next(r for r in geometry if r['text']==long_name)
                        self.assertGreater(long_row['height'],30);self.assertLessEqual(long_row['scroll'],long_row['width']+1)
                        self.assertEqual(long_row['whiteSpace'],'normal')
                        out=ROOT/'build/archive-locations';out.mkdir(parents=True,exist_ok=True)
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                            for width,height in ((1440,900),(1000,680),(375,812)):
                                page.set_viewport_size({'width':width,'height':height});self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                            page.set_viewport_size({'width':1440,'height':900});page.screenshot(path=str(out/(theme+'.png')))
                        page.locator('#catalog-add-root').click();network=r'\\192.168.0.181\新摄影\照片'
                        page.locator('#catalog-location-path').fill(network)
                        sent=[]
                        def reject(route):
                            sent.append(route.request.post_data_json);route.fulfill(status=400,json={'error':'共享不可访问测试'})
                        page.route('**/api/catalog/add',reject);page.locator('#catalog-location-submit').click()
                        page.wait_for_function('()=>document.getElementById("catalog-location-error").textContent.includes("共享不可访问测试")')
                        self.assertEqual(sent[-1]['folder'],network);self.assertEqual(page.locator('#catalog-location-path').input_value(),network);self.assertFalse(page.locator('#catalog-location-submit').is_disabled())
                        page.screenshot(path=str(out/'network-input.png'));self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close();archive.close()
                for item in reversed(patches):item.stop()


if __name__=='__main__':unittest.main()
