"""百万级缓存/队列元数据压力验证；不是百万真实照片解码吞吐测试。"""
from argparse import ArgumentParser
from itertools import islice
import hashlib
import io
import json
from pathlib import Path
import statistics
import tempfile
import time
from unittest.mock import patch

from PIL import Image
import psutil
import media_engine as media
from cache_storage import CacheStorage
from preview_cache import PreviewCache,PreviewWarmer,cache_name

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=ArgumentParser();parser.add_argument('--rows',type=int,default=1_000_000);args=parser.parse_args()
    if not 1000<=args.rows<=2_000_000:raise ValueError('压力记录范围 1000–2000000')
    output=ROOT/'build/cache-scale';output.mkdir(exist_ok=True)
    process=psutil.Process();initial=process.memory_info().rss
    result={'rows':args.rows,'scope':'SQLite 缓存索引与持久队列；仅 64 张真实 JPEG，不代表百万原图解码吞吐','stages':{}}
    def stage(name,start):
        result['stages'][name]=round(time.perf_counter()-start,3);print(name,result['stages'][name],flush=True)
    def timing(call,count=64):
        samples=[]
        for _ in range(count):
            start=time.perf_counter();call();samples.append((time.perf_counter()-start)*1000)
        return {'p50_ms':round(statistics.median(samples),3),'p95_ms':round(sorted(samples)[int(len(samples)*.95)],3),'max_ms':round(max(samples),3)}
    with tempfile.TemporaryDirectory(prefix='million-',dir=output) as folder:
        base=Path(folder);storage=CacheStorage(base/'data');cache=storage.cache
        jpeg=io.BytesIO();Image.new('RGB',(96,64),'teal').save(jpeg,format='JPEG');payload=jpeg.getvalue();digest=hashlib.sha256(payload).hexdigest()
        samples=[];start=time.perf_counter()
        with cache.connect() as c:
            for offset in range(0,args.rows,1000):
                batch=[]
                for i in range(offset,min(args.rows,offset+1000)):
                    record={'fingerprint':'scale-'+str(i),'orientation':1};name=cache_name(record,480)
                    batch.append((name,len(payload),i,record['fingerprint'],480,0,1,digest))
                    if i<64:samples.append(name);(cache.folder/name).write_bytes(payload)
                c.executemany('INSERT INTO previews(name,bytes,accessed,fingerprint,edge,decoded,orientation,digest) VALUES (?,?,?,?,?,?,?,?)',batch);c.commit()
                if offset and offset%100000==0:print('preview rows',offset,flush=True)
        stage('seed_preview_index_seconds',start)
        worker=PreviewWarmer(cache,None,autostart=False);start=time.perf_counter()
        records=({'id':f'{i:08d}','fingerprint':'capture-'+str(i),'jpg':f'{i}.JPG'} for i in range(args.rows))
        worker.enqueue(base/'not-real-originals',records);stage('production_enqueue_seconds',start)
        state=storage.state();assert state['images']==args.rows and state['pending']==args.rows,state
        with cache.connect() as c:
            result['queue_plan']=[r[3] for r in c.execute('EXPLAIN QUERY PLAN SELECT * FROM pending INDEXED BY pending_active_order WHERE attempts<3 ORDER BY edge,attempts,requested LIMIT 1')]
            result['lru_plan']=[r[3] for r in c.execute('EXPLAIN QUERY PLAN SELECT name,bytes,accessed FROM previews WHERE name!=? ORDER BY accessed,name',('',))]
        assert all('TEMP B-TREE' not in line for line in result['queue_plan']+result['lru_plan']),result
        result['state']=timing(storage.state)
        result['cache_hit']=timing(lambda:cache.get(samples[32],read=True))
        with patch.object(media,'preview',return_value=cache.folder/samples[0]):result['queue_claim']=timing(worker.process_one)
        start=time.perf_counter();batch=list(islice(storage._registered(cache),512));stage('stream_first_512_metadata_seconds',start);assert len(batch)==512
        with cache.lock,cache.connect() as c:
            before=c.execute('SELECT bytes FROM usage WHERE id=1').fetchone()[0]
            assert storage.budget.trim(cache,c,limit=before-len(payload),max_entries=16)
        assert storage.state()['images']==args.rows-1
        start=time.perf_counter()
        with cache.connect() as c:c.execute('UPDATE pending SET attempts=3')
        stage('mark_failed_queue_seconds',start)
        assert storage.state()['pending']==0 and storage.state()['failed']==args.rows
        result['idle_failed_queue']=timing(worker.process_one)
        start=time.perf_counter();restarted=PreviewCache(cache.folder);stage('bounded_restart_seconds',start)
        with restarted.connect() as c:
            actual=c.execute('SELECT count(*),COALESCE(SUM(bytes),0) FROM previews').fetchone()
            state=restarted.state();assert state['images']==actual[0] and state['bytes']==actual[1],(state,tuple(actual))
        result['restart_images_checked_upper_bound']=256
        result['index_bytes']=sum(p.stat().st_size for p in base.rglob('*.sqlite3'))
        result['rss_mb']=round(process.memory_info().rss/1024**2,2)
        result['peak_rss_mb']=round(getattr(process.memory_info(),'peak_wset',process.memory_info().rss)/1024**2,2)
        result['rss_growth_mb']=round((process.memory_info().rss-initial)/1024**2,2)
        assert result['rss_growth_mb']<256,result
        assert result['state']['p95_ms']<1000 and result['idle_failed_queue']['p95_ms']<1000,result
        storage.close();result['passed']=True
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
