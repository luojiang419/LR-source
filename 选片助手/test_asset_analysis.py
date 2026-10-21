import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from test_asset_archive import AssetTests
from asset_analysis import AssetAnalysis
from model_providers import Providers
from vision_scheduler import VisionScheduler

class LocalVision(BaseHTTPRequestHandler):
    lock=threading.Lock();active=0;peak=0;calls=[];invalid=False
    def log_message(self,*args):pass
    def do_GET(self):
        payload=json.dumps({'data':[{'id':'qwen-local-test'}]}).encode();self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with self.lock:self.__class__.active+=1;self.__class__.peak=max(self.peak,self.active);self.calls.append(body)
        try:
            time.sleep(.3)
            value={'vision_ok':True} if '连接测试' in json.dumps(body,ensure_ascii=False) else {'color':'blue'}
            text='not-json' if self.invalid else json.dumps(value)
            payload=json.dumps({'choices':[{'message':{'content':text}}]}).encode();self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        finally:
            with self.lock:self.__class__.active-=1

class AnalysisTests(AssetTests):
    # 复用临时目录工具，不重复运行基类功能测试。
    def test_local_two_requests_overlap_dedupe_schema_and_recovery(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),LocalVision);threading.Thread(target=server.serve_forever,daemon=True).start();LocalVision.calls=[];LocalVision.peak=0;LocalVision.invalid=False
        providers=Providers(self.store.data);p=providers.save({'name':'LM local','category':'multimodal','protocol':'chat','url':f'http://127.0.0.1:{server.server_port}/v1','model':'qwen-local-test','key':'test-secret'});pid=p['providers'][0]['id'];providers.bind('asset_analysis',pid)
        scheduler=VisionScheduler();analysis=AssetAnalysis(self.archive,providers,scheduler,autostart=False)
        try:
            self.assertTrue(analysis.test_provider(pid)['ok'])
            aid1=self.archive.ensure(self.sid,self.add('001.JPG'));aid2=self.archive.ensure(self.sid,self.add('002.JPG'))
            state=analysis.save_schema('颜色试验','返回 color 字段。',{'type':'object','properties':{'color':{'type':'string'}},'required':['color'],'additionalProperties':False});s=state['schemas'][0]
            analysis.create_batch([aid1,aid2],s['id'],s['version'])
            with ThreadPoolExecutor(2) as pool:list(pool.map(lambda _:analysis.tick(),range(2)))
            state=analysis.state();self.assertEqual(state['counts'].get('completed'),2,state);self.assertEqual(LocalVision.peak,2)
            for call in LocalVision.calls:self.assertEqual(call['model'],'qwen-local-test');self.assertNotIn('test-secret',json.dumps(call))
            analysis.create_batch([aid1,aid2],s['id'],s['version']);self.assertEqual(len(analysis.state()['jobs']),2)
            with analysis.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM results').fetchone()[0],2)
            analysis.control(paused=True,concurrency=1);analysis.close();analysis=AssetAnalysis(self.archive,providers,scheduler,autostart=False)
            self.assertTrue(analysis.paused);self.assertEqual(scheduler.limit,1)
            new=analysis.save_schema('颜色新版','只返回 color。',json.loads(s['schema_json']),s['id'])['schemas'][0]
            self.assertEqual(new['version'],2)
            analysis.create_batch([aid1],new['id'],new['version']);LocalVision.invalid=True;analysis.control(paused=False);analysis.tick()
            self.assertEqual(analysis.state()['counts'].get('failed'),1)
        finally:analysis.close();server.shutdown();server.server_close()
    def test_shared_scheduler_prioritizes_label_with_global_two_slots(self):
        scheduler=VisionScheduler();gate=threading.Event();entered=[]
        def work(name,label=False):
            with scheduler.slot(label=label):entered.append(name);gate.wait(2)
        with ThreadPoolExecutor(4) as pool:
            first=pool.submit(work,'first');second=pool.submit(work,'second')
            for _ in range(50):
                if scheduler.active==2:break
                time.sleep(.01)
            ordinary=pool.submit(work,'ordinary');label=pool.submit(work,'label',True)
            for _ in range(50):
                if scheduler.waiting_labels:break
                time.sleep(.01)
            self.assertEqual(scheduler.active,2);gate.set()
            for f in [first,second,ordinary,label]:f.result(timeout=5)
        self.assertLess(entered.index('label'),entered.index('ordinary'))

# unittest 不把工具基类导入成重复测试集；分析类只保留此文件的用例。
for _name in list(AssetTests.__dict__):
    if _name.startswith('test_'):setattr(AnalysisTests,_name,None)
del AssetTests
