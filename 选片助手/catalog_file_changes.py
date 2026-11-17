"""归档名称/属性变化同步；不解码照片，不在热搜索中探测文件系统。"""
from collections import deque
from pathlib import Path, PurePosixPath
import hashlib
import os
import stat
import sqlite3
import time

from asset_catalog import MEDIA_EXTENSIONS, _id

BATCH=128
IGNORED={'#recycle','$recycle.bin','system volume information'}


def _relative(value):
    value=str(value).replace('\\','/')
    if value.startswith('/'):raise ValueError('文件变化路径无效')
    value=value.rstrip('/')
    parts=PurePosixPath(value)
    if parts.is_absolute() or any(part in ('.','..') or ':' in part for part in parts.parts):raise ValueError('文件变化路径无效')
    return '' if value in ('','.') else value


def _owned(library,root,path):
    value=str(path).replace('/','\\').rstrip('\\').casefold()
    with library.lock:roots=[(entry['id'],str(entry['path']).replace('/','\\').rstrip('\\').casefold()) for entry in library.config['roots'] if not entry.get('removed')]
    owners=[(len(base),key) for key,base in roots if value==base or value.startswith(base+'\\')]
    return not owners or max(owners)[1]==root


def _excluded(relative,excluded):
    value=relative.casefold()
    return any(value==folder.casefold() or value.startswith(folder.casefold()+'/') for folder in excluded)


def _internal(library,path):
    # scandir已经排除链接；这里只做词法边界检查，不能为每张NAS照片调用resolve。
    from preview_cache import _LOCATIONS
    value=str(path).replace('/','\\').rstrip('\\').casefold()
    protected=[library.data,library.storage.path,*_LOCATIONS.values()]
    if library.storage.previous:protected.append(Path(library.storage.previous))
    return any(value==str(base).replace('/','\\').rstrip('\\').casefold() or value.startswith(str(base).replace('/','\\').rstrip('\\').casefold()+'\\') for base in protected)


def _alive(library,root):
    if library.stopping.is_set() or library.storage.pausing.is_set():return False
    try:library._root(root)
    except ValueError:return False
    return True


def _offline_ids(library,catalog,root,ids,*,before=None):
    changed=0
    for start in range(0,len(ids),BATCH):
        if not _alive(library,root):break
        with catalog.connect() as c:
            condition=' AND last_seen<=?' if before is not None else ''
            c.executemany('UPDATE items SET online=0 WHERE id=? AND root=? AND online=1'+condition,[(item,root,*([before] if before is not None else [])) for item in ids[start:start+BATCH]])
            changed+=c.total_changes
    return changed


def missing(library,root,relative):
    """仅在根可访问时标记已消失路径；以索引有界分批，保留SKU和历史。"""
    relative=_relative(relative);entry=library._root(root);base=Path(entry['path'])
    catalog=library.catalog(root);full=base.joinpath(*relative.split('/')) if relative else base
    checked=time.time()
    try:full.stat()
    except FileNotFoundError:pass
    else:return
    if not base.is_dir():raise OSError('归档位置暂不可访问，保留已有记录')
    _offline_ids(library,catalog,root,[_id(str(full).casefold())],before=checked)
    ranges=[('folder COLLATE NOCASE=?',[relative or '/'])]
    if relative:ranges.append(('folder COLLATE NOCASE>=? AND folder COLLATE NOCASE<?',[relative+'/',relative+'0']))
    for condition,args in ranges:
        while _alive(library,root):
            with catalog.connect() as c:
                ids=[row[0] for row in c.execute('SELECT id FROM items INDEXED BY catalog_folder_candidates WHERE root=? AND online=1 AND '+condition+' AND last_seen<=? LIMIT ?',[root,*args,checked,BATCH])]
            if not ids:break
            _offline_ids(library,catalog,root,ids,before=checked)


def _rows(catalog,root,folder):
    rows={}
    for online in (0,1):
        with catalog.connect() as c:
            for row in c.execute('SELECT id,path,fingerprint,online,last_seen FROM items INDEXED BY catalog_folder_candidates WHERE root=? AND online=? AND folder COLLATE NOCASE=?',(root,online,folder or '/')):
                rows[str(row['path']).casefold()]=dict(row)
    return rows


def _checkpoint(library,root,base,pending,error=''):
    with library.lock:
        entry=next((entry for entry in library.config['roots'] if entry['id']==root and not entry.get('removed')),None)
        if entry is None:return
        entry['monitor_error']=str(error)[:240]
        entry['monitor_resume']={'path':str(base),'folders':list(pending)} if pending else None
        if not pending and not error:entry['monitor_checked_at']=time.time()
        library.save()


def reconcile(library,root,folders=None,*,resume=True):
    """逐目录核对名称和属性；通知已先订阅，过程中发生的变化稍后再重放。"""
    entry=library._root(root);base=Path(entry['path']);catalog=library.catalog(root);track=folders is None
    if not base.is_dir():raise OSError('归档位置暂不可访问，保留已有记录')
    with catalog.connect() as c:excluded=[row[0] for row in c.execute('SELECT folder FROM exclusions WHERE root=?',(root,))]
    known=set();last=''
    if folders is None:
        # 与目录候选索引的排序/比较一致，禁止GROUP BY诱使遍历主大表。
        while _alive(library,root):
            with catalog.connect() as c:
                batch=[row[0] for row in c.execute('SELECT DISTINCT folder COLLATE NOCASE AS folder FROM items INDEXED BY catalog_folder_candidates WHERE root=? AND online=1 AND folder COLLATE NOCASE>? ORDER BY folder COLLATE NOCASE LIMIT 128',(root,last))]
            if not batch:break
            for folder in batch:
                value='' if folder=='/' else folder
                known.add(value)
                known.update('' if str(parent)=='.' else parent.as_posix() for parent in PurePosixPath(value).parents)
            last=batch[-1]
        known.add('')
        checkpoint=entry.get('monitor_resume') or {}
        prior=checkpoint.get('folders') if resume and checkpoint.get('path')==str(base) else None
        pending=deque(dict.fromkeys(['',*(prior if isinstance(prior,list) else sorted(known,key=lambda value:(value.count('/'),value)))]))
        scheduled={value.casefold() for value in known}|{value.casefold() for value in pending}
    else:
        pending=deque(_relative(folder) for folder in folders);scheduled={value.casefold() for value in pending}
    visited=set();last_saved=time.monotonic();checked=0;active_folder=None;failed=[];first_error=None
    try:
        while pending and _alive(library,root):
            folder=pending.popleft();path=base.joinpath(*folder.split('/')) if folder else base
            if folder.casefold() in visited or _excluded(folder,excluded) or not _owned(library,root,path):continue
            visited.add(folder.casefold())
            if _internal(library,path):continue
            active_folder=folder
            checked_at=time.time()
            records=[];seen=set();children=[]
            try:
                path=path.resolve(strict=True)
                path.relative_to(base.resolve())
                with os.scandir(path) as entries:
                    for child in entries:
                        if not _alive(library,root):break
                        if child.name.startswith('.') or child.name.casefold() in IGNORED or child.is_symlink() or child.is_junction():continue
                        child_path=Path(child.path);relative=child_path.relative_to(base).as_posix()
                        if _excluded(relative,excluded) or not _owned(library,root,child_path) or _internal(library,child_path):continue
                        if child.is_dir(follow_symlinks=False):children.append(relative);continue
                        if child_path.suffix.lower() not in MEDIA_EXTENSIONS:continue
                        seen.add(str(child_path).casefold())
                        try:info=child.stat(follow_symlinks=False)
                        except OSError:continue
                        if stat.S_ISREG(info.st_mode):records.append({'path':str(child_path),'size':info.st_size,'modified_ns':info.st_mtime_ns})
            except FileNotFoundError:
                missing(library,root,folder);active_folder=None;continue
            except OSError as error:
                if not base.is_dir():pending.appendleft(folder);raise
                failed.append(folder);first_error=first_error or error;active_folder=None;continue
            if not _alive(library,root):pending.appendleft(folder);break
            current=_rows(catalog,root,folder);changed=[]
            for record in records:
                fingerprint=hashlib.sha256(f"{record['path'].casefold()}:{record['size']}:{record['modified_ns']}".encode()).hexdigest()
                previous=current.get(record['path'].casefold())
                if not previous or not previous['online'] or previous['fingerprint']!=fingerprint or previous['path']!=record['path']:changed.append(record)
            for start in range(0,len(changed),BATCH):
                if not _alive(library,root):pending.appendleft(folder);break
                batch=changed[start:start+BATCH];library._index_paths(root,[record['path'] for record in batch],batch)
            _offline_ids(library,catalog,root,[row['id'] for key,row in current.items() if row['online'] and key not in seen],before=checked_at)
            for child in children:
                if child.casefold() not in scheduled:pending.append(child);scheduled.add(child.casefold())
            checked+=1
            active_folder=None
            if track and time.monotonic()-last_saved>3:
                _checkpoint(library,root,base,[*pending,*failed]);last_saved=time.monotonic()
        pending.extend(failed)
        if first_error is not None and _alive(library,root):raise first_error
        if track:_checkpoint(library,root,base,pending)
        return {'folders':checked,'complete':not pending}
    except (OSError,ValueError,sqlite3.Error) as error:
        pending.extend(folder for folder in failed if folder not in pending)
        if active_folder is not None and active_folder not in pending:pending.appendleft(active_folder)
        if track:_checkpoint(library,root,base,pending,error)
        raise


def apply_changes(library,root,changes):
    entry=library._root(root);base=Path(entry['path'])
    with library.catalog(root).connect() as c:excluded=[row[0] for row in c.execute('SELECT folder FROM exclusions WHERE root=?',(root,))]
    for value in sorted(changes,key=len):
        if not _alive(library,root):return
        relative=_relative(value);path=base.joinpath(*relative.split('/'))
        if any(part.startswith('.') or part.casefold() in IGNORED for part in PurePosixPath(relative).parts) or _excluded(relative,excluded) or not _owned(library,root,path) or _internal(library,path):continue
        if path.suffix.lower()=='.xmp':library.shared_catalog.metadata.changed_path(path);continue
        try:
            if path.is_symlink() or path.is_junction():continue
            path=path.resolve(strict=True)
            path.relative_to(base.resolve())
            relative=path.relative_to(base.resolve()).as_posix()
            info=path.stat()
        except FileNotFoundError:missing(library,root,relative);continue
        if stat.S_ISDIR(info.st_mode):reconcile(library,root,[relative],resume=False)
        elif stat.S_ISREG(info.st_mode) and path.suffix.lower() in MEDIA_EXTENSIONS:
            library._index_paths(root,[str(path)],[{'path':str(path),'size':info.st_size,'modified_ns':info.st_mtime_ns}])
