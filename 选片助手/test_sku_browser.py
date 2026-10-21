import base64
import io
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

from PIL import Image,ImageDraw
from catalog_store import Store
from folder_watch import FolderWatch
from model_providers import Providers
from mouse_native.interception import Interception
from mouse_remote import MouseRemote
from sku_service import SkuService
from sku_vision import recognize
from test_mouse_remote import FakeTransport,FakeHotkey,FakeLease
from test_sku_workflow import fixture_book,CODE

ROOT=Path(__file__).resolve().parents[1]


class Upstream(BaseHTTPRequestHandler):
    calls=[];codes=[CODE];confidence=.99;gate=None
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.calls.append({'path':self.path,'body':body,'auth':self.headers.get('Authorization')})
        if self.gate:self.gate.wait(timeout=8)
        result={'text':'\n'.join(self.codes),'codes':self.codes,'confidence':self.confidence,'is_label':True,'handwritten':True}
        if self.path.endswith('/responses'):result={'output':[{'content':[{'type':'output_text','text':json.dumps(result)}]}]}
        elif self.path.endswith('/chat/completions'):result={'choices':[{'message':{'content':json.dumps(result)}}]}
        elif self.path.endswith('/paddle'):result={'result':{'ocrResults':[{'prunedResult':{'rec_texts':self.codes,'rec_scores':[self.confidence]}}]}}
        data=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


class VisionAdapterTests(unittest.TestCase):
    def test_all_protocols_use_model_and_image_and_no_keys_in_result(self):
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream);threading.Thread(target=upstream.serve_forever,daemon=True).start();Upstream.calls=[];Upstream.gate=None;Upstream.codes=[CODE]
        try:
            for protocol,path in [('responses','/v1'),('chat','/v1'),('ocr_json','/ocr'),('paddleocr','/paddle')]:
                p={'id':'test','name':'test','url':f'http://127.0.0.1:{upstream.server_port}'+path,'model':'configured-model','key':'private-test-key','protocol':protocol}
                result=recognize(p,b'image-bytes');self.assertEqual(result['codes'],[CODE]);self.assertNotIn('private-test-key',json.dumps(result))
                call=Upstream.calls[-1];self.assertEqual(call['auth'],'Bearer private-test-key')
                if protocol!='paddleocr':self.assertEqual(call['body']['model'],'configured-model')
        finally:upstream.shutdown();upstream.server_close()


class SkuBrowserTests(unittest.TestCase):
    def test_live_folder_two_clicks_frozen_sheet_review_and_export(self):
        from playwright.sync_api import sync_playwright
        import server
        chrome=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
        if not chrome.is_file():self.skipTest('测试用 Chrome 未安装')
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'incoming';photos.mkdir();xlsx=base/'拍摄清单.xlsx';xlsx.write_bytes(fixture_book())
            store=Store(base/'data',ROOT);providers=Providers(store.data);sku=SkuService(store,providers)
            watch=FolderWatch(store,autostart=False);watch.STABLE_SECONDS=.2;watch.INTERVAL=.1;watch.thread.start()
            transport=FakeTransport();remote=MouseRemote(lambda _,click,error:Interception(None,click,error,transport=transport),FakeHotkey,lease_factory=FakeLease,legacy_guard=lambda:None)
            upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream);Upstream.calls=[];Upstream.codes=[CODE];Upstream.confidence=.99;Upstream.gate=None
            threading.Thread(target=upstream.serve_forever,daemon=True).start()
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            patches=[patch.object(server,key,value) for key,value in [('STORE',store),('DATA',store.data),('MOUSE',remote),('WATCH',watch),('MODELS',providers),('SKU',sku)]]
            for p in patches:p.start()
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start();url=f'http://127.0.0.1:{http.server_port}'
            try:
                with self.assertRaises(urllib.error.HTTPError) as denied:urllib.request.urlopen(urllib.request.Request(url+'/api/sku/import',b'{}',{'Content-Type':'application/json'}))
                self.assertEqual(denied.exception.code,403)
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path=str(chrome),headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050},accept_downloads=True);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(url,wait_until='networkidle')
                        # 卡片添加不会自动选用；点击卡片才改变类别默认。
                        page.locator('#app-settings-open').click();page.locator('#assistant-settings').click();page.locator('#model-add').click()
                        page.locator('#model-name').fill('现场视觉');page.locator('#model-protocol').select_option('chat');page.locator('#model-id').fill('mock-vision');page.locator('#model-url').fill(f'http://127.0.0.1:{upstream.server_port}/v1');page.locator('#model-key').fill('browser-secret-only');page.locator('#model-save').click()
                        page.wait_for_function('()=>document.querySelectorAll("[data-model-card]").length===1')
                        self.assertEqual(providers.public()['selected'],{})
                        page.locator('[data-model-card]').click();page.wait_for_function('()=>document.querySelector(".model-card.selected")!==null')
                        pid=providers.public()['selected']['multimodal']
                        page.locator('[data-model-edit]').click();self.assertEqual(providers.public()['selected']['multimodal'],pid);self.assertEqual(page.locator('#model-key').input_value(),'');page.locator('#model-cancel').click()
                        out=ROOT/'build/sku-acceptance';out.mkdir(exist_ok=True)
                        page.screenshot(path=str(out/'model-cards.png'));page.locator('#models-close').click();page.locator('#app-settings-close').click()
                        # 空目录监控，真实文件进入。
                        page.locator('#folder-watch').click();page.locator('#watch-path').fill(str(photos));page.locator('#watch-start').click()
                        page.wait_for_function('()=>!document.getElementById("watch-dialog").open&&FRAMEKEEP_SESSION.id')
                        page.locator('#sku-open').click();page.locator('#sku-file').set_input_files(str(xlsx))
                        page.wait_for_function('()=>document.querySelectorAll("#sku-panel-scope option").length===3')
                        page.locator('#sku-panel-scope').select_option('模特图')
                        page.wait_for_function('()=>document.querySelector(".sku-product-image img")?.naturalWidth===40')
                        page.locator('.sku-product-image').first.click();page.wait_for_function('()=>document.querySelector(".sku-image-dialog>img")?.naturalWidth===40');page.locator('.sku-image-dialog button').click()
                        page.wait_for_function('()=>document.querySelectorAll("#sku-panel-scope option").length===3')
                        page.locator('#sku-panel-scope').select_option('模特图');page.wait_for_function('()=>document.getElementById("sku-project-info").textContent.includes("模特图")');page.locator('#sku-close').click()
                        if not page.locator('#selection-tools').evaluate('(e)=>e.open'):page.locator('#view-open').hover();page.locator('#layout-tools').click()
                        page.locator('#app-settings-open').click();page.locator('#mouse-remote').click();page.locator('#mouse-start').click();page.wait_for_function('()=>!document.getElementById("mouse-dialog").open')
                        def label(name,text):
                            image=Image.new('RGB',(1000,500),'white');ImageDraw.Draw(image).text((70,150),text,fill='black',font_size=65);image.save(photos/name)
                            page.wait_for_function('(name)=>document.getElementById("filename").textContent===name',arg=name)
                        def clicks(n):
                            for _ in range(n):transport.feed(1,2)
                        label('label1.JPG',CODE);clicks(2)
                        page.wait_for_function('()=>document.querySelectorAll("#stars .on").length===2')
                        # 在 450ms 保存窗口内切换页，必须仍记入原模特图页。
                        page.locator('#current-shooting-tab').click();page.locator('#sku-live-scope').select_option('细节图')
                        page.wait_for_function('()=>document.getElementById("sku-live-scope").value==="细节图"')
                        page.wait_for_function('()=>document.getElementById("sku-open").textContent.includes("1 已拍摄")')
                        state=sku.state();self.assertEqual(state['jobs'][0]['scope'],'模特图')
                        shot=[r for r in state['rows'] if r['status']=='已拍摄'];self.assertEqual([r['sheet'] for r in shot],['模特图'])
                        self.assertEqual(page.locator('#sku-current-table tbody tr td:last-child').all_text_contents(),['未拍摄','未拍摄'])
                        page.locator('#photo-settings-tab').click()
                        self.assertEqual(Upstream.calls[-1]['body']['model'],'mock-vision')
                        # 新标签走当前细节图；低置信结果必须复核。
                        Upstream.confidence=.5;Upstream.codes=['LC7877496-P9O5']
                        label('label2.JPG','LC7877496-P9O5');clicks(2)
                        page.wait_for_function('()=>document.querySelectorAll("#stars .on").length===2')
                        page.locator('#sku-open').click();page.wait_for_function('()=>document.querySelector(".sku-job[data-status=review]")!==null')
                        page.locator('#sku-records > summary').click()
                        card=page.locator('.sku-job[data-status=review]').first
                        target=next(r for r in sku.state()['rows'] if r['sheet']=='细节图' and r['sku']==CODE)
                        card.locator('[data-sku-target]').select_option(target['id']);card.locator('[data-sku-action=confirm]').click()
                        page.wait_for_function('()=>document.querySelector(".sku-job[data-status=confirmed]")!==null')
                        self.assertEqual(len([r for r in sku.state()['rows'] if r['status']=='已拍摄']),2)
                        page.screenshot(path=str(out/'sku-reconciliation.png'))
                        with page.expect_download() as download:page.locator('#sku-export').click()
                        file=download.value;file.save_as(str(out/'browser-export.xlsx'))
                        with ZipFileLike(out/'browser-export.xlsx') as z:self.assertIn('拍摄状态',z.read('xl/worksheets/sheet1.xml').decode())
                        page.locator('#sku-close').click();page.keyboard.press('3');page.evaluate('()=>FramekeepIO.flush()')
                        page.wait_for_function('()=>document.getElementById("sku-open").textContent.includes("1 已拍摄")')
                        self.assertEqual([r['sheet'] for r in sku.state()['rows'] if r['status']=='已拍摄'],['模特图'])
                        page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>document.getElementById("sku-live-scope")?.value==="细节图"');self.assertEqual(page.locator('#sku-live-scope').input_value(),'细节图')
                        self.assertNotIn('browser-secret-only',json.dumps(providers.public()))
                        self.assertFalse(errors,errors)
                        (out/'browser-result.json').write_text(json.dumps({'passed':True,'errors':errors,'checks':['cards add edit select','secret not returned','real watched JPG','simulated native two clicks','sheet frozen before autosave','other page unchanged','fuzzy review','manual confirm only job sheet','Excel download','change rating revokes only own contribution','reload selected scope']},indent=2),encoding='utf8')
                    finally:browser.close()
            finally:
                Upstream.gate=None;sku.close();watch.close();remote.close();http.shutdown();http.server_close();upstream.shutdown();upstream.server_close();thread.join(timeout=2)
                for p in reversed(patches):p.stop()


from zipfile import ZipFile as ZipFileLike
if __name__=='__main__':unittest.main()
