"""归档刷新不能依赖拍摄会话、素材页或进度接口全部成功。"""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
import server
import media_engine as media
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from model_providers import Providers
from sku_service import SkuService

ROOT=Path(__file__).resolve().parents[1]


class ArchiveRefreshBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.stack=ExitStack()
        self.store=Store(self.base/'data',ROOT);self.catalog=AssetCatalog(self.store.data)
        self.providers=Providers(self.store.data);self.archive=AssetArchive(self.store,autostart=False)
        self.sku=SkuService(self.store,self.providers,autostart=False);self.photos=[]
        for name in ('archive-one','archive-two'):
            folder=self.base/name;folder.mkdir();photo=folder/'photo.JPG';Image.new('RGB',(120,80),'teal').save(photo)
            self.catalog.add_root(str(folder));self.photos.append(photo)
        self.before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.photos}
        values=dict(STORE=self.store,CATALOG=self.catalog,ARCHIVE=self.archive,SKU=self.sku,MODELS=self.providers,DATA=self.store.data,
                    WATCH=None,MOUSE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=None)
        for key,value in values.items():self.stack.enter_context(patch.object(server,key,value))
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.http.daemon_threads=True
        threading.Thread(target=self.http.serve_forever,daemon=True).start();self.url=f'http://127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.stack.close();self.sku.close();self.archive.close();self.catalog.close();self.temp.cleanup()

    def roots_visible(self,page):
        page.wait_for_function("()=>[...document.querySelectorAll('#catalog-tree summary[data-root]')].filter(n=>n.dataset.folder==='').length===2",timeout=5000)
        self.assertIn('2 个归档位置',page.locator('#catalog-location').inner_text())
        self.assertEqual(len(self.catalog.state()['roots']),2)

    def test_blank_context_refresh_retains_roots_with_slow_reference_and_failed_session(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':950});removes=[];held=[];errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('request',lambda r:removes.append(r.url) if '/api/catalog/remove' in r.url else None)
                page.route('**/api/reference/config',lambda route:held.append(route))
                page.route('**/api/state',lambda route:route.fulfill(status=503,json={'error':'拍摄状态暂不可读'}))
                for iteration in range(3):
                    if iteration==0:page.goto(self.url,wait_until='domcontentloaded')
                    else:
                        page.locator('.catalog-heading:visible').click(button='right');page.reload(wait_until='domcontentloaded')
                    page.locator('#asset-archive-open').click();self.roots_visible(page)
                    while held:held.pop().abort('timedout')
                    page.wait_for_timeout(100)
                self.assertFalse(removes);self.assertFalse(errors,errors)
                self.assertEqual(self.before,{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.photos})
                out=ROOT/'build/archive-refresh';out.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(out/'slow-reference-refresh.png'))
            finally:browser.close()
        self.catalog.close();self.catalog=AssetCatalog(self.store.data);self.assertEqual(len(self.catalog.state()['roots']),2)

    def test_failed_items_and_statistics_preserve_tree_and_post_still_uses_write_token(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();errors=[];writes=[];page.on('pageerror',lambda e:errors.append(str(e)))
                for pattern in ('**/api/catalog/items*','**/api/catalog/state'):
                    page.route(pattern,lambda route:route.fulfill(status=503,json={'error':'临时读取失败'}))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click();self.roots_visible(page)
                page.wait_for_function('()=>document.getElementById("catalog-summary").textContent.includes("读取失败")')
                page.reload(wait_until='domcontentloaded');page.locator('#asset-archive-open').click();self.roots_visible(page)
                page.on('request',lambda r:writes.append(r.headers.get('x-framekeep-token')) if r.method=='POST' and '/api/catalog/scan' in r.url else None)
                root=self.catalog.state()['roots'][0]['id']
                page.evaluate('(root_id)=>FramekeepIO.api("/api/catalog/scan",{root_id})',root)
                self.assertTrue(writes);self.assertEqual(writes[-1],server.TOKEN);self.assertFalse(errors,errors)
            finally:browser.close()

    def test_hidden_selection_preview_requests_stop_and_returning_restores_images(self):
        folder=self.base/'selection';folder.mkdir();photo=folder/'capture.JPG';Image.new('RGB',(160,120),'blue').save(photo)
        sid=self.store.ensure_live_session(folder);record=media.capture_record(folder,photo,None,{})
        self.store.append_live(sid,[record]);self.store.activate(sid)
        with self.store.connect() as connection:connection.execute('UPDATE sessions SET state=? WHERE id=?',(json.dumps({'view':'single','selectedId':record['id']}),sid))
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded')
                page.wait_for_function('()=>document.querySelector("#photos img")?.naturalWidth>0')
                original=page.locator('#photos img').get_attribute('src')
                page.locator('#asset-archive-open').click();self.roots_visible(page)
                page.wait_for_function("()=>![...document.querySelectorAll('#selection-page img[src]')].some(img=>img.getAttribute('src').includes('/api/preview/'))")
                page.evaluate("url=>{const img=document.createElement('img');img.id='late-preview-fixture';img.src=url;document.getElementById('selection-page').append(img)}",original)
                page.wait_for_function("()=>!document.getElementById('late-preview-fixture').hasAttribute('src')")
                page.locator('#selection-tab').click();page.wait_for_function('()=>document.querySelector("#photos img")?.naturalWidth>0')
                self.assertEqual(page.locator('#photos img').get_attribute('src'),original)
                self.assertEqual(page.locator('#late-preview-fixture').get_attribute('src'),original)
            finally:browser.close()


if __name__=='__main__':unittest.main()
