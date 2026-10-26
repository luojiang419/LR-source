"""有界会话浏览基准：合成记录，不读取或修改用户原片。"""
import argparse
from datetime import datetime,timedelta
import json
import math
from pathlib import Path
import statistics
import tempfile
import time

from catalog_store import Store,stable_json
from session_browser import PAGE_SIZE,read


def benchmark(rows):
    with tempfile.TemporaryDirectory(prefix='framekeep-session-benchmark-') as temporary:
        root=Path(temporary);photos=root/'photos';photos.mkdir();store=Store(root/'data',Path(__file__).resolve().parents[1])
        sid=store.ensure_live_session(photos);base=datetime(2026,10,23,8)
        started=time.perf_counter()
        with store.connect() as db:
            for start in range(0,rows,512):
                batch=[]
                for i in range(start,min(rows,start+512)):
                    record={'id':f'PHOTO{i:08d}','name':f'PHOTO{i:08d}','jpg':f'批次{i//10000}/PHOTO{i:08d}.JPG','raw':None,'time':(base+timedelta(seconds=i)).isoformat(),
                            'missingTime':False,'cameraKey':'synthetic','existingRating':0,'fileCount':1,'fingerprint':f'synthetic-{i}'}
                    batch.append((sid,record['id'],stable_json(record),i))
                db.executemany('INSERT INTO captures VALUES (?,?,?,?)',batch)
            overrides={f'PHOTO{i:08d}':i%5+1 for i in range(0,rows,97)}
            db.execute('UPDATE sessions SET state=? WHERE id=?',(stable_json({'overrides':overrides}),sid))
        indexed_seconds=time.perf_counter()-started
        started=time.perf_counter();session,page=read(store,sid);cold_ms=(time.perf_counter()-started)*1000
        assert len(page)==min(rows,PAGE_SIZE) and session['browser']['total']==rows
        response_bytes=len(stable_json({'session':session,'records':page}).encode())
        times=[]
        for i in range(12):
            started=time.perf_counter();s,records=read(store,sid,{'offset':(i*7919)%rows});times.append((time.perf_counter()-started)*1000)
            assert len(records)<=PAGE_SIZE and s['browser']['total']==rows
        started=time.perf_counter();s,result=read(store,sid,{'q':f'PHOTO{rows-10:08d}'});search_ms=(time.perf_counter()-started)*1000
        assert len(result)==1 and result[0]['id']==f'PHOTO{rows-10:08d}'
        started=time.perf_counter();s,result=read(store,sid,{'sort':'rating-desc','filter':'picked'});rating_ms=(time.perf_counter()-started)*1000
        assert s['browser']['matched']==len(overrides)+int(f'PHOTO{rows-1:08d}' not in overrides)
        s,result=read(store,sid,{'around':f'PHOTO{rows-1:08d}'});assert result[-1]['id']==f'PHOTO{rows-1:08d}'
        with store.connect() as db:
            plan=[r[3] for r in db.execute('EXPLAIN QUERY PLAN SELECT data FROM captures WHERE session=? AND id IN (?,?)',(sid,'PHOTO00000001','PHOTO00000002'))]
        return {'rows':rows,'real_source_images':0,'manual_ratings':len(overrides),'batch_limit':PAGE_SIZE,'index_build_seconds':round(indexed_seconds,3),
                'cold_first_page_ms':round(cold_ms,3),'module_response_bytes':response_bytes,'warm_samples':len(times),'warm_median_ms':round(statistics.median(times),3),
                'warm_p95_ms':round(sorted(times)[math.ceil(.95*len(times))-1],3),'search_ms':round(search_ms,3),'rating_sort_ms':round(rating_ms,3),
                'database_bytes':store.db.stat().st_size,'photo_json_lookup_plan':plan,'passed':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--rows',type=int,default=100000);parser.add_argument('--output',type=Path);args=parser.parse_args()
    result=benchmark(max(10,args.rows));payload=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(payload,'utf8')
    print(payload)
