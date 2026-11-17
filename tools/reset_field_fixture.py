"""仅重置本任务隔离全量夹具；保留旧数据库和报告作为失败证据。"""
from pathlib import Path
import json,os,subprocess,sys,time,shutil,sqlite3
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'选片助手'))
os.environ['FRAMEKEEP_APP_ROOT']='D:/Program Files/Framekeep'
import media_engine as media
from catalog_store import Store
from metadata_writer import MetadataWriter
out=(Path(sys.argv[1]).resolve() if len(sys.argv)>1 else (ROOT/'build/field-trial/full').resolve())
assert out.is_relative_to(ROOT.resolve()/'build/field-trial')
manifest=json.loads((out/'manifest.json').read_text(encoding='utf8'))
photos=Path(manifest.get('photos_root',str(out/'photos'))).resolve();data=out/'data'
assert (photos.is_relative_to(ROOT.resolve()/'build/field-trial') and photos.name=='photos') or (photos.parent==Path(r'\\192.168.0.181\新摄影\细节图') and photos.name.startswith('SHIYING隔离实战-20261105-'))
(out/'fixture-ready.json').unlink(missing_ok=True)
stamp=str(int(time.time()));old=out/('data-attempt-'+stamp);data.rename(old);data.mkdir()
if (out/'run.json').exists():shutil.copy2(out/'run.json',out/('run-attempt-'+stamp+'.json'))
for p in photos.glob('*.xmp'):
    assert p.resolve().parent==photos
    p.unlink()
w=MetadataWriter(ROOT,data);dirty=set()
if (old/'asset_archive.sqlite3').exists():
 with sqlite3.connect(old/'asset_archive.sqlite3') as c:
    dirty.update(r[0] for r in c.execute("SELECT path FROM metadata_jobs WHERE status IN ('applied','running','failed')"))
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='rating_file_sync'").fetchone():dirty.update(r[0] for r in c.execute('SELECT path FROM rating_file_sync'))
else:dirty={str(p) for p in photos.glob('*.JPG')}
dirty=[p for p in dirty if Path(p).suffix.lower()=='.jpg' and Path(p).resolve().parent==photos]
for start in range(0,len(dirty),100):
 w.run('-overwrite_original','-XMP-fk:all=','-XMP-xmp:Rating=0',*dirty[start:start+100])
w.close()
store=Store(data,ROOT);sid=store.ensure_live_session(photos)
with sqlite3.connect(old/'framekeep.sqlite3') as c:records=[json.loads(r[0]) for r in c.execute('SELECT data FROM captures ORDER BY position')]
by_name={r['jpg']:i for i,r in enumerate(records)};paths=[Path(p) for p in dirty]
for start in range(0,len(paths),100):
    batch=paths[start:start+100];metadata=media.read_metadata(batch,media.find_exiftool(ROOT))
    for p,m in zip(batch,metadata):records[by_name[p.name]]=media.capture_record(photos,p,None,m)
store.append_live(sid,records);store.activate(sid)
manifest=json.loads((out/'manifest.json').read_text(encoding='utf8'));manifest['session_id']=sid
for g in manifest['groups']:g['capture_id']=next(r['id'] for r in records if r['jpg']==g['label'])
(out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
shutil.copy2(Path('D:/Program Files/Framekeep/data/model-providers.json'),data/'model-providers.json')
(data/'desktop-preferences.json').write_text(json.dumps({'close_behavior':'exit','startup_mode':'disabled'}),encoding='utf8')
(out/'fixture-ready.json').write_text(json.dumps({'session_id':sid,'count':len(records),'ready_at':time.time()}),encoding='utf8')
print('全量隔离副本已重置；旧报告与数据库已保留',flush=True)
