"""真实页面验证无目录/监控中重复导入、旧请求保护及预览标星持久化。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from folder_watch import FolderWatch
from model_providers import Providers
from sku_service import SkuService
from test_sku_workflow import fixture_book

ROOT=Path(__file__).resolve().parents[1]


class ImportPreviewBrowserTests(unittest.TestCase):
    def test_import_any_context_and_preview_rating(self):
        import server
        out=ROOT/'build/import-preview-acceptance';out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory() as temporary,ExitStack() as stack:
            base=Path(temporary);photos=base/'photos';photos.mkdir()
            store=Store(base/'data',ROOT);providers=Providers(store.data)
            sku=SkuService(store,providers,autostart=False);watch=FolderWatch(store)
            for key,value in dict(STORE=store,DATA=store.data,MODELS=providers,SKU=sku,WATCH=watch,
                                  MOUSE=None,ARCHIVE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,
                                  CANON=None,CATALOG=None,COMPONENTS=None,DRIVER=None,PREVIEWS=None).items():
                stack.enter_context(patch.object(server,key,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':940},reduced_motion='reduce')
                        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        document=page.evaluate('performance.timeOrigin')
                        def upload(selector,name,content=None):
                            page.locator(selector).set_input_files({'name':name,'mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':fixture_book() if content is None else content})
                            page.wait_for_function('(name)=>document.getElementById("shooting-document").textContent.includes(name)',arg=name)
                            page.wait_for_function('()=>!FramekeepSku.isBusy()&&!document.getElementById("shooting-import").disabled')
                        # 全新启动，无照片目录也立即展示首个有效工作表及款号。
                        self.assertFalse(page.evaluate('!!FRAMEKEEP_SESSION.id'))
                        upload('#shooting-plan-file','无目录.xlsx')
                        self.assertEqual(sku.project()['scope'],'模特图');self.assertFalse(sku.project()['session_id'])
                        self.assertIsNone(page.evaluate('FramekeepSku.context()'))
                        self.assertEqual(page.locator('#sku-current-table tbody tr').count(),2)
                        self.assertEqual(page.locator('#sku-bind-current').inner_text(),'绑定当前照片目录')
                        page.locator('#sku-open').click()
                        self.assertEqual(page.locator('#sku-rows tbody tr').count(),2)
                        page.locator('[data-progress-sheet="细节图"]').click()
                        page.wait_for_function('()=>document.getElementById("sku-panel-scope").value==="细节图"')
                        page.locator('#sku-search').fill('不存在的款号');page.locator('#sku-filter').select_option('shot')
                        # 延迟旧轮询响应，导入新表后回包也不能覆盖新任务。
                        held=[]
                        def delay(route):
                            if not held:held.append((route,route.fetch()))
                            else:route.continue_()
                        page.route('**/api/sku',delay);page.wait_for_timeout(1300)
                        self.assertTrue(held)
                        upload('#sku-file','重复导入.xlsx')
                        self.assertEqual(page.locator('#sku-panel-scope').input_value(),'细节图')
                        self.assertEqual(page.locator('#sku-search').input_value(),'')
                        self.assertEqual(page.locator('#sku-filter').input_value(),'all')
                        self.assertEqual(page.locator('#sku-rows tbody tr').count(),2)
                        held[0][0].fulfill(response=held[0][1]);page.unroute('**/api/sku',delay)
                        page.wait_for_timeout(200)
                        self.assertIn('重复导入.xlsx',page.locator('#shooting-document').inner_text())
                        pid=sku.project()['id']
                        page.locator('#sku-file').set_input_files({'name':'损坏.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':b'broken xlsx'})
                        page.wait_for_function('()=>document.getElementById("sku-message").textContent.includes("表格无法导入")')
                        self.assertEqual(sku.project()['id'],pid)
                        page.locator('#sku-close').click();page.locator('#folder-watch').click()
                        page.locator('#watch-path').fill(str(photos));page.locator('#watch-start').click()
                        page.wait_for_function('()=>!!FRAMEKEEP_SESSION.id&&!document.getElementById("watch-dialog").open')
                        sid=page.evaluate('FRAMEKEEP_SESSION.id')
                        for index in range(2):Image.new('RGB',(800,1200),['#607d8b','#887558'][index]).save(photos/f'{index:03}.JPG')
                        page.wait_for_function('()=>DEMO_PHOTOS.length===2&&document.querySelector("#photos img")?.naturalWidth>0')
                        page.locator('#stars [data-rating="4"]').click();page.evaluate('FramekeepIO.flush()')
                        selected=page.evaluate('Framekeep.remoteTarget().photoId')
                        upload('#shooting-plan-file','监控中.xlsx')
                        self.assertEqual(sku.project()['session_id'],sid);self.assertEqual(sku.project()['scope'],'细节图')
                        self.assertTrue(watch.state()['enabled'])
                        self.assertEqual(store.session(sid)['state']['overrides'][selected],4)
                        self.assertEqual(page.locator('.right-panel #stars').count(),0)
                        self.assertEqual(page.locator('.preview-surface > #stars').count(),1)
                        page.locator('#photo-settings-tab').click()
                        page.locator('[data-view="single"]').click()
                        for theme in ['dark','light','pure-white','hardcore']:
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            for width in [1440,1024,600,375]:
                                page.set_viewport_size({'width':width,'height':940})
                                page.screenshot(path=str(out/'latest-layout.png'))
                                stars=page.locator('#stars').bounding_box();surface=page.locator('.preview-surface').bounding_box()
                                zoom=page.locator('.thumbnail-control').bounding_box()
                                self.assertTrue(page.locator('#stars').is_visible())
                                self.assertGreaterEqual(stars['x'],surface['x']);self.assertLess(stars['x']-surface['x'],20,(theme,width,stars,surface,page.locator('#stars').evaluate('e=>({left:getComputedStyle(e).left,position:getComputedStyle(e).position,margin:getComputedStyle(e).margin,transform:getComputedStyle(e).transform})')))
                                self.assertLessEqual(stars['y']+stars['height'],surface['y']+surface['height'])
                                self.assertLess(surface['y']+surface['height']-stars['y']-stars['height'],70)
                                if zoom:self.assertTrue(stars['x']+stars['width']<=zoom['x'] or stars['y']+stars['height']<=zoom['y'])
                                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                            page.set_viewport_size({'width':1440,'height':940})
                            page.locator('#stars [data-rating="3"]').click();page.evaluate('FramekeepIO.flush()')
                            self.assertEqual(page.locator('#stars .on').count(),3)
                            page.screenshot(path=str(out/f'preview-rating-{theme}.png'))
                        page.locator('#next').click() if not page.locator('#next').is_disabled() else page.locator('#previous').click()
                        page.keyboard.press('5');page.evaluate('FramekeepIO.flush()')
                        other=page.evaluate('Framekeep.remoteTarget().photoId')
                        self.assertNotEqual(other,selected);self.assertEqual(store.session(sid)['state']['overrides'][other],5)
                        self.assertEqual(store.session(sid)['state']['overrides'][selected],3)
                        self.assertEqual(page.evaluate('performance.timeOrigin'),document)
                        page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        page.wait_for_function('()=>document.getElementById("shooting-document").textContent.includes("监控中.xlsx")')
                        page.wait_for_function('()=>FramekeepLayout.isMonitoring()')
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',other)
                        page.wait_for_function('()=>document.querySelectorAll("#stars .on").length===5')
                        self.assertEqual(sku.project()['scope'],'细节图');self.assertEqual(page.locator('#stars .on').count(),5)
                        self.assertFalse(errors,errors)
                        (out/'result.json').write_text(json.dumps({'passed':True,'errors':errors,'checks':['import before folder','unbound sheet switch','replace clears filters','stale poll ignored','failed import retained','import while monitoring','preview stars four themes four widths','click/keyboard/save/reload']},ensure_ascii=False,indent=2),encoding='utf8')
                    finally:browser.close()
            finally:http.shutdown();http.server_close();watch.close();sku.close()


if __name__=='__main__':unittest.main()
