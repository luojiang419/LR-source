"""真实 OCR 来源、SKU 双视图、缩略图标注及非破坏归档移出。"""
import json
from datetime import datetime
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
from sku_service import SkuService
from sku_library import browse,labels
import media_engine as media
import server

ROOT=Path(__file__).resolve().parents[1]


class SkuLibraryTests(unittest.TestCase):
    def test_confirmed_onsite_assignment_is_visible_without_ocr_and_wins_over_old_reading(self):
        self.sku.archive=self.archive
        preview=self.archive.preview(self.sid,[self.ids[0]],'REAL-SKU-100','细节图')
        self.archive.apply(preview['preview_id']);self.recognize(0,'OLD-WRONG-900',100)
        self.assertEqual(labels(self.sku,self.sid)['labels'],{self.ids[0]:'REAL-SKU-100'})
        result=browse(self.sku);self.assertEqual(result['total'],1);self.assertEqual(result['groups'][0]['sku'],'REAL-SKU-100')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos'
        self.photos.mkdir()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos);self.ids=[]
        for i,name in enumerate(('A/shot2.JPG','B/shot10.JPG','A/shot3.JPG','SKU-BOGUS999/canvas-20261021.JPG')):
            path=self.photos/name;path.parent.mkdir(exist_ok=True,parents=True)
            exif=Image.Exif();exif[274]=8;Image.new('RGB',(300,200),(50+i*30,90,120)).save(path,exif=exif)
            record=media.capture_record(self.photos,path,None,{'DateTimeOriginal':f'2026:10:21 10:00:0{i}'})
            self.store.append_live(self.sid,[record]);self.ids.append(record['id'])
        self.store.activate(self.sid);self.providers=Providers(self.store.data)
        self.sku=SkuService(self.store,self.providers,autostart=False);self.archive=AssetArchive(self.store,autostart=False)
        self.catalog=AssetCatalog(self.store.data);self.root=self.catalog.add_root(str(self.photos))['root_id']

    def tearDown(self):
        self.sku.close();self.archive.close();self.temp.cleanup()

    def recognize(self,index,code,created):
        codes=code if isinstance(code,list) else [code]
        with self.sku.connect() as c:
            c.execute('INSERT INTO parse_jobs(id,session_id,capture_id,status,created,result) VALUES (?,?,?,?,?,?)',(f'read-{index}-{created}',self.sid,self.ids[index],'completed',created,json.dumps({'codes':codes})))

    def test_only_ocr_evidence_is_listed_with_exact_groups_sorting_and_pagination(self):
        self.assertTrue(any(row['sku'] for row in self.catalog.items()['rows']))
        unknown=self.catalog.items()['rows'][0];self.catalog.assign(unknown['id'],'MANUAL-ONLY999')
        self.assertEqual(browse(self.sku)['groups'],[])
        self.recognize(0,'A-100',10);self.recognize(1,'A-1001',20);self.recognize(2,['B-200','B-201'],30)
        listing=browse(self.sku)
        self.assertEqual([r['created'] for r in listing['groups']],[10,20])
        self.assertEqual([entry['created'] for root in listing['roots'] for folder in root['folders'] for entry in folder['skus']],[10,20])
        self.assertEqual([r['sku'] for r in listing['groups']],['A-100','A-1001'])
        self.assertEqual(labels(self.sku,self.sid)['labels'],{self.ids[0]:'A-100',self.ids[1]:'A-1001'})
        self.assertEqual(labels(self.sku,'another-session')['labels'],{})
        self.assertEqual(browse(self.sku,order='added')['groups'][0]['sku'],'A-1001')
        exact=browse(self.sku,code='A-100');self.assertEqual(exact['total'],1);self.assertEqual(exact['rows'][0]['capture_id'],self.ids[0])
        self.assertEqual(browse(self.sku,search='BOGUS')['groups'],[])
        self.assertEqual(browse(self.sku,folder='B')['total'],1)
        first=browse(self.sku,limit=1);second=browse(self.sku,offset=1,limit=1)
        self.assertTrue(first['has_more']);self.assertFalse(second['has_more']);self.assertNotEqual(first['rows'][0]['id'],second['rows'][0]['id'])
        self.assertEqual({r['folder'] for r in listing['roots'][0]['folders']},{'A','B'})
        with self.assertRaises(ValueError):browse(self.sku,order='sql injection')

    def test_archive_removal_survives_scan_and_keeps_files_and_corrections(self):
        item=self.catalog.items(folder='A')['rows'][0];self.catalog.assign(item['id'],'MANUAL-100')
        with patch('asset_catalog.os.startfile',create=True) as opened:
            self.catalog.open_folder(self.root,'A');opened.assert_called_once_with(str(self.photos/'A'))
        with self.assertRaises(ValueError):self.catalog.open_folder(self.root,'../outside')
        self.catalog.remove(self.root,'A');self.assertEqual(self.catalog.items(folder='A')['total'],0)
        self.catalog.scan(self.root);self.assertEqual(self.catalog.items(folder='A')['total'],0)
        self.assertTrue((self.photos/'A/shot2.JPG').is_file());self.assertEqual(self.catalog.item(item['id'])['sku'],'MANUAL-100')
        self.catalog.remove(self.root);self.assertEqual(self.catalog.tree()['roots'],[])
        self.assertEqual(self.catalog.state()['counts']['total'],0)
        self.catalog.add_root(str(self.photos));self.assertEqual(self.catalog.items()['total'],4)
        self.assertEqual(self.catalog.item(item['id'])['sku'],'MANUAL-100')

    def test_browser_labels_tabs_tree_search_sort_and_archive_context_actions(self):
        stamp=datetime(2026,10,22,10).timestamp()
        self.recognize(0,'A-100',stamp);self.recognize(1,'B-200',stamp+60)
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values={'STORE':self.store,'DATA':self.store.data,'SKU':self.sku,'ARCHIVE':self.archive,'ANALYSIS':None,'MODELS':self.providers,'CATALOG':self.catalog,'WATCH':None,'MOUSE':None,'RAWLINK':None,'FILESEARCH':None,'UPDATES':None}
        patches=[patch.object(server,key,value) for key,value in values.items()]
        for item in patches:item.start()
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                    page.wait_for_function('()=>document.querySelectorAll(".watch-sku-label").length===2')
                    self.assertEqual(page.locator('[data-watch-photo="'+self.ids[0]+'"] .watch-sku-label').inner_text(),'SKU:A-100')
                    self.assertEqual(page.locator('[data-watch-photo="'+self.ids[1]+'"] .watch-sku-label').inner_text(),'SKU:B-200')
                    out=ROOT/'build/sku-library';out.mkdir(exist_ok=True,parents=True);page.screenshot(path=str(out/'selection.png'))
                    page.locator('#sku-database-open').click();page.wait_for_selector('#sku-database-list [data-db-sku]')
                    self.assertEqual(page.locator('#sku-database-list [data-db-sku]').count(),2)
                    self.assertEqual(page.locator('#sku-database-list .sku-added-date').count(),2)
                    self.assertEqual(page.locator('#sku-database-list .sku-added-date').first.inner_text(),'入库 2026-10-22')
                    self.assertEqual(page.locator('.sku-database-sidebar #sku-database-search').count(),1)
                    page.locator('#sku-database-sort').select_option('added')
                    page.wait_for_function('()=>document.querySelector("#sku-database-list button").textContent.includes("B-200")')
                    page.locator('#sku-database-list [data-db-sku="A100"]').click()
                    page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===1')
                    page.wait_for_function('()=>[...document.querySelectorAll("#sku-database-grid img")].every(img=>img.naturalHeight>0)')
                    page.screenshot(path=str(out/'flat.png'))
                    page.locator('#sku-view-tree').click();page.wait_for_selector('#sku-database-tree [data-db-root]')
                    self.assertEqual(page.locator('#sku-database-tree .catalog-root>summary>span').inner_text(),'photos')
                    self.assertEqual(page.locator('#sku-database-tree .sku-added-date').count(),2)
                    page.locator('#sku-database-tree summary[data-db-folder="B"]').click()
                    page.wait_for_function('()=>document.getElementById("sku-database-title").textContent.startsWith("B ·")')
                    self.assertEqual(page.locator('#sku-database-grid .catalog-card').count(),1)
                    page.wait_for_function('()=>[...document.querySelectorAll("#sku-database-grid img")].every(img=>img.naturalHeight>0)')
                    page.screenshot(path=str(out/'tree.png'))
                    for theme in ('dark','light','pure-white','hardcore'):
                        page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                        for width in (1680,1024,375):
                            page.set_viewport_size({'width':width,'height':900});self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'),(theme,width))
                    page.set_viewport_size({'width':1680,'height':1050})
                    page.locator('#sku-database-search').fill('BOGUS')
                    page.wait_for_function('()=>document.querySelectorAll("#sku-database-tree [data-db-root]").length===0')
                    page.locator('#asset-archive-open').click();page.wait_for_selector('#catalog-tree [data-root]')
                    target=page.locator('#catalog-tree summary[data-folder="A"]');target.click(button='right')
                    with patch('asset_catalog.os.startfile',create=True) as opened:
                        with page.expect_response('**/api/catalog/open'):page.locator('[data-catalog-action="open"]').click()
                        opened.assert_called_once_with(str(self.photos/'A'))
                    target.click(button='right')
                    page.wait_for_function('()=>!document.getElementById("catalog-context-menu").hidden && Number(getComputedStyle(document.getElementById("catalog-context-menu")).opacity)===1')
                    page.screenshot(path=str(out/'context.png'))
                    page.locator('[data-catalog-action="remove"]').click()
                    page.wait_for_function('()=>!document.querySelector("#catalog-tree summary[data-folder=A]")')
                    page.locator('#catalog-scan').click()
                    page.wait_for_function('()=>!document.getElementById("catalog-scan").disabled')
                    self.assertEqual(page.locator('#catalog-tree summary[data-folder="A"]').count(),0)
                    self.assertTrue((self.photos/'A/shot2.JPG').exists());self.assertFalse(errors,errors)
                finally:browser.close()
        finally:
            http.shutdown();http.server_close()
            for item in reversed(patches):item.stop()
