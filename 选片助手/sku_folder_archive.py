"""按快门元数据身份归档；可先整理大小档目录，照片内容保持不变。"""
from __future__ import annotations
from contextlib import closing
from sku_archive_paths import sku_directory

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid

from PIL import Image, ImageOps
from metadata_writer import sha
from rating_xmp import sidecar_paths
VIDEO_EXTENSIONS={'.mp4','.mov','.mxf','.mkv','.avi','.mts','.m2ts','.webm','.m4v','.wmv'}
from folder_archive_metadata import capture_metadata_batches, signature,metadata_sidecars

EXTENSIONS = {'.jpg', '.jpeg', '.cr3', '.cr2', '.nef', '.arw', '.dng', '.raf'}


def camera_metadata(full):
    def tag(*keys):
        return next((str(full[k]).strip() for k in keys if full.get(k) is not None), '')
    return {
        'model': tag('IFD0:Model', 'XMP-tiff:Model'),
        'serial': tag('ExifIFD:SerialNumber', 'Canon:SerialNumber', 'IFD0:CameraSerialNumber', 'XMP-aux:SerialNumber', 'XMP-exifEX:BodySerialNumber'),
        'time': tag('ExifIFD:DateTimeOriginal', 'XMP-exif:DateTimeOriginal'),
        'subsecond': tag('ExifIFD:SubSecTimeOriginal'),
        'combined': tag('Composite:SubSecDateTimeOriginal', 'XMP-exif:DateTimeOriginal', 'XMP-photoshop:DateCreated'),
        'number': tag('Canon:ShutterCount', 'Track4:ShutterCount', 'Canon:FileNumber', 'ExifIFD:ImageNumber'),
    }


def _capture_values(metadata, *, require_number=True):
    m = dict(metadata)
    stamp = m.get('combined') or m.get('time', '')
    match = re.match(r'^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?', stamp)
    if not match or not m.get('model') or not m.get('serial') or require_number and not m.get('number'):
        raise ValueError('缺少可确定快门身份的相机元数据')
    datetime(*map(int, match.groups()[:6]))  # 不接受无效日期。
    # 优先解析保留零位的复合时间，ExifTool 的数值输出可能把 .08 变成 8。
    sub = match[7] if match[7] is not None else m.get('subsecond', '')
    if not sub or not sub.isdigit():
        raise ValueError('缺少原始拍摄子秒，不能自动猜配')
    serial = str(int(m['serial'])) if m['serial'].isdigit() else m['serial'].casefold()
    if serial in ('0', 'unknown', 'none'):
        raise ValueError('无有效相机序列号')
    values = [re.sub(r'\s+', ' ', m['model']).casefold(), serial,
              '-'.join(match.groups()[:3]) + 'T' + ':'.join(match.groups()[3:6]),
              sub.rstrip('0') or '0']
    if require_number:values.append(str(int(m['number'])) if m['number'].isdigit() else m['number'].casefold())
    return values


def capture_key(metadata):
    return 'capture-v2:' + json.dumps(_capture_values(metadata), ensure_ascii=False, separators=(',', ':'))


def capture_moment_key(metadata):
    return 'capture-moment-v1:' + json.dumps(_capture_values(metadata,require_number=False),ensure_ascii=False,separators=(',',':'))


def match_capture(metadata, by_key, by_moment, *, exported_jpg=False):
    # 有快门序号时坚持完整身份，不能用时间回退掩盖序号不一致。
    if metadata.get('number') or not exported_jpg:
        key=capture_key(metadata)
        return key,by_key.get(key,[]),'capture_metadata'
    moment=capture_moment_key(metadata);candidates=by_moment.get(moment,[])
    if len({r['fingerprint'] for r in candidates})>1:
        raise ValueError('导出JPG的相机与原始子秒时间对应多次快门，无法唯一匹配')
    # 回连原入库完整身份，不给后期导出另造资产/标签归属。
    return candidates[0]['fingerprint'] if candidates else moment,candidates,'exported_jpg_metadata'


def image_pixels(path):
    with Image.open(path) as image:
        rgb = image.convert('RGB')
        return {'size': list(image.size), 'sha256': hashlib.sha256(rgb.tobytes()).hexdigest()}


def files_under(root, *, progress=None, stage='scanning', versions=None):
    root = Path(root).resolve()
    result=[];pending=[root];directories=0;last=0
    def report(path,force=False):
        nonlocal last
        now=time.monotonic()
        if progress and (force or now-last>=.2):
            progress(stage=stage,done=len(result),total=0,discovered=len(result),directories=directories,path=str(path),indeterminate=True)
            last=now
    report(root,True)
    while pending:
        parent=pending.pop();report(parent,True)
        with os.scandir(parent) as entries:
            for entry in entries:
                # Windows DirEntry复用枚举属性；根已解析且不跟随链接，不逐文件resolve NAS路径。
                if entry.is_symlink() or entry.is_junction():raise ValueError('扫描范围包含链接，不能静默越界')
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in ('.shiying','.核查缩略图','.归档文件提交日志'):pending.append(Path(entry.path))
                elif Path(entry.name).suffix.lower() in EXTENSIONS|VIDEO_EXTENSIONS:
                    result.append(Path(entry.path))
                    if versions is not None:
                        info=entry.stat(follow_symlinks=False)
                        versions[str(Path(entry.path))]=[info.st_size,info.st_mtime_ns]
                report(parent)
        directories+=1;report(parent,True)
    return sorted(result)


def manifest_hashes(files,progress=None,stage='manifest_hashing',expected_versions=None):
    result={};total=len(files)
    for index,path in enumerate(files):
        if progress:progress(stage=stage,done=index,total=total,path=str(path),indeterminate=False)
        before=signature(path)
        if expected_versions and expected_versions.get(str(path)) and before!=expected_versions[str(path)]:
            raise ValueError('元数据读取后文件已改变，请重新匹配：'+str(path))
        result[str(path)]=sha(path)
        if signature(path)!=before:raise ValueError('摘要计算期间文件改变，请重新匹配：'+str(path))
    if progress:progress(stage=stage,done=total,total=total,path='',indeterminate=False)
    return result


def metadata_fields(record, key):
    asset = str(uuid.UUID(str(record['asset_id']))) if record.get('asset_id') else str(uuid.uuid5(uuid.NAMESPACE_URL, key))
    file_id = str(uuid.UUID(str(record['file_id']))) if record.get('file_id') else str(uuid.uuid5(uuid.UUID(asset), record.get('file_identity_source',record['source'])))
    return {**record.get('portable_fields',{}), 'AssetId': asset, 'FileId': file_id,
            'SKU': record['sku'], 'ShootType': record['scope'], 'Rating': record['rating'],
            'MetadataRevision': record.get('portable_fields',{}).get('MetadataRevision','1')}


def make_thumbnail(writer, source, output):
    if source.suffix.lower() in ('.jpg', '.jpeg'):
        image = Image.open(source)
        image.draft('RGB',(240,240))
    elif source.suffix.lower() in VIDEO_EXTENSIONS:
        from video_preview import extract_frame
        image=extract_frame(source,240,3,writer.root)
    else:
        import rawpy
        with rawpy.imread(str(source)) as raw:
            thumb = raw.extract_thumb()
            image = Image.open(io.BytesIO(thumb.data)) if isinstance(thumb.data, bytes) else Image.fromarray(thumb.data)
    with image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        image.thumbnail((120, 120))
        image.save(output, 'JPEG', quality=86)
        return image.size


class SkuFolderArchive:
    def __init__(self, writer, database):
        self.writer, self.database = writer, Path(database).resolve()

    def plan(self, source_root, final_root, target_root, *, preprocess=True, include_label_images=False, parent_length=4, direct=False, exclude_unregistered=True, fast=False, progress=None):
        progress=progress or (lambda **_:None)
        if type(parent_length) is not int or not 2<=parent_length<=6:
            raise ValueError('母SKU位数须为2–6的整数')
        if type(preprocess) is not bool:
            raise ValueError('归档前处理须为布尔开关')
        if type(exclude_unregistered) is not bool:raise ValueError('未入库排除开关无效')
        if type(fast) is not bool:raise ValueError('极速归档开关无效')
        if not self.database.is_file():
            raise ValueError('现场SKU数据库不存在')
        if preprocess:
            from sku_archive_preprocess import recover_source_moves
            recover_source_moves(self.database)
        source_root, final_root, target_root = map(lambda p: Path(p).resolve(), (source_root, final_root, target_root))
        if source_root!=final_root and (source_root in final_root.parents or final_root in source_root.parents):
            raise ValueError('大小档目录不能互相包含')
        if preprocess and source_root==final_root and not direct:
            raise ValueError('散图匹配需要独立的大小档目录')
        for source in (source_root, final_root):
            if target_root == source or source in target_root.parents or target_root in source.parents:
                raise ValueError('归档目标不能与来源目录重叠')
        with closing(sqlite3.connect(self.database)) as c, c:
            c.row_factory = sqlite3.Row
            refs = [dict(r) for r in c.execute('SELECT * FROM captures')]
            if any(Path(r['source']).suffix.lower() in VIDEO_EXTENSIONS for r in refs):fast=False
            history=dict(c.execute('SELECT current_path,original_path FROM archive_source_paths')) if c.execute("SELECT 1 FROM sqlite_master WHERE name='archive_source_paths'").fetchone() else {}
        final_records=[r for r in refs if Path(r['source']).is_relative_to(final_root)]
        final_refs={r['source']:r for r in final_records if r['sku']}
        final_by_key={};final_by_moment={}
        for r in final_records:
            metadata=json.loads(r['metadata'])
            try:
                moment=capture_moment_key(metadata)
                fingerprint=capture_key(metadata) if metadata.get('number') else moment
            except ValueError:continue
            candidate={**r,'fingerprint':fingerprint}
            final_by_key.setdefault(fingerprint,[]).append(candidate)
            final_by_moment.setdefault(moment,[]).append(candidate)
        refs = [r for r in refs if Path(r['source']).is_relative_to(source_root)]
        if not refs and not final_refs:
            raise ValueError('所选递归范围内没有现场 SKU 记录')
        by_key = {};by_moment={}
        for r in refs:
            by_key.setdefault(r['fingerprint'], []).append(r)
            try:moment=capture_moment_key(json.loads(r['metadata']))
            except (ValueError,TypeError):continue
            by_moment.setdefault(moment,[]).append(r)
        unassigned_refs=[r for r in refs if not r['sku']]
        refs=[r for r in refs if r['sku']]
        items, planned, excluded_records = [], set(), []
        final_files = files_under(final_root,progress=progress,stage='scanning_final')
        if not final_files:
            raise ValueError('最终大档目录为空')
        final_sidecars=metadata_sidecars(final_files,progress=progress)
        versions={}
        for index,(source,full) in enumerate(capture_metadata_batches(self.writer,final_files,progress=progress,stage='final_metadata')):
            if source.suffix.lower() in VIDEO_EXTENSIONS and not direct:raise ValueError('首版视频请使用已确认SKU目录归档，不参与照片快门匹配')
            progress(stage='matching',done=index,total=len(final_files),path=str(source))
            versions[str(source)]=full.get('_archive_source_signature') or signature(source)
            if source.suffix.lower() not in ('.jpg','.jpeg'):
                for side in final_sidecars[str(source)]:
                    full.update({k:v for k,v in self.writer.read(side).items() if k.startswith('XMP-fk:')})
            metadata = camera_metadata(full)
            if direct:
                candidates=[r for r in refs if Path(r['source'])==source]
                key=candidates[0]['fingerprint'] if candidates else ''
                match_method='capture_metadata'
            else:
                try:key,candidates,match_method=match_capture(metadata,by_key,by_moment,exported_jpg=source.suffix.lower() in ('.jpg','.jpeg'))
                except ValueError:
                    metadata=camera_metadata(self.writer.read(source,full=True))
                    try:key,candidates,match_method=match_capture(metadata,by_key,by_moment,exported_jpg=source.suffix.lower() in ('.jpg','.jpeg'))
                    except ValueError as error:raise ValueError(str(error)+'：'+source.name) from error
                if not candidates and str(source) in final_refs:
                    # 已整理大档可独立提供归属，但仍须有可核对的快门身份。
                    key,candidates,_=match_capture(metadata,final_by_key,final_by_moment,exported_jpg=source.suffix.lower() in ('.jpg','.jpeg'))
                    # 每个最终文件保留自己的资产ID及原路径，不能借用另一格式的文件身份。
                    own=final_refs[str(source)]
                    if candidates and len({(r['sku'],r['scope'],r['rating']) for r in candidates})==1:
                        candidates=[own]
                    match_method='confirmed_final_metadata'
                own=final_refs.get(str(source))
                if own and candidates:
                    folder_sku=json.loads(own['metadata']).get('directory_sku')
                    if folder_sku and any(r['sku'] and r['sku'].casefold()!=folder_sku.casefold() for r in candidates):
                        raise ValueError('款号目录与指纹匹配SKU冲突，请先复核：'+str(source))
            business=full
            pending=[r for r in candidates if not r['sku']]
            if (not candidates or pending) and not direct and source.suffix.lower() in ('.jpg','.jpeg'):
                business=dict(full)
                for side in final_sidecars[str(source)]:
                    business.update({k:v for k,v in self.writer.read(side).items() if k.startswith('XMP-fk:')})
            # 扫描到的空SKU记录只证明快门存在，不构成已入库。与无来源记录使用同一排除规则；
            # 混有确认记录或大档已有业务身份时仍需复核，不能借排除掩盖冲突。
            unregistered=not candidates or pending and len(pending)==len(candidates)
            if unregistered and exclude_unregistered and not direct and str(source) not in final_refs and not business.get('XMP-fk:SKU') and not business.get('XMP-fk:AssetId'):
                excluded_records.append({'source':str(source),'original_source':str(source),'metadata_fingerprint':key,
                                         'status':'unregistered_excluded',
                                         'reason':'对应来源尚无确认SKU，按未选用规则排除' if pending else '无现场入库记录，按未选用规则排除',
                                         **({'reference':pending[0]['source']} if pending else {}),
                                         'raw':source.suffix.lower() not in ('.jpg','.jpeg')})
                continue
            if pending:
                raise ValueError('本次最终大档对应的来源图片尚无确认SKU，请先备案：'+pending[0]['source']+'；最终文件：'+source.name)
            if not candidates or len({(r['sku'], r['scope'], r['rating']) for r in candidates}) != 1:
                raise ValueError('最终大档元数据身份未唯一匹配：' + source.name)
            ref = candidates[0]
            if int(full.get('XMP-xmp:Rating') or 0)==2:
                ref={**ref,'rating':2}
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{3,79}', ref['sku']):
                raise ValueError('SKU 包含不允许的路径字符')
            parent_sku = ref['sku'][:parent_length]
            destination=sku_directory(target_root,ref['sku'],parent_length)/source.name
            prior_asset=full.get('XMP-fk:AssetId')
            if prior_asset and ref.get('asset_id') and uuid.UUID(str(prior_asset))!=uuid.UUID(str(ref['asset_id'])):
                raise ValueError('目标大档已属于另一资产，需复核：'+source.name)
            if destination in planned:
                raise ValueError('存在同名不同来源文件，需复核：' + source.name)
            planned.add(destination)
            raw = source.suffix.lower() not in ('.jpg', '.jpeg')
            xmp = Path(str(destination)+'.xmp') if source.suffix.lower() in VIDEO_EXTENSIONS else destination.with_suffix('.xmp') if raw else None
            if xmp in planned:
                raise ValueError('多个 RAW 共用同名 XMP，需复核')
            if xmp:
                planned.add(xmp)
            file_id=full.get('XMP-fk:FileId')
            items.append({**ref, 'id': len(items) + 1, 'source': str(source), 'reference': ref['source'],
                          'destination': str(destination), 'xmp': str(xmp) if xmp else None,
                          'parent_sku': parent_sku, 'metadata_fingerprint': key,
                          'match_method': match_method,
                          'input_signature':versions[str(source)],'input_sha256':None,
                          'asset_id':ref.get('asset_id') or full.get('XMP-fk:AssetId'),
                          'file_id':file_id,
                          'original_source':history.get(str(source),str(source)),
                          'file_identity_source':history.get(str(source),str(source)),
                          'original_reference':history.get(ref['source'],ref['source']),
                          'photo_role':'label' if ref['rating']==2 else 'product','origin':'final',
                          'raw': raw, 'video':source.suffix.lower() in VIDEO_EXTENSIONS,'metadata': metadata})
        final_keys = {r['metadata_fingerprint'] for r in items}
        supplemented=set()
        if include_label_images:
            labels=[r for r in refs if r['rating']==2 and r['fingerprint'] not in final_keys]
            for index,ref in enumerate(labels):
                if ref['fingerprint'] in supplemented:
                    continue
                progress(stage='label_matching',done=index,total=len(labels),path=ref['source'])
                label_source=Path(ref['source'])
                if label_source.suffix.lower() not in ('.jpg','.jpeg') or not label_source.is_file():
                    raise ValueError('缺少可补充的二星标签JPG：'+str(label_source))
                if len({(r['sku'],r['scope'],r['rating']) for r in by_key[ref['fingerprint']]})!=1:
                    raise ValueError('二星标签记录SKU存在冲突，不能猜配')
                label_metadata=self.writer.read(label_source,full=True)
                if capture_key(camera_metadata(label_metadata))!=ref['fingerprint']:
                    raise ValueError('二星标签原文件身份已变化')
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{3,79}',ref['sku']):
                    raise ValueError('标签SKU包含不允许的路径字符')
                parent_sku=ref['sku'][:parent_length]
                destination=(target_root if target_root.name.upper()==parent_sku.upper() else target_root/parent_sku)/ref['sku']/label_source.name
                if destination in planned:
                    raise ValueError('标签图与其他归档文件重名，需复核')
                planned.add(destination);supplemented.add(ref['fingerprint'])
                items.append({**ref,'id':len(items)+1,'source':str(label_source),'reference':str(label_source),
                              'destination':str(destination),'xmp':None,'parent_sku':parent_sku,
                              'asset_id':ref.get('asset_id'),
                              'file_id':label_metadata.get('XMP-fk:FileId'),
                              'metadata_fingerprint':ref['fingerprint'],'match_method':'onsite_label_metadata',
                              'original_source':history.get(str(label_source),str(label_source)),
                              'file_identity_source':history.get(str(label_source),str(label_source)),
                              'original_reference':history.get(str(label_source),str(label_source)),
                              'input_signature':signature(label_source),'input_sha256':None if fast else sha(label_source),'raw':False,'photo_role':'label','origin':'onsite_label'})
        deleted = [{**r, 'reference': r['source'], 'parent_sku': r['sku'][:parent_length],
                    'status': 'post_deleted_skipped', 'reason': '最终大档已无此快门记录，自动跳过'}
                   for r in refs if r['fingerprint'] not in final_keys|supplemented]
        excluded_keys={r['metadata_fingerprint'] for r in excluded_records}
        unrelated_unassigned=[{'source':r['source'],'metadata_fingerprint':r['fingerprint'],
                              'status':'unassigned_reference_not_selected','reason':'无确认SKU，且本次最终大档无对应快门；来源保持原样'} for r in unassigned_refs]
        unrelated_unassigned=[r for r in unrelated_unassigned if r['metadata_fingerprint'] not in excluded_keys]
        if target_root.name.upper() in {r['parent_sku'].upper() for r in items} and len({r['parent_sku'] for r in items})>1:
            raise ValueError('多母SKU任务请选择共同的上级归档根目录')
        if target_root.name.casefold() in {r['sku'].casefold() for r in items} and len({r['sku'].casefold() for r in items})>1:
            raise ValueError('多子SKU任务请选择共同的上级归档根目录')
        self.preflight_target(target_root, items)
        if fast:
            progress(stage='manifest_listing',done=0,total=len(final_files),path=str(final_root))
            manifest={str(p):versions[str(p)] for p in final_files}
        else:manifest=manifest_hashes(final_files,progress,expected_versions=versions)
        references=[]
        if preprocess:
            selected={r['reference'] for r in items}
            selected_refs={r['source']:r for r in [*refs,*final_refs.values()] if r['source'] in selected}
            for index,r in enumerate(selected_refs.values()):
                progress(stage='reference_listing' if fast else 'reference_hashing',done=index,total=len(selected_refs),path=r['source'])
                expected=json.loads(r['metadata']).get('source_signature')
                if not fast and expected and signature(r['source'])!=expected:raise ValueError('来源在匹配后改变，请重新读取：'+r['source'])
                references.append({**r,'input_signature':expected or signature(r['source']),
                                   'input_sha256':None if fast else manifest[r['source']] if r['source'] in manifest else sha(r['source'])})
        sides={}
        paths=[*final_files,*[Path(r['source']) for r in references]]
        found=metadata_sidecars(paths,progress=progress)
        side_files=list(dict.fromkeys(side for group in found.values() for side in group))
        sides=manifest_hashes(side_files,progress,'sidecar_hashing')
        for index,row in enumerate(items):
            if fast:continue
            if row.get('origin')=='final':row['input_sha256']=manifest[row['source']]
            destination=Path(row['destination'])
            if destination.exists():
                progress(stage='existing_targets',done=index,total=len(items),path=str(destination))
                if not row.get('file_id'):
                    target_meta=self.writer.read_asset(destination)
                    expected_asset=str(uuid.UUID(str(row['asset_id']))) if row.get('asset_id') else str(uuid.uuid5(uuid.NAMESPACE_URL,row['metadata_fingerprint']))
                    target_asset=target_meta.get('XMP-fk:AssetId')
                    if target_asset and str(uuid.UUID(str(target_asset)))!=expected_asset:raise ValueError('归档目标属于其他资产，不能覆盖：'+str(destination))
                    equal=sha(destination)==row['input_sha256'] if row['raw'] else image_pixels(destination)==image_pixels(row['source'])
                    if equal:row['file_id']=target_meta.get('XMP-fk:FileId')
                self.verify_item(row)
        plan = {'source': str(final_root), 'reference_root': str(source_root), 'target': str(target_root),'parent_length':parent_length,'direct':direct,'fast':fast,
                'preprocess':preprocess,'reference_records':references,'reference_scope':'matched_final_only',
                'basis': 'final_directory_manifest', 'expected': len(items),
                'final_expected':len(final_files)-len(excluded_records),'final_inventory_count':len(final_files),
                'exclude_unregistered':exclude_unregistered,'excluded_records':excluded_records,'excluded_count':len(excluded_records),
                'supplemental_label_count':len(supplemented),
                'label_file_count':sum(r['photo_role']=='label' for r in items),
                'expected_xmp': sum(r['raw'] for r in items), 'items': items,
                'deleted_reference_count': len(deleted), 'skipped_reference_records': deleted,
                'unassigned_reference_count':len(unrelated_unassigned),'unassigned_reference_records':unrelated_unassigned,
                'sidecar_manifest':sides,'manifest':manifest}
        capture=getattr(self.writer,'capture_service',None)
        if capture:
            from archive_review_scope import validate_archive_review_scope
            validate_archive_review_scope(capture, plan)
        return plan

    def execute(self, plan, *, progress=None, on_plan_changed=None, preprocess_context=None, _portable_internal=False):
        capture=getattr(self.writer,'capture_service',None)
        if capture and not _portable_internal:
            with capture.archive_execution(plan) as destination:
                result=self.execute(plan,progress=progress,on_plan_changed=on_plan_changed,preprocess_context=preprocess_context,_portable_internal=True)
                capture.finalize_archive(result,destination)
                return result
        if plan.get('fast'):
            from sku_archive_fast import execute_fast
            return execute_fast(self,plan,progress=progress,on_plan_changed=on_plan_changed,preprocess_context=preprocess_context)
        from contextlib import nullcontext
        progress=progress or (lambda **_:None)
        self.preflight_target(Path(plan['target']), plan['items'])
        if manifest_hashes(files_under(plan['source'],progress=progress,stage='scanning_verify'),progress,'verifying_source')!=plan['manifest']:
            raise ValueError('最终大档在预览后改变，未开始整理或归档')
        for side,digest in plan.get('sidecar_manifest',{}).items():
            if not Path(side).is_file() or sha(side)!=digest:
                raise ValueError('侧车在预览后改变，未开始整理或归档')
        from sku_archive_preprocess import arrange_sources
        progress(stage='preprocessing' if plan.get('preprocess') else 'copying',done=0,total=len(plan['items']))
        with preprocess_context(plan) if preprocess_context and plan.get('preprocess') else nullcontext():
            plan=arrange_sources(plan,self.database,progress=progress)
            if on_plan_changed:on_plan_changed(plan)
        target = Path(plan['target'])
        target.mkdir(parents=True, exist_ok=True)
        thumb_dir = target / '.核查缩略图'
        thumb_dir.mkdir(exist_ok=True)
        result = {**plan, 'demo': False, 'real_validation': True, 'recursive': True, 'items': []}
        from folder_archive_commit import prepare as prepare_commit,recover as recover_commits,store as store_commit
        commit_dir=(target if self.database.is_relative_to(Path(plan['reference_root'])) else self.database.parent)/'.归档文件提交日志'
        receipt_path=target/('.归档回执-'+str(uuid.uuid5(uuid.NAMESPACE_URL,plan['source']+'|'+plan['reference_root'])))
        receipts=json.loads(receipt_path.read_text(encoding='utf8')) if receipt_path.exists() else {}
        receipts.update(recover_commits(commit_dir,target))
        for index,expected in enumerate(plan['items']):
            progress(stage='copying',done=index,total=len(plan['items']),path=expected['source'])
            row = dict(expected)
            source, destination = Path(row['source']), Path(row['destination'])
            from sku_archive_preprocess import safe_path
            safe_path(destination, target)
            if sha(source) != row['input_sha256']:
                raise ValueError('归档计划之后源文件发生变化')
            destination.parent.mkdir(parents=True, exist_ok=True)
            fields = metadata_fields(row, row['metadata_fingerprint'])
            xmp = Path(row['xmp']) if row['xmp'] else None
            existing = destination.exists()
            if existing:
                self.verify_item(row)
                receipt=receipts.get(str(destination))
                row.update(status='identical_verified_skip', archived_at=receipt['archived_at'] if receipt and receipt['sha256']==sha(destination) else None)
            else:
                stage = destination.parent / ('.archive-' + uuid.uuid4().hex)
                stage.mkdir()
                staging_file = stage / destination.name
                try:
                    shutil.copy2(source, staging_file)
                    if sha(staging_file) != row['input_sha256']:
                        raise ValueError('临时复制哈希校验失败')
                    # 先生成和验证 XMP；RAW 永不写入内嵌字段。
                    if xmp:
                        existing_sides = sidecar_paths(source)
                        if len(existing_sides) > 1:
                            raise ValueError('来源包含多个 XMP，需复核')
                        if existing_sides:
                            shutil.copy2(existing_sides[0], Path(str(staging_file)+'.xmp') if row.get('video') else staging_file.with_suffix('.xmp'))
                    content, metadata_path, journal = self.writer.write(staging_file, fields, sha(staging_file), mode='sidecar',**({'_target':Path(str(staging_file)+'.xmp')} if row.get('video') else {}))
                    metadata_path.write_bytes(content)
                    if sha(source) != row['input_sha256']:
                        raise ValueError('准备期间源文件改变')
                    staged_row = {**row, 'destination': str(staging_file), 'xmp': str(metadata_path) if xmp else None}
                    self.verify_item(staged_row)
                    commit_path,commit=prepare_commit(commit_dir,row,staging_file,metadata_path)
                    if xmp:
                        if xmp.exists():
                            raise ValueError('目标 XMP 已存在，不覆盖')
                    # Windows rename 不覆盖用户现有文件。
                    os.rename(staging_file, destination)
                    if xmp:os.rename(metadata_path,xmp)
                    commit['state']='committed';store_commit(commit_path,commit)
                    row.update(status='verified', archived_at=commit['archived_at'])
                finally:
                    # 仅删除本次唯一 stage 下产生的文件，不递归清理其他目录。
                    for p in stage.iterdir():
                        if p.is_file():
                            p.unlink()
                    stage.rmdir()
            row.update(self.verify_item(row))
            receipts[str(destination)]={'sha256':row['output_sha256'],'archived_at':row['archived_at']}
            temporary=receipt_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(receipts,ensure_ascii=False),encoding='utf8');os.replace(temporary,receipt_path)
            # 整理后扫描顺序可能改变，禁止用列表序号命名而覆盖别张图的缩略图。
            thumb = thumb_dir / f"{fields['FileId']}.jpg"
            width, height = make_thumbnail(self.writer, destination, thumb)
            row.update(thumbnail_path=str(thumb), thumbnail_width=width, thumbnail_height=height)
            result['items'].append(row)
        for index, row in enumerate(result['items']):
            progress(stage='verifying',done=index,total=len(plan['items']),path=row['destination'])
            actual = self.verify_item(row)
            if any(actual[key] != row[key] for key in ('output_sha256', 'xmp_sha256')):
                raise ValueError('最终复核时目标文件已改变，不能报告完成')
        if manifest_hashes(files_under(plan['source'],progress=progress,stage='scanning_verify'),progress,'verifying_source') != plan['manifest']:
            raise ValueError('最终目录的文件清单或内容在归档期间改变，不能报告完成')
        result.update(expected_images=plan['expected'], expected_files=plan['expected']+plan['expected_xmp'],
                      verified=len(result['items']), excluded=plan.get('excluded_count',0), xmp=plan['expected_xmp'], result='verified',
                      completed_at=datetime.now(timezone.utc).isoformat())
        return result

    @staticmethod
    def preflight_target(target, items):
        """在整理来源前检查目标目录、链接与可用容量；执行时再次检查。"""
        from sku_archive_preprocess import FastPathChecks
        target = Path(target)
        nearest = target
        while not nearest.exists() and nearest != nearest.parent:
            nearest = nearest.parent
        if not nearest.is_dir() or not os.access(nearest, os.W_OK):
            raise ValueError('归档目标不可写或上级路径不是目录')
        required = 0;checks=FastPathChecks();parents_checked=set()
        for row in items:
            destination,info = checks.inspect(Path(row['destination']),target)
            for parent in destination.parents:
                if parent in parents_checked:break
                if parent.exists() and not parent.is_dir():
                    raise ValueError('归档目标层级被同名文件占用：' + str(parent))
                parents_checked.add(parent)
                if parent == target:
                    break
            if info is None:
                required += (row['input_signature'][0] if row.get('input_signature') else Path(row['source']).stat().st_size) + 1024 * 1024
        if shutil.disk_usage(nearest).free < required:
            raise ValueError('归档目标剩余空间不足，未整理来源')

    def verify_item(self, row):
        source, destination = Path(row['source']), Path(row['destination'])
        if not destination.is_file():
            raise ValueError('目标图片缺失')
        if sha(source) != row['input_sha256']:
            raise ValueError('源文件改变')
        if row['raw']:
            if sha(destination) != row['input_sha256']:
                raise ValueError('RAW 字节校验失败')
            metadata_path = Path(row['xmp'])
            if not metadata_path.is_file():
                raise ValueError('配套 XMP 缺失')
        else:
            if image_pixels(source) != image_pixels(destination):
                raise ValueError('JPG 像素校验失败')
            # RAW+JPG 同名时共享 RAW 的 .xmp 合法，禁止为 JPG 额外生成 .jpg.xmp。
            shared_raw = any(destination.with_suffix(ext).is_file() for ext in ('.CR3','.cr3','.CR2','.cr2','.NEF','.nef','.ARW','.arw','.DNG','.dng','.RAF','.raf'))
            if Path(str(destination)+'.xmp').exists() or (destination.with_suffix('.xmp').exists() and not shared_raw):
                raise ValueError('JPG 不应产生 XMP')
            metadata_path = destination
        current = self.writer.read(metadata_path, full=True)
        for tag, value in [('XMP-fk:SKU', row['sku']), ('XMP-fk:ShootType', row['scope']), ('XMP-xmp:Rating', row['rating'])]:
            if str(current.get(tag)) != str(value):
                raise ValueError('元数据读回不符：' + tag)
        expected_fields = metadata_fields(row, row['metadata_fingerprint'])
        for name in ('AssetId','FileId'):
            if str(current.get('XMP-fk:'+name)) != expected_fields[name]:
                raise ValueError('归档资产身份读回不一致：'+name)
        actual_key=row['metadata_fingerprint']
        if not actual_key.startswith('direct:'):
            actual_metadata=camera_metadata(self.writer.read(destination,full=True) if row['raw'] else current)
            if actual_key.startswith('capture-moment-v1:'):
                actual_key=capture_moment_key(actual_metadata)
            elif not row['raw'] and row.get('match_method')=='exported_jpg_metadata' and not actual_metadata.get('number'):
                expected_values=json.loads(actual_key.partition(':')[2])
                if _capture_values(actual_metadata,require_number=False)!=expected_values[:-1]:
                    raise ValueError('复制后相机身份改变')
            else:actual_key=capture_key(actual_metadata)
        if actual_key != row['metadata_fingerprint']:
            raise ValueError('复制后相机身份改变')
        return {'output_sha256': sha(destination), 'xmp_sha256': sha(metadata_path) if row['raw'] else None}
