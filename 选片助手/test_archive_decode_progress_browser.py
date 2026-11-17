"""真实服务和真实预览队列：解码数量、剩余时间及归档右栏展示。"""
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
from catalog_store import Store
from model_providers import Providers
from preview_cache import PreviewWarmer,cache_for
from sku_service import SkuService


ROOT=Path(__file__).resolve().parents[1]


class ArchiveDecodeProgressBrowserTests(unittest.TestCase):
    def test_real_decodes_eta_discovery_failures_and_completed_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir()
            for number in range(6):Image.new('RGB',(640,480),(30*number,70,90)).save(photos/f'{number}.jpg')
            store=Store(base/'data',ROOT)
            providers=Providers(store.data);sku=SkuService(store,providers,autostart=False)
            archive=AssetArchive(store,autostart=False);catalog=AssetCatalog(store.data)
            worker=PreviewWarmer(cache_for(store.data/'previews'),'unused',catalog=catalog,autostart=False)
            catalog.on_previews=worker.enqueue
            root=catalog.add_root(str(photos))['root_id']
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            patches=[patch.object(server,key,value) for key,value in {
                'STORE':store,'ARCHIVE':archive,'CATALOG':catalog,'SKU':sku,'PREVIEWS':worker,
                'REFERENCES':None,'WATCH':None,'MOUSE':None,'MODELS':providers,'ANALYSIS':None,
                'RAWLINK':None,'FILESEARCH':None,'UPDATES':None,'DATA':store.data,
            }.items()]
            for item in patches:item.start()
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1000,'height':680});errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.locator('#asset-archive-open').click()
                        def decoded(ready):
                            page.wait_for_function('(n)=>document.getElementById("catalog-preview-status").textContent.includes(`当前已缓存 ${n} / 6 项`)',arg=ready)
                        decoded(0)
                        self.assertIn('正在估算',page.locator('#catalog-preview-eta').inner_text())
                        self.assertNotIn('已扫描',page.locator('#asset-archive-dialog .catalog-scan-panel').inner_text())
                        self.assertNotIn('目录估算',page.locator('#asset-archive-dialog .catalog-scan-panel').inner_text())
                        self.assertEqual(page.locator('#asset-archive-dialog .catalog-scan-panel progress').count(),1)
                        worker.process_one();decoded(1)
                        self.assertIn('正在估算',page.locator('#catalog-preview-eta').inner_text())
                        worker.process_one();decoded(2)
                        page.wait_for_function('()=>document.getElementById("catalog-preview-eta").textContent.includes("约")')
                        self.assertEqual(page.locator('#catalog-preview-bar').get_attribute('value'),'33')
                        out=ROOT/'build/archive-decode-progress';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'decoding.png'))
                        # 核对期间仍展示已完成数量，ETA 只描述当前队列。
                        worker.reconciling=True
                        page.wait_for_function('()=>document.getElementById("catalog-preview-status").textContent.includes("总量更新中")')
                        self.assertIn('当前队列预计剩余',page.locator('#catalog-preview-eta').inner_text())
                        worker.foreground_active=1
                        page.wait_for_function('()=>document.getElementById("catalog-preview-eta").textContent.includes("约")')
                        self.assertNotIn('前台操作',page.locator('#catalog-preview-eta').inner_text())
                        worker.foreground_active=0;worker.reconciling=False
                        # 停留页面时只刷新解码进度，不重新请求列表或替换卡片。
                        item_requests=[]
                        page.on('request',lambda request:item_requests.append(request.url) if '/api/catalog/items?' in request.url else None)
                        page.evaluate('window.savedCard=document.querySelector("#catalog-grid .catalog-card");window.savedImage=savedCard.querySelector("img")')
                        page.evaluate('window.gridMutations=0;window.gridObserver=new MutationObserver(records=>gridMutations+=records.filter(r=>r.type==="childList").length);gridObserver.observe(document.getElementById("catalog-grid"),{childList:true,subtree:true})')
                        page.wait_for_timeout(4500)
                        self.assertEqual(item_requests,[])
                        self.assertTrue(page.evaluate('savedCard===document.querySelector("#catalog-grid .catalog-card")&&savedImage===savedCard.querySelector("img")'))
                        self.assertEqual(page.evaluate('gridMutations'),0)
                        page.locator('#selection-tab').click()
                        with page.expect_response(lambda response:'/api/catalog/items?' in response.url):
                            page.locator('#asset-archive-open').click()
                        self.assertTrue(page.evaluate('savedCard===document.querySelector("#catalog-grid .catalog-card")&&savedImage===savedCard.querySelector("img")'))
                        # 后台资料变化在切回时呈现，照片源相同则继续用原图片节点。
                        item_id=page.evaluate('savedCard.dataset.catalogItem')
                        with catalog.connect() as c:c.execute('UPDATE items SET name=? WHERE id=?',('updated-name.jpg',item_id))
                        page.locator('#catalog-grid .catalog-card').first.click()
                        page.evaluate('document.getElementById("catalog-grid").style.maxHeight="80px";document.getElementById("catalog-grid").scrollTop=40;window.savedScroll=document.getElementById("catalog-grid").scrollTop')
                        page.locator('#selection-tab').click()
                        with page.expect_response(lambda response:'/api/catalog/items?' in response.url):
                            page.locator('#asset-archive-open').click()
                        page.wait_for_function('()=>document.querySelector("#catalog-grid .catalog-card-name").textContent==="updated-name.jpg"')
                        self.assertTrue(page.evaluate('savedImage===document.querySelector("#catalog-grid .catalog-card img")'))
                        self.assertTrue(page.locator('#catalog-grid .catalog-card').first.evaluate('e=>e.classList.contains("active")'))
                        self.assertEqual(page.evaluate('document.getElementById("catalog-grid").scrollTop'),page.evaluate('savedScroll'))
                        page.evaluate('gridObserver.disconnect();document.getElementById("catalog-grid").style.maxHeight=""')
                        # 用确定响应核对较长时间的格式，不把它当作实测耗时。
                        state=catalog.state();state['previews']=dict(worker.archive_state([root]),eta_seconds=3661)
                        page.route('**/api/catalog/state',lambda route:route.fulfill(json=state))
                        page.wait_for_function('()=>document.getElementById("catalog-preview-eta").textContent.includes("1 小时 2 分钟")')
                        page.unroute('**/api/catalog/state')
                        while worker.process_one():pass
                        decoded(6)
                        page.wait_for_function('()=>document.getElementById("catalog-preview-status").textContent.includes("全部完成")')
                        self.assertEqual(page.locator('#catalog-preview-percent').inner_text(),'100%')
                        self.assertEqual(page.locator('#catalog-preview-eta').inner_text(),'预计剩余：0 秒')
                        # 失败不能作为完成量或显示 100%。
                        state['previews'].update(ready=5,pending=0,failed=1,eta_seconds=0)
                        page.route('**/api/catalog/state',lambda route:route.fulfill(json=state))
                        page.wait_for_function('()=>document.getElementById("catalog-preview-status").textContent.includes("失败 1 项")')
                        self.assertEqual(page.locator('#catalog-preview-percent').inner_text(),'83%')
                        self.assertNotIn('0 秒',page.locator('#catalog-preview-eta').inner_text())
                        page.screenshot(path=str(out/'failed.png'))
                        page.unroute('**/api/catalog/state')
                        page.wait_for_function('()=>document.getElementById("catalog-preview-percent").textContent==="100%"')
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            geometry=page.locator('#asset-archive-dialog .catalog-scan-panel').evaluate('e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width,overflow:e.scrollWidth>e.clientWidth}}')
                            self.assertFalse(geometry['overflow'],(theme,geometry))
                            self.assertLessEqual(geometry['right'],1000)
                        page.screenshot(path=str(out/'completed.png'))
                        (out/'result.json').write_text(json.dumps({'ready':worker.archive_state([root]),'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf8')
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close();thread.join(timeout=3)
                archive.close();sku.close();worker.close();catalog.close()
                for item in reversed(patches):item.stop()


if __name__=='__main__':unittest.main()
