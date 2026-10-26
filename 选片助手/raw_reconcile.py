"""先 JPG 后 RAW 的跨目录匹配、明确评级继承与 RAW 内嵌补写。"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import threading
import time

import media_engine as media
from asset_archive import uid,dumps
from metadata_writer import sha,EMBEDDED_RAW

def text(v):return str(v or '').strip().casefold()
def serial(value):
    s=re.sub(r'\s+','',text(value))
    if not s or not re.sub('0','',s) or s in ('unknown','none','n/a'):return ''
    return str(int(s)) if s.isdigit() else s
def identity(record):
    if '_identity' in record:return record['_identity']
    m=record.get('captureIdentity') or {};stamp=str(m.get('DateTimeOriginal',''))
    full=str(m.get('SubSecDateTimeOriginal',''))
    if not stamp:stamp=full[:19] or str(record.get('time') or '')[:19]
    stamp=stamp.replace(':','-',2).replace(' ','T')[:19]
    try:datetime.fromisoformat(stamp)
    except ValueError:stamp=''
    sub=str(m.get('SubSecTimeOriginal',''))
    if not sub:
        part=re.search(r'\.(\d+)',full or str(record.get('time') or ''));sub=part.group(1) if part else ''
    if sub and not sub.isdigit():sub=''
    precision=len(sub);sub=(sub.rstrip('0') or '0') if sub else ''
    model=text(m.get('Model') or record.get('camera'));camera_serial=serial(m.get('SerialNumber') or m.get('BodySerialNumber') or m.get('InternalSerialNumber'))
    if not camera_serial and ':' in str(record.get('cameraKey','')):camera_serial=serial(record['cameraKey'].split(':',1)[1])
    name=m.get('OriginalRawFileName') or m.get('RawFileName') or record.get('name') or Path(record.get('jpg') or record.get('raw') or '').stem
    stem=text(Path(str(name)).stem);unique=re.sub(r'[^a-z0-9]','',text(m.get('ImageUniqueID')))
    if not unique or len(unique)<12 or len(set(unique))==1:unique=''
    return {'model':model,'serial':camera_serial,'time':stamp,'sub':sub,'precision':precision,'stem':stem,'number':text(m.get('FileNumber') or m.get('ImageNumber')),'unique':unique}

def compare(raw,source):
    a=identity(raw);b=identity(source)
    if not a['model'] or a['model'] in ('未知相机','unknown') or a['model']!=b['model']:return None
    if not a['time'] or a['time']!=b['time']:return None
    if a['serial'] and b['serial'] and a['serial']!=b['serial']:return None
    if a['sub'] and b['sub'] and a['sub']!=b['sub']:return None
    shared_serial=bool(a['serial'] and a['serial']==b['serial'])
    shared_sub=bool(a['sub'] and a['sub']==b['sub'])
    filename=bool(a['stem'] and a['stem']==b['stem'])
    number=bool(a['number'] and a['number']==b['number'])
    unique=bool(a['unique'] and a['unique']==b['unique'])
    evidence={'camera':a['model'],'time':a['time'],'serial_equal':shared_serial,'subsecond_equal':shared_sub,'filename_equal':filename,'number_equal':number,'unique_id_equal':unique}
    # 相机编号+同秒+文件身份，或同一原始图像标识；缺序列号时要求子秒与文件身份。
    fine_sub=shared_sub and min(a['precision'],b['precision'])>=2
    certain=(shared_serial and (filename or number or unique or fine_sub)) or (unique and (filename or shared_sub)) or (fine_sub and (filename or number))
    return {'certain':certain,'evidence':evidence,'score':int(shared_serial)*8+int(unique)*10+int(shared_sub)*4+int(filename)*3+int(number)*3}

class RawReconcile:
    def __init__(self,archive,*,autostart=True):
        self.archive=archive;self.store=archive.store;self.lock=threading.RLock();self.closed=threading.Event();self.pending=set();self.version=0
        with archive.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS raw_sources(session TEXT,capture TEXT,record TEXT,rating INTEGER,updated REAL,PRIMARY KEY(session,capture));
CREATE TABLE IF NOT EXISTS raw_pairs(id TEXT PRIMARY KEY,session TEXT,capture TEXT,status TEXT,reason TEXT,source_session TEXT,source_capture TEXT,candidates TEXT DEFAULT '[]',signature TEXT DEFAULT '',operation TEXT DEFAULT '',managed_rating INTEGER,updated REAL,UNIQUE(session,capture));
CREATE TABLE IF NOT EXISTS raw_session_work(session TEXT PRIMARY KEY,status TEXT,reason TEXT DEFAULT '',updated REAL);
CREATE TABLE IF NOT EXISTS archive_sync_targets(id TEXT PRIMARY KEY,folder TEXT UNIQUE,session TEXT,status TEXT DEFAULT 'ready',scan TEXT DEFAULT '',reason TEXT DEFAULT '',updated REAL);
CREATE INDEX IF NOT EXISTS raw_pairs_status ON raw_pairs(session,status,updated DESC);
CREATE INDEX IF NOT EXISTS raw_pairs_updated ON raw_pairs(session,updated DESC);''')
            if 'discover' not in {r[1] for r in c.execute('PRAGMA table_info(raw_session_work)')}:c.execute('ALTER TABLE raw_session_work ADD COLUMN discover INTEGER DEFAULT 0')
            if 'capture_ids' not in {r[1] for r in c.execute('PRAGMA table_info(raw_session_work)')}:c.execute('ALTER TABLE raw_session_work ADD COLUMN capture_ids TEXT')
            if 'verification' not in {r[1] for r in c.execute('PRAGMA table_info(archive_sync_targets)')}:c.execute("ALTER TABLE archive_sync_targets ADD COLUMN verification TEXT DEFAULT '{}'")
            import task_history
            task_history.initialize(c, 'archive_sync_targets')
            self.pending.update(r[0] for r in c.execute("SELECT session FROM raw_session_work WHERE status IN ('queued','running') OR discover=1"))
        archive.on_assignment=self.assignments_changed
        self.thread=threading.Thread(target=self._run,name='jpg-raw-reconcile',daemon=True)
        if autostart:self.thread.start()
    def targets(self):
        with self.archive.connect() as c:return [dict(r) for r in c.execute("SELECT * FROM archive_sync_targets ORDER BY CASE WHEN status IN ('indexing','matching','verifying') THEN 0 ELSE 1 END,updated DESC")]
    def add_target(self,folder):
        if not isinstance(folder,str) or not folder.strip().strip('"'):raise ValueError('请填写最终归档文件夹路径')
        folder=str(Path(folder.strip().strip('"')).absolute())
        sid=hashlib.sha256(folder.casefold().encode()).hexdigest()[:24]
        with self.archive.connect() as c:
            c.execute("INSERT OR IGNORE INTO archive_sync_targets(id,folder,session,updated) VALUES (?,?,?,?)",(sid,folder,sid,time.time()))
        return {'target_id':sid,'targets':self.targets()}
    def sync_target(self,target_id):
        with self.lock:
            with self.archive.connect() as c:target=c.execute('SELECT * FROM archive_sync_targets WHERE id=?',(target_id,)).fetchone()
            if not target:raise ValueError('归档文件夹不存在，请先添加')
            if target['status'] not in ('indexing','matching','verifying'):
                scan=self.store.scan(target['folder'],activate=False)
                with self.archive.connect() as c:c.execute("UPDATE archive_sync_targets SET scan=?,status='indexing',reason='',verification='{}',updated=? WHERE id=?",(scan,time.time(),target_id))
        return self.state(target['session'])
    def refresh_targets(self):
        for target in self.targets():
            if target['status']=='indexing':
                job=self.store.jobs.get(target['scan'])
                if not job:
                    status,reason='failed','上次扫描已中断，请重新同步'
                elif job['status'] in ('failed','cancelled'):
                    status,reason='failed',job.get('error') or '扫描已停止，请重新同步'
                elif job['status']=='completed':
                    self.request(job['session_id']);status,reason='matching',''
                else:continue
                with self.archive.connect() as c:c.execute('UPDATE archive_sync_targets SET status=?,reason=?,session=? WHERE id=?',(status,reason,(job or {}).get('session_id',target['session']),target['id']))
            elif target['status']=='matching':
                with self.archive.connect() as c:
                    work=c.execute('SELECT * FROM raw_session_work WHERE session=?',(target['session'],)).fetchone()
                    pending=c.execute("SELECT 1 FROM raw_pairs WHERE session=? AND status IN ('queued','retrying') LIMIT 1",(target['session'],)).fetchone()
                    if work and work['status'] in ('completed','failed') and not pending:
                        c.execute('UPDATE archive_sync_targets SET status=?,reason=? WHERE id=?',('verifying' if work['status']=='completed' else 'failed',work['reason'],target['id']))
            elif target['status']=='verifying':self.verify_target(target)
    def verify_target(self,target):
        sid=target['session'];records=self.store.records(sid);paths=[(r,k) for r in records for k in ('raw','jpg') if r.get(k)]
        report={'status':'running','total':len(paths),'checked':0,'valid':0,'missing_assets':[],'missing_components':[],'errors':[]}
        def save(final=False):
            with self.archive.connect() as c:
                c.execute("UPDATE archive_sync_targets SET verification=?,status=? WHERE id=? AND status='verifying' AND updated=?",(dumps(report),'completed' if final else 'verifying',target['id'],target['updated']))
        save()
        with self.archive.connect() as c:
            record_ids={r['id'] for r in records}
            pairs={r['capture']:dict(r) for r in c.execute('SELECT * FROM raw_pairs WHERE session=?',(sid,)) if r['capture'] in record_ids}
            fields={(r['operation'],r['path']):json.loads(r['fields']) for r in c.execute('SELECT j.operation,j.path,j.fields FROM metadata_jobs j JOIN raw_pairs p ON p.operation=j.operation WHERE p.session=?',(sid,))}
            source_sessions={p['source_session'] for p in pairs.values() if p['source_session']}
            expected={}
            for row in c.execute('SELECT s.*,l.asset,a.sku FROM raw_sources s LEFT JOIN catalog_links l ON l.session=s.session AND l.capture=s.capture LEFT JOIN assets a ON a.id=l.asset'):
                if row['session'] in source_sessions and (row['rating'] is not None or row['sku']):expected[row['asset'] or (row['session'],row['capture'])]=json.loads(row['record']).get('jpg',row['capture'])
            matched=set()
            for pair in pairs.values():
                link=c.execute('SELECT asset FROM catalog_links WHERE session=? AND capture=?',(pair['source_session'],pair['source_capture'])).fetchone()
                if pair['source_session']:matched.add(link[0] if link else (pair['source_session'],pair['source_capture']))
        report['missing_assets']=[cid for key,cid in expected.items() if key not in matched]
        try:
            _,inventory=media.discover(target['folder'],cancel=self.closed)
            known={str(Path(target['folder'])/r[k]).casefold() for r,k in paths}
            extras=[str(p) for primary,companion in inventory for p in (primary,companion) if p and str(p).casefold() not in known]
            if extras:report['errors'].append(f'新增 {len(extras)} 个文件尚未匹配，请重新同步')
            for record in records:
                for kind in ('raw','jpg'):
                    if not record.get(kind):report['missing_components'].append(record.get('name',record['id'])+' · '+kind.upper())
            for record,kind in paths:
                if self.closed.is_set():return
                path=Path(target['folder'])/record[kind];pair=pairs.get(record['id']);wanted=fields.get((pair['operation'],str(path))) if pair else None
                try:
                    if not path.is_file():raise ValueError('文件缺失')
                    if not pair or pair['status']!='applied' or not wanted:raise ValueError('未完成元数据写入')
                    actual=self.archive.writer.read_asset(path)
                    for key,value in wanted.items():
                        got=actual.get('XMP-xmp:Rating' if key=='Rating' else 'XMP-fk:'+key)
                        equal=got==value if key=='Rating' else str(got or '')==str(value or '')
                        if not equal and not (key in ('AssetId','FileId') and str(got).replace('-','')==str(value).replace('-','')):raise ValueError('读回不一致：'+key)
                    report['valid']+=1
                except (OSError,ValueError) as exc:report['errors'].append(record[kind]+' · '+str(exc))
                report['checked']+=1;save()
        except Exception as exc:report['errors'].append(str(exc)[:300])
        report['status']='attention' if report['errors'] or report['missing_assets'] or report['missing_components'] else 'completed'
        report['source_assets']=len(expected);report['matched_assets']=len(matched);save(final=True)
    def is_target(self,sid):
        with self.archive.connect() as c:return c.execute('SELECT 1 FROM archive_sync_targets WHERE session=?',(sid,)).fetchone() is not None
    def task_state(self, *, limit=20):
        """解析页的真实文件级进度；新一轮核对前不沿用旧轮的成功计数。"""
        batches=[]
        for target in self.targets():
            if target['status']=='ready' or target['task_hidden']:continue
            if limit is not None and len(batches)>=limit:break
            batch={**target,'verification':json.loads(target['verification'] or '{}'),'counts':{},'total':0,'finished':0,'files':[]}
            scan=self.store.jobs.get(target['scan'])
            if target['status'] in ('indexing','failed'):
                batch['scan']={k:scan.get(k) for k in ('stage','done','total','current')} if scan else {}
                batches.append(batch);continue
            with self.archive.connect() as c:
                pairs={r['capture']:dict(r) for r in c.execute('SELECT * FROM raw_pairs WHERE session=?',(target['session'],))}
                jobs={}
                for row in c.execute('SELECT j.* FROM metadata_jobs j JOIN raw_pairs p ON p.operation=j.operation WHERE p.session=?',(target['session'],)):
                    jobs[(row['operation'],row['path'])]=dict(row)
                sources={(r['session'],r['capture']):json.loads(r['record']) for r in c.execute('SELECT s.session,s.capture,s.record FROM raw_sources s JOIN raw_pairs p ON p.source_session=s.session AND p.source_capture=s.capture WHERE p.session=?',(target['session'],))}
                work=c.execute('SELECT status FROM raw_session_work WHERE session=?',(target['session'],)).fetchone()
            files=[];source_roots={}
            for record in self.store.records(target['session']):
                pair=pairs.get(record['id'])
                if pair and pair['updated']<target['updated']:pair=None
                source_name='';source_path=''
                if pair and pair['source_session']:
                    source=sources.get((pair['source_session'],pair['source_capture']),{});source_name=source.get('jpg') or source.get('raw') or source.get('name','')
                    if pair['source_session'] not in source_roots:source_roots[pair['source_session']]=(self.store.session(pair['source_session']) or {}).get('root','')
                    source_path=str(Path(source_roots[pair['source_session']])/source_name) if source_name else ''
                for kind in ('raw','jpg'):
                    if not record.get(kind):continue
                    path=str(Path(target['folder'])/record[kind]);status=pair['status'] if pair else 'queued';reason=pair['reason'] if pair else '等待匹配现场记录';fields={}
                    job=jobs.get((pair['operation'],path)) if pair and status in ('queued','applied','failed') else None
                    if job:
                        status=job['status'];reason=job['reason'];fields=json.loads(job['fields'])
                    if status in ('waiting_source','unsupported'):status='review'
                    if status=='retrying':status='queued'
                    files.append({'path':path,'name':record[kind],'kind':kind,'format':'RAW' if kind=='raw' else 'JPG','source_name':source_name,'source_path':source_path,'status':status,'reason':reason,'rating':fields.get('Rating'),'sku':fields.get('SKU','')})
                    batch['counts'][status]=batch['counts'].get(status,0)+1
            batch['total']=len(files);batch['finished']=sum(n for status,n in batch['counts'].items() if status not in ('queued','running'))
            if target['status']=='matching':batch['status']='matching' if work and work[0] in ('queued','running') else 'writing'
            if target['status']=='completed' and any(batch['counts'].get(k) for k in ('queued','running')):batch['status']='writing'
            if batch['status']=='completed' and any(batch['counts'].get(k) for k in ('failed','review','unmatched','cancelled')):batch['status']='attention'
            if batch['status']=='completed' and batch['verification'].get('status')=='attention':batch['status']='attention'
            # 进行中和异常靠前；总进度始终来自全部文件，列表限制与其他解析任务一致。
            priority={'running':0,'failed':1,'review':1,'unmatched':1,'queued':2,'applied':3}
            batch['files']=sorted(files,key=lambda f:(priority.get(f['status'],2),f['name']))[:100]
            batches.append(batch)
        return {'batches':batches}

    def clear_completed_tasks(self):
        import task_history
        # 基于实时文件状态与校验结果清理，不能只凭目标目录旧状态判断成功。
        with self.lock:
            ids=[batch['id'] for batch in self.task_state(limit=None)['batches'] if batch['status']=='completed' and batch['verification'].get('status')=='completed']
            with self.archive.connect() as c:
                return sum(c.execute("UPDATE archive_sync_targets SET task_hidden=1 WHERE id=? AND status='completed' AND task_hidden=0", (target_id,)).rowcount for target_id in ids)
    def source_asset(self,source):
        # 使用永久记录，不要求现场 JPG 仍在线；未备案的纯评级照片也可恢复资产。
        with self.archive.lock,self.archive.connect() as c:
            link=c.execute('SELECT asset FROM catalog_links WHERE session=? AND capture=?',(source['session'],source['capture'])).fetchone()
            if link:return link[0]
            record=source.get('parsed') or json.loads(source['record']);aid=uid()
            c.execute('INSERT INTO assets(id,created,captured,camera) VALUES (?,?,?,?)',(aid,time.time(),record.get('time'),record.get('cameraKey','')))
            c.execute('INSERT INTO catalog_links VALUES (?,?,?)',(source['session'],source['capture'],aid))
            return aid
    def request(self,sid,discover=False,capture_ids=None):
        if not sid or not self.store.session(sid):return
        with self.store.connect() as c:
            if c.execute('SELECT 1 FROM settings WHERE key=?',('archive_disabled:'+sid,)).fetchone():return
        if capture_ids is not None:
            capture_ids=list(dict.fromkeys(capture_ids))
            if not capture_ids:return
            if len(capture_ids)>2000:capture_ids=None
        if discover:capture_ids=None
        with self.lock,self.archive.connect() as c:
            previous=c.execute('SELECT status,capture_ids,discover FROM raw_session_work WHERE session=?',(sid,)).fetchone()
            if previous and previous['status']=='queued':
                if previous['capture_ids'] is None or capture_ids is None:capture_ids=None
                else:capture_ids=list(dict.fromkeys(json.loads(previous['capture_ids'])+capture_ids))
            if capture_ids is not None and len(capture_ids)>2000:capture_ids=None
            c.execute("INSERT INTO raw_session_work(session,status,reason,updated,discover,capture_ids) VALUES (?,'queued','',?,?,?) ON CONFLICT(session) DO UPDATE SET status='queued',reason='',updated=excluded.updated,discover=MAX(raw_session_work.discover,excluded.discover),capture_ids=excluded.capture_ids",(sid,time.time(),int(discover),dumps(capture_ids) if capture_ids is not None else None))
            c.execute("UPDATE archive_sync_targets SET status='matching',verification='{}',updated=? WHERE session=? AND status IN ('completed','failed','verifying')",(time.time(),sid))
            if discover:c.execute("UPDATE raw_pairs SET status='retrying',signature='' WHERE session=? AND status='failed'",(sid,))
            self.pending.add(sid)
    def assignments_changed(self,asset_ids):
        with self.archive.connect() as c:
            rows=[r for aid in asset_ids for r in c.execute('SELECT session,capture FROM catalog_links WHERE asset=?',(aid,))]
        sessions={}
        for row in rows:sessions.setdefault(row['session'],[]).append(row['capture'])
        for sid,capture_ids in sessions.items():self.request(sid,capture_ids=capture_ids)
    def enrich(self,sid,records):
        session=self.store.session(sid);missing=[]
        for r in records:
            if r.get('captureIdentity',{}).get('Model'):continue
            try:missing.append((r,media.resolved_file(session['root'],r.get('raw') or r['jpg'])))
            except (OSError,ValueError):continue
        for start in range(0,len(missing),24):
            batch=missing[start:start+24];metadata=media.read_metadata([p for _,p in batch],media.find_exiftool(self.store.root),cancel=self.closed)
            for (r,path),m in zip(batch,metadata):
                r['captureIdentity']=media.capture_record(path.parent,path,None,m)['captureIdentity']
                with self.store.connect() as c:c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(dumps(r),sid,r['id']))
        return records
    def index_sources(self,sid,capture_ids=None):
        session=self.store.session(sid)
        if not session or self.is_target(sid):return False
        if capture_ids is None:self.archive.ratings.seed_session(sid)
        with self.store.connect() as c:shooting=bool(c.execute('SELECT 1 FROM settings WHERE key=?',('shooting_session:'+sid,)).fetchone())
        batches=[self.store.records(sid,ids=capture_ids)] if capture_ids is not None else self.store.iter_records(sid)
        overrides=session['state'].get('overrides',{});changed=False
        for batch in batches:
            if self.closed.is_set():raise media.ScanCancelled()
            if capture_ids is not None:self.archive.ratings.seed_records(session,batch)
            records=self.enrich(sid,[r for r in batch if shooting or (r.get('jpg') and not r.get('raw'))])
            with self.archive.connect() as c:
                for r in records:
                    if self.closed.is_set():raise media.ScanCancelled()
                    rating=max(0,overrides[r['id']]) if r['id'] in overrides else (r.get('existingRating') if r.get('existingRating',0)>0 else None)
                    shared=self.archive.ratings.get(self.archive.ratings.paths(sid,r,session['root']),c)
                    if shared is not None:rating=max(0,shared)
                    old=c.execute('SELECT record,rating FROM raw_sources WHERE session=? AND capture=?',(sid,r['id'])).fetchone()
                    changed=changed or not old or old['record']!=dumps(r) or old['rating']!=rating
                    c.execute('INSERT INTO raw_sources VALUES (?,?,?,?,?) ON CONFLICT(session,capture) DO UPDATE SET record=excluded.record,rating=excluded.rating,updated=excluded.updated',(sid,r['id'],dumps(r),rating,time.time()))
        return changed
    def sources(self):
        with self.archive.connect() as c:return [dict(r) for r in c.execute('SELECT * FROM raw_sources')]
    def _save_pair(self,sid,cid,status,reason,**values):
        with self.archive.connect() as c:
            c.execute('INSERT INTO raw_pairs(id,session,capture,status,reason,updated) VALUES (?,?,?,?,?,?) ON CONFLICT(session,capture) DO UPDATE SET status=excluded.status,reason=excluded.reason,updated=excluded.updated',(uid(),sid,cid,status,reason,time.time()))
            for key,value in values.items():
                if key not in ('source_session','source_capture','candidates','signature','operation','managed_rating'):raise ValueError('配对字段无效')
                c.execute('UPDATE raw_pairs SET '+key+'=? WHERE session=? AND capture=?',(value,sid,cid))
        self.version+=1
        if status=='queued':
            with self.archive.connect() as c:c.execute("UPDATE archive_sync_targets SET status='matching',verification='{}' WHERE session=? AND status='completed'",(sid,))
    def match_one(self,sid,raw,sources,chosen=None,overwrite=False):
        # 与写入队列串行核对，避免读到 RAW/JPG 两组件更新过程中的中间状态。
        with self.archive.lock:return self._match_one(sid,raw,sources,chosen,overwrite)
    def _match_one(self,sid,raw,sources,chosen=None,overwrite=False):
        session=self.store.session(sid);cid=raw['id']
        target_path=media.resolved_file(session['root'],raw.get('raw') or raw['jpg'])
        target_metadata=self.archive.writer.read_asset(target_path)
        embedded_asset=str(target_metadata.get('XMP-fk:AssetId') or '').replace('-','')
        # 同名目录用于限定候选范围；文件身份与拍摄证据仍须独立验证。
        target_parts={part.casefold() for part in Path(raw.get('raw') or raw['jpg']).parent.parts}
        folder_sources=[]
        for source in sources:
            origin=self.store.session(source['session'])
            if origin and Path(origin['root']).name.casefold() in target_parts:folder_sources.append(source)
        if folder_sources:sources=folder_sources
        with self.archive.connect() as c:prior=c.execute('SELECT * FROM raw_pairs WHERE session=? AND capture=?',(sid,cid)).fetchone()
        if prior and prior['operation']:
            with self.archive.connect() as c:active=c.execute("SELECT 1 FROM metadata_jobs WHERE operation=? AND status IN ('queued','running') LIMIT 1",(prior['operation'],)).fetchone()
            if active:
                if prior['status']!='queued':self._save_pair(sid,cid,'queued','正在写入，完成后重新核对来源变更')
                return
        candidates=[]
        for source in sources:
            parsed=source.get('parsed') or json.loads(source['record']);candidate=compare(raw,parsed)
            if embedded_asset:
                with self.archive.connect() as c:link=c.execute('SELECT asset FROM catalog_links WHERE session=? AND capture=?',(source['session'],source['capture'])).fetchone()
                if link and link[0]==embedded_asset:candidate={'certain':True,'evidence':{'embedded_asset_id':True},'score':100}
            if candidate:candidates.append({'session':source['session'],'capture':source['capture'],'filename':parsed.get('jpg') or parsed.get('raw') or parsed.get('name',''),**candidate})
        candidates.sort(key=lambda r:-r['score'])
        if chosen:
            candidate=next((s for s in sources if s['session']==chosen[0] and s['capture']==chosen[1]),None)
            if not candidate:raise ValueError('所选 JPG 来源不存在')
        else:
            certain=[r for r in candidates if r['certain']]
            # 同一永久资产的多个目录副本可归一；未登记身份的候选保持歧义。
            groups={}
            with self.archive.connect() as c:
                for r in certain:
                    a=c.execute('SELECT asset FROM catalog_links WHERE session=? AND capture=?',(r['session'],r['capture'])).fetchone();key=a[0] if a else (r['session'],r['capture'])
                    groups.setdefault(key,[]).append(r)
            if len(groups)!=1:
                self._save_pair(sid,cid,'review' if candidates else 'unmatched','多个可靠 JPG 候选，请确认来源' if len(groups)>1 else '未找到可靠的现场拍摄记录，未写入',candidates=dumps(candidates[:12]));return
            selected=next(iter(groups.values()))
            ratings={s['rating'] for s in sources if any(s['session']==r['session'] and s['capture']==r['capture'] for r in selected)}
            if len(ratings)>1:
                self._save_pair(sid,cid,'review','同一资产的 JPG 评级不一致，请确认来源',candidates=dumps(candidates[:12]));return
            picked=selected[0];candidate=next(s for s in sources if s['session']==picked['session'] and s['capture']==picked['capture'])
        source_sid=candidate['session'];source_cid=candidate['capture']
        aid=self.source_asset(candidate);asset=self.archive.asset(aid);rating=candidate['rating']
        paths=[media.resolved_file(session['root'],raw[k]) for k in ('raw','jpg') if raw.get(k)]
        source_record=json.loads(candidate['record'])
        shared=self.archive.ratings.get(paths+self.archive.ratings.paths(source_sid,source_record),explicit_only=True)
        if shared is not None:rating=max(0,shared)
        if not asset['sku'] and rating is None:
            self._save_pair(sid,cid,'waiting_source','现场照片已找到，等待保存评级或 SKU',source_session=source_sid,source_capture=source_cid,candidates=dumps(candidates[:12]));return
        signature=hashlib.sha256(dumps([source_sid,source_cid,aid,asset['revision'],asset['sku'],asset['scope'],rating]).encode()).hexdigest()
        path=paths[0]
        if any(p.suffix.lower() not in media.RAW_EXTS|{'.jpg','.jpeg'} for p in paths):
            self._save_pair(sid,cid,'unsupported','此格式尚未支持元数据写入',source_session=source_sid,source_capture=source_cid);return
        metadata=target_metadata;existing=metadata.get('XMP-xmp:Rating',raw.get('existingRating',0));manual=session['state'].get('overrides',{}).get(cid)
        expected_prior=prior['managed_rating'] if prior else None
        prior_fields={}
        if prior and prior['operation']:
            with self.archive.connect() as c:
                previous_job=c.execute('SELECT fields,status FROM metadata_jobs WHERE operation=? LIMIT 1',(prior['operation'],)).fetchone()
                if previous_job:
                    prior_fields=json.loads(previous_job['fields'])
                    if previous_job['status']=='applied':expected_prior=prior_fields.get('Rating',expected_prior)
        manual_conflict=rating is not None and manual is not None and max(0,manual)!=rating
        rating_conflict=rating is not None and existing not in (None,0,rating,expected_prior)
        sku_conflict=bool(metadata.get('XMP-fk:SKU') and metadata['XMP-fk:SKU'] not in (asset['sku'],prior_fields.get('SKU')))
        xmp=path.with_suffix('.xmp')
        if xmp.exists():
            side=self.archive.writer.read(xmp)
            if not overwrite and ((rating is not None and side.get('XMP-xmp:Rating') not in (None,0,rating,expected_prior)) or (side.get('XMP-fk:SKU') and side['XMP-fk:SKU'] not in (asset['sku'],prior_fields.get('SKU')))):
                self._save_pair(sid,cid,'review','同名 XMP 有不同评级/SKU，需先处理，未写 RAW',candidates=dumps(candidates[:12]));return
        if manual_conflict:
            self._save_pair(sid,cid,'review','归档目录有不同人工星级；请先核对并在工作台设为来源星级后重试',source_session=source_sid,source_capture=source_cid,candidates=dumps(candidates[:12]));return
        if (rating_conflict or sku_conflict) and not overwrite:
            self._save_pair(sid,cid,'review',f'归档文件已有 {existing or 0} 星 / {metadata.get("XMP-fk:SKU") or "未备案"}；JPG 为 {rating if rating is not None else "未标记"} 星 / {asset["sku"] or "未备案"}，需要明确确认覆盖',source_session=source_sid,source_capture=source_cid,candidates=dumps(candidates[:12]));return
        all_metadata={str(path):metadata}
        for companion in paths[1:]:
            m=self.archive.writer.read_asset(companion);all_metadata[str(companion)]=m
            if not overwrite and ((rating is not None and m.get('XMP-xmp:Rating') not in (None,0,rating,expected_prior)) or (m.get('XMP-fk:SKU') and m['XMP-fk:SKU'] not in (asset['sku'],prior_fields.get('SKU')))):
                self._save_pair(sid,cid,'review','归档 JPG 已有不同星级/SKU，请核对后确认覆盖',source_session=source_sid,source_capture=source_cid,candidates=dumps(candidates[:12]));return
        if prior and prior['signature']==signature and prior['operation']:
            embedded_ok=all(m.get('XMP-fk:SKU','')==asset['sku'] and m.get('XMP-fk:ShootType','')==asset['scope'] and (rating is None or m.get('XMP-xmp:Rating')==rating) for m in all_metadata.values())
            if prior['status'] in ('queued','failed'):return
            if embedded_ok:
                self._save_pair(sid,cid,'applied','元数据一致，无需重复写入');return
        with self.archive.lock:
            with self.archive.connect() as c:
                link=c.execute('SELECT asset FROM catalog_links WHERE session=? AND capture=?',(sid,cid)).fetchone()
                if link and link[0]!=aid:
                    old=c.execute('SELECT * FROM assets WHERE id=?',(link[0],)).fetchone()
                    used=c.execute('SELECT 1 FROM metadata_jobs WHERE asset=? LIMIT 1',(link[0],)).fetchone() or c.execute('SELECT 1 FROM bindings WHERE asset=? LIMIT 1',(link[0],)).fetchone()
                    if old['revision'] or old['sku'] or used:raise ValueError('归档照片已独立备案，请先复核归属')
                    c.execute('UPDATE files SET asset=? WHERE asset=?',(aid,link[0]));c.execute('UPDATE catalog_links SET asset=? WHERE asset=?',(aid,link[0]));c.execute('DELETE FROM assets WHERE id=?',(link[0],))
            self.archive.ensure(sid,cid,asset_hint=aid,inherit=False)
            with self.archive.connect() as c:
                op=uid();c.execute('INSERT INTO operations(id,created,kind,source) VALUES (?,?,?,?)',(op,time.time(),'raw_sync',source_sid+':'+source_cid))
                for path in paths:
                    file=c.execute('SELECT f.* FROM files f JOIN locations l ON l.file=f.id WHERE l.path=?',(str(path),)).fetchone();metadata=all_metadata[str(path)]
                    fields={'AssetId':aid,'FileId':file['id'],'SKU':asset['sku'],'ShootType':asset['scope'],'ProjectId':asset['project'],'SegmentId':asset['segment'],'MetadataRevision':max(1,asset['revision'],int(metadata.get('XMP-fk:MetadataRevision') or 0)+1)}
                    if rating is not None:fields['Rating']=rating
                    c.execute("UPDATE metadata_jobs SET status='cancelled',reason='由归档星级/SKU 联合同步取代' WHERE file=? AND path=? AND status='queued'",(file['id'],str(path)))
                    c.execute('INSERT INTO metadata_jobs(id,operation,file,asset,revision,path,expected_sha,mode,fields,status,created) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uid(),op,file['id'],aid,asset['revision'],str(path),sha(path),'sidecar',dumps(fields),'queued',time.time()))
            self._save_pair(sid,cid,'queued','已匹配现场记录，RAW 使用 XMP，JPG 更新元数据',source_session=source_sid,source_capture=source_cid,candidates=dumps(candidates[:12]),signature=signature,operation=op)
    def process_session(self,sid,capture_ids=None):
        records=self.store.records(sid,ids=capture_ids);archive_folder=self.is_target(sid);raws=[r for r in records if r.get('raw') or (archive_folder and r.get('jpg'))]
        with self.store.connect() as c:
            if not archive_folder and c.execute('SELECT 1 FROM settings WHERE key=?',('shooting_session:'+sid,)).fetchone():raws=[]
            if c.execute('SELECT 1 FROM settings WHERE key=?',('archive_disabled:'+sid,)).fetchone():return
            exclusions=c.execute('SELECT value FROM settings WHERE key=?',('archive_excluded:'+sid,)).fetchone()
        excluded=json.loads(exclusions[0]) if exclusions else []
        raws=[r for r in raws if not any(str(Path(r.get('raw') or r.get('jpg')).parent).replace('\\','/')==folder or str(Path(r.get('raw') or r.get('jpg')).parent).replace('\\','/').startswith(folder+'/') for folder in excluded)]
        changed=self.index_sources(sid,capture_ids)
        if not raws:
            if capture_ids is not None:
                with self.archive.connect() as c:pairs=c.execute('SELECT session,capture FROM raw_pairs WHERE source_session=? AND source_capture IN ('+','.join('?' for _ in capture_ids)+')',(sid,*capture_ids)).fetchall()
                related={}
                for pair in pairs:related.setdefault(pair['session'],[]).append(pair['capture'])
                for related_sid,ids in related.items():self.request(related_sid,capture_ids=ids)
                return
            with self.archive.connect() as c:related={r[0] for r in c.execute('SELECT DISTINCT session FROM raw_pairs WHERE source_session=?',(sid,))}
            if changed:
                with self.store.connect() as c:managed={r[0].split(':',1)[1] for r in c.execute("SELECT key FROM settings WHERE key LIKE 'archive_session:%'")}
                related.update(t['session'] for t in self.targets() if t['status']!='ready' or t['session'] in managed)
            for related_sid in related:self.request(related_sid,discover=True)
            return
        if capture_ids is None:
            with self.store.connect() as c:sessions=[dict(s) for s in c.execute('SELECT id FROM sessions')]
            for s in sessions:
                if s['id']!=sid:self.index_sources(s['id'])
        raws=self.enrich(sid,raws);buckets={};all_sources=[]
        sources=None
        if capture_ids is not None:
            with self.archive.connect() as c:
                known=c.execute('SELECT DISTINCT s.*,p.capture AS target_capture FROM raw_sources s JOIN raw_pairs p ON p.source_session=s.session AND p.source_capture=s.capture WHERE p.session=? AND p.capture IN ('+','.join('?' for _ in capture_ids)+')',(sid,*capture_ids)).fetchall()
            if len({row['target_capture'] for row in known})>=len(raws):sources=[dict(row) for row in known]
        for source in sources if sources is not None else self.sources():
            source['parsed']=json.loads(source['record']);ident=identity(source['parsed']);source['parsed']['_identity']=ident
            buckets.setdefault((ident['model'],ident['time']),[]).append(source)
            all_sources.append(source)
        for raw in raws:
            if self.closed.is_set():return
            ident=identity(raw);sources=buckets.get((ident['model'],ident['time']),[]) or all_sources
            try:
                self.match_one(sid,raw,sources)
            except Exception as exc:self._save_pair(sid,raw['id'],'review',str(exc)[:300])
    def discover_raws(self,sid):
        with self.store.connect() as c:
            if c.execute('SELECT 1 FROM settings WHERE key=?',('archive_session:'+sid,)).fetchone():return
        session=self.store.session(sid)
        if not session:return
        root,shots=media.discover(session['root'],cancel=self.closed)
        previous={r.get('raw') or r.get('jpg'):r for r in self.store.records(sid)};pending=[];archive_folder=self.is_target(sid)
        for raw,companion in shots:
            if raw.suffix.lower() not in media.RAW_EXTS and not archive_folder:continue
            relative=raw.relative_to(root).as_posix();old=previous.get(relative)
            if old and old['fingerprint']==media.fingerprint([raw]+([companion] if companion else [])):continue
            pending.append((raw,companion))
        for start in range(0,len(pending),24):
            batch=pending[start:start+24];metadata=media.read_metadata([p for p,_ in batch],media.find_exiftool(self.store.root),cancel=self.closed)
            records=[media.capture_record(root,p,q,m) for (p,q),m in zip(batch,metadata) if not m.get('Error')]
            if records:self.store.append_live(sid,records)
    def refresh_completed(self):
        with self.archive.connect() as c:rows=c.execute("SELECT * FROM raw_pairs WHERE status='queued'").fetchall()
        for row in rows:
            op=self.archive.operation(row['operation']);statuses={j['status'] for j in op['jobs']}
            if statuses=={'applied'}:
                rating=None
                with self.archive.connect() as c:
                    job=c.execute('SELECT fields FROM metadata_jobs WHERE operation=? LIMIT 1',(row['operation'],)).fetchone();rating=json.loads(job[0]).get('Rating')
                with self.store.connect() as c:
                    record=c.execute('SELECT data FROM captures WHERE session=? AND id=?',(row['session'],row['capture'])).fetchone()
                    if record:
                        r=json.loads(record[0]);r['liveRating']=rating if rating is not None else r.get('existingRating',0)
                        if rating is not None:r['existingRating']=rating
                        c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(dumps(r),row['session'],row['capture']))
                self._save_pair(row['session'],row['capture'],'applied','星级与 SKU 已同步到归档元数据' if rating is not None else 'SKU 已同步；现场无明确评级，原星级保持',managed_rating=rating)
                self.request(row['session'],capture_ids=[row['capture']])
            elif 'failed' in statuses:self._save_pair(row['session'],row['capture'],'failed','；'.join(j['reason'] for j in op['jobs'] if j['status']=='failed')[:350])
            elif statuses=={'cancelled'}:
                self._save_pair(row['session'],row['capture'],'cancelled','来源已变更，旧任务已取消');self.request(row['session'],capture_ids=[row['capture']])
    def review(self,pair_id,source_session,source_capture,overwrite=False):
        with self.archive.connect() as c:pair=c.execute('SELECT * FROM raw_pairs WHERE id=?',(pair_id,)).fetchone()
        if not pair:raise ValueError('RAW 配对任务不存在')
        raw=self.store.records(pair['session'],ids=[pair['capture']])[0]
        self.match_one(pair['session'],raw,self.sources(),(source_session,source_capture),overwrite)
        return self.state(pair['session'])
    def retry(self,pair_id):
        with self.archive.connect() as c:row=c.execute('SELECT * FROM raw_pairs WHERE id=?',(pair_id,)).fetchone()
        if not row:raise ValueError('配对记录不存在')
        if row['operation']:
            with self.archive.connect() as c:job=c.execute('SELECT journal FROM metadata_jobs WHERE operation=? LIMIT 1',(row['operation'],)).fetchone()
            journal=json.loads(job[0]) if job else {}
            if journal.get('target') and Path(journal['target']).is_file() and sha(journal['target'])==journal.get('after_hash'):
                self.archive.retry(row['operation']);self._save_pair(row['session'],row['capture'],'queued','恢复已提交的文件记录')
            else:
                self._save_pair(row['session'],row['capture'],'retrying','重新核对文件版本',signature='');self.request(row['session'],discover=True)
        else:self.request(row['session'],discover=True)
        return self.state(row['session'])
    def state(self,sid=None,status='',offset=0):
        conditions=[];args=[]
        if sid:conditions.append('session=?');args.append(sid)
        base=' WHERE '+' AND '.join(conditions) if conditions else ''
        with self.archive.connect() as c:
            counts={r['status']:r['n'] for r in c.execute('SELECT status,count(*) AS n FROM raw_pairs'+base+' GROUP BY status',args)}
            if status:conditions.append('status=?');args.append(status)
            where=' WHERE '+' AND '.join(conditions) if conditions else ''
            total=c.execute('SELECT count(*) FROM raw_pairs'+where,args).fetchone()[0]
            rows=[dict(r) for r in c.execute('SELECT * FROM raw_pairs'+where+' ORDER BY updated DESC LIMIT 100 OFFSET ?',args+[max(0,int(offset))])]
            work=[dict(r) for r in c.execute('SELECT * FROM raw_session_work ORDER BY updated DESC LIMIT 20')]
            sources={}
            for row in rows:
                key=(row['source_session'],row['source_capture'])
                if key[0] and key not in sources:
                    source=c.execute('SELECT * FROM raw_sources WHERE session=? AND capture=?',key).fetchone()
                    if source:sources[key]=dict(source)
        catalog={}
        for row in rows:
            if row['session'] not in catalog:catalog[row['session']]={r['id']:r for r in self.store.records(row['session'],ids=[p['capture'] for p in rows if p['session']==row['session']])}
            row['candidates']=json.loads(row['candidates']);record=catalog[row['session']].get(row['capture'],{});row['filename']=record.get('raw') or record.get('jpg') or row['capture']
            source=sources.get((row['source_session'],row['source_capture']))
            if source:
                source_record=json.loads(source['record']);row['source_filename']=source_record.get('jpg') or source_record.get('raw');row['source_rating']=source['rating']
        targets=self.targets()
        for target in targets:
            job=self.store.jobs.get(target['scan'])
            if job:target['progress']={k:job.get(k) for k in ('stage','done','total','current')}
        return {'version':self.version,'pairs':rows,'work':work,'counts':counts,'total':total,'offset':offset,'targets':targets}
    def tick(self):
        self.refresh_completed()
        self.refresh_targets()
        with self.lock:
            sid=self.pending.pop() if self.pending else None
            if sid:
                with self.archive.connect() as c:
                    work=c.execute('SELECT updated,discover,capture_ids FROM raw_session_work WHERE session=?',(sid,)).fetchone()
                    if work is None:return
                    stamp=work['updated']
                    c.execute("UPDATE raw_session_work SET status='running',discover=0 WHERE session=?",(sid,))
        if sid:
            try:
                if work['discover']:self.discover_raws(sid)
                self.process_session(sid,json.loads(work['capture_ids']) if work['capture_ids'] is not None and not work['discover'] else None)
                with self.archive.connect() as c:c.execute("UPDATE raw_session_work SET status='completed',reason='' WHERE session=? AND updated=?",(sid,stamp))
            except Exception as exc:
                with self.archive.connect() as c:c.execute('UPDATE raw_session_work SET status=?,reason=? WHERE session=? AND updated=?',('queued' if self.closed.is_set() else 'failed','应用退出，等待恢复' if self.closed.is_set() else str(exc)[:300],sid,stamp))
    def _run(self):
        while not self.closed.is_set():
            try:self.tick()
            except Exception:pass
            self.closed.wait(.5)
    def close(self):
        self.closed.set()
        if self.thread.is_alive():self.thread.join(timeout=3)
