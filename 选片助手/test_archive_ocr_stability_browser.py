"""二星解析期间原位回显；后台变化不能移除已显示的缩略图。"""
from contextlib import ExitStack
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from playwright.sync_api import sync_playwright
import server
import test_archive_capture_service as fixtures


class ArchiveOcrStabilityTests(unittest.TestCase):
    setUp = fixtures.ArchiveCaptureTests.setUp
    tearDown = fixtures.ArchiveCaptureTests.tearDown
    client = fixtures.ArchiveCaptureTests.client
    item = fixtures.ArchiveCaptureTests.item
    drain = fixtures.ArchiveCaptureTests.drain

    def test_ocr_refresh_keeps_images_connected_and_preserves_draft(self):
        with ExitStack() as stack:
            values = dict(STORE=self.store, CATALOG=self.catalog, ARCHIVE=self.archive,
                          SKU=self.sku, MODELS=self.sku.providers, DATA=self.store.data,
                          WATCH=None, MOUSE=None, ANALYSIS=None, RAWLINK=None,
                          FILESEARCH=None, UPDATES=None, COMPONENTS=None,
                          DRIVER=None, CANON=None, PREVIEWS=None, REFERENCES=None)
            for key, value in values.items():
                stack.enter_context(patch.object(server, key, value))
            http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            http.daemon_threads = True
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(channel='msedge', headless=True)
                    try:
                        page = browser.new_page(viewport={'width':1600, 'height':1000})
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}', wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepArchiveOpenItem&&window.FramekeepArchiveCapture')
                        page.evaluate('(id)=>FramekeepArchiveOpenItem(id)', self.item(2))
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid img[data-preview-done=ready]').length===4")
                        page.wait_for_function("()=>![...document.querySelectorAll('[data-catalog-rating]')].find(n=>n.dataset.catalogRating==='2').disabled")
                        # 走真实标星和当前标记解析入口；识别结果由隔离模型确定。
                        page.locator('[data-catalog-rating="2"]').click()
                        self.api.tick()
                        page.evaluate('()=>FramekeepArchiveCapture.refresh()')
                        page.wait_for_function("()=>!document.getElementById('capture-ocr').disabled")
                        page.locator('#capture-ocr').click()
                        self.api.tick()
                        page.wait_for_function("()=>document.getElementById('capture-ocr-text').textContent.includes('LC123456')")
                        self.drain()
                        page.wait_for_function("()=>[...document.querySelectorAll('#catalog-grid .catalog-card-sku')].filter(n=>n.textContent==='SKU：LC123456').length===2")
                        page.wait_for_timeout(500)
                        page.evaluate('''()=>{
                            window.stableImages=[...document.querySelectorAll('#catalog-grid img')];
                            window.removedPreviews=0;
                            window.stabilityObserver=new MutationObserver(records=>{
                                for(const record of records)for(const node of record.removedNodes)
                                    for(const image of stableImages)if(node===image||node.contains?.(image))removedPreviews++;
                            });
                            stabilityObserver.observe(document.getElementById('catalog-grid'),{childList:true,subtree:true});
                            document.getElementById('catalog-selected-sku').focus();
                            document.getElementById('catalog-selected-sku').value='UNSAVED-DRAFT';
                        }''')
                        # 投影/同步进度连续变化，图片内容和文件版本不变。
                        for sku in ['LC123456-A', 'LC123456-B', 'LC123456-C']:
                            with self.catalog.connect() as c:
                                c.execute("UPDATE items SET sku=?,source='portable' WHERE id=?", (sku, self.item(1)))
                            page.evaluate("()=>document.dispatchEvent(new CustomEvent('framekeep:metadata-imported'))")
                            page.wait_for_function('(sku)=>[...document.querySelectorAll("#catalog-grid .catalog-card-sku")].some(n=>n.textContent==="SKU："+sku)', arg=sku)
                        page.wait_for_timeout(4500)
                        result=page.evaluate('''()=>({removed:removedPreviews,
                            same:stableImages.every(img=>img.isConnected),
                            focused:document.activeElement.id,
                            draft:document.getElementById('catalog-selected-sku').value})''')
                        out=Path(__file__).resolve().parents[1]/'build/archive-ocr-stability'
                        out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'verified.png'))
                        self.assertEqual(result['removed'],0,result)
                        self.assertTrue(result['same'],result)
                        self.assertEqual(result['focused'],'catalog-selected-sku')
                        self.assertEqual(result['draft'],'UNSAVED-DRAFT')
                        # 搜索结果按文件夹分组时，同样保留所有已加载图片。
                        page.locator('#catalog-search').fill('IMG_')
                        page.wait_for_function("()=>document.querySelectorAll('#catalog-grid .catalog-folder-group .catalog-card').length===4")
                        page.evaluate("()=>{stabilityObserver.disconnect();stableImages=[...document.querySelectorAll('#catalog-grid img')];removedPreviews=0;stabilityObserver.observe(document.getElementById('catalog-grid'),{childList:true,subtree:true})}")
                        with self.catalog.connect() as c:
                            c.execute("UPDATE items SET sku='GROUPED-UPDATE' WHERE id=?", (self.item(1),))
                        page.evaluate("()=>document.dispatchEvent(new CustomEvent('framekeep:metadata-imported'))")
                        page.wait_for_function("()=>document.querySelector('#catalog-grid .catalog-card-sku').textContent==='SKU：GROUPED-UPDATE'")
                        self.assertEqual(page.evaluate('()=>removedPreviews'),0)
                        # 真实文件版本改变时，只更新对应图片，不复用旧预览。
                        page.evaluate('()=>stabilityObserver.disconnect()')
                        with self.catalog.connect() as c:
                            c.execute("UPDATE items SET fingerprint=fingerprint||':changed' WHERE id=?", (self.item(1),))
                        page.evaluate("()=>document.dispatchEvent(new CustomEvent('framekeep:metadata-imported'))")
                        page.wait_for_function("()=>!stableImages[0].isConnected&&document.querySelector('#catalog-grid img').dataset.previewDone==='ready'")
                        self.assertTrue(page.evaluate('()=>stableImages.slice(1).every(img=>img.isConnected)'))
                        # 空结果仍从加载状态切换为明确空列表。
                        page.select_option('#catalog-media','design')
                        page.wait_for_function("()=>document.querySelector('#catalog-grid .catalog-empty')?.textContent==='没有符合当前条件的照片'")
                        self.assertFalse(errors,errors)
                    finally:
                        browser.close()
            finally:
                http.shutdown()
                http.server_close()


if __name__=='__main__':
    unittest.main()
