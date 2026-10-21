"""用户可操作的实拍演示：正式桌面程序、隔离副本和独立文件读回核验。"""
import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import urllib.request
import webbrowser

from catalog_store import Store
from asset_archive import AssetArchive
from raw_reconcile import RawReconcile
from metadata_writer import MetadataWriter,raw_integrity,sha

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'build/真实数据可操作演示'
SOURCE=Path(r'X:\细节图\源图片备份（勿动）\2026\2026-10-20')
ENGINE_PORT=18769
PAGE_PORT=18770
ENGINE_URL=f'http://127.0.0.1:{ENGINE_PORT}'
PAGE_URL=f'http://127.0.0.1:{PAGE_PORT}'

def read_json(url,timeout=2):
    with urllib.request.urlopen(url,timeout=timeout) as response:return json.load(response)

def write_json(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

class Demo:
    def __init__(self):
        BASE.mkdir(parents=True,exist_ok=True);self.lock=threading.RLock();self.engine=None
        self.check={'status':'idle','done':0,'total':20,'rows':[]};self.manifest=self.baseline()
        self.operation={'status':'idle','message':'','done':0,'total':10}
        current=BASE/'current.json'
        self.run=Path(json.loads(current.read_text(encoding='utf-8'))['run']) if current.exists() else self.prepare()
    def baseline(self):
        folder=BASE/'原始基准';manifest=BASE/'真实来源与哈希.json'
        if manifest.exists():return json.loads(manifest.read_text(encoding='utf-8'))
        raws=[p for p in sorted(SOURCE.glob('*.CR3')) if p.with_suffix('.JPG').is_file()][-10:]
        if len(raws)!=10:raise ValueError('指定真实拍摄目录中未找到 10 对 CR3/JPG')
        folder.mkdir(exist_ok=True);rows=[]
        for raw in raws:
            for source in (raw,raw.with_suffix('.JPG')):
                before=sha(source);target=folder/source.name;shutil.copy2(source,target)
                if sha(target)!=before:raise ValueError('真实副本哈希不一致：'+source.name)
                rows.append({'source':str(source),'baseline':str(target),'name':source.name,'bytes':source.stat().st_size,'sha256':before})
        write_json(manifest,rows);return rows
    def prepare(self):
        index=max([int(p.name.split('-')[-1]) for p in BASE.glob('round-*') if p.is_dir()]+[0])+1
        run=BASE/f'round-{index:03d}';onsite=run/'现场JPG-真实副本';target=run/'最终归档-真实副本/存储卡/DCIM'
        onsite.mkdir(parents=True);target.mkdir(parents=True)
        for row in self.manifest:
            baseline=Path(row['baseline'])
            if sha(baseline)!=row['sha256']:raise ValueError('基准被修改：'+row['name'])
            shutil.copy2(baseline,target/row['name'])
            if baseline.suffix.lower()=='.jpg':shutil.copy2(baseline,onsite/row['name'])
        store=Store(run/'data',ROOT);job=store.scan(str(onsite));deadline=time.monotonic()+30
        while store.jobs[job]['status'] not in ('completed','failed') and time.monotonic()<deadline:time.sleep(.05)
        if store.jobs[job]['status']!='completed':raise ValueError('真实照片索引失败：'+str(store.jobs[job]))
        sid=store.jobs[job]['session_id'];session=store.session(sid);records=store.records(sid)
        # 明确保留原图真实星级，不使用末帧推荐，也不填造 SKU。
        state={**session['state'],'overrides':{r['id']:r.get('existingRating',0) for r in records},'view':'grid','filter':'all'}
        store.save(sid,session['revision'],state);archive=AssetArchive(store,autostart=False);link=RawReconcile(archive,autostart=False)
        link.index_sources(sid);link.add_target(str(target.parents[1]));link.close();archive.close()
        with sqlite3.connect(run/'data/sku.sqlite3') as c:
            c.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)')
            c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('task_controls',json.dumps({'mode':'manual','paused':False})))
        write_json(BASE/'current.json',{'run':str(run),'source_session':sid})
        write_json(run/'演示步骤记录.json',{'phase':0,'events':[]})
        (run/'操作说明.txt').write_text('真实数据可操作演示\n\n打开 http://127.0.0.1:18770，按四步按钮实际执行：\n①随机标星，包含 1 星/2 星组；②随机演示 SKU 与款图备案；③写入最终 JPG/RAW；④独立读文件、图像数据与原片哈希核验。\n可打开同一正式引擎的工作台查看真实值。随机星级和带“演示SKU”前缀的款号为用户授权的演示值，不冒充原有业务数据。\n所有修改限于本轮副本和数据库；X 盘原件与原始基准只读。可开始新一轮，旧轮次保留。\n',encoding='utf-8')
        return run
    def online(self):
        try:
            state=read_json(ENGINE_URL+'/api/state')
            return state if state.get('session') and str(self.run) in state['session']['root'] else None
        except (OSError,ValueError):return None
    def open(self):
        if self.online():return {'ok':True,'workbench':ENGINE_URL}
        env={k:v for k,v in os.environ.items() if not k.startswith('FRAMEKEEP_')}
        secret=secrets.token_urlsafe(24);app=ROOT/'dist/末帧选片'
        env.update(FRAMEKEEP_DATA=str(self.run/'data'),FRAMEKEEP_APP_ROOT=str(app),FRAMEKEEP_CONFIG=str(self.run/'no-provider-config.md'),FRAMEKEEP_DESKTOP_TOKEN=secret)
        with (self.run/'引擎日志.txt').open('a',encoding='utf-8') as log:self.engine=subprocess.Popen([str(app/'engine/framekeep-engine.exe'),'--port',str(ENGINE_PORT),'--no-browser'],cwd=app,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        write_json(BASE/'engine-control.json',{'pid':self.engine.pid,'secret':secret})
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if self.online():return {'ok':True,'workbench':ENGINE_URL}
            if self.engine.poll() is not None:break
            time.sleep(.1)
        raise ValueError('演示引擎未启动，请检查本轮引擎日志')
    def stop(self):
        control=BASE/'engine-control.json'
        if not self.online():return
        if not control.exists():raise ValueError('请先关闭现有演示工作台')
        saved=json.loads(control.read_text(encoding='utf-8'));health=read_json(ENGINE_URL+'/api/health')
        if health['process_id']!=saved['pid']:raise ValueError('当前引擎不属于本演示启动器')
        request=urllib.request.Request(ENGINE_URL+'/api/desktop/shutdown',data=b'{}',headers={'X-Framekeep-Desktop':saved['secret']},method='POST')
        with urllib.request.urlopen(request,timeout=5) as response:response.read()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline and self.online():time.sleep(.1)
        if self.engine:self.engine.wait(timeout=15)
        else:subprocess.run(['powershell.exe','-NoProfile','-Command',f'Wait-Process -Id {int(saved["pid"])} -Timeout 15 -ErrorAction SilentlyContinue'],creationflags=subprocess.CREATE_NO_WINDOW,capture_output=True,timeout=20)
    def plan(self):
        path=self.run/'演示步骤记录.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'phase':0,'events':[]}
    def event(self,phase,message,details=None):
        plan=self.plan();plan['phase']=phase;plan['events'].append({'time':datetime.now().astimezone().isoformat(),'message':message,'details':details});write_json(self.run/'演示步骤记录.json',plan)
    def engine_api(self,path,body):
        state=self.source_state();data={'session_id':state['session']['id'],**body}
        request=urllib.request.Request(ENGINE_URL+path,data=json.dumps(data,ensure_ascii=False).encode('utf-8'),headers={'Content-Type':'application/json','X-Framekeep-Token':state['request_token']},method='POST')
        with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)
    def source_state(self):
        sid=json.loads((BASE/'current.json').read_text(encoding='utf-8'))['source_session']
        return read_json(ENGINE_URL+'/api/state?session_id='+sid)
    def stars(self):
        with self.lock:
            if self.plan()['phase']!=0:raise ValueError('本轮已经标星；可开始新一轮重新随机')
            state=self.source_state();ratings=[1,1,1,2,2,2,3,4,5,random.choice([3,4,5])];random.SystemRandom().shuffle(ratings)
            records=state['catalog'];overrides={r['id']:n for r,n in zip(records,ratings)}
            for attempt in range(3):
                state=self.source_state();saved={**state['session']['state'],'overrides':overrides}
                try:self.engine_api('/api/session',{'revision':state['session']['revision'],'state':saved});break
                except urllib.error.HTTPError as exc:
                    if exc.code!=409 or attempt==2:raise
            self.event(1,'随机标星已真实保存，包含 1 星组和 2 星组',{r['jpg']:n for r,n in zip(records,ratings)})
        return {'ok':True}
    def sku(self):
        with self.lock:
            if self.plan()['phase']!=1 or self.operation['status']=='running':raise ValueError('请先完成随机标星，再设置 SKU')
            self.operation={'status':'running','message':'正在提交款图备案','done':0,'total':10};threading.Thread(target=self._sku,daemon=True).start()
        return {'ok':True}
    def _sku(self):
        try:
            state=self.source_state();saved=state['session']['state']['overrides'];groups={};nonce=secrets.randbelow(9000)+1000
            for r in state['catalog']:
                group='A' if saved[r['id']]==1 else 'B' if saved[r['id']]==2 else 'C';groups.setdefault(group,[]).append(r['id'])
            operations=[];assigned={}
            for group,ids in groups.items():
                sku=f'演示SKU-{nonce}-{group}';preview=self.engine_api('/api/metadata/preview',{'capture_ids':ids,'sku':sku,'scope':'款图','overwrite':True});result=self.engine_api('/api/metadata/apply',{'preview_id':preview['preview_id']});operations.append(result['operation']['id']);assigned[sku]=len(ids)
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                jobs=[j for oid in operations for j in read_json(ENGINE_URL+'/api/metadata/operations/'+oid)['jobs']]
                failed=[j for j in jobs if j['status']=='failed']
                if failed:raise ValueError(failed[0]['reason'])
                done=sum(j['status']=='applied' for j in jobs);self.operation={'status':'running','message':'正在把演示 SKU / 款图写入现场 JPG','done':done,'total':10}
                if done==10:break
                time.sleep(.2)
            else:raise ValueError('现场 SKU 写入超时')
            self.event(2,'随机演示 SKU 与款图类型已写入现场副本',assigned);self.operation={'status':'completed','message':'现场 SKU 备案完成','done':10,'total':10}
        except Exception as exc:self.operation={**self.operation,'status':'failed','message':str(exc)}
    def sync(self):
        if self.plan()['phase']!=2:raise ValueError('请先完成随机星级与 SKU 设置')
        folder=str(self.run/'最终归档-真实副本');target=next(t for t in read_json(ENGINE_URL+'/api/raw-sync')['targets'] if t['folder'].casefold()==folder.casefold());self.engine_api('/api/raw-sync/sync-target',{'target_id':target['id']});self.event(3,'已启动最终 JPG / RAW 元数据写入');return {'ok':True}
    def expected(self):
        db=self.run/'data/framekeep.sqlite3';archive=self.run/'data/asset_archive.sqlite3'
        with sqlite3.connect('file:'+db.as_posix()+'?mode=ro',uri=True) as c:
            c.row_factory=sqlite3.Row
            session=c.execute("SELECT * FROM sessions WHERE root=?",(str(self.run/'现场JPG-真实副本'),)).fetchone()
            saved=json.loads(session['state']);rows=[]
            with sqlite3.connect('file:'+archive.as_posix()+'?mode=ro',uri=True) as a:
                for capture in c.execute('SELECT id,data FROM captures WHERE session=? ORDER BY position',(session['id'],)):
                    record=json.loads(capture['data']);asset=a.execute('SELECT a.id,a.sku,a.scope FROM assets a JOIN catalog_links l ON l.asset=a.id WHERE l.session=? AND l.capture=?',(session['id'],capture['id'])).fetchone()
                    rows.append({'name':Path(record['jpg']).stem,'time':record['time'],'camera':record['camera'],'rating':max(0,saved.get('overrides',{}).get(capture['id'],record.get('existingRating',0))),'sku':asset[1] if asset else '', 'scope':asset[2] if asset else '', 'asset':asset[0] if asset else ''})
        return rows
    def info(self):
        online=self.online();batches=[]
        if online:
            try:batches=read_json(ENGINE_URL+'/api/parsing')['metadata']['batches']
            except (OSError,ValueError):pass
        metrics=self.run/'data/desktop-metrics.json';desktop_pid=json.loads(metrics.read_text(encoding='utf-8')).get('desktop_pid') if metrics.exists() else None
        previous=any((p/'独立核验报告.json').exists() for p in BASE.glob('round-*') if p!=self.run)
        return {'application':'framekeep-real-demo','root':str(ROOT),'run':str(self.run),'source':str(SOURCE),'target':str(self.run/'最终归档-真实副本'),'online':bool(online),'desktop_pid':desktop_pid,'photos':self.expected(),'batches':batches,'check':self.check,'plan':self.plan(),'operation':self.operation,'previous_report':previous,'report_exists':(self.run/'独立核验报告.json').exists()}
    def verify(self):
        with self.lock:
            if self.check['status']=='running':return {'ok':True}
            if self.plan()['phase']<3:raise ValueError('请先按步骤写入最终目录，再独立核验')
            if not self.online():raise ValueError('请先打开演示工作台')
            if any(b['status'] in ('indexing','matching','writing','verifying') for b in self.info()['batches']):raise ValueError('请等当前写入和最终校验完成，再独立核验')
            self.check={'status':'running','done':0,'total':20,'rows':[],'message':'正在直接读取文件'}
            threading.Thread(target=self._verify,daemon=True).start()
        return {'ok':True}
    def _verify(self):
        run=self.run;expected=self.expected();config=run/'独立核验配置';config.mkdir(exist_ok=True);writer=MetadataWriter(ROOT,config);manifest={r['name']:r for r in self.manifest};rows=[];originals=True
        try:
            for want in expected:
                row={'name':want['name'],'expected':want,'files':{},'errors':[]}
                for ext in ('.JPG','.CR3'):
                    name=want['name']+ext;target=run/'最终归档-真实副本/存储卡/DCIM'/name;origin=manifest[name];baseline=Path(origin['baseline'])
                    actual=writer.read(target);values={'rating':actual.get('XMP-xmp:Rating',0),'sku':actual.get('XMP-fk:SKU',''),'scope':actual.get('XMP-fk:ShootType',''),'asset':str(actual.get('XMP-fk:AssetId','')).replace('-',''),'file_id':actual.get('XMP-fk:FileId','')}
                    row['files'][ext[1:]]=values
                    if any(values[k]!=want[k] for k in ('rating','sku','scope')):row['errors'].append(ext+' 星级/SKU/类型与已保存现场记录不一致')
                    if not want['asset'] or values['asset']!=want['asset'] or not values['file_id']:row['errors'].append(ext+' 尚未写入本次资产身份')
                    if sha(origin['source'])!=origin['sha256'] or sha(baseline)!=origin['sha256']:originals=False;row['errors'].append(ext+' 原件/基准哈希发生变化')
                    if ext=='.CR3':
                        integrity=raw_integrity(target);row['raw_integrity']=integrity
                        if integrity!=raw_integrity(baseline):row['errors'].append('RAW 感光/预览/mdat 数据变化')
                    else:
                        from PIL import Image
                        with Image.open(baseline) as a,Image.open(target) as b:
                            if a.size!=b.size or a.convert('RGB').tobytes()!=b.convert('RGB').tobytes():row['errors'].append('JPG 像素变化')
                    self.check={**self.check,'done':self.check['done']+1,'message':name}
                row['ok']=not row['errors'];rows.append(row);self.check={**self.check,'rows':rows.copy()}
            report={'time':datetime.now().astimezone().isoformat(),'run':str(run),'real_source':str(SOURCE),'captures':len(rows),'files':len(rows)*2,'passed':all(r['ok'] for r in rows),'originals_unchanged':originals,'rows':rows}
            if self.expected()!=expected:raise ValueError('核验期间现场记录发生变化，请等待写入完成后重新核验')
            write_json(run/'独立核验报告.json',report)
            self.check={**self.check,'status':'completed','passed':report['passed'],'originals_unchanged':originals,'message':'核验完成'}
            if report['passed']:self.event(4,'独立核验通过：10 组 / 20 文件，原片与图像数据保持')
        except Exception as exc:self.check={**self.check,'status':'failed','message':str(exc)}
    def new_round(self):
        with self.lock:
            if self.check['status']=='running' or self.operation['status']=='running' or any(b['status'] in ('indexing','matching','writing','verifying') for b in self.info()['batches']):raise ValueError('请等待当前操作完成；旧轮次会保留')
            self.stop();self.run=self.prepare();self.check={'status':'idle','done':0,'total':20,'rows':[]};self.operation={'status':'idle','done':0,'total':10,'message':''};self.open()
        return {'ok':True}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,value,kind='application/json; charset=utf-8',status=200):
        data=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode('utf-8');self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
    def trusted(self):return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'
    def do_GET(self):
        if not self.trusted():return self.reply({'error':'仅供本机演示使用'},status=403)
        path=urlsplit(self.path).path
        try:
            if path=='/':return self.reply((ROOT/'演示方案/真实数据验证/index.html').read_bytes(),'text/html; charset=utf-8')
            if path=='/api/info':return self.reply(self.server.demo.info())
            if path=='/api/report':
                report=self.server.demo.run/'独立核验报告.json'
                return self.reply(json.loads(report.read_text(encoding='utf-8')) if report.exists() else {'message':'尚未核验'})
            if path=='/api/previous-report':
                reports=[p/'独立核验报告.json' for p in sorted(BASE.glob('round-*'),reverse=True) if p!=self.server.demo.run and (p/'独立核验报告.json').exists()]
                return self.reply(json.loads(reports[0].read_text(encoding='utf-8')) if reports else {'message':'尚无之前轮次的报告'})
            if path.startswith('/photo/'):
                name=path.rsplit('/',1)[-1]+'.JPG';row=next(r for r in self.server.demo.manifest if r['name']==name)
                return self.reply(Path(row['baseline']).read_bytes(),'image/jpeg')
            return self.reply({'error':'不存在'},status=404)
        except Exception as exc:return self.reply({'error':str(exc)},status=400)
    def do_POST(self):
        if not self.trusted() or self.headers.get('Origin',PAGE_URL)!=PAGE_URL:return self.reply({'error':'请从本机演示页操作'},status=403)
        demo=self.server.demo
        try:
            if self.path=='/api/open':return self.reply(demo.open())
            if self.path=='/api/stars':return self.reply(demo.stars())
            if self.path=='/api/sku':return self.reply(demo.sku())
            if self.path=='/api/sync':return self.reply(demo.sync())
            if self.path=='/api/verify':return self.reply(demo.verify())
            if self.path=='/api/new-round':return self.reply(demo.new_round())
            if self.path=='/api/folder':subprocess.Popen(['explorer.exe',str(demo.run)]);return self.reply({'ok':True})
            return self.reply({'error':'不存在'},status=404)
        except Exception as exc:return self.reply({'error':str(exc)},status=400)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true');parser.add_argument('--no-open',action='store_true');args=parser.parse_args()
    if args.serve:
        demo=Demo();server=ThreadingHTTPServer(('127.0.0.1',PAGE_PORT),Handler);server.daemon_threads=True;server.demo=demo
        threading.Thread(target=demo.open,daemon=True).start();server.serve_forever();return
    BASE.mkdir(parents=True,exist_ok=True)
    try:existing=read_json(PAGE_URL+'/api/info');assert existing['application']=='framekeep-real-demo' and existing['root']==str(ROOT)
    except (OSError,ValueError,AssertionError):
        with (BASE/'demo-server.log').open('a',encoding='utf-8') as log:subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve'],cwd=ROOT,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            try:
                info=read_json(PAGE_URL+'/api/info')
                if info.get('application')=='framekeep-real-demo':break
            except (OSError,ValueError):time.sleep(.2)
        else:raise RuntimeError('演示服务未启动，请查看 build/真实数据可操作演示/demo-server.log')
    if not args.no_open:webbrowser.open(PAGE_URL)
    print(PAGE_URL)

if __name__=='__main__':main()
