"""极速归档：版本清单预览、流式源摘要、单次目标读回、原子提交。"""
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from contextlib import nullcontext
from pathlib import Path
import hashlib,json,os,shutil,threading,time,uuid

from folder_archive_metadata import signature
from folder_archive_commit import prepare,recover,store
from metadata_writer import sha,FIELDS
from rating_xmp import sidecar_paths

def inventory(root,progress=None):
    from sku_folder_archive import files_under
    versions={}
    files_under(root,progress=progress,stage='scanning_verify',versions=versions)
    return versions

def copy_stream(writer,source,target,expected,on_bytes):
    if signature(source)!=expected:raise ValueError('来源文件版本已改变：'+str(source))
    digest=hashlib.sha256();size=0
    with writer.exclusive(source),source.open('rb',buffering=4*1024*1024) as src,target.open('xb',buffering=4*1024*1024) as dst:
        while block:=src.read(4*1024*1024):
            digest.update(block);size+=len(block)
            view=memoryview(block)
            while view:
                count=dst.write(view)
                if not count:raise OSError('目标文件写入未完成')
                view=view[count:]
            on_bytes(len(block))
        dst.flush()
        if source.suffix.lower() not in ('.jpg','.jpeg'):os.fsync(dst.fileno())
    if size!=expected[0] or signature(source)!=expected:raise ValueError('复制期间来源文件改变：'+str(source))
    shutil.copystat(source,target)
    return digest.hexdigest()

def write_xmp(writer,path,fields):
    before=writer.read(path,full=True) if path.exists() else {}
    for key in ('AssetId','FileId'):
        if before.get('XMP-fk:'+key) and str(before['XMP-fk:'+key]).replace('-','')!=str(fields[key]).replace('-',''):
            raise ValueError('来源XMP已有其他资产身份，需复核：'+str(path))
    values={key:before.get('XMP-fk:'+key) or '' for key in FIELDS};values.update(fields)
    subjects=before.get('XMP-dc:Subject') or [];subjects=subjects if isinstance(subjects,list) else [subjects]
    subjects=[v for v in subjects if not (isinstance(v,str) and v.startswith(('SKU:','ShootType:')))]
    subjects+=[f'SKU:{fields["SKU"]}',f'ShootType:{fields["ShootType"]}']
    args=[f'-XMP-fk:{key}={values[key]}' for key in FIELDS]+['-XMP-xmp:Rating='+str(fields['Rating'])]
    args+=[f'-XMP-dc:Subject={value}' for value in dict.fromkeys(subjects)]
    payload=writer.edit(path,args);after=writer.read(payload,full=True)
    for key in FIELDS:
        if str(after.get('XMP-fk:'+key) or '')!=str(values[key]):raise ValueError('XMP字段读回不一致：'+key)
    if int(after.get('XMP-xmp:Rating',-1))!=fields['Rating']:raise ValueError('XMP评级读回不一致')
    for key,value in before.items():
        if key=='SourceFile' or key in ('XMP-x:XMPToolkit','XMP-dc:Subject','XMP-xmp:Rating') or key.startswith(('File:','System:','ExifTool:','XMP-fk:')):continue
        if after.get(key)!=value:raise ValueError('XMP原有编辑字段改变：'+key)
    with path.open('wb') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
    written_hash=sha(path)
    if written_hash!=hashlib.sha256(payload).hexdigest():raise ValueError('XMP落盘校验失败')
    return written_hash

def execute_fast(core,plan,*,progress=None,on_plan_changed=None,preprocess_context=None):
    from sku_folder_archive import metadata_fields,make_thumbnail
    from sku_archive_preprocess import arrange_sources,safe_path
    progress=progress or (lambda **_:None)
    core.preflight_target(Path(plan['target']),plan['items'])
    if inventory(plan['source'],progress)!=plan['manifest']:raise ValueError('最终大档清单/版本在预览后改变，未整理或复制')
    for path,digest in plan.get('sidecar_manifest',{}).items():
        if not Path(path).is_file() or sha(path)!=digest:raise ValueError('来源XMP在预览后改变')
    with preprocess_context(plan) if preprocess_context and plan.get('preprocess') else nullcontext():
        plan=arrange_sources(plan,core.database,progress=progress)
        if on_plan_changed:on_plan_changed(plan)
    target=Path(plan['target']);target.mkdir(parents=True,exist_ok=True)
    thumb_dir=target/'.核查缩略图';thumb_dir.mkdir(exist_ok=True)
    logs=(target if core.database.is_relative_to(Path(plan['reference_root'])) else core.database.parent)/'.归档文件提交日志'
    receipt_path=target/('.归档回执-'+str(uuid.uuid5(uuid.NAMESPACE_URL,plan['source']+'|'+plan['reference_root'])))
    receipts=json.loads(receipt_path.read_text(encoding='utf8')) if receipt_path.exists() else {}
    receipts.update(recover(logs,target,fast=True))
    lock=threading.RLock();completed=0;transferred=0;started=time.monotonic()
    total=len(plan['items']);byte_total=sum(row['input_signature'][0] for row in plan['items'])
    def pulse(path='',amount=0):
        nonlocal transferred
        with lock:
            transferred+=amount
            values={'stage':'copying_fast','done':completed,'total':total,'path':str(path),'workers':2,
                    'bytes_done':transferred,'bytes_total':byte_total,'rate_bps':transferred/max(.01,time.monotonic()-started)}
        progress(**values)
    def checkpoint():
        temporary=receipt_path.with_suffix('.tmp');temporary.write_text(json.dumps(receipts,ensure_ascii=False),encoding='utf8');os.replace(temporary,receipt_path)
    def process(expected):
        nonlocal completed
        row=dict(expected);source=Path(row['source']);destination=Path(row['destination']);safe_path(destination,target)
        if signature(source)!=row['input_signature']:raise ValueError('来源文件版本已改变：'+str(source))
        destination.parent.mkdir(parents=True,exist_ok=True)
        xmp=Path(row['xmp']) if row.get('xmp') else None
        if xmp and len(sidecar_paths(source))>1:raise ValueError('来源包含多个XMP，需复核')
        side_hashes=[plan.get('sidecar_manifest',{}).get(str(p)) for p in (source.with_suffix('.xmp'),Path(str(source)+'.xmp'))]
        side_hashes=list(dict.fromkeys(v for v in side_hashes if v));input_xmp_sha=side_hashes[0] if len(side_hashes)==1 else None
        reusable=False
        with lock:receipt=receipts.get(str(destination))
        if receipt and not row.get('file_id') and receipt.get('metadata_fingerprint')==row['metadata_fingerprint']:
            row['file_id']=receipt.get('fields',{}).get('FileId')
        fields=metadata_fields(row,row['metadata_fingerprint'])
        if destination.exists():
            if xmp and receipt and 'input_xmp_sha' in receipt and receipt['input_xmp_sha']!=input_xmp_sha:raise ValueError('来源XMP编辑已更新，现有归档需复核，未覆盖')
            reusable=receipt and receipt.get('source_signature')==row['input_signature'] and receipt.get('output_signature')==signature(destination) and receipt.get('fields')==fields and receipt.get('input_xmp_sha')==input_xmp_sha
            if reusable and xmp:reusable=xmp.is_file() and receipt.get('xmp_signature')==signature(xmp)
            if reusable:
                row.update(input_sha256=receipt.get('input_sha256'),output_sha256=receipt['sha256'],xmp_sha256=receipt.get('xmp_sha256'),
                           status='identical_verified_skip',archived_at=receipt.get('archived_at'),verification_method='verified_receipt_and_file_version')
            else:
                row['input_sha256']=sha(source)
                if not row.get('file_id'):
                    current=core.writer.read_asset(destination);row['file_id']=current.get('XMP-fk:FileId');fields=metadata_fields(row,row['metadata_fingerprint'])
                row.update(core.verify_item(row),status='identical_verified_skip',archived_at=receipt.get('archived_at') if receipt else None,
                           verification_method='existing_file_full_verification')
        else:
            stage=destination.parent/('.archive-'+uuid.uuid4().hex);stage.mkdir();staged=stage/destination.name
            try:
                row['input_sha256']=copy_stream(core.writer,source,staged,row['input_signature'],lambda n:pulse(source,n))
                if expected.get('input_sha256') and row['input_sha256']!=expected['input_sha256']:raise ValueError('复制时来源与原有摘要不一致')
                if xmp:
                    output_hash=sha(staged)
                    if output_hash!=row['input_sha256']:raise ValueError('RAW复制读回校验失败')
                    metadata=staged.with_suffix('.xmp');sides=sidecar_paths(source)
                    if len(sides)>1:raise ValueError('来源包含多个XMP，需复核')
                    if sides:shutil.copy2(sides[0],metadata)
                    if bool(sides)!=bool(input_xmp_sha) or sides and sha(metadata)!=input_xmp_sha:raise ValueError('来源XMP在预览后新增或改变')
                    xmp_hash=write_xmp(core.writer,metadata,fields)
                else:
                    staged.chmod(staged.stat().st_mode|0o200)
                    payload,metadata,journal=core.writer.write(staged,fields,row['input_sha256'],mode='sidecar',jpeg_fast=True)
                    with metadata.open('wb') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
                    output_hash=sha(staged);xmp_hash=None
                    if output_hash!=journal['after_hash']:raise ValueError('JPG写入落盘读回校验失败')
                proof={'source_signature':row['input_signature'],'output_signature':signature(staged),'input_sha256':row['input_sha256'],
                       'fields':fields,'metadata_fingerprint':row['metadata_fingerprint'],'xmp_sha256':xmp_hash,
                       'xmp_signature':signature(metadata) if xmp else None,'input_xmp_sha':input_xmp_sha}
                log,entry=prepare(logs,row,staged,metadata,verified_sha=output_hash,proof=proof)
                if xmp and xmp.exists():raise ValueError('目标XMP已存在，不能覆盖')
                os.rename(staged,destination)
                if xmp:os.rename(metadata,xmp)
                entry['state']='committed';store(log,entry)
                row.update(output_sha256=output_hash,xmp_sha256=xmp_hash,status='verified',archived_at=entry['archived_at'],verification_method='stream_sha256_and_single_readback')
            finally:
                for path in stage.iterdir():
                    if path.is_file():
                        path.chmod(path.stat().st_mode|0o200);path.unlink()
                stage.rmdir()
        row['output_signature']=signature(destination);row['xmp_signature']=signature(xmp) if xmp else None
        with lock:
            receipts[str(destination)]={'sha256':row['output_sha256'],'archived_at':row.get('archived_at'),'input_sha256':row.get('input_sha256'),
                'source_signature':row['input_signature'],'output_signature':row['output_signature'],'fields':fields,
                'metadata_fingerprint':row['metadata_fingerprint'],'xmp_sha256':row.get('xmp_sha256'),'xmp_signature':row['xmp_signature'],'input_xmp_sha':input_xmp_sha}
            completed+=1
            if completed%64==0:checkpoint()
        thumb=thumb_dir/(fields['FileId']+'.jpg')
        preview=Path(row['reference']) if row.get('raw') and Path(row['reference']).suffix.lower() in ('.jpg','.jpeg') else destination
        try:
            if reusable and receipt.get('thumbnail_signature') and thumb.is_file() and signature(thumb)==receipt['thumbnail_signature']:
                width,height=receipt['thumbnail_size']
            else:width,height=make_thumbnail(core.writer,preview,thumb)
            row.update(thumbnail_path=str(thumb),thumbnail_width=width,thumbnail_height=height)
            with lock:receipts[str(destination)].update(thumbnail_signature=signature(thumb),thumbnail_size=[width,height])
            if preview!=destination:row['preview_note']='RAW预览取同一快门的现场JPG'
        except Exception as error:row['thumbnail_error']=str(error)
        pulse(destination)
        return row
    pulse()
    result={**plan,'demo':False,'real_validation':True,'recursive':True,'items':[],'verification_policy':'fast-stream-v1'}
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='sku-archive-copy') as pool:
        pending=set();iterator=iter(plan['items'])
        for _ in range(4):
            row=next(iterator,None)
            if row is not None:pending.add(pool.submit(process,row))
        while pending:
            done,_=wait(pending,timeout=.25,return_when=FIRST_COMPLETED)
            if not done:pulse();continue
            for future in done:
                pending.remove(future);result['items'].append(future.result())
                row=next(iterator,None)
                if row is not None:pending.add(pool.submit(process,row))
    with lock:checkpoint()
    result['items'].sort(key=lambda r:r['id'])
    for index,row in enumerate(result['items']):
        progress(stage='verifying',done=index,total=total,path=row['destination'])
        if not Path(row['destination']).is_file() or signature(row['destination'])!=row['output_signature']:raise ValueError('已归档目标在提交后改变')
        if row.get('xmp') and (not Path(row['xmp']).is_file() or signature(row['xmp'])!=row['xmp_signature']):raise ValueError('配套XMP在提交后改变')
    if inventory(plan['source'],progress)!=plan['manifest']:raise ValueError('最终来源清单/版本在归档期间改变')
    from datetime import datetime,timezone
    result.update(verified=total,expected_images=total,expected_files=total+plan['expected_xmp'],xmp=plan['expected_xmp'],
                  excluded=plan.get('excluded_count',0),result='verified',completed_at=datetime.now(timezone.utc).isoformat())
    return result
