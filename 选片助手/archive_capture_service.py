"""归档与现场共用的随行元数据、二星修正、文件级恢复及目录导出。"""
from collections import defaultdict
from contextlib import contextmanager,nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import string
import threading
import time
import uuid

from portable_capture_store import Dataset, DatasetBusy, digest, encoded, relative_path, resolve_under, BUSINESS_FIELDS
from sku_folder_archive import camera_metadata, capture_key
from sku_intervals import partition
from sku_match import normalize
from capture_metadata_reconcile import parse_metadata,reconcile,conflict_data,merge_fields,boundary,READER_VERSION

RECOVERY_FIELDS = ['BaseSKU', 'ParentSKU', 'ShootVersion', 'PhotoRole', 'ShootDate',
                   'AssignmentState', 'DecisionId', 'DecisionParents', 'DecisionSourceId',
                   'MetadataSchemaVersion', 'CatalogScopeId', 'Customer', 'Brand',
                   'GroupRevision', 'GroupProductCount', 'GroupLabelCount','GroupFileCount','GroupFilesDigest', 'GroupMembersDigest', 'GroupManifestScopeId']


def valid_sku(value, *, empty=False):
    if not isinstance(value, str) or len(value) > 80 or any(ord(ch) < 32 for ch in value):
        raise ValueError('SKU 须为最多 80 字符的文字，不能包含控制字符')
    value = value.strip()
    if not value and not empty:
        raise ValueError('请输入正确 SKU')
    return value


def uuid_value(value, fallback=None):
    try:
        return uuid.UUID(str(value)).hex
    except (ValueError, TypeError, AttributeError):
        return fallback or uuid.uuid4().hex


def file_signature(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns]


def xmp_signature(path,*,sidecars=None):
    from rating_xmp import sidecar_paths
    parts=[]
    for side in sidecar_paths(path) if sidecars is None else sidecars:
        with side.open('rb') as stream:parts.append([side.name,hashlib.file_digest(stream,'sha256').hexdigest()])
    return digest(encoded(parts))


def decision_parents(record):
    return list(dict.fromkeys(v for v in [record.get('decision_id'),*record.get('parents',[])] if v))


class ArchiveCaptureService:
    def __init__(self, store, archive, catalog, sku=None, *, autostart=True, watch=None, readonly=None, metadata_workers=None):
        self.store, self.archive, self.catalog, self.sku = store, archive, catalog, sku
        self.readonly=readonly or (lambda:False)
        self.watch = watch
        self.datasets = {}
        self.lock = threading.RLock()
        from capture_io_lock import CaptureIOLock
        self.io_lock = CaptureIOLock()
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.metadata_request_start_lock=threading.Lock()
        self.metadata_request_thread=None
        from archive_resources import PREVIEW_WORKERS
        self.metadata_workers=max(1,int(PREVIEW_WORKERS if metadata_workers is None else metadata_workers))
        self.metadata_work_lock=threading.RLock()
        self.metadata_executor=None
        self.metadata_inflight={}
        self.metadata_active_keys=set()
        self.metadata_dispatch_thread=None
        self.metadata_wake=threading.Event()
        self.errors = {}
        self.loading = {}
        self.last_scan = {}
        self.pending = {}
        self.live_sessions = {}
        self.validated = {}
        self.scan_cursor = {}
        self.active_root = ''
        self.scan_page = {}
        self.scan_reads = {}
        self.backup_scan_scope = threading.local()
        # 锁外取得的目录浅枚举；锁内扫描直接采用，避免在交互锁内等待NAS。
        self.scan_versions_ready = None
        # 本进程内已完成过整轮读取的数据集；例行复核不再阻挡补写请求。
        self.scan_complete = {}
        self.scan_window = {}
        self.scan_interval = {}
        self.scan_progress = {}
        self.pass_signature = {}
        self.authority_cache = {}
        self.request_items = {}
        self.projection_pending = {}
        self.refreshed = set()
        self.capture_assets = defaultdict(lambda:defaultdict(set))
        self.outbox_after=0
        self.registry = store.data / 'portable-capture' / 'registry.json'
        self.restore_roots=[];self.passive_roots=[];self.restoring=''
        self._install_evidence()
        archive.capture_service = self
        archive.writer.capture_service = self
        catalog.capture_service = self
        store.capture_service = self
        from capture_publish import CapturePublisher
        self.publisher=CapturePublisher(autostart=autostart)
        self.thread = threading.Thread(target=self._run, daemon=True, name='portable-capture')
        if self.registry.is_file():
            try:
                self.restore_roots=list(json.loads(self.registry.read_text(encoding='utf8')))
                self.restore_roots.sort(key=lambda root:not self._resume_needed(root))
            except (OSError, ValueError):
                pass
        if autostart:
            self.thread.start()

    def _install_evidence(self):
        self.archive.ratings
        with self.archive.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS capture_metadata_requests(
                id TEXT PRIMARY KEY,root TEXT NOT NULL,folder TEXT NOT NULL,status TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',result TEXT NOT NULL DEFAULT '{}',due REAL NOT NULL DEFAULT 0,
                updated REAL NOT NULL,UNIQUE(root,folder))""")
            c.execute("UPDATE capture_metadata_requests SET status='queued',reason='上次补写请求正在恢复',due=0 WHERE status='preparing'")
            if 'requested' not in {r[1] for r in c.execute('PRAGMA table_info(capture_metadata_requests)')}:
                c.execute('ALTER TABLE capture_metadata_requests ADD COLUMN requested REAL NOT NULL DEFAULT 0')
            # 手动确认进度独立保存，不能覆盖仍在等待目录读取的补写请求。
            c.execute('CREATE TABLE IF NOT EXISTS capture_metadata_operations(id TEXT PRIMARY KEY,root TEXT NOT NULL,folder TEXT NOT NULL,result TEXT NOT NULL,updated REAL NOT NULL)')
            c.execute('CREATE INDEX IF NOT EXISTS capture_metadata_operation_scope ON capture_metadata_operations(root,folder,updated DESC)')
            c.execute('CREATE TABLE IF NOT EXISTS portable_sku_evidence(path TEXT PRIMARY KEY,data TEXT NOT NULL,revision TEXT,updated REAL)')
            # SKU 读投影可能已经初始化；新来源须补上变更触发器。
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='sku_browser_revision'").fetchone():
                for event in ('INSERT', 'UPDATE', 'DELETE'):
                    c.execute(f'CREATE TRIGGER IF NOT EXISTS sku_browser_portable_sku_evidence_{event.lower()} AFTER {event} ON portable_sku_evidence BEGIN UPDATE sku_browser_revision SET value=value+1 WHERE id=1; END')
            for event in ('INSERT', 'UPDATE', 'DELETE'):
                c.execute(f'CREATE TRIGGER IF NOT EXISTS portable_rating_view_{event.lower()} AFTER {event} ON portable_ratings BEGIN UPDATE rating_view_version SET value=value+1 WHERE id=1; END')

    def _dataset(self, root):
        root = str(Path(root).resolve(strict=True))
        with self.lock:
            if root not in self.datasets:
                dataset = Dataset(root, self.store.data)
                # 历史待写只允许从资产归档手动补写；保留已手动提交的断点任务。
                from metadata_backfill import pause_pending
                with dataset.connect() as c:pause_pending(c,'tasks','data')
                dataset.publish_callback=self.publisher.request
                self.datasets[root] = dataset
                self.pending[root] = True
                self.registry.parent.mkdir(exist_ok=True)
                from file_safety import atomic_write
                atomic_write(self.registry, encoded(list(dict.fromkeys([*self.datasets,*self.restore_roots,*self.passive_roots,*([self.restoring] if self.restoring else [])]))))
            return self.datasets[root]

    def scope(self, root_id, folder='', *, activate=True):
        from folder_archive_sources import FolderArchiveSources
        chosen = FolderArchiveSources(self.store, self.archive, self.catalog).selection(root_id, folder)
        path = Path(chosen['path'])
        with self.catalog.connect() as c:
            registered = Path(c.execute('SELECT path FROM roots WHERE id=?', (root_id,)).fetchone()[0]).resolve()
        base = path
        for parent in (path, *path.parents):
            if not parent.is_relative_to(registered):
                break
            if (parent / '.shiying/dataset.json').is_file():
                base = parent
                break
        dataset = self._dataset(base)
        # 后台补写请求只解析范围，不能把浏览中的目录优先级切到请求目录。
        if activate:self.active_root=str(dataset.root)
        return dataset, path

    def _authoritative(self,dataset,ttl=5):
        """独立数据集标记很少变化；只读视图短时复用，避免每次轮询逐个探测NAS。"""
        key=str(dataset.root);now=time.monotonic()
        cached=self.authority_cache.get(key)
        if cached and now-cached[0]<ttl:return cached[1]
        value=(dataset.folder/'dataset.json').is_file()
        self.authority_cache[key]=(now,value)
        return value

    def dataset_for_path(self, path):
        path = Path(path).absolute()
        with self.lock:
            candidates = [dataset for dataset in self.datasets.values() if path.is_relative_to(dataset.root)]
        authoritative = [d for d in candidates if (d.folder / 'dataset.json').is_file()]
        return max(authoritative or candidates, key=lambda d: len(d.root.parts)) if candidates else None

    @staticmethod
    def _check_new_root(dataset):
        if (dataset.folder/'dataset.json').is_file():return
        for parent in dataset.root.parents:
            if (parent/'.shiying/dataset.json').is_file():
                raise DatasetBusy('此目录属于上级拍摄数据集，请添加并进入该数据集根目录取得编辑权限：'+str(parent))

    def guard(self, paths):
        if self.readonly():raise DatasetBusy('正在单向接收，请断开同步后修改本机资料')
        for path in paths:
            dataset = self.dataset_for_path(path)
            if dataset is None:
                with self.catalog.connect() as c:roots=[Path(r[0]).absolute() for r in c.execute('SELECT path FROM roots WHERE removed=0')]
                roots.extend(Path(s['root']).absolute() for s in self.store.sessions())
                source=Path(path).absolute();allowed=[r for r in roots if source.is_relative_to(r)]
                if allowed:
                    boundary=min(allowed,key=lambda r:len(r.parts))
                    for parent in (source.parent,*source.parent.parents):
                        if not parent.is_relative_to(boundary):break
                        if (parent/'.shiying/dataset.json').is_file():
                            dataset=self._dataset(parent);break
            if dataset:
                source=Path(path).absolute()
                for parent in (source.parent,*source.parent.parents):
                    if not parent.is_relative_to(dataset.root) or parent==dataset.root:break
                    if (parent/'.shiying/dataset.json').is_file():
                        dataset=self._dataset(parent);break
                dataset.ensure_ownership()
                if dataset.meta('archive_freeze'):raise DatasetBusy('此数据集正在等待归档，源文件写入暂缓，执行或取消归档后恢复')

    def frozen_roots(self):
        return [str(d.root) for d in list(self.datasets.values()) if d.meta('archive_freeze')]

    def blocked_io_roots(self):
        return list(dict.fromkeys([*self.frozen_roots(),*self.restore_roots,*([self.restoring] if self.restoring else [])]))

    def check_editable(self,dataset):
        if self.readonly():raise DatasetBusy('正在单向接收，请断开同步后修改本机资料')
        dataset.ensure_ownership()
        if dataset.meta('archive_freeze'):raise DatasetBusy('此数据集有待执行的归档计划，请先完成或取消计划再修正')

    def _acquire_editor(self,dataset,*,metadata_scope=None,item_path=None):
        if self.readonly():raise DatasetBusy('正在单向接收，请断开同步后标星或修改款号')
        self._check_new_root(dataset)
        if dataset.meta('archive_freeze'):raise DatasetBusy('请先完成或取消当前归档计划，再调整数据集')
        # 单图操作已解析到真实所属数据集；其它日期的独立子数据集不应阻挡当前图。
        # 目录编辑和补写仍按原有递归范围检查，不能跨过独立子数据集取得写权限。
        scope=item_path or metadata_scope or dataset.root
        if any(d is not dataset and d.root.is_relative_to(dataset.root) and
               (d.root.is_relative_to(scope) or scope.is_relative_to(d.root)) and
               (d.folder/'dataset.json').is_file() for d in self.datasets.values()):
            raise ValueError('此目录包含独立子数据集，请进入具体拍摄目录操作')
        if metadata_scope is None and len({(r.get('customer',''),r.get('brand','')) for r in dataset.records_snapshot() if r.get('customer') or r.get('brand')})>1:
            raise ValueError('此目录包含多个客户／品牌，请按独立拍摄子目录编辑')
        manual_only=dataset.meta('manual_metadata_only')=='1'
        if not dataset.editing:
            dataset.acquire()
            if metadata_scope is not None:
                # 补写只提交所选范围，不能因取得上级数据集的锁而恢复其它目录/OCR任务。
                with dataset.connect() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('manual_metadata_only','1')")
                return
        elif not manual_only or metadata_scope is not None:
            dataset.ensure_ownership()
            return
        if metadata_scope is None:
            with dataset.connect() as c:c.execute("DELETE FROM meta WHERE key='manual_metadata_only'")
            if dataset.groups():dataset.change(groups=dataset.groups(),kind='recover_intervals')
            self._recover_tasks(dataset)
            self.schedule(dataset,force=True)

    def _capture_mode(self,dataset,enabled):
        value='1'  # 二星统一是款末标记；保留旧接口参数只为兼容已安装客户端。
        if dataset.meta('legacy_capture_mode','0')!=value:
            with dataset.connect() as c:c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('legacy_capture_mode',value))
            self.schedule(dataset,force=True)

    def _cancel_legacy(self,dataset,records,reason):
        with self.archive.connect() as c:
            for record in records:
                c.execute("UPDATE metadata_jobs SET status='cancelled',reason=? WHERE path=? AND status IN ('queued','running')",(reason,str(resolve_under(dataset.root,record['relative_path']))))

    def require_live(self,sid):
        session=self.store.session(sid)
        if session:
            dataset=self.dataset_for_path(session['root'])
            if dataset is None:raise DatasetBusy('拍摄目录尚未取得编辑权限，请重新开始现场监控或进入编辑')
            self.guard([session['root']])

    @contextmanager
    def _archive_io(self):
        # 归档需要跨队列独占；不能拿着业务锁等待正在持有文件锁的写入者。
        with self.metadata_work_lock:
            self._reap_metadata()
            if self.metadata_inflight:raise DatasetBusy('元数据仍在写入，请完成后再归档')
            with self.io_lock,self.archive.metadata_io_lock:yield

    @contextmanager
    def freeze_archive(self,roots,job_id):
        """准备后保持源字节版本稳定；不取消任何兼容写入任务。"""
        with self._archive_io():
            with (self.sku.lock if self.sku else nullcontext()),self.archive.lock:
                self._mirror_live()
                selected=[]
                for dataset in list(self.datasets.values()):
                    if self.dataset_for_path(dataset.root) is not dataset:continue
                    if any(dataset.root.is_relative_to(Path(root).resolve()) or Path(root).resolve().is_relative_to(dataset.root) for root in roots):
                        dataset.ownership.check()
                        prior=dataset.meta('archive_freeze')
                        if prior and prior!=job_id:raise ValueError('来源已有其它待执行归档计划，请先处理')
                        selected.append(dataset)
                for dataset in selected:
                    with dataset.connect() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('archive_freeze',?)",(job_id,))
        yield

    def thaw_archive(self,job_id):
        for dataset in list(self.datasets.values()):
            with dataset.connect() as c:c.execute("DELETE FROM meta WHERE key='archive_freeze' AND value=?",(job_id,))
        self.wake.set()

    def prepare_live(self, sid):
        # 现场改星与目录入库按同一顺序提交，防止旧扫描结果覆盖新评级。
        with self.io_lock:return self._prepare_live(sid)

    def _prepare_live(self, sid):
        session = self.store.session(sid)
        if not session:
            return
        with self.store.connect() as c:
            if c.execute('SELECT 1 FROM settings WHERE key=?', ('archive_session:' + sid,)).fetchone():
                return
        root = Path(session['root']).resolve(strict=True)
        dataset = self._dataset(root)
        self._check_new_root(dataset)
        if dataset.meta('archive_freeze'):raise DatasetBusy('此拍摄目录有待执行归档计划，请先完成或取消归档')
        if not dataset.editing:
            dataset.acquire()
        with dataset.connect() as c:c.execute("DELETE FROM meta WHERE key='manual_metadata_only'")
        self.live_sessions[str(root)] = sid
        with self.catalog.connect() as c:
            exists = c.execute('SELECT 1 FROM roots WHERE path=? AND removed=0', (str(root),)).fetchone()
        if not exists:
            self.catalog.add_root(str(root))
        overrides = session['state'].get('overrides', {})
        by_path = {}
        for capture in self.store.records(sid):
            if capture['id'] in overrides:
                for kind in ('jpg', 'raw'):
                    if capture.get(kind):
                        by_path[capture[kind].casefold()] = overrides[capture['id']]
        updates, before = [], []
        for record in dataset.records():
            value = by_path.get(record['relative_path'].casefold())
            if value is None or value == record.get('rating'):
                continue
            token = uuid.uuid4().hex
            before.append(record)
            updates.append({**record, 'rating': value, 'decision_id': token, 'parents':decision_parents(record),
                            'boundary': value == 2,'assignment_state':'unassigned' if value==2 else record.get('assignment_state','unassigned'),
                            'role': 'sku_label' if value == 2 else 'product', 'write_state': 'pending','write_policy':'full', 'updated': time.time()})
        if updates:
            dataset.change(updates, kind='live_rating', before=before,
                           tasks=[{'kind': 'metadata', 'record': r['id'], 'revision': r['decision_id'],'data':{'_manual_backfill':-1}} for r in updates])
            for record in updates:
                self._project(dataset, record)
        self.schedule(dataset, force=True)

    def _live_records(self,dataset,session,targets,assignment):
        marks=','.join('?' for _ in targets)
        with self.archive.connect() as c:
            files=[dict(r) for r in c.execute('SELECT f.id,f.asset,f.component,f.sha,l.path,a.role,a.captured,a.camera FROM files f JOIN locations l ON l.file=f.id JOIN assets a ON a.id=f.asset WHERE f.asset IN ('+marks+')',list(targets))]
            links={r['asset']:r['capture'] for r in c.execute('SELECT asset,capture FROM catalog_links WHERE session=? AND asset IN ('+marks+')',[session['id'],*targets])}
        captures={r['id']:r for r in self.store.records(session['id'],ids=list(links.values()))}
        prior={r['id']:r.get('before',{}) for r in assignment.get('targets',[])}
        result=[]
        for file in files:
            path=Path(file['path']).absolute()
            if not path.is_relative_to(dataset.root):continue
            relative=path.relative_to(dataset.root).as_posix()
            old=dataset.record(path=relative)
            location_id=old['id'] if old else uuid.uuid5(uuid.UUID(file['id']),relative).hex
            capture=captures.get(links.get(file['asset']),{})
            identity=capture.get('captureIdentity',{})
            camera={'model':identity.get('Model',capture.get('camera','')),'serial':identity.get('SerialNumber',identity.get('BodySerialNumber','')),
                    'time':identity.get('DateTimeOriginal',''),'subsecond':identity.get('SubSecTimeOriginal',''),
                    'combined':identity.get('SubSecDateTimeOriginal',''),'number':identity.get('FileNumber',identity.get('ImageNumber',''))}
            record={**(old or {}),'id':location_id,'file_id':file['id'],'location_id':location_id,'asset_id':file['asset'],'relative_path':relative,'signature':file_signature(path),
                    'media_type':'image','camera':camera,'capture_metadata':identity,'capture_identity':(old or {}).get('capture_identity',''),
                    'time':capture.get('time') or file['captured'] or '', 'sequence':(old or {}).get('sequence',len(result)),
                    'sku':(old or {}).get('sku',prior.get(file['asset'],{}).get('sku','')),'role':file['role'],
                    'rating':session['state'].get('overrides',{}).get(capture.get('id'),capture.get('existingRating',0)),
                    'shoot_date':str(capture.get('time') or file['captured'] or '')[:10],
                    'customer':dataset.meta('customer'),'brand':dataset.meta('brand'),
                    'catalog_scope':uuid.uuid5(uuid.NAMESPACE_URL,json.dumps([dataset.meta('customer'),dataset.meta('brand')],ensure_ascii=False)).hex,
                    'file_sha256':file['sha']}
            with self.archive.connect() as c:
                c.execute('INSERT OR REPLACE INTO portable_source_links VALUES(?,?,?,?,?,?)',(location_id,str(dataset.root),session['id'],capture.get('id',''),assignment.get('project',''),assignment.get('scope','')))
            result.append(record)
        return result

    def _mirror_live(self):
        with self.archive.connect() as c:
            events = [dict(r) for r in c.execute('SELECT rowid AS position,* FROM portable_capture_outbox WHERE delivered=0 AND rowid>? ORDER BY rowid LIMIT 10',(self.outbox_after,))]
        self.outbox_after=events[-1]['position'] if len(events)==10 else 0
        for event in events:
            payload = json.loads(event['data'])
            assignment = payload['assignment']
            segment = assignment.get('segment')
            with self.archive.connect() as c:
                context = c.execute('SELECT t.session,s.label FROM segments s JOIN streams t ON t.id=s.stream WHERE s.id=?', (segment,)).fetchone() if segment else None
            if not context:
                root=assignment.get('source_root')
                source_session=next((s for s in self.store.sessions() if root and Path(s['root']).absolute()==Path(root).absolute()),None)
                if source_session:
                    context={'session':source_session['id'],'label':''}
                    segment=segment or uuid.uuid5(uuid.UUID(event['id']),'manual-group').hex
                else:
                    with self.archive.connect() as c:
                        c.execute('UPDATE portable_capture_outbox SET delivered=1 WHERE id=?', (event['id'],))
                    continue
            session = self.store.session(context['session'])
            if not session:
                continue
            dataset = self._dataset(session['root'])
            if not dataset.editing:
                # 已交接时不重新抢占；新的持有端可根据源快照接续。
                continue
            if dataset.meta('archive_freeze'):continue
            with self.io_lock:
                targets = set(payload['targets'])
                originals = self._live_records(dataset,session,targets,assignment)
                if targets-{r['asset_id'] for r in originals}:
                    continue
                with self.archive.connect() as c:
                    current = {r['id']: dict(r) for r in c.execute('SELECT * FROM assets WHERE id IN (' + ','.join('?' for _ in targets) + ')', list(targets))}
                if any(current.get(r['asset_id'], {}).get('sku') != assignment['sku'] for r in originals):
                    with self.archive.connect() as c:
                        c.execute('UPDATE portable_capture_outbox SET delivered=1 WHERE id=?', (event['id'],))
                    continue
                if all(r.get('sku')==assignment['sku'] and r.get('assignment_state')=='confirmed' and r.get('decision_id') for r in originals):
                    with self.archive.connect() as c:c.execute('UPDATE portable_capture_outbox SET delivered=1 WHERE id=?',(event['id'],))
                    continue
                gid = uuid_value(segment)
                variant = None
                if self.sku:
                    with self.sku.connect() as c:
                        if c.execute("SELECT 1 FROM sqlite_master WHERE name='sku_shoot_versions'").fetchone():
                            variant = c.execute('SELECT base_sku,version FROM sku_shoot_versions WHERE asset=? AND sku=? ORDER BY created DESC LIMIT 1', (context['label'], assignment['sku'])).fetchone()
                base_sku = variant['base_sku'] if variant else assignment['sku']
                shoot_version = string.ascii_uppercase[variant['version']-1] if variant and variant['version'] else ''
                products = len({r['asset_id'] for r in originals if r['asset_id'] != context['label']})
                labels = int(any(r['asset_id'] == context['label'] for r in originals))
                token = event['id']
                members_hash = digest(encoded(sorted(targets)))
                originals=[{**r,'group_id':r.get('group_id') or gid,'role':'sku_label' if r['asset_id']==context['label'] else 'product',
                            'boundary':r['asset_id']==context['label'],'expected_products':r.get('expected_products',products),
                            'expected_labels':r.get('expected_labels',labels),'members_digest':r.get('members_digest') or members_hash} for r in originals]
                updates = [{**r, 'sku': assignment['sku'], 'base_sku': base_sku, 'shoot_version': shoot_version, 'group_id': gid,
                            'parent_sku':r.get('parent_sku') or base_sku[:4],
                            'role': 'sku_label' if r['asset_id'] == context['label'] else 'product',
                            'boundary': r['asset_id'] == context['label'], 'decision_id': token,'parents':list(dict.fromkeys(([r['decision_id']] if r.get('decision_id') else [])+r.get('parents',[]))), 'group_revision': token,
                            'expected_products': products, 'expected_labels': labels,'expected_files':len(originals),'files_digest':digest(encoded(sorted(r.get('file_id') or r['id'] for r in originals))), 'members_digest': members_hash,
                            'assignment_source': 'live', 'assignment_state': 'confirmed', 'shoot_type': assignment['scope'],
                            'write_state': 'pending', 'updated': time.time()} for r in originals]
                group = {'id': gid, 'label': context['label'], 'members': sorted(targets), 'sku': assignment['sku'], 'base_sku': base_sku, 'shoot_version': shoot_version,
                         'status': 'confirmed', 'decision_id': token, 'expected_products': products, 'expected_labels': labels,
                         'members_digest': members_hash,'expected_files':len(originals),'files_digest':digest(encoded(sorted(r.get('file_id') or r['id'] for r in originals))), 'created': time.time()}
                dataset.change(updates, [group], kind='live_confirm', before=originals,
                               tasks=[{'kind': 'metadata', 'record': r['id'], 'revision': token} for r in updates])
                self._merge_native_jobs(dataset,updates,token)
                for r in updates:
                    self._project(dataset, r)
                with self.archive.connect() as c:
                    c.execute('UPDATE portable_capture_outbox SET delivered=1 WHERE id=?', (event['id'],))

    def _merge_native_jobs(self,dataset,records,token):
        """补充尚未领取的现场作业，不改其真实状态；新队列等真实写后验证。"""
        waiting=[]
        with self.archive.connect() as c:
            for record in records:
                path=str(resolve_under(dataset.root,record['relative_path']))
                job=c.execute("SELECT * FROM metadata_jobs WHERE path=? AND operation=? AND status IN ('queued','running') ORDER BY created DESC LIMIT 1",(path,token)).fetchone()
                if not job:continue
                if job['status']=='queued':
                    fields=json.loads(job['fields']);revision=fields.get('MetadataRevision')
                    fields.update(self._fields(dataset,record))
                    if revision is not None:fields['MetadataRevision']=revision
                    c.execute("UPDATE metadata_jobs SET fields=? WHERE id=? AND status='queued'",(json.dumps(fields,ensure_ascii=False),job['id']))
                waiting.append((json.dumps({'native_job':job['id']}),record['id'],token))
        with dataset.connect() as c:
            for data,record,revision in waiting:
                c.execute("UPDATE tasks SET status='waiting_native',data=? WHERE kind='metadata' AND record=? AND revision=? AND status='queued'",(data,record,revision))

    def _wake_native_jobs(self,dataset):
        with dataset.connect() as c:
            waiting=[dict(r) for r in c.execute("SELECT id,record,revision,data FROM tasks WHERE status='waiting_native'")]
        if not waiting:return
        ready=[]
        with self.archive.connect() as c:
            for task in waiting:
                job=c.execute('SELECT status FROM metadata_jobs WHERE id=?',(json.loads(task['data']).get('native_job'),)).fetchone()
                if not job or job['status'] not in ('queued','running'):ready.append(task['id'])
        with dataset.connect() as c:
            c.executemany("UPDATE tasks SET status='queued' WHERE id=?",[(value,) for value in ready])

    def _publish_live_boundaries(self):
        if not self.sku:return
        with self.sku.connect() as c:
            jobs=[dict(r) for r in c.execute("SELECT * FROM jobs WHERE status IN ('queued','running','waiting_model') AND trigger!='full_auto' ORDER BY created DESC LIMIT 64")]
        for job in jobs:
            session=self.store.session(job['session_id'])
            dataset=self.dataset_for_path(session['root']) if session else None
            if not dataset or not dataset.editing or dataset.meta('archive_freeze') or dataset.meta('boundary:'+job['id']):continue
            with self.archive.connect() as c:
                segment=c.execute("SELECT * FROM segments WHERE job=? AND status='waiting_sku'",(job['id'],)).fetchone()
            if not segment:continue
            members=set(json.loads(segment['members']))
            if not members:continue
            with self.io_lock:
                records=self._live_records(dataset,session,members,{'targets':[],'project':job['project'],'scope':job['scope']})
                if members-{r['asset_id'] for r in records}:continue
                products=len(members-{segment['label']});files_hash=digest(encoded(sorted(r['file_id'] for r in records)))
                updates=[]
                for r in records:
                    label=r['asset_id']==segment['label']
                    updated={**r,'boundary':label,'boundary_token':job['id'] if label else r.get('boundary_token',''),
                             'rating':2 if label else r.get('rating',0),'role':'sku_label' if label else r.get('role','product')}
                    if not r.get('sku'):
                        updated.update(group_id=segment['id'],decision_id=job['id'],assignment_state='unassigned',
                                       expected_products=products,expected_labels=1,expected_files=len(records),
                                       members_digest=digest(encoded(sorted(members))),files_digest=files_hash,
                                       shoot_type=job['scope'],write_state='pending')
                    updates.append(updated)
                group={'id':segment['id'],'label':segment['label'],'members':sorted(members),'sku':'','status':'pending',
                       'expected_products':products,'expected_labels':1,'expected_files':len(records),
                       'members_digest':digest(encoded(sorted(members))),'files_digest':files_hash,'created':job['created']}
                dataset.change(updates,[group],kind='live_boundary')
                with dataset.connect() as c:c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('boundary:'+job['id'],'1'))

    def observe_metadata(self, row, payload):
        """资料队列已完成读取时只登记恢复通知，不在索引事务里跑文件 I/O。"""
        path = Path(row['path'])
        dataset = self.dataset_for_path(path)
        if dataset:
            self.schedule(dataset)
            return
        tags = (payload or {}).get('tags', {})
        with self.catalog.connect() as c:
            root = c.execute('SELECT path FROM roots WHERE id=? AND removed=0', (row['root'],)).fetchone()
        if not root:
            return
        registered = Path(root[0]).resolve()
        for parent in (path.parent, *path.parent.parents):
            if not parent.is_relative_to(registered):
                break
            if (parent / '.shiying/dataset.json').is_file():
                self.schedule(self._dataset(parent))
                return
        if tags.get('XMP-fk:SKU') or any(str(v).startswith('SKU:') for v in tags.get('XMP-dc:Subject', []) if isinstance(v, str)):
            self.schedule(self._dataset(path.parent))

    def scan_backup_metadata(self,dataset):
        """显式立即同步允许读取拍摄根目录，不切换用户的归档访问范围。"""
        with self.io_lock:
            self.backup_scan_scope.full_root=True
            try:
                deadline=time.monotonic()+.8
                while True:
                    self._scan(dataset)
                    if not self.scan_cursor.get(str(dataset.root)) and not self.loading.get(str(dataset.root)):
                        return True
                    if self.closed.is_set() or time.monotonic()>=deadline:return False
            finally:self.backup_scan_scope.full_root=False

    def _rows(self, dataset, *, after='', focus=None, limit=16):
        prefix = str(dataset.root).rstrip('\\/') + '\\'
        with self.catalog.connect() as c:
            if focus is not None:
                rows=[dict(r) for r in c.execute('SELECT rowid AS capture_order,* FROM items WHERE path=? AND online=1',(str(focus),))]
            elif getattr(self.catalog,'visits',None) and not getattr(self.backup_scan_scope,'full_root',False):
                visits=self.catalog.visits
                with visits.lock:key=visits.key;path=visits.path
                if not key or path is None or not visits.allows(path/'_scope'):return []
                rows=[dict(r) for r in c.execute('SELECT rowid AS capture_order,* FROM items WHERE root=? AND folder=? AND path>? AND online=1 ORDER BY path LIMIT ?', (key[0],key[1] or '/',after or '',limit+1))]
                more=len(rows)>limit;rows=rows[:limit]
                self.scan_page[str(dataset.root)]=(rows[-1]['path'] if rows else after,more)
            else:
                upper=prefix[:-1]+chr(ord(prefix[-1])+1)
                rows=[dict(r) for r in c.execute('SELECT rowid AS capture_order,* FROM items INDEXED BY catalog_capture_paths WHERE path>=? AND path<? AND path>? AND online=1 ORDER BY path LIMIT ?', (prefix,upper,after or prefix,limit+1))]
                more=len(rows)>limit;rows=rows[:limit]
                self.scan_page[str(dataset.root)]=(rows[-1]['path'] if rows else after,more)
        # 同一实际路径可能由重叠的归档根索引；只保留一个位置。
        children = [d.root for d in self.datasets.values() if d.root != dataset.root and d.root.is_relative_to(dataset.root) and (d.folder / 'dataset.json').is_file()]
        checked=set()
        for row in rows:
            for parent in (Path(row['path']).parent,*Path(row['path']).parent.parents):
                if parent==dataset.root or not parent.is_relative_to(dataset.root):break
                if parent in checked:continue
                checked.add(parent)
                if (parent/'.shiying/dataset.json').is_file():
                    children.append(self._dataset(parent).root);break
        rows = [r for r in rows if not any(Path(r['path']).is_relative_to(root) for root in children)]
        return sorted({r['path'].casefold(): r for r in rows}.values(), key=lambda r: (Path(r['path']).stem.casefold(), Path(r['path']).suffix.lower() not in ('.jpg', '.jpeg')))

    def _pass_signature(self,dataset,can_edit):
        """整轮结束重算的全部输入：记录/分组版本、业务元信息与未完成任务集合。"""
        with dataset.connect() as c:
            version=c.execute('SELECT value FROM records_version WHERE id=1').fetchone()[0]
            meta=tuple(c.execute("SELECT key,value FROM meta WHERE key IN ('revision','metadata_view_revision','archive_partition_pending','customer','brand') ORDER BY key").fetchall())
            tasks=tuple(c.execute("SELECT count(*),coalesce(max(rowid),0) FROM tasks WHERE status IN ('queued','running','waiting_native','failed')").fetchone())
        return (version,meta,tasks,bool(can_edit),str(dataset.root) in self.live_sessions)

    def _scan_total(self,dataset):
        """当前一轮读取的素材总数，仅用于进度显示。"""
        try:
            with self.catalog.connect() as c:
                visits=getattr(self.catalog,'visits',None)
                if visits:
                    with visits.lock:key=visits.key
                    if not key:return 0
                    # 大库可能误选 online 开头的覆盖索引，遍历全部在线素材；明确使用目录索引。
                    return c.execute("SELECT count(*) FROM items INDEXED BY catalog_items_root_folder WHERE root=? AND folder=? AND online=1 AND media_type IN ('image','video')",(key[0],key[1] or '/')).fetchone()[0]
                prefix=str(dataset.root).rstrip('\\/')+'\\';upper=prefix[:-1]+chr(ord(prefix[-1])+1)
                return c.execute("SELECT count(*) FROM items INDEXED BY catalog_capture_paths WHERE path>=? AND path<? AND online=1 AND media_type IN ('image','video')",(prefix,upper)).fetchone()[0]
        except sqlite3.Error:return 0

    def schedule(self, dataset, *, force=False):
        key = str(dataset.root)
        if force or time.monotonic() - self.last_scan.get(key, 0) > self.scan_interval.get(key, 4):
            with self.lock:
                self.pending[key] = True
            self.wake.set()

    def _scope_records(self, dataset, scope):
        datasets=list(self.datasets.values())
        scope_path=Path(scope)
        # 与当前范围既不包含也不被包含的子数据集不可能贡献记录，不解码其全部资料。
        containers = [dataset] + [d for d in datasets if d is not dataset and d.root.is_relative_to(dataset.root) and (d.root.is_relative_to(scope_path) or scope_path.is_relative_to(d.root)) and self._authoritative(d)]
        records = []
        groups = []
        authorities=sorted([d for d in datasets if (d.root.is_relative_to(scope_path) or scope_path.is_relative_to(d.root)) and self._authoritative(d)],key=lambda d:len(d.root.parts),reverse=True)
        authority_prefixes=[(d,str(d.root).rstrip('\\/').casefold()+'\\') for d in authorities]
        scope_prefix=str(scope).rstrip('\\/').casefold()+'\\'
        prefix=str(dataset.root).rstrip('\\/')+'\\';upper=prefix[:-1]+chr(ord(prefix[-1])+1)
        with self.catalog.connect() as c:
            available={r[0].casefold() for r in c.execute('SELECT path FROM items INDEXED BY catalog_capture_paths WHERE online=1 AND path>=? AND path<?',(prefix,upper))}
        for current in containers:
            identity = current.dataset_id or digest(str(current.root).encode())[:24]
            current_prefix=str(current.root).rstrip('\\/')+'\\'
            for record in current.records_snapshot():
                # 相对位置已在入库时校验；展示阶段只拼接字符串，不逐项
                # 重建 Path／重复校验或探测NAS。真实读写仍经 resolve_under。
                path=current_prefix+record['relative_path'].replace('/','\\');path_key=path.casefold()
                authority=next((d for d,p in authority_prefixes if path_key.startswith(p)),None)
                if not path_key.startswith(scope_prefix) or (authority is not current and authority is not None):
                    continue
                gid=record.get('inherited_group_id') or record.get('group_id','')
                records.append({**record,'sku':record.get('sku') or record.get('inherited_sku',''),'group_id':gid, 'relative_path': path[len(prefix):].replace('\\','/'), 'origin_dataset': identity,
                                'group_view_id': identity + ':' + gid,'available':path_key in available})
            groups.extend({**g, 'origin_dataset': identity} for g in current.groups())
        return records, groups

    def state(self, root_id, folder='', *, capture_mode=None):
        dataset, scope = self.scope(root_id, folder)
        if capture_mode is not None:self._capture_mode(dataset,capture_mode)
        self.schedule(dataset)
        records, all_groups = self._scope_records(dataset, scope)
        groups = []
        visible_assets = {r['asset_id'] for r in records}
        grouped_records=defaultdict(list)
        for r in records:grouped_records[(r['origin_dataset'],r.get('group_id',''))].append(r)
        for group in all_groups:
            if visible_assets.intersection(group.get('members', [])) and group.get('status') != 'cancelled':
                members = grouped_records[(group['origin_dataset'],group['id'])]
                if members and not group.get('label') and all(r.get('media_type')=='image' for r in members):continue
                found = len({r['asset_id'] for r in members if r.get('role') != 'sku_label' and r.get('available')})
                labels = len({r['asset_id'] for r in members if r.get('role') == 'sku_label' and r.get('available')})
                actual_digest = digest(encoded(sorted({r['asset_id'] for r in members})))
                revisions = {r.get('group_revision') for r in members if r.get('group_revision')}
                known = group.get('expected_products') is not None and group.get('expected_labels') is not None and bool(group.get('members_digest')) and group.get('expected_files') is not None and bool(group.get('files_digest'))
                actual_files=digest(encoded(sorted(r.get('file_id') or r['id'] for r in members)))
                if not members:continue
                complete = found == group.get('expected_products') and labels == group.get('expected_labels') and actual_digest == group.get('members_digest') and len(revisions) <= 1 and all(r.get('available') for r in members) and len(members)==group.get('expected_files') and actual_files==group.get('files_digest') and not any(r.get('identity_conflict') or r.get('metadata_conflict') for r in members) if known else None
                groups.append({**group, 'found_products': found, 'found_labels': labels,'found_files':sum(bool(r.get('available')) for r in members), 'complete': complete})
        with dataset.connect() as c:
            # 失败日志保留，但旧确认版本的写入失败不应冒充当前整款的失败。
            tasks = [dict(r) for r in c.execute("""SELECT t.id,t.kind,t.record,t.status,t.reason FROM tasks t
                LEFT JOIN records r ON r.id=t.record
                WHERE t.status NOT IN ('complete','cancelled') AND
                (t.kind NOT IN ('metadata','metadata_fill','metadata_sidecar','source_sync') OR
                 t.revision=json_extract(r.data,'$.decision_id')) ORDER BY t.rowid LIMIT 100""")]
            history = [dict(r) for r in c.execute('SELECT id,kind,created FROM history ORDER BY revision DESC,created DESC LIMIT 20')]
            unpublished = c.execute("SELECT count(*) FROM events WHERE status='pending'").fetchone()[0]
            record_versions={r['id']:r.get('decision_id') for r in records}
            latest_metadata={r['record']:dict(r) for r in c.execute("SELECT record,revision,status,reason FROM tasks WHERE kind IN ('metadata','metadata_fill','metadata_sidecar') AND status!='cancelled' ORDER BY rowid") if record_versions.get(r['record'])==r['revision']}
            metadata_tasks=[t for t in latest_metadata.values() if t['status']!='complete']
            metadata_counts={status:sum(t['status']==status for t in metadata_tasks) for status in ('queued','running','waiting_native','failed')}
        return {'dataset_id': dataset.dataset_id, 'root': str(dataset.root), 'scope': str(scope), 'editing': dataset.editing,
                'readonly_reason':'正在单向接收，请断开同步后标星或修改款号' if self.readonly() else '',
                'revision': int(dataset.meta('revision')),'view_revision':int(dataset.meta('metadata_view_revision','0')), 'customer': dataset.meta('customer'), 'brand': dataset.meta('brand'),
                'groups': sorted(groups, key=lambda g: g.get('created', 0)), 'records': records[:240], 'total': len(records),
                'metadata_verified': sum(r.get('write_state') == 'verified' for r in records),
                'sku_file_verified':sum(bool(r.get('sku')) and r.get('file_sku')==r['sku'] for r in records),
                'sku_fill_needed':sum(bool(r.get('sku')) and not r.get('file_sku') for r in records),
                'metadata_required': sum(bool(r.get('decision_id')) for r in records),
                'unclosed': len({r['asset_id'] for r in records if r.get('media_type')=='image'}-{aid for g in groups for aid in g.get('members',[])}),
                'tasks': tasks, 'history': history, 'unpublished': unpublished,
                'metadata_tasks':metadata_counts,
                'metadata_concurrency':self.metadata_workers,
                'metadata_wait_reason':next((t['reason'] for t in metadata_tasks if t['status']=='queued' and t['reason']),''),
                'freeze_job':dataset.meta('archive_freeze'),
                'sku_projection_pending':sum(d is dataset for d in list(self.projection_pending.values())),
                'scan_progress':dict(self.scan_progress.get(str(dataset.root)) or {}),
                'loading': self.loading.get(str(dataset.root), False), 'error': self.errors.get(str(dataset.root), '') or dataset.last_error}

    def edit(self, root_id, folder='', *, enabled=True, customer=None, brand=None):
        dataset, _ = self.scope(root_id, folder)
        with self.io_lock:
            if enabled:
                self._acquire_editor(dataset)
                if customer is not None or brand is not None:
                    customer, brand = str(customer or '').strip(), str(brand or '').strip()
                    if max(len(customer), len(brand)) > 100 or any(ord(ch) < 32 for ch in customer + brand):
                        raise ValueError('客户和品牌名称无效')
                    if dataset.meta('customer') != customer or dataset.meta('brand') != brand:
                        existing=dataset.records()
                        if any(r.get('sku') and (r.get('customer') or r.get('brand')) and (r.get('customer',''),r.get('brand',''))!=(customer,brand) for r in existing):
                            raise ValueError('已有确认款号，不能直接更换整个数据集的客户／品牌')
                        token=uuid.uuid4().hex;scope_id=uuid.uuid5(uuid.NAMESPACE_URL,json.dumps([customer,brand],ensure_ascii=False)).hex
                        updates=[{**r,'customer':customer,'brand':brand,'catalog_scope':scope_id,
                                  **({'decision_id':token,'parents':decision_parents(r),'write_state':'pending'} if r.get('sku') else {})} for r in existing]
                        dataset.change(updates,kind='scope',before=existing,tasks=[{'kind':'metadata','record':r['id'],'revision':token} for r in updates if r.get('sku')],context={'customer':customer,'brand':brand})
                        self._cancel_legacy(dataset,updates,'数据集作用域已明确补充，旧字段作业已取消')
                        for r in updates:self._project(dataset,r)
                self._scan(dataset)
                self._recover_tasks(dataset)
            else:
                with self.metadata_work_lock:
                    if any(d is dataset and not future.done() for future,(d,_,_) in self.metadata_inflight.items()):
                        raise DatasetBusy('当前目录仍有元数据正在写入，请完成后再交接编辑权限')
                if self.watch:
                    watched = self.watch.state()
                    folder = watched.get('folder') or watched.get('root')
                    if watched.get('enabled') and folder:
                        path = Path(folder).resolve()
                        if path.is_relative_to(dataset.root) or dataset.root.is_relative_to(path):
                            raise ValueError('此目录仍在现场拍摄监控，请先停止监控再交接')
                if dataset.meta('archive_freeze'):raise ValueError('请先完成或取消当前归档计划，再交接编辑权限')
                with self.archive.metadata_io_lock:dataset.release()
        return self.state(root_id, folder)

    def _recover_tasks(self, dataset, *, resume=True):
        with dataset.connect() as c:
            if resume:
                c.execute("""UPDATE tasks SET status='queued',data=json_remove(data,'$._retry_at')
                    WHERE status='failed' AND kind IN ('metadata','metadata_fill','metadata_sidecar')
                    AND (reason LIKE '%[WinError 32]%' OR reason LIKE '%[WinError 33]%')""")
            if dataset.meta('manual_metadata_only')=='1':
                if resume:c.execute("UPDATE tasks SET status='queued' WHERE status='running' AND kind IN ('metadata','metadata_fill','metadata_sidecar')")
                return
            if resume:
                c.execute("UPDATE tasks SET status='queued' WHERE status='running'")
                # 接续编辑复用失败任务，不能每次生成新任务并遗留旧失败计数。
                c.execute("UPDATE tasks SET status='queued',reason='',data=json_remove(data,'$._retry_at','$._retry_count') WHERE status='failed' AND kind IN ('metadata','metadata_fill','metadata_sidecar') AND revision=(SELECT json_extract(data,'$.decision_id') FROM records WHERE records.id=tasks.record)")
            statuses=('queued','running','waiting_native') if resume else ('queued','running','waiting_native','failed')
            pending = {(r['record'], r['revision']) for r in c.execute("SELECT record,revision FROM tasks WHERE kind IN ('metadata','metadata_fill','metadata_sidecar') AND status IN ("+','.join('?' for _ in statuses)+')',statuses)}
            dataset_id=dataset.dataset_id
            for record in dataset.records():
                if record.get('metadata_conflict') or record.get('identity_conflict'):continue
                repair_sidecar=(record.get('write_state')=='verified' and record.get('decision_source_id')==dataset_id
                    and Path(record.get('relative_path','')).suffix.lower() in ('.jpg','.jpeg')
                    and record.get('sidecar_signature') in (None,digest(encoded([]))))
                if record.get('decision_id') and (record.get('write_state') not in ('verified','sku_verified') or repair_sidecar) and (record['id'], record['decision_id']) not in pending:
                    kind='metadata_sidecar' if repair_sidecar else 'metadata_fill' if record.get('write_policy')=='fill_missing_sku' else 'metadata'
                    c.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, kind, record['id'], record['decision_id'], 'queued', '', '{"_manual_backfill":-1}'))
                if record.get('boundary') and record.get('boundary_token') and not record.get('ocr') and not record.get('sku') and record.get('assignment_state')!='cleared':
                    if not c.execute("SELECT 1 FROM tasks WHERE kind IN ('ocr','ocr_now') AND record=? AND revision=? AND status<>'cancelled'", (record['id'], record['boundary_token'])).fetchone():
                        c.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, 'ocr', record['id'], record['boundary_token'], 'queued', '', '{}'))
        for record in dataset.records():self._queue_ocr_apply(dataset,record)
        self.wake.set()

    def _scan(self, dataset, _locked=False, focus=None, *, limit=16, total=None):
        if not _locked:
            if focus is None and not self.scan_cursor.get(str(dataset.root)) and total is None:
                total=self._scan_total(dataset)
            with self.io_lock:return self._scan(dataset, True,focus,limit=limit,total=total)
        key = str(dataset.root)
        authority = self.dataset_for_path(dataset.root)
        if authority is not None and authority is not dataset and authority.root in dataset.root.parents and (authority.folder / 'dataset.json').is_file():
            return
        self.loading[key] = True
        more=False
        try:
            # 多机拍摄仍可能追加独立来源；编辑期间也定期读取，避免等重启才发现新款。
            shared_refresh=getattr(self,'shared_refresh',None)
            if shared_refresh is None:self.shared_refresh=shared_refresh={}
            if key not in self.refreshed or (focus is None and not self.scan_cursor.get(key) and
                    (not dataset.editing or time.monotonic()-shared_refresh.get(key,0)>=5)):
                dataset.refresh()
                shared_refresh[key]=time.monotonic()
                self.refreshed.add(key)
            self._seed_imported(dataset)
            can_edit=dataset.editing and not self.readonly() and dataset.meta('manual_metadata_only')!='1'
            capture_assets=self.capture_assets[key]
            observed=[];recovered_groups={};recovered_records={};unchanged=[]
            capture_mode=True
            starting=focus is None and not self.scan_cursor.get(key)
            if starting:
                self.scan_progress[key]={'done':0,'total':total if total is not None else 0,'started':time.time(),'running':True}
                self._pass_started=getattr(self,'_pass_started',{});self._pass_started[key]=time.monotonic()
            items=self._rows(dataset,after=self.scan_cursor.get(key,''),focus=focus,limit=limit)
            ready=self.scan_versions_ready if focus is None else None
            # 调度线程已在锁外、预读之后取得同批浅枚举；其余调用仍在此即时枚举。
            versions=(ready if ready is not None else self._scan_versions(items)) if focus is None else {}
            if focus is None:
                progress=self.scan_progress.setdefault(key,{'done':0,'total':0,'started':time.time(),'running':True})
                progress['done']=min(progress.get('total') or 0,progress.get('done',0)+sum(r.get('media_type') in ('image','video') for r in items)) if progress.get('total') else progress.get('done',0)+len(items)
            for item in items:
                if self.closed.is_set():
                    break
                path = Path(item['path'])
                if item.get('media_type') not in ('image', 'video'):
                    continue
                relative = path.relative_to(dataset.root).as_posix()
                version=versions.get(str(path));sidecars=version[2] if version else None
                if version:signature=version[0]
                else:
                    if not path.is_file():continue
                    signature=file_signature(path)
                from rating_xmp import sidecar_metadata
                side_signature=version[1] if version else xmp_signature(path)
                old = dataset.record(path=relative)
                if old is None:self.last_scan.pop(key,None)
                validation_key = (key, relative.casefold())
                if old and old.get('signature') == signature and old.get('sidecar_signature')==side_signature and old.get('metadata_reader')==READER_VERSION and (focus is None or self.validated.get(validation_key)==signature):
                    # 已保存的reader版本、原文件版本及侧车摘要足以复用；重启不再
                    # 为没有XMP的整目录重新提取EXIF。显式focus仍允许强制重读。
                    self.validated[validation_key]=signature
                    if can_edit and (not dataset.is_shared(old['id']) or old.get('metadata_import_pending')):observed.append({**old,'metadata_import_pending':False})
                    unchanged.append((old,item))
                    continue
                cached=self.scan_reads.pop(str(path),None)
                if cached and cached[:2]==(signature,side_signature):
                    full,side_metadata=cached[2:]
                else:
                    full = self.archive.writer.read(path, full=True)
                    if sidecars is None or sidecars:full.update(self.archive.writer.read_asset(path, embedded=full))
                    side_metadata=sidecar_metadata(path,grouped=True) if sidecars is None or sidecars else {}
                if item.get('media_type') == 'video' and Path(str(path) + '.xmp').is_file():
                    full.update(self.archive.writer.read(Path(str(path) + '.xmp')))
                from rating_xmp import sidecar_paths
                has_sidecar=bool(sidecar_paths(path)) if sidecars is None else bool(sidecars)
                if has_sidecar and 'XMP-xmp:Rating' not in side_metadata:full.pop('XMP-xmp:Rating',None)
                with self.archive.connect() as c:
                    native = c.execute('SELECT f.id,f.asset,a.role FROM locations l JOIN files f ON f.id=l.file JOIN assets a ON a.id=f.asset WHERE l.path=?', (str(path),)).fetchone()
                if key in self.live_sessions and native is None and not full.get('XMP-fk:FileId'):
                    continue
                fid = uuid_value(full.get('XMP-fk:FileId'), native['id'] if native else (old.get('file_id') or old['id']) if old else None)
                peers=dataset.file_records(fid) if old is None else []
                missing=[r for r in peers if not resolve_under(dataset.root,r['relative_path']).is_file()]
                previous=old or (missing[0] if len(missing)==1 else None)
                location_id=previous['id'] if previous else uuid.uuid5(uuid.UUID(fid),relative).hex
                if old and full.get('XMP-fk:FileId') and uuid_value(full['XMP-fk:FileId']) != (old.get('file_id') or old['id']):
                    from archive_identity_recovery import recover
                    candidates=[recovered_records.get(r['id'],r) for r in dataset.records()]
                    restored=recover(old,full,candidates,[recovered_groups.get(g['id'],g) for g in dataset.groups()])
                    if restored is None:
                        self._identity_conflict(dataset, old, item, '同名文件身份已改变，请核对替换文件：' + relative)
                        continue
                    old,group=restored;previous=old;recovered_groups[group['id']]=group;recovered_records[old['id']]=old
                camera = camera_metadata(full)
                try:
                    capture = capture_key(camera)
                    proposed_asset = uuid.uuid5(uuid.NAMESPACE_URL, capture).hex
                except ValueError:
                    capture, proposed_asset = '', uuid.uuid4().hex
                if old and old.get('capture_identity') and capture and old['capture_identity'] != capture:
                    self._identity_conflict(dataset, old, item, '相机快门身份发生冲突，未继承旧款号：' + relative)
                    continue
                peers = capture_assets.get(capture, set()) if capture else set()
                fallback_asset = next(iter(peers)) if len(peers) == 1 else proposed_asset
                asset = uuid_value(full.get('XMP-fk:AssetId'), native['asset'] if native else previous.get('asset_id') if previous else fallback_asset)
                raw_time = str(camera.get('combined') or camera.get('time') or full.get('IFD0:DateTimeOriginal') or '')
                if raw_time:
                    raw_time = raw_time[:10].replace(':', '-') + raw_time[10:]
                    raw_time = raw_time.replace(' ', 'T', 1)
                if not raw_time:
                    raw_time = (previous or {}).get('time') or datetime.fromtimestamp(path.stat().st_mtime, timezone(timedelta(hours=8))).isoformat()
                record = dict(previous or {}, id=location_id,file_id=fid,location_id=location_id, asset_id=asset, relative_path=relative, signature=signature,
                              media_type=item['media_type'], camera=camera, capture_identity=capture, time=raw_time,
                              sequence=(previous or {}).get('sequence',item.get('capture_order',int(dataset.meta('record_count','0'))+len(observed))),
                              rating=(previous or {}).get('rating',0) if (previous or {}).get('decision_id') else int(full.get('XMP-xmp:Rating') or 0),
                              sidecar_signature=side_signature,legacy_mode_seen=capture_mode,
                              legacy_xmp_rating=side_metadata.get('XMP-xmp:Rating')==2 and not side_metadata.get('XMP-fk:DecisionId') and not side_metadata.get('XMP-fk:PhotoRole'))
                record.pop('identity_conflict', None)
                record['rating'] = min(5, max(0, record['rating']))
                for field,value in {'sku':'','base_sku':'','group_id':'','decision_id':'','shoot_type':'未分类','shoot_date':raw_time[:10],
                                    'customer':dataset.meta('customer'),'brand':dataset.meta('brand'),'assignment_state':'unassigned'}.items():record.setdefault(field,value)
                record.setdefault('catalog_scope',uuid.uuid5(uuid.NAMESPACE_URL,json.dumps([record['customer'],record['brand']],ensure_ascii=False)).hex)
                metadata=parse_metadata(full)
                record['file_sku']=str(metadata['patch'].get('sku') or '')
                record['file_rating']=metadata['patch'].get('rating')
                if not metadata['patch'].get('sku') and not metadata['patch'].get('assignment_state') and item.get('source') in ('manual','shooting','embedded'):
                    metadata['patch']['sku']=str(item.get('sku') or '')
                # 首次恢复时命名空间由文件自身确定，不能把默认空作用域当作冲突。
                outcome=reconcile(record,metadata,dataset_id=dataset.dataset_id)
                if outcome['relation']=='identity':
                    self._identity_conflict(dataset,record,item,outcome['reason']);continue
                if outcome['relation']=='conflict':
                    record=boundary(record)
                    record.update(metadata_conflict=outcome['reason'],metadata_conflict_data=conflict_data(record,metadata,outcome),write_state='conflict')
                else:
                    record=outcome['record']
                    record.pop('metadata_conflict',None);record.pop('metadata_conflict_data',None)
                    imported=outcome['relation'] in ('bootstrap','file_newer')
                    if imported and record.get('file_sku'):record['assignment_source']='xmp'
                    record['metadata_import_pending']=bool(imported and record.get('decision_id') and not can_edit)
                    record['write_state']='pending' if outcome['relation']=='archive_newer' else 'verified' if metadata['kind']=='complete' else 'sku_verified' if record['file_sku'] and record['file_sku']==record.get('sku') else 'unknown'
                    if record.get('write_policy')=='fill_missing_sku' and record['file_sku'] and record['file_sku']==record.get('sku'):record['write_state']='sku_verified'
                    if outcome['relation']=='same' and self._fields_match(full,self._fields(dataset,record)):record['write_state']='verified'
                record.update(metadata_format=metadata['kind'],metadata_schema=metadata['schema'],metadata_reader=READER_VERSION)
                record['metadata_relation']=outcome['relation']
                if record.get('boundary'):
                    if not record.get('boundary_token') or outcome['relation']=='file_newer':record['boundary_token']=record.get('decision_id') or 'file:'+digest(encoded([fid,side_signature,record['rating']]))
                    if not record.get('decision_id'):record['assignment_source']='legacy_xmp'
                else:record.pop('boundary_token',None)
                if previous and previous['relative_path'] != relative:
                    record['aliases'] = list(dict.fromkeys(previous.get('aliases', []) + [previous['relative_path']]))
                if can_edit:
                    record['metadata_import_pending']=False
                    observed.append(record)
                else:
                    dataset._validate(record)
                    with dataset.connect() as c:
                        c.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,data=excluded.data', (location_id, relative, encoded(record).decode()))
                        if record.get('identity_recovery'):
                            group=recovered_groups.get(record['group_id'])
                            if group:c.execute('INSERT INTO groups_ VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',(group['id'],encoded(group).decode()))
                        if record!=old:c.execute("INSERT INTO meta VALUES('metadata_view_revision','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
                if capture:capture_assets[capture].add(asset)
                self.validated[validation_key] = signature
                self._project(dataset, record, item)
            if unchanged:
                # 稳定记录的投影共享短事务；不能每张照片重复打开并提交业务库。
                with self.catalog.connect() as cc,self.archive.connect() as ac:
                    for record,item in unchanged:self._project(dataset,record,item,catalog_connection=cc,archive_connection=ac)
            if observed:dataset.change(observed,groups=list(recovered_groups.values()),kind='observe')
            if focus is None:
                cursor,more=self.scan_page.get(key,('',False))
                if more:
                    self.scan_cursor[key]=cursor
                    with self.lock:self.pending[key]=True
                else:
                    self.scan_cursor.pop(key,None)
                    # 整轮复核未带来任何资料、分组或任务变化时，分区/区间/补齐结果必然相同；
                    # 跳过重复全量计算以免长时间占住交互锁。最多 60 秒仍完整重算一次兜底。
                    signature=self._pass_signature(dataset,can_edit)
                    previous=self.pass_signature.get(key)
                    dirty=not previous or previous[0]!=signature or time.monotonic()-previous[1]>=60
                    if dirty:
                        self._recover_groups(dataset)
                        if can_edit and (key not in self.live_sessions or dataset.meta('archive_partition_pending')):
                            self._partition(dataset)
                            self._inherit_interval_skus(dataset,can_edit=True)
                            self._apply_known_labels(dataset)
                            self._recover_tasks(dataset,resume=False)
                            with dataset.connect() as c:c.execute("DELETE FROM meta WHERE key='archive_partition_pending'")
                        elif capture_mode and not can_edit:
                            _,recovered=self._partition(dataset,apply=False)
                            existing={g['id']:g for g in dataset.groups()}
                            with dataset.connect() as c:
                                c.executemany('INSERT INTO groups_ VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',[(g['id'],encoded({**g,'recovered_local':True}).decode()) for g in recovered])
                            self._inherit_interval_skus(dataset,can_edit=False)
                        self.pass_signature[key]=(self._pass_signature(dataset,can_edit),time.monotonic())
                    conflicts = [r.get('identity_conflict') or r.get('metadata_conflict') for r in dataset.records_snapshot() if r.get('identity_conflict') or r.get('metadata_conflict')]
                    if conflicts:self.errors[key] = conflicts[0]
                    else:self.errors.pop(key, None)
                    now=time.monotonic()
                    self.last_scan[key] = now;self.scan_complete[key]=now
                    started=getattr(self,'_pass_started',{}).get(key,now)
                    # 大目录例行复核按实际耗时放宽间隔，后台占用不超过约三分之一；有变化时保持4秒。
                    self.scan_interval[key]=4 if dirty else min(10,max(4,2*(now-started)))
                    progress=self.scan_progress.setdefault(key,{})
                    progress.update(done=progress.get('total',0),running=False,finished=time.time())
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.errors[key] = str(exc)
        finally:
            self.loading[key] = ((more or bool(self.scan_cursor.get(key))) and key not in self.last_scan) or bool(dataset.imported_records)
            if dataset.imported_records:
                with self.lock:self.pending[key]=True

    def _identity_conflict(self, dataset, record, item, reason):
        with dataset.connect() as c:
            c.execute('UPDATE records SET data=? WHERE id=?', (encoded({**record, 'identity_conflict': reason}).decode(), record['id']))
            c.execute("INSERT INTO meta VALUES('metadata_view_revision','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
        with self.catalog.connect() as c:
            c.execute("UPDATE items SET sku='',normalized='',source='review',confidence=NULL WHERE id=?", (item['id'],))
        with self.archive.connect() as c:
            c.execute('DELETE FROM portable_sku_evidence WHERE path=?', (item['path'],))
            c.execute('DELETE FROM portable_ratings WHERE path=?', (item['path'],))
        self.errors[str(dataset.root)] = reason

    @staticmethod
    def _number(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _recover_groups(self, dataset):
        all_records=dataset.records()
        pairs={(r.get('customer',''),r.get('brand','')) for r in all_records if r.get('sku') or r.get('customer') or r.get('brand')}
        if len(pairs)==1 and not dataset.meta('customer') and not dataset.meta('brand'):
            customer,brand=next(iter(pairs))
            with dataset.connect() as c:c.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',[('customer',customer),('brand',brand)])
        groups = {g['id']: g for g in dataset.groups()}
        found = defaultdict(list)
        for record in all_records:
            if record.get('group_id'):
                found[record['group_id']].append(record)
        for gid, rows in found.items():
            old=groups.get(gid)
            try:
                gid = uuid.UUID(gid).hex
            except ValueError:
                continue
            example = next((r for r in rows if r.get('metadata_format')=='complete'),rows[0])
            versions = {r.get('group_revision') for r in rows if r.get('group_revision')}
            codes = {r.get('sku') for r in rows if r.get('sku')}
            group = {'id': gid, 'members': list(dict.fromkeys(r['asset_id'] for r in rows)),
                     'label': next((r['asset_id'] for r in rows if r.get('role') == 'sku_label'), ''),
                     'sku': example.get('sku', ''), 'base_sku': example.get('base_sku', ''),
                     'status': 'confirmed' if len(codes) == 1 and len(versions) <= 1 else 'pending' if not codes else 'review',
                     'decision_id': example.get('group_revision') or example.get('decision_id', ''), 'expected_products': example.get('expected_products'),
                     'expected_labels': example.get('expected_labels'), 'members_digest': example.get('members_digest', ''),
                     'expected_files':example.get('expected_files'),'files_digest':example.get('files_digest',''),
                     'created': 0, 'recovered': True,'recovered_local':not any(r.get('decision_id') for r in rows)}
            if old:
                value=dict(old)
                if not value.get('label') and group['label']:value['label']=group['label']
                if any(r.get('metadata_conflict') for r in rows) or len(codes)>1 or len(versions)>1:
                    value.update(status='review',reason='区间文件的款号或修订不一致，请核对XMP与归档记录',review_source='metadata')
                elif any(r.get('metadata_relation')=='file_newer' or r.get('metadata_import_pending') for r in rows) or old.get('review_source')=='metadata':
                    value.update(sku=group['sku'],base_sku=group['base_sku'],decision_id=group['decision_id'],status=group['status'],reason='')
                    for k in ('expected_products','expected_labels','expected_files','files_digest','members_digest'):
                        if group.get(k) is not None:value[k]=group[k]
                    value.pop('review_source',None)
                group=value
                if group==old:continue
            with dataset.connect() as c:
                c.execute('INSERT INTO groups_ VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (gid, encoded(group).decode()))

    def _seed_imported(self,dataset):
        """轻量业务资料先入查询投影，原图身份仍由后台逐份核验。"""
        if not dataset.imported_records:return
        # 每轮限定轻量投影数量；大目录不能长期占住编辑操作使用的门禁。
        identities=list({r['id'] for r in dataset.imported_records[:512]})
        with dataset.connect() as c:
            records=[json.loads(r[0]) for r in c.execute('SELECT data FROM records WHERE id IN ('+','.join('?' for _ in identities)+')',identities)]
        records=[r for r in records if not r.get('identity_conflict')]
        dataset_id=dataset.dataset_id
        for start in range(0,len(records),256):
            batch=records[start:start+256]
            items={r['path']:r for r in self.catalog.seed_portable_records(batch,dataset.root)}
            with self.catalog.connect() as cc,self.archive.connect() as ac:
                for record in batch:
                    path=str(resolve_under(dataset.root,record['relative_path'],verify_links=False))
                    if path in items:self._project(dataset,record,items[path],catalog_connection=cc,archive_connection=ac,dataset_id=dataset_id)
        del dataset.imported_records[:512]

    def _project(self, dataset, record, item=None, *, catalog_connection=None, archive_connection=None,dataset_id=None):
        path = resolve_under(dataset.root, record['relative_path'],verify_links=False)
        dataset_id=dataset.dataset_id if dataset_id is None else dataset_id
        if item is None:
            with self.catalog.connect() as c:
                row = c.execute('SELECT * FROM items WHERE path=?', (str(path),)).fetchone()
            if row is None:
                return
            item = dict(row)
        code = record.get('sku') or record.get('inherited_sku','')
        source='portable' if dataset_id else 'embedded';normalized=normalize(code);confidence=1 if code else None
        # 没有款号的普通图片无需改写大索引来源；避免每张扫描都争抢写锁。
        if (code or item.get('sku')) and (item.get('sku'),item.get('normalized'),item.get('source'),item.get('confidence'))!=(code,normalized,source,confidence):
            try:
                with (nullcontext(catalog_connection) if catalog_connection else self.catalog.connect()) as c:
                    c.execute('PRAGMA busy_timeout=0')
                    c.execute('UPDATE items SET sku=?,normalized=?,source=?,confidence=? WHERE id=? AND (sku<>? OR normalized<>? OR source<>? OR confidence IS NOT ?)', (code,normalized,source,confidence,item['id'],code,normalized,source,confidence))
            except sqlite3.OperationalError as exc:
                if not any(word in str(exc).lower() for word in ('locked','busy')):raise
                # 本机便携库已可靠保存；大索引被其它任务写入时后台接续投影。
                self.projection_pending[(str(dataset.root),record['id'])]=dataset
                return
        scope = (record.get('inherited_scope') if not record.get('sku') else record.get('catalog_scope')) or record.get('catalog_scope') or 'default'
        key = normalize(code) + '@' + scope + ':' + digest(code.encode())[:12] if code else ''
        value = {**item, 'id': item['id'], 'capture_id': record['id'], 'session_id': 'portable:' + (dataset_id or digest(str(dataset.root).encode())[:24]),
                 'sku': code, 'normalized': key, 'source': 'portable', 'provenance': 'portable', 'provenance_label': '目录元数据',
                 'created': record.get('updated', 0), 'time': record.get('time', ''), 'root': digest(str(dataset.root).casefold().encode())[:32],
                 'root_path': str(dataset.root), 'folder': Path(record['relative_path']).parent.as_posix(), 'relative': record['relative_path'],
                 'path': str(path), 'name': path.name, 'rating': record.get('rating', 0), 'asset_id': record['asset_id'],
                 'interval_id': record.get('inherited_group_id') or record.get('group_id',''), 'preview': '/api/catalog/preview/' + item['id'] + '?size=420',
                 'has_jpg': path.suffix.lower() in ('.jpg', '.jpeg'), 'has_raw': item.get('media_type') == 'image' and path.suffix.lower() not in ('.jpg', '.jpeg'),
                 'role': record.get('role', 'product'), 'customer': record.get('customer', ''), 'brand': record.get('brand', '')}
        content = encoded(value).decode()
        with (nullcontext(archive_connection) if archive_connection else self.archive.connect()) as c:
            if code or record.get('decision_id'):
                c.execute('INSERT INTO portable_sku_evidence VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET data=excluded.data,revision=excluded.revision,updated=excluded.updated WHERE data<>excluded.data', (str(path), content, record.get('decision_id', ''), time.time()))
            else:
                c.execute('DELETE FROM portable_sku_evidence WHERE path=?', (str(path),))
            c.execute('INSERT INTO portable_ratings VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET rating=excluded.rating,updated=excluded.updated WHERE rating<>excluded.rating', (str(path), record.get('rating', 0), time.time()))

    def record_for_item(self, item_id, dataset=None, _locked=False, *, allow_conflict=False):
        if not _locked:
            with self.io_lock:return self.record_for_item(item_id, dataset, True,allow_conflict=allow_conflict)
        item = self.catalog.item(item_id)
        dataset = dataset or self.dataset_for_path(item['path'])
        # 浏览范围可能是父数据集，且实际子数据集尚未加载。以磁盘上的最近
        # dataset.json 为准，避免读父库旧记录或把子库锁误当成父库权限。
        path=Path(item['path'])
        if dataset:
            boundary=dataset.root
        else:
            with self.catalog.connect() as c:
                root=c.execute('SELECT path FROM roots WHERE id=?',(item['root'],)).fetchone()
            boundary=Path(root[0]) if root else path.parent
        for parent in (path.parent,*path.parent.parents):
            if not parent.is_relative_to(boundary):break
            if (parent/'.shiying/dataset.json').is_file():
                dataset=self._dataset(parent);break
        if not dataset:
            dataset = self._dataset(path.parent)
        relative = Path(item['path']).relative_to(dataset.root).as_posix()
        record = dataset.record(path=relative)
        signature=file_signature(Path(item['path'])) if Path(item['path']).is_file() else None
        side_signature=xmp_signature(Path(item['path']))
        if record is None or record.get('sidecar_signature')!=side_signature or (signature is not None and self.validated.get((str(dataset.root),relative.casefold()))!=signature):
            self._scan(dataset,focus=Path(item['path']))
            record = dataset.record(path=relative)
        if record is None:
            raise ValueError('素材资料尚未准备完成，请稍后再试')
        if record.get('identity_conflict') and not allow_conflict:
            raise ValueError(record['identity_conflict'])
        return dataset, record

    @staticmethod
    def _require_resolved(record):
        if record.get('metadata_conflict'):raise ValueError('XMP与归档记录存在冲突，请先选择采用XMP或保留归档确认')

    def resolve_metadata(self,item_id,choice,token,*,root_id=None,folder=''):
        if choice not in ('xmp','archive'):raise ValueError('请选择采用XMP或保留归档记录')
        with self.io_lock:
            dataset=None
            if root_id:
                dataset,scope=self.scope(root_id,folder)
                if not Path(self.catalog.item(item_id)['path']).is_relative_to(scope):raise ValueError('所选素材不在当前目录范围')
            dataset,record=self.record_for_item(item_id,dataset,allow_conflict=True)
            if record.get('identity_conflict'):raise ValueError('文件身份已经改变，不能按旧记录覆盖；请重新核对文件')
            self._acquire_editor(dataset,item_path=Path(self.catalog.item(item_id)['path']))
            self.validated.pop((str(dataset.root),record['relative_path'].casefold()),None)
            self._scan(dataset,focus=Path(self.catalog.item(item_id)['path']))
            record=dataset.record(path=record['relative_path']);info=record.get('metadata_conflict_data')
            if not info or info.get('token')!=token:raise ValueError('冲突内容已变化，请刷新后重新选择')
            if not info.get('can_use_file' if choice=='xmp' else 'can_keep_archive'):raise ValueError(info['reason'])
            group=next((g for g in dataset.groups() if g.get('status')!='cancelled' and record['asset_id'] in g.get('members',[])),None)
            assets=set(group['members']) if group else {record['asset_id']}
            before=[r for r in dataset.records() if r['asset_id'] in assets]
            for r in before:
                if r.get('identity_conflict'):raise ValueError('区间中存在身份变化的文件，未批量覆盖')
                other=r.get('metadata_conflict_data')
                if other and not other.get('can_use_file' if choice=='xmp' else 'can_keep_archive'):raise ValueError('区间中还有不支持自动处理的冲突：'+r['relative_path'])
            patch=info['file']['patch'];operation_token=uuid.uuid4().hex;updates=[];tasks=[]
            shared={k:v for k,v in patch.items() if k not in ('rating','boundary','role','shoot_date','decision_id','parents','decision_source_id','group_revision')}
            for r in before:
                if choice=='archive' and not r.get('metadata_conflict'):
                    updates.append(dict(r));continue
                current=dict(r)
                if choice=='xmp':
                    current=merge_fields(r,shared)
                    if r['asset_id']==record['asset_id']:current=merge_fields(current,patch)
                own_file=(r.get('metadata_conflict_data') or {}).get('file',{}).get('patch',{})
                current.update(decision_id=operation_token,decision_source_id=dataset.dataset_id,
                               parents=list(dict.fromkeys([*decision_parents(r),*patch.get('parents',[]),*([patch['decision_id']] if patch.get('decision_id') else []),*own_file.get('parents',[]),*([own_file['decision_id']] if own_file.get('decision_id') else [])])),
                               assignment_state='confirmed' if current.get('sku') else 'cleared' if patch.get('assignment_state')=='cleared' else 'unassigned',
                               updated=time.time())
                if choice=='xmp':current['group_revision']=operation_token
                for k in list(current):
                    if k.startswith('inherited_') or k in ('metadata_conflict','metadata_conflict_data'):current.pop(k,None)
                same_file_code=bool(current.get('sku')) and r.get('file_sku')==current['sku']
                no_write=choice=='xmp' and same_file_code
                current['write_policy']='fill_missing_sku' if no_write else 'full'
                current['write_state']='sku_verified' if no_write else 'pending'
                if current.get('boundary') and not r.get('boundary'):current['boundary_token']=operation_token
                updates.append(current)
                if not no_write:tasks.append({'kind':'metadata','record':r['id'],'revision':operation_token})
            groups=[]
            if group and choice=='xmp':
                gid=updates[0].get('group_id') or group['id']
                for r in updates:r['group_id']=gid
                codes={r.get('sku') for r in updates if r.get('sku')}
                groups.append({**group,'id':gid,'sku':next(iter(codes)) if len(codes)==1 else '',
                               'status':'confirmed' if len(codes)==1 else 'manual','decision_id':operation_token,'reason':''})
                if gid!=group['id']:groups.append({**group,'status':'cancelled','reason':'已选择XMP分组信息'})
            operation=dataset.change(updates,groups,kind='resolve_xmp',before=before,tasks=tasks)
            self._cancel_legacy(dataset,updates,'已明确处理XMP与归档确认冲突')
            self._recover_groups(dataset);self._partition(dataset)
            self._inherit_interval_skus(dataset,can_edit=True)
            for r in updates:self._project(dataset,r)
            self.wake.set()
            return {'operation_id':operation,'count':len(updates),'files_to_write':len(tasks),'choice':choice}

    def rate(self, item_id, rating, *, correction=False, force=False, root_id=None, folder=''):
        correction=True
        if type(rating) is not int or rating not in range(6):
            raise ValueError('星级必须为 0–5 的整数')
        with self.io_lock:
            dataset=None
            if root_id:
                dataset,scope=self.scope(root_id,folder)
                if not Path(self.catalog.item(item_id)['path']).is_relative_to(scope):raise ValueError('所选素材不在当前目录范围')
            dataset, record = self.record_for_item(item_id,dataset)
            self._require_resolved(record)
            if correction:self._capture_mode(dataset,True)
            self._acquire_editor(dataset,item_path=Path(self.catalog.item(item_id)['path']))
            self.check_editable(dataset)
            # record_for_item 已校验当前图片，不再重复扫描与刷新历史。
            record=dataset.record(path=record['relative_path']);self._require_resolved(record)
            if str(dataset.root) not in self.last_scan and not self.scan_cursor.get(str(dataset.root)):
                self.schedule(dataset,force=True)
                self.loading[str(dataset.root)]=True
            preparing=bool(self.loading.get(str(dataset.root)))
            if preparing:
                with dataset.connect() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('archive_partition_pending','1')")
            original=dataset.records()
            before = [r for r in original if r['asset_id'] == record['asset_id']]
            token = uuid.uuid4().hex
            boundary = record.get('boundary', False)
            if correction and record['media_type'] == 'image' and rating == 2:
                boundary = True
            if rating != 2:
                boundary = False
            if not force and record.get('rating',0)==rating and bool(record.get('boundary'))==bool(boundary) and not (correction and rating==2 and (not record.get('sku') or not record.get('decision_id'))):
                return {'item_id':item_id,'rating':rating,'queued':False,'ocr_queued':False}
            updates = [{**r, 'rating': rating, 'boundary': boundary, 'role': 'sku_label' if boundary else 'product',
                        'boundary_token': token, 'decision_id': token,'parents':decision_parents(r), 'write_state': 'pending',
                        'write_policy':'full',
                        'assignment_state':'unassigned' if boundary and not r.get('sku') else r.get('assignment_state','unassigned'),'updated': time.time()} for r in before]
            for value in updates:
                for k in list(value):
                    if k.startswith('inherited_'):value.pop(k)
            combined={r['id']:r for r in original};combined.update({r['id']:r for r in updates})
            partitioned,groups=self._partition(dataset,records=list(combined.values()),apply=False) if not preparing else ([],[])
            changed={r['id']:r for r in updates};changed.update({r['id']:r for r in partitioned})
            updates=list(changed.values());before=[r for r in original if r['id'] in changed]
            tasks = [{'kind': 'metadata', 'record': r['id'], 'revision': r['decision_id']} for r in updates]+self._source_tasks(dataset,updates,token)
            if boundary and (not record.get('boundary') or correction) and (not record.get('sku') or force):
                tasks.insert(0, {'kind': 'ocr', 'record': record['id'], 'revision': token})
            with dataset.connect() as c:
                c.execute("UPDATE tasks SET status='cancelled',reason='边界版本已更新' WHERE record=? AND kind IN ('ocr','ocr_now','ocr_apply') AND status IN ('queued','running')", (record['id'],))
            # 本机库在锁内提交即生效；共享目录发布（NAS写入与回读）移到锁外，不让下一次标星排队等待。
            with dataset.deferred_publish():
                operation = dataset.change(updates, groups,kind='rating', before=before, tasks=tasks)
                self._cancel_legacy(dataset,updates,'数据集评级或边界版本已更新')
                applied=self._apply_known_labels(dataset) if not preparing else []
                for r in updates:
                    self._project(dataset, r)
            self.wake.set()
        dataset.publish_quietly()
        return {'item_id': item_id, 'rating': rating, 'operation_id': operation, 'ocr_queued': bool(boundary and correction and (not record.get('sku') or force)), 'preparing':preparing,'applied_existing_sku':record['asset_id'] in applied}

    def _inherit_interval_skus(self,dataset,*,can_edit):
        """仅建立查询投影；读取区间现有唯一SKU，不写图片或生成OCR。"""
        records=dataset.records();by_asset=defaultdict(list);updates={};groups=[]
        for old in records:
            r={k:v for k,v in old.items() if not k.startswith('inherited_')}
            if r!=old:updates[r['id']]=r
            by_asset[r['asset_id']].append(r)
        for g in dataset.groups():
            if not g.get('label') or g.get('status')=='cancelled':continue
            rows=[r for aid in g.get('members',[]) for r in by_asset[aid]]
            if not rows:continue
            for r in rows:updates[r['id']]={**updates.get(r['id'],r),'inherited_group_id':g['id']}
            if any(r.get('metadata_conflict') or r.get('identity_conflict') for r in rows):continue
            known=[r for r in rows if r.get('sku') and r.get('assignment_state')!='cleared']
            codes={r['sku'] for r in known};scopes={r.get('catalog_scope') for r in known}
            if len(codes)>1 or len(scopes)>1:
                groups.append({**g,'status':'review','reason':'区间内已有不同款号或客户／品牌，已保留各图片原值','review_source':'metadata'});continue
            code=next(iter(codes),'')
            if not code:
                if g.get('sku_origin')=='metadata' and g.get('sku'):groups.append({**g,'sku':'','status':'manual','reason':'区间已保留，请填写款号'})
                continue
            example=known[0]
            for r in rows:
                if r.get('assignment_state')=='cleared':continue
                value=dict(updates.get(r['id'],r))
                value['inherited_group_id']=g['id']
                if not r.get('sku'):value.update(inherited_sku=code,inherited_scope=example.get('catalog_scope',''))
                if value!=r:updates[r['id']]=value
            if g.get('sku')!=code or g.get('status') in ('pending','manual'):
                groups.append({**g,'sku':code,'base_sku':example.get('base_sku') or code,'status':'confirmed','sku_origin':'metadata','reason':''})
        originals={r['id']:r for r in records}
        updates={key:value for key,value in updates.items() if originals.get(key)!=value}
        with dataset.connect() as c:
            for r in updates.values():c.execute('UPDATE records SET data=? WHERE id=?',(encoded(r).decode(),r['id']))
            for g in groups:c.execute('INSERT INTO groups_ VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',(g['id'],encoded(g).decode()))
            if updates or groups:c.execute("INSERT INTO meta VALUES('metadata_view_revision','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
        for r in updates.values():self._project(dataset,r)

    def _apply_known_labels(self,dataset,*,scope=None):
        """补齐缺SKU的文件；已有SKU只入业务档案，不重写原文件。"""
        self._inherit_interval_skus(dataset,can_edit=True)
        records=dataset.records();by_asset=defaultdict(list);applied=[]
        for r in records:by_asset[r['asset_id']].append(r)
        with dataset.connect() as c:
            pending={(r[0],r[1]) for r in c.execute("SELECT record,revision FROM tasks WHERE kind IN ('metadata','metadata_fill') AND status IN ('queued','running','failed')")}
        for g in dataset.groups():
            if not g.get('label') or not g.get('sku') or g.get('status') not in ('pending','manual','confirmed'):continue
            rows=[r for aid in g['members'] for r in by_asset[aid]]
            if any(r.get('metadata_conflict') or r.get('identity_conflict') or r.get('sku') and r['sku']!=g['sku'] for r in rows):continue
            if not any(r.get('inherited_sku') or not r.get('sku') for r in rows):continue
            token=uuid.uuid5(uuid.UUID(g['id']),'restore:'+g['sku']+digest(encoded(sorted(g['members'])))).hex
            example=next((r for r in rows if r.get('sku')),None)
            if example is None:continue
            updates=[];tasks=[]
            group_revision=example.get('group_revision') or token
            products=len({r['asset_id'] for r in rows if not r.get('boundary')});labels=len({r['asset_id'] for r in rows if r.get('boundary')})
            for r in rows:
                if r.get('file_sku')==g['sku']:
                    value=dict(r)
                    value['inherited_group_id']=g['id']
                    value['write_state']='verified' if r.get('write_state')=='verified' else 'sku_verified'
                    updates.append(value)
                    continue
                value={**r,'sku':g['sku'],'base_sku':example.get('base_sku') or g['sku'],'group_id':g['id'],
                       'parent_sku':example.get('parent_sku',''),'shoot_version':example.get('shoot_version',''),'shoot_type':example.get('shoot_type','未分类'),
                       'customer':example.get('customer',''),'brand':example.get('brand',''),'catalog_scope':example.get('catalog_scope',''),
                       'group_revision':group_revision,'expected_products':products,'expected_labels':labels,'expected_files':len(rows),
                       'members_digest':digest(encoded(sorted(g['members']))),'files_digest':digest(encoded(sorted(p.get('file_id') or p['id'] for p in rows))),
                       'assignment_state':'confirmed','write_policy':'fill_missing_sku'}
                if not r.get('sku') or not r.get('decision_id'):
                    value.update(decision_id=uuid.uuid5(uuid.UUID(token),r['id']).hex,parents=list(dict.fromkeys([*decision_parents(r),*decision_parents(example)])))
                for k in ('inherited_sku','inherited_group_id','inherited_scope'):value.pop(k,None)
                value['write_state']='verified' if r.get('write_state')=='verified' and r.get('file_sku')==g['sku'] else 'sku_verified' if r.get('file_sku')==g['sku'] else 'pending'
                updates.append(value)
                if not r.get('file_sku') and (r['id'],value['decision_id']) not in pending and (scope is None or resolve_under(dataset.root,r['relative_path'],verify_links=False).is_relative_to(scope)):
                    tasks.append({'kind':'metadata_fill','record':r['id'],'revision':value['decision_id'],'data':{'_manual_backfill':-1}})
            group={**g,'status':'confirmed','decision_id':group_revision,'expected_products':products,'expected_labels':labels,'expected_files':len(rows),'members_digest':digest(encoded(sorted(g['members']))),'files_digest':digest(encoded(sorted(p.get('file_id') or p['id'] for p in rows)))}
            dataset.change(updates,[group],kind='restore_interval',tasks=tasks)
            for r in updates:self._project(dataset,r)
            applied.append(g['label'])
        return applied

    def _partition(self, dataset, *, records=None,apply=True):
        records = dataset.records() if records is None else records
        original_groups={g['id']:g for g in dataset.groups()}
        # 已确认的多机区间有完整成员清单；换机、复制顺序不能重新拆散它。
        # 用户在区间内新增/撤销二星时，边界校验不再成立，仍走正常重划。
        preserved={};preserved_assets=set()
        for group in original_groups.values():
            if not group.get('recovered_from') or group.get('status')!='confirmed':continue
            members=set(group.get('members',[]));rows=[r for r in records if r['asset_id'] in members]
            labels={r['asset_id'] for r in rows if r.get('rating')==2 and r.get('boundary')}
            if (members and {r['asset_id'] for r in rows}==members and labels=={group.get('label')} and
                all(r.get('group_id')==group['id'] and r.get('sku')==group.get('sku') for r in rows) and
                group.get('expected_files')==len(rows) and group.get('members_digest')==digest(encoded(sorted(members))) and
                group.get('files_digest')==digest(encoded(sorted(r.get('file_id') or r['id'] for r in rows)))):
                preserved[group['label']]=group;preserved_assets.update(members)
        images = {}
        for r in records:
            if r['asset_id'] in preserved_assets:continue
            if r['media_type'] != 'image':
                continue
            old = images.get(r['asset_id'])
            if old is None or Path(r['relative_path']).suffix.lower() in ('.jpg', '.jpeg'):
                images[r['asset_id']] = r
        ordered = sorted(images.values(), key=lambda r: (r.get('sequence', 0), r['id']))
        captures = [{'id': r['asset_id'], 'jpg': r['relative_path'], 'time': r.get('time'),
                     'cameraKey': '|'.join([str(r.get('camera', {}).get('serial') or r.get('camera', {}).get('model') or ''),str(r.get('shoot_date',''))[:10],r.get('catalog_scope',''),r.get('shoot_type','未分类')])} for r in ordered]
        boundaries = {r['asset_id']: r for r in ordered if r.get('boundary') and r.get('rating') == 2}
        existing = {g.get('label'): g for g in dataset.groups() if g.get('label')}
        groups = list(preserved.values())
        active = set(preserved)
        for interval in partition(captures, boundaries):
            label = interval['label']
            old = existing.get(label)
            gid = old['id'] if old else uuid.uuid5(uuid.UUID(label), 'sku-group').hex
            members = interval['members']
            if old and set(old.get('members',[]))==set(members):members=old['members']
            group = dict(old or {}, id=gid, label=label, members=members,
                         status=(old or {}).get('status', 'pending'), created=(old or {}).get('created', time.time()), order_source=interval['order_source'])
            if old and old.get('status') == 'confirmed' and set(old.get('members',[])) != set(members):
                physical=[r for r in records if r['asset_id'] in set(members)]
                recovered_proof=old.get('recovered') and old.get('members_digest')==digest(encoded(sorted(set(members)))) and old.get('expected_files')==len(physical) and old.get('files_digest')==digest(encoded(sorted(r.get('file_id') or r['id'] for r in physical)))
                recovered_legacy=recovered_proof or old.get('recovered') and old.get('expected_products') is None
                group['status'] = 'pending' if recovered_legacy else 'review'
                group['reason'] = '沿用现场XMP款号，补齐当前区间' if recovered_legacy else '边界变化影响已确认成员，请核对后重新确认'
            groups.append(group)
            active.add(label)
        updates = []
        for label, group in existing.items():
            if label not in active and group.get('status') != 'cancelled':
                groups.append({**group, 'status': 'cancelled', 'reason': '款末标签已撤销'})
                for r in records:
                    if r.get('group_id') == group['id'] and r.get('assignment_source') not in ('manual_file','manual_group','xmp'):
                        token = r.get('boundary_token') if r.get('boundary_token')==r.get('decision_id') else uuid.uuid4().hex
                        updates.append({**r, 'sku': '', 'group_id': '', 'assignment_state': 'cleared', 'decision_id': token,'parents':decision_parents(r), 'write_state': 'pending'})
        groups=[g for g in groups if original_groups.get(g['id'])!=g]
        if not apply:return updates,groups
        if groups or updates:
            dataset.change(updates, groups, kind='partition', tasks=[{'kind': 'metadata', 'record': r['id'], 'revision': r['decision_id']} for r in updates])

    def confirm(self, item_id, sku, *, whole_group=True, expected_revision=None, automatic=False,auto_edit=False,root_id=None,folder=''):
        code = valid_sku(sku)
        with self.io_lock:
            dataset=None
            if root_id:
                dataset,scope=self.scope(root_id,folder)
                if not Path(self.catalog.item(item_id)['path']).is_relative_to(scope):raise ValueError('所选素材不在当前目录范围')
            dataset, record = self.record_for_item(item_id,dataset)
            self._require_resolved(record)
            if auto_edit:
                self._acquire_editor(dataset,item_path=Path(self.catalog.item(item_id)['path']))
                self._scan(dataset,focus=Path(self.catalog.item(item_id)['path']))
                record=dataset.record(path=record['relative_path']);self._require_resolved(record)
                if whole_group and record.get('boundary') and str(dataset.root) not in self.last_scan:self._scan(dataset)
            self.check_editable(dataset)
            if whole_group and record.get('boundary') and self.loading.get(str(dataset.root)):
                raise ValueError('区间仍在准备，输入款号可保留，请待完整区间读取完成后确认')
            if expected_revision is not None and record.get('decision_id', '') != expected_revision:
                raise ValueError('资料已更新，请刷新后重新确认')
            group = next((g for g in dataset.groups() if g.get('status') != 'cancelled' and record['asset_id'] in g.get('members', [])), None) if whole_group else None
            members = set(group['members']) if group else {record['asset_id']}
            originals = [r for r in dataset.records() if r['asset_id'] in members]
            for member in originals:self._require_resolved(member)
            customer=dataset.meta('customer') or record.get('customer','');brand=dataset.meta('brand') or record.get('brand','')
            if (record.get('customer') or record.get('brand')) and (record.get('customer',''),record.get('brand',''))!=(customer,brand):raise ValueError('此素材属于另一客户／品牌，未覆盖已有作用域')
            if len({(r.get('customer',''),r.get('brand','')) for r in originals if r.get('sku')})>1:raise ValueError('当前整款包含不同客户／品牌，请拆分后确认')
            known_scopes={r.get('catalog_scope') for r in originals if r.get('sku') and r.get('catalog_scope')}
            scope = next(iter(known_scopes)) if len(known_scopes)==1 else uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([customer,brand], ensure_ascii=False)).hex
            version = ''
            base = code
            if automatic and group:
                if group.get('base_sku') == code and group.get('sku'):
                    code, version = group['sku'], group.get('shoot_version', '')
                else:
                    from sku_shoot_versions import record_day
                    day=record_day(record,'dataset:'+dataset.dataset_id)
                    prior=[r for r in dataset.records() if r.get('group_id') != group['id'] and r.get('catalog_scope') == scope and record_day(r,'dataset:'+dataset.dataset_id)==day]
                    used={r.get('sku') for r in prior}
                    duplicate=any((r.get('base_sku') or r.get('sku'))==base and r.get('sku') for r in prior)
                    for suffix in (string.ascii_uppercase if duplicate else ['']):
                        candidate = base + ('-' + suffix if suffix else '')
                        if not duplicate or candidate not in used:
                            code, version = valid_sku(candidate), suffix
                            break
                    else:
                        raise ValueError('重拍版本 A–Z 已用完，请手动使用新款号')
            if automatic and any(r.get('sku') and r['sku']!=code for r in originals):
                raise ValueError('区间中已有不同的已确认SKU，OCR结果需要人工核对，未覆盖原确认')
            if group and group.get('sku')==code and all(r.get('sku')==code and r.get('file_sku')==code for r in originals):
                if not automatic:
                    with dataset.connect() as c:
                        for r in originals:c.execute("UPDATE tasks SET status='cancelled',reason='已明确保留当前款号' WHERE record=? AND kind IN ('ocr','ocr_now','ocr_apply') AND status IN ('queued','running')",(r['id'],))
                result={'operation_id':'','sku':code,'count':len(originals),'decision_id':record.get('decision_id',''),'unchanged':True}
                return result if automatic else self._track_confirmation(dataset,originals,result,item_id,root_id,folder)
            token = uuid.uuid4().hex
            gid = group['id'] if group else uuid.uuid4().hex
            products = len({r['asset_id'] for r in originals if not r.get('boundary')})
            labels = len({r['asset_id'] for r in originals if r.get('boundary')})
            members_hash = digest(encoded(sorted(members)))
            files_hash=digest(encoded(sorted(r.get('file_id') or r['id'] for r in originals)))
            updates = [{**r, 'sku': code, 'base_sku': base, 'parent_sku': r.get('parent_sku') or base[:4], 'shoot_version': version,
                        'group_id': gid, 'catalog_scope': scope, 'customer': customer, 'brand': brand,
                        'decision_id': token, 'parents': decision_parents(r),
                        'write_policy':'full','boundary_token':token if r.get('boundary') else r.get('boundary_token',''),
                        'assignment_state': 'confirmed', 'assignment_source': ('group' if automatic else 'manual_group') if group else 'manual_file',
                        'group_revision': token, 'expected_products': products, 'expected_labels': labels,
                        'members_digest': members_hash,'expected_files':len(originals),'files_digest':files_hash, 'write_state': 'pending', 'updated': time.time()} for r in originals]
            updated_group = {**(group or {}), 'id': gid, 'members': sorted(members), 'sku': code, 'base_sku': base,
                             'shoot_version': version, 'status': 'confirmed', 'decision_id': token,
                             'expected_products': products, 'expected_labels': labels, 'members_digest': members_hash,'expected_files':len(originals),'files_digest':files_hash, 'created': (group or {}).get('created', time.time())}
            for value in updates:
                for k in list(value):
                    if k.startswith('inherited_'):value.pop(k)
            write_tasks=[]
            for value in updates:
                same_rating=value.get('file_rating')==value.get('rating') or value.get('file_rating') is None and value.get('rating',0)==0
                if value.get('file_sku')==code and same_rating:
                    value.update(write_policy='fill_missing_sku',write_state='sku_verified')
                else:write_tasks.append({'kind':'metadata','record':value['id'],'revision':token})
            operation = dataset.change(updates, [updated_group], kind='confirm', before=originals,
                                       tasks=write_tasks+self._source_tasks(dataset,updates,token))
            if not automatic:
                with dataset.connect() as c:
                    for r in updates:c.execute("UPDATE tasks SET status='cancelled',reason='已手工填写款号' WHERE record=? AND kind IN ('ocr','ocr_now','ocr_apply') AND status IN ('queued','running')",(r['id'],))
            with self.archive.connect() as c:
                for r in updates:
                    c.execute("UPDATE metadata_jobs SET status='cancelled',reason='当前数据集已明确修正，旧写入贡献已撤销' WHERE path=? AND status IN ('queued','running')", (str(resolve_under(dataset.root, r['relative_path'])),))
            for r in updates:
                self._project(dataset, r)
            self.wake.set()
            result={'operation_id': operation, 'sku': code, 'count': len(updates), 'decision_id': token}
            return result if automatic else self._track_confirmation(dataset,updates,result,item_id,root_id,folder)

    def _track_confirmation(self,dataset,records,result,item_id,root_id,folder):
        if not root_id:
            item=self.catalog.item(item_id);root_id=item['root'];folder=item.get('folder','')
        root_id,folder=self._metadata_request_scope(root_id,folder)
        request_id=result.get('operation_id') or uuid.uuid4().hex
        payload={'mode':'manual','sku':result['sku'],'item_id':item_id,'dataset':str(dataset.root),
                 'records':[r['id'] for r in records],'revisions':{r['id']:r.get('decision_id','') for r in records},
                 'queued':sum(r.get('write_state') not in ('verified','sku_verified') for r in records)}
        with self.archive.connect() as c:
            c.execute('INSERT INTO capture_metadata_operations VALUES(?,?,?,?,?)',(request_id,root_id,folder,encoded(payload).decode(),time.time()))
        result['write_request']=self.metadata_write_state(root_id,folder)
        return result

    def batch_preview(self, root_id, folder, item_ids, sku):
        dataset, scope = self.scope(root_id, folder)
        self.check_editable(dataset)
        code = valid_sku(sku)
        if not isinstance(item_ids, list) or not 1 <= len(item_ids) <= 240:
            raise ValueError('请选择 1–240 个实际素材')
        records = []
        chosen=[]
        for item_id in dict.fromkeys(item_ids):
            d, r = self.record_for_item(item_id, dataset)
            if d is not dataset:raise ValueError('所选素材属于独立子数据集，请进入对应拍摄目录批量修改')
            self._require_resolved(r)
            path = resolve_under(dataset.root, r['relative_path'])
            if not path.is_relative_to(scope):
                raise ValueError('所选素材超出本次数据集范围')
            chosen.append((item_id,r))
        image_assets={r['asset_id'] for _,r in chosen if r['media_type']=='image'}
        selected_ids={r['id'] for _,r in chosen}
        for r in dataset.records():
            if r['id'] in selected_ids or r['asset_id'] not in image_assets:continue
            with self.catalog.connect() as c:
                item=c.execute('SELECT id FROM items WHERE path=? AND online=1',(str(resolve_under(dataset.root,r['relative_path'])),)).fetchone()
            if item:chosen.append((item['id'],r))
        if len(chosen)>2000:raise ValueError('包含配对副本后超过2000个文件，请缩小选择范围')
        for item_id,r in chosen:
            records.append({'item_id':item_id,'file_id':r.get('file_id') or r['id'],'location_id':r['id'],'path':r['relative_path'],
                            'before':r.get('sku',''),'after':code,'revision':r.get('decision_id',''),'paired':r['id'] not in selected_ids})
        token = uuid.uuid4().hex
        with dataset.connect() as c:
            c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('preview:' + token, json.dumps({'records': records, 'sku': code, 'created': time.time()})))
        return {'preview_id': token, 'rows': records, 'count': len(records)}

    def batch_apply(self, root_id, folder, preview_id):
        dataset, _ = self.scope(root_id, folder)
        self.check_editable(dataset)
        raw = dataset.meta('preview:' + preview_id)
        if not raw:
            raise ValueError('批量预览不存在或已使用')
        preview = json.loads(raw)
        if time.time() - preview['created'] > 600:
            raise ValueError('批量预览已过期，请重新核对')
        with self.io_lock:
            originals = []
            for row in preview['records']:
                d, current = self.record_for_item(row['item_id'], dataset)
                if d is not dataset:raise ValueError('预览后素材已属于独立子数据集，请进入对应拍摄目录重新预览')
                self._require_resolved(current)
                if current.get('decision_id', '') != row['revision']:
                    raise ValueError('预览后素材已改变，请重新预览')
                path = resolve_under(dataset.root, current['relative_path'])
                if file_signature(path) != current.get('signature'):
                    raise ValueError('预览后文件发生变化，请刷新资料后重新预览')
                originals.append(current)
            token = uuid.uuid4().hex
            scope = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([dataset.meta('customer'), dataset.meta('brand')], ensure_ascii=False)).hex
            group_ids = {r['asset_id']: uuid.uuid4().hex for r in originals}
            updates = [{**r, 'sku': preview['sku'], 'base_sku': preview['sku'], 'group_id': group_ids[r['asset_id']],
                        'catalog_scope': scope, 'customer': dataset.meta('customer'), 'brand': dataset.meta('brand'),
                        'decision_id': token, 'group_revision': token, 'parents': decision_parents(r),
                        'assignment_state': 'confirmed', 'assignment_source': 'manual_file', 'write_state': 'pending',
                        'expected_products': int(r.get('role') != 'sku_label'), 'expected_labels': int(r.get('role') == 'sku_label'),
                        'members_digest': digest(encoded([r['asset_id']])), 'updated': time.time()} for r in originals]
            for r in updates:
                peers=[p for p in updates if p['asset_id']==r['asset_id']]
                r.update(expected_files=len(peers),files_digest=digest(encoded(sorted(p.get('file_id') or p['id'] for p in peers))))
            groups = [{'id': group_ids[asset], 'members': [asset], 'sku': preview['sku'], 'base_sku': preview['sku'],
                       'status': 'confirmed', 'decision_id': token, 'expected_products': next(r['expected_products'] for r in updates if r['asset_id'] == asset),
                       'expected_labels': next(r['expected_labels'] for r in updates if r['asset_id'] == asset), 'created': time.time()} for asset in group_ids]
            for group in groups:
                first=next(r for r in updates if r['asset_id'] in group['members'])
                group.update(expected_files=first['expected_files'],files_digest=first['files_digest'],members_digest=first['members_digest'])
            changed_ids = {r['group_id'] for r in originals if r.get('group_id')}
            groups.extend({**g, 'status': 'review', 'reason': '部分成员已明确单独修正，请核对原分组'} for g in dataset.groups() if g['id'] in changed_ids)
            operation = dataset.change(updates, groups, kind='batch_confirm', before=originals,
                                       tasks=[{'kind': 'metadata', 'record': r['id'], 'revision': token} for r in updates]+self._source_tasks(dataset,updates,token))
            with self.archive.connect() as c:
                for r in updates:
                    c.execute("UPDATE metadata_jobs SET status='cancelled',reason='当前数据集已批量修正' WHERE path=? AND status IN ('queued','running')", (str(resolve_under(dataset.root, r['relative_path'])),))
            for r in updates:
                self._project(dataset, r)
            with dataset.connect() as c:
                c.execute('DELETE FROM meta WHERE key=?', ('preview:' + preview_id,))
            self.wake.set()
        return {'count': len(updates), 'operation_id': operation}

    def undo(self, root_id, folder, operation_id):
        dataset, _ = self.scope(root_id, folder)
        self.check_editable(dataset)
        with self.io_lock, dataset.connect() as c:
            row = c.execute('SELECT * FROM history WHERE id=?', (operation_id,)).fetchone()
            if not row:
                raise ValueError('操作历史不存在')
            before, after = json.loads(row['before_data']), {r['id']: r for r in json.loads(row['after_data'])}
            context_row=c.execute('SELECT data FROM history_context WHERE id=?',(operation_id,)).fetchone()
            context=json.loads(context_row[0]) if context_row else None
            if context and dataset.meta('context_decision')!=operation_id:raise ValueError('客户／品牌作用域已有后续修改，未按旧记录撤销')
            current = {r['id']: r for r in dataset.records()}
            for r in before:
                if r['id'] not in current or current[r['id']].get('decision_id') != after[r['id']].get('decision_id'):
                    raise ValueError('此操作之后存在新的修改，未覆盖后续更正')
                self._require_resolved(current[r['id']])
                if xmp_signature(resolve_under(dataset.root,current[r['id']]['relative_path']))!=current[r['id']].get('sidecar_signature'):
                    raise ValueError('撤销前XMP发生变化，请刷新核对后再操作')
                if file_signature(resolve_under(dataset.root,current[r['id']]['relative_path']))!=current[r['id']].get('signature'):
                    raise ValueError('撤销前文件发生变化，请重新核对当前版本')
            token = uuid.uuid4().hex
            updates=[]
            for r in before:
                updated=dict(current[r['id']])
                if r.get('asset_id') and r['asset_id']!=updated['asset_id']:raise ValueError('照片身份已经改变，未按旧记录撤销')
                for key in BUSINESS_FIELDS:
                    if key in r:updated[key]=r[key]
                    else:updated.pop(key,None)
                updated.update(decision_id=token,group_revision=token,parents=decision_parents(current[r['id']]),
                               assignment_state='confirmed' if r.get('sku') else 'cleared',write_state='pending')
                updates.append(updated)
            restored = defaultdict(list)
            for r in updates:
                if r.get('group_id'):
                    restored[r['group_id']].append(r)
            groups = []
            affected = {r.get('group_id') for r in after.values()} | set(restored)
            for group in dataset.groups():
                if group['id'] not in affected:
                    continue
                prior = restored.get(group['id'], [])
                if prior:
                    groups.append({**group, 'sku': prior[0].get('sku', ''), 'base_sku': prior[0].get('base_sku', ''),
                                   'decision_id': token, 'status': 'confirmed' if prior[0].get('sku') else 'pending'})
                else:
                    groups.append({**group, 'status': 'cancelled', 'reason': '此操作已撤销'})
            result = dataset.change(updates, groups, kind='undo', before=list(current[r['id']] for r in before),
                                    tasks=[{'kind': 'metadata', 'record': r['id'], 'revision': token} for r in updates]+self._source_tasks(dataset,updates,token),context=context['before'] if context else None)
            self._cancel_legacy(dataset,updates,'此操作已按当前业务版本撤销，旧作业已取消')
            if row['kind'] == 'rating':
                self._partition(dataset)
            for r in updates:
                self._project(dataset, r)
            self.wake.set()
            return {'operation_id': result, 'count': len(updates)}

    def _fields(self, dataset, record):
        fields = {'AssetId': record['asset_id'], 'FileId': record.get('file_id') or record['id'], 'SKU': record.get('sku', ''),
                  'ShootType': record.get('shoot_type', '未分类'), 'SegmentId': record.get('group_id', ''),
                  'MetadataRevision': str(int(dataset.meta('revision'))), 'BaseSKU': record.get('base_sku', ''),
                  'ParentSKU': record.get('parent_sku', ''), 'ShootVersion': record.get('shoot_version', ''),
                  'PhotoRole': record.get('role', 'product'), 'ShootDate': record.get('shoot_date', record.get('time', '')[:10]),
                  'AssignmentState': record.get('assignment_state', 'unassigned'), 'DecisionId': record.get('decision_id', ''),
                  'DecisionParents': json.dumps(record.get('parents', [])), 'DecisionSourceId': dataset.dataset_id,
                  'MetadataSchemaVersion': '1', 'CatalogScopeId': record.get('catalog_scope', ''),
                  'Customer': record.get('customer', ''), 'Brand': record.get('brand', ''),
                  'GroupRevision': record.get('group_revision', ''), 'GroupProductCount': str(record.get('expected_products', '') if record.get('expected_products') is not None else ''),
                  'GroupLabelCount': str(record.get('expected_labels', '') if record.get('expected_labels') is not None else ''),
                  'GroupFileCount':str(record.get('expected_files','') if record.get('expected_files') is not None else ''),'GroupFilesDigest':record.get('files_digest',''),
                  'GroupMembersDigest': record.get('members_digest', ''), 'GroupManifestScopeId': dataset.dataset_id,
                  'Rating': record.get('rating', 0)}
        return fields

    def _metadata_task(self, dataset, task):
        # 重连可能读到其它客户端的新确认，必须先同步，再判断任务是否仍有效。
        self.check_editable(dataset)
        record = dataset.record(file_id=task['record'])
        if not record or record.get('decision_id') != task['revision']:
            return 'cancelled'
        self._require_resolved(record)
        path = resolve_under(dataset.root, record['relative_path'])
        from metadata_writer import sha
        with self.archive.writer.file_lock(path,timeout=.2):
            capture_before = self.archive._capture_fingerprints(record['asset_id'])
            # ExifTool 在部分共享盘占用时只返回空输出，先保留真实 Windows 占用错误。
            with self.archive.writer.exclusive(path):
                current = self.archive.writer.read_asset(path)
            existing = current.get('XMP-fk:FileId')
            if existing and uuid_value(existing) != (record.get('file_id') or record['id']):
                raise ValueError('文件已被其它身份替换，未写入')
            if current.get('XMP-fk:AssetId') and uuid_value(current['XMP-fk:AssetId'])!=record['asset_id']:
                raise ValueError('文件资产身份不一致，未覆盖')
            actual = file_signature(path)
            if task['kind']=='metadata_fill':
                file_code=str(parse_metadata(current)['patch'].get('sku') or '')
                if file_code and file_code!=record.get('sku'):raise ValueError('文件已有不同SKU，未用区间继承值覆盖')
                if file_code==record.get('sku') and file_code:
                    updated={**record,'file_sku':file_code,'write_state':'sku_verified','signature':actual,
                             'sidecar_signature':xmp_signature(path),'verified_at':time.time(),'metadata_reader':0,'metadata_import_pending':False}
                    with self.io_lock:
                        self.check_editable(dataset)
                        latest=dataset.record(file_id=record['id'])
                        if not latest or latest.get('decision_id')!=task['revision']:return 'cancelled'
                        updated={**latest,**{k:updated[k] for k in ('file_sku','write_state','signature','sidecar_signature','verified_at','metadata_reader','metadata_import_pending')}}
                        dataset.change([updated],kind='sku_receipt')
                    return 'complete'
            current_decision=current.get('XMP-fk:DecisionId')
            allowed={None,'',record.get('decision_id'),*record.get('parents',[])}
            if current_decision not in allowed:
                raise ValueError('文件字段已被其它程序修改，请核对后重试')
            if not existing and record.get('signature')!=actual:
                raise ValueError('文件尚无永久身份且版本已改变，未按旧路径猜配')
            if not existing and record.get('file_sha256') and sha(path)!=record['file_sha256']:
                raise ValueError('无文件级身份时的内容指纹不一致，未继承旧款号')
            if not existing and record.get('capture_identity'):
                try:key=capture_key(camera_metadata(self.archive.writer.read(path,full=True)))
                except ValueError:raise ValueError('原相机身份不再完整，未按旧路径写入') from None
                if key!=record['capture_identity']:raise ValueError('相机快门身份变化，未写入旧款号')
            fields = self._fields(dataset, record)
            target = Path(str(path) + '.xmp') if record['media_type'] == 'video' else None
            matched=self._fields_match(current,fields)
            from rating_xmp import sidecar_paths
            sides=sidecar_paths(path)
            destination=path if path.suffix.lower() in ('.jpg','.jpeg') else target or (sides[-1] if sides else path.with_suffix('.xmp'))
            mirrors=[]
            if matched and path.suffix.lower() in ('.jpg','.jpeg'):
                for side in self.archive.writer.mirrored_sidecars(path,fields):
                    metadata=self.archive.writer.read(side) if side.exists() else {}
                    wanted={**fields,'FileId':metadata.get('XMP-fk:FileId') or fields['FileId']}
                    if not self._fields_match(metadata,wanted):mirrors.append(side)
            if task['kind']=='metadata_sidecar' and not matched:return 'cancelled'
            if not matched or mirrors:
                if matched:payload,destination,journal=self.archive.writer.write_sidecars(path,fields,sha(path),targets=mirrors)
                else:payload, destination, journal = self.archive.writer.write(path, fields, sha(path), mode='sidecar', _target=target)
                with self.io_lock.parallel():
                    with self.io_lock.inspect():
                        self.check_editable(dataset)
                        latest=dataset.record(file_id=record['id'])
                        if not latest or latest.get('decision_id')!=task['revision']:return 'cancelled'
                        self.archive.writer.check_sidecars(journal)
                        from metadata_history import save_record
                        save_record(self.store.data,task['id'],journal,source=str(path))
                    self.archive.writer.commit(payload, destination, journal)
                    self.archive.writer.commit_sidecars(journal)
                self.archive.writer.verify_committed(journal)
                save_record(self.store.data,task['id'],journal,source=str(path),status='applied')
            full = self.archive.writer.read(destination)
            if str(full.get('XMP-fk:SKU') or '') != fields['SKU'] or str(full.get('XMP-fk:DecisionId') or '') != task['revision']:
                raise ValueError('SKU／确认版本写后读回不一致')
            updated = {**record, 'signature': file_signature(path), 'write_state': 'verified',
                       'file_sku':record.get('sku',''),'file_rating':record.get('rating',0),'decision_source_id':dataset.dataset_id,'metadata_import_pending':False,
                       'file_sha256': sha(path), 'sidecar_sha256': sha(destination) if destination != path else '', 'verified_at': time.time()}
            updated['sidecar_signature']=xmp_signature(path);updated.pop('metadata_conflict',None)
            with self.io_lock:
                self.check_editable(dataset)
                latest=dataset.record(file_id=record['id'])
                if not latest or latest.get('decision_id')!=task['revision']:return 'cancelled'
                # OCR可能在文件写入期间补充候选；回执只合并落盘字段。
                updated={**latest,**{k:updated[k] for k in ('signature','write_state','file_sku','file_rating','decision_source_id','metadata_import_pending','file_sha256','sidecar_sha256','verified_at','sidecar_signature')}}
                dataset.change([updated], kind='metadata_receipt')
                self.validated[(str(dataset.root), record['relative_path'].casefold())] = updated['signature']
            # 回执不改变SKU、评级或分组，不能为它重查大型素材库并占住全部提交槽位。
            with self.archive.connect() as c:
                logical_file=record.get('file_id') or record['id']
                c.execute('UPDATE files SET sha=? WHERE id=?', (updated['file_sha256'], logical_file))
                if c.execute('SELECT 1 FROM files WHERE id=?', (logical_file,)).fetchone():
                    c.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?)', (logical_file, updated['file_sha256'], time.time()))
                c.execute("UPDATE metadata_jobs SET expected_sha=? WHERE path=? AND status IN ('queued','running')", (updated['file_sha256'], str(path)))
            self.archive._refresh_catalog(record['asset_id'], capture_before)
        return 'complete'

    @staticmethod
    def _fields_match(current,fields):
        for name,value in fields.items():
            if name=='MetadataRevision':continue
            actual=current.get('XMP-xmp:Rating' if name=='Rating' else 'XMP-fk:'+name)
            if name in ('AssetId','FileId'):
                if not actual or uuid_value(actual)!=uuid_value(value):return False
            elif str(actual if actual is not None else '')!=str(value if value is not None else ''):return False
        return True

    def _source_tasks(self,dataset,records,token):
        if not self.sku:return []
        result=[]
        with self.archive.connect() as c:
            for record in records:
                link=c.execute('SELECT * FROM portable_source_links WHERE file=? AND root=?',(record['id'],str(dataset.root))).fetchone()
                if link:
                    result.append({'kind':'source_sync','record':record['id'],'revision':record.get('decision_id') or token,'data':dict(link)})
        return result

    def _source_task(self,dataset,task):
        record=dataset.record(file_id=task['record'])
        if not record or record.get('decision_id')!=task['revision']:return 'cancelled'
        link=json.loads(task['data'])
        session=self.store.session(link['session'])
        if not session or Path(session['root']).resolve()!=dataset.root:return 'cancelled'
        if not record.get('sku'):
            with self.sku.lock,self.sku.connect() as c:
                projects=[r[0] for r in c.execute('SELECT DISTINCT project FROM jobs WHERE session_id=? AND capture_id=?',(link['session'],link['capture']))]
                c.execute('DELETE FROM ocr_choices WHERE session_id=? AND capture_id=?',(link['session'],link['capture']))
                c.execute("UPDATE jobs SET status='cancelled',reason='当前数据集已撤销确认',updated=? WHERE session_id=? AND capture_id=?",(time.time(),link['session'],link['capture']))
                c.execute('INSERT OR REPLACE INTO ocr_resets VALUES(?,?,?)',(link['session'],link['capture'],time.time()))
                for pid in projects:self.sku._refresh_progress(c,pid)
            return 'complete'
        details=self.sku.ocr_details(link['session'],link['capture'])
        if not details.get('has_ocr'):return 'cancelled'
        self.sku.choose_ocr_sku(link['session'],link['capture'],record['sku'],details['reading_key'],details['image_key'],link['scope'],project_id=link['project'],allow_manual=True,literal=True,defer_archive=True,portable_owned=True)
        return 'complete'

    def source_suppressed(self,sid,cid):
        with self.archive.connect() as c:
            links=[dict(r) for r in c.execute('SELECT file,root FROM portable_source_links WHERE session=? AND capture=?',(sid,cid))]
        for link in links:
            dataset=self.datasets.get(link['root'])
            record=dataset.record(file_id=link['file']) if dataset else None
            if record and record.get('assignment_state')=='cleared':return True
        return False

    def native_assignment_owned(self,sid,cid,code):
        with self.archive.connect() as c:
            links=[dict(r) for r in c.execute('SELECT file,root FROM portable_source_links WHERE session=? AND capture=?',(sid,cid))]
        for link in links:
            dataset=self.datasets.get(link['root']);record=dataset.record(file_id=link['file']) if dataset else None
            if record and record.get('assignment_state')=='confirmed' and record.get('sku')==code:return True
        return False

    def view_selection(self,sid,assets):
        session=self.store.session(sid)
        if not session:return assets
        root=Path(session['root']).absolute()
        result=[]
        for asset in assets:
            files=[f for f in asset['files'] if Path(f['path']).absolute().is_relative_to(root)]
            record=next((record for f in files for _,record in [self.record_for_path(f['path'])] if record is not None and record.get('decision_id')),None)
            if record:
                asset={**asset,'sku':record.get('sku',''),'scope':record.get('shoot_type',asset['scope']),'segment':record.get('group_id',''),
                       'source':'portable','portable_revision':record['decision_id'],'portable_metadata_state':record.get('write_state','unknown'),
                       'files':files,'jobs':[j for j in asset['jobs'] if Path(j['path']).absolute().is_relative_to(root)]}
            result.append(asset)
        return result

    def _ocr_task(self, dataset, task):
        record = next((r for r in dataset.records() if r['id'] == task['record']), None)
        auxiliary = task['kind'] == 'ocr_read'
        if not record or (record.get('ocr_token') != task['revision'] if auxiliary else not record.get('boundary') or record.get('boundary_token') != task['revision']):
            return 'cancelled'
        provider = self.sku.providers.selected('ocr') if self.sku else None
        if not provider:
            return self._manual_sku(dataset,task,'未配置OCR，区间已建立，可直接手工填写款号')
        with self.catalog.connect() as c:
            item = c.execute('SELECT id FROM items WHERE path=?', (str(resolve_under(dataset.root, record['relative_path'])),)).fetchone()
        if not item:
            raise ValueError('标签图片尚未进入素材索引')
        try:
            image = self.catalog.preview(item['id'], 1600, read=True)
            reading = self.sku.recognizer(provider, image)
        except (OSError,ValueError,RuntimeError) as exc:
            self._manual_sku(dataset,task,'OCR未完成：'+str(exc)+'；区间已保留，可手工填写款号')
            raise
        if not isinstance(reading, dict):
            raise ValueError('OCR 返回格式无效')
        with self.io_lock:
            current = next((r for r in dataset.records() if r['id'] == record['id']), None)
            if self.closed.is_set():
                return 'cancelled'
            if not current or (current.get('ocr_token') != task['revision'] if auxiliary else not current.get('boundary') or current.get('boundary_token') != task['revision']):
                return 'cancelled'
            with dataset.connect() as c:
                state=c.execute('SELECT status FROM tasks WHERE id=?',(task['id'],)).fetchone()
                if state is not None and state[0]!='running':return 'cancelled'
            self.check_editable(dataset)
            recommended = self.sku.recommended_codes([reading])
            updated = {**current, 'ocr': reading, 'candidates': recommended, 'ocr_state': 'ready' if recommended else 'manual'}
            from sku_rules import match_rules
            accepted = match_rules([reading], self.sku.sku_rules()['rules'])['suggested']
            review = [{**g, 'status': 'review' if recommended else 'manual', 'reason': 'OCR已完成，请核对并确认正确款号' if recommended else '未识别出款号，区间已保留，请手工填写SKU'} for g in dataset.groups() if g.get('label') == record['asset_id']] if not auxiliary and (len(accepted) != 1 or task['kind']=='ocr_now') else []
            dataset.change([updated], review, kind='ocr_result')
            if not auxiliary:self._queue_ocr_apply(dataset,updated)
        return 'complete'

    def _queue_ocr_apply(self,dataset,record):
        """识别与完整区间确认分开持久化；包括升级前只保存了候选的二星图。"""
        if not self.sku or not record.get('boundary') or record.get('rating')!=2 or record.get('sku') or not record.get('ocr') or record.get('assignment_state')=='cleared':return False
        with self.io_lock:
            record=dataset.record(file_id=record['id'])
            if not record or not record.get('boundary') or record.get('rating')!=2 or record.get('sku') or not record.get('ocr') or record.get('assignment_state')=='cleared' or record.get('metadata_conflict') or record.get('identity_conflict'):return False
            token=record.get('boundary_token')
            if not token:return False
            if record.get('ocr_apply_token')==token and record.get('ocr_apply_state') in ('applied','review'):return False
            from sku_rules import match_rules
            codes=match_rules([record['ocr']],self.sku.sku_rules()['rules'])['suggested']
            if len(codes)!=1:return False
            with dataset.connect() as c:
                if c.execute("SELECT 1 FROM tasks WHERE kind='ocr_apply' AND record=? AND revision=? AND status IN ('queued','running','failed')",(record['id'],token)).fetchone():return False
            updated={**record,'ocr_apply_token':token,'ocr_apply_sku':codes[0],'ocr_apply_state':'waiting_interval','ocr_reason':''}
            dataset.change([updated],kind='ocr_apply_queued',tasks=[{'kind':'ocr_apply','record':record['id'],'revision':token}])
            self.wake.set()
            return True

    def _ocr_apply_task(self,dataset,task):
        with self.io_lock:
            record=dataset.record(file_id=task['record'])
            if not record or not record.get('boundary') or record.get('rating')!=2 or record.get('assignment_state')=='cleared' or record.get('boundary_token')!=task['revision'] or record.get('ocr_apply_token')!=task['revision']:return 'cancelled'
            self.check_editable(dataset)
            if self.loading.get(str(dataset.root)):return 'queued'
            group=next((g for g in dataset.groups() if g.get('label')==record['asset_id'] and g.get('status')!='cancelled'),None)
            if not group:
                self._partition(dataset)
                group=next((g for g in dataset.groups() if g.get('label')==record['asset_id'] and g.get('status')!='cancelled'),None)
            code=record.get('ocr_apply_sku','')
            try:
                if not group:raise ValueError('二星图尚未关联完整区间，未仅确认单图')
                if record.get('sku') and record['sku']!=code:raise ValueError('已确认款号发生改变，OCR未覆盖人工结果')
                with self.catalog.connect() as c:item=c.execute('SELECT id FROM items WHERE path=?',(str(resolve_under(dataset.root,record['relative_path'])),)).fetchone()
                if not item:raise ValueError('二星标记图片不在当前素材索引中')
                self.confirm(item['id'],code,automatic=True,expected_revision=record.get('decision_id',''))
                current=dataset.record(file_id=record['id'])
                dataset.change([{**current,'ocr_apply_state':'applied','ocr_reason':''}],kind='ocr_applied')
            except ValueError as exc:
                current=dataset.record(file_id=record['id'])
                groups=[{**g,'status':'review','reason':str(exc)} for g in dataset.groups() if g.get('label')==record['asset_id'] and g.get('status')!='cancelled']
                dataset.change([{**current,'ocr_apply_state':'review','ocr_reason':str(exc)}],groups,kind='ocr_review')
            return 'complete'

    def _manual_sku(self,dataset,task,reason):
        with self.io_lock:
            record=dataset.record(file_id=task['record'])
            if not record or record.get('boundary_token')!=task['revision']:return 'cancelled'
            self.check_editable(dataset)
            groups=[{**g,'status':'manual','reason':reason} for g in dataset.groups() if g.get('label')==record['asset_id']]
            dataset.change([{**record,'ocr_state':'manual','ocr_reason':reason}],groups,kind='ocr_manual')
        return 'complete'

    @staticmethod
    def _scan_versions(items):
        """每批浅枚举一次目录；无XMP的NAS不能对每张图反复探测不存在的侧车。"""
        parents=defaultdict(list);result={}
        for item in items:parents[Path(item['path']).parent].append(Path(item['path']))
        name_key=(lambda name:name.casefold()) if os.name=='nt' else (lambda name:name)
        for parent,paths in parents.items():
            needed={name_key(p.name) for p in paths}
            for path in paths:needed.update(name_key(p.name) for p in (path.with_suffix('.xmp'),Path(str(path)+'.xmp')))
            try:
                files={}
                with os.scandir(parent) as entries:
                    for entry in entries:
                        name=name_key(entry.name)
                        if name in needed and entry.is_file(follow_symlinks=False) and not entry.is_symlink() and not entry.is_junction():files[name]=entry.stat(follow_symlinks=False)
                for path in paths:
                    stat=files.get(name_key(path.name))
                    if stat is None:continue
                    sides=[p for p in dict.fromkeys((path.with_suffix('.xmp'),Path(str(path)+'.xmp'))) if name_key(p.name) in files]
                    result[str(path)]=([stat.st_size,stat.st_mtime_ns],xmp_signature(path,sidecars=sides),sides)
            except OSError:
                # 离线/并发删除等情况仍走原逐文件校验，不把枚举失败当成没有XMP。
                continue
        return result

    def _prefetch_scan(self,dataset):
        """慢盘／ExifTool 读取在交互锁之外准备，锁内只接纳签名仍一致的结果。"""
        from rating_xmp import sidecar_metadata
        key=str(dataset.root)
        prepared={}
        items=self._rows(dataset,after=self.scan_cursor.get(key,''),limit=self.scan_window.get(key,16));versions=self._scan_versions(items)
        # 供调度线程判断：本批没有任何预读时，这份枚举即锁内扫描所需的最新版本。
        self._prefetched=(key,[r['path'] for r in items],versions)
        for item in items:
            path=Path(item['path'])
            if item.get('media_type') not in ('image','video'):continue
            version=versions.get(str(path));sidecars=version[2] if version else None
            if version:signature,side_signature=version[:2]
            else:
                if not path.is_file():continue
                signature=file_signature(path);side_signature=xmp_signature(path)
            old=dataset.record(path=path.relative_to(dataset.root).as_posix())
            if old and old.get('signature')==signature and old.get('sidecar_signature')==side_signature and old.get('metadata_reader')==READER_VERSION:continue
            full=self.archive.writer.read(path,full=True)
            if sidecars is None or sidecars:full.update(self.archive.writer.read_asset(path,embedded=full))
            prepared[str(path)]=(signature,side_signature,full,sidecar_metadata(path,grouped=True) if sidecars is None or sidecars else {})
        return prepared

    @staticmethod
    def _metadata_kind(task):return task['kind'] in ('metadata','metadata_fill','metadata_sidecar')

    def _execute_task(self,dataset,task):
        try:
            if task['kind'] in ('ocr', 'ocr_read', 'ocr_now'):
                status = self._ocr_task(dataset, task)
            elif task['kind']=='ocr_apply':
                status=self._ocr_apply_task(dataset,task)
            elif task['kind']=='source_sync':
                with self.io_lock:status=self._source_task(dataset,task)
            else:
                # 文件已验证的回执先提交本机，NAS事件发布放到交互锁和文件锁之外。
                with dataset.deferred_publish():status=self._metadata_task(dataset,task)
                dataset.publish_quietly()
            reason = ''
        except Exception as exc:
            status, reason = 'failed', str(exc)[:1000]
            if task['kind'] in ('metadata','metadata_fill','metadata_sidecar'):
                from file_safety import metadata_retry_delay
                data=json.loads(task['data']);attempts=data.get('_retry_count',0)
                delay=metadata_retry_delay(exc,attempts)
                if delay is not None:
                    status='queued';reason=f'文件暂被占用，{delay}秒后自动重试（第{attempts+1}次）：'+reason
                    data.update(_retry_at=time.time()+delay,_retry_count=attempts+1)
                    with dataset.connect() as c:c.execute('UPDATE tasks SET data=? WHERE id=?',(encoded(data).decode(),task['id']))
        with dataset.connect() as c:
            c.execute("UPDATE tasks SET status=?,reason=? WHERE id=? AND status='running'", (status, reason, task['id']))
        return status

    def _reap_metadata(self):
        worked=False
        for future,(dataset,task,key) in list(self.metadata_inflight.items()):
            if not future.done():continue
            try:worked=future.result()!='queued' or worked
            except Exception as exc:
                self.errors[str(dataset.root)]=str(exc)
                # 文件执行器已结束，不再有写入者；保留任务日志并恢复异常遗留的领取状态。
                try:
                    with dataset.connect() as c:
                        c.execute("UPDATE tasks SET status='queued',reason=? WHERE id=? AND status='running'",('已恢复中断的文件写入：'+str(exc)[:250],task['id']))
                except sqlite3.Error:continue # 数据库仍忙时保留future，下轮再恢复，不能丢掉运行回执。
            self.metadata_inflight.pop(future);self.metadata_active_keys.discard(key)
        return worked

    def _claim_task(self,dataset,parallel,*,metadata_only=False):
        allowed=not parallel or len(self.metadata_inflight)<self.metadata_workers
        manual_only=int(dataset.meta('manual_metadata_only')=='1')
        with dataset.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute("""SELECT t.*,r.path AS record_path FROM tasks t LEFT JOIN records r ON r.id=t.record
                WHERE t.status='queued' AND coalesce(json_extract(t.data,'$._retry_at'),0)<=?
                AND (t.kind NOT IN ('metadata','metadata_fill','metadata_sidecar') OR coalesce(json_extract(t.data,'$._manual_backfill'),0)!=-1)
                AND (?=0 OR t.kind!='ocr_apply')
                AND (?=0 OR t.kind IN ('metadata','metadata_fill','metadata_sidecar'))
                AND (?=1 OR t.kind NOT IN ('metadata','metadata_fill','metadata_sidecar'))
                AND (?=0 OR t.kind IN ('metadata','metadata_fill','metadata_sidecar'))
                AND (t.kind NOT IN ('metadata','metadata_fill','metadata_sidecar') OR NOT EXISTS (
                    SELECT 1 FROM tasks o WHERE o.record=t.record AND o.kind IN ('ocr','ocr_now','ocr_read')
                    AND (o.status='running' OR (?=0 AND o.status='queued' AND
                        (o.revision=t.revision OR o.revision=json_extract(r.data,'$.boundary_token') OR o.revision=json_extract(r.data,'$.ocr_token'))))))
                ORDER BY CASE t.kind WHEN 'ocr_now' THEN 0 WHEN 'ocr_read' THEN 0 WHEN 'ocr_apply' THEN 1 WHEN 'ocr' THEN 2 ELSE 3 END,t.rowid LIMIT 64""",
                (time.time(),int(bool(self.loading.get(str(dataset.root)))),manual_only,int(allowed),int(metadata_only),manual_only)).fetchall()
            for row in rows:
                task=dict(row);path=Path(dataset.root)/(task.get('record_path') or task['record'])
                key=str(path.with_suffix('')).casefold()
                if (self._metadata_kind(task) or task['kind'] in ('ocr','ocr_now','ocr_read')) and key in self.metadata_active_keys:continue
                if c.execute("UPDATE tasks SET status='running' WHERE id=? AND status='queued'",(task['id'],)).rowcount:return task,key
        return None,None

    def _submit_metadata(self,dataset,task,key):
        if self.metadata_executor is None:self.metadata_executor=ThreadPoolExecutor(max_workers=self.metadata_workers,thread_name_prefix='capture-metadata')
        self.metadata_active_keys.add(key)
        future=self.metadata_executor.submit(self._execute_task,dataset,task)
        self.metadata_inflight[future]=(dataset,task,key)
        future.add_done_callback(lambda _:(self.wake.set(),self.metadata_wake.set()))

    def _run_metadata_dispatch(self):
        # 目录预读可能等待NAS；写入调度独立运行，完成一个槽位就立即接续。
        while not self.closed.is_set():
            try:
                with self.metadata_work_lock:self._reap_metadata()
                if not self.readonly():
                    for dataset in sorted(list(self.datasets.values()),key=lambda d:str(d.root)!=self.active_root):
                        if dataset.meta('archive_freeze'):continue
                        if not self._ensure_dataset_connection(dataset):continue
                        with self.metadata_work_lock:
                            while not self.closed.is_set() and len(self.metadata_inflight)<self.metadata_workers:
                                task,key=self._claim_task(dataset,True,metadata_only=True)
                                if not task:break
                                self._submit_metadata(dataset,task,key)
            except (OSError,ValueError,sqlite3.Error) as exc:self.errors['metadata_dispatch']=str(exc)
            self.metadata_wake.wait(.05);self.metadata_wake.clear()

    def _ensure_dataset_connection(self,dataset):
        if not dataset.editing and dataset.meta('was_editing','0')!='1':return False
        now=time.monotonic()
        if now<getattr(dataset,'ownership_retry_at',0):return False
        try:dataset.ensure_ownership()
        except (OSError,DatasetBusy) as exc:
            dataset.ownership_retry_at=now+5
            self.errors[str(dataset.root)]=str(exc)
            return False
        dataset.ownership_retry_at=0
        return True

    def _pending_write_restores(self):
        """持久写入不依赖用户正在浏览哪个目录；只查询本地队列，不探测NAS。"""
        if not self.restore_roots:return []
        with self.archive.connect() as c:
            paths=[Path(r[0]) for r in c.execute("SELECT DISTINCT path FROM metadata_jobs WHERE status IN ('queued','running')")]
        return [root for root in self.restore_roots if any(path.is_relative_to(Path(root)) for path in paths)]

    def tick(self,*,parallel_metadata=False):
        with self.metadata_work_lock:completed=self._reap_metadata()
        # 不持有交互锁等待大索引写锁，且重试时读取最新业务版本。
        for key in list(self.projection_pending)[:16]:
            with self.io_lock:
                dataset=self.projection_pending.pop(key)
                record=dataset.record(file_id=key[1])
                if record:self._project(dataset,record)
        if not self.readonly():
            self._mirror_live()
            self._publish_live_boundaries()
        with self.lock:
            pending = list(self.pending)
            self.pending.clear()
        pending.sort(key=lambda root:root!=self.active_root)
        worked=completed
        for root in pending:
            visits=getattr(self.catalog,'visits',None)
            if visits:
                with visits.lock:path=visits.path
                if path is None or not path.is_relative_to(self.datasets[root].root) or not visits.allows(path/'_scope'):continue
            if root!=self.active_root and not self.datasets[root].editing:continue
            if self.datasets[root].meta('archive_freeze'):
                with self.lock:self.pending[root]=True
                continue
            worked=self._scan_batches(self.datasets[root]) or worked
        for dataset in sorted(list(self.datasets.values()),key=lambda d:str(d.root)!=self.active_root):
            if self.readonly():break
            if not dataset.editing and dataset.meta('was_editing','0')!='1':
                continue
            if dataset.meta('archive_freeze'):continue
            try:
                if not self._ensure_dataset_connection(dataset):continue
                dataset.publish_quietly()
                self._wake_native_jobs(dataset)
                for _ in range(self.metadata_workers+1 if parallel_metadata else 1):
                    with self.metadata_work_lock:
                        if self.closed.is_set():break
                        task,key=self._claim_task(dataset,parallel_metadata)
                        if not task:break
                        if parallel_metadata and self._metadata_kind(task):
                            self._submit_metadata(dataset,task,key)
                            worked=True
                            continue
                    if self._execute_task(dataset,task)!='queued':worked=True
                    break
            except (OSError, ValueError, sqlite3.Error) as exc:
                self.errors[str(dataset.root)] = str(exc)
        return worked

    def _scan_batches(self,dataset,budget=.8):
        """锁外预读与浅枚举，锁内只核对；无变化的复核批次逐步放大，并在预算内连续推进。"""
        root=str(dataset.root);deadline=time.monotonic()+budget;worked=False
        while True:
            before=self.scan_cursor.get(root,'')
            self._prefetched=None
            prepared=self._prefetch_scan(dataset)
            # 锁内扫描会逐项取走预读结果，须先记下本批是否读取过文件。
            read_files=bool(prepared)
            hint=self._prefetched
            if hint and hint[0]==root and not read_files:versions=hint[2]
            else:
                # 预读期间可能已过去较久；锁内采用的版本须在预读之后重新枚举。
                items=self._rows(dataset,after=before,limit=self.scan_window.get(root,16))
                versions=self._scan_versions(items)
            # 进度计数不参与业务校验，不能持有标星锁等待目录统计 SQL。
            total=self._scan_total(dataset) if not before else None
            with self.io_lock:
                self.scan_reads=prepared;self.scan_versions_ready=versions
                try:self._scan(dataset,limit=self.scan_window.get(root,16),total=total)
                finally:self.scan_reads={};self.scan_versions_ready=None
            worked=True
            # 有文件需要读取时保持小批，交互锁每批很快释放；连续无变化则放大到128。
            window=self.scan_window.get(root,16)
            self.scan_window[root]=16 if read_files else min(128,window*2)
            cursor=self.scan_cursor.get(root,'')
            # 需要读取文件的批次（首次/新文件）每轮只推进一批，让二星识别等任务照常穿插；
            # 只有无变化的例行复核批次才在预算内连续推进。
            if read_files or self.closed.is_set() or not cursor or cursor==before or time.monotonic()>=deadline:break
            # 让等待交互锁的标星、读取资料等请求先进入。
            time.sleep(.002)
        return worked

    def retry(self, root_id, folder=''):
        dataset, _ = self.scope(root_id, folder)
        self.check_editable(dataset)
        with dataset.connect() as c:
            c.execute("UPDATE tasks SET status='queued',reason='',data=json_remove(data,'$._retry_at','$._retry_count') WHERE status='failed'")
        self.wake.set()
        return {'queued': True}

    def write_metadata(self, root_id, folder='', *, activate=True):
        """手动补齐当前范围的漏写任务，只重试当前确认版本，不触发 OCR。"""
        dataset,scope=self.scope(root_id,folder,activate=activate)
        with self.io_lock:
            # 首次/新文件读取未完成时不能补写；已完成整轮后的例行复核与补写串行于交互锁，无需等待。
            if self.loading.get(str(dataset.root)):
                raise ValueError('正在读取元数据及SKU区间，请读取完成后补写')
            self._acquire_editor(dataset,metadata_scope=scope)
            self.check_editable(dataset)
            with dataset.connect() as c:
                before={r[0] for r in c.execute('SELECT id FROM tasks')}
            self._apply_known_labels(dataset,scope=scope)
            scoped,_=self._scope_records(dataset,scope)
            available={r['id'] for r in scoped if r.get('available')}
            queued=retried=pending=skipped=already=unassigned=0;batch=[];unassigned_examples=[]
            with dataset.connect() as c:
                for record in dataset.records():
                    if not resolve_under(dataset.root,record['relative_path'],verify_links=False).is_relative_to(scope):continue
                    if not record.get('sku'):
                        unassigned+=1
                        if len(unassigned_examples)<5:unassigned_examples.append(record['relative_path'])
                        continue
                    if not record.get('decision_id'):continue
                    if record.get('metadata_conflict') or record.get('identity_conflict') or record['id'] not in available:
                        skipped+=1;continue
                    if record.get('write_state') in ('verified','sku_verified'):already+=1;continue
                    # 仅释放本次范围内可写的确认成员；保留手动批次的重启接续。
                    c.execute("""UPDATE tasks SET data=json_set(data,'$._manual_backfill',1)
                        WHERE record=? AND revision=? AND kind IN ('metadata','metadata_fill','metadata_sidecar')
                        AND status IN ('queued','running','waiting_native','failed')""",(record['id'],record['decision_id']))
                    batch.append(record['id'])
                    tasks=c.execute("SELECT id,status FROM tasks WHERE record=? AND revision=? AND kind IN ('metadata','metadata_fill','metadata_sidecar') AND status IN ('queued','running','waiting_native','failed') ORDER BY rowid DESC",(record['id'],record['decision_id'])).fetchall()
                    active=next((t for t in tasks if t['status']!='failed'),None)
                    if active:
                        if active['id'] in before:pending+=1
                        else:queued+=1
                        continue
                    failed=next(iter(tasks),None)
                    if failed:
                        c.execute("UPDATE tasks SET status='queued',reason='',data=json_remove(data,'$._retry_at','$._retry_count') WHERE id=?",(failed['id'],));retried+=1
                    else:
                        kind='metadata_fill' if record.get('write_policy')=='fill_missing_sku' else 'metadata'
                        c.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,kind,record['id'],record['decision_id'],'queued','','{"_manual_backfill":1}'));queued+=1
            self.wake.set()
            # 资产库里尚未建任务的旧照片，只在用户点击补写时按所选范围补齐。
            if hasattr(self.archive,'request_backfill'):self.archive.request_backfill(str(scope))
            # records/dataset 仅供进度查询（本批需要写入的记录），接口不原样返回。
            return {'queued':queued,'retried':retried,'pending':pending,'skipped':skipped,'already':already,
                    'unassigned':unassigned,'unassigned_examples':unassigned_examples,'records':batch,'dataset':str(dataset.root)}

    def _metadata_request_scope(self,root_id,folder):
        from pathlib import PurePosixPath
        self.catalog._root(root_id)  # 这里只查本机登记，不等待 NAS resolve 或文件扫描。
        folder=str(folder or '').replace('\\','/')
        if folder=='/':folder=''
        parts=PurePosixPath(folder)
        if parts.is_absolute() or any(p in ('.','..') or ':' in p for p in folder.split('/')):
            raise ValueError('无效的子目录路径')
        return root_id,folder.rstrip('/')

    def metadata_write_state(self,root_id,folder=''):
        root_id,folder=self._metadata_request_scope(root_id,folder)
        with self.archive.connect() as c:
            row=c.execute('SELECT id,status,reason,result,updated,requested FROM capture_metadata_requests WHERE root=? AND folder=?',(root_id,folder)).fetchone()
            manual=c.execute("SELECT id,'submitted' AS status,'' AS reason,result,updated FROM capture_metadata_operations WHERE root=? AND folder=? ORDER BY updated DESC LIMIT 1",(root_id,folder)).fetchone()
        # 以用户发起时间选择显示入口，后台推进补写不能抢走较新的手动确认进度。
        if manual is not None and (row is None or manual['updated']>(row['requested'] or row['updated'])):row=manual
        if not row:return {'status':'idle'}
        result=json.loads(row['result'])
        state={'id':row['id'],'status':row['status'],'reason':row['reason'],'updated':row['updated'],
               'result':{k:v for k,v in result.items() if k not in ('records','dataset','revisions')}}
        dataset=self.datasets.get(result.get('dataset',''))
        if dataset is not None:
            key=str(dataset.root)
            if row['status'] in ('queued','preparing'):
                state['scan']={**(self.scan_progress.get(key) or {}),'loading':bool(self.loading.get(key)),'complete':key in self.scan_complete}
            elif row['status']=='submitted':
                try:state['progress']=self._request_progress(row['id'],dataset,result.get('records') or [],result.get('revisions'))
                except (OSError,ValueError,sqlite3.Error) as exc:state['progress_error']=str(exc)[:300]
        return state

    def _request_progress(self,request_id,dataset,records,revisions=None):
        """本批记录逐项进度：已写入、写入中、排队、失败或已被新的确认版本取代。"""
        progress={'total':len(records),'done':0,'running':0,'queued':0,'failed':0,'skipped':0,'items':{},'failures':[]}
        if not records:return progress
        identities=json.dumps(records)
        with dataset.connect() as c:
            current={r[0]:(r[1],r[2],r[3]) for r in c.execute("SELECT id,json_extract(data,'$.write_state'),json_extract(data,'$.decision_id'),json_extract(data,'$.relative_path') FROM records WHERE id IN (SELECT value FROM json_each(?))",(identities,))}
            latest={}
            for task in c.execute("SELECT record,revision,status,reason FROM tasks WHERE kind IN ('metadata','metadata_fill','metadata_sidecar') AND record IN (SELECT value FROM json_each(?)) ORDER BY rowid",(identities,)):
                latest[(task[0],task[1])]=(task[2],task[3])
        mapping=self.request_items.get(request_id)
        if mapping is None or len(mapping)<len(current):
            paths={record:str(resolve_under(dataset.root,value[2],verify_links=False)) for record,value in current.items() if value[2]}
            with self.catalog.connect() as c:
                found={r[1]:r[0] for r in c.execute('SELECT id,path FROM items WHERE path IN (SELECT value FROM json_each(?))',(json.dumps(list(paths.values()),ensure_ascii=False),))}
            mapping={record:found.get(path,'') for record,path in paths.items()}
            # 只保留最近几次请求的映射，切换目录不累积。
            while len(self.request_items)>=8:self.request_items.pop(next(iter(self.request_items)))
            self.request_items[request_id]=mapping
        for record in records:
            value=current.get(record)
            if value is None:state,reason='skipped','记录已不存在'
            else:
                write_state,decision,_=value
                status,reason=latest.get((record,decision),(None,''))
                if revisions is not None and revisions.get(record)!=decision:state,reason='skipped','确认版本已更新，按新版本写入'
                elif write_state in ('verified','sku_verified') or status=='complete':state='done'
                elif status=='running':state='running'
                elif status in ('queued','waiting_native'):state='queued'
                elif status=='failed':state='failed'
                else:state,reason='skipped','确认版本已更新，按新版本写入'
            progress[state]+=1
            item=(mapping or {}).get(record,'')
            if item:progress['items'][item]=state
            if state=='failed' and len(progress['failures'])<20:progress['failures'].append({'item_id':item,'path':value[2] if value else '','reason':reason})
        return progress

    def request_metadata_write(self,root_id,folder=''):
        if self.readonly():raise DatasetBusy('正在单向接收，请断开同步后补写元数据')
        if self.closed.is_set():raise ValueError('服务正在关闭，请重新打开后补写')
        root_id,folder=self._metadata_request_scope(root_id,folder)
        with self.archive.connect() as c:
            c.execute("""INSERT INTO capture_metadata_requests(id,root,folder,status,reason,updated)
                VALUES (?,?,?,'queued','等待目录读取完成，随后自动补写全部款号区间',?)
                ON CONFLICT(root,folder) DO UPDATE SET id=excluded.id,status='queued',reason=excluded.reason,
                result='{}',due=0,updated=excluded.updated WHERE capture_metadata_requests.status NOT IN ('queued','preparing')""",
                (uuid.uuid4().hex,root_id,folder,time.time()))
            c.execute('UPDATE capture_metadata_requests SET requested=? WHERE root=? AND folder=?',(time.time(),root_id,folder))
        self._start_metadata_request_worker()
        self.wake.set()
        return self.metadata_write_state(root_id,folder)

    def _metadata_request_wait(self,dataset):
        """补写请求的等待原因；空字符串表示可以立即补写。"""
        key=str(dataset.root)
        if key in self.scan_complete and not self.loading.get(key):return ''
        visits=getattr(self.catalog,'visits',None)
        if visits:
            with visits.lock:path=visits.path
            # 按访问加载只读取正在浏览的文件夹；离开后不在后台扫描，需回到该文件夹继续。
            if path is None or not path.is_relative_to(dataset.root) or not visits.allows(path/'_scope'):
                return '请打开此文件夹，目录读取完成后自动补写'
        if not self.loading.get(key) and not self.scan_cursor.get(key):self.schedule(dataset,force=True)
        return '正在读取目录元数据及SKU区间，完成后自动补写'

    def _start_metadata_request_worker(self):
        with self.metadata_request_start_lock:
            if not self.metadata_request_thread or not self.metadata_request_thread.is_alive():
                self.metadata_request_thread=threading.Thread(target=self._run_metadata_requests,name='manual-metadata-request',daemon=True)
                self.metadata_request_thread.start()

    def _run_metadata_requests(self):
        while not self.closed.is_set():
            try:self.process_metadata_request()
            except (OSError,ValueError,sqlite3.Error) as exc:self.errors['metadata_request']=str(exc)
            self.closed.wait(.2)

    def process_metadata_request(self):
        with self.archive.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT * FROM capture_metadata_requests WHERE status='queued' AND due<=? ORDER BY updated LIMIT 1",(time.time(),)).fetchone()
            if not row:return False
            request=dict(row)
            # 只改状态以便重启恢复；保留上次的等待原因，界面不在两种文案间闪烁。
            c.execute("UPDATE capture_metadata_requests SET status='preparing' WHERE id=?",(request['id'],))
        def wait(reason,dataset=None):
            payload=encoded({'dataset':str(dataset.root)} if dataset is not None else {}).decode()
            with self.archive.connect() as c:
                c.execute("UPDATE capture_metadata_requests SET status='queued',reason=?,result=?,due=? WHERE id=?",(reason,payload,time.time()+.5,request['id']))
            return False
        dataset=None
        try:
            if self.readonly():raise DatasetBusy('正在单向接收，请断开同步后补写元数据')
            dataset,_=self.scope(request['root'],request['folder'],activate=False)
            key=str(dataset.root)
            if self.closed.is_set():return False
            reason=self._metadata_request_wait(dataset)
            if reason:return wait(reason,dataset)
            with self.archive.connect() as c:
                c.execute("UPDATE capture_metadata_requests SET reason='正在核对补写范围…',result=? WHERE id=?",(encoded({'dataset':key}).decode(),request['id']))
            result=self.write_metadata(request['root'],request['folder'],activate=False)
            status='submitted'
            reason='已确认款号无需新增补写任务' if not result.get('records') else '已提交补写，正在逐张写入'
            if result.get('unassigned'):reason+=f"；{result['unassigned']} 张无SKU，保持原样，不参与款号补写"
        except Exception as exc:
            if str(exc)=='正在读取元数据及SKU区间，请读取完成后补写':
                return wait('正在读取目录元数据及SKU区间，完成后自动补写',dataset)
            result={};status,reason='failed',str(exc)[:1000]
        with self.archive.connect() as c:
            c.execute('UPDATE capture_metadata_requests SET status=?,reason=?,result=?,updated=? WHERE id=?',
                      (status,reason,encoded(result).decode(),time.time(),request['id']))
        return True

    def recognize(self, item_id, *, auto_edit=False):
        with self.io_lock:
            dataset, record = self.record_for_item(item_id)
            self._require_resolved(record)
            if auto_edit:self._acquire_editor(dataset,item_path=Path(self.catalog.item(item_id)['path']))
            self.check_editable(dataset)
            if record['media_type'] != 'image':
                raise ValueError('OCR 仅支持照片标签')
            token = uuid.uuid4().hex
            kind='ocr_now' if record.get('boundary') else 'ocr_read'
            with dataset.connect() as c:
                c.execute("UPDATE tasks SET status='cancelled',reason='用户重新解析当前图' WHERE record=? AND kind IN ('ocr','ocr_now','ocr_read','ocr_apply') AND status IN ('queued','running')",(record['id'],))
            dataset.change([{**record, 'boundary_token' if kind=='ocr_now' else 'ocr_token': token,'ocr_state':'queued','ocr_reason':''}], kind='ocr_read', tasks=[{'kind':kind,'record':record['id'],'revision':token}])
            self.wake.set()
            return {'queued': True,'ocr_queued':True}

    def export(self, root_id, folder='', day=''):
        dataset, scope = self.scope(root_id, folder)
        with self.io_lock:
            records, _ = self._scope_records(dataset, scope)
            revision=dataset.meta('revision')
        if day:
            records = [r for r in records if r.get('shoot_date', '')[:10] == day]
        if not records:
            raise ValueError('当前范围尚无可导出的拍摄记录')
        from archive_capture_export import export_records
        content,name=export_records(dataset, scope, records, day, self.catalog, revision=revision)
        with dataset.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            key='export_count:'+name
            row=c.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            count=int(row[0]) if row else 0
            c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,str(count+1)))
        if count:name=Path(name).stem+f'_{count:02d}.xlsx'
        return content,name

    def record_for_path(self, path):
        dataset = self.dataset_for_path(path)
        if not dataset:
            return None, None
        try:
            relative = Path(path).absolute().relative_to(dataset.root).as_posix()
        except ValueError:
            return None, None
        return dataset, dataset.record(path=relative)

    def observe_file_version(self,path,checksum):
        dataset,record=self.record_for_path(path)
        if not dataset or not record or not dataset.editing:return
        updated={**record,'signature':file_signature(path),'file_sha256':checksum}
        dataset.change([updated],kind='file_version')
        self.validated[(str(dataset.root),record['relative_path'].casefold())]=updated['signature']

    def relocate(self, moves):
        changed = defaultdict(list)
        for move in moves:
            dataset, record = self.record_for_path(move['before'])
            if not dataset or not record:
                continue
            dataset.ownership.check()
            destination = Path(move['after']).resolve()
            if not destination.is_relative_to(dataset.root):
                raise ValueError('整理移动超出原数据集根，请使用独立归档复制')
            relative = destination.relative_to(dataset.root).as_posix()
            updated = {**record, 'relative_path': relative, 'signature': file_signature(destination),
                       'aliases': list(dict.fromkeys(record.get('aliases', []) + [record['relative_path']]))}
            changed[dataset].append(updated)
            with self.archive.connect() as c:
                c.execute('DELETE FROM portable_sku_evidence WHERE path=?', (move['before'],))
                c.execute('DELETE FROM portable_ratings WHERE path=?', (move['before'],))
        for dataset, records in changed.items():
            dataset.change(records, kind='relocate')
            for record in records:
                self._project(dataset, record)

    @contextmanager
    def archive_execution(self, plan):
        """冻结共享写入，重入复用原归档核心；不改变其原图及恢复日志策略。"""
        token='execute:'+uuid.uuid4().hex;temporary=[];destination=None;release=False
        try:
            with self._archive_io():
                from archive_review_scope import validate_archive_review_scope
                validate_archive_review_scope(self, plan)
                for name in ('reference_root', 'source'):
                    root=Path(plan[name]).resolve()
                    for dataset in list(self.datasets.values()):
                        if self.dataset_for_path(dataset.root) is not dataset:continue
                        if root.is_relative_to(dataset.root) or dataset.root.is_relative_to(root):
                            dataset.ownership.check()
                            if not dataset.meta('archive_freeze'):
                                with dataset.connect() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('archive_freeze',?)",(token,))
                                temporary.append(dataset)
                target=Path(plan['target']).resolve();target.mkdir(parents=True,exist_ok=True)
                destination=self.dataset_for_path(target) or self._dataset(target);release=not destination.editing
                self._check_new_root(destination)
                if release:destination.acquire()
                if destination.meta('archive_freeze'):raise ValueError('目标已有其它归档计划，请先处理')
                with destination.connect() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('archive_freeze',?)",(token,))
                temporary.append(destination)
                self._prepare_archive_fields(plan, destination)
            yield destination
        finally:
            # 目标仍冻结，没有同目录提交；清理不能因其它目录继续写入而遗留冻结标记。
            with self.io_lock:
                for dataset in temporary:
                    with dataset.connect() as c:c.execute("DELETE FROM meta WHERE key='archive_freeze' AND value=?",(token,))
                if release and destination:destination.release()

    def _prepare_archive_fields(self, plan, destination):
        groups = defaultdict(list)
        for row in plan['items']:
            _, source = self.record_for_path(row.get('reference', row['source']))
            if source:
                src_dataset, _ = self.record_for_path(row.get('reference', row['source']))
                fields = self._fields(src_dataset, source)
                row['portable_recovery']={key:source[key] for key in ('ocr','candidates','ocr_state','assignment_source','parents','capture_identity','capture_metadata') if key in source}
                row['portable_origin'] = {'dataset_id': src_dataset.dataset_id, 'relative_path': source['relative_path'], 'business_revision': source.get('decision_id', '')}
            else:
                path = Path(row.get('reference', row['source']))
                full = self.archive.writer.read_asset(path)
                fields = self.archive.writer.owned(full)
            asset = uuid_value(row.get('asset_id') or fields.get('AssetId'), uuid.uuid5(uuid.NAMESPACE_URL, row['metadata_fingerprint']).hex)
            same_file = str(row.get('original_source', row['source'])).casefold() == str(row.get('original_reference', row.get('reference', row['source']))).casefold()
            file_id = row.get('file_id') or (fields.get('FileId') if same_file else None)
            row['asset_id'] = asset
            row['file_id'] = uuid_value(file_id, uuid.uuid5(uuid.UUID(asset), 'file:' + Path(row['source']).suffix.lower() + ':' + str(row.get('input_sha256') or row.get('file_identity_source'))).hex)
            gid = uuid_value(fields.get('SegmentId'), uuid.uuid5(uuid.UUID(asset), 'independent-group').hex)
            role = fields.get('PhotoRole') or ('sku_label' if row.get('photo_role') == 'label' else 'product')
            fields.update(AssetId=asset, FileId=row['file_id'], SKU=row['sku'], ShootType=row['scope'], Rating=row['rating'],
                          SegmentId=gid, PhotoRole=role, ParentSKU=row.get('parent_sku', ''),
                          BaseSKU=fields.get('BaseSKU') or row['sku'], AssignmentState='confirmed', MetadataSchemaVersion='1',
                          DecisionId=fields.get('DecisionId') or uuid.uuid5(uuid.UUID(gid), 'decision:' + row['sku']).hex,
                          DecisionSourceId=fields.get('DecisionSourceId') or destination.dataset_id,
                          GroupManifestScopeId=destination.dataset_id)
            fields['GroupRevision'] = fields.get('GroupRevision') or fields['DecisionId']
            row['portable_fields'] = fields
            groups[gid].append(row)
        for rows in groups.values():
            members = sorted({r['asset_id'] for r in rows})
            old = next((g for g in destination.groups() if g['id'] == rows[0]['portable_fields']['SegmentId'] and g.get('status') == 'confirmed'), None)
            if old and set(old['members']) != set(members):
                raise ValueError('目标已有同组不同成员范围，请按完整组归档或选择独立目标')
            products = len({r['asset_id'] for r in rows if r['portable_fields']['PhotoRole'] != 'sku_label'})
            labels = len({r['asset_id'] for r in rows if r['portable_fields']['PhotoRole'] == 'sku_label'})
            for row in rows:
                row['portable_fields'].update(GroupProductCount=str(products), GroupLabelCount=str(labels),GroupFileCount=str(len(rows)),GroupFilesDigest=digest(encoded(sorted(uuid_value(r['file_id']) for r in rows))), GroupMembersDigest=digest(encoded(members)))

    def finalize_archive(self, result, destination):
        records = []
        groups = defaultdict(list)
        from metadata_writer import sha
        for row in result['items']:
            path = Path(row['destination'])
            fields = row['portable_fields']
            full = self.archive.writer.read_asset(path)
            for name in ('AssetId', 'FileId', 'SKU', 'DecisionId', 'SegmentId'):
                if name in ('AssetId', 'FileId'):
                    matches = uuid_value(full.get('XMP-fk:' + name)) == uuid_value(fields[name])
                else:
                    matches = str(full.get('XMP-fk:' + name) or '') == str(fields[name])
                if not matches:
                    raise ValueError('目标文件级身份／SKU 验证失败：' + path.name + ' / ' + name)
            location_id=uuid.uuid5(uuid.UUID(fields['FileId']),path.relative_to(destination.root).as_posix()).hex
            record = {'id': location_id,'file_id':uuid_value(fields['FileId']),'location_id':location_id, 'asset_id': uuid_value(fields['AssetId']),
                      'relative_path': path.relative_to(destination.root).as_posix(), 'media_type': 'video' if row.get('video') else 'image', 'signature': file_signature(path),
                      'sku': fields['SKU'], 'base_sku': fields['BaseSKU'], 'parent_sku': fields.get('ParentSKU', ''),
                      'shoot_version': fields.get('ShootVersion', ''), 'group_id': fields['SegmentId'], 'role': fields['PhotoRole'],
                      'boundary': fields['PhotoRole'] == 'sku_label' and row['rating'] == 2, 'rating': row['rating'],
                      'decision_id': fields['DecisionId'], 'group_revision': fields['GroupRevision'],
                      'catalog_scope': fields.get('CatalogScopeId', ''), 'customer': fields.get('Customer', ''), 'brand': fields.get('Brand', ''),
                      'expected_products': int(fields['GroupProductCount']), 'expected_labels': int(fields['GroupLabelCount']),
                      'expected_files':int(fields['GroupFileCount']),'files_digest':fields['GroupFilesDigest'],
                      'members_digest': fields['GroupMembersDigest'], 'shoot_type': fields['ShootType'], 'shoot_date': fields.get('ShootDate', ''),
                      'time': camera_metadata(full).get('combined', '').replace(':','-',2).replace(' ','T',1), 'sequence':len(records), 'camera': camera_metadata(full), 'write_state': 'verified',
                      'assignment_state': 'confirmed', 'file_sha256': sha(path), 'archive_operation': result.get('id', ''),
                      'origin': row.get('portable_origin', {'relative_path': Path(row.get('original_reference', row.get('reference', row['source']))).name,
                                 'business_revision': fields['DecisionId']}), 'updated': time.time()}
            record.update(row.get('portable_recovery',{}))
            records.append(record)
            groups[record['group_id']].append(record)
        group_rows = []
        for gid, rows in groups.items():
            record = rows[0]
            group_rows.append({'id': gid, 'members': sorted({r['asset_id'] for r in rows}), 'sku': record['sku'], 'base_sku': record['base_sku'],
                               'label': next((r['asset_id'] for r in rows if r['role'] == 'sku_label'), ''), 'status': 'confirmed',
                               'decision_id': record['decision_id'], 'expected_products': record['expected_products'],
                               'expected_labels': record['expected_labels'], 'members_digest': record['members_digest'], 'created': time.time()})
            group_rows[-1].update(expected_files=record['expected_files'],files_digest=record['files_digest'])
        destination.change(records, group_rows, kind='archive_commit')
        destination.publish()
        if destination.last_error:
            raise ValueError('目标文件已验证，但便携档案待同步：' + destination.last_error)
        destination.snapshot()
        with self.catalog.connect() as c:
            root = c.execute('SELECT id FROM roots WHERE path=? AND removed=0', (str(destination.root),)).fetchone()
        if not root:
            self.catalog.add_root(str(destination.root))
        self.schedule(destination, force=True)
        result['portable_dataset_id'] = destination.dataset_id
        result['portable_revision'] = int(destination.meta('revision'))

    def _resume_needed(self,root):
        # 先读本机持有标志，不能为恢复历史只读目录而连接旧NAS或遍历共享历史。
        db=self.registry.parent/(digest(str(root).casefold().encode())+'.sqlite3')
        if not db.is_file():return False
        try:
            c=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)
            try:return c.execute("SELECT value FROM meta WHERE key='was_editing'").fetchone()==('1',)
            finally:c.close()
        except sqlite3.Error as exc:
            self.errors[str(root)]=str(exc)
            return False

    def _run(self):
        self.metadata_dispatch_thread=threading.Thread(target=self._run_metadata_dispatch,daemon=True,name='capture-metadata-dispatch')
        self.metadata_dispatch_thread.start()
        with self.archive.connect() as c:
            pending=c.execute("SELECT 1 FROM capture_metadata_requests WHERE status='queued' LIMIT 1").fetchone()
        if pending:self._start_metadata_request_worker()
        while not self.closed.is_set():
            before_cursor=dict(self.scan_cursor);worked=False
            try:
                visits=getattr(self.catalog,'visits',None);resume=True
                if visits:
                    with visits.lock:path=visits.path
                    matches=[root for root in self.restore_roots if path is not None and path.is_relative_to(Path(root)) and visits.allows(path/'_scope')]
                    matches=list(dict.fromkeys([*self._pending_write_restores(),*matches]))
                    resume=bool(matches)
                    if matches:self.restore_roots.insert(0,self.restore_roots.pop(self.restore_roots.index(matches[0])))
                if self.restore_roots and resume:
                    self.restoring=self.restore_roots.pop(0)
                    try:
                        if not self._resume_needed(self.restoring):
                            self.passive_roots.append(self.restoring)
                            continue
                        dataset=self._dataset(self.restoring)
                        if dataset.meta('was_editing','0')=='1' and not self.readonly():
                            dataset.acquire();self._recover_tasks(dataset)
                        elif not dataset.editing:
                            with self.lock:self.pending.pop(str(dataset.root),None)
                    except (OSError,ValueError) as exc:self.errors[self.restoring]=str(exc)
                    finally:self.restoring=''
                worked=self.tick(parallel_metadata=True)
            except Exception as exc:
                self.errors['service'] = str(exc)
            # 首次区间入库逐张释放锁并尽快接续；刚处理过任务时立即接续下一张，空闲保持低频。
            busy=worked or (self.pending and any(self.loading.values()) and self.scan_cursor!=before_cursor)
            self.wake.wait(.01 if busy else .2)
            self.wake.clear()

    def close(self):
        self.closed.set()
        self.wake.set()
        self.metadata_wake.set()
        if self.metadata_request_thread and self.metadata_request_thread.is_alive():self.metadata_request_thread.join()
        if self.thread.is_alive():
            self.thread.join(timeout=10)
        if self.metadata_dispatch_thread and self.metadata_dispatch_thread.is_alive():self.metadata_dispatch_thread.join()
        if self.metadata_executor is not None:self.metadata_executor.shutdown(wait=True)
        with self.metadata_work_lock:self._reap_metadata()
        self.publisher.close()
        with self.io_lock,self.archive.metadata_io_lock:
            for dataset in self.datasets.values():
                try:dataset.close()
                except (OSError,ValueError) as exc:self.errors[str(dataset.root)]=str(exc)
