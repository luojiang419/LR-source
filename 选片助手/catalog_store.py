"""正式会话数据库；与演示数据库隔离，源文件始终只读。"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import media_engine as media

def now():return datetime.now(timezone.utc).isoformat()
def stable_json(data):return json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def folder_name(folder):
    root=Path(folder)
    return root.name or root.anchor.rstrip('\\/') or str(root)

class Store:
    def __init__(self,data,root):
        self.data=Path(data);self.data.mkdir(parents=True,exist_ok=True);self.root=Path(root);self.db=self.data/'framekeep.sqlite3';self.jobs={};self.lock=threading.Lock()
        self.scan_queue=deque();self.scan_worker_running=False;self.scan_cancellations={};self.browser_lock=threading.RLock();self.browser_totals={}
        self.on_catalog=None
        self.on_previews=None
        self.discovery=None
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,root TEXT NOT NULL,name TEXT NOT NULL,snapshot TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT '{}',updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS captures(session TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,position INTEGER NOT NULL,PRIMARY KEY(session,id));
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session TEXT NOT NULL,snapshot TEXT NOT NULL,keep_last INTEGER NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,created TEXT NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reviews(run TEXT NOT NULL,group_index INTEGER NOT NULL,PRIMARY KEY(run,group_index));
            CREATE TABLE IF NOT EXISTS bridge_jobs(id TEXT PRIMARY KEY,created TEXT NOT NULL,status TEXT NOT NULL,manifest TEXT NOT NULL,result TEXT);
            ''')
        self._retire_ai_selection()
        import session_browser
        with self.connect() as c:session_browser.install(c)

    def _retire_ai_selection(self):
        """只迁移已采用的历史评级；保留分析记录，不重新分析或删除数据。"""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute('SELECT * FROM sessions').fetchall():
                state=json.loads(row['state'])
                if 'activeAiRunId' not in state and 'analysisMode' not in state:continue
                if state.get('analysisMode')=='ai':
                    run=c.execute('SELECT * FROM runs WHERE id=?',(state.get('activeAiRunId'),)).fetchone()
                    if not run or run['status']!='completed' or run['session']!=row['id'] or run['snapshot']!=row['snapshot']:
                        raise ValueError(f"历史 AI 评级无法迁移，请恢复有效结果后重试：{row['name']}")
                    result=json.loads(run['data'])['result']
                    records={r['id']:r for r in (json.loads(r[0]) for r in c.execute('SELECT data FROM captures WHERE session=?',(row['id'],)))}
                    reviewed={r[0] for r in c.execute('SELECT group_index FROM reviews WHERE run=?',(run['id'],))}
                    ratings={};covered=[]
                    for index,group in enumerate(result['groups']):
                        pending=(group['needs_review'] or group['confidence']<.75) and index not in reviewed
                        keep_pending=pending and result.get('basis')!='visual-contact-sheet'
                        for cid in group['ids']:
                            if cid not in records:raise ValueError('历史 AI 评级与照片索引不匹配，未修改评级。')
                            covered.append(cid)
                            auto=int(keep_pending or cid in group['keep_ids'])
                            ratings[cid]=state.get('overrides',{}).get(cid,max(auto,records[cid].get('existingRating',0)))
                    if len(covered)!=len(set(covered)) or set(covered)!=set(records):
                        raise ValueError('历史 AI 评级未完整覆盖照片索引，未修改评级。')
                    state['overrides']=ratings
                    state['scope']='all'
                state.pop('analysisMode',None);state.pop('activeAiRunId',None)
                if state.get('filter')=='review':state['filter']='all'
                c.execute('UPDATE sessions SET state=?,revision=revision+1,updated=? WHERE id=?',(stable_json(state),now(),row['id']))
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.db,timeout=30);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def active_id(self):
        with self.connect() as c:r=c.execute("SELECT value FROM settings WHERE key='active'").fetchone()
        return r[0] if r else None
    def session(self,sid=None):
        sid=sid or self.active_id()
        with self.connect() as c:r=c.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
        if not r:return None
        result=dict(r);result['state']=json.loads(result['state']);result['name']=result['name'] or folder_name(result['root']);return result
    def records(self,sid=None,ids=None):
        sid=sid or self.active_id()
        if ids is not None:
            ids=list(dict.fromkeys(ids))
            if not ids:return []
            with self.connect() as c:return [json.loads(r[0]) for r in c.execute('SELECT data FROM captures WHERE session=? AND id IN ('+','.join('?' for _ in ids)+') ORDER BY position',(sid,*ids))]
        with self.connect() as c:return [json.loads(r[0]) for r in c.execute('SELECT data FROM captures WHERE session=? ORDER BY position',(sid,))]

    def iter_records(self,sid,batch=256):
        with self.connect() as c:
            cursor=c.execute('SELECT data FROM captures WHERE session=? ORDER BY position,id',(sid,))
            while rows:=cursor.fetchmany(batch):
                yield [json.loads(r[0]) for r in rows]
    def sessions(self):
        with self.connect() as c:return [dict(r,name=r['name'] or folder_name(r['root'])) for r in c.execute('SELECT s.id,s.name,s.root,s.updated,COUNT(c.id) AS capture_count FROM sessions s LEFT JOIN captures c ON c.session=s.id WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key=\'removed_session:\'||s.id) GROUP BY s.id ORDER BY s.root COLLATE NOCASE')]
    def folder_state(self):
        with self.connect() as c:
            removed={r[0].casefold() for r in c.execute("SELECT value FROM settings WHERE key LIKE 'removed_session:%'")}
            shooting={r[0].split(':',1)[1] for r in c.execute("SELECT key FROM settings WHERE key LIKE 'shooting_session:%'")}
            archives=[r[0].split(':',1)[1] for r in c.execute("SELECT key FROM settings WHERE key LIKE 'archive_session:%'") if r[0].split(':',1)[1] not in shooting]
        with self.lock:scans=[dict(j) for j in self.jobs.values() if j['folder'].casefold() not in removed]
        return {'sessions':self.sessions(),'scans':scans,'archive_sessions':archives}
    def is_removed(self,sid):
        if not isinstance(sid,str) or not sid:return True
        with self.connect() as c:return c.execute('SELECT 1 FROM settings WHERE key=?',('removed_session:'+sid,)).fetchone() is not None
    def remove_session(self,sid):
        """只移出软件列表；保留原文件、索引和评级，允许重新添加恢复。"""
        if not isinstance(sid,str) or not sid:raise ValueError('文件夹编号无效。')
        with self.lock:
            session=self.session(sid)
            if not session:raise ValueError('文件夹不存在。')
            for job in self.jobs.values():
                if (job.get('session_id')==sid or job['folder'].casefold()==session['root'].casefold()) and job['status'] in ('queued','running','cancelling'):
                    self.scan_cancellations[job['id']].set()
                    job['status']='cancelled' if job['status']=='queued' else 'cancelling'
            with self.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('removed_session:'+sid,session['root']))
                active=c.execute("SELECT value FROM settings WHERE key='active'").fetchone()
                if active and active[0]==sid:
                    next_session=c.execute("SELECT id FROM sessions s WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key='removed_session:'||s.id) ORDER BY root COLLATE NOCASE LIMIT 1").fetchone()
                    if next_session:c.execute("UPDATE settings SET value=? WHERE key='active'",(next_session[0],))
                    else:c.execute("DELETE FROM settings WHERE key='active'")
        return self.active_id()
    def activate(self,sid):
        if not self.session(sid) or self.is_removed(sid):raise ValueError('会话不存在或已移除。')
        with self.connect() as c:c.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)",(sid,))
        if self.on_catalog:self.on_catalog(sid)
    def scan(self,folder,recursive=True,activate=True):
        return self.scan_many([folder],recursive,activate)[0]
    def scan_many(self,folders,recursive=True,activate=False):
        if not isinstance(folders,list) or not 1<=len(folders)<=50:raise ValueError('每次请添加 1–50 个文件夹。')
        normalized=[]
        for folder in folders:
            if not isinstance(folder,(str,Path)) or not str(folder).strip().strip('"'):raise ValueError('请填写照片文件夹路径。')
            # 不在请求线程访问网络盘，路径是否可读由后台索引报告。
            normalized.append(str(Path(str(folder).strip().strip('"')).absolute()))
        ids=[]
        with self.lock:
            for folder in normalized:
                with self.connect() as c:c.execute("DELETE FROM settings WHERE key LIKE 'removed_session:%' AND value=? COLLATE NOCASE",(folder,))
                existing=next((j for j in self.jobs.values() if j['folder'].casefold()==folder.casefold() and j['status'] in ('queued','running','cancelling')),None)
                if existing:ids.append(existing['id']);continue
                job=uuid.uuid4().hex;self.scan_cancellations[job]=threading.Event()
                self.jobs[job]={'id':job,'folder':folder,'name':folder_name(folder),'recursive':bool(recursive),'status':'queued','stage':'queued','done':0,'total':0,'error':None}
                self.scan_queue.append((job,folder,bool(recursive),activate));ids.append(job)
            if not self.scan_worker_running:
                self.scan_worker_running=True
                threading.Thread(target=self._scan_worker,daemon=True).start()
        return ids
    def _scan_worker(self):
        while True:
            with self.lock:
                if not self.scan_queue:self.scan_worker_running=False;return
                args=self.scan_queue.popleft()
            self._scan(*args)
    def cancel_scan(self,job):
        with self.lock:
            if job not in self.jobs:raise ValueError('索引任务不存在。')
            status=self.jobs[job]
            if status['status'] in ('queued','running'):
                self.scan_cancellations[job].set()
                status['status']='cancelled' if status['status']=='queued' else 'cancelling'
            return dict(status)
    def _scan(self,job,folder,recursive,activate=True):
        status=self.jobs[job]
        cancel=self.scan_cancellations[job]
        try:
            if cancel.is_set():raise media.ScanCancelled()
            status.update(status='running',stage='discovering')
            from file_safety import photo_directory
            photo_directory(folder,self.data)
            early={}
            exiftool=None
            def indexed_ready(base,pairs):
                nonlocal exiftool
                if not self.on_previews:return
                status['stage']='indexed_previews';status['indexed_candidates']=len(pairs)
                exiftool=media.find_exiftool(self.root)
                session=media.ExifToolSession(exiftool,timeout=3);started=time.monotonic()
                try:
                    for start in range(0,len(pairs),24):
                        if time.monotonic()-started>=3:break
                        session.timeout=max(.1,3-(time.monotonic()-started))
                        batch=pairs[start:start+24]
                        if cancel.is_set():raise media.ScanCancelled()
                        try:
                            metadata=media.read_metadata([p for p,_ in batch],exiftool,cancel=cancel,session=session)
                            records=[media.capture_record(base,p,q,m) for (p,q),m in zip(batch,metadata)]
                        except (OSError,ValueError,RuntimeError) as exc:
                            status['indexed_preview_error']=str(exc)[:160];break
                        early.update((r['id'],r) for r in records)
                        try:self.on_previews(str(base),records)
                        except Exception as exc:status['preview_cache_error']=str(exc)[:160]
                finally:session.close()
                status['stage']='discovering'
            root,shots=media.discover(folder,recursive,cancel=cancel,exclude=(self.data,),indexed=self.discovery,on_indexed=indexed_ready)
            if not shots:raise ValueError('目录中未发现支持的照片文件。')
            sid=hashlib.sha256(str(root).casefold().encode()).hexdigest()[:24];old={r['id']:r for r in self.records(sid)}
            old_paths={p.casefold():r for r in old.values() for p in (r.get('raw'),r.get('jpg')) if p}
            status.update(total=len(shots),stage='metadata');exiftool=exiftool or media.find_exiftool(self.root);records=[];pending=[]
            for primary,companion in shots:
                if cancel.is_set():raise media.ScanCancelled()
                rel=str(primary.relative_to(root)).replace('\\','/');cid=hashlib.sha256(rel.casefold().encode()).hexdigest()[:24]
                prior=old_paths.get(rel.casefold())
                if prior and 'liveRating' in prior:cid=prior['id']
                fingerprint=media.fingerprint([primary]+([companion] if companion else []))
                if cid in old and old[cid]['fingerprint']==fingerprint:records.append(old[cid]);status['done']+=1
                elif cid in early and early[cid]['fingerprint']==fingerprint:records.append(early[cid]);status['done']+=1
                else:pending.append((primary,companion))
            if records and self.on_previews:
                try:self.on_previews(str(root),records)
                except Exception as exc:status['preview_cache_error']=str(exc)[:160]
            for start in range(0,len(pending),24):
                if cancel.is_set():raise media.ScanCancelled()
                batch=pending[start:start+24];status['current']=str(batch[0][0].relative_to(root))
                meta=media.read_metadata([p for p,_ in batch],exiftool,cancel=cancel)
                for (primary,companion),m in zip(batch,meta):
                    record=media.capture_record(root,primary,companion,m);prior=old_paths.get((record.get('raw') or record['jpg']).casefold())
                    if prior and 'liveRating' in prior:record.update(id=prior['id'],liveRating=prior['liveRating'])
                    records.append(record);status['done']+=1
                if self.on_previews:
                    try:self.on_previews(str(root),records[-len(batch):])
                    except Exception as exc:status['preview_cache_error']=str(exc)[:160]
            def order(r):
                if not r['time']:return (1,0,r['id'])
                return (0,datetime.fromisoformat(r['time']).timestamp(),r['id'])
            records.sort(key=order);snapshot=hashlib.sha256(stable_json([(r['id'],r['fingerprint'],r['time']) for r in records]).encode()).hexdigest()
            if cancel.is_set():raise media.ScanCancelled()
            status['stage']='saving'
            existing=self.session(sid);state=existing['state'] if existing else {}
            unchanged={r['id'] for r in records if r['id'] in old and (r['fingerprint']==old[r['id']]['fingerprint'] or 'liveRating' in old[r['id']])}
            state['overrides']={k:v for k,v in state.get('overrides',{}).items() if k in unchanged}
            with self.lock:
                if cancel.is_set():raise media.ScanCancelled()
                with self.connect() as c:
                    c.execute('INSERT INTO sessions(id,root,name,snapshot,revision,state,updated) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET root=excluded.root,snapshot=excluded.snapshot,revision=sessions.revision+1,state=excluded.state,updated=excluded.updated',(sid,str(root),folder_name(root),snapshot,0,stable_json(state),now()))
                    c.execute('DELETE FROM captures WHERE session=?',(sid,));c.executemany('INSERT INTO captures VALUES (?,?,?,?)',[(sid,r['id'],stable_json(r),i) for i,r in enumerate(records)])
                    if activate:c.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)",(sid,))
                status.update(session_id=sid,missing_time=sum(r['missingTime'] for r in records))
            if self.on_catalog:
                try:self.on_catalog(sid)
                except Exception as exc:status['raw_sync_error']=str(exc)[:160]
            status['status']='completed'
        except media.ScanCancelled:status.update(status='cancelled',error=None)
        except (FileNotFoundError,PermissionError,OSError) as e:status.update(status='failed',error='无法读取文件夹，请检查路径、网络连接和访问权限：'+str(e)[:200])
        except Exception as e:status.update(status='failed',error=str(e)[:300])
    def save(self,sid,revision,state):
        session=self.session(sid)
        if not session or self.is_removed(sid):raise ValueError('会话不存在或已移除。')
        valid={r['id'] for r in self.records(sid)};overrides=state.get('overrides',{})
        if not isinstance(overrides,dict) or any(k not in valid or type(v) is not int or v not in range(-1,6) for k,v in overrides.items()):raise ValueError('评级记录非法。')
        if state.get('keep',1) not in (1,2) or not 0<state.get('threshold',4)<=60:raise ValueError('选片设置非法。')
        if state.get('analysisMode')=='ai':
            raise ValueError('AI 选片已移除，请刷新工作台。')
        state={k:v for k,v in state.items() if k not in ('analysisMode','activeAiRunId')}
        with self.connect() as c:
            cursor=c.execute('UPDATE sessions SET state=?,revision=revision+1,updated=? WHERE id=? AND revision=?',(stable_json(state),now(),sid,revision))
            if cursor.rowcount!=1:
                current=c.execute('SELECT revision,state FROM sessions WHERE id=?',(sid,)).fetchone()
                if current and current['state']==stable_json(state):return current['revision']
                raise ValueError('会话已被其他窗口修改，请刷新后再操作。')
        return revision+1
    def ensure_live_session(self,folder):
        root=Path(folder).resolve(strict=True)
        if not root.is_dir():raise ValueError('请选择照片传入文件夹。')
        sid=hashlib.sha256(str(root).casefold().encode()).hexdigest()[:24]
        # 将已有的有效评级冻结为基础值，追加新照片不改变原来的末帧推荐。
        ratings={r['id']:(-1 if r['excluded'] else r['rating']) for r in self.manifest(False,sid)['captures']} if self.session(sid) else {}
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO sessions VALUES (?,?,?,?,?,?,?)',(sid,str(root),folder_name(root),'',0,'{}',now()))
            c.execute('DELETE FROM settings WHERE key=?',('removed_session:'+sid,))
            for row in c.execute('SELECT id,data FROM captures WHERE session=?',(sid,)).fetchall():
                r=json.loads(row['data'])
                if 'liveRating' not in r:
                    r['liveRating']=ratings.get(r['id'],0)
                    c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(stable_json(r),sid,r['id']))
        return sid
    def append_live(self,sid,records):
        """只更新照片目录，不改会话 state/revision；人工保存可并发进行。"""
        added=[];changed=[];sidecar_changes=[]
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM sessions WHERE id=?',(sid,)).fetchone():raise ValueError('现场文件夹会话不存在。')
            records=list(records);known={}
            # 新图片/后到配对只查涉及的路径，不能为每张新图反复解码整个历史目录。
            for start in range(0,len(records),128):
                batch=records[start:start+128];ids=[r['id'] for r in batch]
                paths=list({r[k] for r in batch for k in ('raw','jpg') if r.get(k)})
                clauses=['p.id IN ('+','.join('?' for _ in ids)+')'];args=[sid,*ids]
                if paths:
                    marks=','.join('?' for _ in paths)
                    clauses.extend(('p.raw COLLATE NOCASE IN ('+marks+')','p.jpg COLLATE NOCASE IN ('+marks+')'));args.extend(paths+paths)
                for row in c.execute('SELECT c.id,c.data FROM browser_captures p JOIN captures c ON c.session=p.session AND c.id=p.id WHERE p.session=? AND ('+' OR '.join(clauses)+')',args):known[row['id']]=json.loads(row['data'])
            position=c.execute('SELECT coalesce(max(position),-1)+1 FROM browser_captures WHERE session=?',(sid,)).fetchone()[0]
            by_path={p.casefold():r for r in known.values() for p in (r.get('raw'),r.get('jpg')) if p}
            for source in records:
                r=dict(source)
                # JPG 先到、RAW 后到时沿用 JPG 的编号，人工评级无需迁移或覆盖。
                primary=r.get('raw') or r.get('jpg')
                previous=by_path.get(primary.casefold())
                if previous is None and r.get('raw') and r.get('jpg'):
                    candidate=by_path.get(r['jpg'].casefold())
                    if candidate and not candidate.get('raw'):previous=candidate
                if previous:
                    r['id']=previous['id'];r['liveRating']=previous.get('liveRating',0)
                    if r.get('existingRating')!=previous.get('existingRating'):r['liveRating']=r.get('existingRating',0)
                    if r['fingerprint']==previous['fingerprint'] and r.get('sidecarFingerprint')==previous.get('sidecarFingerprint'):continue
                    if r.get('sidecarFingerprint')!=previous.get('sidecarFingerprint') and r.get('sidecarMetadata',{}).get('Rating') is not None:
                        sidecar_changes.append(r)
                    c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(stable_json(r),sid,r['id']))
                else:
                    r['liveRating']=r.get('existingRating',0)
                    c.execute('INSERT INTO captures VALUES (?,?,?,?)',(sid,r['id'],stable_json(r),position));position+=1;added.append(r['id'])
                known[r['id']]=r
                for path in (r.get('raw'),r.get('jpg')):
                    if path:by_path[path.casefold()]=r
                changed.append(r['id'])
            if changed:
                digest=hashlib.sha256();digest.update(b'[');first=True
                for row in c.execute('SELECT id,fingerprint,time FROM browser_captures WHERE session=? ORDER BY position,id',(sid,)):
                    if not first:digest.update(b',')
                    digest.update(stable_json(tuple(row)).encode());first=False
                digest.update(b']');snapshot=digest.hexdigest()
                c.execute('UPDATE sessions SET snapshot=?,updated=? WHERE id=?',(snapshot,now(),sid))
        if sidecar_changes and getattr(self,'on_external_metadata',None):
            self.on_external_metadata(sid,sidecar_changes)
        live_callback=getattr(self,'on_live_catalog',None)
        if changed and live_callback:
            try:live_callback(sid,[known[cid] for cid in changed])
            except Exception:pass  # 已提交的增量可由后续目录/评级读取恢复。
        if changed and self.on_previews and not live_callback:
            try:self.on_previews(self.session(sid)['root'],[known[cid] for cid in changed])
            except Exception:pass  # 缓存失败不回滚已入库照片，浏览时仍可按需生成。
        if changed and self.on_catalog and not live_callback:
            try:self.on_catalog(sid)
            except Exception:pass  # 目录已提交；后台消费者可在后续请求恢复。
        return {'added':added,'changed':changed}
    def manifest(self,selected_only=True,sid=None):
        session=self.session(sid)
        if not session:raise ValueError('请先打开照片文件夹。')
        records=self.records(session['id']);state=session['state'];keep=state.get('keep',1);threshold=state.get('threshold',4);groups=[]
        for r in records:
            if not groups or r['missingTime'] or groups[-1][-1]['missingTime'] or r['cameraKey']!=groups[-1][-1]['cameraKey'] or (datetime.fromisoformat(r['time']).timestamp()-datetime.fromisoformat(groups[-1][-1]['time']).timestamp())>threshold:groups.append([])
            groups[-1].append(r)
        captures=[]
        for index,g in enumerate(groups):
            for pos,r in enumerate(g):
                auto=1 if (r['missingTime'] or pos>=len(g)-keep) else 0
                rating=state.get('overrides',{}).get(r['id'],r.get('liveRating',max(auto,r.get('existingRating',0))))
                if selected_only and rating<=0:continue
                captures.append({'id':r['id'],'relativePaths':[x for x in (r.get('raw'),r.get('jpg')) if x],'rating':max(0,rating),'excluded':rating<0,'group':index+1,'fingerprint':r['fingerprint'],'pendingReview':False})
        return {'schema':'framekeep.selection','version':2,'demo':False,'createdAt':now(),'source':{'sessionId':session['id'],'root':session['root'],'pathAliases':media.path_aliases(session['root']),'folderName':session['name'],'snapshot':session['snapshot'],'captureCount':len(records)},'applyPolicy':{'mode':'selected-only' if selected_only else 'include-unselected','protectExistingRatingAtLeast':2,'clearUnlisted':False},'captures':captures}
