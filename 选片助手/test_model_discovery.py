from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import json
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

from model_providers import Providers
from api_endpoints import validated_url, api_base, api_endpoint


class ModelService:
    def __init__(self):
        self.calls=[];self.routes={};self.status=200;self.payload={'data':[{'id':'qwen-vl'},{'id':'text-mini'},{'id':'qwen-vl'},{'id':None},{'id':''}]}
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                owner.calls.append((self.path,self.headers.get('Authorization')))
                status,payload=owner.routes.get(self.path,(owner.status,owner.payload))
                raw=payload if isinstance(payload,bytes) else json.dumps(payload).encode()
                self.send_response(status)
                if status==302:self.send_header('Location',f'http://127.0.0.1:{self.server.server_port}/redirect-target')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.path,body))
                value=json.dumps({'text':'AB123','codes':['AB123'],'confidence':.98,'is_label':True,'vision_ok':True})
                payload={'output':[{'content':[{'type':'output_text','text':value}]}]} if self.path.endswith('/responses') else {'choices':[{'message':{'content':value}}]}
                raw=json.dumps(payload).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.http.daemon_threads=True
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.http.server_port}'
    def close(self):self.http.shutdown();self.http.server_close();self.thread.join(timeout=2)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):self.folder=tempfile.TemporaryDirectory();self.providers=Providers(self.folder.name);self.service=ModelService()
    def tearDown(self):self.service.close();self.folder.cleanup()
    def test_draft_and_saved_key_discovery_never_persists_or_selects(self):
        p=self.providers;s=self.service
        for suffix in ['', '/v1','/v1/chat/completions','/v1/responses']:
            self.assertEqual(p.models({'protocol':'chat','url':s.url+suffix,'key':'draft-key'})['models'],['qwen-vl','text-mini'])
            self.assertEqual(s.calls[-1],('/v1/models','Bearer draft-key'))
        self.assertFalse(p.path.exists())
        state=p.save({'name':'视觉','category':'multimodal','protocol':'responses','url':s.url+'/v1','model':'qwen-vl','key':'saved-key'})
        pid=state['providers'][0]['id'];before=p.path.read_bytes()
        result=p.models({'id':pid,'key':''});self.assertEqual(s.calls[-1][1],'Bearer saved-key');self.assertNotIn('saved-key',json.dumps(result));self.assertEqual(p.path.read_bytes(),before);self.assertEqual(p.public()['selected'],{})
        p.models({'id':pid,'key':'override-key'});self.assertEqual(s.calls[-1][1],'Bearer override-key');self.assertEqual(p.by_id(pid)['key'],'saved-key')
    def test_failures_are_actionable_and_do_not_leak_keys_or_follow_redirect(self):
        s=self.service;body={'url':s.url,'protocol':'chat','key':'do-not-leak'}
        for status,match in [(401,'密钥'),(404,'手动'),(500,'HTTP 500'),(302,'HTTP 302')]:
            s.status=status;before=len(s.calls)
            with self.assertRaisesRegex(ValueError,match) as error:self.providers.models(body)
            self.assertNotIn('do-not-leak',str(error.exception));self.assertEqual(len(s.calls),before+(2 if status==404 else 1))
        s.status=200
        for payload in [b'not-json',{'data':{}},b'x'*2_000_001]:
            s.payload=payload
            with self.assertRaisesRegex(ValueError,'手动'):self.providers.models(body)
        s.payload={'data':[]};self.assertEqual(self.providers.models(body)['models'],[])
    def test_validation_and_save_select_category_isolation_restart(self):
        p=self.providers
        for body in [{'id':'missing'},{'url':self.service.url,'protocol':'paddleocr'},{'url':'ftp://remote.invalid','protocol':'chat'},{'url':self.service.url+'?key=secret','protocol':'chat'},{'url':self.service.url,'protocol':'chat','key':'a\nb'}]:
            with self.assertRaises(ValueError):p.models(body)
        self.assertEqual(self.service.calls,[])
        def save(name,category,select=False):return p.save({'name':name,'category':category,'protocol':'chat','url':self.service.url,'model':name,'select':select})
        state=save('vision-a','multimodal',True);vision=state['selected']['multimodal']
        state=save('text-a','text',True);text=state['selected']['text']
        state=save('vision-b','multimodal');self.assertEqual(state['selected'],{'multimodal':vision,'text':text})
        other=state['providers'][-1];state=p.save({**other,'select':True});self.assertEqual(len(state['providers']),3);self.assertEqual(state['selected'],{'multimodal':other['id'],'text':text})
        self.assertEqual(Providers(self.folder.name).public(),state)

    def test_http_any_host_and_shorthand_normalization(self):
        for raw,expected in [('http://remote.example:8080/api','http://remote.example:8080/api'),('192.168.1.8:8000','http://192.168.1.8:8000'),('api.example:8000','http://api.example:8000'),('api.example','https://api.example'),('api.example:443','https://api.example:443'),('[::1]:1234','http://[::1]:1234'),('HTTPS://API.EXAMPLE/v1/','https://api.example/v1')]:
            self.assertEqual(validated_url(raw),expected)
            saved=self.providers.save({'name':raw,'category':'ocr','protocol':'ocr_json','url':raw,'model':'manual'})
            self.assertEqual(saved['providers'][-1]['url'],expected)
        for invalid in ['', 'http://user:pw@host', 'http://host?a=b','http://host#part','host:99999','ftp://host','http://host/\npath','http://[::1','http://host/path\\bad']:
            with self.assertRaises(ValueError,msg=invalid):validated_url(invalid)

    def test_shorthand_custom_paths_and_inference_use_discovered_endpoint(self):
        from sku_vision import recognize
        from asset_analysis import vision
        s=self.service
        for prefix in ['/v1','/gateway/v2','/proxy/v1','']:
            base=s.url+prefix;s.status=404;s.routes={prefix+'/models':(200,{'data':[{'id':'qwen-vl'}]})}
            # /proxy 自动补出 /v1，其余输入保留指定路径。
            raw=base.removeprefix('http://') if prefix!='/proxy/v1' else s.url.removeprefix('http://')+'/proxy'
            for protocol in ['chat','responses']:
                result=self.providers.models({'url':raw,'protocol':protocol})
                self.assertEqual(result['models'],['qwen-vl'])
                saved=self.providers.save({'name':'resolved','category':'multimodal','protocol':protocol,'url':result['url'],'model':'qwen-vl','select':True})
                provider=Providers(self.folder.name).selected('multimodal')
                expected=base+('/responses' if protocol=='responses' else '/chat/completions')
                self.assertEqual(api_endpoint(provider['url'],protocol),expected)
                self.assertEqual(recognize(provider,b'test-image')['codes'],['AB123']);self.assertEqual(s.calls[-1][0],expected.removeprefix(s.url));self.assertEqual(s.calls[-1][1]['model'],'qwen-vl')
                self.assertTrue(json.loads(vision(provider,b'test-image','test',{'type':'object'}))['vision_ok']);self.assertEqual(s.calls[-1][0],expected.removeprefix(s.url))
        s.status=404;s.routes={'/v1/models':(200,{'data':[]})}
        for suffix in ['/v1/models','/v1/responses','/v1/chat/completions']:
            self.assertEqual(self.providers.models({'url':s.url+suffix,'protocol':'chat'})['url'],s.url+'/v1')
        self.assertEqual(api_base('http://remote.example/custom/v2/responses'),'http://remote.example/custom/v2')

    def test_scheme_fallback_domain_root_formats_and_explicit_protocol(self):
        from io import BytesIO
        calls=[]
        class Opener:
            def open(self,request,timeout):
                calls.append(request.full_url)
                if request.full_url.startswith('https:'):raise urllib.error.URLError('TLS unavailable')
                return BytesIO(json.dumps({'data':[{'id':'resolved-model'}]}).encode())
        with patch('model_providers.urllib.request.build_opener',return_value=Opener()):
            result=self.providers.models({'url':'api.example','protocol':'chat'})
            self.assertEqual(result['url'],'http://api.example/v1');self.assertEqual(calls,['https://api.example/v1/models','http://api.example/v1/models'])
            calls.clear()
            with self.assertRaises(ValueError):self.providers.models({'url':'https://api.example','protocol':'chat'})
            self.assertEqual(calls,['https://api.example/v1/models'])
        s=self.service;s.routes={'/v1/models':(200,b'<html>homepage</html>'),'/models':(200,{'models':[{'name':'model-b'},{'model':'model-a'}]})}
        result=self.providers.models({'url':s.url.replace('127.0.0.1','localhost'),'protocol':'chat'})
        self.assertEqual(result['models'],['model-a','model-b']);self.assertTrue(result['url'].endswith('/chat/completions'))


class DiscoveryBrowserTests(unittest.TestCase):
    def test_fetch_search_save_select_edit_and_late_response(self):
        from playwright.sync_api import sync_playwright
        from catalog_store import Store
        import server
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'data',root);providers=Providers(store.data);service=ModelService()
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('MODELS',providers),('MOUSE',None),('WATCH',None),('SKU',None),('ARCHIVE',None),('ANALYSIS',None),('RAWLINK',None),('FILESEARCH',None)]]
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':1100});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepBoot?.loading===false');page.locator('#app-settings-open').click();page.locator('#assistant-settings').click();page.locator('#model-add').click()
                        self.assertEqual(page.locator('#model-concurrency').input_value(),'4');page.locator('#model-concurrency').fill('8')
                        page.locator('#model-url').fill(service.url.removeprefix('http://'));page.locator('#model-key').fill('private-browser-key');page.locator('#model-id').focus()
                        page.wait_for_function('()=>document.getElementById("model-discovered").options.length===2');page.locator('#model-search').fill('QWEN');self.assertEqual(page.locator('#model-discovered option').count(),1)
                        self.assertEqual(page.locator('#model-url').input_value(),service.url+'/v1');self.assertEqual(page.locator('#model-url').get_attribute('type'),'text')
                        page.locator('#model-discovered').select_option('qwen-vl');self.assertEqual(page.locator('#model-id').input_value(),'qwen-vl');page.locator('#model-save').click();page.wait_for_selector('[data-model-card]');self.assertEqual(providers.public()['selected'],{})
                        pid=providers.public()['providers'][0]['id'];page.locator('[data-model-card]').click();page.wait_for_function('()=>document.querySelector("[data-model-card]").getAttribute("aria-pressed")==="true"')
                        self.assertEqual(page.locator('.model-concurrency-summary').inner_text(),'最大并发 8');self.assertEqual(Providers(store.data).by_id(pid)['concurrency'],8)
                        page.locator('[data-model-edit]').click();self.assertEqual(page.locator('#model-key').input_value(),'');page.locator('#model-fetch').click();page.wait_for_function('()=>document.getElementById("model-discovered").options.length===2');self.assertEqual(service.calls[-1][1],'Bearer private-browser-key')
                        self.assertEqual(page.locator('#model-concurrency').input_value(),'8')
                        page.locator('#model-protocol').select_option('local');self.assertTrue(page.locator('#model-concurrency').is_disabled());self.assertEqual(page.locator('#model-concurrency').input_value(),'1')
                        page.locator('#model-protocol').select_option('chat');self.assertEqual(page.locator('#model-concurrency').input_value(),'8');page.locator('#model-url').fill(service.url+'/v1')
                        # 失败保留手填标识；迟到响应不能污染新的接口草稿。
                        service.status=401;page.locator('#model-id').fill('manual-id');page.locator('#model-fetch').click();page.wait_for_function('()=>document.getElementById("model-discovery-message").textContent.includes("401")');self.assertEqual(page.locator('#model-id').input_value(),'manual-id');service.status=200
                        def delayed(route):
                            response=route.fetch();page.locator('#model-url').fill(service.url+'/changed');route.fulfill(response=response)
                        page.route('**/api/providers/models',delayed);page.locator('#model-fetch').click();page.wait_for_function('()=>document.getElementById("model-url").value.endsWith("/changed")');page.wait_for_timeout(150);self.assertTrue(page.locator('#model-discovered').is_disabled());page.unroute('**/api/providers/models',delayed)
                        page.locator('#model-cancel').click();page.locator('#model-add').click();page.locator('#model-category').select_option('text');page.locator('#model-url').fill(service.url.replace('127.0.0.1','localhost').removeprefix('http://'));page.locator('#model-fetch').click();page.wait_for_function('()=>document.getElementById("model-discovered").options.length===2');page.locator('#model-discovered').select_option('text-mini')
                        out=root/'build/model-discovery-acceptance';out.mkdir(exist_ok=True);page.locator('#model-discovery-message').scroll_into_view_if_needed();page.screenshot(path=str(out/'model-picker.png'))
                        page.locator('#model-save-select').click();page.wait_for_function('()=>document.querySelectorAll("[data-model-card]").length===2');self.assertEqual(providers.public()['selected']['multimodal'],pid);self.assertIn('text',providers.public()['selected'])
                        page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepBoot?.loading===false');page.locator('#app-settings-open').click();page.locator('#assistant-settings').click();page.wait_for_function('()=>document.querySelectorAll("[data-model-card][aria-pressed=true]").length===2');self.assertFalse(errors,errors)
                        # 保存会等待补全；等待期间取消不能创建卡片。
                        def cancel_pending(route):
                            response=route.fetch();page.locator('#model-cancel').click();route.fulfill(response=response)
                        page.route('**/api/providers/models',cancel_pending);page.locator('#model-add').click();page.locator('#model-name').fill('cancelled');page.locator('#model-id').fill('manual-id');page.locator('#model-url').fill(service.url.removeprefix('http://'));page.locator('#model-save').click()
                        page.wait_for_function('()=>document.getElementById("model-editor").classList.contains("hidden")&&!document.getElementById("model-save").disabled');self.assertEqual(len(providers.public()['providers']),2);page.unroute('**/api/providers/models',cancel_pending)
                        page.locator('#model-add').click();page.locator('#model-name').fill('wait-and-save');page.locator('#model-id').fill('manual-id');page.locator('#model-url').fill(service.url.removeprefix('http://'));page.locator('#model-save-select').click();page.wait_for_function('()=>document.querySelectorAll("[data-model-card]").length===3')
                        self.assertEqual(providers.public()['providers'][-1]['url'],service.url+'/v1');self.assertEqual(providers.selected('multimodal')['model'],'manual-id');self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close();service.close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
