"""全局资产索引检索；不读取原片，不把计数/文件探测放在首屏路径。"""
import base64
import hashlib
import json
import math
import sqlite3
import time

from sku_match import normalize


def install(c):
    c.executescript('''
CREATE INDEX IF NOT EXISTS files_asset ON files(asset);
CREATE INDEX IF NOT EXISTS locations_file ON locations(file);
CREATE INDEX IF NOT EXISTS locations_path_nocase ON locations(path COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS links_asset ON catalog_links(asset,session,capture);
CREATE INDEX IF NOT EXISTS assets_search ON assets(normalized DESC,created DESC,id DESC);
CREATE INDEX IF NOT EXISTS assets_scope_search ON assets(scope,normalized DESC,created DESC,id DESC);
CREATE INDEX IF NOT EXISTS assets_project_search ON assets(project,normalized DESC,created DESC,id DESC);
CREATE INDEX IF NOT EXISTS assets_project_scope_search ON assets(project,scope,normalized DESC,created DESC,id DESC);
CREATE INDEX IF NOT EXISTS metadata_queue ON metadata_jobs(status,created);
CREATE INDEX IF NOT EXISTS metadata_asset ON metadata_jobs(asset,created DESC);
CREATE INDEX IF NOT EXISTS metadata_operation ON metadata_jobs(operation);
CREATE INDEX IF NOT EXISTS bindings_pending ON bindings(stream,state,seq);
CREATE INDEX IF NOT EXISTS bindings_late ON bindings(state,seq);
CREATE INDEX IF NOT EXISTS streams_created ON streams(created DESC);
CREATE INDEX IF NOT EXISTS segments_created ON segments(created DESC);
CREATE INDEX IF NOT EXISTS operations_created ON operations(created DESC);
''')


def hydrate(c, rows):
    if not rows:return rows
    ids=[r['id'] for r in rows];marks=','.join('?' for _ in ids)
    by_id={r['id']:r for r in rows}
    for r in rows:r.update(locations=[],preview=None)
    # 关联批量读取；极端多副本资产不允许撑爆一次响应。
    locations=c.execute(f'''SELECT f.asset,l.path,l.status FROM files f
        JOIN locations l ON l.file=f.id WHERE f.asset IN ({marks}) LIMIT 2001''',ids).fetchall()
    for row in locations[:2000]:by_id[row['asset']]['locations'].append({'path':row['path'],'status':row['status']})
    for r in rows:r['locations_truncated']=len(locations)>2000
    for row in c.execute(f'''SELECT asset,session,capture FROM catalog_links
        WHERE asset IN ({marks}) GROUP BY asset''',ids):
        by_id[row['asset']]['preview']=f"/api/preview/{row['session']}/{row['capture']}?size=480"
    return rows


def search(archive,sku='',scope='',project='',mode='prefix',cursor='',limit=60):
    started=time.perf_counter()
    if mode not in ('exact','prefix'):raise ValueError('请选择 SKU 精确或前缀检索')
    if any(not isinstance(x,str) or len(x)>200 for x in (sku,scope,project)):raise ValueError('检索条件过长')
    if sku.strip() and not normalize(sku):raise ValueError('款号需包含字母或数字')
    sku=normalize(sku);limit=max(1,min(100,int(limit)))
    signature=hashlib.sha256(json.dumps([sku,scope,project,mode],ensure_ascii=False).encode()).hexdigest()[:24]
    clauses=[];args=[]
    for field,value in [('project',project),('scope',scope)]:
        if value:clauses.append(field+'=?');args.append(value)
    if sku:
        if mode=='exact':clauses.append('normalized=?');args.append(sku)
        else:
            # 沿用款号规范化（ASCII 字母数字），末字符进位构成前缀区间。
            upper=sku[:-1]+chr(ord(sku[-1])+1)
            clauses.append('normalized>=? AND normalized<?');args.extend([sku,upper])
    if cursor:
        try:
            if len(cursor)>1800:raise ValueError()
            token=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
            key=token['key']
            if token['q']!=signature or len(key)!=3 or not isinstance(key[0],str) or not isinstance(key[2],str) or not math.isfinite(float(key[1])):raise ValueError()
        except (ValueError,TypeError,KeyError,OverflowError):raise ValueError('分页游标无效或检索条件已变化') from None
        clauses.append('(normalized,created,id)<(?,?,?)');args.extend(key)
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    with archive.connect() as c:
        # 错误计划或异常大过滤也有预算，不让请求长期占用引擎。
        deadline=time.monotonic()+1
        c.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
        try:
            rows=[dict(r) for r in c.execute('SELECT * FROM assets'+where+' ORDER BY normalized DESC,created DESC,id DESC LIMIT ?',args+[limit+1])]
            more=len(rows)>limit;rows=rows[:limit]
            hydrate(c,rows)
        except sqlite3.OperationalError as exc:
            if 'interrupt' in str(exc):raise ValueError('检索超时，请缩小 SKU 或选择项目/拍摄类型') from None
            raise
    next_cursor=''
    if more and rows:
        last=rows[-1];next_cursor=base64.urlsafe_b64encode(json.dumps({'q':signature,'key':[last['normalized'],last['created'],last['id']]},ensure_ascii=False).encode()).decode().rstrip('=')
    return {'rows':rows,'has_more':more,'next_cursor':next_cursor,'total':None,'order':'sku_desc,created_desc,id_desc','elapsed_ms':round((time.perf_counter()-started)*1000,2)}


def known_paths(archive,paths):
    """索引命中仅表示此前备案路径，不能证明当前位置内容没有变。"""
    if not paths:return {}
    with archive.connect() as c:
        marks=','.join('?' for _ in paths)
        rows=c.execute(f'''SELECT l.path,l.status,a.id,a.sku,a.scope,a.project FROM locations l
            JOIN files f ON f.id=l.file JOIN assets a ON a.id=f.asset
            WHERE l.path COLLATE NOCASE IN ({marks})''',paths)
        return {r['path'].casefold():dict(r) for r in rows}


def local_paths(archive,text,offset=0,limit=60):
    value='%'+text.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    with archive.connect() as c:
        deadline=time.monotonic()+.25
        c.set_progress_handler(lambda:int(time.monotonic()>deadline),5000)
        try:
            rows=[r[0] for r in c.execute("SELECT path FROM locations WHERE path LIKE ? ESCAPE '\\' ORDER BY path LIMIT ? OFFSET ?",(value,limit+1,offset))]
        except sqlite3.OperationalError as exc:
            if 'interrupt' in str(exc):return {'paths':[],'has_more':False,'incomplete':True,'message':'已备案路径扫描达到时间预算，请缩小条件或启动 Everything'}
            raise
    return {'paths':rows[:limit],'has_more':len(rows)>limit,'incomplete':False}
