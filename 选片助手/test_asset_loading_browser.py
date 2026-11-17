"""真实浏览器慢请求：离开视口腾槽、陈旧回调和暂停后的失败恢复。"""
from io import BytesIO
from contextlib import suppress
from pathlib import Path
import time
import unittest

from PIL import Image
from playwright.sync_api import sync_playwright,Error

ROOT=Path(__file__).resolve().parents[1]


class AssetLoadingBrowserTests(unittest.TestCase):
    def test_hung_requests_time_out_and_allow_later_cached_images(self):
        payload=BytesIO();Image.new('RGB',(80,80),'teal').save(payload,format='JPEG')
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True);pending=[]
            try:
                page=browser.new_page();page.clock.install();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def respond(route):
                    if '/slow/' in route.request.url:pending.append(route)
                    else:route.fulfill(body=payload.getvalue(),content_type='image/jpeg')
                page.route('http://preview.test/**',respond)
                page.set_content('<div id="grid" style="height:600px;overflow:auto">'+''.join(
                    f'<div class="catalog-thumb"><img id="i{i}" style="width:80px;height:80px" data-preview-src="http://preview.test/{"slow" if i<3 else "cached"}/{i}"><span class="asset-loading-label"></span></div>' for i in range(6))+'</div>')
                page.add_script_tag(path=str(ROOT/'演示方案/asset-loading.js'))
                page.evaluate('FramekeepAssetLoading.watch(grid)')
                page.wait_for_function('i0.src&&i1.src&&i2.src')
                page.evaluate('window.oldLoad=i0.onload;window.oldError=i0.onerror')
                page.clock.run_for(15010)
                page.wait_for_function('[i3,i4,i5].every(img=>img.dataset.previewDone==="ready")')
                page.evaluate('oldLoad();oldError()')
                self.assertIsNone(page.evaluate('i0.dataset.previewDone||null'))
                page.clock.run_for(40000)
                page.wait_for_function('[i0,i1,i2].every(img=>img.dataset.previewDone==="error")')
                self.assertEqual(errors,[])
            finally:
                if 'page' in locals():
                    with suppress(Error):page.evaluate('window.FramekeepAssetLoading?.pause()')
                    for route in pending:
                        with suppress(Error):route.abort()
                    with suppress(Error):page.unroute_all(behavior='ignoreErrors')
                browser.close()

    def test_only_import_or_migration_retries_failed_images_not_inflight_images(self):
        source=(ROOT/'演示方案/asset-metadata.js').read_text(encoding='utf8')
        # 直接执行产品的状态绘制函数和事件监听器，省去业务服务与后台解码。
        state=source[source.index("  let storageStarting=false"):source.index('  async function cacheOperation')]
        listener=source[source.index("  document.addEventListener('framekeep:cache-imported'"):source.index("  document.querySelector('[data-settings-section=\"library\"]')")]
        payload=BytesIO();Image.new('RGB',(80,80),'teal').save(payload,format='JPEG')
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True);pending=[]
            try:
                page=browser.new_page();page.set_default_timeout(4000);requests=[];errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def handle(route):
                    requests.append(route.request.url)
                    if '/slow' in route.request.url:pending.append(route)
                    elif 'cache_revision=' in route.request.url:route.fulfill(body=payload.getvalue(),content_type='image/jpeg')
                    else:route.abort()
                page.route('http://preview.test/api/**',handle)
                ids=['cache-storage-current','preview-cache-limit','preview-cache-save','cache-storage-card','cache-storage-clean','cache-storage-status','cache-storage-progress']
                page.set_content(''.join(f'<div id="{key}"></div>' for key in ids)+
                    '<img id="slow" src="http://preview.test/api/preview/slow"><img id="failed" src="http://preview.test/api/preview/failed">'+
                    '<div id="grid" style="height:200px;overflow:auto"><div class="catalog-thumb"><img id="managed" style="width:80px;height:80px" data-preview-src="http://preview.test/api/catalog/preview/managed"><span class="asset-loading-label"></span></div></div>',wait_until='domcontentloaded')
                page.add_script_tag(path=str(ROOT/'演示方案/asset-loading.js'))
                page.add_script_tag(content='const $=id=>document.getElementById(id);let cacheSaving=false;'+state+listener+
                    ';window.events=0;document.addEventListener("framekeep:cache-imported",()=>events++);FramekeepAssetLoading.watch(grid);')
                page.wait_for_function('()=>failed.complete&&!failed.naturalWidth&&managed.dataset.previewDone==="error"')
                for action in ('maintenance','resize','export','prepare','clean_previous'):
                    page.evaluate('action=>paintCacheStorage({path:"cache",job:{id:action,action,status:"completed",message:"done"}})',action)
                self.assertEqual(page.evaluate('events'),0)
                page.evaluate('paintCacheStorage({path:"cache",job:{id:"import-1",action:"import",status:"completed",message:"done"}})')
                page.wait_for_function('()=>failed.naturalWidth>0&&managed.dataset.previewDone==="ready"')
                self.assertEqual(page.evaluate('events'),1)
                self.assertEqual(len([url for url in requests if '/slow' in url]),1)
                self.assertFalse(page.evaluate('slow.complete'))
                page.evaluate('paintCacheStorage({path:"cache",job:{id:"import-1",action:"import",status:"completed",message:"done"}});paintCacheStorage({path:"cache",job:{id:"migrate-1",action:"migrate",status:"completed",message:"done"}})')
                self.assertEqual(page.evaluate('events'),2)
                self.assertEqual(len([url for url in requests if 'cache_revision=' in url]),2)
                self.assertEqual(errors,[])
            finally:
                if 'page' in locals():
                    with suppress(Error):page.evaluate('window.FramekeepAssetLoading?.pause()')
                    for route in pending:
                        with suppress(Error):route.abort()
                    with suppress(Error):page.unroute_all(behavior='ignoreErrors')
                browser.close()

    def test_offscreen_requests_release_slots_and_ignore_stale_callbacks(self):
        payload=BytesIO();Image.new('RGB',(80,80),'teal').save(payload,format='JPEG')
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.set_default_timeout(4000)
                pending=[];errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.route('http://preview.test/slow/**',lambda route:pending.append(route))
                page.set_content('<style>#grid{height:200px;width:200px;overflow:auto}.catalog-thumb{height:100px}img{height:80px;width:80px}</style><div id="grid">'+
                    ''.join(f'<div class="catalog-thumb"><img id="i{i}" data-preview-src="http://preview.test/slow/{i}.jpg"><span class="asset-loading-label"></span></div>' for i in range(12))+'</div>')
                page.add_script_tag(path=str(ROOT/'演示方案/asset-loading.js'))
                page.evaluate('FramekeepAssetLoading.watch(document.querySelector("#grid"))')
                def wait_requests(count):
                    until=time.monotonic()+3
                    while len(pending)<count and time.monotonic()<until:page.wait_for_timeout(10)
                    self.assertGreaterEqual(len(pending),count)
                wait_requests(3)
                page.evaluate('window.oldLoad=i0.onload;window.oldError=i0.onerror;grid.scrollTop=800')
                # 不释放前三个慢响应，新视口也必须开始加载。
                wait_requests(6)
                self.assertTrue(any(int(route.request.url.rsplit('/',1)[-1].split('.')[0])>=6 for route in pending[3:]))
                self.assertIsNone(page.locator('#i0').get_attribute('src'))
                page.evaluate('oldLoad();oldError()')
                self.assertIsNone(page.locator('#i0').get_attribute('data-preview-done'))
                self.assertIsNone(page.locator('#i0').get_attribute('data-preview-attempts'))
                self.assertLessEqual(page.locator('img[src]').count(),4)
                # 暂停时捕获旧回调，恢复同一元素后旧回调不能完成新请求。
                page.evaluate('window.target=[...document.querySelectorAll("img[src]")][0];window.cancelledLoad=target.onload;window.cancelledError=target.onerror;FramekeepAssetLoading.pause();FramekeepAssetLoading.resume();cancelledLoad();cancelledError()')
                self.assertIsNone(page.evaluate('target.dataset.previewDone||null'))
                self.assertIsNone(page.evaluate('target.dataset.previewAttempts||null'))
                page.wait_for_timeout(50)
                source=page.evaluate('target.src')
                routes=[route for route in pending if route.request.url==source]
                self.assertTrue(routes)
                # 模拟当前请求出错进入重试，再暂停；恢复不能卡在 previewRetrying。
                page.evaluate('target.onerror();FramekeepAssetLoading.pause();FramekeepAssetLoading.resume()')
                self.assertIsNone(page.evaluate('target.dataset.previewRetrying||null'))
                page.wait_for_timeout(50)
                routes=[route for route in pending if route.request.url==source]
                routes[-1].fulfill(body=payload.getvalue(),content_type='image/jpeg')
                page.wait_for_function('target.dataset.previewDone==="ready"')
                self.assertIsNone(page.evaluate('target.dataset.previewAttempts||null'))
                self.assertEqual(errors,[])
            finally:
                if 'page' in locals():
                    with suppress(Error):page.evaluate('window.FramekeepAssetLoading?.pause()')
                    for route in pending:
                        with suppress(Error):route.abort()
                    with suppress(Error):page.unroute_all(behavior='ignoreErrors')
                browser.close()


if __name__=='__main__':unittest.main()
