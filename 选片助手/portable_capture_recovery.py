"""多机拍摄档案的只读合并与可验证接续；不删除原提交、不改写照片。"""
import json
import re
import uuid

from portable_capture_store import MAX_EVENT,digest,encoded,publish_file,read_json,relative_path,resolve_under


def _filename(value):
    name=relative_path(value)
    if '/' in name or name!=uuid.UUID(name.removesuffix('.json')).hex+'.json':
        raise ValueError('接续记录的事件文件名无效')
    return name


def _proof(commit):
    source=uuid.UUID(str(commit['dataset_id'])).hex
    if commit.get('schema')!=1 or type(commit.get('revision')) is not int or commit['revision']<1:
        raise ValueError('接续来源提交格式无效')
    sha=commit.get('sha256','')
    if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):raise ValueError('接续来源摘要无效')
    return {'dataset_id':source,'event':_filename(commit['event']),'sha256':sha}


def _event(dataset,commit):
    proof=_proof(commit);path=resolve_under(dataset.folder,'events/'+proof['event'])
    if path.stat().st_size>MAX_EVENT:raise ValueError('接续事件过大')
    content=path.read_bytes()
    if digest(content)!=proof['sha256']:raise ValueError('接续来源事件摘要不一致')
    event=json.loads(content)
    if (event.get('schema')!=1 or event.get('dataset_id')!=proof['dataset_id'] or
        event.get('id')!=proof['event'][:-5] or event.get('revision')!=commit['revision']):
        raise ValueError('接续来源事件身份或版本不一致')
    return event


def _marker(dataset,commit):
    return resolve_under(dataset.folder,'recovered/'+digest(encoded(_proof(commit)))+'.json')


def _payload(event,proof):
    return ([{**r,'recovered_from':proof} for r in event['updates']],
            [{**g,'recovered_from':proof} for g in event['groups']])


def _check_import(dataset,commit,event,source):
    if commit.get('dataset_id')!=dataset.dataset_id or event.get('kind')!='foreign_dataset_recovery':
        raise ValueError('接续目标不是当前数据集的恢复提交')
    updates,groups=_payload(source,_proof(source['_commit']))
    if event.get('updates')!=updates or event.get('groups')!=groups:
        raise ValueError('接续记录与来源内容不一致')


def is_recovered(dataset,commit):
    """只有有完整规范接续证据的外来提交才可跳过；未知外来提交继续阻挡编辑。"""
    proof=_proof(commit)
    path=_marker(dataset,commit)
    if not path.is_file():return False
    marker=read_json(path)
    if marker.get('schema')!=1 or marker.get('dataset_id')!=dataset.dataset_id or marker.get('source')!=proof:
        raise ValueError('接续标记身份不一致')
    target=marker.get('target',{})
    if not isinstance(target,dict) or type(target.get('revision')) is not int or target['revision']<1 or not isinstance(target.get('event'),str):
        raise ValueError('接续目标提交格式无效')
    filename=_filename(target['event'])
    target_commit=read_json(resolve_under(dataset.folder,'commits/'+f"{target['revision']:020d}-"+filename))
    if any(target_commit.get(k)!=target.get(k) for k in ('event','revision','sha256')):
        raise ValueError('接续目标提交摘要不一致')
    imported=_event(dataset,target_commit);source=_event(dataset,commit);source['_commit']=commit
    _check_import(dataset,target_commit,imported,source)
    return True


def _validate_source(dataset,commit,event):
    if commit['dataset_id']==dataset.dataset_id:raise ValueError('来源已经是当前数据集')
    if event.get('parent')!=commit['revision']-1 or event.get('kind') not in ('live_confirm','confirm'):
        raise ValueError('来源不是完整确认的拍摄区间，请核对后接续')
    updates=event.get('updates');groups=event.get('groups')
    if not isinstance(updates,list) or not updates or not isinstance(groups,list) or not groups:
        raise ValueError('接续来源缺少完整区间')
    paths=set();identities=set();assets={r.get('asset_id') for r in updates};files=set()
    for record in updates:
        dataset._validate(record)
        path=relative_path(record['relative_path']).casefold();identity=record['id'];file_id=record.get('file_id')
        if path in paths or identity in identities or not file_id or file_id in files:
            raise ValueError('接续来源存在重复路径或身份')
        if record.get('assignment_state')!='confirmed' or not record.get('sku') or not record.get('decision_id'):
            raise ValueError('接续来源尚未确认款号')
        paths.add(path);identities.add(identity);files.add(file_id)
    covered=set();group_ids=set()
    for group in groups:
        dataset._validate(group);members=set(group.get('members',[]));group_ids.add(group['id'])
        rows=[r for r in updates if r.get('group_id')==group['id']]
        row_assets={r['asset_id'] for r in rows};row_files={r['file_id'] for r in rows}
        labels={r['asset_id'] for r in rows if r.get('rating')==2 and r.get('boundary') and r.get('role')=='sku_label'}
        if (not members or group.get('status')!='confirmed' or not group.get('sku') or members!=row_assets or covered&members or
            group.get('label') not in labels or len(labels)!=1 or
            any(r['sku']!=group['sku'] for r in rows) or
            group.get('expected_files')!=len(rows) or group.get('expected_labels')!=len(labels) or
            group.get('expected_products')!=len(row_assets-labels) or
            group.get('members_digest')!=digest(encoded(sorted(row_assets))) or
            group.get('files_digest')!=digest(encoded(sorted(row_files)))):
            raise ValueError('接续来源区间成员或数量校验不一致')
        covered.update(members)
    if covered!=assets or any(r.get('group_id') not in group_ids for r in updates):
        raise ValueError('接续来源没有覆盖全部图片')


def _validate_files(dataset,updates,groups,proof):
    current=[r for r in dataset.records() if not (r.get('source_projection') and r.get('recovered_from')==proof)]
    by_path={r['relative_path'].casefold():r for r in current};ids={r['id'] for r in current};assets={r['asset_id'] for r in current};file_ids={r.get('file_id') for r in current}
    existing_groups={g['id'] for g in dataset.groups() if not (g.get('source_projection') and g.get('recovered_from')==proof)}
    for record in updates:
        if record['relative_path'].casefold() in by_path or record['id'] in ids or record['asset_id'] in assets or record['file_id'] in file_ids:
            raise ValueError('接续图片与当前档案路径或身份冲突：'+record['relative_path'])
        path=resolve_under(dataset.root,record['relative_path']);expected=record.get('file_sha256','')
        if not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('接续图片缺少可靠文件摘要')
        import hashlib
        with path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=expected:raise ValueError('接续图片内容与原记录不一致：'+record['relative_path'])
    if any(g['id'] in existing_groups for g in groups):raise ValueError('接续区间身份与当前档案冲突')


def _adopt_locked(dataset,commit,source):
    dataset.ownership.check();proof=_proof(commit);updates,groups=_payload(source,proof);target_commit=None
    source['_commit']=commit
    # 中断后复用已发布的接续事件；原始多来源记录始终保留。
    for path in sorted((dataset.folder/'commits').glob('*.json')):
        candidate=read_json(path)
        if candidate.get('dataset_id')!=dataset.dataset_id:continue
        event=_event(dataset,candidate)
        if event.get('kind')=='foreign_dataset_recovery' and event.get('updates')==updates and event.get('groups')==groups:
            _check_import(dataset,candidate,event,source);target_commit=candidate;break
    if target_commit is None:
        _validate_files(dataset,updates,groups,proof)
        operation=dataset.change(updates,groups,kind='foreign_dataset_recovery')
        dataset.publish()
        target_commit=read_json(resolve_under(dataset.folder,'commits/'+f"{int(dataset.meta('revision')):020d}-{operation}.json"))
        _check_import(dataset,target_commit,_event(dataset,target_commit),source)
    marker={'schema':1,'dataset_id':dataset.dataset_id,'source':proof,
            'target':{k:target_commit[k] for k in ('event','revision','sha256')}}
    publish_file(_marker(dataset,commit),encoded(marker))
    if not is_recovered(dataset,commit):raise ValueError('接续标记回读失败')
    dataset.remote_revision=max(dataset.remote_revision,target_commit['revision'])


def project_sources(dataset,commits):
    """多机完整区间先只读合并；只有取得原生写锁后才追加规范接续事件。"""
    with dataset.connect() as c:c.execute('CREATE TABLE IF NOT EXISTS source_projections(id TEXT PRIMARY KEY)')
    for commit in commits:
        if is_recovered(dataset,commit):continue
        source=_event(dataset,commit);_validate_source(dataset,commit,source);proof=_proof(commit)
        if dataset.editing:
            _adopt_locked(dataset,commit,source)
            continue
        key=digest(encoded(proof))
        with dataset.connect() as c:projected=c.execute('SELECT 1 FROM source_projections WHERE id=?',(key,)).fetchone()
        if projected:continue
        updates,groups=_payload(source,proof);_validate_files(dataset,updates,groups,proof)
        updates=[{**r,'source_projection':True} for r in updates];groups=[{**g,'source_projection':True} for g in groups]
        with dataset.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for record in updates:c.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,data=excluded.data',(record['id'],record['relative_path'],encoded(record).decode()))
            for group in groups:c.execute('INSERT INTO groups_ VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',(group['id'],encoded(group).decode()))
            c.execute('INSERT OR IGNORE INTO source_projections VALUES(?)',(key,))
        dataset.imported_records.extend(updates)


def recover(dataset,source_commit):
    """显式接续入口：浏览已可读取多来源记录；在锁下追加证据，不修改照片。"""
    if dataset.editing:raise ValueError('请先交接当前编辑会话，再接续独立档案')
    commit=read_json(resolve_under(dataset.folder,'commits/'+relative_path(source_commit)))
    source=_event(dataset,commit)
    dataset.ownership.acquire()
    try:
        descriptor=read_json(dataset.folder/'dataset.json')
        with dataset.connect() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('dataset_id',?)",(uuid.UUID(descriptor['dataset_id']).hex,))
        before=is_recovered(dataset,commit)
        dataset.refresh()
        if not is_recovered(dataset,commit):raise ValueError('来源拍摄档案尚未完成接续')
        dataset.snapshot()
        return {'recovered':not before,'records':len(source['updates']),'groups':len(source['groups']),
                'source_dataset':commit['dataset_id'],'target_revision':int(dataset.meta('revision'))}
    finally:dataset.ownership.release()
