"""在备份目录之外新建NAS全量副本，所有写操作只允许新建隔离根路径。"""
from pathlib import Path
import hashlib,json,os,shutil,sqlite3,sys,time,uuid
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'选片助手'))
from field_trial import SOURCE,APP,write,read
from catalog_store import Store
from metadata_writer import MetadataWriter,sha
import media_engine as media
from concurrent.futures import ThreadPoolExecutor,as_completed
out=ROOT/'build/field-trial/nas-final';resume='--resume' in sys.argv
out.mkdir(exist_ok=resume);data=out/'data';data.mkdir(exist_ok=resume)
base=Path(r'\\192.168.0.181\新摄影\细节图')
manifest=read(out/'manifest.json') if resume else read(ROOT/'build/field-trial/full/manifest.json')
photos=Path(manifest['photos_root']) if resume else base/('SHIYING隔离实战-20261105-'+uuid.uuid4().hex[:8])
assert photos.parent==base and '源图片备份' not in str(photos)
assert photos.name.startswith('SHIYING隔离实战-20261105-')
photos.mkdir(exist_ok=resume)
manifest['photos_root']=str(photos);manifest['prepared_at']=time.time()
write(out/'manifest.json',manifest)
for index,(name,info) in enumerate([] if resume else manifest['files'].items()):
    source=SOURCE/name;target=photos/name;shutil.copy2(source,target)
    if sha(target)!=info['sha']:raise RuntimeError('原素材与初始哈希不一致：'+name)
    if index%100==0:print(f'NAS独立副本 {index+1}/{len(manifest["files"])}',flush=True)
w=MetadataWriter(ROOT,data)
assert len(list(photos.glob('*.JPG')))==len(manifest['files'])
paths=sorted(photos.glob('*.JPG'))
for temporary in photos.glob('*_exiftool_tmp'):
    original=photos/temporary.name.removesuffix('_exiftool_tmp')
    assert temporary.parent==photos and original.name in manifest['files'] and original.exists()
    from PIL import Image
    with Image.open(original) as im:digest=hashlib.sha256(im.convert('RGB').tobytes()).hexdigest()
    assert digest==manifest['files'][original.name]['pixels'],'原图像素校验失败，保留临时文件'
    temporary.unlink() # 仅清理由上次工具超时留下、且原文件像素已校验的临时文件。
checkpoint=out/'normalized-batches.json';done=set(read(checkpoint) if checkpoint.exists() else [])
def normalize(start):
    w.run('-overwrite_original','-XMP-fk:all=','-XMP-xmp:Rating=0',*paths[start:start+100]);return start
with ThreadPoolExecutor(max_workers=4) as pool:
    futures=[pool.submit(normalize,start) for start in range(0,len(paths),100) if start not in done]
    for future in as_completed(futures):
        done.add(future.result());write(checkpoint,sorted(done))
        print(f'NAS副本初始化 {sum(min(100,len(paths)-start) for start in done)}/{len(paths)}',flush=True)
w.close()
store=Store(data,ROOT);sid=store.ensure_live_session(photos);paths=sorted(photos.glob('*.JPG'));records=[]
for start in range(0,len(paths),150):
    batch=paths[start:start+150];metadata=media.read_metadata(batch,media.find_exiftool(ROOT))
    records.extend(media.capture_record(photos,p,None,m) for p,m in zip(batch,metadata))
    print(f'NAS真实EXIF登记 {min(start+150,len(paths))}/{len(paths)}',flush=True)
store.append_live(sid,records);store.activate(sid);manifest['session_id']=sid
for g in manifest['groups']:g['capture_id']=next(r['id'] for r in records if r['jpg']==g['label'])
write(out/'manifest.json',manifest)
review=read(ROOT/'build/field-trial/full/visual-review.json')
for r in review['labels']:r['capture_id']=next(g['capture_id'] for g in manifest['groups'] if g['label']==r['label'])
write(out/'visual-review.json',review)
shutil.copy2(APP/'data/model-providers.json',data/'model-providers.json')
write(data/'desktop-preferences.json',{'close_behavior':'exit','startup_mode':'disabled'})
write(out/'fixture-ready.json',{'session_id':sid,'count':len(records),'ready_at':time.time()})
print('NAS全量夹具准备完成：'+str(photos),flush=True)
