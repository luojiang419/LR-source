"""真实模型/真实发行引擎的二星延迟验收；素材只读取，写入仅发生在输出副本。"""
from pathlib import Path
import argparse, hashlib, json, os, shutil, socket, sqlite3, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'选片助手'))
from metadata_writer import MetadataWriter,sha
from catalog_store import Store
import media_engine as media

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app',type=Path,default=Path('D:/Program Files/Framekeep'))
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--source',action='store_true')
    parser.add_argument('--ocr-only',action='store_true')
    parser.add_argument('--sequential',action='store_true')
    parser.add_argument('--observe',type=float,default=30)
    args=parser.parse_args();args.app=args.app.resolve();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    data=out/'data';photos=out/'photos';data.mkdir();photos.mkdir()
    original=Path(r'\\192.168.0.181\新摄影\细节图\源图片备份（勿动）\现场拍摄小档\2026-11-04')
    groups=[('LC7877529-P105',ROOT/'错误/例图',['XSY_9288.JPG','XSY_9289.JPG','XSY_9290.JPG','XSY_9291.JPG']),
            ('LC7877836-P1',original,['XSY_2235.JPG','XSY_2295.JPG','XSY_2296.JPG','XSY_2297.JPG']),
            ('LC7832852-P305',original,['XSY_2487.JPG','XSY_2556.JPG','XSY_2557.JPG','XSY_2558.JPG']),
            ('LC7832852-P804',original,['XSY_2559.JPG','XSY_2609.JPG','XSY_2610.JPG','XSY_2611.JPG'])]
    from PIL import Image
    writer=MetadataWriter(ROOT,data);pixels={};originals={}
    store=Store(data,ROOT);sid=store.ensure_live_session(photos);records=[]
    for code,folder,names in groups:
        for name in names:
            path=folder/name;originals[str(path)]=sha(path);shutil.copy2(path,photos/name)
            with Image.open(photos/name) as im:pixels[name]=hashlib.sha256(im.convert('RGB').tobytes()).hexdigest()
        writer.write_rating(photos/names[-1],0)
        metadata=media.read_metadata([photos/name for name in names],media.find_exiftool(ROOT))
        for name,meta in zip(names,metadata):records.append(media.capture_record(photos,photos/name,None,meta))
    store.append_live(sid,records);store.activate(sid)
    (data/'folder-watch.json').write_text(json.dumps({'enabled':True,'folder':str(photos),'recursive':False,'session_id':sid,'source':'manual'}),encoding='utf-8')
    (data/'desktop-preferences.json').write_text(json.dumps({'close_behavior':'exit','startup_mode':'disabled'}),encoding='utf-8')
    config=json.loads(Path('D:/Program Files/Framekeep/data/model-providers.json').read_text(encoding='utf-8'))
    if args.ocr_only:
        config['selected'].pop('multimodal',None);config.get('bindings',{}).pop('sku_label',None)
    # 透明转发实际请求，仅记录时间/模型/输入尺寸和响应字符数，不记录鉴权或图片正文。
    requests=[];request_lock=threading.Lock();active=0;peak=0;targets={}
    class Relay(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            nonlocal active,peak
            payload=self.rfile.read(int(self.headers['Content-Length']));body=json.loads(payload)
            with request_lock:
                active+=1;peak=max(peak,active)
                item={'model':body.get('model'),'started':time.time(),'input_bytes':len(payload)};requests.append(item)
            try:
                req=urllib.request.Request(targets[body['model']]+self.path,payload,{'Content-Type':'application/json','Authorization':self.headers.get('Authorization','')})
                with urllib.request.urlopen(req,timeout=150) as response:raw=response.read();status=response.status
                item['seconds']=round(time.time()-item['started'],3)
                result=json.loads(raw);item['output_chars']=len(str(result.get('choices',[{}])[0].get('message',{}).get('content','')))
                self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            except Exception as exc:
                item['error']=type(exc).__name__;self.send_error(502)
            finally:
                with request_lock:active-=1
    relay=ThreadingHTTPServer(('127.0.0.1',0),Relay);relay.daemon_threads=True
    threading.Thread(target=relay.serve_forever,daemon=True).start()
    for provider in config['providers']:
        if provider['protocol']=='chat':
            targets[provider['model']]=provider['url'].removesuffix('/v1').rstrip('/')
            provider['url']=f'http://127.0.0.1:{relay.server_port}'
    (data/'model-providers.json').write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
    def free_port():
        with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
    port=free_port();url=f'http://127.0.0.1:{port}';token=os.urandom(24).hex()
    env={**os.environ,'FRAMEKEEP_DATA':str(data),'FRAMEKEEP_APP_ROOT':str(ROOT if args.source else args.app),
         'FRAMEKEEP_CANON_REGISTRATION':'0','FRAMEKEEP_PORT':str(port),'FRAMEKEEP_DESKTOP_TOKEN':token}
    if args.source:command=[sys.executable,'-X','utf8',str(ROOT/'选片助手/server.py'),'--port',str(port),'--no-browser']
    else:command=[str(args.app/'engine/framekeep-engine.exe'),'--port',str(port),'--no-browser']
    log=(out/'engine.log').open('w',encoding='utf-8')
    proc=subprocess.Popen(command,env=env,stdout=log,stderr=log,cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW)
    report={'app':str(args.app),'source_engine':args.source,'native':False,'ocr_only':args.ocr_only,
            'release':json.loads((args.app/'update-config.json').read_text(encoding='utf-8'))['releaseId'],
            'groups':[],'http_requests':requests,'errors':[],'passed':False,'out':str(out)}
    def save():
        report['peak_model_requests']=peak;(out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def get(path):
        with urllib.request.urlopen(url+path,timeout=10) as r:return json.load(r)
    browser=None;page=None
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            deadline=time.monotonic()+60
            while True:
                try:
                    if get('/api/ready')['ready']:break
                except OSError:pass
                if time.monotonic()>deadline or proc.poll() is not None:raise RuntimeError('引擎未启动')
                time.sleep(.1)
            browser=pw.chromium.launch(channel='msedge',headless=True);page=browser.new_page(viewport={'width':1680,'height':1050});page.goto(url)
            page.on('pageerror',lambda e:report['errors'].append(str(e)))
            page.wait_for_function('()=>window.FramekeepBoot&&!FramekeepBoot.loading',timeout=60000)
            # 打开真实界面后，使用同一份当前会话，不注入 OCR 或款号答案。
            for index,(code,folder,names) in enumerate(groups):
                cid=next(r['id'] for r in records if r['jpg']==names[-1])
                page.evaluate('(id)=>Framekeep.browsePhoto(id)',cid);page.evaluate('()=>FramekeepIO.flush()')
                start=time.time();item={'sku':code,'capture_id':cid,'names':names,'started':start,'phases':[]};report['groups'].append(item)
                page.locator('#stars [data-rating="2"]').click();page.evaluate('()=>FramekeepIO.flush()')
                item['submit_seconds']=round(time.time()-start,3);save()
                if args.sequential:
                    deadline=time.time()+60
                    while time.time()<deadline:
                        state=get('/api/sku')
                        if any(r['sku']==code and r['status']=='已拍摄' for r in state['rows']):break
                        page.wait_for_timeout(100)
            deadline=time.time()+args.observe;first_four=None
            while time.time()<deadline:
                state=get('/api/sku');now=time.time();count=sum(p['shot'] for p in state['progress'])
                ui=page.locator('#sku-open').inner_text()
                for item in report['groups']:
                    capture=get('/api/parsing/capture?session_id='+sid+'&capture_id='+item['capture_id'])['ocr']
                    phase=(capture['status'],(capture.get('progress') or {}).get('phase'),(capture.get('progress') or {}).get('model'))
                    if not item['phases'] or item['phases'][-1]['value']!=list(phase):item['phases'].append({'seconds':round(now-item['started'],3),'value':list(phase)})
                    row=next((r for r in state['rows'] if r['sku']==item['sku'] and r['status']=='已拍摄'),None)
                    if row:
                        item.setdefault('database_seconds',round(now-item['started'],3))
                        if (row.get('metadata') or {}).get('status')=='applied':item.setdefault('writeback_seconds',round(now-item['started'],3))
                    item['final_status']=capture['status']
                if count==4 and '4 已拍摄' in ui:
                    if first_four is None:
                        first_four=now;report['all_four_visible_seconds']=round(now-min(g['started'] for g in report['groups']),3)
                        page.locator('#sku-open').click();page.screenshot(path=str(out/'all-four-visible.png'))
                    if all('writeback_seconds' in g for g in report['groups']) and now-first_four>=12:break
                save();page.wait_for_timeout(150)
            report['final_count']=count;report['footer']=ui
            page.locator('#sku-open').click()
            if all('writeback_seconds' in g for g in report['groups']):
                page.wait_for_function('()=>document.querySelectorAll("#sku-rows .sku-metadata-status").length===0',timeout=10000)
                report['writeback_hints_cleared']=True
            page.screenshot(path=str(out/'final.png'))
            with sqlite3.connect((data/'sku.sqlite3').as_uri()+'?mode=ro',uri=True) as c:
                report['raw_ocr']=[{'text':r[0],'raw':json.loads(r[1])} for r in c.execute('SELECT text,raw FROM ocr_documents') if 'glm-ocr' in r[1]]
                report['jobs']=[list(r) for r in c.execute('SELECT capture_id,trigger,status FROM jobs')]
            report['pixels_unchanged']=True;report['fields_correct']=True
            for item in report['groups']:
                for name in item['names']:
                    fields=writer.read(photos/name)
                    if fields.get('XMP-fk:SKU')!=item['sku']:report['fields_correct']=False
                    with Image.open(photos/name) as im:
                        if hashlib.sha256(im.convert('RGB').tobytes()).hexdigest()!=pixels[name]:report['pixels_unchanged']=False
            report['originals_unchanged']=all(sha(Path(path))==value for path,value in originals.items())
            report['passed']=bool(count==4 and '4 已拍摄' in ui and report['fields_correct'] and report['pixels_unchanged'] and report['originals_unchanged'] and not report['errors'] and len(requests)==4 and all('writeback_seconds' in g for g in report['groups']))
        except Exception as exc:report['failure']=str(exc);raise
        finally:
            save()
            req=urllib.request.Request(url+'/api/desktop/shutdown',b'{}',{'Content-Type':'application/json','X-Framekeep-Desktop':token})
            try:urllib.request.urlopen(req,timeout=5).close()
            except OSError:pass
            try:proc.wait(timeout=45)
            except subprocess.TimeoutExpired:report['shutdown_pending']=True
            report['exit_code']=proc.poll();save();log.close();relay.shutdown()
            if browser:browser.close()
    print(json.dumps({k:v for k,v in report.items() if k not in ('raw_ocr','jobs')},ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
