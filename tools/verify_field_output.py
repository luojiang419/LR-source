"""回读全部真实输出与源件；不改写照片或业务库。"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib,io,json,os,re,sqlite3,sys,time
from PIL import Image
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'选片助手'))
os.environ.setdefault('FRAMEKEEP_APP_ROOT','D:/Program Files/Framekeep')
from metadata_writer import MetadataWriter,jpeg_thumbnail
out=Path(sys.argv[1]).resolve();data=out/'data'
manifest=json.loads((out/'manifest.json').read_text(encoding='utf8'));photos=Path(manifest.get('photos_root',out/'photos'));source=Path(manifest['source'])
assert photos!=source and out.is_relative_to(ROOT/'build/field-trial')
visual=json.loads((out/'visual-review.json').read_text(encoding='utf8'))['labels'];expected={r['label']:r['visual_sku'] for r in visual}
groups={name:g for g in manifest['groups'] for name in g['names']};labels={g['label'] for g in manifest['groups']}
with sqlite3.connect(data/'sku.sqlite3') as c:
    choices=dict(c.execute('SELECT capture_id,sku FROM ocr_choices'))
    wrong_choices=[r['label'] for r in visual if choices.get(r['capture_id'],'').upper()!=r['visual_sku'].upper()]
with sqlite3.connect(data/'asset_archive.sqlite3') as c:
    current=[dict(zip(['path','status','reason','sku'],r)) for r in c.execute("""SELECT m.path,m.status,m.reason,a.sku FROM metadata_jobs m
        JOIN assets a ON a.id=m.asset AND a.revision=m.revision
        WHERE coalesce(json_extract(m.fields,'$._descriptive'),0)=0 AND m.id=(SELECT n.id FROM metadata_jobs n
        WHERE n.asset=m.asset AND n.revision=m.revision AND n.file=m.file AND n.path=m.path
        AND coalesce(json_extract(n.fields,'$._descriptive'),0)=0 ORDER BY n.created DESC,n.rowid DESC LIMIT 1)""")]
    file_hashes=dict(c.execute('SELECT l.path,f.sha FROM locations l JOIN files f ON f.id=l.file'))
if wrong_choices or len(current)!=len(manifest['files']) or any(r['status']!='applied' for r in current):
    raise RuntimeError('尚未满足全量验证前置条件：'+json.dumps({'wrong_choices':wrong_choices,'current_jobs':len(current),'status_counts':{s:sum(r['status']==s for r in current) for s in {r['status'] for r in current}}},ensure_ascii=False))
check_data=out/'verification-work';check_data.mkdir(exist_ok=True);writer=MetadataWriter(ROOT,check_data)
report={'started':time.time(),'expected_files':len(manifest['files']),'checked':0,'failures':[],'legacy_keyword_files':[],
        'source_unchanged':True,'pixels_unchanged':True,'properties_correct':True,'non_target_fields_unchanged':True,'passed':False}
allowed={'SourceFile','XMP-dc:Subject','XMP-x:XMPToolkit','XMP-xmp:Rating','IFD0:XPSubject','IFD0:XPKeywords','IFD0:ThumbnailOffset','IFD1:ThumbnailOffset'}
def verify(name):
    target=photos/name;original=source/name;original_bytes=original.read_bytes();after=target.read_bytes();fail=[]
    source_ok=hashlib.sha256(original_bytes).hexdigest()==manifest['files'][name]['sha']
    with Image.open(io.BytesIO(after)) as im:pixel_ok=hashlib.sha256(im.convert('RGB').tobytes()).hexdigest()==manifest['files'][name]['pixels']
    fields=writer.read(after,full=True);before=writer.read(original_bytes,full=True);code=expected[groups[name]['label']]
    if str(fields.get('XMP-fk:SKU','')).upper()!=code.upper():fail.append('XMP主款号')
    if str(fields.get('IFD0:XPSubject','')).upper()!=code.upper():fail.append('Windows主题')
    keywords=fields.get('XMP-dc:Subject',[]);keywords=keywords if isinstance(keywords,list) else [keywords]
    if 'SKU:'+code.upper() not in [str(k).upper() for k in keywords]:fail.append('XMP款号关键词')
    windows=[k.strip() for k in str(fields.get('IFD0:XPKeywords','')).split(';')]
    if 'SKU:'+code.upper() not in [k.upper() for k in windows]:fail.append('Windows款号关键词')
    if int(fields.get('XMP-xmp:Rating',0) or 0)!=(2 if name in labels else 0):fail.append('星级')
    if file_hashes.get(str(target))!=hashlib.sha256(after).hexdigest():fail.append('资产登记哈希')
    changes=[key for key,value in before.items() if key not in allowed and not key.startswith(('File:','System:','ExifTool:','Composite:','XMP-fk:'))
             and not re.fullmatch(r'MPImage\d+:MPImageStart',key) and fields.get(key)!=value]
    thumb_before=jpeg_thumbnail(original_bytes,before);thumb_after=jpeg_thumbnail(after,fields)
    if thumb_before is not None and thumb_after!=thumb_before:fail.append('缩略图')
    for side in (target.with_suffix('.xmp'),Path(str(target)+'.xmp')):
        if side.exists():
            side_fields=writer.read(side)
            if side_fields.get('XMP-fk:SKU') and str(side_fields['XMP-fk:SKU']).upper()!=code.upper():fail.append('侧车主款号')
    legacy=sorted({str(k) for k in [*keywords,*windows] if str(k).startswith('SKU:') and str(k).upper()!='SKU:'+code.upper()})
    if legacy:fail.append('历史受管SKU关键词残留')
    return {'name':name,'source_ok':source_ok,'pixel_ok':pixel_ok,'fields':fail,'non_target':changes,'legacy_keywords':legacy}
def save():(out/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
try:
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks={pool.submit(verify,name):name for name in manifest['files']}
        for future in as_completed(tasks):
            name=tasks[future]
            try:
                result=future.result();report['checked']+=1
                report['source_unchanged'] &= result['source_ok'];report['pixels_unchanged'] &= result['pixel_ok']
                report['properties_correct'] &= not result['fields'];report['non_target_fields_unchanged'] &= not result['non_target']
                if not result['source_ok'] or not result['pixel_ok'] or result['fields'] or result['non_target']:report['failures'].append(result)
                if result['legacy_keywords']:report['legacy_keyword_files'].append({'name':name,'keywords':result['legacy_keywords']})
            except Exception as exc:report['failures'].append({'name':name,'error':str(exc)})
            if report['checked']%100==0:save();print('全量回读',report['checked'],'/',report['expected_files'],'失败',len(report['failures']),flush=True)
    report['xmp_sources_unchanged']=all(hashlib.sha256((source/Path(g['label']).with_suffix('.xmp')).read_bytes()).hexdigest()==g['xmp_sha'] for g in manifest['groups'])
    report['integrity']={}
    for name in ('framekeep.sqlite3','sku.sqlite3','asset_archive.sqlite3'):
        with sqlite3.connect(data/name) as c:report['integrity'][name]=c.execute('PRAGMA integrity_check').fetchone()[0]
    report['finished']=time.time();report['passed']=report['checked']==report['expected_files'] and not report['failures'] and report['xmp_sources_unchanged'] and all(v=='ok' for v in report['integrity'].values());save()
    run=json.loads((out/'run.json').read_text(encoding='utf8'));run['passed']=report['passed'];run['verified_at']=report['finished'];(out/'run.json').write_text(json.dumps(run,ensure_ascii=False,indent=2),encoding='utf8')
finally:writer.close()
print(json.dumps({k:v for k,v in report.items() if k not in ('failures','legacy_keyword_files')},ensure_ascii=False))
raise SystemExit(0 if report['passed'] else 1)
