"""隔离合成库基准；不读原图、不接触正式数据，记录实际首屏与查询计划。"""
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
import argparse,json,statistics,time
from asset_catalog import AssetCatalog
from asset_intelligence import AssetIntelligence
from model_providers import Providers
from search_intent import plan,LOCAL_TZ
from smart_query import query

ROOT=Path(__file__).resolve().parents[1]
NOW=datetime(2026,10,29,12,tzinfo=LOCAL_TZ)
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--rows',type=int,default=100000);parser.add_argument('--reuse');args=parser.parse_args()
 base=Path(args.reuse) if args.reuse else ROOT/'build'/f'smart-search-benchmark-{args.rows}-{int(time.time())}'
 if args.reuse:
  marker=json.loads((base/'synthetic.json').read_text());assert marker['kind']=='smart-search-synthetic' and marker['rows']==args.rows
 else:base.mkdir(parents=True,exist_ok=False)
 catalog=AssetCatalog(base);service=AssetIntelligence(catalog,Providers(base),autostart=False)
 try:
  if not args.reuse:
   started=time.perf_counter()
   with catalog.connect() as c:
    c.execute('PRAGMA synchronous=NORMAL');c.execute("INSERT INTO roots(id,path,created) VALUES('r','S:/synthetic-only',?)",(NOW.timestamp(),))
    for start in range(0,args.rows,5000):
     items=[];results=[];frames=[];wear=[];dates=[]
     for i in range(start,min(start+5000,args.rows)):
      item=f'{i:032x}';modified=NOW.timestamp()-((i*97)%365)*86400-(i%86400);gender='female' if i%10 not in (2,5,6,7) else 'male' if i%10 in (2,5) else 'unknown'
      category='jeans' if i%10==1 else 'dress' if i%10==3 else 'coat';color='blue' if i%10 in (1,5,8) else 'green' if i%10==3 else 'red'
      terms=f'gender{gender} category{category} color{color} scene{("studio" if i%2 else "outdoor")} 女 人 外 套 红 色 蓝 牛 仔 裤'
      items.append((item,'r',f'S:/synthetic-only/{i}.jpg',f'{i}.jpg','',f'{i}.jpg','.jpg',100,modified,'f',f'pair{i}',modified))
      results.append((item,'f','fixture','fixture','[]','合成基准，不代表模型准确率',1.))
      if i%10==4:terms+=' categoryjeans colorblue'
      frames.append((i+1,item,0,'f',1.,terms))
      wear.append((i+1,item,modified,0,0,gender,category,color))
      if i%10==4:wear.append((i+1,item,modified,0,1,gender,'jeans','blue'))
      if i%2==0:dates.append((item,'f',modified-86400*2))
     c.executemany('INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,pair_key,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',items)
     c.executemany('INSERT INTO intelligence_results VALUES(?,?,?,?,?,?,?)',results)
     c.executemany('INSERT INTO smart_frames(id,item,frame,fingerprint,result_time,terms) VALUES(?,?,?,?,?,?)',frames)
     c.executemany('INSERT INTO smart_wear VALUES(?,?,?,?,?,?,?,?)',wear)
     c.executemany('INSERT INTO smart_dates VALUES(?,?,?)',dates);c.commit()
     if (start+5000)%100000==0:print(f'已生成 {start+5000:,} 条合成解析资产',flush=True)
    c.execute('DELETE FROM smart_pending');c.execute("UPDATE smart_state SET value='1' WHERE key IN ('results_done','dates_done')");c.execute('ANALYZE');c.commit();c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
   (base/'synthetic.json').write_text(json.dumps({'kind':'smart-search-synthetic','rows':args.rows,'build_seconds':round(time.perf_counter()-started,2)}))
  metrics={}
  scenarios={'归档首屏':lambda:catalog.items(limit=60),'搜索入口':lambda:service.search_state(),'昨天':lambda:query(catalog,plan('昨天的图片',now=NOW)),
    '上个月':lambda:query(catalog,plan('上个月的图片',now=NOW)),
    '女性红外套':lambda:query(catalog,plan('女人穿着红色外套',now=NOW)),
    '女性蓝牛仔裤':lambda:query(catalog,plan('女人穿着蓝色牛仔裤',now=NOW)),
    '同人两件衣服':lambda:query(catalog,plan('女人穿着红色外套和蓝色牛仔裤',now=NOW)),
    '常见颜色':lambda:query(catalog,plan('红色',now=NOW)),
    '没有命中':lambda:query(catalog,plan('女人穿紫色连衣裙',now=NOW)),
    '日期与着装':lambda:query(catalog,plan('上个月女人穿红色外套',now=NOW))}
  for name,run in scenarios.items():
   samples=[];failure=None
   for i in range(11):
    start=time.perf_counter()
    try:result=run()
    except Exception as error:failure=str(error);break
    elapsed=(time.perf_counter()-start)*1000
    if i==0:first=elapsed
    else:samples.append(elapsed)
   metrics[name]={'error':failure} if failure else {'first_ms':round(first,3),'p50_ms':round(statistics.median(samples),3),'p95_ms':round(sorted(samples)[-1],3),'rows':len(result.get('rows',[]))}
   print(name,json.dumps(metrics[name],ensure_ascii=False),flush=True)
  original_connect=catalog.connect;traces=[]
  @contextmanager
  def tracing():
   with original_connect() as c:
    c.set_trace_callback(lambda sql:traces.append(sql) if sql.startswith('SELECT i.id') or sql.startswith('SELECT DISTINCT i.id') else None)
    yield c
  catalog.connect=tracing
  try:
   for name in ('上个月','女性红外套','日期与着装','常见颜色'):scenarios[name]()
  finally:catalog.connect=original_connect
  with original_connect() as c:
   plans=[{'sql':sql,'plan':[tuple(row) for row in c.execute('EXPLAIN QUERY PLAN '+sql)]} for sql in traces]
  (base/'explain.json').write_text(json.dumps(plans,ensure_ascii=False,indent=2),encoding='utf-8')
  report={'rows':args.rows,'data':str(base),'scope':'短描述合成解析索引；无模型调用、无原图文件；首次请求不等同清空操作系统缓存的物理冷启动；10 次后续请求 p95 取最大值','metrics':metrics}
  (base/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
  print(base,flush=True)
 finally:service.close();catalog.close()
if __name__=='__main__':main()
