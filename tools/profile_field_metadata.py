from pathlib import Path
import cProfile,os,shutil,sys,time,pstats
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'选片助手'))
os.environ['FRAMEKEEP_APP_ROOT']='D:/Program Files/Framekeep'
import media_engine as media
from catalog_store import Store
from asset_archive import AssetArchive
out=ROOT/'build/field-trial'/(sys.argv[1] if len(sys.argv)>1 else 'profile');out.mkdir(exist_ok=False);data=out/'data';data.mkdir();photos=out/'photos';photos.mkdir()
paths=[]
for p in sorted((ROOT/'build/field-trial/optimized/photos').glob('*.JPG'))[:12]:shutil.copy2(p,photos/p.name);paths.append(photos/p.name)
store=Store(data,ROOT);sid=store.ensure_live_session(photos);records=[media.capture_record(photos,p,None,m) for p,m in zip(paths,media.read_metadata(paths,media.find_exiftool(ROOT)))];store.append_live(sid,records)
archive=AssetArchive(store,autostart=False)
start=time.perf_counter();preview=archive.preview(sid,[r['id'] for r in records],'LC-PROFILE','现场记录');archive.apply(preview['preview_id']);print('prepare',time.perf_counter()-start,flush=True)
prof=cProfile.Profile();prof.enable();start=time.perf_counter()
for _ in records:archive.process_one()
prof.disable();prof.dump_stats(str(out/'metadata.pstats'));print('write',time.perf_counter()-start,flush=True);pstats.Stats(prof).sort_stats('cumtime').print_stats(24);archive.close();archive.writer.close()
