"""慢缩略图下的首屏调度、连接预算与滚动取消。"""
from io import BytesIO
from pathlib import Path
import unittest

from PIL import Image
from playwright.sync_api import sync_playwright,Error


class AssetLoadingBrowserTests(unittest.TestCase):
    def test_prefetch_decodes_mounted_and_next_page_images_without_scrolling(self):
        payload=BytesIO();Image.new('RGB',(120,120),'teal').save(payload,format='JPEG')
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            pending=[]
            try:
                page=browser.new_page();calls=[]
                page.route('http://preview.test/**',lambda route:(calls.append(route.request.url),pending.append(route)))
                page.set_content('<style>#grid{height:130px;width:400px;overflow:auto;display:grid;grid-template-columns:1fr 1fr}img{width:180px;height:120px}.catalog-thumb{height:125px}</style><div id="grid">'+''.join(
                    f'<span class="catalog-thumb"><img data-preview-src="http://preview.test/{i}"></span>' for i in range(12))+'</div>')
                page.add_script_tag(path=str(Path(__file__).resolve().parents[1]/'演示方案/asset-loading.js'))
                page.evaluate('()=>{window.control=new AbortController();FramekeepAssetLoading.prefetch(grid,Array.from({length:24},(_,i)=>"http://preview.test/"+i),{signal:control.signal}).then(value=>window.result=value)}')
                for _ in range(150):
                    page.wait_for_timeout(20)
                    self.assertLessEqual(len(pending),4)
                    for route in pending[:]:
                        pending.remove(route);route.fulfill(body=payload.getvalue(),content_type='image/jpeg')
                    if page.evaluate('!!window.result'):break
                self.assertEqual(page.evaluate('window.result'),{'ready':24,'failed':0,'cancelled':False})
                self.assertEqual(page.locator('#grid').evaluate('e=>e.scrollTop'),0)
                self.assertEqual(page.locator('#grid img').count(),12,'后续分页预取不能扩充卡片DOM')
                self.assertEqual(page.locator('#grid img').evaluate_all('(images)=>images.filter(e=>e.naturalWidth>0).length'),12)
                self.assertEqual(len(calls),24)
            finally:
                for route in pending:
                    try:route.abort()
                    except Error:pass
                browser.close()

    def test_prefetch_abort_stops_unstarted_offscreen_images(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True);pending=[]
            try:
                page=browser.new_page();page.route('http://preview.test/**',lambda route:pending.append(route))
                page.set_content('<div id="grid" style="height:200px;width:200px"></div>')
                page.add_script_tag(path=str(Path(__file__).resolve().parents[1]/'演示方案/asset-loading.js'))
                page.evaluate('()=>{window.control=new AbortController();FramekeepAssetLoading.prefetch(grid,Array.from({length:24},(_,i)=>"http://preview.test/"+i),{signal:control.signal}).then(value=>window.result=value)}')
                page.wait_for_timeout(100);self.assertEqual(len(pending),2,'纯后台最多两路')
                page.evaluate('control.abort()');page.wait_for_function('()=>window.result?.cancelled')
                page.wait_for_timeout(100);self.assertEqual(len(pending),2,'取消后不得继续发起剩余图片')
            finally:
                for route in pending:
                    try:route.abort()
                    except Error:pass
                browser.close()

    def test_cached_visible_images_first_and_scroll_cancels_old_requests(self):
        payload=BytesIO();Image.new('RGB',(120,120),'teal').save(payload,format='JPEG')
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':800,'height':600});pending=[];calls=[];errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def preview(route):
                    calls.append(route.request.url);pending.append(route)
                page.route('http://127.0.0.1/preview/**',preview)
                page.set_content('''<style>#grid{height:250px;width:400px;overflow:auto;display:grid;grid-template-columns:1fr 1fr;gap:0}img{display:block;width:180px;height:120px}.catalog-thumb{height:125px}</style><div id="grid">'''+''.join(
                    f'<span class="catalog-thumb"><img data-preview-src="http://127.0.0.1/preview/{i}'+('?cached=known' if i in (1,10) else '')+'"></span>' for i in range(30))+'</div>')
                page.add_script_tag(path=str(Path(__file__).resolve().parents[1]/'演示方案/asset-loading.js'))
                page.evaluate('FramekeepAssetLoading.watch(document.getElementById("grid"))')
                page.wait_for_function('()=>document.querySelectorAll("img[src]").length===4')
                page.wait_for_timeout(100)
                self.assertEqual(len(pending),4)
                self.assertTrue(calls[0].endswith('/1?cached=known'),calls)
                self.assertTrue(all(int(url.split('/')[-1].split('?')[0])<4 for url in calls),calls)
                # 前三张慢读不结束，第四张完成后下一张仍能前进。
                pending.pop().fulfill(body=payload.getvalue(),content_type='image/jpeg')
                page.wait_for_function('()=>document.querySelectorAll("img[src]").length===5')
                page.evaluate('document.getElementById("grid").scrollTop=1250')
                page.wait_for_function('()=>![...document.querySelectorAll("img")].slice(0,3).some(e=>e.hasAttribute("src"))')
                page.wait_for_function('()=>[...document.querySelectorAll("img[src]")].some(e=>e.dataset.previewSrc.endsWith("/20"))')
                page.evaluate('FramekeepAssetLoading.pause()')
                self.assertLessEqual(page.locator('img[src]').count(),1,'暂停后仅保留已完成图片')
                self.assertFalse(errors,errors)
            finally:
                for route in pending:
                    try:route.abort()
                    except Error:pass
                browser.close()


if __name__=='__main__':unittest.main()
