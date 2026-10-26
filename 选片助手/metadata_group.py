"""整款 SKU 编辑：以拍摄边界定位成员，提交前复核范围，复用持久写入队列。"""
import hashlib
import json
import time

from sku_intervals import partition


def group_context(archive,sku,sid,cid,selection):
    session=archive.store.session(sid)
    records=archive.store.records(sid) if session else []
    by_id={r['id']:r for r in records}
    if cid not in by_id:raise ValueError('当前照片不存在，请重新选择')
    overrides=session['state'].get('overrides',{})
    boundaries={r['id']:None for r in records if overrides.get(r['id'],r.get('existingRating',0))==2}
    with archive.connect() as c:
        links={r['capture']:dict(r) for r in c.execute('SELECT k.capture,a.* FROM catalog_links k JOIN assets a ON a.id=k.asset WHERE k.session=?',(sid,))}
        segments=[dict(r) for r in c.execute("SELECT g.*,b.capture FROM segments g JOIN streams s ON s.id=g.stream JOIN bindings b ON b.stream=s.id AND b.asset=g.label WHERE s.session=? AND g.status NOT IN ('cancelled','superseded') ORDER BY g.created",(sid,))]
    for segment in segments:boundaries[segment['capture']]=segment
    interval=next((g for g in partition(records,boundaries) if cid in g['members']),None)
    if interval:
        members=interval['members'];label=interval['label'];segment=interval['boundary']
        kind='interval';segment_id=segment['id'] if segment else ''
    else:
        members=list(dict.fromkeys(selection or [cid]));label=cid;kind='selection';segment_id=''
        if not members or len(members)>2000 or any(member not in by_id for member in members):raise ValueError('所选照片已变化，请重新选择')
        # 已有其他款边界的图片不能混进未封口尾部的手动选择。
        sealed={member for g in partition(records,boundaries) for member in g['members']}
        if any(member in sealed for member in members):raise ValueError('所选照片跨越其他款，请从目标款的一张照片进入修改')
    assigned=[links[member] for member in members if member in links]
    values={a['sku'] for a in assigned if a['sku']}
    code=next(iter(values)) if len(values)==1 else ''
    scopes={a['scope'] for a in assigned if a['scope']}
    scope=next(iter(scopes)) if len(scopes)==1 else ''
    projects={a['project'] for a in assigned if a['project']}
    project=next(iter(projects)) if len(projects)==1 else ''
    revision=[(member,links[member]['id'],links[member]['revision']) if member in links else (member,None,0) for member in members]
    identity=[sid,cid,label,kind,segment_id,members,revision]
    key=hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()
    return dict(session_id=sid,capture_id=cid,capture_ids=members,label=label,kind=kind,segment=segment_id,sku=code,scope=scope,project=project,key=key)


def preview_group(archive,sku,sid,cid,selection,code,scope,project=''):
    with archive.lock:
        group=group_context(archive,sku,sid,cid,selection)
        # 侧栏输入作为草稿；空草稿沿用现有整款归属。
        code=code or group['sku']
        scope=(group['scope'] or scope) if group['kind']=='interval' else (scope or group['scope'])
        project=(group['project'] or project) if group['kind']=='interval' else (project or group['project'])
        preview=archive.preview(sid,group['capture_ids'],code or 'PENDING-SKU',scope,project,overwrite=True)
        preview['sku']=code
        group=group_context(archive,sku,sid,cid,selection) # ensure 后的实际资产版本用于提交校验。
        preview['group']=group
        with archive.connect() as c:c.execute('UPDATE previews SET data=? WHERE id=?',(json.dumps({k:v for k,v in preview.items() if k!='preview_id'},ensure_ascii=False),preview['preview_id']))
        return preview


def apply_group(archive,sku,pid,code):
    # 服务层始终先拿 SKU 锁再拿归档锁，与现有 OCR 确认的锁顺序一致。
    from contextlib import nullcontext
    with (sku.lock if sku else nullcontext()),archive.lock:
        with archive.connect() as c:row=c.execute('SELECT * FROM previews WHERE id=?',(pid,)).fetchone()
        if not row or row['used'] or time.time()-row['created']>900:raise ValueError('编辑已失效，请重新打开整款编辑')
        if isinstance(code,str):code=code.strip()
        preview=json.loads(row['data']);original=preview.get('group')
        if not original:raise ValueError('此操作不是整款编辑，请重新打开')
        current=group_context(archive,sku,original['session_id'],original['capture_id'],original['capture_ids'])
        if current['key']!=original['key']:raise ValueError('整款范围或归属已变化，请重新打开编辑')
        # 先校验新输入，任何非法 SKU 不得改变选择或归属。
        fresh=archive.preview(current['session_id'],current['capture_ids'],code,preview['scope'],preview['project'],overwrite=True)
        chosen={}
        if current['kind']=='interval' and sku:
            details=sku.ocr_details(current['session_id'],current['label'])
            chosen=sku.choose_ocr_sku(current['session_id'],current['label'],code,details['reading_key'],details['image_key'],preview['scope'],allow_manual=True,project_id=preview['project'] or None)
            current=group_context(archive,sku,current['session_id'],current['capture_id'],current['capture_ids'])
        # 明确修改整款允许覆盖该范围原 SKU，保留区间身份，避免变成单图人工锁定。
        if current['kind']=='interval':
            fresh=archive.preview(current['session_id'],current['capture_ids'],code,preview['scope'],preview['project'],overwrite=True,source='segment',segment=current['segment'])
        result=archive.apply(fresh['preview_id'])
        with archive.connect() as c:
            c.execute('UPDATE previews SET used=1 WHERE id=?',(pid,))
            if current['segment']:c.execute("UPDATE segments SET sku=?,operation=?,status='confirmed',reason='' WHERE id=?",(code.strip(),result['operation']['id'],current['segment']))
        return {**result,**chosen,'session_id':current['session_id'],'capture_id':original['capture_id'],'sku':code.strip(),'capture_count':len(current['capture_ids']),'group_kind':current['kind']}
