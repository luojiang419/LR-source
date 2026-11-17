"""归档刷新不能依赖拍摄会话、素材页或进度接口全部成功。"""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
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
from preview_cache import PreviewWarmer,cache_for
from catalog_search_requests import SearchCancelled

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

    def test_large_tree_creates_children_on_expand_and_restores_search_state(self):
        roots=self.catalog.tree()['roots'];root=roots[0]['id']
        roots[0]['folders']=[{'folder':f'组{group:03}/子目录{child:03}/深层','count':1}
                             for group in range(80) for child in range(100)]
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':950});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.route('**/api/catalog/tree*',lambda route:route.fulfill(json={'roots':roots}))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                self.roots_visible(page)
                self.assertLess(page.locator('#catalog-tree [data-catalog-node]').count(),100)
                self.assertEqual(page.locator('summary[data-folder="组000/子目录000"]').count(),0)
                group=page.locator('summary[data-folder="组000"]');group.press('Enter')
                child=page.locator('summary[data-folder="组000/子目录000"]');child.wait_for(state='visible')
                self.assertEqual(page.locator('summary[data-folder="组000/子目录000/深层"]').count(),0)
                child.press('Enter');page.wait_for_selector('summary[data-folder="组000/子目录000/深层"]')
                self.assertEqual(child.evaluate('(n)=>n.parentElement.open'),True)
                # 关闭父节点后切换搜索并清空，折叠状态保留，未展开分支不创建。
                group.press('Enter');page.wait_for_function("()=>!document.querySelector('summary[data-folder=\"组000\"]').parentElement.open")
                page.locator('#catalog-search').fill('不存在的文件')
                page.wait_for_function("()=>document.getElementById('catalog-grid').textContent.includes('没有符合')")
                page.locator('#catalog-search').fill('');self.roots_visible(page)
                group=page.locator('summary[data-folder="组000"]')
                page.wait_for_function("()=>!!document.querySelector('summary[data-folder=\"组000\"]')")
                self.assertFalse(group.evaluate('(n)=>n.parentElement.open'))
                self.assertEqual(page.locator('summary[data-folder="组001/子目录000"]').count(),0)
                group.press('Enter');page.wait_for_selector('summary[data-folder="组000/子目录000/深层"]')
                page.locator('#selection-tab').click();page.locator('#asset-archive-open').click();self.roots_visible(page)
                self.assertTrue(page.locator('summary[data-folder="组000"]').evaluate('(n)=>n.parentElement.open'))
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_leaving_archive_interrupts_slow_tree_sql(self):
        import asset_catalog
        entered=threading.Event();finished=threading.Event();cancelled=threading.Event()
        ranked=asset_catalog._ranked_catalog;tree=self.catalog.tree
        def slow_ranked(columns):
            if columns!='root,folder,media_type':return ranked(columns)
            entered.set()
            return "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT 'root' AS root,'/' AS folder,'image' AS media_type,1 AS representative FROM n"
        def tracked(*args,**kwargs):
            try:return tree(*args,**kwargs)
            except SearchCancelled:cancelled.set();raise
            finally:finished.set()
        with patch('asset_catalog._ranked_catalog',side_effect=slow_ranked),patch.object(self.catalog,'tree',side_effect=tracked),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded')
                page.locator('#asset-archive-open').click();self.assertTrue(entered.wait(3))
                self.assertFalse(finished.is_set())
                page.locator('#selection-tab').click()
                self.assertTrue(finished.wait(3),'切页后目录SQL仍继续运行')
                self.assertTrue(cancelled.is_set())
            finally:browser.close()

    def test_slow_archive_http_preview_does_not_pause_background_decode(self):
        worker=PreviewWarmer(cache_for(self.store.data/'previews'),'unused',catalog=self.catalog,autostart=False)
        self.catalog.on_previews=worker.enqueue;self.catalog.queue_all_previews()
        roots=[root['id'] for root in self.catalog.state()['roots']]
        item=self.catalog.items()['rows'][0]['id']
        entered=threading.Event();release=threading.Event();original=self.catalog.preview
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise RuntimeError('测试未释放慢 NAS 请求')
            return original(*args,**kwargs)
        def fetch():
            with urllib.request.urlopen(self.url+'/api/catalog/preview/'+item+'?size=420',timeout=8) as response:return response.read()
        try:
            with patch.object(server,'PREVIEWS',worker),patch.object(self.catalog,'preview',side_effect=slow),ThreadPoolExecutor(1) as pool:
                request=pool.submit(fetch)
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(worker.foreground_active,0)
                    self.assertTrue(worker.process_one(),'归档 HTTP 请求不能阻止后台领取任务')
                    state=worker.archive_state(roots)
                    self.assertGreater(state['ready'],0);self.assertFalse(state['paused'])
                    self.assertFalse(request.done(),'必须在慢请求仍未返回时推进')
                finally:release.set()
                self.assertTrue(request.result(5).startswith(b'\xff\xd8'))
        finally:release.set();worker.close()

    def test_quick_browse_returns_before_slow_shared_rating_lookup(self):
        entered=threading.Event();release=threading.Event()
        original=self.archive.ratings.decorate
        def slow(rows):
            entered.set()
            if not release.wait(5):raise RuntimeError('测试未释放评级读取')
            return original(rows)
        def request(path):
            with urllib.request.urlopen(self.url+path,timeout=5) as response:return json.load(response)
        try:
            with patch.object(self.archive.ratings,'decorate',side_effect=slow):
                started=time.monotonic();browse=request('/api/catalog/items?quick=1&limit=120')
                self.assertLess(time.monotonic()-started,1)
                self.assertTrue(browse['rows']);self.assertFalse(entered.is_set())
                with ThreadPoolExecutor(1) as pool:
                    future=pool.submit(request,'/api/catalog/ratings?id='+browse['rows'][0]['id'])
                    self.assertTrue(entered.wait(1));self.assertFalse(future.done())
                    release.set();ratings=future.result(5)
                self.assertIn(browse['rows'][0]['id'],ratings['ratings'])
        finally:release.set()

    def test_archive_cards_do_not_wait_for_shared_ratings(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True);held=[]
            try:
                page=browser.new_page();page.set_default_timeout(3000)
                page.route('**/api/catalog/ratings*',lambda route:held.append(route))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===2")
                self.assertTrue(held,'素材显示后才允许评级请求继续等待')
            finally:
                for route in held:route.abort('timedout')
                browser.close()

    def test_first_page_failure_recovers_without_another_click(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();attempts=[]
                def respond(route):
                    attempts.append(route.request.url)
                    if len(attempts)==1:
                        route.fulfill(status=503,json={'error':'首次读取暂不可用'})
                    else:
                        route.continue_()
                page.route('**/api/catalog/items*',respond)
                page.goto(self.url,wait_until='domcontentloaded')
                page.locator('#asset-archive-open').click()
                page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-card').length===2",timeout=8000)
                self.assertGreaterEqual(len(attempts),2)
                page.locator('#catalog-search').fill('不存在的文件')
                page.wait_for_function("()=>document.getElementById('catalog-grid').textContent.includes('没有符合')")
                page.locator('#selection-tab').click();page.locator('#asset-archive-open').click()
                page.wait_for_function("()=>document.getElementById('catalog-grid').textContent.includes('没有符合')")
            finally:browser.close()

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

    def test_statistics_timeout_is_distinct_from_decode_failure_and_clears_on_recovery(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page()
                page.add_init_script("""const nativeFetch=window.fetch;let fail=true;
                    window.fetch=(url,...args)=>{if(String(url)==='/api/catalog/state'&&fail){fail=false;return Promise.reject(new DOMException('signal timed out','TimeoutError'))}return nativeFetch(url,...args)};""")
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                page.wait_for_function("()=>document.getElementById('catalog-scan-status').textContent.includes('正在自动重试')")
                self.roots_visible(page)
                page.wait_for_function("()=>document.getElementById('catalog-scan-status').textContent==='缩略图与清晰预览均已缓存后，计为已解码。'")
                self.assertNotIn('signal timed out',page.locator('#catalog-scan-status').inner_text())
            finally:browser.close()


if __name__=='__main__':unittest.main()
