"""独立 SKU 对账库、二星标签队列、冻结工作表范围及可追溯复核。"""
from contextlib import contextmanager,nullcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from datetime import datetime,timezone

import media_engine as media
from sku_match import match,normalize,match_automatic
from full_auto_ocr import FullAutoOCR
from ocr_metadata import OcrMetadata,initialize_ocr,record_ocr
from sku_vision import recognize
from sku_rules import SkuRules
from sku_workbook import inspect_workbook,export_workbook,read_image
from sku_progress import capture_time,statistics,completion_order
from label_concurrency import ConcurrentLabels
from ocr_progress import OcrProgress


class SkuService(ConcurrentLabels,FullAutoOCR,OcrMetadata,SkuRules):
    RETRY_DELAY=1.2

    def __init__(self,store,providers,*,recognizer=recognize,clock=time.time,autostart=True,archive=None,scheduler=None,require_sku_choice=False):
        self.store=store;self.providers=providers;self.recognizer=recognizer;self.clock=clock
        self.archive=archive;self.scheduler=scheduler
        self.require_sku_choice=require_sku_choice
        self.ocr_progress=OcrProgress()
        self.db=store.data/'sku.sqlite3';self.root=store.data/'sku-projects';self.root.mkdir(exist_ok=True)
        self.lock=threading.RLock();self.closed=threading.Event();self.wake=threading.Event();self.dirty=set()
        self._init_label_workers()
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT,metadata TEXT,session_id TEXT,scope TEXT NOT NULL DEFAULT '',enabled INTEGER,created REAL,export_error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS sku_rows(id TEXT PRIMARY KEY,project TEXT,sheet TEXT,excel_row INTEGER,sku TEXT,normalized TEXT,baseline TEXT,status TEXT,UNIQUE(project,sheet,excel_row));
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,project TEXT,session_id TEXT,capture_id TEXT,fingerprint TEXT,scope TEXT,status TEXT,due REAL,created REAL,result TEXT,targets TEXT,reason TEXT,UNIQUE(project,session_id,capture_id,fingerprint,scope));
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);''')
            c.execute('CREATE TABLE IF NOT EXISTS archive_events(id TEXT PRIMARY KEY,job TEXT,payload TEXT,delivered INTEGER DEFAULT 0)')
            c.execute('CREATE TABLE IF NOT EXISTS metadata_versions(session TEXT,capture TEXT,before TEXT,after TEXT,PRIMARY KEY(session,capture,before))')
            c.execute('CREATE TABLE IF NOT EXISTS live_sessions(session TEXT PRIMARY KEY,updated REAL)')
            c.execute("CREATE INDEX IF NOT EXISTS project_source_hash ON projects(json_extract(metadata,'$.source_sha256'))")
            c.execute("CREATE TABLE IF NOT EXISTS parse_jobs(id TEXT PRIMARY KEY,session_id TEXT,capture_id TEXT,status TEXT,created REAL,result TEXT DEFAULT '{}',reason TEXT DEFAULT '')")
            for table in ('parse_jobs','jobs'):
                import task_history
                task_history.initialize(c, table)
                c.execute(f'CREATE INDEX IF NOT EXISTS {table}_capture_status ON {table}(session_id,capture_id,created DESC)')
                if 'updated' not in {r['name'] for r in c.execute('PRAGMA table_info('+table+')')}:
                    c.execute('ALTER TABLE '+table+' ADD COLUMN updated REAL NOT NULL DEFAULT 0')
            for table,columns in [('sku_rows',['baseline_completed_at','completed_at','completion_job','source_values']),('jobs',['captured_at','capture_time_source','trigger'])]:
                present={r['name'] for r in c.execute('PRAGMA table_info('+table+')')}
                for column in columns:
                    if column not in present:c.execute('ALTER TABLE '+table+' ADD COLUMN '+column+" TEXT NOT NULL DEFAULT ''")
            if 'priority' not in {r['name'] for r in c.execute('PRAGMA table_info(jobs)')}:
                c.execute('ALTER TABLE jobs ADD COLUMN priority REAL NOT NULL DEFAULT 0')
            for table in ('jobs','parse_jobs'):
                if 'run_token' not in {r['name'] for r in c.execute('PRAGMA table_info('+table+')')}:
                    c.execute('ALTER TABLE '+table+" ADD COLUMN run_token TEXT NOT NULL DEFAULT ''")
            c.execute("UPDATE jobs SET status='queued',reason='程序重启后恢复' WHERE status='running'")
            c.execute("UPDATE parse_jobs SET status='queued',reason='程序重启后恢复' WHERE status='running'")
            controls=c.execute("SELECT value FROM settings WHERE key='task_controls'").fetchone()
            full=c.execute("SELECT value FROM settings WHERE key='full_auto_ocr'").fetchone()
        initialize_ocr(self)
        controls=json.loads(controls[0]) if controls else {}
        self.task_mode=controls.get('mode','auto');self.tasks_paused=controls.get('paused',False)
        self.full_auto=json.loads(full[0]) if full else {'enabled':False};self.full_scan_at=0
        self.catalog_pending=set();self.recovery_at=0
        self._upgrade_progress()
        self.thread=threading.Thread(target=self._run,name='sku-recognition',daemon=True)
        if autostart:self.thread.start()

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.db,timeout=30);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()

    def project(self,pid=None):
        with self.connect() as c:
            if pid is None:
                active=c.execute("SELECT value FROM settings WHERE key='active'").fetchone();pid=active[0] if active else None
            row=c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
        if not row:return None
        item=dict(row);item['metadata']=json.loads(item['metadata']);return item

    def _upgrade_progress(self):
        with self.connect() as c:projects=[r[0] for r in c.execute('SELECT id FROM projects')]
        for pid in projects:
            p=self.project(pid)
            if p['metadata'].get('progress_version')==1:continue
            try:sheets=inspect_workbook(self.root/pid/'source.xlsx')
            except (OSError,ValueError):continue
            metadata=dict(p['metadata'],sheets=[{k:v for k,v in s.items() if k!='rows'} for s in sheets],progress_version=1)
            with self.connect() as c:
                for s in sheets:
                    for r in s['rows']:
                        c.execute('UPDATE sku_rows SET baseline_completed_at=?,completed_at=?,source_values=? WHERE project=? AND sheet=? AND excel_row=?',(r.get('baseline_completed_at',''),r.get('baseline_completed_at',''),json.dumps(r.get('cells',{}),ensure_ascii=False),pid,s['name'],r['excel_row']))
                records={}
                for job in c.execute("SELECT * FROM jobs WHERE project=? AND captured_at=''",(pid,)).fetchall():
                    sid=job['session_id']
                    if sid not in records:records[sid]={r['id']:r for r in self.store.records(sid)} if self.store.session(sid) else {}
                    record=records[sid].get(job['capture_id'])
                    if record and (record['fingerprint']==job['fingerprint'] or self._valid(dict(job))):
                        stamp,source=capture_time(record);c.execute('UPDATE jobs SET captured_at=?,capture_time_source=? WHERE id=?',(stamp,source,job['id']))
                c.execute('UPDATE projects SET metadata=? WHERE id=?',(json.dumps(metadata,ensure_ascii=False),pid))
            self.dirty.add(pid)

    def rows(self,pid,scope=None):
        with self.connect() as c:
            sql='SELECT * FROM sku_rows WHERE project=?';args=[pid]
            if scope:sql+=' AND sheet=?';args.append(scope)
            return [dict(r) for r in c.execute(sql+' ORDER BY sheet,excel_row',args)]

    def state(self):
        p=self.project()
        if p and p['metadata'].get('removed_at'):p=None
        with self.connect() as c:
            projects=[dict(r) for r in c.execute("SELECT id,name,scope,created FROM projects WHERE json_extract(metadata,'$.removed_at') IS NULL ORDER BY created DESC")]
            jobs=[dict(r) for r in c.execute('SELECT * FROM jobs WHERE project=? ORDER BY created DESC LIMIT 60',(p['id'],))] if p else []
        for job in jobs:
            job['result']=json.loads(job['result'] or '{}');job['targets']=json.loads(job['targets'] or '[]')
        rows=self.rows(p['id']) if p else []
        if p:
            from sku_presentation import preferences
            p['presentation']=preferences(self,p['id'])
            self._ensure_images(p)
            p['export_path']=str(self.root/p['id']/'拍摄对账.xlsx')
            if p['metadata'].get('automatic_day'):p['export_path']=str(self.root/p['id']/(p['metadata']['automatic_day']+'.xlsx'))
            image_map={(s['name'],r['excel_row']):[i for i in s.get('images',[]) if int(''.join(filter(str.isdigit,i['cell'])))==r['excel_row']]
                       for s in p['metadata']['sheets'] for r in rows if r['sheet']==s['name']}
            for row in rows:
                row['images']=[{'cell':im['cell'],'url':f"/api/sku/image?row_id={row['id']}&index={i}"} for i,im in enumerate(image_map.get((row['sheet'],row['excel_row']),[]))]
        for row in rows:row['source_values']=json.loads(row.get('source_values') or '{}')
        with self.connect() as c:
            ids=list({r['completion_job'] for r in rows if r.get('completion_job')})
            evidence={}
            for start in range(0,len(ids),500):
                batch=ids[start:start+500]
                evidence.update({r['id']:dict(r) for r in c.execute('SELECT id,session_id,capture_id,captured_at,capture_time_source FROM jobs WHERE id IN ('+','.join('?' for _ in batch)+')',batch)})
        for row in rows:row['completion']=evidence.get(row.get('completion_job'))
        from sku_reconciliation import decorate_images
        decorate_images(self,rows)
        from sku_presentation import grouped_rows
        return {'project':p,'projects':projects,'rows':rows,'all_rows':grouped_rows(rows,p['metadata']['sheets']) if p else [],'jobs':jobs,'progress':statistics(rows,p['metadata']['sheets']) if p else [],'full_auto':self.full_auto_state()}

    def _ensure_images(self,p):
        if all('images' in s or s.get('skipped') for s in p['metadata']['sheets']):return
        with self.lock:
            sheets=inspect_workbook(self.root/p['id']/'source.xlsx')
            by_name={s['name']:s.get('images',[]) for s in sheets}
            for sheet in p['metadata']['sheets']:sheet['images']=by_name.get(sheet['name'],[])
            with self.connect() as c:c.execute('UPDATE projects SET metadata=? WHERE id=?',(json.dumps(p['metadata'],ensure_ascii=False),p['id']))

    def image(self,row_id,index=0):
        with self.connect() as c:row=c.execute('SELECT project,sheet,excel_row FROM sku_rows WHERE id=?',(row_id,)).fetchone()
        if not row:raise ValueError('产品图所属款号不存在')
        p=self.project(row['project']);self._ensure_images(p)
        sheet=next(s for s in p['metadata']['sheets'] if s['name']==row['sheet'])
        images=[i for i in sheet.get('images',[]) if int(''.join(filter(str.isdigit,i['cell'])))==row['excel_row']]
        if not 0<=index<len(images):raise ValueError('该款号没有此产品图')
        return read_image(self.root/p['id']/'source.xlsx',images[index]['media'])

    def import_file(self,name,content,session_id=None,preferred_scope=None):
        if not str(name).lower().endswith('.xlsx'):raise ValueError('目前支持 .xlsx，请先将其他格式另存为 xlsx')
        if not 0<len(content)<=20_000_000:raise ValueError('表格须小于 20MB')
        if session_id and not self.store.session(session_id):raise ValueError('当前照片目录不存在')
        digest=hashlib.sha256(content).hexdigest();folder=None
        with self.lock:
            try:
                with self.connect() as c:
                    # 指纹检查与登记同一事务；多窗口/多服务同时导入也只新建一次。
                    c.execute('BEGIN IMMEDIATE')
                    existing=c.execute("""SELECT id FROM projects WHERE json_extract(metadata,'$.source_sha256')=?
                        AND json_extract(metadata,'$.removed_at') IS NULL AND json_extract(metadata,'$.automatic_day') IS NULL
                        ORDER BY id=(SELECT value FROM settings WHERE key='active') DESC,
                        (SELECT count(*) FROM jobs WHERE project=projects.id AND status IN ('confirmed','matched')) DESC,created DESC LIMIT 1""",(digest,)).fetchone()
                    if existing:pid=existing['id']
                    else:
                        pid=uuid.uuid4().hex;folder=self.root/pid;folder.mkdir();source=folder/'source.xlsx';source.write_bytes(content)
                        try:sheets=inspect_workbook(source)
                        except Exception as exc:raise ValueError('表格无法导入：'+str(exc)[:180]) from None
                        names=[s['name'] for s in sheets if not s.get('skipped')]
                        scope=(preferred_scope if preferred_scope in names else names[0]) if preferred_scope is not None else ''
                        metadata={'sheets':[{k:v for k,v in s.items() if k!='rows'} for s in sheets],'source_sha256':digest,'progress_version':1}
                        c.execute('INSERT INTO projects(id,name,metadata,session_id,scope,enabled,created) VALUES (?,?,?,?,?,?,?)',(pid,Path(name).name,json.dumps(metadata,ensure_ascii=False),session_id,scope,1,self.clock()))
                        for sheet in sheets:
                            for row in sheet['rows']:
                                c.execute('INSERT INTO sku_rows(id,project,sheet,excel_row,sku,normalized,baseline,status,baseline_completed_at,completed_at,source_values) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,pid,sheet['name'],row['excel_row'],row['sku'],row['normalized'],row['baseline'],row['baseline'],row.get('baseline_completed_at',''),row.get('baseline_completed_at',''),json.dumps(row.get('cells',{}),ensure_ascii=False)))
                        c.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)",(pid,))
            except Exception:
                if folder:
                    (folder/'source.xlsx').unlink(missing_ok=True);folder.rmdir()
                raise
            if existing:return self._reuse_import(pid,session_id,preferred_scope)
            aliases={key:value for sheet in sheets for key,value in sheet.get('header_aliases',{}).items()}
            if aliases:
                from sku_presentation import save_preferences
                save_preferences(self,pid,{'headers':aliases,'images':{}})
            if self.archive and session_id and scope:self.archive.start_stream(session_id,pid,scope)
            self._reconcile_full_context();self.export(pid)
            return dict(self.state(),already_imported=False)

    def _reuse_import(self,pid,session_id,preferred_scope):
        project=self.project(pid);sid=project['session_id'] or session_id;scope=project['scope']
        names=[s['name'] for s in project['metadata']['sheets'] if not s.get('skipped')]
        if not scope and preferred_scope is not None:scope=preferred_scope if preferred_scope in names else names[0]
        with self.connect() as c:
            active=c.execute("SELECT value FROM settings WHERE key='active'").fetchone()
            c.execute('UPDATE projects SET session_id=?,scope=? WHERE id=?',(sid,scope,pid))
            c.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)",(pid,))
        if self.archive and sid and scope and (not active or active[0]!=pid or sid!=project['session_id'] or scope!=project['scope']):self.archive.start_stream(sid,pid,scope)
        self._reconcile_full_context()
        return dict(self.state(),already_imported=True)

    def configure(self,pid,session_id,scope,enabled=True):
        p=self.project(pid)
        if not p or p['metadata'].get('removed_at'):raise ValueError('拍摄对账任务不存在或已移除，请重新导入')
        names={s['name'] for s in p['metadata']['sheets'] if not s.get('skipped')}
        if scope not in names:raise ValueError('请先选择当前拍摄类型／工作表')
        if session_id and not self.store.session(session_id):raise ValueError('请先打开现场照片目录，再绑定对账任务')
        with self.lock,self.connect() as c:
            c.execute('UPDATE projects SET session_id=?,scope=?,enabled=? WHERE id=?',(session_id,scope,int(bool(enabled)),pid))
            c.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)",(pid,))
        if self.archive and session_id and (p['session_id']!=session_id or p['scope']!=scope):self.archive.start_stream(session_id,pid,scope)
        self._reconcile_full_context();return self.state()

    def activate(self,pid):
        project=self.project(pid)
        if not project or project['metadata'].get('removed_at'):raise ValueError('对账任务不存在或已移除，请重新导入')
        with self.lock,self.connect() as c:c.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)",(pid,))
        if self.archive:
            if project['scope'] and project['session_id']:self.archive.start_stream(project['session_id'],pid,project['scope'])
            else:
                self.archive.sync_stream()
                with self.archive.connect() as c:c.execute("UPDATE streams SET state='paused' WHERE state='active'")
        self._reconcile_full_context();return self.state()

    def on_save(self,sid,old,state):
        p=self.project()
        if any(value==2 for value in state.get('overrides',{}).values()):
            with self.connect() as c:c.execute('INSERT OR REPLACE INTO live_sessions VALUES (?,?)',(sid,self.clock()))
            with self.store.connect() as c:c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('shooting_session:'+sid,self.store.session(sid)['root']))
        previous=old.get('overrides',{});current=state.get('overrides',{})
        for cid,value in previous.items():
            if value==2 and current.get(cid)!=2:
                with self.lock,self.connect() as c:
                    if current.get(cid,0)==0:
                        affected=[r[0] for r in c.execute('SELECT DISTINCT project FROM jobs WHERE session_id=? AND capture_id=?',(sid,cid))]
                        self._clear_capture_ocr(c,sid,cid)
                    else:
                        # 非零评级沿用原规则，全图 OCR 的单张资料不因改成其他星级而丢失。
                        affected=[r[0] for r in c.execute("SELECT DISTINCT project FROM jobs WHERE session_id=? AND capture_id=? AND trigger!='full_auto'",(sid,cid))]
                        c.execute("UPDATE jobs SET status='cancelled',reason='标签不再是二星，本次识别贡献已撤销' WHERE session_id=? AND capture_id=? AND status!='ignored' AND trigger!='full_auto'",(sid,cid))
                    self.dirty.update(affected)
                    # 人工确认及补识别的段可能使用合成 job ID，按标签身份统一撤销。
                    payload={'kind':'ocr_reset','session_id':sid,'capture_id':cid}
                    c.execute('INSERT INTO archive_events(id,job,payload) VALUES (?,?,?)',(uuid.uuid4().hex,'reset:'+sid+':'+cid,json.dumps(payload)))
                    # 清单读库立即可见；Excel 和照片补偿仍由原后台队列执行。
                    for pid in affected:self._refresh_progress(c,pid)
                self._wake_label()
        for cid,value in current.items():
            if value!=2 or previous.get(cid)==2:continue
            if self._queue_confirmed_interval(sid,cid):continue
            if self._prioritize_full_label(sid,cid):continue
            if self.task_mode!='auto':continue
            context=state.get('skuMarkers',{}).get(cid)
            if isinstance(context,dict) and context.get('project_id') and not context.get('ignore'):
                self.enqueue(sid,cid,context.get('project_id'),context.get('scope'),trigger='auto',immediate=True)
            elif p and p['enabled'] and p['session_id']==sid and p['scope']:self.enqueue(sid,cid,trigger='auto',immediate=True)
            else:self.enqueue(sid,cid,trigger='auto',immediate=True)
    def request_catalog(self,sid):
        with self.lock:self.catalog_pending.add(sid)

    def _rating(self,sid,record):
        return self.store.session(sid)['state'].get('overrides',{}).get(record['id'],record.get('existingRating',0))

    def _queue_confirmed_interval(self,sid,cid,*,only_missing=False):
        """单张已确认后才标二星：复用确切款号，补齐区间事件，不再请求模型。"""
        if not self.archive:return False
        with self.lock,self.connect() as c:
            choice=c.execute('SELECT sku FROM ocr_choices WHERE session_id=? AND capture_id=?',(sid,cid)).fetchone()
            if not choice:return False
            job=c.execute("SELECT * FROM jobs WHERE session_id=? AND capture_id=? AND status='confirmed' ORDER BY updated DESC,created DESC LIMIT 1",(sid,cid)).fetchone()
            if not job:return False
            job=dict(job);project=self.project(job['project'])
            if not project or project['metadata'].get('removed_at'):return False
            with self.archive.connect() as archive:
                applied=archive.execute("""SELECT 1 FROM segments g JOIN streams s ON s.id=g.stream
                    JOIN bindings b ON b.stream=g.stream AND b.asset=g.label
                    WHERE s.session=? AND b.capture=? AND g.sku=? AND g.status NOT IN ('cancelled','superseded') LIMIT 1""",(sid,cid,choice['sku'])).fetchone()
            if applied and only_missing:return True
            record=self._valid(job)
            if not record or self._rating(sid,record)!=2:return False
            identity='choice:'+sid+':'+cid
            reset=c.execute("SELECT COALESCE(MAX(rowid),0) FROM archive_events WHERE json_extract(payload,'$.kind')='ocr_reset' AND json_extract(payload,'$.session_id')=? AND json_extract(payload,'$.capture_id')=?",(sid,cid)).fetchone()[0]
            if not c.execute('SELECT 1 FROM archive_events WHERE job=? AND delivered=0 AND rowid>?',(identity,reset)).fetchone():
                payload={'id':identity,'kind':'ocr_choice','session_id':sid,'capture_id':cid,'project':job['project'],
                         'scope':job['scope'],'status':'confirmed','sku':choice['sku']}
                c.execute('INSERT INTO archive_events(id,job,payload) VALUES (?,?,?)',(uuid.uuid4().hex,identity,json.dumps(payload,ensure_ascii=False)))
        self._wake_label();return True

    def _recover_confirmed_intervals(self):
        # 只补当前现场的已确认二星；历史目录和普通单张识别不在本次恢复范围。
        if not self.archive:return
        session=self.store.session()
        if not session:return
        overrides=session['state'].get('overrides',{})
        ratings={r['id']:overrides.get(r['id'],r.get('existingRating',0)) for r in self.store.records(session['id'])}
        with self.connect() as c:choices=c.execute('SELECT capture_id FROM ocr_choices WHERE session_id=?',(session['id'],)).fetchall()
        for choice in choices:
            if ratings.get(choice['capture_id'])==2:self._queue_confirmed_interval(session['id'],choice['capture_id'],only_missing=True)

    def _automatic_project(self,sid,*,start_stream=True):
        from sku_workbook_auto import create_book
        with self.lock:
            p=self.project()
            day=datetime.fromtimestamp(self.clock()).strftime('%Y-%m-%d')
            if p and p['session_id'] in (None,'',sid) and p['enabled'] and p['metadata'].get('automatic_day',day)==day:
                scope=p['scope'] or next((s['name'] for s in p['metadata']['sheets'] if not s.get('skipped')),'现场记录')
                if p['session_id']!=sid or not p['scope']:self.configure(p['id'],sid,scope)
                return self.project(p['id'])
            with self.connect() as c:row=c.execute("SELECT id FROM projects WHERE session_id=? AND json_extract(metadata,'$.automatic_day')=?",(sid,day)).fetchone()
            if row:
                with self.connect() as c:c.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)",(row['id'],))
                return self.project(row['id'])
            pid=uuid.uuid4().hex;folder=self.root/pid;folder.mkdir();content=create_book();(folder/'source.xlsx').write_bytes(content)
            sheets=inspect_workbook(folder/'source.xlsx',allow_empty=True)
            metadata={'sheets':[{k:v for k,v in s.items() if k!='rows'} for s in sheets],'automatic_day':day,'managed_sheet':'现场记录','progress_version':1,'source_sha256':hashlib.sha256(content).hexdigest()}
            with self.connect() as c:
                c.execute('INSERT INTO projects(id,name,metadata,session_id,scope,enabled,created) VALUES (?,?,?,?,?,?,?)',(pid,day+'.xlsx',json.dumps(metadata,ensure_ascii=False),sid,'现场记录',1,self.clock()))
                c.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)",(pid,))
            if self.archive and start_stream:self.archive.start_stream(sid,pid,'现场记录')
            self.export(pid);return self.project(pid)

    def _record_unplanned(self,job,readings,result,*,accept_observed=False,connection=None):
        codes={}
        for reading in readings:
            if reading.get('confidence',0)<.9 and not reading.get('unscored'):return result
            for code in reading.get('codes',[]):
                if isinstance(code,str) and 0<len(code.strip())<=80 and normalize(code):codes.setdefault(normalize(code),code.strip())
        if len(codes)!=1:return result
        key,code=next(iter(codes.items()))
        uncertain=not accept_observed and bool(result.get('candidates') and max(candidate['score'] for candidate in result['candidates'])>=.85 and result['status']!='matched')
        all_rows=self.rows(job['project']);managed=self.project(job['project'])['metadata'].get('managed_sheet')
        exact=[r for r in all_rows if r['normalized']==key and r['sheet'] in (job['scope'],managed)]
        if len(exact)>1:return result
        if exact:
            if exact[0]['baseline']=='待核对' and not accept_observed:return dict(result,status='review',row_ids=[],reason='插拍款号与原计划近似，请核对后确认')
            return dict(result,status='matched',row_ids=[exact[0]['id']],reason='OCR 唯一款号精确匹配')
        with self.lock,self.connect() if connection is None else nullcontext(connection) as c:
            project=self.project(job['project']);metadata=project['metadata'];sheet=metadata.get('managed_sheet')
            if not sheet:
                sheet='临时插拍';names={s['name'] for s in metadata['sheets']}
                while sheet in names:sheet+='记录'
                metadata['managed_sheet']=sheet
            row_number=c.execute('SELECT COALESCE(MAX(excel_row),1)+1 FROM sku_rows WHERE project=? AND sheet=?',(job['project'],sheet)).fetchone()[0]
            rid=uuid.uuid4().hex;record=next(r for r in self.store.records(job['session_id']) if r['id']==job['capture_id'])
            stamp=datetime.fromtimestamp(self.clock(),timezone.utc).astimezone().isoformat(timespec='seconds')
            values={'A':code,'B':record.get('jpg') or record.get('raw'),'C':Path(self.store.session(job['session_id'])['root']).name,'D':stamp}
            baseline='待核对' if uncertain else '未拍摄'
            c.execute('INSERT INTO sku_rows(id,project,sheet,excel_row,sku,normalized,baseline,status,baseline_completed_at,completed_at,source_values) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(rid,job['project'],sheet,row_number,code,key,baseline,baseline,'','',json.dumps(values,ensure_ascii=False)))
            if sheet not in {s['name'] for s in metadata['sheets']}:
                metadata['sheets'].append({'name':sheet,'path':'','header_row':1,'sku_col':1,'status_col':5,'status_letter':'E','new_status':False,'time_col':6,'time_letter':'F','columns':[{'index':i,'letter':chr(64+i),'title':title} for i,title in enumerate(('款号','来源文件','现场文件夹','记录时间'),1)],'images':[]})
            c.execute('UPDATE projects SET metadata=? WHERE id=?',(json.dumps(metadata,ensure_ascii=False),job['project']))
        if uncertain:
            candidates=list(result.get('candidates',[]))+[{'row_id':rid,'sku':code,'sheet':sheet,'row':row_number,'score':1.0}]
            return dict(result,status='review',row_ids=[],reason='新款已记入临时插拍；与计划款号近似，请确认',candidates=candidates)
        return dict(result,status='matched',row_ids=[rid],reason='OCR 唯一款号已记录到'+sheet)

    def parse_capture(self,sid,cid):
        """兼容旧手动入口，并将进度与结果记录到解析任务页。"""
        jid=uuid.uuid4().hex
        with self.connect() as c:c.execute('INSERT INTO parse_jobs(id,session_id,capture_id,status,created) VALUES (?,?,?,?,?)',(jid,sid,cid,'running',self.clock()))
        return self._parse_job(jid,sid,cid)

    def _parse_job(self,jid,sid,cid,run_token=None):
        if run_token is None:
            with self.connect() as c:run_token=c.execute('SELECT run_token FROM parse_jobs WHERE id=?',(jid,)).fetchone()[0]
        try:
            result=self._read_capture(sid,cid,jid,run_token)
            if self.require_sku_choice:result['reading']['requires_sku_choice']=True
            with self.connect() as c:
                changed=c.execute("UPDATE parse_jobs SET status='completed',result=?,reason='',updated=? WHERE id=? AND status='running' AND run_token=?",(json.dumps(result['reading'],ensure_ascii=False),self.clock(),jid,run_token)).rowcount
                if changed:record_ocr(c,'read',jid,sid,cid,result['reading'],self.clock())
                if changed and self.archive:
                    payload={'kind':'ocr_read','session_id':sid,'capture_id':cid,'read_id':jid}
                    c.execute('INSERT INTO archive_events(id,job,payload) VALUES (?,?,?)',(uuid.uuid4().hex,jid,json.dumps(payload)))
            if changed:
                result['auto_applied']=self.apply_sku_rules(sid,cid,[result['reading']])
            result['recommended']=self.recommended_codes([result['reading']])
            return result
        except Exception as exc:
            with self.connect() as c:c.execute("UPDATE parse_jobs SET status=?,reason=? WHERE id=? AND status='running' AND run_token=?",('queued' if self.tasks_paused or self.closed.is_set() else 'failed',str(exc)[:400],jid,run_token))
            raise

    def enqueue_parses(self,sid,cids):
        if not isinstance(cids,list) or not 1<=len(cids)<=2000:raise ValueError('请选择 1–2000 张照片')
        if not self.store.session(sid) or self.store.is_removed(sid):raise ValueError('照片目录不存在')
        valid={r['id'] for r in self.store.records(sid)}
        if any(cid not in valid for cid in cids):raise ValueError('所选照片已变化，请重新选择')
        if not (self.providers.selected('ocr') or self.providers.purpose('sku_label') or self.providers.selected('multimodal')):raise ValueError('请先选用 OCR 或多模态模型')
        with self.connect() as c:
            for cid in dict.fromkeys(cids):
                if not c.execute("SELECT 1 FROM parse_jobs WHERE session_id=? AND capture_id=? AND status IN ('queued','running')",(sid,cid)).fetchone():
                    c.execute('INSERT INTO parse_jobs(id,session_id,capture_id,status,created) VALUES (?,?,?,?,?)',(uuid.uuid4().hex,sid,cid,'queued',self.clock()))
        self._wake_label()
        return self.task_state()

    def configure_tasks(self,mode=None,paused=None,action=None,job_id=None):
        if mode is not None and mode not in ('auto','manual'):raise ValueError('解析模式无效')
        if paused is not None and type(paused) is not bool:raise ValueError('暂停状态无效')
        if action is not None and action not in ('retry','cancel'):raise ValueError('任务操作无效')
        with self.lock,self.connect() as c:
            if mode is not None:self.task_mode=mode
            if paused is not None:self.tasks_paused=paused
            c.execute("INSERT OR REPLACE INTO settings VALUES ('task_controls',?)",(json.dumps({'mode':self.task_mode,'paused':self.tasks_paused}),))
            if action and str(job_id).startswith('read:'):
                jid=job_id[5:]
                if action=='retry':c.execute("UPDATE parse_jobs SET status='queued',reason='' WHERE id=? AND status IN ('failed','cancelled')",(jid,))
                else:c.execute("UPDATE parse_jobs SET status='cancelled',reason='人工取消' WHERE id=? AND status IN ('queued','running')",(jid,))
        if action and str(job_id).startswith('label:'):
            jid=job_id[6:]
            if action=='retry':
                self.review(jid,'retry')
                with self.connect() as c:c.execute("UPDATE jobs SET trigger='manual' WHERE id=? AND trigger!='full_auto'",(jid,))
            else:self.review(jid,'ignore')
        if mode=='manual' and self.full_auto.get('enabled'):self.set_full_auto(False)
        self._wake_label()
        return self.task_state()

    def task_state(self):
        with self.connect() as c:
            manual=[dict(r,id='read:'+r['id'],trigger='manual',kind='ocr') for r in c.execute('SELECT * FROM parse_jobs WHERE task_hidden=0 ORDER BY created DESC LIMIT 100')]
            labels=[dict(r,id='label:'+r['id'],kind='label') for r in c.execute('SELECT * FROM jobs WHERE task_hidden=0 ORDER BY created DESC LIMIT 100')]
            counts={}
            for table in ('parse_jobs','jobs'):
                for r in c.execute(f'SELECT status,count(*) AS n FROM {table} WHERE task_hidden=0 GROUP BY status'):counts[r['status']]=counts.get(r['status'],0)+r['n']
        jobs=sorted(manual+labels,key=lambda j:j['created'],reverse=True)[:100];catalog={}
        for j in jobs:
            if j.get('trigger')=='full_auto':j['kind']='ocr'
            sid=j['session_id']
            if sid not in catalog:catalog[sid]={r['id']:r for r in self.store.records(sid)} if self.store.session(sid) else {}
            record=catalog[sid].get(j['capture_id'],{})
            j.update(name=record.get('jpg') or record.get('raw') or j['capture_id'],preview=f"/api/preview/{sid}/{j['capture_id']}?size=180" if record else '',trigger=j.get('trigger') or 'auto')
            j['result']=json.loads(j['result'] or '{}')
        provider=self.providers.selected('ocr') or self.providers.purpose('sku_label') or self.providers.selected('multimodal')
        return {'mode':self.task_mode,'paused':self.tasks_paused,'jobs':jobs,'counts':counts,'model':provider['name'] if provider else '未配置模型','full_auto':self.full_auto_state()}

    def clear_completed_tasks(self):
        import task_history
        with self.lock, self.connect() as c:
            return sum(task_history.clear(c, table, ('completed','matched','confirmed')) for table in ('parse_jobs','jobs'))

    def clear_task_list(self):
        import task_history
        with self.lock, self.connect() as c:
            return sum(task_history.clear_inactive(c, table) for table in ('parse_jobs','jobs'))

    def _read_capture(self,sid,cid,jid=None,run_token=None):
        with self.ocr_progress.track('parse_jobs',jid,run_token) as progress:
            return self._read_capture_progress(sid,cid,jid,run_token,progress)

    def _read_capture_progress(self,sid,cid,jid,run_token,progress):
        """手动读取当前图的 SKU 文本；不要求二星或 Excel，不记账、不备案。"""
        session=self.store.session(sid)
        if not session:raise ValueError('请先打开照片目录')
        record=next((r for r in self.store.records(sid) if r['id']==cid),None)
        if not record:raise ValueError('请选择要解析的当前照片')
        provider=self.providers.selected('ocr') or self.providers.purpose('sku_label') or self.providers.selected('multimodal')
        if not provider:raise ValueError('请先在 AI 助手配置中选用 OCR 或多模态模型')
        paths=[media.resolved_file(session['root'],record[k]) for k in ('raw','jpg') if record.get(k)]
        def signature():
            return [(str(path),stat.st_size,stat.st_mtime_ns) for path in paths for stat in [path.stat()]]
        before=signature()
        fresh=dict(record,fingerprint=hashlib.sha256(json.dumps(before,ensure_ascii=False).encode()).hexdigest())
        image=media.preview_bytes(fresh,session['root'],self.store.data/'previews',media.find_exiftool(self.store.root),2400,False)
        progress('waiting_slot',provider)
        def current():
            if self.tasks_paused or self.closed.is_set():return False
            if not jid:return True
            with self.connect() as c:
                row=c.execute('SELECT status,run_token FROM parse_jobs WHERE id=?',(jid,)).fetchone()
            return bool(row and row['status']=='running' and (run_token is None or row['run_token']==run_token))
        with self.providers.inference_slot(provider,cancel=self.closed,valid=current), self.scheduler.slot(label=True,cancel=self.closed,independent=provider.get('protocol')!='local') if self.scheduler else nullcontext():
            if self.tasks_paused:raise ValueError('OCR 队列已暂停')
            if jid:
                with self.connect() as c:
                    row=c.execute('SELECT status,run_token FROM parse_jobs WHERE id=?',(jid,)).fetchone()
                    if not row or row['status']!='running' or run_token is not None and row['run_token']!=run_token:raise ValueError('任务已取消')
            self.providers.check_inference(provider)
            progress('recognizing',provider)
            reading=self.recognizer({**provider,'_progress':lambda phase:progress(phase,provider)},image)
        current=next((r for r in self.store.records(sid) if r['id']==cid),None)
        after=signature()
        if not current or current['fingerprint']!=record['fingerprint'] or after!=before:
            # 评级只改元数据；等待本软件写入完成后按已登记版本链验证，不能误丢 OCR。
            with self.archive.lock if self.archive else nullcontext():
                current=next((r for r in self.store.records(sid) if r['id']==cid),None);after=signature()
                observed=hashlib.sha256(json.dumps(after,ensure_ascii=False).encode()).hexdigest()
                fingerprint=hashlib.sha256(json.dumps(before,ensure_ascii=False).encode()).hexdigest();seen=set()
                managed=bool(current and observed==current['fingerprint'] and fingerprint==record['fingerprint'])
                if managed:
                    with self.connect() as c:
                        while fingerprint!=observed and fingerprint not in seen:
                            seen.add(fingerprint)
                            version=c.execute('SELECT after FROM metadata_versions WHERE session=? AND capture=? AND before=?',(sid,cid,fingerprint)).fetchone()
                            if not version:break
                            fingerprint=version[0]
                if not managed or fingerprint!=observed:raise ValueError('解析期间照片发生变化，请重新解析')
        return {'session_id':sid,'capture_id':cid,'name':record['name'],'reading':reading}

    def enqueue(self,sid,cid,project_id=None,scope=None,*,trigger='manual',immediate=False):
        record=next((r for r in self.store.records(sid) if r['id']==cid),None)
        if not record:raise ValueError('标签照片不存在')
        if self._rating(sid,record)!=2:raise ValueError('请先将标签照片标为二星')
        with self.connect() as c:prior=[dict(r) for r in c.execute("SELECT * FROM jobs WHERE session_id=? AND capture_id=? AND status NOT IN ('cancelled','ignored')",(sid,cid))]
        if trigger=='auto' and any(self._valid(job) for job in prior):
            if immediate:
                with self.lock,self.connect() as c:
                    for job in prior:
                        if job['status'] in ('queued','waiting_model') and self._valid(job):c.execute('UPDATE jobs SET due=?,priority=? WHERE id=?',(self.clock(),self.clock(),job['id']))
                self._wake_label()
            return {'queued':False}
        p=self.project(project_id)
        day=datetime.fromtimestamp(self.clock()).strftime('%Y-%m-%d')
        if trigger=='auto' and (not p or not p['enabled'] or p['session_id']!=sid or not p['scope'] or p['metadata'].get('automatic_day',day)!=day):p=self._automatic_project(sid)
        scope=scope or (p['scope'] if p else '')
        if not p or not p['enabled'] or not scope or p['session_id']!=sid:raise ValueError('请先选拍摄工作表并绑定当前照片目录')
        if scope not in {s['name'] for s in p['metadata']['sheets'] if not s.get('skipped')}:raise ValueError('识别工作表范围无效')
        with self.lock,self.connect() as c:
            stamp,time_source=capture_time(record)
            c.execute('''INSERT INTO jobs(id,project,session_id,capture_id,fingerprint,scope,status,due,created,result,targets,reason,captured_at,capture_time_source,priority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,session_id,capture_id,fingerprint,scope)
                DO UPDATE SET status=CASE WHEN jobs.status IN ('matched','confirmed','running','queued') THEN jobs.status ELSE 'queued' END,due=excluded.due,priority=MAX(jobs.priority,excluded.priority),updated=excluded.created''',
                (uuid.uuid4().hex,p['id'],sid,cid,record['fingerprint'],scope,'queued',self.clock(),self.clock(),'{}','[]','二星标签已入队，优先识别' if immediate else '等待识别',stamp,time_source,self.clock() if immediate or trigger=='manual' else 0))
            if trigger=='manual':c.execute("UPDATE jobs SET trigger='manual' WHERE project=? AND session_id=? AND capture_id=? AND fingerprint=? AND scope=?",(p['id'],sid,cid,record['fingerprint'],scope))
            c.execute('DELETE FROM ocr_resets WHERE session_id=? AND capture_id=?',(sid,cid))
        if immediate or trigger=='manual':self._wake_label()
        return self.state()

    def _valid(self,job):
        session=self.store.session(job['session_id'])
        if not session:return None
        record=next((r for r in self.store.records(job['session_id']) if r['id']==job['capture_id']),None)
        if not record or job.get('trigger') not in ('full_auto','choice') and self._rating(job['session_id'],record)!=2:return None
        fingerprint=job['fingerprint'];seen=set()
        with self.connect() as c:
            while fingerprint!=record['fingerprint'] and fingerprint not in seen:
                seen.add(fingerprint);change=c.execute('SELECT after FROM metadata_versions WHERE session=? AND capture=? AND before=?',(job['session_id'],job['capture_id'],fingerprint)).fetchone()
                if not change:return None
                fingerprint=change[0]
        return record if fingerprint==record['fingerprint'] else None

    def tick(self):
        if self.clock()>=getattr(self,'interval_recovery_at',0):
            self.interval_recovery_at=self.clock()+10
            self._recover_confirmed_intervals()
        self._queue_full_auto()
        if self.task_mode=='auto':
            with self.lock:
                pending=list(self.catalog_pending);self.catalog_pending.clear()
            if self.clock()>=self.recovery_at:
                with self.connect() as c:pending+= [r[0] for r in c.execute('SELECT session FROM live_sessions')]
                self.recovery_at=self.clock()+10
            for sid in dict.fromkeys(pending):
                session=self.store.session(sid)
                if not session or self.store.is_removed(sid):continue
                if (context:=self._full_context()) and context['session']==sid:continue
                overrides=session['state'].get('overrides',{})
                for record in self.store.records(sid):
                    if overrides.get(record['id'],record.get('existingRating',0))==2:
                        try:self.enqueue(sid,record['id'],trigger='auto')
                        except ValueError:pass
                    else:
                        with self.connect() as c:
                            previous=[dict(row) for row in c.execute("SELECT * FROM jobs WHERE session_id=? AND capture_id=? AND trigger NOT IN ('manual','full_auto','choice') AND status NOT IN ('cancelled','ignored')",(sid,record['id']))]
                            for job in previous:
                                c.execute("UPDATE jobs SET status='cancelled',reason='文件实际星级已改变' WHERE id=?",(job['id'],))
                                self._archive_event(c,dict(job,status='cancelled'))
        with self.lock:
            dirty=list(self.dirty);self.dirty.clear()
        for pid in dirty:self.export(pid)
        if self.tasks_paused:return
        context=self._full_context()
        with self.lock,self.connect() as c:
            urgent=c.execute("SELECT 1 FROM jobs WHERE priority>0 AND status IN ('queued','waiting_model') AND due<=? AND (trigger='manual' OR (trigger='full_auto' AND ?) OR (trigger NOT IN ('manual','full_auto') AND ?)) LIMIT 1",(self.clock(),bool(context),self.task_mode=='auto')).fetchone()
            read=None if urgent or self.label_thread.is_alive() and self._parallel_reads_enabled() else c.execute("SELECT * FROM parse_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if read:
            claimed=self._claim_parse(read['id'])
            if not claimed:return
            try:self._parse_job(read['id'],read['session_id'],read['capture_id'],claimed['run_token'])
            except Exception:pass
            return
        context=self._full_context();starred=self._full_starred(context) if context else []
        parallel=self.label_thread.is_alive() and self._parallel_labels_enabled()
        with self.connect() as c:
            row=c.execute("SELECT * FROM jobs WHERE status IN ('queued','waiting_model') AND due<=? AND (NOT ? OR (priority<=0 AND trigger='full_auto' AND NOT ?)) AND (trigger='manual' OR (trigger='full_auto' AND ?) OR (trigger NOT IN ('manual','full_auto') AND ?)) ORDER BY priority DESC,CASE WHEN trigger='manual' OR (session_id=? AND capture_id IN (SELECT value FROM json_each(?))) THEN 0 ELSE 1 END,due,created LIMIT 1",(self.clock(),parallel,self._parallel_reads_enabled(),bool(context),self.task_mode=='auto',context['session'] if context else '',json.dumps(starred))).fetchone()
            if not row:return
        job=self._claim_label(row['id'])
        if job:self._process_label(job)

    def _process_label(self,job,*,parallel=False):
        with self.ocr_progress.track('jobs',job['id'],job.get('_run_token','')) as progress:
            return self._process_label_progress(job,parallel=parallel,progress=progress)

    def _process_label_progress(self,job,*,parallel,progress):
        try:
            if self.closed.is_set() or self.tasks_paused:
                self._release_label(job);return
            if not self._label_current(job):return
            p=self.project(job['project'])
            if not p or not p['enabled']:
                self._finish(job,'cancelled',{},[],'自动识别已关闭');return
            if job.get('trigger')=='full_auto' and not self._full_job_current(job):
                self._finish(job,'cancelled',{},[],'自动模式或输入文档已变化');return
            record=self._valid(job)
            if not record:
                self._finish(job,'cancelled',{},[],'星级或照片已变化，本次未记账');return
            reserve=self.archive and (job.get('trigger')!='full_auto' or self._rating(job['session_id'],record)==2)
            if reserve and not parallel:self.archive.reserve(job)
            selected=self._label_providers(job)
            if parallel:selected.sort(key=lambda p:p.get('protocol')=='local')
            if not selected:
                with self.connect() as c:c.execute("UPDATE jobs SET status='waiting_model',due=?,reason='请在 AI 助手选择 OCR 或多模态模型' WHERE id=? AND status='running' AND run_token=?",(self.clock()+2,job['id'],job['_run_token']))
                return
            session=self.store.session(job['session_id'])
            image=media.preview_bytes(record,session['root'],self.store.data/'previews',media.find_exiftool(self.store.root),2400,False)
            progress('waiting_slot')
            readings=[];failures=[]
            for provider in selected:
                if self.closed.is_set():
                    self._release_label(job)
                    return
                try:
                    valid=lambda:not self.tasks_paused and self._label_current(job) and (job.get('trigger')!='full_auto' or self._full_job_current(job)) and (job.get('trigger') in ('manual','full_auto') or self.task_mode=='auto')
                    with self.providers.inference_slot(provider,priority=job.get('priority',0),cancel=self.closed,valid=valid), self.scheduler.slot(label=True,cancel=self.closed,independent=provider.get('protocol')!='local') if self.scheduler else nullcontext():
                        if not self._label_current(job):return
                        if self.tasks_paused or job.get('trigger')=='full_auto' and not self._full_job_current(job) or job.get('trigger') not in ('manual','full_auto') and self.task_mode!='auto':
                            self._release_label(job);return
                        self.providers.check_inference(provider)
                        progress('recognizing',provider)
                        readings.append(self.recognizer({**provider,'_progress':lambda phase:progress(phase,provider)},image))
                except Exception as exc:failures.append(provider['name']+'：'+str(exc)[:150])
            if not readings:raise ValueError('；'.join(failures))
            if self.closed.is_set() or self.tasks_paused:
                self._release_label(job);return
            # API 先发出；归档区间/写图锁只影响结果提交，不能挡住最新二星的网络请求。
            if reserve and parallel and self._label_current(job) and self._valid(job):self.archive.reserve(job)
            with self.lock:
                if not self._label_current(job,confirmed=True):return
                if not self._valid(job):
                    self._finish(job,'cancelled',{},[],'识别期间星级或照片发生变化，未记账');return
                result=(match_automatic if job.get('trigger')=='full_auto' else match)(readings,self.rows(job['project'],job['scope']))
                starred_label=job.get('trigger')=='full_auto' and self._rating(job['session_id'],record)==2
                if self.require_sku_choice:result.update(status='review',row_ids=[],requires_sku_choice=True,reason='OCR 资料已保存，请选择正确 SKU 款号')
                elif not failures and (job.get('trigger')!='full_auto' or starred_label):result=self._record_unplanned(job,readings,result,accept_observed=self._rating(job['session_id'],record)==2)
                if starred_label:result['starred_label']=True
                result['readings']=readings;result['errors']=failures
                if failures and result['status']=='matched':result.update(status='review',row_ids=[],reason='部分选用模型失败，请确认后记账')
                # 星级与图像在请求期间变化，或者用户忽略了任务，都不能提交为已拍摄。
                with self.connect() as c:current=c.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()
                if not self._valid(job) or not self._label_current(job):
                    if self.require_sku_choice and self._label_current(job,confirmed=True) and current['status']=='confirmed' and self._valid(job):
                        with self.connect() as c:record_ocr(c,'job',job['id'],job['session_id'],job['capture_id'],result,self.clock())
                    if self._label_current(job):self._finish(job,'cancelled',result,[],'识别期间星级或照片发生变化，未记账')
                    return
                self._finish(job,result['status'],result,result['row_ids'],result['reason'])
                if not failures:self.apply_sku_rules(job['session_id'],job['capture_id'],readings)
        except Exception as exc:
            if self.closed.is_set() or self.tasks_paused or job.get('trigger') not in ('manual','full_auto') and self.task_mode!='auto':
                self._release_label(job);return
            with self.connect() as c:current=c.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()
            if self._label_current(job):self._finish(job,'failed',{},[],str(exc)[:350])

    def _finish(self,job,status,result,targets,reason):
        with self.lock,self.connect() as c:
            if job.get('_run_token') and not self._label_current(job):return
            if job.get('trigger')=='full_auto':status,result,targets,reason=self._full_commit(job,status,result,targets,reason)
            if status in ('ignored','cancelled'):c.execute('DELETE FROM ocr_choices WHERE session_id=? AND capture_id=? AND reading_key=?',(job['session_id'],job['capture_id'],'legacy:'+job['id']))
            record_ocr(c,'job',job['id'],job['session_id'],job['capture_id'],result,self.clock())
            c.execute('UPDATE jobs SET status=?,result=?,targets=?,reason=?,updated=? WHERE id=?',(status,json.dumps(result,ensure_ascii=False),json.dumps(targets),reason,self.clock(),job['id']))
            self._archive_event(c,dict(job,status=status,targets=json.dumps(targets),reason=reason))
        try:self.export(job['project'])
        except Exception as exc:
            from operation_errors import record_error
            record_error(self.store.data,'sku-finish-export',exc)
            self.dirty.add(job['project']);self.wake.set()
        # 识别结果已提交；历史归档/导出故障不能跳过后续公司规则自动确认。
        if self.archive:
            try:self.deliver_events(self.archive)
            except Exception as exc:
                from operation_errors import record_error
                record_error(self.store.data,'sku-finish-archive',exc)

    def review(self,jid,action,row_id=None):
        with self.lock:
            with self.connect() as c:
                row=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
                if not row:raise ValueError('识别记录不存在')
                job=dict(row)
                if job.get('trigger')=='full_auto':return self.review_full_auto(job,action,row_id)
                if action=='confirm':
                    if not self._valid(job):raise ValueError('此照片已不再是原二星标签，请重新标记后识别')
                    managed=self.project(job['project'])['metadata'].get('managed_sheet','')
                    target=c.execute('SELECT * FROM sku_rows WHERE id=? AND project=? AND sheet IN (?,?)',(row_id,job['project'],job['scope'],managed)).fetchone()
                    if not target:raise ValueError('只能确认本次识别所属工作表中的款号')
                    c.execute("UPDATE jobs SET status='confirmed',targets=?,reason='人工确认款号与工作表' WHERE id=?",(json.dumps([row_id]),jid))
                elif action=='ignore':c.execute("UPDATE jobs SET status='ignored',targets='[]',reason='人工忽略，不计入对账' WHERE id=?",(jid,))
                elif action=='retry':
                    if not self._valid(job):raise ValueError('请先将原标签照片标为二星')
                    c.execute("UPDATE jobs SET status='queued',targets='[]',due=?,reason='等待重试' WHERE id=?",(self.clock()+self.RETRY_DELAY,jid))
                else:raise ValueError('无效复核操作')
                c.execute('UPDATE jobs SET updated=? WHERE id=?',(self.clock(),jid))
                self._archive_event(c,dict(c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()))
            self.export(job['project'])
        return self.state()

    def _archive_event(self,c,job):
        if job.get('trigger')=='full_auto':
            record=next((r for r in self.store.records(job['session_id']) if r['id']==job['capture_id']),None)
            if not record or self._rating(job['session_id'],record)!=2:return
            job=dict(job,kind='ocr_interval')
        c.execute('INSERT INTO archive_events(id,job,payload) VALUES (?,?,?)',(uuid.uuid4().hex,job['id'],json.dumps(job,ensure_ascii=False)))

    def deliver_events(self,archive):
        # 后台、人工确认和全自动完成会同时投递，按持久事件顺序处理清零及重标。
        with archive.lock:self._deliver_events(archive)

    def _deliver_events(self,archive):
        with self.connect() as c:events=c.execute('SELECT * FROM archive_events WHERE delivered=0 ORDER BY rowid LIMIT 20').fetchall()
        for event in events:
            job=json.loads(event['payload'])
            if job.get('kind')=='ocr_reset':
                archive.clear_ocr_intervals(event['id'],job['session_id'],job['capture_id'])
                with self.connect() as c:c.execute('UPDATE archive_events SET delivered=1 WHERE id=?',(event['id'],))
                continue
            if job.get('kind')=='ocr_read':
                archive.reconcile_ocr(event['id'],job['session_id'],job['capture_id'])
                with self.connect() as c:c.execute('UPDATE archive_events SET delivered=1 WHERE id=?',(event['id'],))
                continue
            if job.get('kind')=='ocr_choice':
                archive.confirm_ocr_interval(event['id'],job,job['sku'])
                with self.connect() as c:c.execute('UPDATE archive_events SET delivered=1 WHERE id=?',(event['id'],))
                continue
            targets=json.loads(job.get('targets') or '[]');sku=None
            if targets:
                with self.connect() as c:
                    row=c.execute('SELECT sku FROM sku_rows WHERE id=?',(targets[0],)).fetchone();sku=row[0] if row else None
            if job.get('kind')=='ocr_interval':archive.confirm_ocr_interval(event['id'],job,sku)
            else:archive.handle_event(event['id'],job,sku)
            with self.connect() as c:c.execute('UPDATE archive_events SET delivered=1 WHERE id=?',(event['id'],))

    def metadata_changed(self,sid,cid,before,after):
        if before==after:return
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO metadata_versions VALUES (?,?,?,?)',(sid,cid,before,after))

    def _refresh_progress(self,c,pid):
        targets={}
        for job in c.execute("SELECT * FROM jobs WHERE project=? AND status IN ('matched','confirmed') ORDER BY captured_at,created,id",(pid,)):
            for rid in json.loads(job['targets']):
                prior=targets.get(rid)
                if prior is None or completion_order(job)>completion_order(prior):targets[rid]=job
        c.execute("UPDATE sku_rows SET status=baseline,completed_at=baseline_completed_at,completion_job='' WHERE project=?",(pid,))
        for rid,job in targets.items():c.execute("UPDATE sku_rows SET status='已拍摄',completed_at=?,completion_job=? WHERE project=? AND id=?",(job['captured_at'],job['id'],pid,rid))
        return targets

    def export(self,pid):
        with self.lock:
            p=self.project(pid)
            if not p:raise ValueError('对账任务不存在')
            with self.connect() as c:
                targets=self._refresh_progress(c,pid)
            rows=self.rows(pid);metadata=p['metadata'];plans=[]
            if metadata.get('managed_sheet'):
                from sku_workbook_auto import replace_sheet
                source=self.root/pid/'source.xlsx';name=metadata['managed_sheet']
                dynamic=[]
                for row in rows:
                    if row['sheet']!=name:continue
                    values=json.loads(row.get('source_values') or '{}')
                    dynamic.append([row['sku'],values.get('B',''),values.get('C',''),values.get('D',''),row['status'],row.get('completed_at','')])
                content,part=replace_sheet(source.read_bytes(),name,dynamic);temp=source.with_suffix('.tmp.xlsx');temp.write_bytes(content);temp.replace(source)
                for sheet in metadata['sheets']:
                    if sheet['name']==name:sheet['path']=part
                with self.connect() as c:c.execute('UPDATE projects SET metadata=? WHERE id=?',(json.dumps(metadata,ensure_ascii=False),pid))
            for sheet in metadata['sheets']:
                plan=dict(sheet);plan['rows']=[r for r in rows if r['sheet']==sheet['name']];plans.append(plan)
            source=self.root/pid/'source.xlsx';output=self.root/pid/'拍摄对账.xlsx'
            if metadata.get('automatic_day'):output=self.root/pid/(metadata['automatic_day']+'.xlsx')
            try:
                from sku_reconciliation import decorate_images,export_images
                for row in rows:row['completion']=dict(targets[row['id']]) if row['id'] in targets else None
                decorate_images(self,rows)
                from sku_presentation import preferences,grouped_rows
                from sku_workbook_presentation import finalize
                pictures=export_images(self,rows);working=output.with_suffix('.work.xlsx')
                try:
                    export_workbook(source,working,plans,{(r['sheet'],r['excel_row']):r['status'] for r in rows},{(r['sheet'],r['excel_row']):r['completed_at'] for r in rows},capture_images=pictures)
                    finalize(working,plans,preferences(self,pid)['headers'],grouped_rows(rows,metadata['sheets']),pictures)
                    working.replace(output)
                finally:working.unlink(missing_ok=True)
                with self.connect() as c:c.execute("UPDATE projects SET export_error='' WHERE id=?",(pid,))
            except Exception as exc:
                with self.connect() as c:c.execute('UPDATE projects SET export_error=? WHERE id=?',('Excel 更新失败，请关闭占用文件后重试：'+str(exc)[:150],pid))
                raise ValueError('Excel 工作副本更新失败，识别记录仍保留在数据库') from None
            return output

    def _run(self):
        self.label_thread.start()
        while not self.closed.is_set():
            self.wake.clear()
            try:self.tick()
            except Exception:pass  # 已保存的任务和导出错误可在界面查询；下次循环继续。
            self.wake.wait(.2)

    def close(self):
        if getattr(self,'metadata_sync',None):self.metadata_sync.close()
        self.closed.set()
        self._wake_label()
        if self.thread.is_alive():self.thread.join(timeout=2)
        self._close_label_workers()
