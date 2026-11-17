"""真实 HTTP 队列验证归档暂停、继续、全速恢复和窄栏按钮布局。"""
import json
from pathlib import Path
from unittest.mock import patch
import unittest
import threading
import time
import urllib.request

from playwright.sync_api import expect,sync_playwright
import server
from preview_cache import PreviewWarmer,cache_for
from catalog_status import CatalogStatus
import test_asset_retrieval_browser as fixture


ROOT=Path(__file__).resolve().parents[1]


class ArchiveDecodePauseBrowserTests(unittest.TestCase):
    def setUp(self):
        fixture.RetrievalBrowserTests.setUp(self)
        self.worker=PreviewWarmer(cache_for(self.store.data/'previews'),'unused',catalog=self.catalog,autostart=False)
        self.catalog.on_previews=self.worker.enqueue;self.catalog.preview_warmer=self.worker
        self.stack.enter_context(patch.object(server,'PREVIEWS',self.worker))
        self.catalog.queue_all_previews();self.worker.refresh_phase()

    def tearDown(self):
        self.worker.close();fixture.RetrievalBrowserTests.tearDown(self)

    def test_start_skips_persisted_model_controls_when_already_paused(self):
        from asset_analysis import AssetAnalysis
        from vision_scheduler import VisionScheduler
        self.sku.configure_tasks(mode='manual',paused=True)
        self.service.configure({'enabled':False,'manual_active':False,'paused':True},include_state=False)
        analysis=AssetAnalysis(self.archive,self.providers,VisionScheduler(),autostart=False)
        try:
            analysis.configure_auto(False);analysis.control(paused=True)
            with urllib.request.urlopen(self.url+'/api/bootstrap') as response:token=json.load(response)['request_token']
            request=urllib.request.Request(self.url+'/api/catalog/decode',data=b'{"action":"start"}',headers={'Content-Type':'application/json','X-Framekeep-Token':token})
            with patch.object(server,'ANALYSIS',analysis),patch.object(self.service,'configure',side_effect=AssertionError('不应重写已暂停配置')),patch.object(self.sku,'configure_tasks',side_effect=AssertionError('不应读取无关任务统计')),patch.object(analysis,'configure_auto',side_effect=AssertionError('不应触发无关数据库检查点')),patch.object(analysis,'control',side_effect=AssertionError('不应读取无关模型状态')):
                with urllib.request.urlopen(request,timeout=3) as response:value=json.load(response)
            self.assertTrue(value['started']);self.assertTrue(value['decode']['full_speed'])
        finally:analysis.close()

    def test_finished_queue_does_not_look_stuck_while_directories_are_scanning(self):
        while self.worker.process_one():pass
        with self.catalog.connect() as c:c.execute("UPDATE roots SET status='scanning'")
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                expect(page.locator('#catalog-preview-status')).to_contain_text('当前队列已完成')
                expect(page.locator('#catalog-preview-status')).to_contain_text('正在检查新增或修改素材')
                expect(page.locator('#catalog-preview-eta')).to_contain_text('预计剩余：0 秒')
                expect(page.locator('#catalog-preview-percent')).to_have_text('100%')
                self.assertEqual(page.locator('#catalog-preview-bar').get_attribute('value'),'100')
            finally:browser.close()

    def test_released_cache_is_not_shown_as_completed_or_automatically_rebuilt(self):
        while self.worker.process_one():pass
        index=cache_for(self.catalog.previews)
        with index.lock,index.connect() as c:
            name=c.execute('SELECT name FROM previews WHERE edge=2048 LIMIT 1').fetchone()[0];index._remove(c,name)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded')
                # 验证统计，不让页面的按需缩略图请求提前补齐被测试的缺项。
                page.route('**/api/catalog/preview/**',lambda route:route.abort())
                page.route('**/api/catalog/cached/**',lambda route:route.abort())
                page.locator('#asset-archive-open').click()
                expect(page.locator('#catalog-preview-status')).to_contain_text('当前已缓存 2 / 3 项')
                expect(page.locator('#catalog-preview-status')).to_contain_text('未缓存 1 项')
                expect(page.locator('#catalog-preview-eta')).to_contain_text('按需生成')
                expect(page.locator('#catalog-preview-percent')).to_have_text('66%')
                self.assertEqual(self.worker.archive_state()['pending'],0)
            finally:browser.close()

    def test_full_speed_button_fills_released_cache_without_decoding_valid_items(self):
        from PIL import Image
        import json
        other=self.base/'其他归档';other.mkdir();Image.new('RGB',(80,60),'blue').save(other/'missing.jpg')
        other_root=self.catalog.add_root(str(other))['root_id']
        while self.worker.process_one():pass
        index=cache_for(self.catalog.previews)
        with self.worker.cache.read_connection() as c:
            record=json.loads(c.execute('SELECT record FROM archive_jobs WHERE root_id=?',(other_root,)).fetchone()[0])
        from preview_cache import cache_name
        name=cache_name(record,2048)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                # 隐藏根的缺失项不能被当前页按需预加载补齐，必须由全速准备。
                page.locator(f'#catalog-tree [data-root="{self.rows[0]["root"]}"][data-folder=""]').first.click()
                expect(page.locator('#catalog-grid .catalog-card')).to_have_count(3)
                with index.lock,index.connect() as c:index._remove(c,name)
                completed=self.worker.archive_state()['completed_this_run']
                with patch.object(self.catalog,'render_preview_batch',wraps=self.catalog.render_preview_batch) as render:
                    self.worker.start();page.locator('#catalog-decode-start').click()
                    until=time.monotonic()+8
                    while time.monotonic()<until:
                        value=self.worker.archive_state()
                        if value['ready']==4 and value['completed_this_run']==completed+1 and not value['active_workers']:break
                        page.wait_for_timeout(30)
                    self.assertEqual(value['completed_this_run'],completed+1)
                self.assertEqual(render.call_count,1)
                expect(page.locator('#catalog-preview-status')).to_contain_text('当前已缓存 4 / 4 项',timeout=10000)
                expect(page.locator('#catalog-preview-percent')).to_have_text('100%')
            finally:browser.close()

    def test_timeout_reconciles_real_pause_and_unrelated_refresh_cannot_fail_decode(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                pause=page.locator('#catalog-decode-pause');message=page.locator('#catalog-decode-message')
                expect(pause).to_be_enabled()
                page.evaluate("""()=>{const original=window.fetch;window.decodePosts=0;window.failDecode=true;
                  window.fetch=async(url,...args)=>{
                    if(String(url)==='/api/intelligence/pipeline')throw new DOMException('signal timed out','TimeoutError');
                    const response=await original(url,...args);
                    if(String(url)==='/api/catalog/decode'){++window.decodePosts;if(window.failDecode){window.failDecode=false;throw new DOMException('signal timed out','TimeoutError')}}
                    return response;
                  }}""")
                pause.click();expect(message).to_have_text('已核对：解码已暂停');expect(pause).to_have_text('继续解码');expect(pause).to_be_enabled()
                self.assertTrue(self.worker.manual_paused);self.assertEqual(page.evaluate('window.decodePosts'),1)
                pause.click();expect(message).to_be_empty();expect(pause).to_have_text('暂停解码');expect(pause).to_be_enabled()
                self.assertFalse(self.worker.manual_paused);self.assertEqual(page.evaluate('window.decodePosts'),2)
            finally:browser.close()

    def test_timeout_keeps_unknown_result_until_progress_confirms_pause(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                pause=page.locator('#catalog-decode-pause');message=page.locator('#catalog-decode-message');expect(pause).to_be_enabled()
                page.evaluate("""()=>{const original=window.fetch;window.decodePosts=0;window.fetch=(url,...args)=>{
                  if(String(url)==='/api/catalog/decode'){++window.decodePosts;return Promise.reject(new DOMException('signal timed out','TimeoutError'))}
                  return original(url,...args);
                }}""")
                pause.click();expect(message).to_contain_text('结果尚未确认');expect(pause).to_be_enabled()
                self.assertFalse(self.worker.manual_paused);self.assertEqual(page.evaluate('window.decodePosts'),1)
                self.worker.set_paused(True)
                expect(message).to_have_text('已核对：解码已暂停',timeout=10000);expect(pause).to_have_text('继续解码')
                self.assertEqual(page.evaluate('window.decodePosts'),1)
            finally:browser.close()

    def test_reopen_restores_progress_and_resumes_with_slow_metadata_statistics(self):
        self.assertTrue(self.worker.process_one());self.worker.set_paused(True);self.worker.close()
        self.worker=PreviewWarmer(cache_for(self.store.data/'previews'),'unused',catalog=self.catalog,autostart=False)
        self.catalog.on_previews=self.worker.enqueue;self.catalog.preview_warmer=self.worker
        self.stack.enter_context(patch.object(server,'PREVIEWS',self.worker))
        self.catalog.progress.close();self.catalog.progress=CatalogStatus(self.catalog)
        entered=threading.Event();release=threading.Event();original=self.catalog.metadata.state
        def slow():
            entered.set();release.wait(30);return original()
        self.worker.start()
        out=ROOT/'build/archive-decode-restart';out.mkdir(parents=True,exist_ok=True)
        with patch.object(self.catalog.metadata,'state',side_effect=slow),sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1000,'height':680});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                status=page.locator('#catalog-preview-status');pause=page.locator('#catalog-decode-pause')
                expect(status).to_contain_text('当前已缓存 1 / 3 项',timeout=10000)
                self.assertTrue(entered.is_set());self.assertFalse(release.is_set())
                self.assertIsNone(self.catalog.progress.value)
                expect(pause).to_have_text('继续解码')
                page.reload(wait_until='domcontentloaded')
                if not pause.is_visible():page.locator('#asset-archive-open').click()
                expect(status).to_contain_text('当前已缓存 1 / 3 项',timeout=10000)
                page.screenshot(path=str(out/'restored-paused.png'))
                pause.click()
                expect(status).to_contain_text('当前已缓存 3 / 3 项',timeout=10000)
                self.assertEqual(self.worker.completed_by_root[next(iter(self.worker.completed_by_root))],2)
                self.assertFalse(release.is_set());self.assertFalse(errors,errors)
                page.screenshot(path=str(out/'resumed-completed.png'))
            finally:release.set();browser.close()

    def test_pause_reload_resume_full_speed_and_failed_request(self):
        out=ROOT/'build/archive-decode-pause';out.mkdir(parents=True,exist_ok=True)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1000,'height':680});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                pause=page.locator('#catalog-decode-pause');start=page.locator('#catalog-decode-start')
                expect(pause).to_be_enabled();expect(start).to_have_text('全速解码')
                page.wait_for_function('async()=>!(await(await fetch("/api/catalog/state")).json()).previews.paused')
                self.assertTrue(self.worker.process_one())
                page.wait_for_function('()=>document.getElementById("catalog-preview-status").textContent.includes("当前已缓存 1 / 3 项")')
                pause.click();expect(pause).to_have_text('继续解码');expect(pause).to_be_enabled()
                self.assertTrue(self.worker.manual_paused);self.assertFalse(self.worker.process_one())
                expect(page.locator('#catalog-preview-eta')).to_contain_text('解码已暂停')
                expect(page.locator('#catalog-preview-runtime')).to_contain_text('已暂停')
                for theme in ('dark','light','pure-white','hardcore'):
                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                    a=start.bounding_box();b=pause.bounding_box()
                    self.assertAlmostEqual(a['y'],b['y'],delta=1)
                    self.assertGreaterEqual(b['x'],a['x']+a['width'])
                    self.assertLessEqual(b['x']+b['width'],1000)
                    self.assertFalse(page.locator('.catalog-decode-actions').evaluate('e=>e.scrollWidth>e.clientWidth'))
                    page.screenshot(path=str(out/f'paused-{theme}.png'))
                page.reload(wait_until='domcontentloaded')
                if not pause.is_visible():page.locator('#asset-archive-open').click()
                expect(pause).to_have_text('继续解码');expect(pause).to_be_enabled()
                pause.click();expect(pause).to_have_text('暂停解码');expect(pause).to_be_enabled()
                self.assertFalse(self.worker.manual_paused);self.assertFalse(self.worker.full_speed)
                self.assertTrue(self.worker.process_one())
                page.wait_for_function('()=>document.getElementById("catalog-preview-status").textContent.includes("当前已缓存 2 / 3 项")')
                pause.click();expect(pause).to_have_text('继续解码');expect(start).to_be_enabled()
                start.click();expect(pause).to_have_text('暂停解码');expect(pause).to_be_enabled()
                self.assertFalse(self.worker.manual_paused);self.assertTrue(self.worker.full_speed)
                self.assertTrue(self.worker.process_one());self.assertFalse(self.worker.process_one())
                page.wait_for_function('()=>document.getElementById("catalog-preview-status").textContent.includes("全部完成")')
                # 错误显示在归档当前面板，不误报暂停或切到继续状态。
                page.route('**/api/catalog/decode',lambda route:route.fulfill(status=503,json={'error':'测试暂停失败'}))
                pause.click();expect(page.locator('#catalog-decode-message')).to_have_text('测试暂停失败')
                expect(pause).to_have_text('暂停解码');expect(pause).to_be_enabled()
                self.assertFalse(self.worker.manual_paused)
                page.unroute('**/api/catalog/decode')
                pause.click();expect(pause).to_have_text('继续解码')
                expect(page.locator('#catalog-decode-message')).to_be_empty()
                self.worker.foreground_active=1;self.worker.set_paused(False)
                expect(pause).to_have_text('暂停解码')
                self.worker.foreground_active=0
                page.screenshot(path=str(out/'completed.png'))
                state=self.worker.archive_state([self.rows[0]['root']])
                (out/'result.json').write_text(json.dumps({'state':state,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf8')
                self.assertEqual((state['ready'],state['failed']),(3,0));self.assertFalse(errors,errors)
            finally:browser.close()


if __name__=='__main__':unittest.main()
