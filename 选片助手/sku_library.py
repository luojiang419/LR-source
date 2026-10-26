"""SKU 库投影持久化 OCR 和已确认的资产归属，不从路径猜测款号。"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote

from sku_match import normalize, extract_codes
from sku_intervals import partition
from ocr_metadata import matching_ocr


def _json(value, default):
    try:
        return json.loads(value or '')
    except (TypeError, ValueError):
        return default


def _natural(value):
    return tuple((1,int(v)) if v.isdigit() else (0,v.casefold()) for v in re.split(r'(\d+)',value))


def recognized_rows(sku, session_id=''):
    if sku is None:
        return []
    conditions = ' AND session_id=?' if session_id else ''
    args = [session_id,session_id] if session_id else []
    with sku.connect() as c:
        c.execute('ATTACH DATABASE ? AS capture_source',(str(sku.store.db),))
        rows = [dict(r) for r in c.execute(f'''WITH evidence AS (
            SELECT id,session_id,capture_id,status,result,created,CASE WHEN updated>0 THEN updated ELSE created END AS evidence_at,'' AS targets,'' AS trigger FROM parse_jobs
              WHERE status='completed'{conditions}
            UNION ALL
            SELECT id,session_id,capture_id,status,result,created,CASE WHEN updated>0 THEN updated ELSE created END AS evidence_at,targets,trigger FROM jobs
              WHERE status IN ('matched','confirmed','review','unmatched'){conditions}
        ), latest AS (
            SELECT *,row_number() OVER (PARTITION BY session_id,capture_id ORDER BY evidence_at DESC,id DESC) AS position FROM evidence
        ) SELECT e.*,p.data,s.root FROM latest e
          JOIN capture_source.captures p ON p.session=e.session_id AND p.id=e.capture_id
          JOIN capture_source.sessions s ON s.id=e.session_id WHERE e.position=1''',args).fetchall()]
        target_skus={r['id']:r['sku'] for r in c.execute('SELECT id,sku FROM sku_rows')}
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='ocr_choices'").fetchone():
            selected=c.execute("""SELECT q.session_id,q.capture_id,q.sku,q.updated,p.data,s.root FROM ocr_choices q
                JOIN capture_source.captures p ON p.session=q.session_id AND p.id=q.capture_id
                JOIN capture_source.sessions s ON s.id=q.session_id"""+(' WHERE q.session_id=?' if session_id else ''),[session_id] if session_id else []).fetchall()
            by_capture={(r['session_id'],r['capture_id']):r for r in rows}
            for chosen in selected:
                by_capture[(chosen['session_id'],chosen['capture_id'])]={'id':'choice:'+chosen['capture_id'],'session_id':chosen['session_id'],'capture_id':chosen['capture_id'],
                    'status':'confirmed','result':json.dumps({'codes':[chosen['sku']]}),'created':chosen['updated'],'evidence_at':chosen['updated'],'targets':'[]',
                    'trigger':'choice','data':chosen['data'],'root':chosen['root']}
            rows=list(by_capture.values())
    recognized=[]
    sessions={}
    for row in rows:
        if row['trigger']=='full_auto' and row['status'] not in ('matched','confirmed'):continue
        result=_json(row['result'],{});record=_json(row['data'],{})
        if not isinstance(result,dict) or not isinstance(record,dict):
            continue
        if result.get('requires_sku_choice'):continue
        codes={}
        readings=[result]+(result.get('readings') or [])
        for reading in readings:
            if not isinstance(reading,dict):
                continue
            for code in reading.get('codes') or extract_codes(reading.get('text','')):
                if isinstance(code,str) and 0<len(code.strip())<=80 and normalize(code):
                    codes.setdefault(normalize(code),code.strip())
        if row['status'] in ('matched','confirmed'):
            targets={normalize(target_skus[t]):target_skus[t] for t in _json(row['targets'],[]) if t in target_skus}
            if targets:
                codes=targets
        # 多个候选需要人工确认，不让候选文字污染正式 SKU 列表。
        if len(codes)!=1:
            continue
        key,code=next(iter(codes.items()));relative=record.get('jpg') or record.get('raw')
        if not relative:
            continue
        root=row['root'];folder=PurePosixPath(relative).parent.as_posix()
        if row['session_id'] not in sessions:sessions[row['session_id']]=sku.store.session(row['session_id'])
        session=sessions[row['session_id']]
        rating=session['state'].get('overrides',{}).get(row['capture_id'],record.get('existingRating',0)) if session else 0
        preview=f"/api/preview/{quote(row['session_id'])}/{quote(row['capture_id'])}?v={quote(record.get('fingerprint',''))}"
        recognized.append({'id':row['session_id']+':'+row['capture_id'],'capture_id':row['capture_id'],'session_id':row['session_id'],
            'sku':code,'normalized':key,'source':'ocr','boundary':row['trigger'] not in ('full_auto','choice') or rating==2,'created':row['evidence_at'],'time':record.get('time') or '',
            'root':hashlib.sha256(root.casefold().encode()).hexdigest()[:32],'root_path':root,
            'folder':'/' if folder=='.' else folder,'relative':relative,'name':PurePosixPath(relative).name,
            'path':str(Path(root)/relative),'preview':preview,'has_jpg':bool(record.get('jpg')),'has_raw':bool(record.get('raw'))})
    # 同一文件出现在多个拍摄会话时仅显示最新证据，保留每个会话自己的缩略图标注。
    return recognized


def interval_rows(sku, session_id=''):
    markers=recognized_rows(sku,session_id)
    if not markers:
        return [],[]
    sessions={};direct=[]
    for marker in markers:
        if not marker['boundary']:
            direct.append({**marker,'interval_id':'','provenance':'onsite','provenance_label':'现场拍摄'})
            continue
        sessions.setdefault(marker['session_id'],{})[marker['capture_id']]=marker
    rows=[];intervals=[]
    for sid,boundaries in sessions.items():
        records=sku.store.records(sid)
        session=sku.store.session(sid);overrides=session['state'].get('overrides',{}) if session else {}
        # 尚未识别成功的二星标签也结束上一段，防止下一款覆盖这个未知区间。
        for record in records:
            if overrides.get(record['id'],record.get('existingRating',0))==2:
                boundaries.setdefault(record['id'],{'sku':'','normalized':''})
        by_id={record['id']:record for record in records};protected={}
        archive=getattr(sku,'archive',None)
        if archive:
            with archive.connect() as c:
                protected={row['capture']:normalize(row['sku']) for row in c.execute('SELECT k.capture,a.sku FROM catalog_links k JOIN assets a ON a.id=k.asset WHERE k.session=? AND a.locked=1',(sid,))}
        for interval in partition(records,boundaries):
            marker=interval['boundary'];interval_id=sid+':'+interval['label']
            if not marker['sku']:continue
            intervals.append({'id':interval_id,'session_id':sid,'label':interval['label'],'sku':marker['sku'],
                              'members':interval['members'],'order_source':interval['order_source']})
            for cid in interval['members']:
                if cid in protected and protected[cid]!=marker['normalized']:
                    continue
                record=by_id[cid];relative=record.get('jpg') or record.get('raw')
                folder=PurePosixPath(relative).parent.as_posix()
                rows.append({**marker,'id':sid+':'+cid,'capture_id':cid,'interval_id':interval_id,
                    'provenance':'onsite','provenance_label':'现场拍摄',
                    'source':'ocr' if cid==interval['label'] else 'interval','relative':relative,
                    'folder':'/' if folder=='.' else folder,'name':PurePosixPath(relative).name,
                    'path':str(Path(marker['root_path'])/relative),'time':record.get('time') or '',
                    'preview':f"/api/preview/{quote(sid)}/{quote(cid)}?v={quote(record.get('fingerprint',''))}",
                    'has_jpg':bool(record.get('jpg')),'has_raw':bool(record.get('raw'))})
    # 普通全图结果保持单张；二星结束标签按区间扩展，明确的单图归属优先。
    direct_ids={r['id'] for r in direct}
    return [r for r in rows if r['id'] not in direct_ids]+direct,intervals


def archived_rows(sku,onsite):
    archive=getattr(sku,'archive',None)
    if not archive:return []
    with sku.store.connect() as c:
        sessions={r['id']:dict(r) for r in c.execute("SELECT s.* FROM sessions s JOIN settings t ON t.key='archive_session:'||s.id WHERE NOT EXISTS (SELECT 1 FROM settings d WHERE d.key='archive_disabled:'||s.id)")}
        exclusions={r['key'].split(':',1)[1]:_json(r['value'],[]) for r in c.execute("SELECT key,value FROM settings WHERE key LIKE 'archive_excluded:%'")}
    if not sessions:return []
    with archive.connect() as c:
        c.execute('ATTACH DATABASE ? AS capture_source',(str(sku.store.db),))
        records=[dict(r) for r in c.execute("""SELECT k.session,k.capture,a.id AS asset_id,a.sku,a.source,a.created,p.data
          FROM catalog_links k JOIN assets a ON a.id=k.asset
          JOIN capture_source.captures p ON p.session=k.session AND p.id=k.capture
          WHERE a.sku!='' AND EXISTS(SELECT 1 FROM capture_source.settings t WHERE t.key='archive_session:'||k.session)
          AND EXISTS(SELECT 1 FROM assignments x WHERE x.asset=a.id)""")]
    result=[]
    for row in records:
        session=sessions.get(row['session'])
        if not session:continue
        record=_json(row['data'],{});relative=record.get('jpg') or record.get('raw')
        if not relative:continue
        root=session['root'];folder=PurePosixPath(relative).parent.as_posix();path=str(Path(root)/relative)
        if any(folder==skip or folder.startswith(skip+'/') for skip in exclusions.get(row['session'],[])):continue
        result.append({'id':row['session']+':'+row['capture'],'capture_id':row['capture'],'session_id':row['session'],'asset_id':row['asset_id'],
            'sku':row['sku'],'normalized':normalize(row['sku']),'source':'archive','provenance':'archive','provenance_label':'最终归档','created':row['created'],'time':record.get('time') or '',
            'root':hashlib.sha256(root.casefold().encode()).hexdigest()[:32],'root_path':root,'folder':'/' if folder=='.' else folder,
            'relative':relative,'name':PurePosixPath(relative).name,'path':path,'preview':f"/api/preview/{quote(row['session'])}/{quote(row['capture'])}?v={quote(record.get('fingerprint',''))}",
            'has_jpg':bool(record.get('jpg')),'has_raw':bool(record.get('raw'))})
    return result


def onsite_assigned_rows(sku,session_id=''):
    archive=getattr(sku,'archive',None)
    if not archive:return []
    with archive.connect() as c:
        c.execute('ATTACH DATABASE ? AS capture_source',(str(sku.store.db),))
        where=' AND k.session=?' if session_id else ''
        records=c.execute("""SELECT k.session,k.capture,a.sku,a.created,p.data,s.root
            FROM catalog_links k JOIN assets a ON a.id=k.asset
            JOIN capture_source.captures p ON p.session=k.session AND p.id=k.capture
            JOIN capture_source.sessions s ON s.id=k.session
            WHERE a.sku!='' AND EXISTS(SELECT 1 FROM assignments x WHERE x.asset=a.id)
            AND NOT EXISTS(SELECT 1 FROM capture_source.settings t WHERE t.key='archive_session:'||k.session)"""+where,[session_id] if session_id else []).fetchall()
    result=[]
    for row in records:
        record=_json(row['data'],{});relative=record.get('jpg') or record.get('raw')
        if not relative:continue
        folder=PurePosixPath(relative).parent.as_posix();root=row['root'];sid=row['session'];cid=row['capture']
        result.append({'id':sid+':'+cid,'capture_id':cid,'session_id':sid,'sku':row['sku'],'normalized':normalize(row['sku']),
            'source':'assignment','provenance':'onsite','provenance_label':'现场拍摄','created':row['created'],'time':record.get('time') or '',
            'root':hashlib.sha256(root.casefold().encode()).hexdigest()[:32],'root_path':root,'folder':'/' if folder=='.' else folder,
            'relative':relative,'name':PurePosixPath(relative).name,'path':str(Path(root)/relative),
            'preview':f"/api/preview/{quote(sid)}/{quote(cid)}?v={quote(record.get('fingerprint',''))}",
            'has_jpg':bool(record.get('jpg')),'has_raw':bool(record.get('raw'))})
    return result


def labels(sku, session_id, capture_ids=None):
    if capture_ids is not None:
        if len(capture_ids)>240:raise ValueError('一次读取的照片标签过多')
        archive=getattr(sku,'archive',None)
        if not archive or not capture_ids:return {'session_id':session_id,'labels':{},'interval_count':0}
        with archive.connect() as db:
            rows=db.execute('SELECT k.capture,a.sku FROM catalog_links k JOIN assets a ON a.id=k.asset WHERE k.session=? AND k.capture IN ('+','.join('?' for _ in capture_ids)+") AND a.sku!=''",(session_id,*capture_ids)).fetchall()
            intervals=db.execute("SELECT count(*) FROM segments g JOIN streams s ON s.id=g.stream WHERE s.session=? AND g.sku!='' AND g.status NOT IN ('cancelled','superseded')",(session_id,)).fetchone()[0]
        return {'session_id':session_id,'labels':{r['capture']:r['sku'] for r in rows},'interval_count':intervals}
    rows,intervals=interval_rows(sku,session_id)
    rows+=onsite_assigned_rows(sku,session_id)
    return {'session_id':session_id,'labels':{r['capture_id']:r['sku'] for r in rows},'interval_count':len(intervals)}


def browse(sku, search='', order='name', root='', folder='', code='', offset=0, limit=120,*,bin_id='',include_removed=False,all_rows=False,management=True):
    orders={'name','name_desc','added','added_asc','captured','captured_asc'}
    if order not in orders:
        raise ValueError('SKU 排序方式无效')
    offset=max(0,int(offset));limit=max(1,min(240,int(limit)));query=normalize(search)
    unique={}
    projected,intervals=interval_rows(sku)
    inherited={r['id']:r for r in projected}
    assigned=onsite_assigned_rows(sku)
    for row in assigned:
        prior=inherited.get(row['id'])
        if prior and prior['normalized']==row['normalized'] and prior.get('interval_id'):
            row['interval_id']=prior['interval_id']
    projected+=assigned
    projected+=archived_rows(sku,projected)
    for row in sorted(projected,key=lambda r:(r['source'] in ('assignment','archive'),r['created'],r['id'])):
        identity=str(Path(row['path']).with_suffix('')).casefold()
        unique[identity]=row
    matched=matching_ocr(sku,search) if search.strip() else []
    auxiliary={r['session_id']+':'+r['capture_id'] for r in matched}
    rows=[r for r in unique.values() if not search.strip() or (query and query in r['normalized']) or r['id'] in auxiliary or r.get('interval_id') in auxiliary]
    # 尚未确认主款号的图片也可按 OCR 辅助资料找到，显示为待选择 SKU。
    known={r['id'] for r in unique.values()};sessions={}
    for doc in matched:
        identity=doc['session_id']+':'+doc['capture_id']
        if identity in known:continue
        known.add(identity);sid=doc['session_id']
        if sid not in sessions:sessions[sid]=(sku.store.session(sid),{r['id']:r for r in sku.store.records(sid)})
        session,records=sessions[sid];record=records.get(doc['capture_id'])
        if not session or not record:continue
        relative=record.get('jpg') or record.get('raw')
        if not relative:continue
        folder_name=PurePosixPath(relative).parent.as_posix();base=session['root']
        rows.append({'id':identity,'session_id':sid,'capture_id':doc['capture_id'],'sku':'','normalized':'','source':'ocr_auxiliary',
            'provenance':'onsite','provenance_label':'待选择 SKU','created':doc['updated'],'time':record.get('time') or '',
            'root':hashlib.sha256(base.casefold().encode()).hexdigest()[:32],'root_path':base,'folder':'/' if folder_name=='.' else folder_name,
            'relative':relative,'name':PurePosixPath(relative).name,'path':str(Path(base)/relative),'preview':f'/api/preview/{quote(sid)}/{quote(doc["capture_id"])}',
            'has_jpg':bool(record.get('jpg')),'has_raw':bool(record.get('raw'))})
    from sku_metadata_sync import prefer_local_archives
    if getattr(sku,'archive',None):rows=prefer_local_archives(sku,rows)
    organization=None
    if management:
        from sku_library_management import organize
        rows,organization=organize(sku,rows,bin_id,include_removed)
    groups={};roots={}
    for row in rows:
        if not row['normalized']:continue
        group=groups.setdefault(row['normalized'],{'sku':row['sku'],'normalized':row['normalized'],'count':0,'created':0,'name':row['name'],'time':row['time']})
        group['count']+=1;group['created']=max(group['created'],row['created']);group['name']=min(group['name'],row['name'])
        location=roots.setdefault(row['root'],{'id':row['root'],'path':row['root_path'],'folders':{}})
        directory=location['folders'].setdefault(row['folder'],{'folder':row['folder'],'count':0,'skus':{}});directory['count']+=1
        entry=directory['skus'].setdefault(row['normalized'],{'sku':row['sku'],'normalized':row['normalized'],'count':0,'created':0});entry['count']+=1
        entry['created']=max(entry['created'],row['created'])
    def sort_key(row):
        return (_natural(row['name']) if order.startswith('name') else row['created'] if order.startswith('added') else row['time'],row.get('id',row.get('normalized','')))
    reverse=order in {'name_desc','added','captured'}
    group_rows=sorted(groups.values(),key=sort_key,reverse=reverse)
    root_rows=[]
    for location in roots.values():
        directories=[]
        for directory in location['folders'].values():
            directory['skus']=sorted(directory['skus'].values(),key=lambda r:_natural(r['sku']));directories.append(directory)
        root_rows.append({**location,'folders':directories})
    selected=[r for r in rows if (not root or r['root']==root) and (not folder or r['folder']==folder or r['folder'].startswith(folder.rstrip('/')+'/')) and (not code or r['normalized']==normalize(code))]
    selected.sort(key=sort_key,reverse=reverse)
    visible_ids={r.get('interval_id') for r in rows}
    visible_intervals=[i for i in intervals if not search.strip() or (query and query in normalize(i['sku'])) or i['id'] in auxiliary or i['id'] in visible_ids]
    captures={r['id'] for r in rows}
    visible_intervals=[i for i in visible_intervals if any(i['session_id']+':'+cid in captures for cid in i['members'])]
    summaries=[{'id':i['id'],'label':i['label'],'sku':i['sku'],'count':len(i['members']),'order_source':i['order_source']} for i in visible_intervals]
    page_rows=selected if all_rows else selected[offset:offset+limit]
    if not all_rows:page_rows=[dict(row,offline=not Path(row['path']).is_file()) for row in page_rows]
    return {'groups':group_rows,'roots':root_rows,'rows':page_rows,'total':len(selected),'offset':offset,'has_more':False if all_rows else len(selected)>offset+limit,'interval_count':len(summaries),'intervals':summaries,'organization':organization}
