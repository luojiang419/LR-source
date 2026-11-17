"""只强制结束本任务清单中拥有的隔离桌面/引擎进程，保存重启对照事实。"""
from pathlib import Path
import hashlib,json,sqlite3,sys,time,urllib.request
import psutil
ROOT=Path(__file__).resolve().parents[1];out=Path(sys.argv[1]).resolve();assert out.is_relative_to(ROOT/'build/field-trial')
data=out/'data';launch=json.loads((out/'desktop-launch.json').read_text(encoding='utf8'));desktop=psutil.Process(launch['pid'])
assert Path(desktop.exe()).name=='SHIYING 数字资产.exe'
engines=[p for p in desktop.children() if p.name()=='framekeep-engine.exe']
assert len(engines)==1 and Path(engines[0].exe()).is_relative_to(ROOT/'build')
engine=engines[0];children=engine.children(recursive=True)
until=time.time()+40
while True:
    with urllib.request.urlopen(launch['url']+'/api/metadata/queue',timeout=5) as response:diagnostics=json.load(response)
    if diagnostics['worker']['phase']=='preparing':break
    if time.time()>until:raise RuntimeError('未捕获写入准备阶段，未终止进程')
    time.sleep(.02)
with sqlite3.connect(data/'sku.sqlite3',timeout=30) as s,sqlite3.connect(data/'asset_archive.sqlite3',timeout=30) as a:
    report={'before_at':time.time(),'desktop_pid':desktop.pid,'engine_pid':engine.pid,'observed_worker':diagnostics['worker'],
            'projects':list(s.execute('SELECT id,name FROM projects')),'choices':list(s.execute('SELECT session_id,capture_id,sku FROM ocr_choices')),
            'jobs_before':dict(s.execute('SELECT status,count(*) FROM jobs GROUP BY status')),'metadata_before':dict(a.execute('SELECT status,count(*) FROM metadata_jobs GROUP BY status'))}
for name in ('desktop-launch.json','data/desktop-metrics.json'):
    path=out/name
    if path.exists():(out/('initial-'+path.name)).write_bytes(path.read_bytes())
(out/'crash-recovery.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
engine.kill();desktop.kill()
for process in children:
    try:process.kill()
    except psutil.NoSuchProcess:pass
psutil.wait_procs([engine,desktop,*children],timeout=10)
report['killed_at']=time.time();(out/'crash-recovery.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps({'confirmed_before':len(report['choices']),'phase':report['observed_worker']['phase'],'metadata_before':report['metadata_before']},ensure_ascii=False))
