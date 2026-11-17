"""同一NAS上12张实图的成对副本对比；每组前4张预热，不参与56款统计。"""
from pathlib import Path
import importlib.util,json,os,shutil,sys,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'选片助手'))
os.environ['FRAMEKEEP_APP_ROOT']='D:/Program Files/Framekeep'
from metadata_writer import MetadataWriter,sha
name=sys.argv[1] if len(sys.argv)>1 else 'nas-io-benchmark'
assert name.replace('-','').isalnum()
out=ROOT/'build/field-trial'/name;out.mkdir(exist_ok=False)
manifest=json.loads((ROOT/'build/field-trial/nas-final/manifest.json').read_text(encoding='utf8'))
root=Path(manifest['photos_root'])/('性能对照-'+name);root.mkdir(exist_ok=False)
spec=importlib.util.spec_from_file_location('prior_metadata_writer',ROOT/'backup/v031/metadata_writer.py');prior=importlib.util.module_from_spec(spec);spec.loader.exec_module(prior)
results=[]
for method,cls in [('before_snapshot',prior.MetadataWriter),('after_snapshot',MetadataWriter)]:
    data=out/method;data.mkdir();photos=root/method;photos.mkdir()
    paths=[]
    for name in [f'XSY_{i}.JPG' for i in range(2300,2312)]:
        path=photos/name;shutil.copy2(Path(manifest['source'])/name,path);paths.append(path)
    writer=cls(ROOT,data)
    try:
        for index,path in enumerate(paths):
            digest=sha(path);started=time.perf_counter()
            with writer.exclusive(path):payload,target,journal=writer.write(path,{'SKU':'BENCHMARK-100'},digest)
            writer.commit(payload,target,journal);writer.commit_sidecars(journal);writer.verify_committed(journal)
            item={'method':method,'name':path.name,'warmup':index<4,'seconds':time.perf_counter()-started};results.append(item);print(item,flush=True)
    finally:writer.close()
    (out/'result.json').write_text(json.dumps({'note':'同一NAS、同12张源图的独立副本；每组前4张预热。运行期间全量队列仍在工作，因此为现场对照而非实验室带宽基准。','samples':results},ensure_ascii=False,indent=2),encoding='utf8')
