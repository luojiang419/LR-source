"""隔离合成基准：百万资产/双组件/双位置，不接触 .framekeep 或原片。"""
import argparse
import json
from pathlib import Path
import random
import sqlite3
import statistics
import time
from types import SimpleNamespace

from asset_archive import AssetArchive

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--rows',type=int,default=1_000_000);args=parser.parse_args()
    base=ROOT/'build/asset-search-benchmark';base.mkdir(parents=True,exist_ok=True)
    db=base/'asset_archive.sqlite3'
    if db.exists():raise SystemExit('基准库已存在，请保留结果或手动移走后再运行；不会覆盖已有库。')
    archive=AssetArchive(SimpleNamespace(data=base,root=ROOT),autostart=False)
    started=time.perf_counter();rng=random.Random(20261020)
    with archive.connect() as c:
        c.execute('PRAGMA synchronous=NORMAL')
        for start in range(0,args.rows,10000):
            ids=range(start,min(start+10000,args.rows))
            assets=[(f'a{i:09d}',float(i),'2026-10-20','Canon R6 II',f'SKU{i//10:06d}',f'SKU{i//10:06d}',f'p{i//100000}',('细节图','模特图')[(i//5)%2]) for i in ids]
            c.executemany('INSERT INTO assets(id,created,captured,camera,sku,normalized,project,scope) VALUES (?,?,?,?,?,?,?,?)',assets)
            c.executemany('INSERT INTO files(id,asset,component,sha) VALUES (?,?,?,?)',[(f'{kind}{i:09d}',f'a{i:09d}',kind,f'{i:064x}') for i in ids for kind in ('jpg','raw')])
            c.executemany('INSERT INTO locations VALUES (?,?,?,?)',[(f'D:/synthetic/p{i//100000}/{i//1000}/IMG{i:09d}.{ext}',f'{kind}{i:09d}',1.,'online') for i in ids for kind,ext in [('jpg','JPG'),('raw','CR3')]])
            c.executemany('INSERT INTO catalog_links VALUES (?,?,?)',[(f's{i//10000}',f'c{i:09d}',f'a{i:09d}') for i in ids])
            c.commit()
            if (start+10000)%100000==0:print(f'已生成 {min(start+10000,args.rows):,} 资产',flush=True)
        c.execute('ANALYZE');c.commit();c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    generated=time.perf_counter()-started
    scenarios={
        'exact':lambda i:{'sku':f'SKU{rng.randrange(max(1,args.rows//10)):06d}','mode':'exact'},
        'prefix_broad':lambda i:{'sku':'SKU','mode':'prefix'},
        'scope_project':lambda i:{'scope':'细节图','project':f'p{rng.randrange(max(1,(args.rows+99999)//100000))}'},
        'browse':lambda i:{},
        'missing':lambda i:{'sku':'SKU999999999','mode':'exact'},
    }
    first={};metrics={}
    for name,make in scenarios.items():
        durations=[]
        for i in range(101):
            begin=time.perf_counter();result=archive.search(**make(i));duration=(time.perf_counter()-begin)*1000
            if i==0:first[name]=round(duration,3)
            else:durations.append(duration)
        samples=sorted(durations)
        metrics[name]={'p50_ms':round(statistics.median(samples),3),'p95_ms':round(samples[94],3),'p99_ms':round(samples[98],3),'max_ms':round(max(samples),3)}
    # 游标跨过 90% 数据，再实际请求下一页。没有用 OFFSET 模拟深分页。
    import base64,hashlib
    anchor=max(0,args.rows//100)
    signature=hashlib.sha256(json.dumps(['','','','prefix'],ensure_ascii=False).encode()).hexdigest()[:24]
    cursor=base64.urlsafe_b64encode(json.dumps({'q':signature,'key':[f'SKU{anchor:06d}',float(anchor*10),f'a{anchor*10:09d}']}).encode()).decode().rstrip('=')
    samples=[]
    for _ in range(100):
        begin=time.perf_counter();archive.search(cursor=cursor);samples.append((time.perf_counter()-begin)*1000)
    samples.sort();metrics['deep_cursor_90pct']={'p50_ms':round(statistics.median(samples),3),'p95_ms':round(samples[94],3),'p99_ms':round(samples[98],3)}
    with archive.connect() as c:
        plans={name:[r[3] for r in c.execute('EXPLAIN QUERY PLAN '+sql,params)] for name,sql,params in [
            ('exact','SELECT * FROM assets WHERE normalized=? ORDER BY normalized DESC,created DESC,id DESC LIMIT 61',['SKU000001']),
            ('scope_project','SELECT * FROM assets WHERE scope=? AND project=? ORDER BY normalized DESC,created DESC,id DESC LIMIT 61',['细节图','p0']),
            ('cursor','SELECT * FROM assets WHERE (normalized,created,id)<(?,?,?) ORDER BY normalized DESC,created DESC,id DESC LIMIT 61',['SKU010000',100000.,'a000100000'])]}
        counts={t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('assets','files','locations','catalog_links')}
        integrity=c.execute('PRAGMA quick_check').fetchone()[0]
    result={'rows':counts,'sqlite_version':sqlite3.sqlite_version,'generation_seconds':round(generated,2),'db_bytes':db.stat().st_size,'first_call_ms':first,'warm_100_calls':metrics,'plans':plans,'quick_check':integrity,'conditions':'合成数据；造数后 OS 文件缓存未清空；每次查询新建 SQLite 连接（无复用页缓存）；首调用不代表冷盘；60项含批量位置/预览链接，不含浏览器/缩略图/原片读写'}
    (base/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True);archive.close()


if __name__=='__main__':main()
