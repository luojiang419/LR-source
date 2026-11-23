"""局域网拍摄资料的固定表投影；不传活动数据库，不接受远端 SQL/目标路径。"""
from contextlib import ExitStack
from datetime import datetime,timedelta,timezone
import sqlite3
import time
import threading
from functools import wraps
import hashlib
import copy
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


def preview_slice(original,limit=3):
    """旧版主机兼容：完整清单只取最新照片及其关联行，快速投影不确认游标。"""
    payload=copy.deepcopy(original);tables=payload['tables'];captures=tables['catalog']['captures'][-limit:];cids={r['id'] for r in captures}
    tables['catalog']['captures']=captures
    for session in tables['catalog']['sessions']:
        state=json.loads(session['state']);state['overrides']={k:v for k,v in state.get('overrides',{}).items() if k in cids};session['state']=stable_json(state)
    archive=tables['archive'];links=[r for r in archive.get('catalog_links',[]) if r['capture'] in cids];aids={r['asset'] for r in links}
    files=[r for r in archive.get('files',[]) if r['asset'] in aids];fids={r['id'] for r in files}
    bindings=[r for r in archive.get('bindings',[]) if r['asset'] in aids];streams={r['stream'] for r in bindings}
    for table,rows in archive.items():
        if table=='catalog_links':archive[table]=links
        elif table=='assets':archive[table]=[r for r in rows if r['id'] in aids]
        elif table=='files':archive[table]=files
        elif table=='streams':archive[table]=[r for r in rows if r['id'] in streams]
        elif table=='bindings':archive[table]=bindings
        elif table=='segments':
            archive[table]=[r for r in rows if r['label'] in aids or any(v in aids for v in json.loads(r['members']))]
            for r in archive[table]:r['members']=stable_json([v for v in json.loads(r['members']) if v in aids])
        else:archive[table]=[r for r in rows if r.get('asset') in aids or r.get('file') in fids]
    for table,rows in tables['sku'].items():
        if table=='projects':continue
        if table=='sku_rows':tables['sku'][table]=[]
        else:tables['sku'][table]=[r for r in rows if r.get('capture_id',r.get('capture')) in cids or r.get('asset') in aids]
    payload['documents']=[];payload['paths']={f['source']:f['relative'] for r in captures for f in r['sync_files']};payload.update(partial=True,seq=0)
    return payload


def batch_hashes(method):
    @wraps(method)
    def run(self,*args,**kwargs):
        with self.store.connect() as connection:
            self.hash_local.reader=connection;self.hash_local.pending=[]
            try:return method(self,*args,**kwargs)
            finally:
                pending=self.hash_local.pending
                del self.hash_local.reader;del self.hash_local.pending
                if pending:connection.executemany('INSERT OR REPLACE INTO lan_sync_hashes VALUES (?,?,?,?)',pending)
    return run


class SyncData:
    VIEW_KEYS=('filter','view','selectedId','query','sort','thumbSize','folderPrefix')
    def __init__(self,store,sku,archive):
        self.store=store;self.sku=sku;self.archive=archive
        self.services={'catalog':store,'sku':sku,'archive':archive}
        self.hash_cache={};self.hash_local=threading.local();self.probes=set();self.clock=lambda:datetime.now(timezone(timedelta(hours=8)))
        # 触发器与业务写入同事务提交；已有 worker_activity.Condition 负责提交后唤醒。
        # 修订只依赖业务表，ack、散列和同步日志自身的写入不会制造无限同步。
        self.archive.ratings
        for key,service in self.services.items():
            with service.connect() as c:
                c.execute('CREATE TABLE IF NOT EXISTS lan_sync_version(id INTEGER PRIMARY KEY,value INTEGER)')
                c.execute('INSERT OR IGNORE INTO lan_sync_version VALUES (1,0)')
                names=set(TABLES[key])|({'settings'} if key in ('catalog','sku') else {'shared_ratings'})
                for table in names:
                    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():continue
                    for event in ('INSERT','UPDATE','DELETE'):
                        c.execute('CREATE TRIGGER IF NOT EXISTS lan_sync_'+table+'_'+event+' AFTER '+event+' ON "'+table+'" BEGIN UPDATE lan_sync_version SET value=value+1 WHERE id=1; END')
        with store.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS lan_sync_receipts(device TEXT PRIMARY KEY,revision TEXT,root TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS lan_sync_rows(device TEXT,db TEXT,tab TEXT,pk TEXT,digest TEXT,PRIMARY KEY(device,db,tab,pk));
CREATE TABLE IF NOT EXISTS lan_sync_hashes(path TEXT PRIMARY KEY,size INTEGER,mtime_ns INTEGER,sha256 TEXT);
CREATE TABLE IF NOT EXISTS lan_sync_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,set_id TEXT,kind TEXT,ref TEXT,created REAL);
CREATE TABLE IF NOT EXISTS lan_sync_snapshot(id INTEGER PRIMARY KEY,payload TEXT);
CREATE TABLE IF NOT EXISTS lan_sync_cursors(device TEXT PRIMARY KEY,set_id TEXT,seq INTEGER);
CREATE TABLE IF NOT EXISTS lan_sync_receive_state(device TEXT PRIMARY KEY,state TEXT);
CREATE INDEX IF NOT EXISTS lan_sync_set_seq ON lan_sync_changes(set_id,seq);
''')
        store.lan_sync_data=self

    def revision(self):
        values=[]
        for service in self.services.values():
            with service.connect() as c:values.append(c.execute('SELECT value FROM lan_sync_version WHERE id=1').fetchone()[0])
        return tuple(values)

    def file_info(self,path):
        path=Path(path);before=path.stat();signature=(before.st_size,before.st_mtime_ns)
        cached=self.hash_cache.get(str(path))
        if cached and cached[0]==signature:sha=cached[1]
        else:
            reader=getattr(self.hash_local,'reader',None)
            if reader is not None:row=reader.execute('SELECT size,mtime_ns,sha256 FROM lan_sync_hashes WHERE path=?',(str(path),)).fetchone()
            else:
                with self.store.connect() as c:row=c.execute('SELECT size,mtime_ns,sha256 FROM lan_sync_hashes WHERE path=?',(str(path),)).fetchone()
            sha=row['sha256'] if row and (row['size'],row['mtime_ns'])==signature else digest(path)
        after=path.stat()
        if signature!=(after.st_size,after.st_mtime_ns):raise ValueError('现场文件仍在写入，稍后重试')
        if not cached or cached!=(signature,sha):
            values=(str(path),*signature,sha)
            pending=getattr(self.hash_local,'pending',None)
            if pending is not None:pending.append(values)
            else:
                with self.store.connect() as c:c.execute('INSERT OR REPLACE INTO lan_sync_hashes VALUES (?,?,?,?)',values)
        self.hash_cache[str(path)]=(signature,sha)
        return {'sha256':sha,'size':before.st_size}

    def prime_files(self,root,records):
        for record in records:
            if record.get('jpg'):
                try:self.file_info(safe_path(root,Path(record['jpg']).as_posix()))
                except (OSError,ValueError):pass  # 不阻断照片入库；接口按张报告错误并补算。

    def current_set(self,host_state,db):
        watch=host_state.get('watch') or {};sid=watch.get('session_id')
        if not watch.get('enabled') or not sid:return None
        row=db.execute("SELECT * FROM sessions WHERE id=? AND NOT EXISTS(SELECT 1 FROM settings WHERE key IN ('archive_session:'||sessions.id,'lan_sync_origin:'||sessions.id))",(sid,)).fetchone()
        if not row or json.loads(row['state']).get('sync_origin'):return None
        # 入库时间优先；旧数据没有入库字段时读取现有 shooting_captures.received，最后才用 EXIF。
        day_sql="date(CASE WHEN json_extract(c.data,'$.ingested_at') IS NOT NULL THEN json_extract(c.data,'$.ingested_at') WHEN t.received IS NOT NULL THEN datetime(t.received,'unixepoch') ELSE json_extract(c.data,'$.time') END, CASE WHEN json_extract(c.data,'$.ingested_at') IS NOT NULL OR t.received IS NOT NULL OR substr(json_extract(c.data,'$.time'),-1)='Z' OR instr(substr(json_extract(c.data,'$.time'),11),'+') THEN '+8 hours' ELSE '+0 hours' END)"
        join=' FROM captures c LEFT JOIN timeline.shooting_captures t ON t.session=c.session AND t.capture=c.id '
        today=self.clock().date().isoformat()
        # 过零点后第一张入库才切套；空监控套使用今天。
        day=db.execute('SELECT max('+day_sql+')'+join+'WHERE c.session=? AND '+day_sql+'<=?',(sid,today)).fetchone()[0] or today
        generation=watch.get('generation','')
        set_id=hashlib.sha256((sid+(':'+generation if generation else '')+':'+day).encode()).hexdigest()[:16]
        return {'id':set_id,'session_id':sid,'date':day,'folder':row['name'],'root':row['root'],'generation':generation,'_session':dict(row),'_day_sql':day_sql,'_join':join}

    @batch_hashes
    def snapshot_current(self,device,name,host_state,*,limit=None):
        tables={key:{table:[] for table in TABLES[key]} for key in TABLES};blobs={};paths={};probes=set();skipped=[]
        ids=lambda values:stable_json(sorted(values))
        # 独立只读 WAL 快照，不获取任何 Python 业务锁，文件散列在事务结束后做。
        with ExitStack() as stack:
            db={}
            for key,service in self.services.items():
                c=sqlite3.connect(service.db.resolve().as_uri()+'?mode=ro',uri=True,timeout=3);c.row_factory=sqlite3.Row
                stack.callback(c.close);db[key]=c
            db['catalog'].execute('ATTACH DATABASE ? AS timeline',(self.sku.db.resolve().as_uri()+'?mode=ro',))
            for c in db.values():c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
            current=self.current_set(host_state,db['catalog'])
            def rows(key,table,where='',args=()):
                if not db[key].execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():return []
                return [dict(r) for r in db[key].execute('SELECT * FROM "'+table+'"'+(' WHERE '+where if where else ''),args)]
            if current:
                sid=current['session_id'];session=current.pop('_session');day_sql=current.pop('_day_sql');join=current.pop('_join')
                capture_query='SELECT c.*'+join+'WHERE c.session=? AND '+day_sql+'=? ORDER BY c.position'+(' DESC LIMIT '+str(int(limit)) if limit else '')
                captures=[dict(r) for r in db['catalog'].execute(capture_query,(sid,current['date']))]
                if limit:current['count']=db['catalog'].execute('SELECT count(*)'+join+'WHERE c.session=? AND '+day_sql+'=?',(sid,current['date'])).fetchone()[0]
                cids={r['id'] for r in captures};sids={sid}
                state=json.loads(session['state']);state['overrides']={k:v for k,v in state.get('overrides',{}).items() if k in cids}
                session['state']=stable_json(state)
                tables['catalog'].update(sessions=[session],captures=captures)
                active=db['sku'].execute("SELECT value FROM settings WHERE key='active'").fetchone()
                projects=rows('sku','projects',"((id=? AND (session_id=? OR id IN (SELECT project FROM shooting_bindings WHERE session=?))) OR (json_extract(metadata,'$.automatic_day')=? AND session_id=?)) AND json_extract(metadata,'$.sync_origin') IS NULL",(active[0] if active else '',sid,sid,current['date'],sid))
                projects=[p for p in projects if not json.loads(p['metadata']).get('removed_at') and (not json.loads(p['metadata']).get('automatic_day') or json.loads(p['metadata'])['automatic_day']==current['date'])]
                pids={p['id'] for p in projects};tables['sku']['projects']=projects
                links=rows('archive','catalog_links','session=? AND capture IN (SELECT value FROM json_each(?))',(sid,ids(cids)))
                aids={r['asset'] for r in links};tables['archive']['catalog_links']=links
                for table in TABLES['sku']:
                    if table=='projects' or limit and table=='sku_rows':continue
                    names={r['name'] for r in db['sku'].execute('PRAGMA table_info("'+table+'")')}
                    if not names:continue
                    if 'asset' in names:where='asset IN (SELECT value FROM json_each(?))';args=(ids(aids),)
                    elif 'capture_id' in names or 'capture' in names:
                        sc='session_id' if 'session_id' in names else 'session';cc='capture_id' if 'capture_id' in names else 'capture'
                        where=sc+'=? AND '+cc+' IN (SELECT value FROM json_each(?))';args=(sid,ids(cids))
                    elif 'project' in names:where='project IN (SELECT value FROM json_each(?))';args=(ids(pids),)
                    else:continue
                    tables['sku'][table]=rows('sku',table,where,args)
                files=rows('archive','files','asset IN (SELECT value FROM json_each(?))',(ids(aids),));fids={r['id'] for r in files}
                streams=rows('archive','streams','session=? AND id IN (SELECT stream FROM bindings WHERE asset IN (SELECT value FROM json_each(?)))',(sid,ids(aids)))
                stream_ids={r['id'] for r in streams}
                for table in TABLES['archive']:
                    names={r['name'] for r in db['archive'].execute('PRAGMA table_info("'+table+'")')}
                    if table=='catalog_links':continue
                    if table=='assets':value=rows('archive',table,'id IN (SELECT value FROM json_each(?))',(ids(aids),))
                    elif table=='files':value=files
                    elif table=='streams':value=streams
                    else:
                        terms=[(col,v) for col,v in (('asset',aids),('file',fids),('stream',stream_ids)) if col in names]
                        value=rows('archive',table,' OR '.join(col+' IN (SELECT value FROM json_each(?))' for col,_ in terms),[ids(v) for _,v in terms]) if terms else []
                    if table=='bindings':value=[r for r in value if r['asset'] in aids]
                    if table=='segments':
                        value=[r for r in value if r['label'] in aids or any(v in aids or v in cids for v in json.loads(r['members']))]
                        for r in value:r['members']=stable_json([v for v in json.loads(r['members']) if v in aids or v in cids])
                    tables['archive'][table]=value
                # 只读取本套照片所关联的共享评级；不调用会懒初始化/获取归档锁的评级服务。
                from asset_catalog import pair_key
                for capture in captures:
                    record=json.loads(capture['data']);keys={pair_key(Path(session['root'])/record[k]) for k in ('jpg','raw') if record.get(k)}
                    keys.update(r['path_key'] for r in db['archive'].execute('SELECT path_key FROM rating_capture_paths WHERE session=? AND capture=?',(sid,capture['id'])))
                    ratings=list(db['archive'].execute('SELECT rating,updated FROM shared_ratings WHERE path_key IN (SELECT value FROM json_each(?)) ORDER BY updated DESC',(ids(keys),)))
                    capture['sync_rating']=ratings[0]['rating'] if ratings else state['overrides'].get(capture['id'],record.get('liveRating',record.get('existingRating',0)))
                    state['overrides'][capture['id']]=capture['sync_rating']
                session['state']=stable_json(state)
            else:projects=[];pids=set();captures=[]
        for capture in captures:
            record=json.loads(capture['data']);relative=record.get('jpg');capture['sync_files']=[]
            try:
                if not relative:raise ValueError('只有 RAW，尚无配对小档' if record.get('raw') else '没有小档文件')
                path=safe_path(current['root'],Path(relative).as_posix());probes.add(path)
                if path.suffix.lower() not in ('.jpg','.jpeg','.png','.tif','.tiff'):raise ValueError('小档格式不支持')
                info=self.file_info(path);dest=current['date']+'/拍摄-'+current['id']+'/'+Path(relative).as_posix()
                capture['sync_files'].append({**info,'relative':dest,'source':str(path),'kind':'jpg'});blobs[info['sha256']]=path;paths[str(path)]=dest
                for media_name in dict.fromkeys([relative,record.get('raw')]):
                    if not media_name:continue
                    media_path=safe_path(current['root'],Path(media_name).as_posix())
                    for side in (media_path.with_suffix('.xmp'),Path(str(media_path)+'.xmp')):
                        probes.add(side)
                        if side.is_file():
                            side_info=self.file_info(side);side_dest=str(Path(dest).with_name(side.name)).replace(chr(92),'/')
                            if not any(f['relative']==side_dest for f in capture['sync_files']):capture['sync_files'].append({**side_info,'relative':side_dest,'source':str(side),'kind':'xmp'})
                            blobs[side_info['sha256']]=side;paths[str(side)]=side_dest
            except (OSError,ValueError) as exc:
                reason='文件不存在或已移走' if isinstance(exc,FileNotFoundError) else str(exc)
                skipped.append({'capture':capture['id'],'name':record.get('name',relative),'reason':reason[:160]})
                capture['sync_files']=[]
        documents=[]
        for p in ([] if limit else projects):
            for filename in ('source.xlsx','拍摄对账.xlsx',current['date']+'.xlsx'):
                path=self.sku.root/p['id']/filename;probes.add(path)
                if path.is_file():
                    try:info=self.file_info(path)
                    except (OSError,ValueError):continue
                    blobs[info['sha256']]=path;documents.append({**info,'project':p['id'],'name':filename})
        if current:current.setdefault('count',len(captures))
        payload={'schema':1,'protocol':2,'device':device,'name':name,'set':current,'skipped':skipped,
                 'tables':tables,'documents':documents,'paths':paths,'host':host_state,
                 'active_session':current['session_id'] if current else '',
                 'active_project':active[0] if current and active and active[0] in pids else ''}
        # 稳定标识供增量投影解析外键；只传 ID，不重复发送历史业务行。
        payload['identities']=sorted({r['id'] for ts in tables.values() for rs in ts.values() for r in rs if isinstance(r.get('id'),str)}-{r['id'] for tab in ('assets','files') for r in tables['archive'][tab]})
        payload['revision']=hashlib.sha256(stable_json(payload).encode()).hexdigest();
        if limit is None:self.probes=probes
        return payload,blobs

    snapshot=snapshot_current

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
        if payload.get('protocol')==2:
            if type(payload.get('seq')) is not int or payload['seq']<0:raise ValueError('同步序号无效')
            current=payload.get('set')
            if current is not None and (not isinstance(current,dict) or not re.fullmatch(r'[0-9a-f]{16}',str(current.get('id',''))) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(current.get('date','')))):raise ValueError('当前套标识无效')
            if not isinstance(payload.get('identities',[]),list) or any(not isinstance(v,str) or not ID.fullmatch(v) for v in payload.get('identities',[])):raise ValueError('同步标识无效')
            for key,tables in payload.get('deleted',{}).items():
                if key not in TABLES or not isinstance(tables,dict) or set(tables)-set(TABLES[key]):raise ValueError('同步删除字段无效')
                for table,rows in tables.items():
                    if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):raise ValueError('同步删除字段无效')
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
        unavailable={r['id'] for r in payload['tables']['catalog']['captures'] if not any(f['kind']=='jpg' for f in r['sync_files'])}
        if unavailable:
            payload=copy.deepcopy(payload);archive=payload['tables']['archive']
            links=archive.get('catalog_links',[]);good={r['asset'] for r in links if r['capture'] not in unavailable}
            bad={r['asset'] for r in links if r['capture'] in unavailable}-good
            bad_files={r['id'] for r in archive.get('files',[]) if r['asset'] in bad}
            for table,rows in archive.items():
                archive[table]=[r for r in rows if r.get('capture') not in unavailable and r.get('asset') not in bad and r.get('file') not in bad_files and not (table=='assets' and r['id'] in bad or table=='files' and r['id'] in bad_files or table=='segments' and r.get('label') in bad)]
                if table=='segments':
                    for r in archive[table]:r['members']=stable_json([v for v in json.loads(r['members']) if v not in bad and v not in unavailable])
            for table,rows in payload['tables']['sku'].items():payload['tables']['sku'][table]=[r for r in rows if r.get('capture_id',r.get('capture')) not in unavailable and r.get('asset') not in bad]
            for session in payload['tables']['catalog']['sessions']:
                state=json.loads(session['state']);state['overrides']={k:v for k,v in state.get('overrides',{}).items() if k not in unavailable};session['state']=stable_json(state)
        ids={r['id'] for tables in payload['tables'].values() for rows in tables.values() for r in rows if isinstance(r.get('id'),str)}
        ids.update(payload.get('identities',[]))
        ids.update(r['id'] for r in payload['tables']['catalog']['captures'])
        # 永久资产/组件 ID 已嵌入原图和 XMP，保留它们才能继续按快门身份归档。
        permanent={r['id'] for table in ('assets','files') for r in payload['tables']['archive'].get(table,[])}
        scope=payload.get('set',{}).get('id','') if payload.get('set') else ''
        mapping={v:mapped_id(device,v+(':'+scope if scope else '')) for v in ids-permanent}
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
                    if payload.get('protocol')==2 and 'target_revision' not in payload and not payload.get('partial'):
                        local_sid=mapping.get(payload['active_session'],'')
                        for table,rows in tables.items():
                            if table in ('sessions','captures','projects','assets','files'):continue
                            cols=c.execute('PRAGMA table_info("'+table+'")').fetchall();names={r['name'] for r in cols};pk=[r['name'] for r in sorted(cols,key=lambda r:r['pk']) if r['pk']]
                            session_column='session_id' if 'session_id' in names else 'session' if 'session' in names else None
                            if not session_column or not local_sid:continue
                            incoming={stable_json([convert(r[k]) for k in pk]) for r in rows}
                            for old in c.execute('SELECT * FROM "'+table+'" WHERE "'+session_column+'"=?',(local_sid,)).fetchall():
                                if stable_json([old[k] for k in pk]) in incoming:continue
                                if table=='ocr_documents':c.execute('DELETE FROM ocr_document_search WHERE rowid=(SELECT rowid FROM ocr_documents WHERE id=?)',(old['id'],))
                                c.execute('DELETE FROM "'+table+'" WHERE '+' AND '.join('"'+k+'" IS ?' for k in pk),[old[k] for k in pk])
                    for table,removed in payload.get('deleted',{}).get(key,{}).items():
                        if table not in TABLES[key] or table in ('sessions','captures','projects','assets','files'):continue
                        pk=[r['name'] for r in sorted(c.execute('PRAGMA table_info("'+table+'")'),key=lambda r:r['pk']) if r['pk']]
                        for old in removed:
                            if table=='ocr_documents':c.execute('DELETE FROM ocr_document_search WHERE rowid=(SELECT rowid FROM ocr_documents WHERE id=?)',(convert(old['id']),))
                            if pk:c.execute('DELETE FROM "'+table+'" WHERE '+' AND '.join('"'+k+'" IS ?' for k in pk),[convert(old[k]) for k in pk])
                    for table,rows in tables.items():
                        cols=c.execute('PRAGMA table_info("'+table+'")').fetchall();pk=[r['name'] for r in sorted(cols,key=lambda r:r['pk']) if r['pk']]
                        if not pk:raise ValueError('同步表缺少主键')
                        for original in rows:
                            row={k:convert(v) for k,v in original.items() if k not in ('sync_files','sync_rating')}
                            if table=='sessions':
                                state=json.loads(row['state']);state['sync_origin']=device
                                local=c.execute('SELECT state FROM sessions WHERE id=?',(row['id'],)).fetchone()
                                local_state=json.loads(local['state']) if local else {}
                                if payload.get('partial'):state['overrides']={**local_state.get('overrides',{}),**state.get('overrides',{})}
                                for field in self.VIEW_KEYS:
                                    state.pop(field,None)
                                    if field in local_state:state[field]=local_state[field]
                                row.update(root=str(root),name=payload['name']+' · '+original['name']+(' · '+payload['set']['date'] if payload.get('set') else ''),state=stable_json(state))
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
            result={'records':changed,'session_id':mapping.get(payload['active_session'],''),'project_id':mapping.get(payload['active_project'],''),
                    'revision':payload.get('target_revision',payload['revision']),'host':payload['host'],'set':payload.get('set'),'seq':payload.get('seq',0),'skipped':payload.get('skipped',[]),'retry_files':bool(payload.get('retry_files'))}
            if not payload.get('partial'):
              with self.store.connect() as c:
                c.execute('INSERT OR REPLACE INTO lan_sync_receive_state VALUES (?,?)',(device,stable_json(result)))
                c.execute('INSERT OR REPLACE INTO lan_sync_receipts VALUES (?,?,?,?)',(device,payload.get('target_revision',payload['revision']),str(root),now()))
                if payload.get('protocol')==2:c.execute('INSERT OR REPLACE INTO lan_sync_cursors VALUES (?,?,?)',(device,scope,payload['seq']))
        index=getattr(self.sku,'_browse_index',None)
        if index:index.wake.set()
        return result
