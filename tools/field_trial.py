"""20款完整真实区间验收；源目录只读，业务操作仅作用于独立副本。"""
from pathlib import Path
import argparse, hashlib, json, os, shutil, socket, sqlite3, subprocess, sys, threading, time, urllib.request
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
APP=Path('D:/Program Files/Framekeep')
SOURCE=Path(r'\\192.168.0.181\新摄影\细节图\源图片备份（勿动）\现场拍摄小档\2026-11-04')
os.environ.setdefault('FRAMEKEEP_APP_ROOT',str(APP))
sys.path.insert(0,str(ROOT/'选片助手'))
import media_engine as media
from catalog_store import Store
from metadata_writer import MetadataWriter,sha

def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def read(path):return json.loads(path.read_text(encoding='utf-8'))
def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def prepare(out,all_materials=False):
    out.mkdir(parents=True,exist_ok=False);photos=out/'photos';photos.mkdir();data=out/'data';data.mkdir()
    labels=sorted(SOURCE.glob('*.xmp'));labels=labels if all_materials else labels[:20]
    names=sorted(p for p in SOURCE.glob('*.JPG') if all_materials or p.stem<=labels[-1].stem)
    manifest={'source':str(SOURCE),'files':{},'groups':[]};previous=''
    for label in labels:
        tree=ET.fromstring(label.read_bytes());sku=tree.find('.//{https://framekeep.local/ns/asset/1.0/}SKU')
        manifest['groups'].append({'label':label.stem+'.JPG','expected':sku.text if sku is not None else None,
            'names':[p.name for p in names if previous<p.stem<=label.stem],'xmp_sha':sha(label)})
        previous=label.stem
    from PIL import Image
    for i,p in enumerate(names):
        dest=photos/p.name;shutil.copy2(p,dest)
        with Image.open(dest) as im:pixel=hashlib.sha256(im.convert('RGB').tobytes()).hexdigest()
        manifest['files'][p.name]={'sha':sha(dest),'pixels':pixel,'bytes':dest.stat().st_size}
        if i%100==0:print(f'复制与像素基线 {i+1}/{len(names)}',flush=True)
    write(out/'manifest.json',manifest)
    # 不复制含SKU答案的侧车；仅在测试副本清除评级和本软件既有归属，保留其余摄影元数据。
    w=MetadataWriter(ROOT,data)
    command=[str(media.find_exiftool(ROOT)),'-config',str(w.config.resolve()),'-overwrite_original',
             '-XMP-fk:all=','-XMP-xmp:Rating=0','-ext','JPG',str(photos)]
    subprocess.run(command,check=True,stdout=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW);w.close()
    records=[]
    for start in range(0,len(names),150):
        paths=[photos/p.name for p in names[start:start+150]]
        metadata=media.read_metadata(paths,media.find_exiftool(ROOT))
        records.extend(media.capture_record(photos,p,None,m) for p,m in zip(paths,metadata))
    store=Store(data,ROOT);sid=store.ensure_live_session(photos);store.append_live(sid,records);store.activate(sid)
    for group in manifest['groups']:group['capture_id']=next(r['id'] for r in records if r['jpg']==group['label'])
    manifest['session_id']=sid;write(out/'manifest.json',manifest)
    write(data/'desktop-preferences.json',{'close_behavior':'exit','startup_mode':'disabled'})
    shutil.copy2(APP/'data/model-providers.json',data/'model-providers.json')
    write(out/'fixture-ready.json',{'session_id':sid,'count':len(records),'ready_at':time.time()})
    # 禁止演练自动替换生产安装目录。软件更新设置格式由既有实现读取。
    print(f'准备完成：{len(names)}张 / {len(labels)}款',flush=True)

def run(out,source=False,native=False,limit=900,attach=None,observe_only=False):
    manifest=read(out/'manifest.json');data=out/'data';http_port=port();debug_port=port();token=os.urandom(24).hex()
    url=attach or f'http://127.0.0.1:{http_port}'
    env={**os.environ,'FRAMEKEEP_DATA':str(data),'FRAMEKEEP_APP_ROOT':str(ROOT if source else APP),
         'FRAMEKEEP_PORT':str(http_port),'FRAMEKEEP_DESKTOP_TOKEN':token,'FRAMEKEEP_CANON_REGISTRATION':'0',
         'FRAMEKEEP_EXIFTOOL':str(media.find_exiftool(ROOT)),
         'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS':f'--remote-debugging-port={debug_port}'}
    if native:
        cmd=[str(APP/'SHIYING 数字资产.exe')]
        if source:env.update(FRAMEKEEP_ENGINE=sys.executable,FRAMEKEEP_ENGINE_SCRIPT=str(ROOT/'选片助手/server.py'))
    else:cmd=[sys.executable,'-X','utf8',str(ROOT/'tools/field_engine.py'),'--port',str(http_port),'--no-browser'] if source else [str(APP/'engine/framekeep-engine.exe'),'--port',str(http_port),'--no-browser']
    report={'source':source,'native':native,'installed_release':read(APP/'update-config.json')['releaseId'],'groups':[],
            'errors':[],'started':time.time(),'url':url,'debug_port':debug_port,'passed':False}
    if observe_only:
        report=read(out/'run.json');report['url']=url;report.setdefault('resumed',[]).append({'at':time.time(),'url':url})
    write(out/'run.json',report)
    report['attached_installed_desktop']=bool(attach)
    log=(out/'engine.log').open('a',encoding='utf-8');proc=None if attach else subprocess.Popen(cmd,env=env,cwd=ROOT,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    report['pid']=proc.pid if proc else None
    def get(path):
        with urllib.request.urlopen(url+path,timeout=5) as r:return json.load(r)
    def save():write(out/'run.json',report)
    from playwright.sync_api import sync_playwright
    try:
        deadline=time.time()+90
        while time.time()<deadline:
            try:
                if get('/api/ready')['ready']:break
            except (OSError,ValueError):pass
            if proc and proc.poll() is not None:raise RuntimeError('启动进程退出')
            time.sleep(.1)
        else:raise TimeoutError('启动超时')
        if not observe_only:report['engine_ready_seconds']=time.time()-report['started']
        save()
        with sync_playwright() as pw:
            if native:
                browser=pw.chromium.connect_over_cdp(f'http://127.0.0.1:{debug_port}',timeout=20000)
                context=browser.contexts[0];page=next((p for p in context.pages if str(http_port) in p.url),context.pages[0])
            else:
                browser=pw.chromium.launch(channel='msedge',headless=True);page=browser.new_page(viewport={'width':1680,'height':1050});page.goto(url)
            page.on('pageerror',lambda e:report['errors'].append(str(e)))
            page.wait_for_function('()=>window.FramekeepBoot&&!FramekeepBoot.loading',timeout=90000)
            if not observe_only:report['workbench_ready_seconds']=time.time()-report['started']
            save()
            page.evaluate('''()=>{
                document.addEventListener('keydown',e=>{if(e.key==='2')window.__fieldKey={started:performance.now()}},true);
                document.addEventListener('framekeep:rating-changed',()=>{if(window.__fieldKey){window.__fieldKey.feedback_ms=performance.now()-window.__fieldKey.started;window.__fieldKey.text=document.querySelector('#sku-open').innerText}});
            }''')
            for index,group in enumerate([] if observe_only else manifest['groups']):
                page.evaluate('async(id)=>{if(window.FRAMEKEEP_SESSION.browser?.paged)await FramekeepPaging.goto(id);Framekeep.browsePhoto(id);await FramekeepIO.flush()}',group['capture_id'])
                page.wait_for_function('(id)=>Framekeep.remoteTarget().photoId===id',arg=group['capture_id'],timeout=15000)
                item={**group,'pressed':time.time()};report['groups'].append(item)
                # 真实页面键盘事件，与用户按2相同；只定位图片，不填入OCR答案。
                page.locator('body').click(position={'x':800,'y':60});page.keyboard.press('2')
                page.evaluate('()=>FramekeepIO.flush()')
                item['save_seconds']=time.time()-item['pressed'];item['key_feedback']=page.evaluate('()=>window.__fieldKey');item['tab_after_save']=page.locator('#sku-open').inner_text();save()
                print(f"标星 {index+1}/{len(manifest['groups'])} {group['label']} 保存 {item['save_seconds']:.3f}s {item['tab_after_save']}",flush=True)
                page.wait_for_timeout(200)
            deadline=time.time()+limit;last_log=0
            while time.time()<deadline:
                now=time.time()
                with sqlite3.connect(data/'sku.sqlite3',timeout=30) as c:
                    c.row_factory=sqlite3.Row;jobs=[dict(r) for r in c.execute('SELECT capture_id,status,result,created,updated,reason FROM jobs')]
                    events=c.execute('SELECT count(*) FROM archive_events WHERE delivered=0').fetchone()[0]
                with sqlite3.connect(data/'asset_archive.sqlite3',timeout=30) as c:
                    c.row_factory=sqlite3.Row
                    statuses=[dict(r) for r in c.execute("""SELECT m.path,m.status,m.reason FROM metadata_jobs m
                        JOIN assets a ON a.id=m.asset AND a.revision=m.revision
                        WHERE coalesce(json_extract(m.fields,'$._descriptive'),0)=0 AND m.id=(
                        SELECT n.id FROM metadata_jobs n WHERE n.asset=m.asset AND n.revision=m.revision
                        AND n.file=m.file AND n.path=m.path AND coalesce(json_extract(n.fields,'$._descriptive'),0)=0
                        ORDER BY n.created DESC,n.rowid DESC LIMIT 1)""")]
                for item in report['groups']:
                    job=next((j for j in reversed(jobs) if j['capture_id']==item['capture_id']),None)
                    if job:
                        item['status']=job['status'];item['reason']=job['reason'];item['ocr_result']=json.loads(job['result'])
                        if job['status'] in ('matched','confirmed','review','failed'):item.setdefault('ocr_result_seconds',job['updated']-item['pressed'])
                        if job['status']=='confirmed':item.setdefault('database_seconds',job['updated']-item['pressed'])
                    own=[s for s in statuses if Path(s['path']).name in item['names']]
                    item['metadata_counts']={s:sum(r['status']==s for r in own) for s in set(r['status'] for r in own)}
                    if len({r['path'] for r in own})==len(item['names']) and all(r['status']=='applied' for r in own):item.setdefault('writeback_seconds',now-item['pressed'])
                    else:item.pop('writeback_seconds',None) # 人工改款产生新版本后，旧完成时间不能冒充本次完成。
                report['pending_events']=events;report['metadata_total']=len(statuses);save()
                if now-last_log>20:
                    print('处理中', {s:sum(j['status']==s for j in jobs) for s in set(j['status'] for j in jobs)}, 'metadata', {s:sum(r['status']==s for r in statuses) for s in set(r['status'] for r in statuses)},flush=True);last_log=now
                if all('writeback_seconds' in g for g in report['groups']):break
                page.wait_for_timeout(250)
            page.locator('#sku-open').click();page.wait_for_timeout(1200);page.screenshot(path=str(out/'progress.png'))
            report['tab_final']=page.locator('#sku-open').inner_text()
            report['api_final']=get('/api/sku');save()
            if not native:browser.close()
    except Exception as exc:
        report['failure']=str(exc);save();raise
    finally:
        if proc:
            try:
                req=urllib.request.Request(url+'/api/desktop/shutdown',b'{}',{'Content-Type':'application/json','X-Framekeep-Desktop':token});urllib.request.urlopen(req,timeout=5).close()
            except OSError:pass
            try:proc.wait(timeout=45)
            except subprocess.TimeoutExpired:report['shutdown_pending']=True
        report['exit_code']=proc.poll() if proc else None;save();log.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run']);p.add_argument('--out',type=Path,required=True);p.add_argument('--source',action='store_true');p.add_argument('--native',action='store_true');p.add_argument('--attach');p.add_argument('--all',action='store_true');p.add_argument('--observe-only',action='store_true');p.add_argument('--limit',type=int,default=900);a=p.parse_args()
    if a.action=='prepare':prepare(a.out.resolve(),a.all)
    else:run(a.out.resolve(),a.source,a.native,a.limit,a.attach,a.observe_only)
