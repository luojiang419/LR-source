"""SKU 库仅投影持久化 OCR 证据，不把路径候选当成识别结果。"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote

from sku_match import normalize, extract_codes
from sku_intervals import partition


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
        rows = c.execute(f'''WITH evidence AS (
            SELECT id,session_id,capture_id,status,result,created,'' AS targets FROM parse_jobs
              WHERE status='completed'{conditions}
            UNION ALL
            SELECT id,session_id,capture_id,status,result,created,targets FROM jobs
              WHERE status IN ('matched','confirmed','review','unmatched'){conditions}
        ), latest AS (
            SELECT *,row_number() OVER (PARTITION BY session_id,capture_id ORDER BY created DESC,id DESC) AS position FROM evidence
        ) SELECT e.*,p.data,s.root FROM latest e
          JOIN capture_source.captures p ON p.session=e.session_id AND p.id=e.capture_id
          JOIN capture_source.sessions s ON s.id=e.session_id WHERE e.position=1''',args).fetchall()
        target_skus={r['id']:r['sku'] for r in c.execute('SELECT id,sku FROM sku_rows')}
    recognized=[]
    for row in rows:
        result=_json(row['result'],{});record=_json(row['data'],{})
        if not isinstance(result,dict) or not isinstance(record,dict):
            continue
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
        preview=f"/api/preview/{quote(row['session_id'])}/{quote(row['capture_id'])}?v={quote(record.get('fingerprint',''))}"
        recognized.append({'id':row['session_id']+':'+row['capture_id'],'capture_id':row['capture_id'],'session_id':row['session_id'],
            'sku':code,'normalized':key,'source':'ocr','created':row['created'],'time':record.get('time') or '',
            'root':hashlib.sha256(root.casefold().encode()).hexdigest()[:32],'root_path':root,
            'folder':'/' if folder=='.' else folder,'relative':relative,'name':PurePosixPath(relative).name,
            'path':str(Path(root)/relative),'preview':preview,'has_jpg':bool(record.get('jpg')),'has_raw':bool(record.get('raw'))})
    # 同一文件出现在多个拍摄会话时仅显示最新证据，保留每个会话自己的缩略图标注。
    return recognized


def interval_rows(sku, session_id=''):
    markers=recognized_rows(sku,session_id)
    if not markers:
        return [],[]
    sessions={}
    for marker in markers:
        sessions.setdefault(marker['session_id'],{})[marker['capture_id']]=marker
    rows=[];intervals=[]
    for sid,boundaries in sessions.items():
        records=sku.store.records(sid)
        by_id={record['id']:record for record in records};protected={}
        archive=getattr(sku,'archive',None)
        if archive:
            with archive.connect() as c:
                protected={row['capture']:normalize(row['sku']) for row in c.execute('SELECT k.capture,a.sku FROM catalog_links k JOIN assets a ON a.id=k.asset WHERE k.session=? AND a.locked=1',(sid,))}
        for interval in partition(records,boundaries):
            marker=interval['boundary'];interval_id=sid+':'+interval['label']
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
    return rows,intervals


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
        if not Path(path).is_file():continue
        result.append({'id':row['session']+':'+row['capture'],'capture_id':row['capture'],'session_id':row['session'],'asset_id':row['asset_id'],
            'sku':row['sku'],'normalized':normalize(row['sku']),'source':'archive','provenance':'archive','provenance_label':'最终归档','created':row['created'],'time':record.get('time') or '',
            'root':hashlib.sha256(root.casefold().encode()).hexdigest()[:32],'root_path':root,'folder':'/' if folder=='.' else folder,
            'relative':relative,'name':PurePosixPath(relative).name,'path':path,'preview':f"/api/preview/{quote(row['session'])}/{quote(row['capture'])}?v={quote(record.get('fingerprint',''))}",
            'has_jpg':bool(record.get('jpg')),'has_raw':bool(record.get('raw'))})
    return result


def labels(sku, session_id):
    rows,intervals=interval_rows(sku,session_id)
    return {'session_id':session_id,'labels':{r['capture_id']:r['sku'] for r in rows},'interval_count':len(intervals)}


def browse(sku, search='', order='name', root='', folder='', code='', offset=0, limit=120):
    orders={'name','name_desc','added','added_asc','captured','captured_asc'}
    if order not in orders:
        raise ValueError('SKU 排序方式无效')
    offset=max(0,int(offset));limit=max(1,min(240,int(limit)));query=normalize(search)
    unique={}
    projected,intervals=interval_rows(sku)
    projected+=archived_rows(sku,projected)
    for row in sorted(projected,key=lambda r:(r['created'],r['id'])):
        identity=str(Path(row['path']).with_suffix('')).casefold()
        unique[identity]=row
    rows=[r for r in unique.values() if not query or query in r['normalized']]
    groups={};roots={}
    for row in rows:
        group=groups.setdefault(row['normalized'],{'sku':row['sku'],'normalized':row['normalized'],'count':0,'created':0,'name':row['name'],'time':row['time']})
        group['count']+=1;group['created']=max(group['created'],row['created']);group['name']=min(group['name'],row['name'])
        location=roots.setdefault(row['root'],{'id':row['root'],'path':row['root_path'],'folders':{}})
        directory=location['folders'].setdefault(row['folder'],{'folder':row['folder'],'count':0,'skus':{}});directory['count']+=1
        entry=directory['skus'].setdefault(row['normalized'],{'sku':row['sku'],'normalized':row['normalized'],'count':0});entry['count']+=1
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
    visible_intervals=[i for i in intervals if not query or query in normalize(i['sku'])]
    summaries=[{'id':i['id'],'label':i['label'],'sku':i['sku'],'count':len(i['members']),'order_source':i['order_source']} for i in visible_intervals]
    return {'groups':group_rows,'roots':root_rows,'rows':selected[offset:offset+limit],'total':len(selected),'offset':offset,'has_more':len(selected)>offset+limit,'interval_count':len(summaries),'intervals':summaries}
