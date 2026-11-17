"""停止拍摄后，归档旧路径或失效独立地址仍能恢复原图浏览。"""
from contextlib import closing
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from direct_channel import DirectImageChannel
from test_jpeg_receiving_preview import application_runtime, ROOT


class MainPreviewRecoveryTests(unittest.TestCase):
    def test_failed_original_and_detail_use_thumbnail_without_retry_loop(self):
        with application_runtime() as (server,base,photos,store,watch,sid,url), patch.object(server,'DIRECT',None), sync_playwright() as pw:
            watch.stop()
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(reduced_motion='reduce');failed=[]
                def reject_detail(route):
                    address=route.request.url
                    if 'full=1' in address or 'size=2048' in address:
                        failed.append(address);route.abort()
                    else:route.continue_()
                page.route('**/api/preview/**',reject_detail)
                page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                page.locator('[data-view=single]').click()
                image=page.locator('#photos [data-loupe] img')
                page.wait_for_function('()=>document.querySelector("#photos [data-loupe] img")?.naturalWidth>0')
                self.assertIn('size=480',image.get_attribute('src'))
                self.assertLessEqual(image.evaluate('i=>Math.max(i.naturalWidth,i.naturalHeight)'),480)
                # 邻图预取在100ms后运行，与当前主图的恢复请求分别计数。
                page.wait_for_timeout(300)
                count=len(failed);page.wait_for_timeout(600);self.assertEqual(len(failed),count)
                self.assertLessEqual(count,4)
            finally:browser.close()

    def test_stopped_monitor_recovers_moved_evicted_and_unavailable_original(self):
        for failure in ('moved', 'evicted', 'unavailable'):
            with self.subTest(failure=failure), application_runtime() as (server,base,photos,store,watch,sid,url):
                watch.stop()
                with closing(DirectImageChannel(watch,url)) as channel, patch.object(server,'DIRECT',channel), sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':960},reduced_motion='reduce')
                        errors=[];failed=False;requests=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.on('request',lambda request:requests.append(request.url))
                        def break_original(route):
                            nonlocal failed
                            if not failed:
                                failed=True
                                if failure=='moved':
                                    destination=photos/'归档款号'/'FIRST.JPG';destination.parent.mkdir()
                                    (photos/'FIRST.JPG').rename(destination)
                                    with store.connect() as c:
                                        row=c.execute('SELECT id,data FROM captures WHERE session=?',(sid,)).fetchone()
                                        record=json.loads(row[1]);record['jpg']='归档款号/FIRST.JPG'
                                        c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(json.dumps(record),sid,row[0]))
                                elif failure=='evicted':
                                    for index in range(4097):channel.register(photos,f'unrelated-{index}.JPG')
                            if failure=='unavailable':route.abort()
                            else:route.continue_()
                        page.route(channel.origin+'/api/direct/image/**',break_original)
                        page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!FramekeepBoot.loading')
                        page.locator('[data-view=single]').click()
                        page.wait_for_function('()=>document.querySelector("#photos [data-loupe] img")?.naturalWidth===600',timeout=4000)
                        image=page.locator('#photos [data-loupe] img')
                        self.assertEqual(urlsplit(image.get_attribute('src')).path.split('/')[:3],['','api','preview'])
                        self.assertIn('full=1',image.get_attribute('src'))
                        self.assertTrue(failed);self.assertFalse(watch.state()['enabled'])
                        # 恢复原始分辨率后仍可点击缩放，切到列表再返回也能继续浏览。
                        image.click();self.assertEqual(page.evaluate('FramekeepViewer.getScale()'),2)
                        page.locator('[data-view=list]').click();page.locator('[data-view=single]').click()
                        expect(image).to_be_visible();self.assertEqual(image.evaluate('i=>i.naturalWidth'),600)
                        self.assertTrue(any('/api/preview/' in address and 'full=1' in address for address in requests))
                        self.assertFalse(errors,errors)
                        out=ROOT/'build/main-preview-recovery';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/(failure+'.png')))
                    finally:browser.close()


if __name__=='__main__':unittest.main()
