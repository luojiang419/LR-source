"""隔离合成大库基准：路径、SKU、多维结果与中文倒排，不接触用户数据库。"""
import argparse
import json
from pathlib import Path
import statistics
import tempfile
import time

from asset_catalog import AssetCatalog
from asset_intelligence import AssetIntelligence
from model_providers import Providers
from retrieval_schema import dumps, tokens
from test_asset_intelligence import result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--rows',type=int,default=100000);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];output=root/'build/multimodal-search';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='framekeep-search-bench-') as temporary:
        base=Path(temporary);catalog=AssetCatalog(base/'data');photos=base/'synthetic';photos.mkdir();catalog.add_root(str(photos))
        service=AssetIntelligence(catalog,Providers(base/'data'),autostart=False)
        root_id=catalog.state()['roots'][0]['id'];start=time.perf_counter()
        with catalog.connect() as c:
            c.execute('PRAGMA synchronous=NORMAL')
            for begin in range(0,args.rows,1000):
                rows=range(begin,min(begin+1000,args.rows))
                c.executemany('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,sku,normalized,source,last_seen,pair_key)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    [(f'{n:032x}',root_id,str(photos/f'LC{n//10:06d}'/f'IMG_{n:09d}.jpg'),f'LC{n//10:06d}/IMG_{n:09d}.jpg',f'LC{n//10:06d}',f'IMG_{n:09d}.jpg','.jpg',12000,float(n),str(n),f'LC{n//10:06d}',f'LC{n//10:06d}','manual',1.,str(n)) for n in rows])
                for n in rows:
                    color=('红色','蓝色','黑色')[n%3];value=result(color)
                    c.execute('INSERT INTO intelligence_results VALUES(?,?,?,?,?,?,?)',(f'{n:032x}',str(n),'bench','synthetic',dumps([{'seconds':0,'result':value}]),value['summary'],1.))
                    c.execute('INSERT INTO intelligence_fts VALUES(?,?)',(f'{n:032x}',' '.join(tokens(value['summary']+' 摄影棚 翻领'))))
                    c.executemany('INSERT INTO intelligence_dimensions VALUES(?,?,?,?,?)',[(f'{n:032x}',0,k,v,v) for k,v in [('color',color),('category','衬衫')]])
                c.commit()
            c.execute('ANALYZE');c.commit()
        generated=time.perf_counter()-start;metrics={}
        try:
            for name,kwargs in [('sku_exact',{'text':'LC000123'}),('chinese_keyword',{'text':'红色'}),('multiple_dimensions',{'facets':{'color':'蓝色','category':'衬衫'}}),('missing',{'text':'不存在的商品'})]:
                samples=[];error='';count=0
                for _ in range(5):
                    start=time.perf_counter()
                    try:data=service.search(**kwargs);count=len(data['rows'])
                    except ValueError as exc:error=str(exc)
                    samples.append((time.perf_counter()-start)*1000)
                metrics[name]={'p50_ms':round(statistics.median(samples),2),'max_ms':round(max(samples),2),'returned':count,'error':error}
            with catalog.connect() as c:integrity=c.execute('PRAGMA quick_check').fetchone()[0]
            report={'rows':args.rows,'generation_seconds':round(generated,2),'database_mb':round(catalog.db.stat().st_size/1024**2,2),
                    'queries':metrics,'quick_check':integrity,'conditions':'合成数据库、五次新连接查询、OS 缓存未清空；每次最多 60 项；不含图片解码、模型解析或网络盘性能。'}
            (output/f'benchmark-{args.rows}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
        finally:service.close();catalog.close()


if __name__=='__main__':main()
