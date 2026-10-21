import tempfile
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright

import server
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from model_providers import Providers
from sku_service import SkuService
import media_engine as media


ROOT = Path(__file__).resolve().parents[1]


class AssetCatalogBrowserTests(unittest.TestCase):
    def test_lr_style_archive_and_sku_database_use_referenced_images(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            photos = base / 'photos' / 'SKU-AW202610'
            photos.mkdir(parents=True)
            for name, color in (('look-01.jpg', (42, 110, 170)), ('detail-02.jpg', (140, 75, 60))):
                exif=Image.Exif();exif[274]=8
                Image.new('RGB', (180, 120), color).save(photos / name,exif=exif)
            (photos / 'look-01.CR3').write_bytes(b'paired-raw-fixture')
            store = Store(base / 'data', ROOT)
            sid=store.ensure_live_session(base/'photos')
            for path in photos.glob('*.jpg'):
                store.append_live(sid,[media.capture_record(base/'photos',path,None,{})])
            providers=Providers(store.data);sku=SkuService(store,providers,autostart=False)
            archive = AssetArchive(store, autostart=False)
            catalog = AssetCatalog(base / 'data')
            catalog.add_root(str((base / 'photos').parent))
            http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            http.daemon_threads = True
            patches = [patch.object(server, key, value) for key, value in {
                'STORE': store, 'ARCHIVE': archive, 'CATALOG': catalog, 'SKU': sku, 'WATCH': None,
                'MOUSE': None, 'MODELS': providers, 'ANALYSIS': None, 'RAWLINK': None,
                'FILESEARCH': None, 'UPDATES': None, 'DATA': store.data,
            }.items()]
            for item in patches:
                item.start()
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                    try:
                        page = browser.new_page(viewport={'width': 1680, 'height': 1050})
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}', wait_until='domcontentloaded')
                        page.locator('#asset-archive-open').click()
                        page.wait_for_function('()=>document.querySelectorAll("#catalog-grid .catalog-card").length===2')
                        self.assertEqual(page.locator('#catalog-grid .photo-format-badge').inner_text(), 'J/R')
                        page.wait_for_function('()=>[...document.querySelectorAll("#catalog-grid img")].every(img=>img.naturalHeight===180 && img.naturalWidth===120)')
                        self.assertTrue(all('?v=' in img.get_attribute('src') and '&size=420' in img.get_attribute('src') for img in page.locator('#catalog-grid img').all()))
                        self.assertFalse(page.locator('.statusbar').is_visible())
                        self.assertEqual(page.locator('.app-header h2:visible').inner_text(), '资产归档')
                        self.assertEqual(page.locator('#asset-archive-dialog h2:visible').count(), 0)
                        out=ROOT/'build/archive-width';out.mkdir(parents=True,exist_ok=True)
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            for width,height in ((1680,1050),(1000,680),(1920,1080)):
                                page.set_viewport_size({'width':width,'height':height})
                                box=page.evaluate("""()=>{const bounds=s=>document.querySelector(s).getBoundingClientRect();return {page:bounds('#asset-archive-dialog').width,right:bounds('.catalog-inspector').right,gridRight:bounds('.catalog-workspace').right,overflow:document.documentElement.scrollWidth>innerWidth}}""")
                                self.assertAlmostEqual(box['page'],width,delta=1,msg=(theme,width,box))
                                self.assertAlmostEqual(box['right'],width-21,delta=1,msg=(theme,width,box))
                                self.assertAlmostEqual(box['gridRight'],width-20,delta=1,msg=(theme,width,box))
                                self.assertFalse(box['overflow'],(theme,width,box))
                        page.set_viewport_size({'width':1680,'height':1050})
                        page.screenshot(path=str(out/'archive.png'))
                        page.locator('#catalog-grid .catalog-card').first.click()
                        page.wait_for_function('()=>!document.getElementById("catalog-inspector-form").hidden')
                        self.assertFalse(page.locator('#catalog-inspector-empty').is_visible())
                        gap=page.evaluate("()=>document.querySelector('#catalog-selected-preview').getBoundingClientRect().top-document.querySelector('.catalog-inspector>header').getBoundingClientRect().bottom")
                        self.assertLessEqual(gap, 14)
                        self.assertTrue(page.locator('#catalog-selected-preview').get_attribute('src').endswith('size=900'))
                        page.wait_for_function('()=>document.getElementById("catalog-selected-preview").naturalHeight===180 && document.getElementById("catalog-selected-preview").naturalWidth===120')
                        page.screenshot(path=str(out/'selected-compact.png'))
                        page.locator('#catalog-selected-sku').fill('FINAL-9001')
                        page.locator('#catalog-save-sku').click()
                        page.wait_for_function('()=>document.getElementById("catalog-scan-status").textContent.includes("FINAL-9001")')
                        name=page.locator('#catalog-selected-name').inner_text().split('/')[-1]
                        cid=next(r['id'] for r in store.records(sid) if Path(r['jpg']).name==name)
                        expected_count=next(i for i,r in enumerate(store.records(sid)) if r['id']==cid)+1
                        with sku.connect() as c:c.execute('INSERT INTO parse_jobs(id,session_id,capture_id,status,created,result) VALUES (?,?,?,?,?,?)',('fixture-ocr',sid,cid,'completed',1,json.dumps({'codes':['FINAL-9001']})))
                        page.locator('#sku-database-open').click()
                        self.assertEqual(page.locator('.app-header h2:visible').inner_text(), 'SKU 数据库')
                        page.wait_for_function('()=>document.querySelector("[data-db-sku]")')
                        page.locator('#sku-database-list [data-db-sku]').filter(has_text='FINAL-9001').click()
                        page.wait_for_function('(count)=>document.querySelectorAll("#sku-database-grid .catalog-card").length===count',arg=expected_count)
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            for width,height in ((1440,900),(1000,680),(1920,1080)):
                                page.set_viewport_size({'width':width,'height':height})
                                box=page.evaluate('''()=>({height:innerHeight,page:document.getElementById('sku-database-page').getBoundingClientRect().height,nav:document.querySelector('.workspace-bottom-bar').getBoundingClientRect().bottom,overflow:document.documentElement.scrollWidth>innerWidth})''')
                                self.assertAlmostEqual(box['nav'],height,delta=2,msg=(theme,width,box))
                                self.assertGreater(box['page'],height-200,(theme,width,box))
                                self.assertFalse(box['overflow'],(theme,width,box))
                        page.set_viewport_size({'width':1440,'height':900})
                        page.locator('#sku-database-search').fill('NO-SUCH-SKU')
                        page.wait_for_function('()=>document.getElementById("sku-database-list").textContent.includes("尚无 OCR 识别的 SKU")')
                        out=ROOT/'build/sku-database-layout';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'empty.png'))
                        page.locator('#sku-database-search').fill('FINAL-9001')
                        page.wait_for_selector('[data-db-sku]');page.locator('[data-db-sku]').first.click()
                        page.wait_for_selector('#sku-database-grid .catalog-card')
                        page.screenshot(path=str(out/'populated.png'))
                        page.locator('#asset-analysis-open').click()
                        self.assertEqual(page.locator('.app-header h2:visible').inner_text(), '解析任务')
                        self.assertEqual(page.locator('#analysis-dialog h2:visible').count(), 0)
                        page.screenshot(path=str(out/'analysis-compact.png'))
                        self.assertFalse(errors, errors)
                    finally:
                        browser.close()
            finally:
                http.shutdown()
                http.server_close()
                archive.close()
                sku.close()
                for item in reversed(patches):
                    item.stop()


if __name__ == '__main__':
    unittest.main()
