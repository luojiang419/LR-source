"""启动快照跨进程、移除安全、慢查询下先显示及后台自动更新。"""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_catalog import AssetCatalog
import test_catalog_pages
import test_archive_refresh_browser
ROOT=test_archive_refresh_browser.ROOT
import server


class CatalogStartupTests(unittest.TestCase):
    setUp = test_catalog_pages.CatalogPagesTests.setUp
    tearDown = test_catalog_pages.CatalogPagesTests.tearDown

    def seed(self):
        self.catalog.tree()
        return self.catalog.items(quick=True)

    def test_fresh_process_uses_snapshot_even_when_strict_page_invalidates(self):
        original=self.seed()
        with self.catalog.connect() as c:c.execute("UPDATE items SET sku='NEW',normalized='NEW'")
        source='''from pathlib import Path
from unittest.mock import patch
from asset_catalog import AssetCatalog
import sys,json
c=AssetCatalog(Path(sys.argv[1]))
try:
 with patch.object(c,'items',side_effect=AssertionError('no live browse')),patch.object(c,'tree',side_effect=AssertionError('no full tree')),patch('os.scandir',side_effect=AssertionError('no NAS scan')):
  print(json.dumps(c.startup.read()))
finally:c.close()
'''
        run=subprocess.run([sys.executable,'-X','utf8','-c',source,str(self.catalog.data)],cwd=Path(__file__).parent,capture_output=True,text=True,encoding='utf8',timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
        cached=json.loads(run.stdout)
        self.assertEqual(cached['items'],original)
        self.assertEqual(cached['tree']['roots'][0]['folders'][0]['count'],1)
        self.assertEqual(self.catalog.items(quick=True)['rows'][0]['sku'],'NEW')

    def test_removed_roots_and_exclusions_are_not_resurrected(self):
        self.seed()
        with self.catalog.connect() as c:c.execute("INSERT INTO exclusions VALUES(?, '')",(self.root,))
        result=self.catalog.startup.read()
        self.assertFalse(result['items']['rows']);self.assertFalse(result['tree']['roots'][0]['folders'])
        self.catalog.remove(self.root)
        result=self.catalog.startup.read()
        self.assertFalse(result['tree']['roots']);self.assertFalse(result['items']['rows'])

    def test_corrupt_foreign_and_bounded_snapshots(self):
        self.seed();startup=self.catalog.startup
        with self.catalog.connect() as c:c.execute("UPDATE items SET sku='invalidate-strict-page'")
        key=startup.page_key(frame=self.catalog.video.frame);path=startup._path(key)
        path.write_text('{broken',encoding='utf8')
        self.assertIsNone(startup.read()['items'])
        startup.remember(key,{'rows':[]})
        value=json.loads(path.read_text(encoding='utf8'));value['database']='different-library'
        path.write_text(json.dumps(value),encoding='utf8');self.assertIsNone(startup.read()['items'])
        for n in range(20):startup.remember(str(n),{'rows':[]})
        self.assertLessEqual(len(list(startup.folder.glob('[0-9a-f]'*64+'.json'))),12)

    def test_first_upgrade_reuses_existing_strict_page_without_new_startup_file(self):
        original=self.seed();startup=self.catalog.startup
        startup._path(startup.page_key(frame=self.catalog.video.frame)).unlink()
        with patch.object(self.catalog,'items',side_effect=AssertionError('no full browse')):
            self.assertEqual(startup.read()['items'],original)

    def test_boot_thumbnail_corruption_falls_back_and_is_repaired(self):
        from urllib.parse import urlsplit,parse_qs
        data=self.seed();row=data['rows'][0]
        cached=parse_qs(urlsplit(row['preview']).query)['cached'][0]
        name=self.catalog.startup.thumbnail_key(row['preview'])
        payload=self.catalog.preview(row['id'],420,read=True,cached=cached)
        self.catalog.startup.thumbnail(name,payload)
        self.assertIn(row['preview'],self.catalog.startup.boot()['thumbnails'])
        path=self.catalog.startup.folder/('thumb-'+name+'.json');value=json.loads(path.read_bytes());value['data']='AAAA'
        path.write_text(json.dumps(value),encoding='utf8')
        self.assertFalse(self.catalog.startup.boot()['thumbnails'])
        self.catalog.startup.thumbnail(name,payload)
        self.assertIn(row['preview'],self.catalog.startup.boot()['thumbnails'])


class ArchiveStartupBrowserTests(unittest.TestCase):
    setUp = test_archive_refresh_browser.ArchiveRefreshBrowserTests.setUp
    tearDown = test_archive_refresh_browser.ArchiveRefreshBrowserTests.tearDown

    def test_boot_images_render_without_waiting_for_any_archive_http(self):
        from urllib.request import urlopen
        self.catalog.tree();data=self.catalog.items(quick=True)
        for row in data['rows']:
            # 先保存尚无cached提示的页面，再通过真实HTTP预览建立副本。
            self.assertNotIn('cached=',row['preview'])
            with urlopen(self.url+row['preview']+'&size=420',timeout=5) as response:self.assertTrue(response.read())
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();held=[]
                page.route('**/api/catalog/**',lambda route:held.append(route))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                page.wait_for_function("()=>document.querySelectorAll('#catalog-tree summary[data-root]').length===2&&[...document.querySelectorAll('#catalog-grid img')].filter(i=>i.complete&&i.naturalWidth>0).length===2",timeout=2500)
                self.assertTrue(page.locator('#catalog-grid img').first.get_attribute('src').startswith('data:image/jpeg;base64,'))
                self.assertTrue(held,'真实刷新必须后台继续')
            finally:browser.close()

    def test_restart_shows_images_before_slow_queries_and_refreshes_changes(self):
        self.catalog.tree();self.catalog.items(quick=True)
        self.catalog.close();self.catalog=AssetCatalog(self.store.data);server.CATALOG=self.catalog
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            held=[];errors=[]
            try:
                page=browser.new_page(viewport={'width':1440,'height':950});page.on('pageerror',lambda e:errors.append(str(e)))
                page.route('**/api/catalog/items*',lambda route:held.append(route))
                page.route('**/api/catalog/tree*',lambda route:held.append(route))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                page.wait_for_function("()=>document.querySelectorAll('#catalog-tree summary[data-root]').length===2&&[...document.querySelectorAll('#catalog-grid img')].filter(i=>i.complete&&i.naturalWidth>0).length===2",timeout=2500)
                self.assertIn('上次浏览结果',page.locator('#catalog-summary').inner_text())
                self.assertTrue(held,'真实刷新应与快照展示并行')
                routes=held[:];held.clear()
                for route in routes:route.continue_()
                page.unroute('**/api/catalog/items*');page.unroute('**/api/catalog/tree*')
                page.wait_for_function("()=>!document.getElementById('catalog-summary').textContent.includes('上次浏览结果')")
                # 不切页、不点击刷新，真实索引变化自动显示与移除。
                photo=self.photos[0].parent/'new.jpg';Image.new('RGB',(120,80),'orange').save(photo)
                root=self.catalog.state()['roots'][0]['id'];self.catalog.scan(root)
                page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===3",timeout=8000)
                self.catalog.remove(root)
                page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===1&&document.querySelectorAll('#catalog-tree summary[data-root]').length===1",timeout=8000)
                out=ROOT/'build/archive-instant-cache';out.mkdir(exist_ok=True)
                page.screenshot(path=str(out/'browser-refresh.png'))
                self.assertFalse(errors,errors)
            finally:
                browser.close()

    def test_reload_restores_selected_directory_while_queries_fail(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                page.wait_for_selector('#catalog-tree summary[data-root]')
                chosen=page.locator('#catalog-tree summary[data-root]').first;root=chosen.get_attribute('data-root');chosen.click()
                page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===1")
                page.route('**/api/catalog/items*',lambda route:route.fulfill(status=503,json={'error':'暂时离线'}))
                page.route('**/api/catalog/tree*',lambda route:route.fulfill(status=503,json={'error':'暂时离线'}))
                page.reload(wait_until='domcontentloaded')
                page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===1",timeout=3000)
                self.assertEqual(page.evaluate('FramekeepArchiveSelection().root_id'),root)
                page.wait_for_function("()=>document.getElementById('catalog-summary').textContent.includes('读取失败')")
                self.assertEqual(page.locator('#catalog-tree summary[data-root]').count(),2)
                self.assertEqual(page.locator('#catalog-grid .catalog-card').count(),1)
            finally:browser.close()


if __name__=='__main__':unittest.main()
