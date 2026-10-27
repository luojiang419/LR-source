"""款号清单显示偏好和封面选择，不修改款号归属或拍摄证据。"""
import json
from urllib.parse import quote

from sku_match import normalize

FIXED_HEADERS={'seq':'序号','row':'原表行','sku':'款号','photo':'款号图片',
               'status':'拍摄状态','time':'拍摄完成时间','source':'所属工作表'}


def header_key(sheet,column):
    return 'sku' if column['index']==sheet['sku_col'] else f"column:{sheet['name']}:{column['letter']}"


def preferences(sku,pid):
    with sku.connect() as c:row=c.execute("SELECT json_extract(metadata,'$.presentation') FROM projects WHERE id=?",(pid,)).fetchone()
    return json.loads(row[0]) if row and row[0] else {'headers':{},'images':{}}


def save_preferences(sku,pid,value):
    with sku.connect() as c:c.execute("UPDATE projects SET metadata=json_set(metadata,'$.presentation',json(?)) WHERE id=?",(json.dumps(value,ensure_ascii=False),pid))


def rename_header(sku,pid,key,title):
    with sku.lock:
        project=sku.project(pid)
        if not project:raise ValueError('拍摄任务不存在')
        valid=set(FIXED_HEADERS)
        for sheet in project['metadata']['sheets']:
            if not sheet.get('skipped'):valid.update(header_key(sheet,col) for col in sheet.get('columns',[]))
        if key not in valid:raise ValueError('此列已变化，请刷新列表')
        if not isinstance(title,str) or not title.strip() or len(title.strip())>40 or any(ord(ch)<32 for ch in title):raise ValueError('列名须为 1–40 个可见字符')
        before=preferences(sku,pid);value=json.loads(json.dumps(before));value['headers'][key]=title.strip();save_preferences(sku,pid,value)
        sku.dirty.add(pid)
        # 即时更新工作副本，识别线程的后续导出也沿用此偏好。
        try:sku.export(pid)
        except Exception:
            save_preferences(sku,pid,before)
            raise
        return sku.state()


def grouped_rows(rows,sheets):
    groups={};order={s['name']:i for i,s in enumerate(sheets) if not s.get('skipped')}
    for row in sorted(rows,key=lambda r:(order.get(r['sheet'],99999),r['excel_row'])):
        key=row['normalized'];group=groups.setdefault(key,{'sku':row['sku'],'normalized':key,'rows':[],'memberships':{},'sku_image':None})
        group['rows'].append({'sheet':row['sheet'],'excel_row':row['excel_row'],'id':row['id']})
        group['memberships'][row['sheet']]=group['memberships'].get(row['sheet'],False) or row['status']=='已拍摄'
        if not group['sku_image'] and row.get('sku_image'):group['sku_image']=row['sku_image']
    for group in groups.values():group['shot']=all(group['memberships'].values());group['has_shots']=any(group['memberships'].values())
    return list(groups.values())


def gallery_items(sku,pid,code=''):
    from sku_library import browse
    project=sku.project(pid)
    if not project:raise ValueError('拍摄任务不存在')
    groups={};requested=normalize(code)
    for row in sku.rows(pid):
        if row['status']=='已拍摄' and (not requested or row['normalized']==requested):groups.setdefault(row['normalized'],{'sku':row['sku'],'normalized':row['normalized'],'images':[]})
    if not groups:return []
    data=browse(sku,all_rows=True)
    with sku.connect() as c:versions={normalize(row['sku']):normalize(row['base_sku']) for row in c.execute('SELECT sku,base_sku FROM sku_shoot_versions')}
    sessions={};ratings={}
    for item in data['rows']:
        if not item.get('normalized'):continue
        sid=item['session_id']
        if sid not in sessions:
            sessions[sid]=sku.store.session(sid) or {}
            ratings[sid]=sessions[sid].get('state',{}).get('overrides',{})
        key=versions.get(item['normalized'],item['normalized'])
        if key not in groups:continue
        group=groups[key]
        group['images'].append({'key':'capture:'+item['session_id']+':'+item['capture_id'],
            'kind':'capture','session_id':item['session_id'],'capture_id':item['capture_id'],
            'name':item['name'],'url':item['preview']+('&' if '?' in item['preview'] else '?')+'size=240',
            'source':'ocr' if ratings[sid].get(item['capture_id'])==2 else item.get('source',''),'normalized':key,'sku':item['sku']})
    # 只选择当前款的实际拍摄图片；原表参考图片保留在独立参考列。
    for group in groups.values():
        products=[im for im in group['images'] if im['source']!='ocr']
        if products:group['images']=products
    selected=preferences(sku,pid)['images']
    for group in groups.values():
        saved=selected.get(group['normalized'],{}).get('key')
        group['selected_key']=saved if any(image['key']==saved for image in group['images']) else (group['images'][0]['key'] if group['images'] else '')
    return list(groups.values())


def gallery(sku,pid,code='',offset=0,limit=60):
    groups=gallery_items(sku,pid,code);key=normalize(code)
    images=[im for group in groups if not key or group['normalized']==key for im in group['images']]
    offset=max(0,int(offset));limit=max(1,min(120,int(limit)))
    return {'project_id':pid,'groups':[{k:v for k,v in group.items() if k!='images'}|{'count':len(group['images'])} for group in groups],
            'images':images[offset:offset+limit],'total':len(images),'offset':offset,'has_more':offset+limit<len(images)}


def select_images(sku,pid,choices,code=''):
    if not isinstance(choices,dict) or len(choices)>2000:raise ValueError('款号图片选择无效')
    with sku.lock:
        requested=normalize(code)
        if requested and any(normalize(key)!=requested for key in choices):raise ValueError('只能选择当前款号区间中的图片')
        groups={g['normalized']:g for g in gallery_items(sku,pid,code)}
        before=preferences(sku,pid);value=json.loads(json.dumps(before))
        for code,key in choices.items():
            code=normalize(code)
            group=groups.get(code);image=next((im for im in group['images'] if im['key']==key),None) if group else None
            if not image:raise ValueError('所选照片不再属于此款号，请重新打开组图选择')
            value['images'][code]={k:v for k,v in image.items() if k not in ('url','name','sku','normalized','source')}
        save_preferences(sku,pid,value);sku.dirty.add(pid)
        try:sku.export(pid)
        except Exception:
            save_preferences(sku,pid,before)
            raise
        return sku.state()


def _capture_cover_codes(sku,requested):
    """只检查已保存封面当前的资产归属；改号后旧封面不能串到其他 SKU。"""
    archive=getattr(sku,'archive',None)
    if not archive:return {}
    links={}
    with archive.connect() as c:
        for sid,ids in requested.items():
            for cid in ids:
                row=c.execute('SELECT a.id,a.normalized FROM catalog_links k JOIN assets a ON a.id=k.asset WHERE k.session=? AND k.capture=?',(sid,cid)).fetchone()
                if row:links[(sid,cid)]=dict(row)
    codes={key:{row['normalized']} for key,row in links.items()}
    with sku.connect() as c:
        for key,row in links.items():
            for version in c.execute('SELECT sku,base_sku FROM sku_shoot_versions WHERE asset=?',(row['id'],)):
                if normalize(version['sku'])==row['normalized']:codes[key].add(normalize(version['base_sku']))
    return codes


def decorate_selected(sku,rows):
    by_project={};sessions={};defaults={}
    requested={}
    for pid in {r['project'] for r in rows}:
        by_project[pid]=(preferences(sku,pid)['images'],sku.project(pid))
        for choice in by_project[pid][0].values():
            if choice['kind']=='capture':requested.setdefault(choice['session_id'],set()).add(choice['capture_id'])
    for sid,ids in requested.items():sessions[sid]={r['id']:r for r in sku.store.records(sid,ids=ids)}
    current_codes=_capture_cover_codes(sku,requested)
    for row in rows:
        if row['status']!='已拍摄':row['sku_image']=None;continue
        pid=row['project']
        chosen,project=by_project[pid];choice=chosen.get(row['normalized'])
        if choice and choice['kind']=='capture':
            key=(choice['session_id'],choice['capture_id'])
            if not sessions[key[0]].get(key[1]) or (key in current_codes and row['normalized'] not in current_codes[key]):choice=None
        if choice:
            if choice['kind']=='imported':
                row['sku_image']={'imported':True,'row_id':choice['row_id'],'index':choice['index'],'key':choice['key'],
                    'name':row['sku'],'url':f"/api/sku/image?row_id={choice['row_id']}&index={choice['index']}"}
            else:
                sid=choice['session_id'];cid=choice['capture_id']
                record=sessions[sid].get(cid)
                row['sku_image']={'session_id':sid,'capture_id':cid,'key':choice['key'],'name':record['name'] if record else row['sku'],
                    'url':f'/api/preview/{quote(sid)}/{quote(cid)}?size=240'+('&v='+quote(record.get('fingerprint','')) if record else '')}
        elif not row.get('sku_image'):
            sheet=next(s for s in project['metadata']['sheets'] if s['name']==row['sheet'])
            entries=[im for im in sheet.get('images',[]) if int(''.join(filter(str.isdigit,im['cell'])))==row['excel_row']]
            if entries:row['sku_image']={'imported':True,'row_id':row['id'],'index':0,'name':row['sku'],'url':f"/api/sku/image?row_id={row['id']}&index=0"}
        image=row.get('sku_image')
        if image:
            if image.get('imported'):image.setdefault('row_id',row['id'])
            key=(pid,row['normalized']);prior=defaults.get(key)
            if not prior or prior.get('imported') and not image.get('imported'):defaults[key]=image
    for row in rows:
        if row['status']=='已拍摄' and (row['normalized'] not in by_project[row['project']][0] or not row.get('sku_image')):row['sku_image']=defaults.get((row['project'],row['normalized']))
