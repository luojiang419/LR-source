"""任务244可重复基准：隔离引擎空闲采样、真实样片元数据和索引查询。

python -X utf8 tools/benchmark_project_performance.py idle --output build/perf-analysis/after
python -X utf8 tools/benchmark_project_performance.py metadata --output build/perf-analysis/metadata-after
可用 --engine-root 指定从 Git 导出的基线源码；不打开生产数据库。
"""
import argparse
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import runpy
import shutil
import sqlite3
import socket
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]


def idle(args):
    import psutil
    counts=Counter();begins=Counter();guard=threading.Lock();original=sqlite3.connect
    class Connection(sqlite3.Connection):
        def execute(self,sql,*values,**options):
            if sql.lstrip().upper().startswith(('BEGIN IMMEDIATE','BEGIN EXCLUSIVE')):
                with guard:begins[threading.current_thread().name]+=1
            return super().execute(sql,*values,**options)
    def connect(*values,**options):
        options.setdefault('factory',Connection)
        with guard:counts[threading.current_thread().name]+=1
        return original(*values,**options)
    sqlite3.connect=connect
    def report():
        time.sleep(args.warmup)
        with guard:c0=counts.copy();b0=begins.copy()
        process=psutil.Process();cpu0=time.process_time();io0=process.io_counters();t0=time.monotonic()
        time.sleep(args.seconds);duration=time.monotonic()-t0
        with guard:delta=counts-c0;writes=begins-b0
        io1=process.io_counters()
        result={'seconds':duration,'connects_per_second':sum(delta.values())/duration,
                'write_transactions_per_second':sum(writes.values())/duration,
                'cpu_one_core_percent':(time.process_time()-cpu0)/duration*100,
                'read_ops_per_second':(io1.read_count-io0.read_count)/duration,
                'read_bytes_per_second':(io1.read_bytes-io0.read_bytes)/duration,
                'threads':process.num_threads(),'rss_mb':process.memory_info().rss/2**20,
                'thread_names':[thread.name for thread in threading.enumerate()],
                'connections_by_thread':dict(delta),'transactions_by_thread':dict(writes)}
        (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(result,ensure_ascii=False),flush=True);os._exit(0)
    threading.Thread(target=report,daemon=True).start()
    sys.argv=['server.py','--port','0','--no-browser']
    runpy.run_path(str(args.engine_root/'选片助手/server.py'),run_name='__main__')


def metadata(args):
    from asset_catalog import AssetCatalog
    from catalog_metadata import dumps
    try:from catalog_metadata_codec import pack
    except ImportError:pack=dumps
    source=ROOT/'build/perf-analysis/photos/D00'
    files=sorted(source.glob('*.JPG'))[:5]
    if len(files)!=5:raise ValueError('需要 build/perf-analysis/photos/D00 内5张真实样片')
    photos=args.output/'photos';photos.mkdir()
    for path in files:shutil.copy2(path,photos/path.name)
    catalog=AssetCatalog(args.output/'data')
    try:
        rid=catalog.add_root(str(photos))['root_id']
        with catalog.connect() as c:rows=[dict(row) for row in c.execute('SELECT * FROM items ORDER BY name')]
        samples=[catalog.metadata._extract(row) for row in rows]
        database=args.output/'sample-1000.sqlite3'
        with closing(sqlite3.connect(database)) as c:
            c.executescript("""CREATE TABLE catalog_metadata(item TEXT PRIMARY KEY,payload TEXT,search_text TEXT);
                CREATE VIRTUAL TABLE fts USING fts5(search_text,content='catalog_metadata',content_rowid='rowid',tokenize='trigram');""")
            started=time.perf_counter()
            for index in range(1000):
                payload,search=samples[index%5]
                c.execute('INSERT INTO catalog_metadata VALUES(?,?,?)',(str(index),pack(payload),search))
            c.execute("INSERT INTO fts(fts) VALUES('rebuild')");c.commit()
            insert=time.perf_counter()-started
            query="SELECT count(*) FROM catalog_metadata WHERE instr(lower(search_text),lower(?))>0"
            timings={}
            for word in ('Canon','50','不存在'):
                values=[]
                for _ in range(10):
                    t=time.perf_counter();c.execute(query,(word,)).fetchone();values.append(time.perf_counter()-t)
                timings[word]=sorted(values)[len(values)//2]*1000
            average=c.execute('SELECT avg(length(cast(payload AS BLOB))+length(cast(search_text AS BLOB))),avg(length(cast(search_text AS BLOB))) FROM catalog_metadata').fetchone()
            sizes={}
            try:sizes=dict(c.execute('SELECT name,sum(pgsize) FROM dbstat GROUP BY name'))
            except sqlite3.Error:pass
        result={'samples':[p.name for p in files],'rows':1000,'average_row_bytes':average[0],
                'average_search_bytes':average[1],'database_bytes':database.stat().st_size,
                'insert_and_fts_seconds':insert,'short_search_median_ms':timings,'page_bytes':sizes}
        (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(result,ensure_ascii=False),flush=True)
    finally:catalog.close()


def load(args):
    import psutil
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    def get(path):
        with urlopen(base+path,timeout=15) as response:return json.load(response)
    def post(path,value,token):
        request=Request(base+path,data=json.dumps(value).encode(),headers={'Content-Type':'application/json','X-Framekeep-Token':token,'Origin':base})
        with urlopen(request,timeout=30) as response:return json.load(response)
    env=dict(os.environ,PYTHONPATH=str(ROOT/'更新系统'),PYTHONUTF8='1')
    with (args.output/'engine.log').open('w',encoding='utf8') as log:
        child=subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'serve','--engine-root',str(args.engine_root),'--output',str(args.output),'--port',str(port)],env=env,stdout=log,stderr=log)
        try:
            started=time.monotonic()
            while time.monotonic()-started<30:
                if child.poll() is not None:raise RuntimeError('引擎启动失败，见 engine.log')
                try:
                    if get('/api/ready').get('services_ready'):break
                except OSError:pass
                time.sleep(.1)
            else:raise TimeoutError('引擎未就绪')
            startup=time.monotonic()-started
            token=get('/api/view-bootstrap')['request_token']
            root=post('/api/catalog/add',{'folder':str(ROOT/'build/perf-analysis/photos')},token)['root_id']
            process=psutil.Process(child.pid);cpu0=process.cpu_times();io0=process.io_counters();t0=time.monotonic();results=[]
            for index in range(10):
                folder=f'D{index:02d}';t=time.monotonic();post('/api/catalog/visit',{'root':root,'folder':folder},token)
                first=None;latencies=[]
                while time.monotonic()-t<30:
                    request_start=time.monotonic();state=get('/api/catalog/state')['visits'];latencies.append(time.monotonic()-request_start)
                    if state['folder']!=folder:time.sleep(.02);continue
                    items=get(f'/api/catalog/items?root={root}&folder={folder}&limit=120&tree=0&quick=1')['rows']
                    if items and first is None:first=time.monotonic()-t
                    if not state['running'] and state['total']>=100 and len(items)>=100:break
                    time.sleep(.05)
                else:raise TimeoutError(f'访问 {folder} 超时: {state}')
                row={'folder':folder,'first_available_seconds':first,'completed_seconds':time.monotonic()-t,'max_state_seconds':max(latencies),'rows':len(items),'total':state['total']}
                results.append(row);print(json.dumps(row),flush=True)
            elapsed=time.monotonic()-t0;cpu=process.cpu_times();io=process.io_counters()
            result={'startup_seconds':startup,'elapsed_seconds':elapsed,'cpu_seconds':cpu.user+cpu.system-cpu0.user-cpu0.system,
                    'read_mb':(io.read_bytes-io0.read_bytes)/2**20,'write_mb':(io.write_bytes-io0.write_bytes)/2**20,'folders':results}
            (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        finally:
            if child.poll() is None:child.terminate()
            child.wait(timeout=15)


def serve(args):
    sys.argv=['server.py','--port',str(args.port),'--no-browser']
    runpy.run_path(str(args.engine_root/'选片助手/server.py'),run_name='__main__')


def browser(args):
    from playwright.sync_api import sync_playwright
    css=(args.engine_root/'演示方案/asset-workspace.css').read_text(encoding='utf8')
    results=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel='msedge',headless=True)
        try:
            context=browser.new_context(viewport={'width':1440,'height':1000})
            context.tracing.start(screenshots=True,snapshots=False)
            for count in (1500,6000):
                page=context.new_page()
                page.set_content('<style>'+css+'</style><style>body{margin:0;background:#222;color:white}.catalog-grid{height:950px;grid-template-columns:repeat(8,minmax(0,1fr))}.catalog-card{background:#333;color:white}.catalog-thumb{background:#666}</style><div id="grid" class="catalog-grid"></div>')
                result=page.evaluate('''async count=>{
                    const grid=document.getElementById('grid'),start=performance.now();
                    grid.innerHTML=Array.from({length:count},(_,i)=>`<button class="catalog-card" data-id="${i}"><span class="catalog-thumb"><span style="font-size:50px">▧</span></span><span class="catalog-card-name">XSY_${i}.JPG</span><span class="catalog-card-sku">SKU-${i}</span></button>`).join('');
                    grid.getBoundingClientRect();await new Promise(requestAnimationFrame);
                    const first=performance.now()-start;
                    const frames=[];let previous=performance.now();
                    for(let i=0;i<30;i++){grid.scrollTop=grid.scrollHeight*i/29;await new Promise(requestAnimationFrame);const now=performance.now();frames.push(now-previous);previous=now}
                    const painted=[...grid.children].filter(node=>node.firstElementChild.checkVisibility({contentVisibilityAuto:true})).length;
                    return {count,first_layout_ms:first,scroll_frame_median_ms:frames.sort((a,b)=>a-b)[15],scroll_frame_max_ms:Math.max(...frames),rendered_cards:painted,scroll_height:grid.scrollHeight,last_card_visible:grid.lastElementChild.checkVisibility({contentVisibilityAuto:true})};
                }''',count)
                results.append(result)
                (args.output/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
                print(json.dumps(result),flush=True)
                page.screenshot(path=str(args.output/f'grid-{count}-end.png'),animations='disabled')
                page.close()
            context.tracing.stop(path=str(args.output/'trace.zip'));context.close()
        finally:browser.close()
    (args.output/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(results),flush=True)


def indexes(args):
    source=ROOT/'build/perf-analysis/load-after-v2/data/asset_catalog.sqlite3'
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as original, closing(sqlite3.connect(args.output/'audit.sqlite3')) as copied:
        original.backup(copied)
        row=copied.execute('SELECT root,folder,pair_key FROM items LIMIT 1').fetchone()
        queries={
            'folder':('SELECT id,name FROM items INDEXED BY catalog_folder_candidates WHERE root=? AND online=1 AND folder=? COLLATE NOCASE LIMIT 60',row[:2]),
            'browse':('SELECT id FROM items INDEXED BY catalog_items_browse WHERE root=? AND online=1 ORDER BY modified DESC,id LIMIT 60',(row[0],)),
            'pair':('SELECT id,source,extension FROM items INDEXED BY catalog_pair_cover WHERE pair_key=? AND online=1',(row[2],))}
        def measure():
            result={}
            for name,(sql,params) in queries.items():
                timings=[]
                for _ in range(30):
                    started=time.perf_counter();copied.execute(sql,params).fetchall();timings.append((time.perf_counter()-started)*1000)
                result[name]={'median_ms':sorted(timings)[15],'plan':[r[3] for r in copied.execute('EXPLAIN QUERY PLAN '+sql,params)]}
            return result
        before=measure();started=time.perf_counter();copied.execute('PRAGMA analysis_limit=400');copied.execute('ANALYZE items');copied.commit();analysis=time.perf_counter()-started
        after=measure();indexes={}
        for index in copied.execute('PRAGMA index_list(items)').fetchall():
            quoted='"'+index[1].replace('"','""')+'"'
            indexes[index[1]]=[{'column':r[2],'descending':bool(r[3]),'collation':r[4]} for r in copied.execute('PRAGMA index_xinfo('+quoted+')') if r[5]]
        result={'rows':copied.execute('SELECT count(*) FROM items').fetchone()[0],'analysis_seconds':analysis,'before':before,'after':after,'indexes':indexes}
    (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in result.items() if k!='indexes'}),flush=True)


def migration(args):
    from asset_catalog import AssetCatalog
    from compact_catalog import compact
    with closing(sqlite3.connect(ROOT/'build/perf-analysis/metadata-before/sample-1000.sqlite3')) as source:
        samples=source.execute('SELECT payload,search_text FROM catalog_metadata LIMIT 5').fetchall()
    catalog=AssetCatalog(args.output/'data')
    try:
        catalog.add_root(str(ROOT/'build/perf-analysis/photos'))
        with catalog.connect() as c:
            ids=[row[0] for row in c.execute('SELECT id FROM items')]
            c.execute('DELETE FROM catalog_metadata_maintenance')
            c.executemany("UPDATE catalog_metadata SET payload=?,search_text=?,status='ready' WHERE item=?",[(samples[index%5][0],samples[index%5][1],item) for index,item in enumerate(ids)])
        catalog.wal_maintenance.checkpoint()
        before=catalog.db.stat().st_size;started=time.perf_counter();batches=0
        while catalog.metadata.compact_batch():batches+=1
        elapsed=time.perf_counter()-started;catalog.wal_maintenance.checkpoint()
        output=compact(catalog.db,args.output/'catalog-compacted.sqlite3')
        with catalog.connect() as c:
            count=c.execute("SELECT count(*) FROM catalog_metadata WHERE status='ready' AND json_extract(payload,'$._format')=2").fetchone()[0]
            generations=c.execute('SELECT min(generation),max(generation) FROM catalog_metadata').fetchone()[:]
        result={'rows':count,'batches':batches,'migration_seconds':elapsed,'generation_range':generations,'before_bytes':before,'compacted_bytes':output['output_bytes'],'compaction_seconds':output['seconds'],'verified':output['verified']}
        (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(result),flush=True)
    finally:catalog.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('idle','metadata','load','browser','indexes','migration','serve'))
    parser.add_argument('--engine-root',type=Path,default=ROOT);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=30);parser.add_argument('--warmup',type=float,default=25)
    parser.add_argument('--port',type=int,default=0)
    args=parser.parse_args();args.engine_root=args.engine_root.resolve();args.output=args.output.resolve()
    if args.mode!='serve' and args.output.exists():raise ValueError('请选择新的隔离输出目录，不覆盖已有基准')
    args.output.mkdir(parents=True,exist_ok=args.mode=='serve');os.environ['FRAMEKEEP_DATA']=str(args.output/'data');os.environ['FRAMEKEEP_APP_ROOT']=str(ROOT)
    sys.path.insert(0,str(args.engine_root/'选片助手'))
    sys.path.insert(1,str(ROOT/'更新系统'))
    # Git 导出的源码没有大型运行时；两组必须使用同一套 GPU/FFmpeg，
    # 否则基线没有加载 CUDA，会把 GPU 运行时的内存误算成源码回归。
    import gpu_media,video_preview
    shared_gpu=ROOT/'.runtime/gpu'
    if shared_gpu.is_dir():gpu_media.runtime_folder=lambda:shared_gpu
    find_tool=video_preview.find_tool
    video_preview.find_tool=lambda name,app_root=None:find_tool(name,app_root or ROOT)
    globals()[args.mode](args)


if __name__=='__main__':main()
