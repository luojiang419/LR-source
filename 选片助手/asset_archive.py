"""永久资产、拍摄结束区间与可恢复文件元数据写入队列。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

import media_engine as media
import asset_search
from metadata_writer import MetadataWriter,sha
from sku_match import normalize
from sku_intervals import partition

def uid():return uuid.uuid4().hex
def dumps(v):return json.dumps(v,ensure_ascii=False,sort_keys=True)

class AssetArchive:
    def __init__(self,store,*,autostart=True):
        self.store=store;self.db=store.data/'asset_archive.sqlite3';self.writer=MetadataWriter(store.root,store.data)
        self.lock=threading.RLock();self.closed=threading.Event();self.sku=None;self.on_assignment=None
        with self.connect() as c:
            c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,created REAL,captured TEXT,camera TEXT,role TEXT DEFAULT 'product',sku TEXT DEFAULT '',normalized TEXT DEFAULT '',project TEXT DEFAULT '',scope TEXT DEFAULT '',segment TEXT DEFAULT '',source TEXT DEFAULT '',locked INTEGER DEFAULT 0,revision INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS asset_sku ON assets(normalized,project,scope);
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,asset TEXT REFERENCES assets(id),component TEXT,sha TEXT,image_revision INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS locations(path TEXT PRIMARY KEY,file TEXT REFERENCES files(id),last_seen REAL,status TEXT DEFAULT 'online');
CREATE TABLE IF NOT EXISTS versions(file TEXT,sha TEXT,created REAL,PRIMARY KEY(file,sha));
CREATE TABLE IF NOT EXISTS file_fingerprints(path TEXT PRIMARY KEY,asset TEXT,file TEXT,filename TEXT,stem TEXT,folder TEXT,camera TEXT,captured TEXT,identity_json TEXT,sha TEXT,origin TEXT,updated REAL);
CREATE INDEX IF NOT EXISTS fingerprint_name ON file_fingerprints(stem,captured,camera);
CREATE INDEX IF NOT EXISTS fingerprint_sha ON file_fingerprints(sha);
CREATE INDEX IF NOT EXISTS fingerprint_asset ON file_fingerprints(asset,file);
CREATE INDEX IF NOT EXISTS version_sha ON versions(sha);
CREATE TABLE IF NOT EXISTS catalog_links(session TEXT,capture TEXT,asset TEXT REFERENCES assets(id),PRIMARY KEY(session,capture));
CREATE TABLE IF NOT EXISTS assignments(asset TEXT,revision INTEGER,data TEXT,created REAL,PRIMARY KEY(asset,revision));
CREATE TABLE IF NOT EXISTS previews(id TEXT PRIMARY KEY,data TEXT,created REAL,used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,created REAL,kind TEXT,source TEXT,status TEXT DEFAULT 'queued');
CREATE TABLE IF NOT EXISTS metadata_jobs(id TEXT PRIMARY KEY,operation TEXT REFERENCES operations(id),file TEXT REFERENCES files(id),asset TEXT,revision INTEGER,path TEXT,expected_sha TEXT,mode TEXT,fields TEXT,status TEXT,reason TEXT DEFAULT '',journal TEXT DEFAULT '{}',created REAL,UNIQUE(operation,file,path));
CREATE TABLE IF NOT EXISTS streams(id TEXT PRIMARY KEY,session TEXT,project TEXT,scope TEXT,auto_write INTEGER,mode TEXT,seen TEXT,state TEXT,last_boundary TEXT DEFAULT '',created REAL);
CREATE TABLE IF NOT EXISTS bindings(stream TEXT,asset TEXT,capture TEXT,seq INTEGER,order_key TEXT,state TEXT DEFAULT 'pending',PRIMARY KEY(stream,asset));
CREATE TABLE IF NOT EXISTS segments(id TEXT PRIMARY KEY,job TEXT UNIQUE,stream TEXT,label TEXT,members TEXT,status TEXT,sku TEXT DEFAULT '',operation TEXT DEFAULT '',reason TEXT DEFAULT '',created REAL);
CREATE TABLE IF NOT EXISTS event_receipts(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,kind TEXT,data TEXT,created REAL);
PRAGMA user_version=1;''')
            asset_search.install(c)
            # 兼容旧拍摄段：SKU 确认后统一自动写入，不再依赖界面开关。
            c.execute('UPDATE streams SET auto_write=1 WHERE auto_write IS NOT 1')
            # 新的 SKU 区间统一写入 RAW/JPG 内部；历史批次保留原策略以便撤销。
            c.execute("UPDATE streams SET mode='embedded' WHERE state='active' AND mode!='embedded'")
            c.execute("UPDATE metadata_jobs SET status='queued' WHERE status='running'")
        self.thread=threading.Thread(target=self._run,daemon=True,name='asset-archive')
        if autostart:self.thread.start()
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.db,timeout=30);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
        try:
            with c:yield c
        finally:c.close()
    @property
    def ratings(self):
        with self.lock:
            if not hasattr(self, '_ratings'):
                from shared_ratings import SharedRatings
                self._ratings = SharedRatings(self)
            return self._ratings

    def ensure(self,sid,cid,*,asset_hint=None,inherit=True):
        with self.lock:
            session=self.store.session(sid)
            if not session:raise ValueError('照片目录不存在')
            record=next((r for r in self.store.records(sid) if r['id']==cid),None)
            if not record:raise ValueError('照片不存在')
            with self.store.connect() as c:final=bool(c.execute('SELECT 1 FROM settings WHERE key=?',('archive_session:'+sid,)).fetchone())
            with self.connect() as c:
                linked=c.execute('SELECT asset FROM catalog_links WHERE session=? AND capture=?',(sid,cid)).fetchone();aid=linked[0] if linked else None
            if asset_hint and aid and aid!=asset_hint:raise ValueError('目标 RAW 已关联其他资产，请先复核')
            aid=aid or asset_hint
            paths=[(k,media.resolved_file(session['root'],record[k])) for k in ('raw','jpg') if record.get(k)]
            # 先查齐所有组件，再决定资产身份；任何失败都不留下半个 RAW/JPG 资产。
            with self.connect() as c:
                components=[]
                for kind,path in paths:
                    known=c.execute('SELECT f.* FROM locations l JOIN files f ON f.id=l.file WHERE l.path=?',(str(path),)).fetchone()
                    if known:
                        components.append((kind,path,dict(known),None,None));continue
                    digest=sha(path);metadata=self.writer.read(path)
                    if kind=='raw' and not metadata.get('XMP-fk:AssetId') and path.with_suffix('.xmp').exists():metadata=self.writer.read(path.with_suffix('.xmp'))
                    identity=metadata.get('XMP-fk:FileId')
                    try:identity=uuid.UUID(str(identity)).hex
                    except ValueError:identity=None
                    file=c.execute('SELECT * FROM files WHERE id=?',(identity,)).fetchone()
                    if file and not c.execute('SELECT 1 FROM versions WHERE file=? AND sha=?',(file['id'],digest)).fetchone():raise ValueError('同一资产身份对应不同内容，请先核对外部编辑或派生图')
                    if not file:file=c.execute('SELECT f.* FROM files f JOIN versions v ON f.id=v.file WHERE v.sha=?',(digest,)).fetchone()
                    components.append((kind,path,dict(file) if file else None,digest,metadata))
                candidates=list(dict.fromkeys(item[2]['asset'] for item in components if item[2]))
                if not aid and candidates:
                    established=[a for a in candidates if c.execute('SELECT 1 FROM catalog_links WHERE asset=? LIMIT 1',(a,)).fetchone()]
                    aid=(established or candidates)[0]
                for other in candidates:
                    if other==aid:continue
                    self._adopt_incomplete(c,other,aid,components)
                for kind,path,file,digest,metadata in components:
                    if digest is None:
                        if file['asset']==aid:continue # 已知位置，无新增组件或元数据写入。
                        digest=file['sha'] # 刚恢复的组件也要继承已确认的归属。
                    if file:fid=file['id']
                    else:
                        proposed=metadata.get('XMP-fk:AssetId')
                        try:proposed=uuid.UUID(str(proposed)).hex
                        except ValueError:proposed=None
                        if proposed and aid and proposed!=aid:raise ValueError('RAW/JPG 资产身份冲突')
                        aid=aid or proposed or uid();fid=uid()
                        try:fid=uuid.UUID(str(metadata.get('XMP-fk:FileId'))).hex
                        except ValueError:pass
                        c.execute('INSERT OR IGNORE INTO assets(id,created,captured,camera) VALUES (?,?,?,?)',(aid,time.time(),record.get('time'),record.get('cameraKey','')))
                        c.execute('INSERT INTO files(id,asset,component,sha) VALUES (?,?,?,?)',(fid,aid,kind,digest))
                        sku=metadata.get('XMP-fk:SKU','');scope=str(metadata.get('XMP-fk:ShootType',''))
                        if sku:c.execute("UPDATE assets SET sku=?,normalized=?,scope=?,source='embedded',revision=MAX(revision,1) WHERE id=? AND sku=''",(sku,normalize(sku),scope,aid))
                    c.execute('INSERT OR IGNORE INTO versions VALUES (?,?,?)',(fid,digest,time.time()))
                    c.execute('INSERT OR REPLACE INTO locations VALUES (?,?,?,?)',(str(path),fid,time.time(),'online'))
                    current=c.execute('SELECT * FROM assets WHERE id=?',(aid,)).fetchone()
                    assignment=c.execute('SELECT data FROM assignments WHERE asset=? AND revision=?',(aid,current['revision'])).fetchone()
                    if inherit and current['sku'] and assignment:
                        origin=json.loads(assignment[0]).get('operation');old_job=c.execute('SELECT mode FROM metadata_jobs WHERE operation=? LIMIT 1',(origin,)).fetchone()
                        if old_job and not c.execute('SELECT 1 FROM metadata_jobs WHERE operation=? AND file=? AND path=?',(origin,fid,str(path))).fetchone():
                            mode=old_job['mode'];fields={'AssetId':aid,'FileId':fid,'SKU':current['sku'],'ShootType':current['scope'],'ProjectId':current['project'],'SegmentId':current['segment'],'MetadataRevision':current['revision']}
                            cap=self.writer.capability(path,mode);status='queued' if cap!='unsupported' else 'failed'
                            c.execute('INSERT INTO metadata_jobs(id,operation,file,asset,revision,path,expected_sha,mode,fields,status,reason,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(uid(),origin,fid,aid,current['revision'],str(path),digest,mode,dumps(fields),status,'' if status=='queued' else '后到组件的格式尚未开放内嵌写入',time.time()))
                c.execute('INSERT OR REPLACE INTO catalog_links VALUES (?,?,?)',(sid,cid,aid))
                for kind,path in paths:
                    file=c.execute('SELECT f.* FROM files f JOIN locations l ON l.file=f.id WHERE l.path=?',(str(path),)).fetchone()
                    if file:c.execute('INSERT OR REPLACE INTO file_fingerprints VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(str(path),aid,file['id'],path.name,path.stem.casefold(),str(path.parent),record.get('cameraKey',''),record.get('time'),dumps(record.get('captureIdentity',{})),file['sha'],'archive' if final else 'onsite',time.time()))
            return aid
    def _adopt_incomplete(self,c,old_id,aid,components):
        """仅回收旧版本中断后留下、没有被任何业务使用的组件资产。"""
        old=c.execute('SELECT * FROM assets WHERE id=?',(old_id,)).fetchone()
        referenced=any(c.execute('SELECT 1 FROM '+table+' WHERE asset=? LIMIT 1',(old_id,)).fetchone() for table in ('catalog_links','bindings','assignments','metadata_jobs'))
        files=[r['id'] for r in c.execute('SELECT id FROM files WHERE asset=?',(old_id,))]
        members=[item for item in components if item[2] and item[2]['asset']==old_id]
        if (not old or referenced or old['revision'] or old['locked'] or old['role']!='product'
                or any(old[k] for k in ('sku','normalized','project','scope','segment','source'))
                or set(files)!={item[2]['id'] for item in members}):raise ValueError('配对文件资产冲突：已有独立归属，请先复核')
        analysis=self.store.data/'vision_analysis.sqlite3'
        if analysis.is_file():
            with sqlite3.connect('file:'+str(analysis.resolve())+'?mode=ro',uri=True) as vision:
                for table in ('jobs','results'):
                    if vision.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() and vision.execute('SELECT 1 FROM '+table+' WHERE asset=? LIMIT 1',(old_id,)).fetchone():raise ValueError('配对文件资产冲突：已有视觉解析记录，请先复核')
        for kind,path,file,digest,metadata in members:
            metadata=metadata if metadata is not None else self.writer.read(path)
            if any(metadata.get('XMP-fk:'+key) for key in ('AssetId','FileId','SKU','ShootType','MetadataRevision')):raise ValueError('配对文件资产冲突：文件已有永久身份或归属，请先复核')
            if kind=='raw' and path.with_suffix('.xmp').exists():raise ValueError('配对文件资产冲突：RAW 已有伴随元数据，请先复核')
            if not c.execute('SELECT 1 FROM versions WHERE file=? AND sha=?',(file['id'],sha(path))).fetchone():raise ValueError('配对文件资产冲突：组件已被外部修改，请先复核')
        c.execute('UPDATE files SET asset=? WHERE asset=?',(aid,old_id))
        c.execute('DELETE FROM assets WHERE id=?',(old_id,))
        c.execute('INSERT INTO audit VALUES (?,?,?,?)',(uid(),'recover_incomplete_pair',dumps({'before':dict(old),'asset':aid,'files':files}),time.time()))
    def asset(self,aid):
        with self.connect() as c:
            row=c.execute('SELECT * FROM assets WHERE id=?',(aid,)).fetchone()
            if not row:raise ValueError('资产不存在')
            value=dict(row);value['files']=[dict(r) for r in c.execute('SELECT f.*,l.path,l.status AS location_status FROM files f JOIN locations l ON l.file=f.id WHERE f.asset=?',(aid,))]
            value['history']=[dict(r) for r in c.execute('SELECT * FROM assignments WHERE asset=? ORDER BY revision DESC LIMIT 30',(aid,))]
            value['jobs']=[dict(r) for r in c.execute('SELECT id,operation,status,reason,mode,path FROM metadata_jobs WHERE asset=? ORDER BY created DESC LIMIT 30',(aid,))]
            return value
    def query(self,sku='',scope='',project='',offset=0,limit=60):
        clauses=[];args=[]
        for key,value in [('normalized',normalize(sku)),('scope',scope),('project',project)]:
            if value:clauses.append(key+' LIKE ?');args.append('%'+value+'%')
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.connect() as c:
            total=c.execute('SELECT count(*) FROM assets'+where,args).fetchone()[0]
            rows=[dict(r) for r in c.execute('SELECT * FROM assets'+where+' ORDER BY created DESC LIMIT ? OFFSET ?',args+[max(1,min(100,limit)),max(0,offset)])]
            asset_search.hydrate(c,rows)
        return {'rows':rows,'total':total,'offset':offset}
    def search(self,**kwargs):return asset_search.search(self,**kwargs)
    def preview(self,sid,cids,sku,scope,project='',mode='embedded',overwrite=False,asset_ids=None,source='manual',segment=''):
        if not isinstance(sku,str) or not normalize(sku) or len(sku)>80 or any(ord(ch)<32 for ch in sku):raise ValueError('请填写有效 SKU（不超过 80 字符）')
        if not isinstance(scope,str) or not scope.strip() or len(scope)>100:raise ValueError('请填写拍摄类型/工作表')
        if mode not in ('embedded','sidecar'):raise ValueError('请选择正确写入策略')
        if source!='manual':mode='embedded' # 旧段的迟到识别/补交也不能新建仅伴随文件任务。
        if asset_ids is None:
            if not isinstance(cids,list) or not 1<=len(cids)<=2000:raise ValueError('请选择 1–2000 张照片')
            asset_ids=list(dict.fromkeys(self.ensure(sid,cid) for cid in cids))
        targets=[]
        for aid in dict.fromkeys(asset_ids):
            a=self.asset(aid);conflict=bool(a['sku'] and (a['sku']!=sku or a['scope']!=scope))
            if source!='manual' and a['locked']:conflict=True
            files=[]
            for f in a['files']:
                online=Path(f['path']).is_file()
                files.append({'id':f['id'],'path':f['path'],'sha':sha(f['path']) if online else f['sha'],'capability':self.writer.capability(f['path'],mode),'online':online})
            targets.append({'id':aid,'revision':a['revision'],'before':{k:a[k] for k in ('sku','normalized','scope','project','segment','source','locked')},'conflict':conflict,'apply':not conflict or (overwrite and source=='manual'),'files':files})
        data={'targets':targets,'sku':sku,'scope':scope,'project':project,'mode':mode,'source':source,'segment':segment}
        pid=uid()
        with self.connect() as c:c.execute('INSERT INTO previews(id,data,created) VALUES (?,?,?)',(pid,dumps(data),time.time()))
        return {'preview_id':pid,**data}
    def apply(self,pid):
        with self.lock,self.connect() as c:
            row=c.execute('SELECT * FROM previews WHERE id=?',(pid,)).fetchone()
            if not row or row['used'] or time.time()-row['created']>900:raise ValueError('预览已失效，请重新预览')
            data=json.loads(row['data']);targets=[t for t in data['targets'] if t['apply']]
            if not targets:raise ValueError('没有可应用的目标，请先处理归属冲突')
            for t in targets:
                current=c.execute('SELECT revision FROM assets WHERE id=?',(t['id'],)).fetchone()
                if not current or current[0]!=t['revision']:raise ValueError('资产归属版本已变，请重新预览')
            oid=uid();c.execute('INSERT INTO operations(id,created,kind,source) VALUES (?,?,?,?)',(oid,time.time(),'assign',data['segment']))
            for t in targets:
                revision=t['revision']+1
                c.execute('INSERT INTO assignments VALUES (?,?,?,?)',(t['id'],revision,dumps({'before':t['before'],'after':{k:data[k] for k in ('sku','scope','project','segment','source')},'operation':oid}),time.time()))
                c.execute('UPDATE assets SET sku=?,normalized=?,scope=?,project=?,segment=?,source=?,locked=?,revision=? WHERE id=?',(data['sku'],normalize(data['sku']),data['scope'],data['project'],data['segment'],data['source'],int(data['source']=='manual'),revision,t['id']))
                for f in t['files']:
                    fields={'AssetId':t['id'],'FileId':f['id'],'SKU':data['sku'],'ShootType':data['scope'],'ProjectId':data['project'],'SegmentId':data['segment'],'MetadataRevision':revision}
                    status='queued' if f['capability']!='unsupported' and f['online'] else 'failed';reason='' if status=='queued' else ('文件离线，请重新定位' if not f['online'] else '此格式尚不支持内部元数据写入，未写入该文件')
                    c.execute('INSERT INTO metadata_jobs(id,operation,file,asset,revision,path,expected_sha,mode,fields,status,reason,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(uid(),oid,f['id'],t['id'],revision,f['path'],f['sha'],data['mode'],dumps(fields),status,reason,time.time()))
            c.execute('UPDATE previews SET used=1 WHERE id=?',(pid,));c.execute('INSERT INTO audit VALUES (?,?,?,?)',(uid(),'assign',dumps({'operation':oid,'preview':pid}),time.time()))
            if not c.execute("SELECT 1 FROM metadata_jobs WHERE operation=? AND status='queued'",(oid,)).fetchone():c.execute("UPDATE operations SET status='finished' WHERE id=?",(oid,))
        if self.on_assignment:self.on_assignment([t['id'] for t in targets])
        return self.operation(oid)
    def operation(self,oid):
        with self.connect() as c:
            row=c.execute('SELECT * FROM operations WHERE id=?',(oid,)).fetchone()
            if not row:raise ValueError('写入批次不存在')
            jobs=[dict(r) for r in c.execute('SELECT id,asset,file,path,mode,status,reason FROM metadata_jobs WHERE operation=?',(oid,))]
            for job in jobs:job['storage']=self.writer.capability(job['path'],job['mode'])
        return {'operation':dict(row),'jobs':jobs}
    def retry(self,oid):
        with self.connect() as c:c.execute("UPDATE metadata_jobs SET status='queued',reason='' WHERE operation=? AND status='failed'",(oid,))
        return self.operation(oid)
    def _refresh_catalog(self,aid):
        with self.connect() as c:links=c.execute('SELECT * FROM catalog_links WHERE asset=?',(aid,)).fetchall()
        for link in links:
            session=self.store.session(link['session'])
            if not session:continue
            record=next((r for r in self.store.records(link['session']) if r['id']==link['capture']),None)
            if not record:continue
            try:
                paths=[media.resolved_file(session['root'],record[k]) for k in ('raw','jpg') if record.get(k)]
                for path in paths:media._STATS[str(path)]=path.stat()
                old=record['fingerprint'];record['fingerprint']=media.fingerprint(paths)
                with self.connect() as c:
                    for path in paths:c.execute('UPDATE file_fingerprints SET sha=?,updated=? WHERE path=?',(sha(path),time.time(),str(path)))
                if self.sku:self.sku.metadata_changed(link['session'],link['capture'],old,record['fingerprint'])
                with self.store.connect() as c:c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(dumps(record),link['session'],link['capture']))
            except (OSError,ValueError):pass
    def process_one(self):
        with self.lock:
            with self.connect() as c:
                row=c.execute("SELECT * FROM metadata_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if not row:return
                job=dict(row);current=c.execute('SELECT revision FROM assets WHERE id=?',(job['asset'],)).fetchone()
                if not current or current[0]!=job['revision']:
                    c.execute("UPDATE metadata_jobs SET status='cancelled',reason='归属版本已更新' WHERE id=?",(job['id'],));return
                c.execute("UPDATE metadata_jobs SET status='running' WHERE id=?",(job['id'],))
            temp=None
            try:
                fields=json.loads(job['fields']);journal=json.loads(job['journal'])
                # 提交后崩溃：读回已持久化的 journal，补记数据库而非再写一遍。
                committed=journal.get('target') and Path(journal['target']).exists() and sha(journal['target'])==journal.get('after_hash')
                if not committed:
                    with self.writer.exclusive(job['path']):
                        temp,target,journal=self.writer.write(job['path'],fields,job['expected_sha'],mode=job['mode'],restore=journal.get('restore'),applied=journal.get('applied'))
                        if target.exists():
                            import shutil
                            backup=self.store.data/'metadata-backups';backup.mkdir(exist_ok=True)
                            backup=backup/(job['id']+'.bin');shutil.copy2(target,backup);journal['backup']=str(backup)
                        with self.connect() as c:c.execute('UPDATE metadata_jobs SET journal=? WHERE id=?',(dumps(journal),job['id']))
                        if sha(job['path'])!=job['expected_sha'] or (sha(target) if target.exists() else None)!=journal['before_hash']:raise ValueError('替换前文件已变化，未覆盖')
                    # Windows MoveFileEx 无法替换仍由本进程读句柄持有的目标；
                    # 结束保护句柄后立即复核并执行同卷替换，备份/journal 已落盘。
                    if sha(job['path'])!=job['expected_sha'] or (sha(target) if target.exists() else None)!=journal['before_hash']:raise ValueError('提交前检测到外部修改')
                    os.replace(temp,target);temp.parent.rmdir();temp=None
                if sha(journal['target'])!=journal['after_hash']:raise ValueError('提交后检测到外部修改，请核对文件')
                digest=sha(job['path'])
                with self.connect() as c:
                    c.execute('UPDATE files SET sha=? WHERE id=?',(digest,job['file']));c.execute('INSERT OR IGNORE INTO versions VALUES (?,?,?)',(job['file'],digest,time.time()))
                    c.execute("UPDATE metadata_jobs SET status='applied',reason='' WHERE id=?",(job['id'],))
                self._refresh_catalog(job['asset'])
            except Exception as exc:
                if temp:
                    temp.unlink(missing_ok=True)
                    try:temp.parent.rmdir()
                    except OSError:pass
                with self.connect() as c:c.execute("UPDATE metadata_jobs SET status='failed',reason=? WHERE id=?",(str(exc)[:350],job['id']))
            with self.connect() as c:
                pending=c.execute("SELECT count(*) FROM metadata_jobs WHERE operation=? AND status IN ('queued','running')",(job['operation'],)).fetchone()[0]
                c.execute('UPDATE operations SET status=? WHERE id=?',('running' if pending else 'finished',job['operation']))
    def revert(self,oid):
        with self.lock,self.connect() as c:
            jobs=c.execute('SELECT * FROM metadata_jobs WHERE operation=?',(oid,)).fetchall()
            c.execute("UPDATE metadata_jobs SET status='cancelled' WHERE operation=? AND status IN ('queued','running','failed')",(oid,))
            rid=uid();c.execute('INSERT INTO operations(id,created,kind,source) VALUES (?,?,?,?)',(rid,time.time(),'revert',oid))
            for aid in {j['asset'] for j in jobs}:
                assignment=c.execute('SELECT * FROM assignments WHERE asset=? AND json_extract(data,\'$.operation\')=?',(aid,oid)).fetchone()
                a=c.execute('SELECT * FROM assets WHERE id=?',(aid,)).fetchone()
                if not assignment or a['revision']!=assignment['revision']:continue
                before=json.loads(assignment['data'])['before'];rev=a['revision']+1
                c.execute('UPDATE assets SET sku=?,normalized=?,scope=?,project=?,segment=?,source=?,locked=?,revision=? WHERE id=?',(*(before[k] for k in ('sku','normalized','scope','project','segment','source','locked')),rev,aid))
                for j in jobs:
                    if j['asset']!=aid or j['status']!='applied':continue
                    journal=json.loads(j['journal']);fields=json.loads(j['fields']);restore=dict(journal['before'],AssetId=fields['AssetId'],FileId=fields['FileId'],MetadataRevision=rev)
                    c.execute('INSERT INTO metadata_jobs(id,operation,file,asset,revision,path,expected_sha,mode,fields,status,journal,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(uid(),rid,j['file'],aid,rev,j['path'],sha(j['path']),j['mode'],dumps(fields),'queued',dumps({'restore':restore,'applied':journal['after']}),time.time()))
                c.execute('INSERT INTO assignments VALUES (?,?,?,?)',(aid,rev,dumps({'before':dict(a),'after':before,'operation':rid}),time.time()))
        if self.on_assignment:self.on_assignment(list({j['asset'] for j in jobs}))
        return self.operation(rid)
    def start_stream(self,sid,project,scope,auto_write=None,mode=None,start_capture=None):
        self.sync_stream()
        records=self.store.records(sid)
        auto_write=True # 保留旧调用参数兼容性，不再允许旧客户端关闭自动备案。
        if mode is None:mode='embedded'
        if mode not in ('embedded','sidecar'):raise ValueError('写入策略无效')
        if not scope:raise ValueError('请先选择拍摄工作表')
        with self.lock,self.connect() as c:
            # 接续同一活动段，避免重复点击或重新激活任务丢掉尚未封口的首款。
            active=c.execute("SELECT id FROM streams WHERE session=? AND project=? AND scope=? AND state='active' ORDER BY created DESC LIMIT 1",(sid,project,scope)).fetchone()
            # 第一张可以是产品图。仅跳过已有拍摄段接管的照片，首款无需起点标签。
            seen=[r[0] for r in c.execute('SELECT DISTINCT b.capture FROM bindings b JOIN streams s ON s.id=b.stream WHERE s.session=?',(sid,))]
            if active and not start_capture:
                stream=active['id'];c.execute('UPDATE streams SET seen=?,mode=? WHERE id=?',(dumps(seen),mode,stream))
            else:
                c.execute("UPDATE streams SET state='paused' WHERE state='active'")
                stream=uid()
                if start_capture:
                    ids=[r['id'] for r in records]
                    if start_capture not in ids:raise ValueError('起点照片不存在')
                    seen=ids[:ids.index(start_capture)]
                c.execute('INSERT INTO streams(id,session,project,scope,auto_write,mode,seen,state,created) VALUES (?,?,?,?,?,?,?,?,?)',(stream,sid,project,scope,int(auto_write),mode,dumps(seen),'active',time.time()))
        self.sync_stream(stream);return self.stream_state()
    def stream_state(self):
        with self.connect() as c:
            streams=[dict(r) for r in c.execute("SELECT id,session,project,scope,auto_write,mode,state,created,(SELECT COUNT(*) FROM bindings b WHERE b.stream=s.id AND b.state='pending') AS pending FROM streams s ORDER BY created DESC LIMIT 15")]
            segments=[dict(r) for r in c.execute("SELECT * FROM segments WHERE status!='superseded' ORDER BY created DESC LIMIT 60")]
            late=[dict(r) for r in c.execute("SELECT * FROM bindings WHERE state='late' ORDER BY seq LIMIT 100")]
        for s in segments:s['members']=json.loads(s['members'])
        return {'streams':streams,'segments':segments,'late':late}
    def sync_stream(self,stream_id=None):
        with self.lock:
            with self.connect() as c:streams=c.execute('SELECT * FROM streams WHERE '+('id=?' if stream_id else "state='active'"),(stream_id,) if stream_id else ()).fetchall()
            for stream in streams:
                seen=set(json.loads(stream['seen']));records=self.store.records(stream['session'])
                with self.connect() as c:
                    seq=c.execute('SELECT COALESCE(MAX(seq),0) FROM bindings WHERE stream=?',(stream['id'],)).fetchone()[0]
                    bound={r[0] for r in c.execute('SELECT capture FROM bindings WHERE stream=?',(stream['id'],))}
                    paths={r[0].casefold() for r in c.execute('SELECT l.path FROM locations l JOIN files f ON f.id=l.file JOIN catalog_links k ON k.asset=f.asset WHERE k.session=?',(stream['session'],))}
                session=self.store.session(stream['session'])
                for r in records:
                    if r['id'] in seen:
                        if r['id'] in bound and any(str(Path(session['root'])/r[k]).casefold() not in paths for k in ('raw','jpg') if r.get(k)):self.ensure(stream['session'],r['id'])
                        continue
                    aid=self.ensure(stream['session'],r['id']);seq+=1;order=(r.get('time') or '')+'|'+f'{seq:012}'
                    late=bool(stream['last_boundary'] and r.get('time') and order<=stream['last_boundary'])
                    with self.connect() as c:c.execute('INSERT OR IGNORE INTO bindings VALUES (?,?,?,?,?,?)',(stream['id'],aid,r['id'],seq,order,'late' if late else 'pending'))
                    seen.add(r['id'])
                with self.connect() as c:c.execute('UPDATE streams SET seen=? WHERE id=?',(dumps(list(seen)),stream['id']))
    def _rebuild_segments(self,stream_id):
        """所有已知结束标签共同决定成员，补入旧标签时重划受影响的自动归属。"""
        with self.lock:
            with self.connect() as c:
                stream=c.execute('SELECT * FROM streams WHERE id=?',(stream_id,)).fetchone()
                if not stream:return
                bindings=[dict(r) for r in c.execute('SELECT * FROM bindings WHERE stream=?',(stream_id,))]
                segments=[dict(r) for r in c.execute("SELECT * FROM segments WHERE stream=? AND status NOT IN ('cancelled','superseded') ORDER BY created,id",(stream_id,))]
            by_capture={b['capture']:b for b in bindings};by_asset={b['asset']:b for b in bindings}
            boundaries={};superseded=[]
            for segment in segments:
                binding=by_asset.get(segment['label'])
                if not binding:continue
                cid=binding['capture']
                if cid in boundaries:superseded.append(boundaries[cid])
                boundaries[cid]=segment
            records=[r for r in self.store.records(stream['session']) if r['id'] in by_capture]
            intervals=partition(records,boundaries)
            changes=[]
            for interval in intervals:
                segment=interval['boundary'];members=list(dict.fromkeys(by_capture[cid]['asset'] for cid in interval['members']))
                if set(members)!=set(json.loads(segment['members'])) or any(old['label']==segment['label'] for old in superseded):
                    changes.append((segment,members))
            # 先通过既有补偿链路退回旧自动操作；人工锁定的新版本由 revert 自身保护。
            for segment,_ in changes:
                if segment['operation']:self.revert(segment['operation'])
            for segment in superseded:
                if segment['operation']:self.revert(segment['operation'])
            with self.connect() as c:
                for segment,members in changes:
                    c.execute("UPDATE segments SET members=?,operation='',status=?,reason='' WHERE id=?",(dumps(members),'ready' if segment['sku'] else 'waiting_sku',segment['id']))
                for segment in superseded:
                    c.execute("UPDATE segments SET status='superseded',reason='同一标签已有更新识别记录' WHERE id=?",(segment['id'],))
                sealed={cid for interval in intervals for cid in interval['members']}
                for binding in bindings:
                    c.execute('UPDATE bindings SET state=? WHERE stream=? AND asset=?',('sealed' if binding['capture'] in sealed else 'pending',stream_id,binding['asset']))
                latest=max((by_capture[cid]['order_key'] for cid in boundaries),default='')
                c.execute('UPDATE streams SET last_boundary=? WHERE id=?',(latest,stream_id))
                c.execute("UPDATE assets SET role='product' WHERE id IN (SELECT label FROM segments WHERE stream=? AND status='cancelled') AND NOT EXISTS (SELECT 1 FROM segments g WHERE g.label=assets.id AND g.status NOT IN ('cancelled','superseded'))",(stream_id,))
                ready=[r[0] for r in c.execute("SELECT id FROM segments WHERE stream=? AND status='ready'",(stream_id,))]
            for sid in ready:
                try:self.apply_segment(sid)
                except ValueError as exc:
                    with self.connect() as c:c.execute("UPDATE segments SET status='needs_review',reason=? WHERE id=?",(str(exc),sid))
    def reconcile_ocr(self,event_id,sid,cid):
        """普通补识别可修正已启用的自动段；独立 OCR 不因此启用文件写入。"""
        from sku_library import recognized_rows
        with self.lock:
            with self.connect() as c:
                if c.execute('SELECT 1 FROM event_receipts WHERE id=?',(event_id,)).fetchone():return
                stream=c.execute("""SELECT s.* FROM streams s JOIN bindings b ON b.stream=s.id
                    WHERE s.session=? AND b.capture=? AND EXISTS (
                      SELECT 1 FROM segments g WHERE g.stream=s.id AND g.sku!=''
                      AND g.status NOT IN ('cancelled','superseded')) ORDER BY s.created DESC LIMIT 1""",(sid,cid)).fetchone()
            if stream and self.sku:
                with self.connect() as c:
                    bindings={r['capture']:dict(r) for r in c.execute('SELECT * FROM bindings WHERE stream=?',(stream['id'],))}
                    segments=[dict(r) for r in c.execute('SELECT * FROM segments WHERE stream=? ORDER BY created,id',(stream['id'],))]
                markers={r['capture_id']:r for r in recognized_rows(self.sku,sid)}
                if cid not in markers:
                    with self.connect() as c:c.execute('INSERT OR IGNORE INTO event_receipts VALUES (?)',(event_id,))
                    return
                for capture,marker in markers.items():
                    binding=bindings.get(capture)
                    if not binding:continue
                    prior=[segment for segment in segments if segment['label']==binding['asset']]
                    active=next((segment for segment in reversed(prior) if segment['status'] not in ('cancelled','superseded')),None)
                    if active and capture!=cid:continue
                    # 不因补识别别的图片重新启用用户已撤销的旧边界。
                    if not active and prior and capture!=cid:continue
                    if active:
                        if active['sku']==marker['sku']:continue
                        if active['operation']:self.revert(active['operation'])
                        with self.connect() as c:c.execute("UPDATE segments SET sku=?,operation='',status='ready',reason='' WHERE id=?",(marker['sku'],active['id']))
                    else:
                        old=prior[-1] if prior else None
                        if old and old['operation']:self.revert(old['operation'])
                        with self.connect() as c:
                            if old:c.execute("UPDATE segments SET sku=?,operation='',status='ready',reason='' WHERE id=?",(marker['sku'],old['id']))
                            else:c.execute('INSERT INTO segments(id,job,stream,label,members,status,sku,created) VALUES (?,?,?,?,?,?,?,?)',(uid(),'ocr:'+sid+':'+capture,stream['id'],binding['asset'],'[]','waiting_sku',marker['sku'],time.time()))
                            c.execute("UPDATE assets SET role='sku_label' WHERE id=?",(binding['asset'],))
                self._rebuild_segments(stream['id'])
            with self.connect() as c:c.execute('INSERT OR IGNORE INTO event_receipts VALUES (?)',(event_id,))
    def reserve(self,job):
        with self.lock:
            with self.connect() as c:
                existing=c.execute('SELECT * FROM segments WHERE job=?',(job['id'],)).fetchone()
                if existing:
                    if existing['status']=='cancelled' and job['status'] not in ('cancelled','ignored'):
                        c.execute("UPDATE segments SET status='waiting_sku',sku='',operation='' WHERE id=?",(existing['id'],))
                    stream_id=existing['stream']
                else:stream_id=None
            if stream_id:
                self._rebuild_segments(stream_id);return
            with self.connect() as c:
                stream=c.execute('SELECT s.* FROM streams s JOIN bindings b ON b.stream=s.id WHERE b.capture=? AND s.session=? AND s.project=? AND s.scope=? ORDER BY s.created DESC LIMIT 1',(job['capture_id'],job['session_id'],job['project'],job['scope'])).fetchone()
                if not stream:stream=c.execute("SELECT * FROM streams WHERE session=? AND project=? AND scope=? AND state='active' ORDER BY created DESC LIMIT 1",(job['session_id'],job['project'],job['scope'])).fetchone()
            if not stream:return
            if stream['state']=='active' and not stream['last_boundary']:
                self.start_stream(stream['session'],stream['project'],stream['scope'])
            self.sync_stream(stream['id'])
            with self.connect() as c:
                label=c.execute('SELECT * FROM bindings WHERE stream=? AND capture=?',(stream['id'],job['capture_id'])).fetchone()
                if not label:return
                old=c.execute("SELECT id FROM segments WHERE stream=? AND label=? AND status NOT IN ('cancelled','superseded') ORDER BY created DESC,id DESC LIMIT 1",(stream['id'],label['asset'])).fetchone()
                if old:c.execute('UPDATE segments SET job=? WHERE id=?',(job['id'],old['id']))
                else:c.execute('INSERT INTO segments(id,job,stream,label,members,status,created) VALUES (?,?,?,?,?,?,?)',(uid(),job['id'],stream['id'],label['asset'],'[]','waiting_sku',time.time()))
                c.execute("UPDATE assets SET role='sku_label' WHERE id=?",(label['asset'],))
            self._rebuild_segments(stream['id'])
    def handle_event(self,event_id,job,sku=None):
        with self.lock:
            with self.connect() as c:
                if c.execute('SELECT 1 FROM event_receipts WHERE id=?',(event_id,)).fetchone():return
                seg=c.execute('SELECT * FROM segments WHERE job=?',(job['id'],)).fetchone()
                if not seg:return
                if seg['status']=='superseded':
                    c.execute('INSERT OR IGNORE INTO event_receipts VALUES (?)',(event_id,));return
                stream=c.execute('SELECT * FROM streams WHERE id=?',(seg['stream'],)).fetchone()
            if job['status'] in ('matched','confirmed') and sku:
                if seg['operation'] and seg['sku']==sku:pass
                else:
                    if seg['operation']:self.revert(seg['operation'])
                    p=self.preview(None,None,sku,stream['scope'],stream['project'],stream['mode'],asset_ids=json.loads(seg['members']),source='segment',segment=seg['id'])
                    try:
                        op=self.apply(p['preview_id'])['operation']['id']
                        protected=sum(not target['apply'] for target in p['targets'])
                        status='needs_review' if protected else 'confirmed';reason=f'{protected} 张人工或其他归属已保留，请复核' if protected else ''
                    except ValueError as exc:op='';status='needs_review';reason=str(exc)
                    with self.connect() as c:c.execute('UPDATE segments SET sku=?,status=?,operation=?,reason=? WHERE id=?',(sku,status,op,reason,seg['id']))
            elif job['status'] in ('cancelled','ignored'):
                if seg['operation']:self.revert(seg['operation'])
                with self.connect() as c:c.execute("UPDATE segments SET status='cancelled',operation='',reason='标签已撤销；已写文件通过补偿队列恢复' WHERE id=?",(seg['id'],))
                self._rebuild_segments(seg['stream'])
            else:
                with self.connect() as c:c.execute('UPDATE segments SET reason=? WHERE id=?',(job.get('reason','等待款号确认'),seg['id']))
            if self.sku and job['status'] in ('matched','confirmed') and sku:
                self.reconcile_ocr('ocr-boundaries:'+event_id,job['session_id'],job['capture_id'])
            with self.connect() as c:c.execute('INSERT OR IGNORE INTO event_receipts VALUES (?)',(event_id,))
    def relocate(self,folder):
        root,shots=media.discover(folder);sid=self.store.ensure_live_session(root)
        records=[]
        metadata=media.read_metadata([p for p,_ in shots],media.find_exiftool(self.store.root))
        for (primary,companion),meta in zip(shots,metadata):records.append(media.capture_record(root,primary,companion,meta))
        self.store.append_live(sid,records);ids=[self.ensure(sid,r['id']) for r in records]
        # 不对全库（可能百万路径、含离线盘）逐一 stat。
        # 此动作只登记本次扫描的身份；旧位置由后续按需核验处理。
        return {'assets':len(set(ids)),'files':len(records)}
    def configure_stream(self,stream_id,auto_write,mode):
        if mode not in ('embedded','sidecar'):raise ValueError('策略无效')
        with self.connect() as c:c.execute('UPDATE streams SET auto_write=1,mode=? WHERE id=?',(mode,stream_id))
        return self.stream_state()
    def apply_segment(self,segment_id):
        with self.connect() as c:
            seg=c.execute('SELECT * FROM segments WHERE id=?',(segment_id,)).fetchone()
            if not seg or seg['status']!='ready':raise ValueError('此区间尚未确认 SKU 或已提交')
            stream=c.execute('SELECT * FROM streams WHERE id=?',(seg['stream'],)).fetchone()
        preview=self.preview(None,None,seg['sku'],stream['scope'],stream['project'],stream['mode'],asset_ids=json.loads(seg['members']),source='segment',segment=seg['id'])
        op=self.apply(preview['preview_id'])
        protected=sum(not target['apply'] for target in preview['targets'])
        with self.connect() as c:c.execute('UPDATE segments SET operation=?,status=?,reason=? WHERE id=?',(op['operation']['id'],'needs_review' if protected else 'confirmed',f'{protected} 张人工或其他归属已保留，请复核' if protected else '',seg['id']))
        return op
    def close_stream(self,stream_id,sku):
        self.sync_stream(stream_id)
        with self.connect() as c:
            stream=c.execute('SELECT * FROM streams WHERE id=?',(stream_id,)).fetchone()
            rows=c.execute("SELECT * FROM bindings WHERE stream=? AND state='pending' ORDER BY seq",(stream_id,)).fetchall()
        if not stream or not rows:raise ValueError('当前段没有待结束的照片')
        job={'id':'manual-'+uid(),'project':stream['project'],'scope':stream['scope'],'session_id':stream['session'],'capture_id':rows[-1]['capture'],'status':'confirmed'}
        self.reserve(job)
        with self.connect() as c:c.execute("UPDATE assets SET role='product' WHERE id=?",(rows[-1]['asset'],))
        self.handle_event(uid(),job,sku);return self.stream_state()
    def tick(self):
        self.sync_stream()
        if self.sku:self.sku.deliver_events(self)
        # 旧版本已识别、等待勾选的当前拍摄段也自动继续处理。
        with self.connect() as c:
            ready=[r[0] for r in c.execute("SELECT g.id FROM segments g JOIN streams s ON s.id=g.stream WHERE g.status='ready' AND s.state='active'")]
        for segment in ready:
            try:self.apply_segment(segment)
            except ValueError as exc:
                with self.connect() as c:c.execute("UPDATE segments SET status='needs_review',reason=? WHERE id=?",(str(exc),segment))
        self.process_one()
    def _run(self):
        while not self.closed.is_set():
            try:self.tick()
            except Exception as exc:
                with self.connect() as c:c.execute('INSERT INTO audit VALUES (?,?,?,?)',(uid(),'worker_error',dumps({'error':str(exc)[:250]}),time.time()))
            self.closed.wait(.4)
    def close(self):
        self.closed.set()
        if self.thread.is_alive():self.thread.join(timeout=3)
