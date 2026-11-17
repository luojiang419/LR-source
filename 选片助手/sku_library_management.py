"""SKU 媒体池的虚拟文件夹、可恢复移除与整款元数据改名。"""
import json
import time
import uuid

from sku_match import normalize


def initialize(sku):
    if getattr(sku,'_library_schema_ready',False):return
    with sku.lock,sku.connect() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS library_bins(id TEXT PRIMARY KEY,name TEXT NOT NULL,parent TEXT NOT NULL DEFAULT '',created REAL,updated REAL);
CREATE UNIQUE INDEX IF NOT EXISTS library_bin_name ON library_bins(parent,name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS library_items(normalized TEXT PRIMARY KEY,bin TEXT NOT NULL DEFAULT '',removed INTEGER NOT NULL DEFAULT 0,updated REAL);
CREATE TABLE IF NOT EXISTS library_renames(id TEXT PRIMARY KEY,old_key TEXT,new_sku TEXT,status TEXT,operations TEXT DEFAULT '[]',capture_count INTEGER DEFAULT 0,error TEXT DEFAULT '',created REAL,updated REAL);
CREATE TABLE IF NOT EXISTS library_corrections(id TEXT PRIMARY KEY,new_sku TEXT,plan TEXT,status TEXT,operations TEXT DEFAULT '[]',capture_count INTEGER,error TEXT DEFAULT '',created REAL,updated REAL);''')
        sku._library_schema_ready=True


def descendants(bins,parent):
    result={parent}
    for _ in range(len(bins)+1):
        added={b['id'] for b in bins if b['parent'] in result}
        if added<=result:break
        result|=added
    return result


def organize(sku,rows,bin_id='',include_removed=False):
    initialize(sku)
    with sku.connect() as c:
        bins=[dict(r) for r in c.execute('SELECT * FROM library_bins ORDER BY name COLLATE NOCASE,id')]
        items={r['normalized']:dict(r) for r in c.execute('SELECT * FROM library_items')}
        edits=[dict(r) for r in c.execute('SELECT id,old_key,new_sku,status,operations,capture_count,error,updated FROM library_renames ORDER BY created DESC LIMIT 5')]
        corrections=[dict(r) for r in c.execute('SELECT id,new_sku,status,operations,capture_count,error,updated FROM library_corrections ORDER BY created DESC LIMIT 5')]
    inventory={r['normalized'] for r in rows if r['normalized']}
    active={key for key in inventory if not items.get(key,{}).get('removed')}
    removed=inventory-active
    for folder in bins:
        tree=descendants(bins,folder['id'])
        folder['count']=sum(items.get(key,{}).get('bin','') in tree for key in active)
    allowed=None if not bin_id or bin_id in ('ungrouped','trash') else descendants(bins,bin_id)
    if allowed is not None and not any(b['id']==bin_id for b in bins):raise ValueError('该组已不存在，请重新选择')
    def visible(row):
        key=row['normalized'];item=items.get(key,{})
        if bin_id=='trash':return bool(key and item.get('removed'))
        if not include_removed and item.get('removed'):return False
        if bin_id=='ungrouped':return bool(key and not item.get('bin'))
        return allowed is None or item.get('bin','') in allowed
    organization={'bins':bins,'total':len(active),'ungrouped':sum(not items.get(key,{}).get('bin') for key in active),'removed':len(removed),'renames':edits,'corrections':corrections}
    return [row for row in rows if visible(row)],organization


def _name(value,label='名称'):
    if not isinstance(value,str) or not value.strip() or len(value)>100 or any(ord(ch)<32 for ch in value):raise ValueError(f'请填写有效{label}（最多 100 字符）')
    return value.strip()


def _inventory(sku):
    from sku_library import browse
    return browse(sku,include_removed=True,all_rows=True,management=False)


def manage(sku,archive,body):
    initialize(sku);action=body.get('action');stamp=time.time()
    if action=='rename':return rename_sku(sku,archive,body.get('sku'),body.get('new_sku'))
    if action=='correct_selection':return correct_selection(sku,archive,body)
    with sku.lock,sku.connect() as c:
        bins=[dict(r) for r in c.execute('SELECT * FROM library_bins')]
        folder=body.get('bin_id','')
        if action in ('create_bin','rename_bin'):
            name=_name(body.get('name'),'组名称')
            parent=body.get('parent_id','')
            if action=='rename_bin':
                current=next((b for b in bins if b['id']==folder),None)
                if not current:raise ValueError('该组已不存在')
                parent=current['parent']
            elif parent and not any(b['id']==parent for b in bins):raise ValueError('上级组已不存在')
            if any(b['parent']==parent and b['name'].casefold()==name.casefold() and b['id']!=folder for b in bins):raise ValueError('该位置已有同名组')
            if action=='create_bin':
                folder=uuid.uuid4().hex;c.execute('INSERT INTO library_bins VALUES (?,?,?,?,?)',(folder,name,parent,stamp,stamp))
            else:c.execute('UPDATE library_bins SET name=?,updated=? WHERE id=?',(name,stamp,folder))
            return {'bin_id':folder,'name':name}
        if action=='delete_bin':
            current=next((b for b in bins if b['id']==folder),None)
            if not current:raise ValueError('该组已不存在')
            for target in descendants(bins,folder):
                c.execute('UPDATE library_items SET bin=?,updated=? WHERE bin=?',(current['parent'],stamp,target));c.execute('DELETE FROM library_bins WHERE id=?',(target,))
            return {'bin_id':current['parent']}
        key=normalize(_name(body.get('sku'),'SKU'))
        inventory=_inventory(sku)
        if not any(row['normalized']==key for row in inventory['groups']):raise ValueError('该 SKU 已不存在，请刷新')
        item=c.execute('SELECT * FROM library_items WHERE normalized=?',(key,)).fetchone()
        assigned=item['bin'] if item else '';removed=item['removed'] if item else 0
        if action=='move':
            if folder and not any(b['id']==folder for b in bins):raise ValueError('目标组已不存在')
            if removed:raise ValueError('请先恢复已移除的 SKU')
            assigned=folder
        elif action in ('remove','restore'):removed=int(action=='remove')
        else:raise ValueError('无效的 SKU 管理操作')
        c.execute('INSERT INTO library_items VALUES (?,?,?,?) ON CONFLICT(normalized) DO UPDATE SET bin=excluded.bin,removed=excluded.removed,updated=excluded.updated',(key,assigned,removed,stamp))
        return {'sku':key,'bin_id':assigned,'removed':bool(removed)}


def correct_selection(sku,archive,body):
    """明确图片范围的人工修正，不改 OCR 标签或整段边界；请求日志支持中断重试。"""
    if not archive:raise ValueError('元数据写入服务未就绪')
    request=body.get('request_id')
    if not isinstance(request,str) or not _valid_request_id(request):raise ValueError('修正请求标识无效，请重新打开修正窗口')
    initialize(sku)
    with sku.lock,archive.lock:
        with sku.connect() as c:prior=c.execute('SELECT * FROM library_corrections WHERE id=?',(request,)).fetchone()
        if prior:
            new=prior['new_sku'];plan=json.loads(prior['plan']);total=prior['capture_count']
            if body.get('new_sku',new)!=new:raise ValueError('此修正请求的款号已提交，请重新打开窗口修改')
        else:
            new=_name(body.get('new_sku'),'SKU')
            if len(new)>80 or not normalize(new):raise ValueError('请填写有效 SKU（最多 80 字符）')
            selected=body.get('items')
            if not isinstance(selected,list) or not 1<=len(selected)<=2000:raise ValueError('请选择 1–2000 张照片')
            if any(not isinstance(item,dict) or not isinstance(item.get('id'),str) or not isinstance(item.get('sku'),str) for item in selected):raise ValueError('所选照片无效，请刷新后重新选择')
            expected={item['id']:item['sku'] for item in selected}
            inventory=_inventory(sku);rows={r['id']:r for r in inventory['rows'] if r['id'] in expected}
            if len(rows)!=len(expected) or any(rows[key]['sku']!=value for key,value in expected.items()):raise ValueError('所选照片或款号已变化，请刷新后重新选择')
            buckets={};seen=set()
            for row in rows.values():
                from sku_library_catalog import ensure_asset
                aid=ensure_asset(archive,row)
                if aid in seen:continue
                seen.add(aid);asset=archive.asset(aid)
                buckets.setdefault((asset['scope'] or '现场记录',asset['project']),[]).append({'id':aid,'revision':asset['revision']})
            # 全部分组先生成写入预览，再登记可恢复请求并提交。
            plan=[]
            for (scope,project),assets in buckets.items():
                preview=archive.preview(None,None,new,scope,project,asset_ids=[a['id'] for a in assets],overwrite=True)
                plan.append({'scope':scope,'project':project,'assets':assets,'preview':preview['preview_id']})
            total=len(rows)
            with sku.connect() as c:
                source_keys={r['normalized'] for r in rows.values()}
                items={r['normalized']:r['bin'] for r in c.execute('SELECT normalized,bin FROM library_items')}
                bins={items.get(key,'') for key in source_keys}
                c.execute('INSERT INTO library_items VALUES (?,?,0,?) ON CONFLICT(normalized) DO UPDATE SET removed=0,updated=excluded.updated',(normalize(new),next(iter(bins)) if len(bins)==1 else '',time.time()))
                c.execute('INSERT INTO library_corrections(id,new_sku,plan,status,capture_count,created,updated) VALUES (?,?,?,?,?,?,?)',(request,new,json.dumps(plan),'running',total,time.time(),time.time()))
        operations=[]
        try:
            for index,bucket in enumerate(plan):
                key='library-correction:'+request+':'+str(index)
                with archive.connect() as c:done=c.execute('SELECT id FROM operations WHERE source=?',(key,)).fetchone()
                if done:operations.append(done['id']);continue
                if any(archive.asset(a['id'])['revision']!=a['revision'] for a in bucket['assets']):raise ValueError('照片归属版本已变化，请刷新后重新选择未修正的图片')
                preview_id=bucket['preview'] if not prior else archive.preview(None,None,new,bucket['scope'],bucket['project'],asset_ids=[a['id'] for a in bucket['assets']],overwrite=True)['preview_id']
                operation=archive.apply(preview_id,request_key=key)['operation']['id'];operations.append(operation)
                with sku.connect() as c:c.execute('UPDATE library_corrections SET operations=?,updated=? WHERE id=?',(json.dumps(operations),time.time(),request))
            with sku.connect() as c:c.execute("UPDATE library_corrections SET status='queued',operations=?,error='',updated=? WHERE id=?",(json.dumps(operations),time.time(),request))
        except Exception as exc:
            with sku.connect() as c:c.execute("UPDATE library_corrections SET status='failed',operations=?,error=?,updated=? WHERE id=?",(json.dumps(operations),str(exc)[:350],time.time(),request))
            raise
        with sku.connect() as c:target=c.execute('SELECT bin FROM library_items WHERE normalized=?',(normalize(new),)).fetchone()
        return {'sku':new,'normalized':normalize(new),'capture_count':total,'operation_ids':operations,'correction_id':request,'bin_id':target['bin'] if target else ''}


def _valid_request_id(value):
    return 1<=len(value)<=80 and all(ch.isascii() and (ch.isalnum() or ch in '-_') for ch in value)


def rename_sku(sku,archive,old,new,*,defer_io=False):
    if not archive:raise ValueError('元数据写入服务未就绪')
    old_key=normalize(_name(old,'原 SKU'));new=_name(new,'SKU')
    if len(new)>80 or not normalize(new):raise ValueError('请填写有效 SKU（最多 80 字符）')
    new_key=normalize(new);initialize(sku)
    with sku.lock,archive.lock:
        if defer_io:
            with archive.connect() as c:
                rows=[dict(r) for r in c.execute('SELECT a.sku,a.normalized,k.session AS session_id,k.capture AS capture_id,k.asset AS asset_id FROM assets a JOIN catalog_links k ON k.asset=a.id WHERE a.normalized=?',(old_key,))]
                groups=[dict(r) for r in c.execute("SELECT DISTINCT normalized FROM assets WHERE sku!=''")]
            inventory={'rows':rows,'groups':groups}
        else:inventory=_inventory(sku);rows=[r for r in inventory['rows'] if r['normalized']==old_key]
        with sku.connect() as c:
            retry=c.execute("SELECT * FROM library_renames WHERE old_key=? AND new_sku=? AND status IN ('running','failed') ORDER BY created DESC LIMIT 1",(old_key,new)).fetchone()
            prior=c.execute("SELECT * FROM library_renames WHERE old_key=? AND new_sku=? AND status='queued' ORDER BY created DESC LIMIT 1",(old_key,new)).fetchone()
        if not rows and not retry:
            if prior:return {'sku':new,'normalized':new_key,'capture_count':prior['capture_count'],'operation_ids':json.loads(prior['operations']),'rename_id':prior['id']}
            raise ValueError('原 SKU 已变化，请刷新后重试')
        if rows and not retry and all(row['sku']==new for row in rows):return {'sku':new,'normalized':new_key,'capture_count':len(rows),'operation_ids':[],'rename_id':'','unchanged':True}
        if old_key!=new_key and not retry and any(r['normalized']==new_key for r in inventory['groups']):raise ValueError('目标 SKU 已存在，请使用其他款号；不会自动合并两款')
        buckets={};seen=set()
        for row in rows:
            from sku_library_catalog import ensure_asset
            aid=row['asset_id'] if defer_io else ensure_asset(archive,row)
            if aid in seen:continue
            seen.add(aid);asset=archive.asset(aid)
            source='segment' if asset['source']=='segment' and asset['segment'] else 'manual'
            key=(asset['scope'] or '现场记录',asset['project'],asset['segment'],source)
            buckets.setdefault(key,[]).append(aid)
        edit_id=retry['id'] if retry else uuid.uuid4().hex
        operations=json.loads(retry['operations']) if retry else []
        total=retry['capture_count'] if retry else len(rows)
        with sku.connect() as c:
            c.execute('INSERT INTO library_renames(id,old_key,new_sku,status,operations,capture_count,created,updated) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,error=\'\',updated=excluded.updated',(edit_id,old_key,new,'running',json.dumps(operations),total,time.time(),time.time()))
        try:
            segments=[]
            for oid in operations:
                segment=archive.operation(oid)['operation']['source']
                if segment:segments.append((segment,oid))
            for (scope,project,segment,source),ids in buckets.items():
                preview=archive.preview(None,None,new,scope,project,asset_ids=ids,overwrite=True,source=source,segment=segment,defer_io=defer_io)
                operation=archive.apply(preview['preview_id'])['operation']['id'];operations.append(operation)
                if segment:segments.append((segment,operation))
                with sku.connect() as c:c.execute('UPDATE library_renames SET operations=?,updated=? WHERE id=?',(json.dumps(operations),time.time(),edit_id))
            with archive.connect() as c:
                for segment,operation in segments:c.execute("UPDATE segments SET sku=?,operation=?,status='confirmed',reason='' WHERE id=?",(new,operation,segment))
            with sku.connect() as c:
                for choice in c.execute('SELECT session_id,capture_id,sku FROM ocr_choices').fetchall():
                    if normalize(choice['sku'])==old_key:c.execute('UPDATE ocr_choices SET sku=?,updated=? WHERE session_id=? AND capture_id=?',(new,sku.clock(),choice['session_id'],choice['capture_id']))
                item=c.execute('SELECT * FROM library_items WHERE normalized=?',(old_key,)).fetchone()
                c.execute('DELETE FROM library_items WHERE normalized=?',(old_key,))
                c.execute('INSERT INTO library_items VALUES (?,?,?,?) ON CONFLICT(normalized) DO UPDATE SET bin=excluded.bin,removed=excluded.removed,updated=excluded.updated',(new_key,item['bin'] if item else '',item['removed'] if item else 0,time.time()))
                c.execute("UPDATE library_renames SET status='queued',updated=? WHERE id=?",(time.time(),edit_id))
        except Exception as exc:
            with sku.connect() as c:c.execute("UPDATE library_renames SET status='failed',error=?,updated=? WHERE id=?",(str(exc)[:350],time.time(),edit_id))
            raise
        return {'sku':new,'normalized':new_key,'capture_count':total,'operation_ids':operations,'rename_id':edit_id}
