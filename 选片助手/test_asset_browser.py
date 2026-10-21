import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from asset_analysis import AssetAnalysis
from vision_scheduler import VisionScheduler
from model_providers import Providers
from test_asset_analysis import LocalVision
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]

class AssetBrowserTests(unittest.TestCase):
    def test_confirmed_sku_autofills_selection_and_manual_batch_remains_editable(self):
        import server
        from test_sku_workflow import CODE
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT)
            sid=store.ensure_live_session(photos);store.activate(sid);archive=AssetArchive(store,autostart=False)
            archive.start_stream(sid,'project','模特图')
            ids=[]
            for i in range(3):
                p=photos/f'{i:03d}.JPG';Image.new('RGB',(200,140),(50+i*20,100,140)).save(p)
                record=media.capture_record(photos,p,None,{});store.append_live(sid,[record]);ids.append(record['id'])
            job={'id':'vision-label','capture_id':ids[-1],'session_id':sid,'project':'project','scope':'模特图','status':'matched'}
            archive.reserve(job)
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            patches=[patch.object(server,key,value) for key,value in [('STORE',store),('DATA',store.data),('ARCHIVE',archive),('SKU',None),('WATCH',None),('MOUSE',None),('MODELS',Providers(store.data))]]
            for p in patches:p.start()
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        covers=page.locator('#photo-grid [data-focus]');covers.nth(0).click()
                        self.assertEqual(page.locator('#asset-auto').count(),0)
                        self.assertEqual(page.locator('#asset-from-current').count(),0)
                        page.wait_for_function('()=>document.getElementById("asset-sku").value===""')
                        archive.handle_event('vision-confirmed',job,CODE)
                        page.wait_for_function('(code)=>document.getElementById("asset-sku").value===code',arg=CODE)
                        self.assertEqual(page.evaluate('FramekeepSku.currentType()'),'');page.locator('#current-shooting-tab').click();page.locator('#shooting-manual-type').fill('模特图');page.locator('#photo-settings-tab').click()
                        covers.nth(2).click(modifiers=['Shift'])
                        page.wait_for_function('(code)=>document.getElementById("asset-sku").value===code',arg=CODE)
                        self.assertEqual(page.locator('#asset-count').inner_text(),'3 张')
                        out=ROOT/'build/asset-acceptance';out.mkdir(parents=True,exist_ok=True)
                        page.locator('#app-settings-open').click()
                        page.wait_for_timeout(400);page.screenshot(path=str(out/'sku-auto-filled.png'));page.locator('#app-settings-close').click()
                        page.locator('#asset-sku').fill('MANUAL123')
                        # 改变区间状态，强制下一次轮询出现新版本；不能覆盖手工输入。
                        with archive.connect() as c:c.execute("UPDATE segments SET reason='刷新',status='needs_review'")
                        page.wait_for_timeout(2100);self.assertEqual(page.locator('#asset-sku').input_value(),'MANUAL123')
                        page.locator('#app-settings-open').click();page.locator('#asset-overwrite').check();page.locator('#app-settings-close').click();page.locator('#asset-preview').click()
                        page.wait_for_selector('#asset-preview-dialog[open]');page.locator('#asset-apply').click()
                        page.wait_for_function('()=>!document.getElementById("asset-preview-dialog").open')
                        for cid in ids:
                            asset=archive.asset(archive.ensure(sid,cid));self.assertEqual(asset['sku'],'MANUAL123');self.assertTrue(asset['locked'])
                        covers.nth(0).click();page.wait_for_function('()=>document.getElementById("asset-sku").value==="MANUAL123"')
                        # 模拟另一个资产的不同款号，多选应显示混合值，不能沿用第一张。
                        preview=archive.preview(sid,[ids[-1]],'OTHER123','模特图',overwrite=True);archive.apply(preview['preview_id'])
                        covers.nth(2).click(modifiers=['Shift'])
                        page.wait_for_function('()=>document.getElementById("asset-sku").placeholder.includes("所选款号不同")')
                        self.assertEqual(page.locator('#asset-sku').input_value(),'')
                        page.screenshot(path=str(out/'sku-auto-fill.png'));self.assertFalse(errors,errors)
                        page.reload(wait_until='domcontentloaded');covers=page.locator('#photo-grid [data-focus]');covers.nth(2).click()
                        page.wait_for_function('()=>document.getElementById("asset-sku").value==="OTHER123"')
                    finally:browser.close()
            finally:
                archive.close();http.shutdown();http.server_close()
                for p in reversed(patches):p.stop()

    def test_shift_metadata_archive_lm_card_and_parallel_analysis(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos);store.activate(sid)
            for i in range(5):
                p=photos/f'{i:03d}.JPG';Image.new('RGB',(200,140),(50+i*20,100,140)).save(p);store.append_live(sid,[media.capture_record(photos,p,None,{})])
            providers=Providers(store.data);archive=AssetArchive(store);scheduler=VisionScheduler();analysis=AssetAnalysis(archive,providers,scheduler)
            catalog=AssetCatalog(base/'data');catalog.add_root(str(photos))
            upstream=ThreadingHTTPServer(('127.0.0.1',0),LocalVision);LocalVision.calls=[];LocalVision.peak=0;LocalVision.invalid=False;threading.Thread(target=upstream.serve_forever,daemon=True).start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            patches=[patch.object(server,key,value) for key,value in [('STORE',store),('DATA',store.data),('MODELS',providers),('ARCHIVE',archive),('CATALOG',catalog),('ANALYSIS',analysis),('SKU',None),('WATCH',None),('MOUSE',None)]]
            for p in patches:p.start()
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                        covers=page.locator('#photo-grid [data-focus]');covers.nth(0).click();covers.nth(2).click(modifiers=['Shift'])
                        self.assertEqual(page.locator('#asset-count').inner_text(),'3 张');self.assertEqual(page.locator('#photo-grid .asset-selected').count(),3)
                        self.assertEqual(store.session(sid)['state'].get('overrides',{}),{})
                        page.locator('#asset-sku').fill('SKU-中文123');page.locator('#current-shooting-tab').click();page.locator('#shooting-manual-type').fill('细节图');page.locator('#photo-settings-tab').click();page.locator('#asset-preview').click();page.wait_for_selector('#asset-preview-dialog[open]');page.locator('#asset-apply').click()
                        page.wait_for_function('()=>document.getElementById("asset-message").textContent.includes("已内嵌 3")')
                        for i in range(3):self.assertEqual(archive.writer.read(photos/f'{i:03d}.JPG')['XMP-fk:SKU'],'SKU-中文123')
                        self.assertFalse(archive.writer.read(photos/'003.JPG').get('XMP-fk:SKU'))
                        page.locator('#asset-archive-open').click();page.wait_for_function('()=>document.querySelectorAll("#catalog-grid .catalog-card").length===5')
                        out=ROOT/'build/asset-acceptance';out.mkdir(exist_ok=True);page.wait_for_function('()=>Array.from(document.querySelectorAll("#catalog-grid img")).every(i=>i.naturalWidth>0)');page.screenshot(path=str(out/'archive-browser.png'))
                        page.locator('#catalog-grid .catalog-card').first.click();page.wait_for_function('()=>!document.getElementById("catalog-inspector-form").hidden')
                        page.locator('#catalog-selected-sku').fill('SKU-中文123');page.locator('#catalog-save-sku').click();page.wait_for_function('()=>document.getElementById("catalog-scan-status").textContent.includes("SKU-中文123")')
                        page.locator('#selection-tab').click()
                        page.locator('#app-settings-open').click();page.locator('#assistant-settings').click();page.locator('#model-lm-preset').click();self.assertEqual(page.locator('#model-url').input_value(),'http://127.0.0.1:1234/v1')
                        page.locator('#model-url').fill(f'http://127.0.0.1:{upstream.server_port}/v1');page.locator('#model-id').fill('qwen-local-test');page.locator('#model-save').click();page.wait_for_function('()=>document.querySelectorAll("[data-model-card]").length===1')
                        self.assertFalse(providers.public()['selected']);pid=providers.public()['providers'][0]['id']
                        page.wait_for_function('()=>document.querySelectorAll("#analysis-provider option").length===2');page.locator('#analysis-provider').select_option(pid);page.locator('#analysis-bind-provider').click();page.wait_for_function('()=>document.getElementById("analysis-provider-message").textContent.includes("选用已保存")');page.locator('#analysis-test-provider').click();page.wait_for_function('()=>document.getElementById("analysis-provider-message").textContent.includes("图片请求通过")')
                        page.screenshot(path=str(out/'local-model-browser.png'));page.locator('#models-close').click();page.locator('#app-settings-close').click()
                        page.locator('#asset-analysis-open').click();page.locator('#analysis-new-schema').click();page.locator('#analysis-schema-name').fill('颜色测试');page.locator('#analysis-prompt').fill('只输出 color 字段。');page.locator('#analysis-json-schema').fill(json.dumps({'type':'object','properties':{'color':{'type':'string'}},'required':['color']}));page.locator('#analysis-save-schema').click();page.wait_for_function('()=>document.getElementById("analysis-schema").value.includes(":1")')
                        page.locator('#analysis-pause').click();page.wait_for_function('()=>document.getElementById("analysis-queue-state").textContent==="已暂停"')
                        page.locator('#analysis-concurrency').select_option('1');page.wait_for_function('()=>document.getElementById("analysis-stats").textContent.includes("0/1")')
                        page.locator('#analysis-concurrency').select_option('2');page.locator('#analysis-resume').click();page.wait_for_function('()=>document.getElementById("analysis-queue-state").textContent==="运行中"')
                        page.locator('#analysis-run').click();page.wait_for_function('()=>document.getElementById("analysis-stats").textContent.includes("已完成 3")');self.assertEqual(LocalVision.peak,2)
                        page.screenshot(path=str(out/'analysis-browser.png'))
                        page.locator('#analysis-jobs details summary').first.click()
                        page.wait_for_timeout(1400)
                        self.assertTrue(page.locator('#analysis-jobs details').first.evaluate('(e)=>e.open'))
                        page.locator('#catalog-search').evaluate('(e)=>e.value="SKU-中文123"')
                        for theme in ['dark','light','pure-white','hardcore']:
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            for target in ['asset-archive-open','asset-analysis-open']:
                                page.locator('#'+target).click()
                                page.wait_for_timeout(100)
                                for width in [1680,1024,768,375,320]:
                                    page.set_viewport_size({'width':width,'height':1000})
                                    self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'),(theme,target,width))
                                page.set_viewport_size({'width':1680,'height':1050})
                                page.screenshot(path=str(out/f'{theme}-{target}.png'))
                        page.locator('#asset-analysis-open').click()
                        self.assertEqual(page.locator('.workspace-tabs [role=tab]').count(),6)
                        self.assertEqual(page.locator('.workspace-tabs [aria-selected=true]').count(),1)
                        self.assertEqual(page.locator('#analysis-schema-name').input_value(),'颜色测试')
                        self.assertEqual(page.locator('#catalog-search').input_value(),'SKU-中文123')
                        page.locator('#selection-tab').click();page.locator('#view-open').hover();page.locator('#layout-header').click()
                        self.assertTrue(page.locator('.workspace-bottom-bar>.workspace-tabs').is_visible())
                        page.locator('#asset-archive-open').click();self.assertTrue(page.locator('#asset-archive-dialog').is_visible())
                        page.locator('#selection-tab').click();page.locator('#view-open').hover();page.locator('#layout-header').click();page.locator('#asset-analysis-open').click()
                        self.assertEqual(page.locator('#analysis-selection').inner_text(),'3 张')
                        page.locator('#asset-analysis-open').press('Home');self.assertTrue(page.locator('#selection-page').is_visible())
                        page.locator('#selection-tab').press('End');self.assertTrue(page.locator('#app-settings-page').is_visible());page.locator('#app-settings-open').press('ArrowLeft');self.assertTrue(page.locator('#analysis-dialog').is_visible())
                        # 其他工作页按数字键或方向键不改变照片评级和选片上下文。
                        prior=page.evaluate('()=>Framekeep.remoteTarget()')
                        page.locator('#analysis-dialog h2').click();page.keyboard.press('5');page.keyboard.press('ArrowRight')
                        self.assertEqual(page.evaluate('()=>Framekeep.remoteTarget()'),prior)
                        self.assertEqual(store.session(sid)['state'].get('overrides',{}),{})
                        self.assertFalse(errors,errors)
                        page.reload(wait_until='networkidle');page.locator('#asset-analysis-open').click();page.wait_for_function('()=>document.getElementById("analysis-stats").textContent.includes("已完成 3")')
                    finally:browser.close()
            finally:
                analysis.close();archive.close();http.shutdown();http.server_close();upstream.shutdown();upstream.server_close()
                for p in reversed(patches):p.stop()

if __name__=='__main__':unittest.main()
