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
CREATE TABLE IF NOT EXISTS library_renames(id TEXT PRIMARY KEY,old_key TEXT,new_sku TEXT,status TEXT,operations TEXT DEFAULT '[]',capture_count INTEGER DEFAULT 0,error TEXT DEFAULT '',created REAL,updated REAL);''')
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
    organization={'bins':bins,'total':len(active),'ungrouped':sum(not items.get(key,{}).get('bin') for key in active),'removed':len(removed),'renames':edits}
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


def rename_sku(sku,archive,old,new):
    if not archive:raise ValueError('元数据写入服务未就绪')
    old_key=normalize(_name(old,'原 SKU'));new=_name(new,'SKU')
    if len(new)>80 or not normalize(new):raise ValueError('请填写有效 SKU（最多 80 字符）')
    new_key=normalize(new);initialize(sku)
    with sku.lock,archive.lock:
        inventory=_inventory(sku);rows=[r for r in inventory['rows'] if r['normalized']==old_key]
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
            aid=archive.ensure(row['session_id'],row['capture_id'])
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
                preview=archive.preview(None,None,new,scope,project,asset_ids=ids,overwrite=True,source=source,segment=segment)
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
