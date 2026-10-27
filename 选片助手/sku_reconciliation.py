"""对账款号首张商品图，复用拍摄区间及本机预览缓存。"""
from urllib.parse import quote

import media_engine as media
from sku_match import normalize


def decorate_images(sku,rows):
    from sku_library import interval_rows
    sessions={};first={};first_sku={}
    with sku.connect() as c:chosen={(r['session_id'],r['capture_id']):r['sku'] for r in c.execute('SELECT session_id,capture_id,sku FROM ocr_choices')}
    for sid in {r['completion']['session_id'] for r in rows if r.get('completion')}:
        projected,_=interval_rows(sku,sid)
        for image in projected:
            if image.get('interval_id') and image['source']=='interval':
                first.setdefault((image['interval_id'],image['normalized']),image['capture_id'])
                from sku_library import _natural
                key=(sid,image['normalized']);prior=first_sku.get(key)
                if prior is None or _natural(image['name'])<_natural(prior['name']):first_sku[key]=image
        sessions[sid]={r['id']:r for r in sku.store.records(sid)}
    project=None
    for row in rows:
        row['sku_image']=None;evidence=row.get('completion')
        if row['status']!='已拍摄':continue
        if evidence:
            sid=evidence['session_id'];label=evidence['capture_id']
            row['shoot_sku']=chosen.get((sid,label),row['sku'])
            code=normalize(row['shoot_sku']);candidate=first_sku.get((sid,code))
            cid=candidate['capture_id'] if candidate else first.get((sid+':'+label,code),label)
            record=sessions[sid].get(cid)
            if record:
                row['sku_image']={'session_id':sid,'capture_id':cid,'name':record['name'],'url':f'/api/preview/{quote(sid)}/{quote(cid)}?size=240&v={quote(record.get("fingerprint",""))}'}
        else:
            project=project or sku.project(row['project'])
            sheet=next((s for s in project['metadata']['sheets'] if s['name']==row['sheet']),{})
            column=sheet.get('image_col')
            if column:
                from sku_workbook import col_number
                indexed=[i for i in sheet.get('images',[]) if int(''.join(filter(str.isdigit,i['cell'])))==row['excel_row']]
                found=next((index for index,image in enumerate(indexed) if col_number(image['cell'])==column),None)
                if found is not None:row['sku_image']={'url':f'/api/sku/image?row_id={row["id"]}&index={found}','imported':True,'index':found,'name':row['sku']}
    from sku_presentation import decorate_selected
    decorate_selected(sku,rows)


def export_images(sku,rows):
    result={};sessions={};cache={}
    for row in rows:
        image=row.get('sku_image')
        if not image:continue
        key=(row['sheet'],row['excel_row'])
        try:
            if image.get('imported'):data=sku.image(image.get('row_id',row['id']),image['index'])[0]
            else:
                sid=image['session_id'];cid=image['capture_id'];identity=(sid,cid)
                if sid not in sessions:sessions[sid]=(sku.store.session(sid),{r['id']:r for r in sku.store.records(sid)})
                session,records=sessions[sid]
                if identity not in cache:cache[identity]=media.preview_bytes(records[cid],session['root'],sku.store.data/'previews',lambda:media.find_exiftool(sku.store.root),240,False)
                data=cache[identity]
            result[key]=data
        except (OSError,ValueError,RuntimeError,KeyError):result[key]=None
    return result
