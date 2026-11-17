"""归档文件/XMP 提交意图与可幂等重放的中断恢复。"""
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

from metadata_writer import sha


def store(path,value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf8') as stream:
        json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def prepare(directory,row,staging_file,metadata_path,*,verified_sha=None,proof=None):
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(uuid.uuid4().hex+'.json')
    entry={'destination':row['destination'],'sha256':verified_sha or sha(staging_file),'archived_at':datetime.now(timezone.utc).isoformat(),
           'xmp':row.get('xmp'),'state':'prepared'}
    if row.get('xmp'):
        entry.update(xmp_sha256=sha(metadata_path),xmp_content=base64.b64encode(metadata_path.read_bytes()).decode('ascii'))
    if proof:entry.update(proof=proof)
    store(path,entry)
    return path,entry


def recover(directory,target,*,fast=False):
    receipts={};target=Path(target).resolve()
    if not directory.is_dir():return receipts
    for path in directory.glob('*.json'):
        entry=json.loads(path.read_text(encoding='utf8'))
        if entry['state']=='not_committed':continue
        destination=Path(entry['destination'])
        if not destination.resolve().is_relative_to(target):raise ValueError('提交日志目标越界，未恢复')
        if not destination.exists():
            if entry['state']=='committed':raise ValueError('已提交的归档图片被删除，请复核目标')
            entry['state']='not_committed';store(path,entry);continue
        from folder_archive_metadata import signature
        unchanged=fast and entry['state']=='committed' and entry.get('proof',{}).get('output_signature')==signature(destination)
        if not unchanged and sha(destination)!=entry['sha256']:raise ValueError('已提交的目标内容变化，不能自动覆盖恢复')
        if entry.get('xmp'):
            xmp=Path(entry['xmp'])
            from sku_folder_archive import VIDEO_EXTENSIONS
            expected=Path(str(destination)+'.xmp') if destination.suffix.lower() in VIDEO_EXTENSIONS else destination.with_suffix('.xmp')
            if xmp!=expected:raise ValueError('提交日志XMP路径不匹配')
            if not xmp.exists():
                # 只创建本任务已验证的配套XMP，不覆盖外部修改。
                content=base64.b64decode(entry['xmp_content'],validate=True)
                with xmp.open('xb') as stream:stream.write(content)
            if sha(xmp)!=entry['xmp_sha256']:raise ValueError('配套XMP被外部修改，不能自动覆盖恢复')
        entry['state']='committed';store(path,entry)
        receipts[str(destination)]={'sha256':entry['sha256'],'archived_at':entry['archived_at'],**entry.get('proof',{})}
    return receipts
