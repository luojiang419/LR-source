"""现场新图、全库自动和按次手动解析，共用持久任务、结果和真实进度。"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from asset_analysis import vision
from sku_vision import recognize
from retrieval_schema import PROMPT,SCHEMA,dumps,digest,parse_result


def install(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS workflow_settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS workflow_runs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,started REAL,finished REAL,total INTEGER DEFAULT 0,done INTEGER DEFAULT 0,failed INTEGER DEFAULT 0,discovered INTEGER DEFAULT 0,catalog_limit INTEGER DEFAULT 0,session TEXT DEFAULT '',capture_limit INTEGER DEFAULT 0,cursor INTEGER DEFAULT 0,capture_cursor INTEGER DEFAULT -1);
CREATE TABLE IF NOT EXISTS workflow_jobs(id TEXT PRIMARY KEY,kind TEXT,source TEXT,source_id TEXT,session TEXT,fingerprint TEXT,config TEXT,status TEXT,result TEXT DEFAULT '',input_sha TEXT DEFAULT '',reason TEXT DEFAULT '',created REAL,updated REAL,asset TEXT DEFAULT '',operation TEXT DEFAULT '',UNIQUE(kind,source,source_id,session,fingerprint,config));
CREATE INDEX IF NOT EXISTS workflow_queue ON workflow_jobs(status,created);
CREATE INDEX IF NOT EXISTS workflow_content ON workflow_jobs(kind,config,input_sha,status);
CREATE INDEX IF NOT EXISTS workflow_capture ON workflow_jobs(source,session,source_id,created DESC);
CREATE INDEX IF NOT EXISTS workflow_asset ON workflow_jobs(asset,created DESC);
CREATE INDEX IF NOT EXISTS workflow_latest ON workflow_jobs(kind,created DESC);
CREATE INDEX IF NOT EXISTS workflow_kind_status ON workflow_jobs(kind,status);
CREATE TABLE IF NOT EXISTS workflow_members(run TEXT,job TEXT,PRIMARY KEY(run,job));
CREATE TABLE IF NOT EXISTS workflow_inbox(identity TEXT PRIMARY KEY,session TEXT,record TEXT);
CREATE TABLE IF NOT EXISTS workflow_catalog_dirty(item TEXT PRIMARY KEY);''')
    if 'name' not in {r['name'] for r in c.execute('PRAGMA table_info(workflow_jobs)')}:
        c.execute("ALTER TABLE workflow_jobs ADD COLUMN name TEXT NOT NULL DEFAULT ''")
    if 'task_hidden' not in {r['name'] for r in c.execute('PRAGMA table_info(workflow_jobs)')}:
        c.execute('ALTER TABLE workflow_jobs ADD COLUMN task_hidden INTEGER NOT NULL DEFAULT 0')
    if 'source_sha' not in {r['name'] for r in c.execute('PRAGMA table_info(workflow_jobs)')}:
        c.execute("ALTER TABLE workflow_jobs ADD COLUMN source_sha TEXT NOT NULL DEFAULT ''")
    c.executescript('''CREATE INDEX IF NOT EXISTS workflow_visible ON workflow_jobs(kind,task_hidden,status);
CREATE INDEX IF NOT EXISTS workflow_visible_latest ON workflow_jobs(kind,task_hidden,created DESC);
CREATE INDEX IF NOT EXISTS workflow_member_job ON workflow_members(job,run);
CREATE TRIGGER IF NOT EXISTS workflow_job_progress AFTER UPDATE OF status ON workflow_jobs WHEN old.status<>new.status BEGIN
 UPDATE workflow_runs SET done=done+(new.status IN ('completed','failed','cancelled'))-(old.status IN ('completed','failed','cancelled')),
 failed=failed+(new.status='failed')-(old.status='failed')
 WHERE (status='running' OR (status='stopped' AND id IN (SELECT value FROM workflow_settings WHERE key IN ('resume_ocr','resume_vision'))))
 AND id IN (SELECT run FROM workflow_members WHERE job=new.id); END;''')
    trigger=c.execute("SELECT sql FROM sqlite_master WHERE name='workflow_job_progress'").fetchone()[0]
    previous="WHERE status='running' AND id IN"
    if previous in trigger:
        current="WHERE (status='running' OR (status='stopped' AND id IN (SELECT value FROM workflow_settings WHERE key IN ('resume_ocr','resume_vision')))) AND id IN"
        c.execute('SAVEPOINT workflow_stop_progress_upgrade')
        c.execute('DROP TRIGGER workflow_job_progress')
        c.execute(trigger.replace(previous,current))
        c.execute('RELEASE workflow_stop_progress_upgrade')


class AnalysisWorkflow:
    def __init__(self,analysis,intelligence,sku,monitor=lambda:{},*,autostart=False):
        self.analysis=analysis;self.intelligence=intelligence;self.sku=sku;self.store=sku.store
        self.catalog=intelligence.catalog;self.archive=sku.archive;self.providers=analysis.providers
        self.scheduler=analysis.scheduler;self.closed=threading.Event();self.lock=threading.RLock()
        self.catalog.metadata.scheduler=self.scheduler
        self.content_condition=threading.Condition();self.content_active=set()
        self.active_lock=threading.Lock();self.active={};self.timings={};self.manual_kinds=set()
        self.progress={};self.progress_epoch=0;self.error='';self.global_cursor=0;self.monitor_seed=None
        with analysis.connect() as c:
            install(c)
            c.execute("UPDATE workflow_jobs SET status='queued' WHERE status='running'")
            saved=c.execute("SELECT value FROM workflow_settings WHERE key='config'").fetchone()
            cursor=c.execute("SELECT value FROM workflow_settings WHERE key='cursor'").fetchone()
            model_key=c.execute("SELECT value FROM workflow_settings WHERE key='model_key'").fetchone()
        self.global_cursor=int(cursor[0]) if cursor else 0;self.global_model_key=model_key[0] if model_key else ''
        self.config={'live_enabled':False,'global_enabled':False,'session_id':None,'ocr_paused':False,'vision_paused':False,**(json.loads(saved[0]) if saved else {})}
        self.monitor=monitor
        self.thread=threading.Thread(target=self._run,name='analysis-workflow-discovery',daemon=True)
        self.workers=[threading.Thread(target=self._worker,name='analysis-workflow-'+str(i),daemon=True) for i in range(2)]
        self.started=False
        self.refresh_progress()
        if autostart:self.start()

    def start(self):
        with self.lock:
            if self.started or self.closed.is_set():return
            self.started=True
            for worker in tuple(self.workers):worker.start()
            self.thread.start()

    def _provider(self,kind):
        p=(self.providers.selected('ocr') if kind=='ocr' else self.providers.purpose('asset_analysis') or self.providers.selected('multimodal'))
        if not p:raise ValueError('请先在 AI 与模型选用'+(' OCR' if kind=='ocr' else '多模态')+'模型')
        self.providers.check_inference(p);return p

    def _key(self,kind):
        p=self._provider(kind)
        # 卡片 UUID 属于本机；相同模型/接口/维度在另一台设备仍可复用断点。
        return digest([kind,{k:p.get(k) for k in ('model','protocol','url','image_size','max_tokens')},self.intelligence.config['custom_dimensions']])

    def _session(self):
        state=self.monitor() or {}
        return state.get('session_id') if state.get('enabled') else self.config.get('session_id')

    def configure(self,body):
        with self.lock:
            next_config=dict(self.config)
            for key in ('live_enabled','global_enabled','ocr_paused','vision_paused'):
                if key in body:
                    if type(body[key]) is not bool:raise ValueError('解析开关无效')
                    next_config[key]=body[key]
            if body.get('session_id'):
                if not self.store.session(body['session_id']):raise ValueError('请先打开照片目录')
                next_config['session_id']=body['session_id']
            if any(next_config[key] and not self.config[key] for key in ('live_enabled','global_enabled')):self._provider('vision')
            if next_config['live_enabled'] and not next_config.get('session_id') and not self._session():raise ValueError('请先打开或监控拍摄目录')
            if next_config['global_enabled'] and not self.config['global_enabled']:self.global_cursor=0;self.monitor_seed=None
            with self.analysis.connect() as c:c.execute("INSERT OR REPLACE INTO workflow_settings VALUES('config',?)",(dumps(next_config),))
            self.config=next_config
        self._sync_background_priority()
        return self.state()

    def changed(self,sid,records=None):
        if not (self.config['live_enabled'] or self.config['global_enabled']) or sid!=self._session() or not records:return
        # 收图回调只写持久入口，不读取模型、不解码、不等待推理。
        with self.analysis.connect() as c:c.executemany('INSERT OR REPLACE INTO workflow_inbox VALUES (?,?,?)',[(sid+':'+r['id'],sid,dumps(r)) for r in records])

    def indexed(self,records):
        if not self.config['global_enabled']:return
        with self.analysis.connect() as c:c.executemany('INSERT OR IGNORE INTO workflow_catalog_dirty VALUES (?)',[(r['id'].removeprefix('catalog:'),) for r in records if r.get('_catalog')])

    def enqueue(self,kind,source,records,sid='',run=''):
        config=self._key(kind);now=time.time()
        with self.analysis.connect() as c:
            for record in records:
                identity=record['id'];fingerprint=record['fingerprint']
                jid=digest([kind,source,identity,sid,fingerprint,config])
                c.execute("INSERT OR IGNORE INTO workflow_jobs(id,kind,source,source_id,session,fingerprint,config,status,created,updated) VALUES (?,?,?,?,?,?,?,'queued',?,?)",(jid,kind,source,identity,sid,fingerprint,config,now,now))
                if record.get('name') or record.get('jpg') or record.get('raw'):
                    c.execute('UPDATE workflow_jobs SET name=? WHERE id=?',(record.get('jpg') or record.get('raw') or record['name'],jid))
                if run:
                    c.execute("UPDATE workflow_jobs SET status='queued',reason='' WHERE id=? AND status IN ('failed','cancelled')",(jid,))
                    added=c.execute('INSERT OR IGNORE INTO workflow_members VALUES (?,?)',(run,jid)).rowcount
                    if added:
                        status=c.execute('SELECT status FROM workflow_jobs WHERE id=?',(jid,)).fetchone()[0]
                        c.execute('UPDATE workflow_runs SET total=total+1,done=done+?,failed=failed+? WHERE id=?',(int(status in ('completed','failed','cancelled')),int(status=='failed'),run))
                    c.execute('UPDATE workflow_jobs SET task_hidden=0 WHERE id=?',(jid,))

    def execute(self,kind,session_id=None):
        if kind not in ('ocr','vision'):raise ValueError('请选择 OCR 或多维度解析')
        self._provider(kind)
        config_key=self._key(kind)
        monitor=self.monitor() or {}
        sid=(monitor.get('session_id') if monitor.get('enabled') else session_id or self._session()) or ''
        with self.lock:
            next_config={**self.config,kind+'_paused':False}
            with self.analysis.connect() as c:
                if c.execute("SELECT 1 FROM workflow_runs WHERE kind=? AND status='running'",(kind,)).fetchone():raise ValueError('该项任务正在执行')
                if self.config[kind+'_paused'] and c.execute("SELECT 1 FROM workflow_jobs WHERE kind=? AND status='running'",(kind,)).fetchone():
                    raise ValueError('正在停止，请等待当前请求收尾后继续')
                saved=c.execute('SELECT value FROM workflow_settings WHERE key=?',('resume_'+kind,)).fetchone()
                previous=c.execute("SELECT id FROM workflow_runs WHERE id=? AND kind=? AND status='stopped'",(saved[0],kind)).fetchone() if saved else None
                if previous and c.execute('''SELECT 1 FROM workflow_members m JOIN workflow_jobs j ON j.id=m.job
                    WHERE m.run=? AND j.config<>? LIMIT 1''',(previous['id'],config_key)).fetchone():previous=None
                if previous:
                    counts=c.execute("""SELECT coalesce(sum(j.status IN ('completed','failed','cancelled')),0),coalesce(sum(j.status='failed'),0)
                        FROM workflow_members m JOIN workflow_jobs j ON j.id=m.job WHERE m.run=?""",(previous['id'],)).fetchone()
                    c.execute("UPDATE workflow_runs SET status='running',finished=NULL,done=?,failed=? WHERE id=?",(*counts,previous['id']))
                else:
                    with self.catalog.connect() as db:maximum=db.execute('SELECT COALESCE(MAX(rowid),0) FROM items').fetchone()[0]
                    with self.store.connect() as db:limit=db.execute('SELECT COALESCE(MAX(position),-1) FROM captures WHERE session=?',(sid,)).fetchone()[0]
                    c.execute("INSERT INTO workflow_runs(id,kind,status,started,catalog_limit,session,capture_limit) VALUES (?,?,'running',?,?,?,?)",(uuid.uuid4().hex,kind,time.time(),maximum,sid,limit))
                c.execute('DELETE FROM workflow_settings WHERE key=?',('resume_'+kind,))
                c.execute("INSERT OR REPLACE INTO workflow_settings VALUES('config',?)",(dumps(next_config),))
            self.config=next_config
            self.progress_epoch+=1
        self.refresh_progress()
        return self.state()

    def stop(self,kind):
        if kind not in ('ocr','vision'):raise ValueError('请选择 OCR 或多维度解析')
        with self.lock:
            next_config={**self.config,kind+'_paused':True}
            with self.analysis.connect() as c:
                running=c.execute("SELECT id FROM workflow_runs WHERE kind=? AND status='running' ORDER BY started LIMIT 1",(kind,)).fetchone()
                if running:c.execute('INSERT OR REPLACE INTO workflow_settings VALUES(?,?)',('resume_'+kind,running['id']))
                c.execute("INSERT OR REPLACE INTO workflow_settings VALUES('config',?)",(dumps(next_config),))
                c.execute("UPDATE workflow_runs SET status='stopped',finished=? WHERE kind=? AND status='running'",(time.time(),kind))
            self.config=next_config
            self.progress_epoch+=1
        self.refresh_progress()
        return self.state()

    def discover_tick(self):
        if self.config['live_enabled'] or self.config['global_enabled']:
            with self.analysis.connect() as c:
                inbox=c.execute('SELECT * FROM workflow_inbox'+('' if self.config['global_enabled'] else ' WHERE session=?')+' LIMIT 128',() if self.config['global_enabled'] else (self._session(),)).fetchall()
            groups={}
            for entry in inbox:groups.setdefault(entry['session'],[]).append(json.loads(entry['record']))
            for sid,records in groups.items():self.enqueue('vision','capture',records,sid)
            with self.analysis.connect() as c:c.executemany('DELETE FROM workflow_inbox WHERE identity=? AND record=?',[(r['identity'],r['record']) for r in inbox])
        with self.analysis.connect() as c:runs=[dict(r) for r in c.execute("SELECT * FROM workflow_runs WHERE status='running' AND discovered=0")]
        for run in runs:
            # 已有足够持久任务时只消费；不要持续写整库队列抢占识别与结果提交。
            if self.config[run['kind']+'_paused'] or run['total']-run['done']>=256:continue
            sid=run['session']
            if sid and run['capture_cursor']<run['capture_limit']:
                with self.store.connect() as c:rows=c.execute('SELECT position,data FROM captures WHERE session=? AND position>? AND position<=? ORDER BY position LIMIT 128',(sid,run['capture_cursor'],run['capture_limit'])).fetchall()
                self.enqueue(run['kind'],'capture',[json.loads(r['data']) for r in rows],sid,run['id'])
                with self.analysis.connect() as c:c.execute('UPDATE workflow_runs SET capture_cursor=? WHERE id=?',(rows[-1]['position'] if rows else run['capture_limit'],run['id']))
                continue
            with self.catalog.connect() as c:rows=[dict(r) for r in c.execute("SELECT rowid AS ordinal,id,fingerprint FROM items WHERE rowid>? AND rowid<=? AND online=1 AND media_type='image' AND root IN (SELECT id FROM roots WHERE removed=0) ORDER BY rowid LIMIT 128",(run['cursor'],run['catalog_limit']))]
            self.enqueue(run['kind'],'catalog',rows,run=run['id'])
            with self.analysis.connect() as c:c.execute('UPDATE workflow_runs SET cursor=?,discovered=? WHERE id=?',(rows[-1]['ordinal'] if rows else run['catalog_limit'],int(not rows),run['id']))
        if self.config['global_enabled']:
            model_key=self._key('vision')
            if model_key!=self.global_model_key:self.global_cursor=0;self.monitor_seed=None;self.global_model_key=model_key
            sid=self._session()
            if sid and self.monitor_seed!=sid:
                for batch in self.store.iter_records(sid):
                    if self.closed.is_set() or not self.config['global_enabled']:break
                    self.enqueue('vision','capture',batch,sid)
                self.monitor_seed=sid
            with self.catalog.connect() as c:rows=[dict(r) for r in c.execute("SELECT rowid AS ordinal,id,fingerprint FROM items WHERE rowid>? AND online=1 AND media_type='image' AND root IN (SELECT id FROM roots WHERE removed=0) ORDER BY rowid LIMIT 128",(self.global_cursor,))]
            if rows:self.enqueue('vision','catalog',rows);self.global_cursor=rows[-1]['ordinal']
            with self.analysis.connect() as c:
                c.execute("INSERT OR REPLACE INTO workflow_settings VALUES('cursor',?)",(str(self.global_cursor),))
                c.execute("INSERT OR REPLACE INTO workflow_settings VALUES('model_key',?)",(model_key,))
                ids=[r[0] for r in c.execute('SELECT item FROM workflow_catalog_dirty LIMIT 128')]
            if ids:
                with self.catalog.connect() as c:changed=[dict(r) for r in c.execute("SELECT id,fingerprint FROM items WHERE id IN ("+','.join('?' for _ in ids)+") AND online=1 AND media_type='image'",ids)]
                self.enqueue('vision','catalog',changed)
                with self.analysis.connect() as c:c.executemany('DELETE FROM workflow_catalog_dirty WHERE item=?',[(item,) for item in ids])
        self.refresh_progress()
        # 老文本逐项提交；慢盘下不能持有整批写事务阻塞新结果与进度首帧。
        from catalog_ocr import backfill
        with self.catalog.connect() as c:backfill(c,self.sku.sku_rules(),limit=1)

    def refresh_progress(self):
        # 完成数由任务状态触发器增量维护；轮询不全表关联，也不把旧
        # 快照写回计数，避免并发完成时覆盖新计数。
        epoch=self.progress_epoch
        with self.analysis.connect() as c:
            c.execute("UPDATE workflow_runs SET status='completed',finished=? WHERE status='running' AND discovered=1 AND done=total",(time.time(),))
            runs=[dict(r) for r in c.execute("""SELECT * FROM workflow_runs ORDER BY CASE
                WHEN status='running' THEN 0 WHEN id IN (SELECT value FROM workflow_settings WHERE key IN ('resume_ocr','resume_vision')) THEN 1
                ELSE 2 END,started DESC LIMIT 20""")]
            manual_kinds={r[0] for r in c.execute("SELECT DISTINCT kind FROM workflow_runs WHERE status='running'")}
            progress={}
            for run in runs:
                if run['kind'] in progress:continue
                run['percent']=min(99,int(run['done']/run['total']*100)) if run['status']=='running' and run['total'] else 100 if run['status']=='completed' else 0
                run['elapsed_seconds']=(run['finished'] or time.time())-run['started']
                progress[run['kind']]=run
        with self.lock:
            if epoch!=self.progress_epoch:return
            self.progress=progress;self.manual_kinds=manual_kinds
        self._sync_background_priority()

    def _sync_background_priority(self):
        with self.lock:
            pending=any(not self.config[kind+'_paused'] for kind in self.manual_kinds)
        with self.active_lock:pending=pending or bool(self.active)
        self.scheduler.order_workflow(self,pending and not self.closed.is_set())

    def _allowed(self,c,job):
        manual=c.execute("SELECT 1 FROM workflow_members m JOIN workflow_runs r ON r.id=m.run WHERE m.job=? AND r.status='running'",(job['id'],)).fetchone()
        return bool(manual or job['kind']=='vision' and (self.config['global_enabled'] or self.config['live_enabled'] and job['source']=='capture' and job['session']==self._session()))

    @contextmanager
    def content_slot(self,key,valid):
        with self.content_condition:
            while key in self.content_active:
                if not valid():raise ValueError('任务已暂停，保留断点')
                self.content_condition.wait(.1)
            if not valid():raise ValueError('任务已暂停，保留断点')
            self.content_active.add(key)
        try:yield
        finally:
            with self.content_condition:self.content_active.discard(key);self.content_condition.notify_all()

    def _phase(self,job,phase):
        with self.active_lock:
            previous=self.active.get(job['id'],{})
            self.active[job['id']]={'kind':job['kind'],'name':job.get('name') or previous.get('name') or job['source_id'],
                                   'phase':phase,'since':time.monotonic()}
        self._sync_background_priority()

    def _timing(self,stage,elapsed):
        with self.active_lock:
            old=self.timings.get(stage,{'count':0,'seconds':0,'maximum_seconds':0})
            self.timings[stage]={'count':old['count']+1,'seconds':old['seconds']+elapsed,'last_seconds':elapsed,
                                 'maximum_seconds':max(old['maximum_seconds'],elapsed)}

    def _ocr_input(self,job,provider,reused):
        import media_engine as media
        from ocr_input import OcrInput,record_input,catalog_input
        progress=lambda phase:self._phase(job,phase)
        if job['source']=='catalog':
            row=self.catalog.item(job['source_id']);self.catalog._root(row['root'])
            if not row['online'] or row['fingerprint']!=job['fingerprint']:raise ValueError('归档图片已变化，请重新执行')
            job['name']=row['name'];progress('cached_result' if reused else 'cache_lookup')
            return row,row['name'],catalog_input(self.catalog,row,provider.get('image_size',1600),result_only=reused,progress=progress)
        records=self.store.records(job['session'],ids=[job['source_id']])
        if not records or records[0]['fingerprint']!=job['fingerprint'] or self.store.is_removed(job['session']):raise ValueError('照片已变化或目录已移除，请重新执行')
        record=records[0];name=record.get('jpg') or record.get('raw') or job['source_id'];job['name']=name
        session=self.store.session(job['session']);path=Path(session['root'])/name
        with self.catalog.connect() as c:linked=c.execute('SELECT * FROM items WHERE path=? AND online=1',(str(path),)).fetchone()
        prepared=OcrInput(b'',True) if reused else record_input(record,session['root'],self.store.data/'previews',lambda:media.find_exiftool(self.store.root),provider.get('image_size',1600),progress=progress)
        if reused:progress('cached_result')
        return dict(linked) if linked else None,name,prepared

    def resolve(self,job,provider,input_sha,payload,valid):
        # 监控目录同时也在资产归档时，两个来源可能并发遇到同一图片。
        # 仅相同内容共用在途槽，结果提交后复用；不同图片不共用条带锁。
        self._phase(job,'waiting_content')
        with self.content_slot((job['kind'],job['config'],input_sha),valid):
            with self.analysis.connect() as c:prior=c.execute("SELECT result FROM workflow_jobs WHERE kind=? AND config=? AND input_sha=? AND result!='' AND status IN ('completed','running','failed') LIMIT 1",(job['kind'],job['config'],input_sha)).fetchone()
            if prior:return json.loads(prior[0])
            self._phase(job,'waiting_model')
            with self.providers.inference_slot(provider,cancel=self.closed,valid=valid,background=True):
                self._phase(job,'waiting_stage')
                with self.scheduler.slot(cancel=self.closed,valid=valid,stage='ocr' if job['kind']=='ocr' else 'vision',priority=True,independent=provider.get('protocol')!='local'):
                    if not valid():raise ValueError('任务已暂停，保留断点')
                    self._phase(job,'inference')
                    started=time.monotonic()
                    if job['kind']=='ocr':value=recognize(provider,payload)
                    else:value=parse_result(vision(provider,payload,PROMPT+'\n额外自定义维度：'+dumps(self.intelligence.config['custom_dimensions']),SCHEMA),self.intelligence.config['custom_dimensions'])
                    self._timing(job['kind']+'_inference',time.monotonic()-started)
            self._phase(job,'saving_response')
            with self.analysis.connect() as c:c.execute("UPDATE workflow_jobs SET result=?,input_sha=? WHERE id=? AND status='running'",(dumps(value),input_sha,job['id']))
            return value

    def tick(self):
        with self.lock,self.analysis.connect() as c:
            row=c.execute("""SELECT * FROM workflow_jobs j WHERE status='queued' AND NOT ((kind='ocr' AND ?) OR (kind='vision' AND ?)) AND
                ((kind='vision' AND (? OR (? AND source='capture' AND session=?))) OR
                 EXISTS(SELECT 1 FROM workflow_members m JOIN workflow_runs r ON r.id=m.run WHERE m.job=j.id AND r.status='running'))
                ORDER BY EXISTS(SELECT 1 FROM workflow_members m JOIN workflow_runs r ON r.id=m.run WHERE m.job=j.id AND r.status='running') DESC,
                (source='capture') DESC,created LIMIT 1""",(self.config['ocr_paused'],self.config['vision_paused'],self.config['global_enabled'],self.config['live_enabled'],self._session())).fetchone()
            job=dict(row) if row else None
            if not job:return False
            c.execute("UPDATE workflow_jobs SET status='running',updated=? WHERE id=?",(time.time(),job['id']))
        save_started=None
        try:
            self._phase(job,'image')
            provider=self._provider(job['kind'])
            if self._key(job['kind'])!=job['config']:raise ValueError('模型或解析维度已变化，请重新执行')
            row=None;aid='';source_sha='';ocr_input=None
            reused=job['kind']=='ocr' and bool(job['result'] and job['input_sha'])
            if job['kind']=='ocr':
                started=time.monotonic();row,name,ocr_input=self._ocr_input(job,provider,reused);payload=ocr_input.payload
                self._timing('reused_result' if reused else 'cached_input' if ocr_input.cached else 'source_input',time.monotonic()-started)
            elif job['source']=='capture':
                records=self.store.records(job['session'],ids=[job['source_id']])
                if not records or records[0]['fingerprint']!=job['fingerprint']:raise ValueError('照片已变化，请重新执行')
                name=records[0].get('jpg') or records[0].get('raw') or job['source_id']
                job['name']=name;self._phase(job,'image')
                # 批量 OCR/视觉都只读原片；注册资产可能排入元数据写回，不能用于已有 NAS。
                import media_engine as media
                session=self.store.session(job['session']);path=media.resolved_file(session['root'],name)
                before=path.stat()
                payload=media.preview_bytes(records[0],session['root'],self.store.data/'previews',lambda:media.find_exiftool(self.store.root),provider.get('image_size',1600),False)
                with self.catalog.connect() as c:linked=c.execute('SELECT * FROM items WHERE path=? AND online=1',(str(path),)).fetchone()
                row=dict(linked) if linked else None
            else:
                row=self.catalog.item(job['source_id'])
                if not row['online'] or row['fingerprint']!=job['fingerprint']:raise ValueError('归档图片已变化，请重新执行')
                job['name']=row['name'];self._phase(job,'image')
                self.catalog._root(row['root']);path=Path(row['path']);before=path.stat()
                payload=self.catalog.preview(row['id'],size=provider.get('image_size',1600),read=True)
                name=row['name']
            if ocr_input is None:
                self._phase(job,'validating');after=path.stat()
                source_version=(before.st_size,before.st_mtime_ns)
                if source_version!=(after.st_size,after.st_mtime_ns):raise ValueError('读取期间源图片发生变化，未保存旧结果')
                if row and (after.st_size!=row['size'] or abs(after.st_mtime-row['modified'])>.001):raise ValueError('归档源图已变化，请刷新索引')
                from metadata_writer import sha
                source_sha=sha(path)
            input_sha=job['input_sha'] if reused else hashlib.sha256(payload).hexdigest()
            def valid():
                if self.closed.is_set() or self.config[job['kind']+'_paused']:return False
                with self.analysis.connect() as c:
                    current=c.execute('SELECT status FROM workflow_jobs WHERE id=?',(job['id'],)).fetchone()
                    return bool(current and current[0]=='running' and self._allowed(c,job))
            value=json.loads(job['result']) if reused else self.resolve(job,provider,input_sha,payload,valid)
            self._phase(job,'validating')
            if ocr_input is not None:
                ocr_input.validate(self.catalog)
                if job['source']=='capture':
                    current_records=self.store.records(job['session'],ids=[job['source_id']])
                    if not current_records or current_records[0]['fingerprint']!=job['fingerprint']:raise ValueError('照片版本已变化，未保存旧 OCR 结果')
            else:
                current_stat=path.stat()
                if (current_stat.st_size,current_stat.st_mtime_ns)!=source_version:raise ValueError('解析期间源图片发生变化，未保存旧结果')
            with self.analysis.connect() as c:
                current=c.execute('SELECT status FROM workflow_jobs WHERE id=?',(job['id'],)).fetchone()
                if not current or current[0]!='running':return True
            self._phase(job,'saving');save_started=time.monotonic();operation=''
            if job['kind']=='vision':
                if row:
                    import retrieval_store
                    from catalog_ocr import verify_visual
                    with self.catalog.connect() as c:
                        current=c.execute('SELECT fingerprint,online FROM items WHERE id=?',(row['id'],)).fetchone()
                        if not current or not current['online'] or current['fingerprint']!=row['fingerprint']:raise ValueError('归档版本已变化，未保存旧视觉结果')
                        retrieval_store.save_result(c,row['id'],row['fingerprint'],job['config'],provider['model'],[{'seconds':0,'result':value}],[])
                        c.execute('INSERT OR REPLACE INTO intelligence_file_fingerprints VALUES(?,?,?,?,?,?)',(row['id'],row['fingerprint'],input_sha,source_sha,'vision',time.time()))
                        verify_visual(c,row,value,self.sku.sku_rules())
            elif job['source']=='capture':
                from ocr_metadata import record_ocr
                with self.sku.connect() as c:record_ocr(c,'workflow',job['id'],job['session'],job['source_id'],value,time.time())
            if job['kind']=='ocr' and row:
                from catalog_ocr import save_ocr
                with self.catalog.connect() as c:save_ocr(c,row,self.intelligence._ocr_key(provider),provider['model'],[{'seconds':0,'reading':value}],self.sku.sku_rules(),input_sha)
            with self.analysis.connect() as c:c.execute("UPDATE workflow_jobs SET status='completed',result=?,input_sha=?,source_sha=?,asset=?,operation=?,reason='',name=?,updated=? WHERE id=? AND status='running'",(dumps(value),input_sha,source_sha,aid,operation,name,time.time(),job['id']))
        except Exception as exc:
            with self.analysis.connect() as c:
                suspended=self.closed.is_set() or self.config[job['kind']+'_paused'] or not self._allowed(c,job)
                busy=isinstance(exc,sqlite3.OperationalError) and (getattr(exc,'sqlite_errorcode',0)&255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED) or 'locked' in str(exc).lower())
                c.execute("UPDATE workflow_jobs SET status=?,reason=?,updated=? WHERE id=? AND status='running'",('queued' if suspended or busy else 'failed',str(exc)[:400],time.time(),job['id']))
        finally:
            if save_started is not None:self._timing('save_attempt',time.monotonic()-save_started)
            with self.active_lock:self.active.pop(job['id'],None)
            self._sync_background_priority()
        return True

    def state(self):
        with self.active_lock:
            active=[{k:v for k,v in entry.items() if k!='since'}|{'elapsed_seconds':max(0,time.monotonic()-entry['since'])} for entry in self.active.values()]
            timings={key:dict(value) for key,value in self.timings.items()}
        with self.lock:state={'config':dict(self.config),'runs':dict(self.progress),'error':self.error,'active':active,'timings':timings}
        if self.archive.catalog_writeback:state['sku_writeback']=self.archive.catalog_writeback.state()
        return state

    def tasks(self,kind):
        with self.analysis.connect() as c:
            jobs=[dict(r) for r in c.execute('SELECT * FROM workflow_jobs WHERE kind=? AND task_hidden=0 ORDER BY created DESC LIMIT 100',(kind,))]
            counts=dict(c.execute('SELECT status,count(*) FROM workflow_jobs WHERE kind=? AND task_hidden=0 GROUP BY status',(kind,)))
        for job in jobs:
            job.update(id='workflow:'+job['id'],name=job['name'] or job['source_id'],trigger='manual' if not self.config['global_enabled'] else 'auto',schema_id='多维度解析' if kind=='vision' else 'OCR',schema_version=1)
        return jobs,counts

    def clear_completed_tasks(self):
        with self.analysis.connect() as c:c.execute("UPDATE workflow_jobs SET task_hidden=1 WHERE status IN ('completed','failed','cancelled')")

    def control_job(self,action,jid):
        if action not in ('retry','cancel'):raise ValueError('解析任务操作无效')
        jid=jid.removeprefix('workflow:')
        with self.analysis.connect() as c:
            job=c.execute('SELECT * FROM workflow_jobs WHERE id=?',(jid,)).fetchone()
            if not job:raise ValueError('任务不存在')
            c.execute('UPDATE workflow_jobs SET status=?,reason=? WHERE id=?',('queued' if action=='retry' else 'cancelled','',jid))
            if action=='retry':
                rid=uuid.uuid4().hex
                c.execute("INSERT INTO workflow_runs(id,kind,status,started,total,discovered) VALUES (?,?,'running',?,1,1)",(rid,job['kind'],time.time()))
                c.execute('INSERT INTO workflow_members VALUES (?,?)',(rid,jid))
        self.refresh_progress()

    def _run(self):
        while not self.closed.is_set():
            try:
                self.discover_tick()
                capacity=max([1]+[self.providers.concurrency(self._provider(kind)) for kind in ('vision','ocr') if self.progress.get(kind,{}).get('status')=='running' or kind=='vision' and (self.config['live_enabled'] or self.config['global_enabled'])])
                # 少量额外工作线程准备下一张、保存上一张，模型槽仍由卡片并发限制。
                desired=max(2,capacity+min(capacity,4))
                while len(self.workers)<desired and not self.closed.is_set():
                    worker=threading.Thread(target=self._worker,name='analysis-workflow-'+str(len(self.workers)),daemon=True);self.workers.append(worker);worker.start()
                self.error=''
            except Exception as exc:self.error=str(exc)[:300]
            self.closed.wait(.5)

    def _worker(self):
        while not self.closed.is_set():
            try:worked=self.tick()
            except Exception:worked=False
            if not worked:self.closed.wait(.2)

    def close(self):
        self.closed.set()
        self.scheduler.order_workflow(self,False)
        deadline=time.monotonic()+2
        for thread in [self.thread,*self.workers]:
            if thread.is_alive() and thread is not threading.current_thread():thread.join(timeout=max(0,deadline-time.monotonic()))
