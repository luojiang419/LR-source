"""真实浏览器验证统一预检覆盖各入口，并在准备成功后才提交识别。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from catalog_store import Store
from component_setup import ComponentManager
from model_providers import Providers
from sku_service import SkuService
import media_engine as media
from test_component_setup import record

ROOT=Path(__file__).resolve().parents[1]


class ComponentBrowserTests(unittest.TestCase):
    def test_ocr_pages_prepare_once_progress_retry_and_continue(self):
        import server
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            base=Path(folder);photos=base/'photos';photos.mkdir();photo=photos/'one.JPG';Image.new('RGB',(200,150),'teal').save(photo)
            store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos);store.activate(sid);store.append_live(sid,[media.capture_record(photos,photo,None,{})]);cid=store.records(sid)[0]['id']
            providers=Providers(store.data);providers.set_builtin(True);archive=AssetArchive(store,autostart=False);sku=SkuService(store,providers,autostart=False)
            files={'runtime/llama-server.exe':b'fake-runtime','models/ocr/model.gguf':b'fake-ocr','models/ocr/mmproj.gguf':b'fake-projection','models/qwen/model.gguf':b'fake-qwen','models/qwen/mmproj.gguf':b'fake-projection'}
            seed=base/'seed';assets=[]
            for name,raw in files.items():
                p=seed/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
                assets.append(record(name,'runtime' if name.startswith('runtime') else 'glm-ocr-local' if '/ocr/' in name else 'qwen2.5-vl-7b-q4',raw,bundled=True))
            manager=ComponentManager(store.data,[seed],{'assets':assets,'archives':[]});calls=[];original=manager._seed
            def slow(r,t):calls.append(r['path']);time.sleep(.2);return original(r,t)
            manager._seed=slow
            for key,value in dict(STORE=store,DATA=store.data,MODELS=providers,ARCHIVE=archive,SKU=sku,COMPONENTS=manager,DRIVER=None,MOUSE=None,WATCH=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None).items():stack.enter_context(patch.object(server,key,value))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1400,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        def accepted(route):
                            self.assertIn('glm-ocr-local',manager.snapshot()['ready'])
                            route.fulfill(status=200,content_type='application/json',body='{"prepared":true}')
                        paths=['/api/sku/parse','/api/sku/recognize','/api/parsing/ocr','/api/analysis/batches','/api/providers/test','/api/parsing/configure','/api/parsing/control']
                        for path in paths:page.route('**'+path,accepted)
                        # 通过正式现场 OCR 按钮触发，原请求在补齐后才发出。
                        page.locator('#photo-settings-tab').click();page.locator('#asset-parse-sku').click()
                        page.locator('#component-progress').wait_for(state='visible')
                        out=ROOT/'build/complete-install';out.mkdir(parents=True,exist_ok=True)
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(t)=>FramekeepTheme.set(t)',theme);page.set_viewport_size({'width':375,'height':900})
                            self.assertFalse(page.locator('#component-progress').evaluate('(e)=>e.scrollWidth>e.clientWidth'))
                        page.set_viewport_size({'width':1400,'height':1000});page.screenshot(path=str(out/'component-progress.png'))
                        page.wait_for_function('()=>document.getElementById("component-progress").hidden')
                        self.assertEqual(len(calls),3)
                        for path in paths:
                            body={'capture_id':cid,'capture_ids':[cid],'asset_ids':[],'id':providers.selected('ocr')['id'],'mode':'auto','channel':'ocr','action':'retry'}
                            self.assertTrue(page.evaluate('async(v)=>await FramekeepIO.api(v[0],v[1])',[path,body])['prepared'])
                        self.assertEqual(len(calls),3)
                        self.assertFalse((manager.root/'models/qwen/model.gguf').exists())
                        # 删除组件且镜像尚未可用：显示错误与重试，不悄悄发出识别。
                        target=manager.root/'models/ocr/model.gguf';target.unlink();saved=(seed/'models/ocr/model.gguf').read_bytes();(seed/'models/ocr/model.gguf').unlink()
                        failed=page.evaluate('async()=>{try{await FramekeepIO.api("/api/sku/parse",{});return "unexpected"}catch(e){return e.message}}')
                        self.assertIn('组件',failed);self.assertNotEqual(failed,'unexpected');self.assertTrue(page.locator('#component-retry').is_visible())
                        (seed/'models/ocr/model.gguf').write_bytes(saved);page.locator('#component-retry').click();page.wait_for_function('()=>document.getElementById("component-progress").hidden')
                        self.assertTrue(page.evaluate('async()=>await FramekeepIO.api("/api/sku/parse",{})')['prepared'])
                        # 后台任务没有原按钮调用栈，也要显示失败并能直接重试组件准备。
                        background=manager.root/'runtime/llama-server.exe';background.unlink()
                        seed_file=seed/'runtime/llama-server.exe';seed_raw=seed_file.read_bytes();seed_file.unlink()
                        manager.start(['runtime'])
                        page.wait_for_function('()=>!document.getElementById("component-progress").hidden&&document.getElementById("component-progress").classList.contains("has-error")')
                        seed_file.write_bytes(seed_raw);page.locator('#component-retry').click()
                        page.wait_for_function('()=>document.getElementById("component-progress").hidden')
                        self.assertIn('runtime',manager.snapshot()['ready'])
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:manager.close();http.shutdown();http.server_close();sku.close();archive.close()


if __name__=='__main__':unittest.main()
