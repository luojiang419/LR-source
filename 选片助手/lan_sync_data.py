"""局域网拍摄资料的固定表投影；不传活动数据库，不接受远端 SQL/目标路径。"""
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import uuid

from catalog_store import now,stable_json

TABLES={
    'catalog':('sessions','captures'),
    'sku':('projects','sku_rows','jobs','parse_jobs','ocr_documents','ocr_choices','shooting_captures','metadata_versions','sku_shoot_versions'),
    'archive':('assets','files','versions','locations','file_fingerprints','catalog_links','assignments','streams','bindings','segments'),
}
ID=re.compile(r'^[a-zA-Z0-9_.:-]{1,160}$')
DIGEST=re.compile(r'^[0-9a-f]{64}$')


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def safe_path(root,relative):
    if not isinstance(relative,str) or not relative or '\\' in relative or ':' in relative:raise ValueError('同步路径无效')
    parts=relative.split('/')
    if any(part in ('','.','..') or part.endswith((' ','.')) or any(ord(c)<32 for c in part) for part in parts):raise ValueError('同步路径无效')
    reserved={'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}
    if any(part.split('.')[0].upper() in reserved for part in parts):raise ValueError('同步路径包含系统保留名称')
    root=Path(root).absolute();target=root.joinpath(*parts)
    # 逐级拒绝 junction/symlink，不能通过现有父目录逃离存储根。
    for p in (root,*reversed(target.parents),target):
        if p.is_symlink() or getattr(p,'is_junction',lambda:False)():raise ValueError('同步目录不能包含链接')
    target.resolve().relative_to(root.resolve())
    return target


def mapped_id(device,value):
    return uuid.uuid5(uuid.NAMESPACE_URL,'framekeep-lan:'+device+':'+str(value)).hex


def folder_label(name):
    text=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',str(name)).strip(' .')[:60]
    return text or '客户端'


class SyncData:
    VIEW_KEYS=('filter','view','selectedId','query','sort','thumbSize','folderPrefix')
    def __init__(self,store,sku,archive):
        self.store=store;self.sku=sku;self.archive=archive
        self.services={'catalog':store,'sku':sku,'archive':archive}
        self.hash_cache={};self.probes=set()
        with store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS lan_sync_receipts(device TEXT PRIMARY KEY,revision TEXT,root TEXT,updated TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS lan_sync_rows(device TEXT,db TEXT,tab TEXT,pk TEXT,digest TEXT,PRIMARY KEY(device,db,tab,pk))')

    def revision(self):
        values=[]
        for service in self.services.values():
            for path in (service.db,Path(str(service.db)+'-wal')):
                try:s=path.stat();values.append((s.st_size,s.st_mtime_ns))
                except FileNotFoundError:values.append(None)
        return hashlib.sha256(stable_json(values).encode()).hexdigest()

    def file_info(self,path):
        before=path.stat();signature=(before.st_size,before.st_mtime_ns)
        cached=self.hash_cache.get(str(path))
        sha=cached[1] if cached and cached[0]==signature else digest(path)
        after=path.stat()
        if signature!=(after.st_size,after.st_mtime_ns):raise ValueError('现场文件仍在写入，稍后重试')
        self.hash_cache[str(path)]=(signature,sha)
        return {'sha256':sha,'size':before.st_size}

    def snapshot(self,device,name,host_state):
        tables={};blobs={};paths={};probes=set()
        # 快照来自固定业务表，后续投影幂等补偿跨库提交，不复制 WAL 文件。
        with self.sku.lock,self.archive.lock,self.store.lock,ExitStack() as stack:
            for key,service in self.services.items():
                c=stack.enter_context(service.connect());c.execute('BEGIN')
                tables[key]={table:[dict(row) for row in c.execute('SELECT * FROM "'+table+'"')]
                    for table in TABLES[key] if c.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(table,)).fetchone()}
            with self.store.connect() as c:active_session=c.execute("SELECT value FROM settings WHERE key='active'").fetchone()
            with self.sku.connect() as c:active_project=c.execute("SELECT value FROM settings WHERE key='active'").fetchone()
            with self.sku.connect() as c:
                capture_sessions={r[0] for r in c.execute('SELECT session FROM shooting_captures UNION SELECT session_id FROM projects UNION SELECT session FROM live_sessions') if r[0]}
            with self.store.connect() as c:
                capture_sessions.update(r[0].split(':',1)[1] for r in c.execute("SELECT key FROM settings WHERE key LIKE 'shooting_session:%'"))
                archive_sessions={r[0].split(':',1)[1] for r in c.execute("SELECT key FROM settings WHERE key LIKE 'archive_session:%'")}
                remote_sessions={r[0].split(':',1)[1] for r in c.execute("SELECT key FROM settings WHERE key LIKE 'lan_sync_origin:%'")}
        if active_session:capture_sessions.add(active_session[0])
        if host_state.get('watch',{}).get('session_id'):capture_sessions.add(host_state['watch']['session_id'])
        # 接收到的资料不再次广播，避免链式同步造成重复业务记录。
        sessions=[r for r in tables['catalog']['sessions'] if r['id'] in capture_sessions-archive_sessions-remote_sessions and not json.loads(r['state']).get('sync_origin')]
        sids={r['id'] for r in sessions};tables['catalog']['sessions']=sessions
        tables['catalog']['captures']=[r for r in tables['catalog']['captures'] if r['session'] in sids]
        projects=[r for r in tables['sku']['projects'] if not json.loads(r['metadata']).get('sync_origin')]
        pids={r['id'] for r in projects};tables['sku']['projects']=projects
        for table,rows in tables['sku'].items():
            if table not in ('projects','sku_shoot_versions'):tables['sku'][table]=[r for r in rows if
                (r.get('session_id',r.get('session')) in sids if 'session_id' in r or 'session' in r else r.get('project') in pids)]
        links=[r for r in tables['archive']['catalog_links'] if r['session'] in sids]
        aids={r['asset'] for r in links};tables['archive']['catalog_links']=links
        tables['sku']['sku_shoot_versions']=[r for r in tables['sku'].get('sku_shoot_versions',[]) if r['asset'] in aids]
        files=[r for r in tables['archive']['files'] if r['asset'] in aids];fids={r['id'] for r in files}
        streams=[r for r in tables['archive']['streams'] if r['session'] in sids];streams_ids={r['id'] for r in streams}
        for table,rows in tables['archive'].items():
            if table=='assets':tables['archive'][table]=[r for r in rows if r['id'] in aids]
            elif table=='files':tables['archive'][table]=files
            elif table=='streams':tables['archive'][table]=streams
            elif table!='catalog_links':tables['archive'][table]=[r for r in rows if r.get('asset') in aids or r.get('file') in fids or r.get('stream') in streams_ids]
        by_sid={s['id']:s for s in sessions}
        ratings={}
        for s in sessions:
            state=json.loads(s['state']);records=[json.loads(r['data']) for r in tables['catalog']['captures'] if r['session']==s['id']]
            shared=self.archive.ratings.session({**s,'state':state},records)['sharedRatings']
            state['overrides']={**state.get('overrides',{}),**shared};s['state']=stable_json(state)
            ratings[s['id']]=state['overrides']
        for capture in tables['catalog']['captures']:
            s=by_sid[capture['session']];record=json.loads(capture['data'])
            relative=record.get('jpg');files=[]
            if relative:
                path=(Path(s['root'])/relative)
                path.resolve().relative_to(Path(s['root']).resolve())
                if path.suffix.lower() not in ('.jpg','.jpeg','.png','.tif','.tiff'):raise ValueError('小档格式不支持')
                info=self.file_info(path);blobs[info['sha256']]=path
                probes.add(path)
                day=str(record.get('time') or '')[:10]
                try:datetime.strptime(day,'%Y-%m-%d')
                except ValueError:day='日期待确认'
                # 会话及原相对目录共同隔离相机同名文件，不改变原文件名。
                sid=hashlib.sha256(s['id'].encode()).hexdigest()[:12]
                rel=Path(relative).as_posix();safe_path(Path(s['root']),rel)
                dest=day+'/拍摄-'+sid+'/'+rel
                files.append({**info,'relative':dest,'source':str(path),'kind':'jpg'})
                paths[str(path)]=dest
                for media_name in dict.fromkeys([relative,record.get('raw')]):
                    if not media_name:continue
                    media_path=Path(s['root'])/media_name
                    media_path.resolve().relative_to(Path(s['root']).resolve())
                    for side in (media_path.with_suffix('.xmp'),Path(str(media_path)+'.xmp')):
                        probes.add(side)
                        if side.is_file():
                            safe_path(s['root'],side.relative_to(s['root']).as_posix())
                            side_info=self.file_info(side);blobs[side_info['sha256']]=side
                            side_dest=str(Path(dest).with_name(side.name)).replace('\\','/')
                            if not any(f['relative']==side_dest for f in files):files.append({**side_info,'relative':side_dest,'source':str(side),'kind':'xmp'})
                            paths[str(side)]=side_dest
            if record.get('raw') and not relative:
                # 没有现场 JPG 的 RAW 不冒充已经同步成功的小档。
                raise ValueError('现场照片只有 RAW，请先准备配对小档：'+str(record.get('name',record['id'])))
            capture['sync_files']=files
            capture['sync_rating']=ratings[capture['session']].get(record['id'],record.get('existingRating',0))
        documents=[]
        for p in projects:
            project_dir=self.sku.root/p['id']
            for filename in ('source.xlsx','拍摄对账.xlsx',str(json.loads(p['metadata']).get('automatic_day',''))+'.xlsx'):
                path=project_dir/filename
                probes.add(path)
                if path.is_file():
                    safe_path(project_dir,filename)
                    info=self.file_info(path);blobs[info['sha256']]=path;documents.append({**info,'project':p['id'],'name':filename})
        payload={'schema':1,'device':device,'name':name,
                 'tables':tables,'documents':documents,'paths':paths,'host':host_state,
                 'active_session':active_session[0] if active_session and active_session[0] in sids else '',
                 'active_project':active_project[0] if active_project and active_project[0] in pids else ''}
        payload['revision']=hashlib.sha256(stable_json(payload).encode()).hexdigest()
        self.probes=probes
        return payload,blobs

    def receipt(self,device):
        with self.store.connect() as c:r=c.execute('SELECT * FROM lan_sync_receipts WHERE device=?',(device,)).fetchone()
        return dict(r) if r else None

    def origin(self,sid):
        with self.store.connect() as c:r=c.execute('SELECT value FROM settings WHERE key=?',('lan_sync_origin:'+sid,)).fetchone()
        return r[0] if r else None

    def validate(self,payload,device):
        if payload.get('schema')!=1 or payload.get('device')!=device or not DIGEST.fullmatch(payload.get('revision','')):raise ValueError('客户端同步协议不匹配')
        if hashlib.sha256(stable_json({k:v for k,v in payload.items() if k!='revision'}).encode()).hexdigest()!=payload['revision']:raise ValueError('同步清单校验失败')
        if not isinstance(payload.get('name'),str) or not 0<len(payload['name'])<=80 or set(payload.get('tables',{}))!=set(TABLES):raise ValueError('同步清单结构无效')
        for key,tables in payload['tables'].items():
            if key not in TABLES or not isinstance(tables,dict) or set(tables)-set(TABLES[key]):raise ValueError('同步数据表无效')
            for table,rows in tables.items():
                if not isinstance(rows,list):raise ValueError('同步记录无效')
                with self.services[key].connect() as c:
                    columns={r['name'] for r in c.execute('PRAGMA table_info("'+table+'")')}
                for row in rows:
                    if not isinstance(row,dict) or set(row)-columns-({'sync_files','sync_rating'} if table=='captures' else set()):raise ValueError('同步字段不兼容')
                    if any(type(v) not in (str,int,float,type(None)) for k,v in row.items() if k!='sync_files'):raise ValueError('同步字段类型无效')
                    if table=='captures':
                        if type(row.get('sync_rating')) is not int or not -1<=row['sync_rating']<=5 or not isinstance(row.get('sync_files'),list):raise ValueError('同步照片字段无效')
                        for info in row['sync_files']:
                            safe_path(self.store.data,info['relative'])
                            if info.get('kind') not in ('jpg','xmp') or not DIGEST.fullmatch(info.get('sha256','')):raise ValueError('同步文件字段无效')
        for doc in payload['documents']:
            if not ID.fullmatch(doc['project']) or doc['name'] not in ('source.xlsx','拍摄对账.xlsx') and not re.fullmatch(r'\d{4}-\d{2}-\d{2}\.xlsx',doc['name']):raise ValueError('文档路径无效')

    def apply(self,payload,device,root):
        """文件完整校验后才发布业务记录；各库幂等更新，最后写收据用于崩溃续传。"""
        self.validate(payload,device)
        ids={r['id'] for tables in payload['tables'].values() for rows in tables.values() for r in rows if isinstance(r.get('id'),str)}
        ids.update(r['id'] for r in payload['tables']['catalog']['captures'])
        # 永久资产/组件 ID 已嵌入原图和 XMP，保留它们才能继续按快门身份归档。
        permanent={r['id'] for table in ('assets','files') for r in payload['tables']['archive'].get(table,[])}
        mapping={v:mapped_id(device,v) for v in ids-permanent}
        paths={p:str(safe_path(root,rel)) for p,rel in payload['paths'].items()}
        def rewrite(value):
            if isinstance(value,str):return mapping.get(value,paths.get(value,value))
            if isinstance(value,list):return [rewrite(v) for v in value]
            if isinstance(value,dict):return {mapping.get(k,k):rewrite(v) for k,v in value.items()}
            return value
        def convert(value):
            if not isinstance(value,str):return value
            if value[:1] in ('{','['):
                try:return stable_json(rewrite(json.loads(value)))
                except ValueError:pass
            return rewrite(value)
        changed=0
        with self.sku.lock,self.archive.lock,self.store.lock:
            for key,tables in payload['tables'].items():
                service=self.services[key]
                with service.connect() as c,self.store.connect() as receipts:
                    c.execute('BEGIN IMMEDIATE')
                    pending=[]
                    for table,rows in tables.items():
                        cols=c.execute('PRAGMA table_info("'+table+'")').fetchall();pk=[r['name'] for r in sorted(cols,key=lambda r:r['pk']) if r['pk']]
                        if not pk:raise ValueError('同步表缺少主键')
                        for original in rows:
                            row={k:convert(v) for k,v in original.items() if k not in ('sync_files','sync_rating')}
                            if table=='sessions':
                                state=json.loads(row['state']);state['sync_origin']=device
                                local=c.execute('SELECT state FROM sessions WHERE id=?',(row['id'],)).fetchone()
                                local_state=json.loads(local['state']) if local else {}
                                for field in self.VIEW_KEYS:
                                    state.pop(field,None)
                                    if field in local_state:state[field]=local_state[field]
                                row.update(root=str(root),name=payload['name']+' · '+original['name'],state=stable_json(state))
                                c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('lan_sync_origin:'+row['id'],device))
                                c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('shooting_session:'+row['id'],str(root)))
                            elif table=='captures':
                                record=json.loads(row['data']);jpg=next((f for f in original['sync_files'] if f['kind']=='jpg'),None)
                                if not jpg:continue
                                record.update(jpg=jpg['relative'],raw=None,existingRating=original['sync_rating'])
                                record['fingerprint']=hashlib.sha256(stable_json([(f['relative'],f['sha256']) for f in original['sync_files']]).encode()).hexdigest()
                                # 原始评级与元数据随 JPG 保持；本地路径只指向接收根。
                                row['data']=stable_json(record)
                            elif table=='projects':
                                meta=json.loads(row['metadata']);meta['sync_origin']=device
                                meta['source_sha256']=mapped_id(device,meta.get('source_sha256',original['id']))
                                meta['sync_day']=meta.pop('automatic_day',None)
                                row.update(enabled=0,metadata=stable_json(meta),name=payload['name']+' · '+original['name'])
                            elif table in ('jobs','parse_jobs') and row.get('status') in ('queued','running','retry','waiting_model'):
                                row.update(status='cancelled',reason='远端识别处理中，等待同步结果')
                            elif table=='streams':row['state']='closed';row['auto_write']=0
                            elif table=='locations':
                                if original['path'] not in paths:continue
                                row['path']=paths[original['path']]
                            elif table=='file_fingerprints':
                                if original['path'] not in paths:continue
                                row.update(path=paths[original['path']],folder=str(Path(paths[original['path']]).parent))
                            elif table=='assets':row['source']='lan:'+device
                            elif table=='sku_shoot_versions' and row['version']>0:
                                conflict=c.execute('SELECT asset FROM sku_shoot_versions WHERE normalized=? AND version=?',(row['normalized'],row['version'])).fetchone()
                                # 两台现场各有 -A 时保留各自真实 SKU 和母款关联，不改动原图款号。
                                if conflict and conflict['asset']!=row['asset']:row['version']=0
                            rowkey=stable_json([row[p] for p in pk]);sha=hashlib.sha256(stable_json(row).encode()).hexdigest()
                            old=receipts.execute('SELECT digest FROM lan_sync_rows WHERE device=? AND db=? AND tab=? AND pk=?',(device,key,table,rowkey)).fetchone()
                            if old and old[0]==sha:
                                current=c.execute('SELECT * FROM "'+table+'" WHERE '+' AND '.join('"'+k+'" IS ?' for k in pk),[row[k] for k in pk]).fetchone()
                                if current and all(current[k]==v for k,v in row.items()):continue
                            if table=='assets':
                                existing=c.execute('SELECT source FROM assets WHERE id=?',(row['id'],)).fetchone()
                                if existing and existing['source']!='lan:'+device:raise ValueError('永久资产身份已在本机使用，已停止同步，请核对来源')
                            fields=','.join('"'+k+'"' for k in row)
                            updates=','.join('"'+k+'"=excluded."'+k+'"' for k in row if k not in pk)
                            c.execute('INSERT INTO "'+table+'" ('+fields+') VALUES ('+','.join('?' for _ in row)+') ON CONFLICT('+','.join('"'+p+'"' for p in pk)+') DO UPDATE SET '+updates,list(row.values()))
                            if table=='ocr_documents':
                                rowid=c.execute('SELECT rowid FROM ocr_documents WHERE id=?',(row['id'],)).fetchone()[0]
                                c.execute('DELETE FROM ocr_document_search WHERE rowid=?',(rowid,))
                                c.execute('INSERT INTO ocr_document_search(rowid,searchable) VALUES (?,?)',(rowid,row['searchable']))
                            pending.append((device,key,table,rowkey,sha));changed+=1
                    # 每库一个事务，先提交业务，再登记散列。断电最多重放，不会漏投影。
                    c.commit()
                    receipts.executemany('INSERT OR REPLACE INTO lan_sync_rows VALUES (?,?,?,?,?)',pending)
            # 本地预览版本取实际接收内容；补上来源指纹到本地版本的桥，保留原作业有效性。
            with self.sku.connect() as c:
                for original in payload['tables']['catalog']['captures']:
                    if not original['sync_files']:continue
                    source=json.loads(original['data'])['fingerprint']
                    local=hashlib.sha256(stable_json([(f['relative'],f['sha256']) for f in original['sync_files']]).encode()).hexdigest()
                    if source!=local:c.execute('INSERT OR REPLACE INTO metadata_versions VALUES (?,?,?,?)',(mapping[original['session']],mapping[original['id']],source,local))
            # 共享评级表也是现有选片/归档读取来源，只更新数据库，不反向写接收到的文件。
            from asset_catalog import pair_key
            self.archive.ratings
            with self.archive.connect() as c:
                for original in payload['tables']['catalog']['captures']:
                    jpg=next((f for f in original['sync_files'] if f['kind']=='jpg'),None)
                    if not jpg:continue
                    path_key=pair_key(safe_path(root,jpg['relative']));rating=original['sync_rating']
                    if type(rating) is not int or not -1<=rating<=5:raise ValueError('同步星级无效')
                    c.execute('INSERT OR IGNORE INTO rating_capture_paths VALUES (?,?,?)',(mapping[original['session']],mapping[original['id']],path_key))
                    c.execute('INSERT INTO shared_ratings VALUES (?,?,1) ON CONFLICT(path_key) DO UPDATE SET rating=excluded.rating,updated=1 WHERE shared_ratings.rating IS NOT excluded.rating',(path_key,rating))
            with self.store.connect() as c:
                c.execute('INSERT OR REPLACE INTO lan_sync_receipts VALUES (?,?,?,?)',(device,payload['revision'],str(root),now()))
        index=getattr(self.sku,'_browse_index',None)
        if index:index.wake.set()
        return {'records':changed,'session_id':mapping.get(payload['active_session'],''),'project_id':mapping.get(payload['active_project'],''),
                'revision':payload['revision'],'host':payload['host']}
