"""真实工作台页面验证出入库导航、四主题与双图预览。"""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from catalog_store import Store
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from sku_service import SkuService
from model_providers import Providers
from inventory_service import InventoryService
from test_inventory import record
import server

ROOT=Path(__file__).resolve().parents[1]
class InventoryBrowserTests(unittest.TestCase):
    def test_navigation_themes_pictures_and_empty_filter(self):
        out=ROOT/'build/inventory-verification';out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'data',ROOT);providers=Providers(store.data);sku=SkuService(store,providers,autostart=False);archive=AssetArchive(store,autostart=False);catalog=AssetCatalog(store.data);inventory=InventoryService(store.data)
            inventory.usb=lambda body=None:{'phase':'idle','auto':False,'devices':[],'message':'测试未连接手机'}
            inventory.receive(record());inventory.receive(record('out','LC7877529-P105'))
            values=dict(STORE=store,DATA=store.data,WATCH=None,MODELS=providers,MOUSE=None,SKU=sku,ARCHIVE=archive,CATALOG=catalog,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,INVENTORY=inventory)
            patches=[patch.object(server,k,v) for k,v in values.items()]
            for p in patches:p.start()
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    page=browser.new_page(viewport={'width':1440,'height':960},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{http.server_port}');page.wait_for_function('()=>window.FramekeepBoot?.loading===false')
                    tabs=page.locator('.workspace-bottom-bar [role=tab]').evaluate_all('(nodes)=>nodes.map(n=>n.id)');self.assertEqual(tabs[tabs.index('asset-archive-open')+1],'inventory-open')
                    page.locator('#inventory-open').click();page.wait_for_function('()=>document.querySelectorAll("#inv-rows tr").length===2');page.wait_for_function('()=>[...document.querySelectorAll("#inv-rows img")].every(i=>i.naturalWidth>0)')
                    self.assertEqual(page.locator('#inv-export').inner_text(),'导出库存报表');self.assertEqual(page.locator('.app-header #inv-in').count(),1);self.assertEqual(page.locator('#inv-in').inner_text(),'1');self.assertEqual(page.locator('#inv-out').inner_text(),'1')
                    for theme in ['dark','light','pure-white','hardcore']:
                        page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                        for width,height in [(1440,960),(1024,700),(390,844)]:
                            page.set_viewport_size({'width':width,'height':height});self.assertTrue(page.locator('#inventory-page').is_visible());self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                            geometry=page.evaluate('()=>({bottom:document.querySelector(".workspace-bottom-bar").getBoundingClientRect().bottom,panel:document.querySelector("#inventory-page").getBoundingClientRect().bottom,nav:document.querySelector(".workspace-bottom-bar").getBoundingClientRect().top})');self.assertAlmostEqual(geometry['bottom'],height,delta=1);self.assertLessEqual(geometry['panel'],geometry['nav']+1)
                            if width>=1024:
                                self.assertLess(page.locator('.inv-heading').bounding_box()['height'],60);self.assertLess(abs(page.locator('.inv-heading h2').bounding_box()['y']-page.locator('#inv-export').bounding_box()['y']),15)
                            if width==1440:page.screenshot(path=str(out/f'desktop-{theme}.png'))
                    page.set_viewport_size({'width':1440,'height':960});page.locator('#inv-rows [data-inv-photo]').first.click();self.assertTrue(page.locator('#inv-photo').is_visible());page.locator('#inv-photo [data-inv-close]').click()
                    page.locator('#inv-rows [data-inv-photo]').first.click();page.locator('#inv-photo-plus').click();self.assertEqual(page.locator('#inv-photo-scale').inner_text(),'150%');page.locator('#inv-photo-fit').click();self.assertEqual(page.locator('#inv-photo-scale').inner_text(),'100%');page.keyboard.press('Escape')
                    page.locator('[data-inv-mode=out]').click();page.locator('#inv-start').fill('2026-11-05');page.locator('#inv-end').fill('2026-11-05');page.wait_for_function('()=>document.querySelector("#inv-total").textContent==="1 条"');page.locator('#inv-export').click()
                    page.evaluate("window.FramekeepDesktop={saveInventoryReport:async(name,query)=>{window.savedInventory={name,query};return false}}")
                    page.locator('#inv-export-filtered').click();page.wait_for_function('()=>window.savedInventory');self.assertTrue(page.locator('#inv-export-dialog').is_visible());self.assertIn('mode=out',page.evaluate('savedInventory.query'));self.assertIn('start=2026-11-05',page.evaluate('savedInventory.query'))
                    page.evaluate('delete window.FramekeepDesktop;window.showSaveFilePicker=undefined')
                    with page.expect_download() as download:page.locator('#inv-export-filtered').click()
                    destination=out/'desktop-filtered.xlsx';download.value.save_as(destination)
                    from openpyxl import load_workbook
                    workbook=load_workbook(destination);self.assertEqual(workbook.active.max_row,2);self.assertEqual(len(workbook.active._images),2)
                    page.locator('#inv-export').click()
                    with page.expect_download() as download:page.locator('#inv-export-all').click()
                    destination=out/'desktop-all.xlsx';download.value.save_as(destination);workbook=load_workbook(destination);self.assertEqual(workbook.active.max_row,3);self.assertEqual(len(workbook.active._images),4);page.locator('#inv-filter-clear').click()
                    page.locator('#inv-search').fill('MISSING');page.wait_for_selector('#inv-empty-connect');self.assertIn('0 条',page.locator('#inv-total').inner_text());self.assertEqual(errors,[]);page.locator('#selection-tab').click();self.assertFalse(page.locator('.inv-metrics').is_visible())
                    browser.close()
            finally:
                http.shutdown();http.server_close();inventory.close();catalog.close();sku.close();archive.close()
                for p in reversed(patches):p.stop()

if __name__=='__main__':unittest.main()
