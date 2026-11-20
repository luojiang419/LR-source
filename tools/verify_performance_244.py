"""任务244：生产副本冷缓存、原生 WebView2 及缺失真实样片验收。

snapshot 只复制生产数据到 D:/Framekeep-perf-244，原程序须退出。
cold --reuse-snapshot 每轮清除系统文件缓存和 standby，暂时启用的权限随后恢复；
仅在副本启动引擎，禁止照片写回和启动后的业务扫描，保留构造阶段的真实开销。
fixture/launch 创建独立原生窗口与7500张真实JPG副本；界面输入使用桌面控制工具。
monitor --pid <本轮原生PID> 只读采集进程组CPU/RSS及其GPU Engine计数器。
real_tests/report 将实际结果落入 ignored build/perf-analysis/acceptance-244。
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager, closing
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'build/perf-analysis/acceptance-244'
PRODUCTION=Path('D:/Program Files/Framekeep/data')
DISK=Path('D:/Framekeep-perf-244')


def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')


@contextmanager
def privileges(*names):
    adv=C.WinDLL('advapi32',use_last_error=True);kernel=C.WinDLL('kernel32',use_last_error=True)
    class LUID(C.Structure):_fields_=[('LowPart',W.DWORD),('HighPart',W.LONG)]
    class TP(C.Structure):_fields_=[('Count',W.DWORD),('Luid',LUID),('Attributes',W.DWORD)]
    adv.OpenProcessToken.argtypes=[W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)]
    adv.LookupPrivilegeValueW.argtypes=[W.LPCWSTR,W.LPCWSTR,C.POINTER(LUID)]
    token=W.HANDLE();previous=[]
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(),0x28,C.byref(token)):raise C.WinError(C.get_last_error())
    try:
        for name in names:
            value=TP(1,LUID(),2);old=TP();size=W.DWORD()
            if not adv.LookupPrivilegeValueW(None,name,C.byref(value.Luid)):raise C.WinError(C.get_last_error())
            C.set_last_error(0)
            if not adv.AdjustTokenPrivileges(token,False,C.byref(value),C.sizeof(old),C.byref(old),C.byref(size)) or C.get_last_error():raise C.WinError(C.get_last_error())
            previous.append(old)
        yield
    finally:
        for old in reversed(previous):adv.AdjustTokenPrivileges(token,False,C.byref(old),0,None,None)
        kernel.CloseHandle(token)


def clear_file_cache():
    kernel=C.WinDLL('kernel32',use_last_error=True);nt=C.WinDLL('ntdll')
    kernel.SetSystemFileCacheSize.argtypes=[C.c_size_t,C.c_size_t,W.DWORD]
    nt.NtSetSystemInformation.argtypes=[W.ULONG,C.c_void_p,W.ULONG];nt.NtSetSystemInformation.restype=W.LONG
    with privileges('SeIncreaseQuotaPrivilege','SeProfileSingleProcessPrivilege'):
        if not kernel.SetSystemFileCacheSize(C.c_size_t(-1),C.c_size_t(-1),0):raise C.WinError(C.get_last_error())
        command=W.ULONG(4)  # MemoryPurgeStandbyList; 不清空用户进程的工作集。
        status=nt.NtSetSystemInformation(80,C.byref(command),C.sizeof(command))
        if status!=0:raise OSError(f'NtSetSystemInformation: 0x{status&0xffffffff:08x}')
    return {'system_file_cache_trimmed':True,'standby_purged':True,'at':time.time()}


def copy_file(source,target):
    source=Path(source);target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():raise FileExistsError(target)
    before=(source.stat().st_size,source.stat().st_mtime_ns)
    callback_type=C.WINFUNCTYPE(W.DWORD,C.c_longlong,C.c_longlong,C.c_longlong,C.c_longlong,W.DWORD,W.DWORD,W.HANDLE,W.HANDLE,C.c_void_p)
    reported=[0]
    def progress(total,done,*unused):
        if time.monotonic()-reported[0]>10:
            reported[0]=time.monotonic();print(json.dumps({'copy':source.name,'percent':round(done/max(1,total)*100,1)},ensure_ascii=False),flush=True)
        return 0
    callback=callback_type(progress);cancel=W.BOOL(False)
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CopyFileExW.argtypes=[W.LPCWSTR,W.LPCWSTR,callback_type,C.c_void_p,C.POINTER(W.BOOL),W.DWORD]
    if not kernel.CopyFileExW(str(source),str(target),callback,None,C.byref(cancel),0x1001):raise C.WinError(C.get_last_error())
    after=(source.stat().st_size,source.stat().st_mtime_ns)
    if before!=after or target.stat().st_size!=before[0]:raise RuntimeError('复制期间源库变化，不能使用不一致快照')
    return {'name':source.name,'bytes':before[0],'source_mtime_ns':before[1],'copy_no_buffering':True}


def snapshot(args):
    import psutil
    active=[p.pid for p in psutil.process_iter(['name']) if (p.info['name'] or '').lower() in ('framekeep-engine.exe','shiying 数字资产.exe','末帧选片.exe')]
    if active:raise RuntimeError(f'生产程序仍在运行: {active}')
    data=DISK/'snapshot';data.mkdir(parents=True,exist_ok=False);files=[]
    for name in ('asset_catalog.sqlite3','asset_archive.sqlite3','framekeep.sqlite3','sku.sqlite3','vision_analysis.sqlite3','archive-browser.sqlite3'):
        source=PRODUCTION/name
        if not source.is_file():continue
        files.append(copy_file(source,data/name))
        wal=source.with_name(name+'-wal')
        if wal.is_file() and wal.stat().st_size:files.append(copy_file(wal,data/wal.name))
    config=PRODUCTION/'reference-library/config.json'
    if config.exists():
        value=json.loads(config.read_text(encoding='utf8'));value.update(keys=[],peers=[],sharing={'enabled':False,'port':8770})
        save(data/'reference-library/config.json',value)
        save(OUTPUT/'reference-config.json',value)
    save(OUTPUT/'snapshot.json',{'data':str(data),'files':files,'source':str(PRODUCTION),'created':time.time()})


def configure_runtime(engine_root):
    sys.path.insert(0,str(engine_root/'选片助手'));sys.path.insert(1,str(ROOT/'更新系统'))
    import gpu_media,video_preview
    shared=ROOT/'.runtime/gpu'
    if shared.is_dir():gpu_media.runtime_folder=lambda:shared
    original=video_preview.find_tool
    video_preview.find_tool=lambda name,app_root=None:original(name,app_root or ROOT)


def serve(args):
    data=Path(os.environ['FRAMEKEEP_DATA']).resolve()
    if not (data.is_relative_to(DISK.resolve()) or data.is_relative_to(OUTPUT.resolve())):raise ValueError('验收只能使用隔离资料目录')
    configure_runtime(args.engine_root)
    from metadata_writer import MetadataWriter
    def forbidden(*unused,**options):raise RuntimeError('任务244验收禁止改写照片')
    MetadataWriter.write=forbidden;MetadataWriter.commit=forbidden;MetadataWriter.commit_sidecars=forbidden
    if args.profile=='startup':
        from startup_tasks import StartupTasks
        from asset_archive import AssetArchive
        StartupTasks.start=lambda self:None
        AssetArchive.start_metadata=lambda self:None
    import server
    server.WEB=args.engine_root/'演示方案'
    if args.profile=='native':install_telemetry(server,data)
    save(data/'acceptance-control.json',{'pid':os.getpid(),'desktop_token':os.environ.get('FRAMEKEEP_DESKTOP_TOKEN','')})
    sys.argv=['server.py','--port',str(args.port),'--no-browser']
    server.main()


def install_telemetry(server,data):
    script=(ROOT/'tools/performance_244_telemetry.js').read_bytes()
    original_get=server.Handler.do_GET;original_post=server.Handler.do_POST;original_reply=server.Handler.reply
    guard=threading.Lock()
    def reply(self,value,status=200,kind='application/json; charset=utf-8',filename=None):
        if isinstance(value,bytes) and kind.startswith('text/html'):
            value=value.replace(b'</head>',b'<script src="/__perf244.js"></script></head>',1)
        return original_reply(self,value,status,kind,filename)
    def get(self):
        if self.path in ('/','/index.html') and self.trusted():
            page=(server.WEB/'index.html').read_bytes().replace(b'</head>',b'<script src="/__perf244.js"></script></head>',1)
            return original_reply(self,page,kind='text/html; charset=utf-8')
        if self.path=='/__perf244.js':return original_reply(self,script,kind='text/javascript; charset=utf-8')
        return original_get(self)
    def post(self):
        if self.path=='/__perf244/report':
            raw=self.rfile.read(min(512000,int(self.headers.get('Content-Length','0'))))
            try:
                record=json.loads(raw);record['received']=time.time()
                with guard:
                    with (data/'webview-telemetry.jsonl').open('a',encoding='utf8') as out:out.write(json.dumps(record,ensure_ascii=False)+'\n')
            except (ValueError,OSError):pass
            self.send_response(204);self.send_header('Content-Length','0');self.end_headers();return
        return original_post(self)
    server.Handler.reply=reply;server.Handler.do_GET=get;server.Handler.do_POST=post


def port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def request(base,path,body=None,headers=None,timeout=2):
    req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers or {})
    with urlopen(req,timeout=timeout) as response:return json.load(response)


def cold(args):
    import psutil
    snapshot=DISK/'snapshot';variant=snapshot if args.reuse_snapshot else DISK/args.label
    marker=OUTPUT/'snapshot.json'
    if not marker.is_file() or Path(json.loads(marker.read_text(encoding='utf8'))['data']).resolve()!=snapshot.resolve():
        raise ValueError('缺少本任务隔离副本标记，不能使用未知资料目录')
    if not args.reuse_snapshot:
        variant.mkdir(exist_ok=False)
        for source in snapshot.iterdir():
            if source.is_file():copy_file(source,variant/source.name)
    reference=snapshot/'reference-library/config.json'
    if reference.exists():
        value=json.loads(reference.read_text(encoding='utf8'))
        if args.adopt:value['roots']=[]
        save(variant/'reference-library/config.json',value)
    env=dict(os.environ,FRAMEKEEP_DATA=str(variant),FRAMEKEEP_APP_ROOT=str(ROOT),FRAMEKEEP_DESKTOP_TOKEN=secrets.token_hex(20),PYTHONUTF8='1')
    logs=OUTPUT/args.label;logs.mkdir(exist_ok=False);results=[]
    for index in range(args.runs):
        if (OUTPUT/'reference-config.json').exists():
            value=json.loads((OUTPUT/'reference-config.json').read_text(encoding='utf8'))
            if args.adopt:value['roots']=[]
            save(variant/'reference-library/config.json',value)
        purge=clear_file_cache();number=port();base=f'http://127.0.0.1:{number}';log_path=logs/f'boot-{index+1:02}.log'
        before_io=psutil.disk_io_counters(perdisk=True);started=time.perf_counter()
        with log_path.open('w',encoding='utf8') as log:
            process=subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'serve','--engine-root',str(args.engine_root),'--profile','startup','--port',str(number)],env=env,stdout=log,stderr=log)
            try:
                while time.perf_counter()-started<420:
                    if process.poll() is not None:raise RuntimeError(f'引擎退出: {log_path}')
                    try:
                        if request(base,'/api/ready').get('services_ready'):break
                    except (OSError,ValueError):pass
                    time.sleep(.05)
                else:raise TimeoutError('冷启动超过420秒')
                elapsed=time.perf_counter()-started
                checkpoints={stage:float(value) for value,stage in re.findall(r'\[startup\] ([\d.]+)s ([^\r\n]+)',log_path.read_text(encoding='utf8'))}
                after_io=psutil.disk_io_counters(perdisk=True)
                row={'run':index+1,'wall_seconds':elapsed,'checkpoints':checkpoints,'cache':purge,
                     'physical_disk_read_bytes':{name:after_io[name].read_bytes-value.read_bytes for name,value in before_io.items() if name in after_io}}
                results.append(row);save(OUTPUT/(args.label+'.json'),{'engine_root':str(args.engine_root),'data':str(variant),'adopt':args.adopt,'runs':results})
                print(json.dumps({'label':args.label,**row},ensure_ascii=False),flush=True)
                request(base,'/api/desktop/shutdown',{},headers={'X-Framekeep-Desktop':env['FRAMEKEEP_DESKTOP_TOKEN']})
                process.wait(timeout=20)
            except Exception as error:
                checkpoints={stage:float(value) for value,stage in re.findall(r'\[startup\] ([\d.]+)s ([^\r\n]+)',log_path.read_text(encoding='utf8'))}
                results.append({'run':index+1,'status':'error','elapsed_seconds':time.perf_counter()-started,'checkpoints':checkpoints,'error':str(error),'cache':purge})
                save(OUTPUT/(args.label+'.json'),{'engine_root':str(args.engine_root),'data':str(variant),'adopt':args.adopt,'runs':results})
                raise
            finally:
                if process.poll() is None:process.terminate();process.wait(timeout=10)


def fixture(args):
    configure_runtime(ROOT)
    from asset_catalog import AssetCatalog
    from catalog_store import Store
    base=OUTPUT/'native-fixture';data=base/'data';photos=base/'photos';inputs=base/'input'
    if (base/'fixture.json').exists():raise FileExistsError(base)
    inputs.mkdir(parents=True,exist_ok=True)
    samples=sorted((ROOT/'build/perf-analysis/photos/D00').glob('*.JPG'))[:5]
    if len(samples)!=5:raise ValueError('真实JPG样片不足5张')
    for sample in samples:
        if not (inputs/sample.name).exists():shutil.copy2(sample,inputs/sample.name)
    samples=sorted(inputs.glob('XSY_*.JPG'))
    for label,count in [('1500张',1500),('6000张',6000)]:
        folder=photos/label;folder.mkdir(parents=True,exist_ok=True)
        for index in range(count):
            target=folder/f'FRAME_{index+1:05}.JPG'
            if target.exists():continue
            # NTFS 单文件至多1024个硬链接；每3000张切换一组真实副本。
            original=samples[index%5];replica=inputs/(str(index//3000)+'-'+original.name)
            if not replica.exists():shutil.copy2(original,replica)
            os.link(replica,target)
    store=Store(data,ROOT)
    catalog=AssetCatalog(data)
    try:
        root=catalog.add_root(str(photos))['root_id']
        payload=json.dumps({'capture':{'camera':'Canon EOS','width':1600,'height':1066},'media':{},'description':{'title':'真实样片验收','keywords':[]},'tags':{}},ensure_ascii=False)
        with catalog.connect() as c:
            c.execute("UPDATE catalog_metadata SET status='ready',payload=?,search_text='Canon EOS 真实样片验收'",(payload,))
        catalog.wal_maintenance.checkpoint()
        save(base/'fixture.json',{'root':root,'counts':{'1500张':1500,'6000张':6000},'sample_sources':[str(p) for p in samples]})
    finally:catalog.close()
    print(json.dumps({'fixture':str(base),'root':root,'images':7500},ensure_ascii=False),flush=True)


def launch(args):
    import psutil
    active=[p.pid for p in psutil.process_iter(['name']) if (p.info['name'] or '').lower() in ('shiying 数字资产.exe','末帧选片.exe')]
    if active:raise RuntimeError(f'桌面实例仍存在，不能接管: {active}')
    base=OUTPUT/args.label;base.mkdir(exist_ok=False)
    shutil.copytree(OUTPUT/'native-fixture/data',base/'data')
    number=port()
    env=dict(os.environ,FRAMEKEEP_DATA=str(base/'data'),FRAMEKEEP_APP_ROOT=str(ROOT),FRAMEKEEP_ENGINE=sys.executable,
             FRAMEKEEP_ENGINE_SCRIPT=str(ROOT/'tools/performance_244_native_entry.py'),FRAMEKEEP_ACCEPTANCE_ENGINE_ROOT=str(args.engine_root),
             FRAMEKEEP_PORT=str(number),WEBVIEW2_USER_DATA_FOLDER=str(base/'webview'),PYTHONUTF8='1',PYTHONPATH=str(ROOT/'tools'))
    executable=Path('D:/Program Files/Framekeep/SHIYING 数字资产.exe')
    child=subprocess.Popen([str(executable)],env=env,cwd=ROOT)
    record={'pid':child.pid,'port':number,'data':str(base/'data'),'engine_root':str(args.engine_root),'exe':str(executable),'started':time.time()}
    save(base/'launch.json',record);print(json.dumps(record,ensure_ascii=False),flush=True)


class GpuCounters:
    def __init__(self):
        self.pdh=C.WinDLL('pdh');self.query=C.c_void_p();self.counter=C.c_void_p()
        self.pdh.PdhOpenQueryW.argtypes=[W.LPCWSTR,C.c_size_t,C.POINTER(C.c_void_p)]
        self.pdh.PdhAddEnglishCounterW.argtypes=[C.c_void_p,W.LPCWSTR,C.c_size_t,C.POINTER(C.c_void_p)]
        self.pdh.PdhGetFormattedCounterArrayW.argtypes=[C.c_void_p,W.DWORD,C.POINTER(W.DWORD),C.POINTER(W.DWORD),C.c_void_p]
        if self.pdh.PdhOpenQueryW(None,0,C.byref(self.query))!=0:raise OSError('PdhOpenQueryW')
        status=self.pdh.PdhAddEnglishCounterW(self.query,r'\GPU Engine(*)\Utilization Percentage',0,C.byref(self.counter))
        if status:raise OSError(f'GPU counter: {status&0xffffffff:08x}')
        self.pdh.PdhCollectQueryData(self.query)
    def sample(self,pids):
        self.pdh.PdhCollectQueryData(self.query);size=W.DWORD();count=W.DWORD()
        self.pdh.PdhGetFormattedCounterArrayW(self.counter,0x8200,C.byref(size),C.byref(count),None)
        if not size.value:return {}
        buffer=C.create_string_buffer(size.value)
        status=self.pdh.PdhGetFormattedCounterArrayW(self.counter,0x8200,C.byref(size),C.byref(count),buffer)
        if status:raise OSError(f'GPU array: {status&0xffffffff:08x}')
        class Value(C.Structure):_fields_=[('status',W.DWORD),('value',C.c_double)]
        class Item(C.Structure):_fields_=[('name',W.LPWSTR),('data',Value)]
        items=C.cast(buffer,C.POINTER(Item));result={}
        for index in range(count.value):
            item=items[index];match=re.search(r'pid_(\d+).*engtype_(.+)',item.name or '')
            if match and int(match[1]) in pids and item.data.status in (0,1):result[match[2]]=result.get(match[2],0)+item.data.value
        return result
    def close(self):self.pdh.PdhCloseQuery(self.query)


def monitor(args):
    import psutil
    if not args.pid:raise ValueError('monitor 必须指定本轮原生窗口的 --pid')
    try:root=psutil.Process(args.pid)
    except psutil.NoSuchProcess:raise RuntimeError('验收窗口进程已退出，请使用下次 launch.json 中的新PID') from None
    records=[json.loads(path.read_text(encoding='utf8')) for path in OUTPUT.glob('*/launch.json')]
    if not any(record['pid']==root.pid and abs(record['started']-root.create_time())<10 for record in records):
        raise ValueError('目标不是本轮创建且身份匹配的原生实例')
    gpu=GpuCounters();samples=[];last={};begin=time.perf_counter()
    try:
        for index in range(args.seconds+1):
            processes=[root,*root.children(recursive=True)];groups={'webview':[],'engine':[],'shell':[]};cpu={key:0. for key in groups};rss={key:0 for key in groups}
            now=time.perf_counter()
            for process in processes:
                try:
                    name=process.name().lower();group='webview' if name=='msedgewebview2.exe' else 'shell' if process.pid==root.pid else 'engine'
                    groups[group].append(process.pid);times=process.cpu_times();total=times.user+times.system
                    if process.pid in last:
                        prior,stamp=last[process.pid];cpu[group]+=(total-prior)/max(.001,now-stamp)*100
                    last[process.pid]=(total,now);rss[group]+=process.memory_info().rss
                except (psutil.NoSuchProcess,psutil.AccessDenied):continue
            row={'elapsed':now-begin,'time':time.time(),'cpu_one_core_percent':cpu,'rss_bytes':rss,'pids':groups,
                 'webview_gpu_percent_by_engine':gpu.sample(set(groups['webview']))}
            if index:samples.append(row)
            if index<args.seconds:time.sleep(max(0,begin+index+1-time.perf_counter()))
    finally:gpu.close()
    result={'label':args.label,'root_pid':args.pid,'samples':samples}
    for group in ('webview','engine','shell'):
        values=[s['cpu_one_core_percent'][group] for s in samples];result[group+'_cpu_mean']=sum(values)/max(1,len(values))
    types=set().union(*(s['webview_gpu_percent_by_engine'] for s in samples))
    result['webview_gpu_mean_by_engine']={kind:sum(s['webview_gpu_percent_by_engine'].get(kind,0) for s in samples)/len(samples) for kind in types}
    result['webview_gpu_max_by_engine']={kind:max(s['webview_gpu_percent_by_engine'].get(kind,0) for s in samples) for kind in types}
    save(OUTPUT/(args.label+'.json'),result);print(json.dumps({k:v for k,v in result.items() if k!='samples'},ensure_ascii=False),flush=True)


def report(args):
    ui_file=OUTPUT/'ui-status.json'
    result={'cold':{},'raw_and_thumbnail':[],'ui_status':json.loads(ui_file.read_text(encoding='utf8')) if ui_file.exists() else {'status':'not_run'},'production_files_unchanged':True}
    for name in ('production-cold-baseline','production-cold-optimized','production-adopt-optimized'):
        source_file=OUTPUT/(name+'.json')
        if not source_file.exists():result['cold'][name]={'status':'not_run'};continue
        source=json.loads(source_file.read_text(encoding='utf8'));summary={'runs':len(source['runs'])}
        for stage in ('catalog ready','reference library ready','http ready'):
            values=sorted(row['checkpoints'][stage] for row in source['runs'] if stage in row['checkpoints'])
            summary[stage]={'median':statistics.median(values),'p90_nearest_rank':values[math.ceil(.9*len(values))-1],'max':max(values)} if values else {'status':'not_reached'}
        summary['all_cache_purges_succeeded']=all(row['cache']['system_file_cache_trimmed'] and row['cache']['standby_purged'] for row in source['runs'])
        result['cold'][name]=summary
    timeout_file=OUTPUT/'production-adopt-baseline-timeout.json'
    result['cold']['production-adopt-baseline']=json.loads(timeout_file.read_text(encoding='utf8')) if timeout_file.exists() else {'status':'no_timeout_record'}
    snapshot=json.loads((OUTPUT/'snapshot.json').read_text(encoding='utf8'));changes=[]
    for entry in snapshot['files']:
        path=PRODUCTION/entry['name'];stat=path.stat()
        if (stat.st_size,stat.st_mtime_ns)!=(entry['bytes'],entry['source_mtime_ns']):changes.append(entry['name'])
    result['production_files_unchanged']=not changes;result['changed_source_files']=changes
    for item in json.loads((OUTPUT/'real-fixtures.json').read_text(encoding='utf8')):
        values={}
        for key in ('source','fixture'):
            with open(item[key],'rb') as stream:values[key]=hashlib.file_digest(stream,'sha256').hexdigest()
        result['raw_and_thumbnail'].append({**item,'sha256_matches':values['source']==values['fixture'],'sha256':values['source']})
    tests_file=OUTPUT/'real-tests.json'
    result['real_fixture_tests']=json.loads(tests_file.read_text(encoding='utf8')) if tests_file.exists() else {'status':'not_run'}
    count_file=OUTPUT/'catalog-dirty-counts.json'
    if count_file.exists():result['catalog_dirty_counts']=json.loads(count_file.read_text(encoding='utf8'))
    save(OUTPUT/'acceptance-summary.json',result);print(json.dumps(result,ensure_ascii=False),flush=True)


def real_tests(args):
    cases=['test_metadata_latency.MetadataLatencyTests.test_real_thumbnail_change_is_rejected_before_any_commit','test_sku_metadata_sync.MetadataArchiveSyncTests.test_real_raw_jpg_pair_restores_fields_and_preserves_sensor_payload']
    started=time.perf_counter()
    completed=subprocess.run([sys.executable,'-X','utf8','-m','unittest','-v',*cases],cwd=ROOT/'选片助手',capture_output=True,text=True,encoding='utf8',timeout=120)
    log=completed.stdout+completed.stderr;(OUTPUT/'real-tests.log').write_text(log,encoding='utf8')
    passed=len(re.findall(r'\.\.\. ok\s*$',log,re.M));skipped=len(re.findall(r'\.\.\. skipped',log))
    result={'cases':cases,'exit_code':completed.returncode,'passed':passed,'skipped':skipped,'wall_seconds':time.perf_counter()-started,'status':'passed' if completed.returncode==0 and passed==len(cases) else 'incomplete'}
    save(OUTPUT/'real-tests.json',result);print(json.dumps(result,ensure_ascii=False),flush=True)
    if result['status']!='passed':raise RuntimeError('真实样片验收未全部通过，见 real-tests.log')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=('snapshot','cold','serve','fixture','launch','monitor','report','real_tests'))
    parser.add_argument('--engine-root',type=Path,default=ROOT);parser.add_argument('--profile',default='native')
    parser.add_argument('--port',type=int,default=0);parser.add_argument('--label',default='optimized-cold');parser.add_argument('--runs',type=int,default=10)
    parser.add_argument('--adopt',action='store_true');parser.add_argument('--pid',type=int);parser.add_argument('--seconds',type=int,default=30)
    parser.add_argument('--reuse-snapshot',action='store_true')
    args=parser.parse_args();args.engine_root=args.engine_root.resolve();OUTPUT.mkdir(parents=True,exist_ok=True)
    globals()[args.command](args)


if __name__=='__main__':main()
