"""归档前原地整理：预检全部路径后移动，持久日志与数据库路径一致更新。"""
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from metadata_writer import sha
from rating_xmp import sidecar_paths
from folder_archive_metadata import signature
from sku_archive_paths import arranged_source

def entry_matches(path,entry):
    return sha(path)==entry['sha256'] if entry.get('sha256') else signature(path)==entry['signature']


def save_journal(path, data):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def safe_path(path, root):
    path, root = Path(path).absolute(), Path(root).resolve()
    if not path.is_relative_to(root):
        raise ValueError('整理路径越出所选目录')
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if part.is_symlink() or part.is_junction():
            raise ValueError('整理路径经过链接，已阻止移动')
    return path


class FastPathChecks:
    """一次操作内共享已检查的父目录；叶文件使用单次lstat，不跟随链接。"""
    def __init__(self):self.roots={};self.directories=set()

    def inspect(self,path,root):
        root=Path(root)
        if root not in self.roots:self.roots[root]=root.resolve()
        root=self.roots[root];path=Path(path).absolute()
        if not path.is_relative_to(root):raise ValueError('整理路径越出所选目录')
        leaf=None
        for part in (path,*path.parents):
            if part==root.parent:break
            if part in self.directories:continue
            try:info=part.lstat()
            except FileNotFoundError:continue
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400):
                raise ValueError('整理路径经过链接，已阻止移动')
            if part==path:leaf=info
            if stat.S_ISDIR(info.st_mode):self.directories.add(part)
        return path,leaf


def move_no_replace(source, destination):
    # Windows rename 不覆盖；其他平台用原子硬链接后删除源，避免替换同名用户文件。
    if os.name == 'nt':
        os.rename(source, destination)
    else:
        os.link(source, destination)
        os.unlink(source)


def recover_source_moves(database):
    """启动时处理移动完成但数据库未提交/日志未收尾的中断窗口。"""
    for path in Path(database).parent.glob('.归档前处理-*.json'):
        journal=json.loads(path.read_text(encoding='utf8'))
        if journal['state'] in ('completed','rolled_back'):
            continue
        with closing(sqlite3.connect(database)) as c, c:
            actual={str(id_):source for id_,source in c.execute('SELECT id,source FROM captures')}
        updates=journal['reference_updates']
        committed=all(actual.get(str(r['id']))==r['after'] for r in updates)
        uncommitted=all(actual.get(str(r['id']))==r['before'] for r in updates)
        if not committed and not uncommitted:
            raise ValueError('整理中断且数据库路径混杂，保留日志等待恢复')
        failures=[]
        for entry in reversed(journal['moves']):
            before,after=Path(entry['before']),Path(entry['after'])
            root=Path(journal['small_root']) if before.is_relative_to(Path(journal['small_root'])) else Path(journal['final_root'])
            safe_path(before,root);safe_path(after,root)
            try:
                if before==after:
                    continue
                if committed:
                    if before.exists() or not after.is_file() or not entry_matches(after,entry):
                        raise ValueError('已提交的整理文件被外部修改')
                elif before.is_file() and not after.exists() and entry_matches(before,entry):
                    continue
                elif not before.exists() and after.is_file() and entry_matches(after,entry):
                    move_no_replace(after,before)
                else:
                    raise ValueError('整理文件有冲突，不自动覆盖恢复')
            except Exception as error:
                failures.append(str(error))
        journal.update(state='needs_recovery' if failures else 'completed' if committed else 'rolled_back',recovery_errors=failures)
        save_journal(path,journal)
        if failures:
            raise ValueError('归档前处理恢复未完成：'+str(path))


def preprocessing_complete(plan,database):
    """只复用本任务已提交且与SKU库一致的来源整理检查点。"""
    if not plan.get('preprocess') or not plan.get('preprocess_journal'):return False
    path=Path(plan['preprocess_journal']);database=Path(database)
    if path.resolve().parent!=database.resolve().parent:raise ValueError('来源整理检查点不属于当前任务')
    journal=json.loads(path.read_text(encoding='utf8'))
    if journal['state']!='completed':return False
    if Path(journal['small_root'])!=Path(plan['reference_root']) or Path(journal['final_root'])!=Path(plan['source']):
        raise ValueError('来源整理检查点范围不一致')
    with closing(sqlite3.connect(database)) as c:
        actual={str(r[0]):tuple(r[1:]) for r in c.execute('SELECT id,source,sku,fingerprint FROM captures')}
    expected={str(r['id']):(r['source'],r['sku'],r['fingerprint']) for r in plan['reference_records']}
    if set(expected)!={str(r['id']) for r in journal['reference_updates']} or any(actual.get(k)!=v for k,v in expected.items()):
        raise ValueError('来源整理检查点与SKU库不一致，未重复移动')
    return True


def arrange_sources(plan, database, *, progress=None):
    progress=progress or (lambda **_:None)
    if plan.get('preprocess') is not True:
        return plan
    if preprocessing_complete(plan,database):
        count=len(plan.get('preprocess_moves',[]))
        progress(stage='preprocessing',done=count,total=count,path='',indeterminate=False)
        return deepcopy(plan)
    plan = deepcopy(plan)
    small_root, final_root = Path(plan['reference_root']), Path(plan['source'])
    if small_root != final_root and (small_root in final_root.parents or final_root in small_root.parents):
        raise ValueError('大小档目录不能互相包含，无法安全整理')
    moves, destinations = {}, {}
    fast=plan.get('fast',False);checks=FastPathChecks()
    if fast:
        from folder_archive_metadata import metadata_sidecars
        sidecars=metadata_sidecars([Path(r['source']) for r in plan['reference_records']+plan['items']],progress=progress)

    def sides(path):return sidecars.get(str(path),[]) if fast else sidecar_paths(Path(path))

    def add(source, sku, root, expected=None,expected_signature=None):
        progress(stage='preprocess_checking',done=len(moves),total=0,path=str(source),indeterminate=True)
        if not sku or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{3,79}',sku):
            raise ValueError('整理SKU无效')
        if fast:source,source_info=checks.inspect(source,root)
        else:source=safe_path(source,root)
        # 已整理路径与母SKU均复用，不逐次追加父目录。
        length=plan.get('parent_length',4)
        arranged=arranged_source(source,root,sku,length)
        if fast:destination,destination_info=checks.inspect(arranged,root)
        else:destination=safe_path(arranged,root)
        if source in moves:
            if moves[source]['after'] != str(destination):
                raise ValueError('同一来源文件存在不同SKU归属')
            return
        source_valid=source_info is not None and stat.S_ISREG(source_info.st_mode) if fast else source.is_file()
        if not source_valid:
            raise ValueError('待整理来源缺失：'+str(source))
        version=[source_info.st_size,source_info.st_mtime_ns] if fast else signature(source)
        if expected_signature and version!=expected_signature:raise ValueError('来源在预览后改变，未整理')
        digest = None if plan.get('fast') and source.suffix.lower()!='.xmp' else sha(source)
        if expected and digest != expected:
            raise ValueError('来源在预览后改变，未整理')
        if source != destination:
            if (destination_info is not None if fast else destination.exists()) or destination in destinations:
                raise ValueError('整理后存在同名文件，未移动任何来源：'+str(destination))
            destinations[destination] = source
        moves[source] = {'before':str(source),'after':str(destination),'sha256':digest,'signature':version}

    for record in plan['reference_records']:
        record_root=small_root if Path(record['source']).is_relative_to(small_root) else final_root
        add(record['source'],record['sku'],record_root,record.get('input_sha256'),record.get('input_signature'))
        for side in sides(record['source']):
            if fast and str(side) not in plan.get('sidecar_manifest',{}):raise ValueError('来源XMP在预览后新增，未整理')
            add(side,record['sku'],record_root,plan.get('sidecar_manifest',{}).get(str(side)))
    for item in plan['items']:
        item_root=small_root if item.get('origin')=='onsite_label' else final_root
        add(item['source'],item['sku'],item_root,item.get('input_sha256'),item.get('input_signature'))
        for side in sides(item['source']):
            if fast and str(side) not in plan.get('sidecar_manifest',{}):raise ValueError('来源XMP在预览后新增，未整理')
            add(side,item['sku'],item_root,plan.get('sidecar_manifest',{}).get(str(side)))

    journal_path = Path(database).parent / ('.归档前处理-'+uuid.uuid4().hex+'.json')
    journal = {'state':'prepared','small_root':str(small_root),'final_root':str(final_root),
               'moves':list(moves.values()),'completed':[],
               'reference_updates':[{'id':r['id'],'before':r['source'],'after':moves[Path(r['source'])]['after']} for r in plan['reference_records']]}
    save_journal(journal_path,journal)
    moved = []
    c = sqlite3.connect(database)
    try:
        c.execute('BEGIN IMMEDIATE')
        for record in plan['reference_records']:
            stored = c.execute('SELECT source,sku,fingerprint FROM captures WHERE id=?',(record['id'],)).fetchone()
            if not stored or tuple(stored) != (record['source'],record['sku'],record['fingerprint']):
                raise ValueError('现场数据库在预览后改变，未整理')
        move_checks=FastPathChecks();created=set()
        for index,(source, entry) in enumerate(moves.items()):
            progress(stage='preprocessing',done=index,total=len(moves),path=str(source),indeterminate=False)
            destination = Path(entry['after'])
            root=small_root if source.is_relative_to(small_root) else final_root
            if fast:
                _,info=move_checks.inspect(source,root)
                valid=info is not None and stat.S_ISREG(info.st_mode) and [info.st_size,info.st_mtime_ns]==entry['signature']
                if valid and entry.get('sha256'):valid=sha(source)==entry['sha256']
            else:valid=entry_matches(source,entry)
            if not valid:
                raise ValueError('移动前来源发生变化')
            if source == destination:
                continue
            if destination.parent not in created:
                destination.parent.mkdir(parents=True,exist_ok=True);created.add(destination.parent)
            if fast:move_checks.inspect(destination,root)
            else:safe_path(destination,root)
            move_no_replace(source,destination)
            moved.append(entry)
            if not entry_matches(destination,entry):
                raise ValueError('整理后内容校验失败')
            journal['completed'].append(entry['before'])
            # prepared意图已保存全部moves，恢复逐项检查前后路径；无需每移动一项重写整个大日志。
        for record in plan['reference_records']:
            destination = moves[Path(record['source'])]['after']
            c.execute('UPDATE captures SET source=? WHERE id=?',(destination,record['id']))
        c.execute('CREATE TABLE IF NOT EXISTS archive_source_paths(current_path TEXT PRIMARY KEY, original_path TEXT NOT NULL)')
        for entry in moves.values():
            if entry['before']==entry['after']:
                continue
            old = c.execute('SELECT original_path FROM archive_source_paths WHERE current_path=?',(entry['before'],)).fetchone()
            c.execute('INSERT OR REPLACE INTO archive_source_paths VALUES (?,?)',(entry['after'],old[0] if old else entry['before']))
        journal['state']='moved_pending_database'
        save_journal(journal_path,journal)
        c.commit()
    except BaseException as error:
        c.rollback()
        failures=[]
        for entry in reversed(moved):
            before,after=Path(entry['before']),Path(entry['after'])
            try:
                if before.exists() or not after.exists() or not entry_matches(after,entry):
                    raise ValueError('移动后的路径被外部修改')
                move_no_replace(after,before)
            except BaseException as rollback_error:
                failures.append(str(rollback_error)+':'+str(after))
        journal.update(state='needs_recovery' if failures else 'rolled_back',error=str(error),rollback_errors=failures)
        save_journal(journal_path,journal)
        raise
    finally:
        c.close()
    journal['state']='completed'
    save_journal(journal_path,journal)
    mapping={str(source):entry['after'] for source,entry in moves.items()}
    for item in plan['items']:
        item['original_source']=item.get('original_source',item['source'])
        item['original_reference']=item.get('original_reference',item['reference'])
        item['file_identity_source']=item.get('file_identity_source',item['source'])
        item['source']=mapping[item['source']]
        item['reference']=mapping.get(item['reference'],item['reference'])
    for record in plan['reference_records']:
        record['source']=mapping[record['source']]
    for record in plan['skipped_reference_records']:
        record['original_reference']=record.get('original_reference',record['reference'])
        record['reference']=mapping.get(record['reference'],record['reference'])
        record['source']=record['reference']
    plan['manifest']={mapping.get(p,p):digest for p,digest in plan['manifest'].items()}
    plan['sidecar_manifest']={mapping.get(p,p):digest for p,digest in plan.get('sidecar_manifest',{}).items()}
    plan['preprocess_journal']=str(journal_path)
    plan['preprocess_moves']=[entry for entry in moves.values() if entry['before']!=entry['after']]
    progress(stage='preprocessing',done=len(moves),total=len(moves),path='',indeterminate=False)
    return plan
