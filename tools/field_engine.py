"""隔离实测入口：调用同一产品实现，记录耗时，不注入模型答案。"""
from pathlib import Path
import cProfile,json,os,runpy,sys,threading,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'选片助手'))
from asset_archive import AssetArchive
out=Path(os.environ['FRAMEKEEP_DATA']);lock=threading.Lock();profiles=0;context=threading.local()
def emit(value):
    with lock:
        with (out/'field-timings.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(value,ensure_ascii=False)+'\n')
for name in ('ensure','sync_stream','_refresh_catalog','process_one'):
    original=getattr(AssetArchive,name)
    def wrap(self,*args,_original=original,_name=name,**kwargs):
        global profiles
        start=time.time();prof=None
        if _name=='process_one' and profiles<12:
            with self.connect() as c:queued=c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued' LIMIT 1").fetchone()
            if queued:prof=cProfile.Profile();prof.enable();profiles+=1
        try:return _original(self,*args,**kwargs)
        finally:
            if prof:prof.disable();prof.dump_stats(str(out/f'profile-{profiles}.pstats'))
            seconds=time.time()-start
            if seconds>.01:emit({'kind':_name,'started':start,'seconds':seconds,'capture':args[1] if _name=='ensure' and len(args)>1 else None})
    setattr(AssetArchive,name,wrap)
from sku_vision import recognize
import sku_service
def timed_recognize(provider,image):
    start=time.time()
    try:return recognize(provider,image)
    finally:emit({'kind':'model','started':start,'seconds':time.time()-start,'model':provider.get('model'),'capture_id':getattr(context,'capture_id',None),'job_id':getattr(context,'job_id',None)})
original_label=sku_service.SkuService._process_label
def traced_label(self,job,**kwargs):
    context.capture_id=job['capture_id'];context.job_id=job['id'];start=time.time()
    try:return original_label(self,job,**kwargs)
    finally:emit({'kind':'label','capture_id':job['capture_id'],'job_id':job['id'],'started':start,'seconds':time.time()-start})
sku_service.SkuService._process_label=traced_label
# __init__ 默认参数已绑定，替换初始化包装，仍调用真实模型。
old_init=sku_service.SkuService.__init__
def init(self,*args,**kwargs):kwargs.setdefault('recognizer',timed_recognize);old_init(self,*args,**kwargs)
sku_service.SkuService.__init__=init
runpy.run_path(str(ROOT/'选片助手/server.py'),run_name='__main__')
