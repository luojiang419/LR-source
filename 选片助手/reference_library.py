"""正式只读参考库：持久授权、后台目录索引、RAW/JPG 与统一预览缓存。"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import suppress
import ctypes
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import stat
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from asset_catalog import AssetCatalog, MEDIA_EXTENSIONS, _id, archive_folder, archive_path, media_format, media_type, pair_key
from reference_cache import ReferenceCache
from native_directory_index import iter_tree_records, ntfs_candidates, DirectoryChanges
from archive_resources import INDEX_WORKERS
from reference_storage import AccessGate, ReferenceStorage, storage_access
from file_safety import atomic_write

SUFFIX = '/api/library/v1'
DEFAULT_PORT = 8770
LAN_NETWORKS = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def normalize_address(value):
    text = str(value or '').strip()
    parsed = urlsplit(text if '://' in text else 'http://' + text)
    try:
        address = ipaddress.IPv4Address(parsed.hostname or '')
        private = address.is_loopback or any(address in network for network in LAN_NETWORKS)
        port = parsed.port if parsed.port is not None else DEFAULT_PORT
    except ValueError:
        raise ValueError('请输入局域网 IPv4 地址及端口，例如 192.168.1.23:8770') from None
    if not private or parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip('/') not in ('', '/api', '/api/library', SUFFIX):
        raise ValueError('仅支持局域网 IP，接口前缀和后缀由程序自动补齐')
    if not 1 <= port <= 65535:
        raise ValueError('端口须在 1–65535 之间')
    return f'{parsed.scheme}://{address}:{port}{SUFFIX}'


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(value, decrypt=False):
    raw = base64.b64decode(value) if decrypt else value.encode('utf8')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('Windows 用户密钥加密失败')
    try:
        result = ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)
    return result.decode('utf8') if decrypt else base64.b64encode(result).decode('ascii')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('设备接口不能重定向，请检查 IP 和端口')


class ScopedDiscovery:
    def __init__(self, search, protected, stopped):
        self.search, self.protected, self.stopped = search, protected, stopped

    def candidates(self, root, **options):
        options['exclude'] = [*options.get('exclude', ()), self.protected]
        for path in self.search.candidates(root, **options):
            if self.stopped():
                raise OSError('参考目录扫描已停止')
            yield path


class SharedArchiveStorage:
    """分屏与资产归档共用主索引时的读写门与状态接口。"""
    def __init__(self, catalog):
        self.catalog=catalog;self.gate=AccessGate();self.pausing=threading.Event()
        self.path=catalog.db.parent;self.previous='';self.warning=''
        self.job={'status':'shared','percent':100,'message':'正在使用资产归档的共享媒体数据库'}

    def require_available(self):
        if not self.catalog.db.is_file():raise ValueError('资产归档数据库不可用')

    def state(self):
        return {'path':str(self.catalog.db),'default_path':str(self.catalog.data/'asset_catalog.sqlite3'),'previous':'',
                'warning':'','shared':True,'job':dict(self.job)}

    def start(self, value):
        raise ValueError('分屏参考库与资产归档已共用同一数据库；请在归档资料目录中统一管理索引位置')

    def close(self):
        pass


class ReferenceCatalog(AssetCatalog):
    def __init__(self, data, protected, stopped, *, fast_index=None):
        super().__init__(data, fast_index=fast_index)
        self.protected, self.stopped = protected, stopped
        self.previews = protected / 'catalog_previews'
        from video_preview import VideoPreview
        self.video = VideoPreview(protected)
        # 保持 WAL 生命周期，避免每个小批次关闭最后连接时反复 checkpoint。
        self._anchor=sqlite3.connect(self.db,check_same_thread=False)
        self._anchor.execute('PRAGMA journal_mode').fetchone()
        self._progress_lock=threading.Lock();self._directory_progress={}

    def store_records(self, root_id, records, epoch=None):
        base=Path(self._root(root_id)['path']);accepted=[];now=time.time()
        with self.connect() as c:excluded=[row[0] for row in c.execute('SELECT folder FROM exclusions WHERE root=?',(root_id,))]
        for record in records:
            path=Path(record['path'])
            try:relative=path.relative_to(base).as_posix()
            except ValueError:continue
            if any(relative==folder or relative.startswith(folder+'/') for folder in excluded):continue
            size,modified=record['size'],record['modified_ns']
            fingerprint=hashlib.sha256(f'{str(path).casefold()}:{size}:{modified}'.encode()).hexdigest()
            extension=path.suffix.lower()
            accepted.append((_id(str(path).casefold()),root_id,str(path),relative,archive_folder(relative),path.name,extension,size,modified/1e9,fingerprint,pair_key(path),media_type(extension),media_format(extension),now))
        if not accepted:return
        with self.connect() as c:
            root=c.execute('SELECT scan_epoch,removed FROM roots WHERE id=?',(root_id,)).fetchone()
            if not root or root['removed']:raise InterruptedError('参考位置已移除')
            current=root['scan_epoch'] if epoch is None else epoch
            c.executemany('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,pair_key,media_type,media_format,last_seen,scan_epoch)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                path=excluded.path,relative=excluded.relative,folder=excluded.folder,name=excluded.name,size=excluded.size,modified=excluded.modified,
                fingerprint=excluded.fingerprint,pair_key=excluded.pair_key,media_type=excluded.media_type,media_format=excluded.media_format,online=1,last_seen=excluded.last_seen,scan_epoch=excluded.scan_epoch''',
                [(*row,current) for row in accepted])
        self.queue_previews({row[10] for row in accepted})

    def scan_with_progress(self, root_id, report, expected_total=0):
        base=Path(self._root(root_id)['path']);scan_metadata={}
        directory_progress=(0,1)
        with self._progress_lock:self._directory_progress[root_id]=directory_progress
        with self.connect() as c:
            c.execute("UPDATE roots SET status='scanning',scan_epoch=scan_epoch+1 WHERE id=?",(root_id,))
            epoch=c.execute('SELECT scan_epoch FROM roots WHERE id=?',(root_id,)).fetchone()[0]
            excluded=[row[0] for row in c.execute('SELECT folder FROM exclusions WHERE root=?',(root_id,))]
        batch=[];done=0;seen=set();report('indexing',0,expected_total)
        # Everything 的索引能立即提供已知候选；完整性仍由后续目录流式扫描保证。
        if self.discovery and hasattr(getattr(self.discovery,'search',None),'candidates'):
            try:
                for path in self.discovery.candidates(base,recursive=True,exclude=[self.protected,*getattr(self,'storage_paths',lambda:[])(),*[base/folder for folder in excluded]]):
                    if path.suffix.lower() not in MEDIA_EXTENSIONS:continue
                    try:info=path.stat()
                    except OSError:continue
                    batch.append({'path':str(path),'size':info.st_size,'modified_ns':info.st_mtime_ns})
                    seen.add(str(path).casefold())
                    if len(batch)>=128:
                        self.store_records(root_id,batch,epoch);batch.clear();report('everything',len(seen),expected_total)
                if batch:self.store_records(root_id,batch,epoch);batch.clear();report('everything',len(seen),expected_total)
            except OSError:
                if self.stopped():raise
        def report_directories(completed,discovered):
            nonlocal directory_progress
            directory_progress=(completed,discovered)
            with self._progress_lock:self._directory_progress[root_id]=directory_progress
            report('indexing',max(done,len(seen)),expected_total)
        # 卷根或已知大目录可用 MFT 提供首批，不能把机会候选当完整性依据。
        if base==Path(base.anchor) or expected_total>=50000:
            from preview_cache import _LOCATIONS
            for record in ntfs_candidates(base,exclude=[self.protected,*_LOCATIONS.values(),*[base/folder for folder in excluded]],stop=self.stopped,limit=4096,timeout=2.0):
                path=Path(record['path'])
                if path.suffix.lower() not in MEDIA_EXTENSIONS:continue
                try:info=path.stat()
                except OSError:continue
                batch.append({'path':str(path),'size':info.st_size,'modified_ns':info.st_mtime_ns});seen.add(str(path).casefold())
                if len(batch)>=128:
                    self.store_records(root_id,batch,epoch);batch.clear();report('mft',len(seen),expected_total)
            if batch:self.store_records(root_id,batch,epoch);batch.clear()
        try:
            for path in self._media_paths(base,excluded,scan_metadata,report_directories):
                if self.stopped():raise InterruptedError('参考索引已取消')
                record=scan_metadata.pop(str(path),None)
                if record is None:
                    try:info=path.stat()
                    except OSError:continue
                    record={'path':str(path),'size':info.st_size,'modified_ns':info.st_mtime_ns}
                if str(path).casefold() not in seen:batch.append(record)
                done+=1
                if len(batch)>=128:
                    self.store_records(root_id,batch,epoch);batch.clear();report('indexing',max(done,len(seen)),expected_total)
            if self.stopped():raise InterruptedError('参考索引已暂停')
            if batch:self.store_records(root_id,batch,epoch)
            with self.connect() as c:
                c.execute('UPDATE items SET online=0 WHERE root=? AND scan_epoch<>?',(root_id,epoch))
                c.execute("UPDATE roots SET scanned=?,status='ready' WHERE id=?",(time.time(),root_id))
            report('completed',done,done)
        finally:scan_metadata.clear()

    def directory_progress(self, root_id):
        with self._progress_lock:return self._directory_progress.get(root_id,(0,0))

    def _media_paths(self, root, excluded=(), scan_metadata=None, on_directory_progress=None):
        from preview_cache import _LOCATIONS
        exclusions=[root/folder for folder in excluded]+[self.protected,*_LOCATIONS.values(),*getattr(self,'storage_paths',lambda:[])()]
        for record in iter_tree_records(root,exclude=exclusions,stop=self.stopped,on_progress=on_directory_progress,workers=INDEX_WORKERS):
            path=Path(record['path'])
            if path.suffix.lower() not in MEDIA_EXTENSIONS:continue
            if scan_metadata is not None:scan_metadata[str(path)]=record
            yield path

    def close(self):self._anchor.close();super().close()


class ReferenceLibrary:
    def __init__(self, data, search=None, warmer=None, *, catalog=None):
        self.data = Path(data).absolute()
        self.home = self.data / 'reference-library'
        self.home.mkdir(parents=True, exist_ok=True)
        self.config_path = self.home / 'config.json'
        self.lock = threading.RLock()
        self.config = json.loads(self.config_path.read_text(encoding='utf8')) if self.config_path.exists() else {}
        self.cache = ReferenceCache(self.home/'temporary')
        for key, default in [('roots', []), ('keys', []), ('peers', []), ('sharing', {'enabled': False, 'port': DEFAULT_PORT}), ('device', socket.gethostname()), ('preferences', None)]:
            self.config.setdefault(key, default)
        self.search, self.warmer, self.shared_catalog = search, warmer, catalog
        self.storage=SharedArchiveStorage(catalog) if catalog is not None else ReferenceStorage(self)
        self.catalogs, self.cancellations, self.pending, self.queue = {}, {}, set(), []
        self.index_workers=INDEX_WORKERS
        self.index_active=set();self.index_pool=ThreadPoolExecutor(max_workers=self.index_workers,thread_name_prefix='native-reference-index')
        self.delta_pool=ThreadPoolExecutor(max_workers=self.index_workers,thread_name_prefix='reference-delta')
        self.delta_active=set();self.delta_pending={};self.watchers={};self.watch_checked={};self.watch_failed={}
        self.folder_refresh = {}
        self.stopping = threading.Event()
        self.condition = threading.Condition(self.lock)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='reference-peer')
        self.shared = None
        self.sharing_error = ''
        if self.shared_catalog is not None:self._adopt_shared_catalog_roots()
        self.thread = threading.Thread(target=self._run, name='reference-index', daemon=True)
        self.thread.start()
        self.watch_thread=threading.Thread(target=self._watch_changes,name='reference-changes',daemon=True)
        self.watch_thread.start()
        for root in self.config['roots']:
            if not root.get('removed') and (self.shared_catalog is None or not root.get('native_complete') or not self._shared_root_exists(root['id'])):
                self.enqueue(root['id'])
        if self.config['sharing'].get('enabled'):
            try:
                self.configure_sharing(True, self.config['sharing']['port'])
            except (ValueError, OSError) as error:
                self.sharing_error = str(error)

    def _adopt_shared_catalog_roots(self):
        changed=False
        with self.shared_catalog.connect() as connection:
            roots=[dict(row) for row in connection.execute('SELECT id,path,status FROM roots WHERE removed=0')]
            counts={row['root']:row['count'] for row in connection.execute('SELECT root,count(*) AS count FROM capture_items WHERE online=1 GROUP BY root')}
        with self.lock:
            known={row['id']:row for row in self.config['roots']}
            for root in roots:
                row=known.get(root['id'])
                if row is None:
                    path=Path(root['path'])
                    row={'id':root['id'],'path':root['path'],'name':path.name or path.anchor.rstrip('\\/'),'count':counts.get(root['id'],0),
                         'status':'ready','native_complete':True,'index_source':'archive',
                         'progress':{'phase':'completed','done':counts.get(root['id'],0),'total':counts.get(root['id'],0),'percent':100,'basis':'files','directories_done':0,'directories_total':0}}
                    self.config['roots'].append(row);changed=True
            if changed:self.save()

    def _shared_root_exists(self, root_id):
        with self.shared_catalog.connect() as connection:
            return bool(connection.execute('SELECT 1 FROM roots WHERE id=? AND removed=0', (root_id,)).fetchone())

    def save(self):
        atomic_write(self.config_path,json.dumps(self.config,ensure_ascii=False,indent=2).encode('utf8'))

    def _root(self, root_id, allowed=None):
        if allowed is not None and root_id not in allowed:
            raise PermissionError('此 API Key 未分配该共享目录')
        with self.lock:
            root = next((row for row in self.config['roots'] if row['id'] == root_id and not row.get('removed')), None)
            if root is None:
                raise ValueError('参考资源位置不存在或已移除')
            return dict(root)

    def resolve(self, root_id, relative='', allowed=None, exists=True):
        root = Path(self._root(root_id, allowed)['path'])
        relative = str(relative).replace('\\', '/')
        if relative.startswith('/') or ':' in relative or any(part in ('.', '..') for part in relative.split('/')):
            raise ValueError('目录路径无效')
        candidate = root / relative
        for part in [candidate, *candidate.parents]:
            if part.is_symlink() or part.is_junction():
                raise ValueError('不访问文件或目录链接')
            if part == root:
                break
        target = candidate.resolve()
        if not target.is_relative_to(root) or self.is_internal(target):
            raise ValueError('超出可共享目录范围')
        if exists and not target.exists():
            raise ValueError('设备目录离线或文件已移动')
        return root, target

    def is_internal(self, path):
        from preview_cache import _LOCATIONS
        return any(path==protected or path.is_relative_to(protected) for protected in {self.data,self.storage.path,*([Path(self.storage.previous)] if self.storage.previous else []), *_LOCATIONS.values()})

    @storage_access
    def catalog(self, root_id):
        with self.lock:
            root = self._root(root_id)
            key='_shared' if self.shared_catalog is not None else root_id
            if key not in self.catalogs:
                cancel = self.cancellations.setdefault(root_id, threading.Event())
                self.storage.require_available()
                stopped = (lambda: self.stopping.is_set() or self.storage.pausing.is_set()) if self.shared_catalog is not None else (lambda: self.stopping.is_set() or cancel.is_set() or self.storage.pausing.is_set())
                catalog = ReferenceCatalog(self.shared_catalog.data if self.shared_catalog is not None else self.storage.path / root_id,
                                         self.data, stopped, fast_index=self.shared_catalog.fast_index if self.shared_catalog is not None else None)
                if self.shared_catalog is not None:catalog.video=self.shared_catalog.video
                catalog.storage_paths=lambda:[self.storage.path,*([Path(self.storage.previous)] if self.storage.previous else [])]
                if self.search:
                    catalog.discovery = ScopedDiscovery(self.search, self.data, stopped)
                catalog.on_previews = self.warmer.enqueue if self.warmer else None
                with catalog.connect() as connection:
                    connection.execute('CREATE INDEX IF NOT EXISTS reference_name_page ON items(root,online,name COLLATE NOCASE,id)')
                self.catalogs[key] = catalog
            catalog=self.catalogs[key]
            # 共享的是数据库连接入口，每个新增位置仍须单独登记；热查询只读检查。
            with catalog.connect() as connection:
                current=connection.execute('SELECT removed FROM roots WHERE id=?',(root_id,)).fetchone()
                if current is None:
                    connection.execute('INSERT INTO roots(id,path,created) VALUES(?,?,?)',(root_id,root['path'],time.time()))
                elif current['removed']:
                    connection.execute('UPDATE roots SET removed=0 WHERE id=?',(root_id,))
            return catalog

    def enqueue(self, root_id):
        self._root(root_id)
        with self.condition:
            self.cancellations.setdefault(root_id, threading.Event()).clear()
            if root_id not in self.pending:
                self.pending.add(root_id)
                self.queue.append(root_id)
                self.condition.notify()
        return {'root_id': root_id, 'queued': True}

    def _run(self):
        while not self.stopping.is_set():
            with self.condition:
                self.condition.wait_for(lambda:self.stopping.is_set() or len(self.index_active)<self.index_workers and any(root not in self.index_active for root in self.queue))
                if self.stopping.is_set():return
                root_id=next(root for root in self.queue if root not in self.index_active);self.queue.remove(root_id)
                self.pending.discard(root_id)
                self.index_active.add(root_id)
            self.index_pool.submit(self._scan_root,root_id)

    def _sdk_request(self,payload):
        if not self.search:return {'available':False,'message':'使用本机索引'}
        cached=getattr(self.search,'cached',None)
        if cached and not cached.get('available') and time.monotonic()-getattr(self.search,'checked',0)<10:return dict(cached)
        return self.search._request(payload)

    @storage_access
    def _scan_root(self,root_id):
        try:
            with self.lock:
                root = next((r for r in self.config['roots'] if r['id'] == root_id), None)
                if not root or root.get('removed'):return
                root['status'], root['error'] = 'scanning', ''
                root['progress'] = {'phase': 'indexing', 'done': 0, 'total': 0, 'percent': 0}
                self.save()
            try:
                catalog = self.catalog(root_id)
                def report(phase, done, total):
                    with self.lock:
                        directories_done,directories_total=catalog.directory_progress(root_id)
                        root['progress'] = {'phase': phase, 'done': done, 'total': total,
                                            'estimated':phase!='completed',
                                            'basis':'files' if total else 'directories' if directories_total else 'pending',
                                            'directories_done':directories_done,'directories_total':directories_total,
                                            'percent':100 if phase=='completed' else min(99,int(done/total*100)) if total else min(99,int(directories_done/directories_total*100)) if directories_total else 0}
                indexed=self._sdk_request({'root':root['path'],'offset':0,'limit':1}) if self.shared_catalog is None and not root.get('native_complete') else {}
                if indexed.get('available') and (indexed.get('total',0)>0 or indexed.get('paths')):
                    # Everything 已维护整个位置的索引，不再复制几十万条或遍历 NAS。
                    total=indexed.get('total',len(indexed['paths']))
                    with self.lock:
                        root.update(count=total,status='queued' if root_id in self.pending else 'ready',error='',index_source='everything')
                        root['progress']={'phase':'completed','done':total,'total':total,'percent':100}
                        self.save()
                    return
                with self.lock:root.update(index_source='native',native_attempted_at=time.time())
                catalog.scan_with_progress(root_id, report, root.get('native_file_count',0))
                with catalog.connect() as connection:
                    count = connection.execute('SELECT COUNT(*) FROM capture_items WHERE root=?', (root_id,)).fetchone()[0]
                with self.lock:
                    root.update(count=count,status='queued' if root_id in self.pending else 'ready',error='',native_complete=True,native_file_count=root['progress']['done'],scan_retry_count=0)
                    self.watch_checked[root_id]=time.monotonic()
                    root['progress'] = {'phase':'queued','done':0,'total':0,'percent':0} if root_id in self.pending else {**root['progress'], 'phase': 'completed', 'percent': 100}
                    root['progress'].pop('estimated',None)
                    self.save()
            except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
                retry=isinstance(error,FileNotFoundError) and not self.stopping.is_set() and root.get('scan_retry_count',0)<2 and Path(root['path']).is_dir()
                with self.lock:
                    interrupted = (self.stopping.is_set() or self.storage.pausing.is_set() or root_id in self.pending) and not root.get('removed')
                    root.update(status='queued' if interrupted or retry else 'error', error='' if interrupted or retry else str(error)[:300])
                    if retry:root['scan_retry_count']=root.get('scan_retry_count',0)+1
                    self.save()
                if retry:
                    with suppress(ValueError):self.enqueue(root_id)
        finally:
            with self.condition:self.index_active.discard(root_id);self.condition.notify_all()

    def _watch_changes(self):
        try:
            while not self.stopping.wait(.15):
                with self.lock:roots=[dict(root) for root in self.config['roots'] if not root.get('removed')]
                ids={root['id'] for root in roots}
                for root_id in list(self.watchers):
                    if root_id not in ids:self.watchers.pop(root_id).close()
                for root in roots:
                    root_id=root['id'];now=time.monotonic()
                    if root_id not in self.watchers and now-self.watch_failed.get(root_id,-120)>60:
                        try:
                            self.watchers[root_id]=DirectoryChanges(root['path'])
                            if root_id in self.watch_failed:self.enqueue(root_id)
                            self.watch_failed.pop(root_id,None)
                        except OSError:self.watch_failed[root_id]=now
                    watcher=self.watchers.get(root_id)
                    if watcher:
                        change=watcher.poll()
                        if change['overflow']:
                            with self.lock:self.delta_pending.pop(root_id,None)
                            self.enqueue(root_id)
                        elif change['paths']:
                            with self.lock:self.delta_pending.setdefault(root_id,set()).update(change['paths'])
                        if not watcher.active:
                            watcher.close();self.watchers.pop(root_id,None);self.watch_failed[root_id]=now
                    # 原生通知用于实时更新；定期后台对账覆盖漏记及程序关闭期间的变化。
                    due=self.watch_checked.setdefault(root_id,now)
                    interval=21600 if watcher and watcher.active else 1800
                    if now-due>interval and root_id not in self.index_active:
                        self.watch_checked[root_id]=now;self.enqueue(root_id)
                    with self.lock:
                        changes=self.delta_pending.get(root_id)
                        if changes and root_id not in self.delta_active and root_id not in self.index_active:
                            changes=self.delta_pending.pop(root_id);self.delta_active.add(root_id)
                            self.delta_pool.submit(self._apply_changes,root_id,changes)
        finally:
            for watcher in self.watchers.values():watcher.close()
            self.watchers.clear()

    @storage_access
    def _apply_changes(self,root_id,changes):
        try:
            root=self._root(root_id);base=Path(root['path']);catalog=self.catalog(root_id)
            for relative in sorted(changes,key=len):
                if self.stopping.is_set():return
                if self.storage.pausing.is_set():raise InterruptedError('索引迁移中，稍后补齐变化')
                folder=str(relative).replace('\\','/')
                if folder.startswith('/') or ':' in folder or any(part.startswith('.') or part in ('#recycle','$RECYCLE.BIN','System Volume Information') for part in folder.split('/')):continue
                if self.is_internal(base/folder):continue
                _,path=self.resolve(root_id,folder,exists=False)
                if path.suffix.lower()=='.xmp':
                    (self.shared_catalog or catalog).metadata.changed_path(path)
                    continue
                try:info=path.stat()
                except FileNotFoundError:
                    if not base.is_dir():raise OSError('参考目录离线，保留已有索引')
                    with catalog.connect() as c:
                        c.execute('UPDATE items SET online=0 WHERE root=? AND (relative=? OR folder=? OR substr(folder,1,?)=?)',(root_id,folder,folder,len(folder)+1,folder+'/'))
                    continue
                if stat.S_ISDIR(info.st_mode):
                    batch=[];stamp=time.time()
                    for record in iter_tree_records(path,exclude=[self.data,self.storage.path],stop=lambda:self.stopping.is_set() or self.storage.pausing.is_set()):
                        if Path(record['path']).suffix.lower() not in MEDIA_EXTENSIONS:continue
                        batch.append(record)
                        if len(batch)>=128:catalog.store_records(root_id,batch);batch.clear()
                    if self.storage.pausing.is_set():raise InterruptedError('索引迁移中，稍后补齐变化')
                    if batch:catalog.store_records(root_id,batch)
                    with catalog.connect() as c:
                        c.execute('UPDATE items SET online=0 WHERE root=? AND (folder=? OR substr(folder,1,?)=?) AND last_seen<?',(root_id,folder,len(folder)+1,folder+'/',stamp))
                elif stat.S_ISREG(info.st_mode) and path.suffix.lower() in MEDIA_EXTENSIONS:
                    self._index_paths(root_id,[str(path)],[{'path':str(path),'size':info.st_size,'modified_ns':info.st_mtime_ns}])
            with self.lock:self.folder_refresh={key:value for key,value in self.folder_refresh.items() if key[0]!=root_id}
        except (OSError,ValueError,sqlite3.Error):
            if not self.stopping.is_set():
                with suppress(ValueError):self.enqueue(root_id)
        finally:
            with self.lock:self.delta_active.discard(root_id)

    @storage_access
    def add_root(self, path, alias=''):
        value = str(path).strip().strip('"')
        if len(value.rstrip(':')) == 1 and value[0].isalpha():
            value = value[0].upper() + ':\\'
        root = archive_path(value).resolve(strict=True)
        if not root.is_dir() or self.is_internal(root):
            raise ValueError('请选择软件资料目录之外的真实分区或文件夹')
        root_id = _id(str(root).casefold())
        with self.lock:
            row = next((r for r in self.config['roots'] if r['id'] == root_id), None)
            if row is None:
                row = {'id': root_id, 'path': str(root), 'name': str(alias).strip()[:80] or root.name or root.anchor.rstrip('\\/'), 'count': 0}
                self.config['roots'].append(row)
            if row.get('removed'):
                row['hidden'] = False
            row.update(removed=False, status='queued', error='', progress={'phase':'queued','done':0,'total':0,'percent':0})
            self.save()
        return self.enqueue(root_id)

    def add_roots(self, entries):
        if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
            raise ValueError('每次请选择 1–50 个分区或文件夹')
        if any(not isinstance(row, dict) or not isinstance(row.get('path'), str) or not isinstance(row.get('name', ''), str) or not isinstance(row.get('visible', True), bool) for row in entries):
            raise ValueError('目录信息格式不正确')
        added, errors = [], []
        for row in entries:
            try:
                result = self.add_root(row['path'], row.get('name', ''))
                if 'visible' in row:
                    self.set_root_visibility(result['root_id'], row['visible'])
                added.append({**result, 'path': row['path']})
            except (ValueError, OSError) as error:
                errors.append({'path': row['path'], 'error': str(error)})
        return {'added': added, 'errors': errors}

    def set_root_visibility(self, root_id, visible):
        if not isinstance(visible, bool):
            raise ValueError('显示开关必须为布尔值')
        with self.lock:
            self._root(root_id)
            next(row for row in self.config['roots'] if row['id'] == root_id)['hidden'] = not visible
            self.save()
        return {'ok': True, 'visible': visible}

    def rescan(self, root_id):
        self._root(root_id)
        with self.lock:
            next(r for r in self.config['roots'] if r['id'] == root_id).update(status='queued', progress={'phase':'queued','done':0,'total':0,'percent':0})
            self.save()
        return self.enqueue(root_id)

    def remove_root(self, root_id):
        self._root(root_id)
        with self.lock:
            next(r for r in self.config['roots'] if r['id'] == root_id)['removed'] = True
            self.cancellations.setdefault(root_id, threading.Event()).set()
            for key in self.config['keys']:
                key['roots'] = [value for value in key['roots'] if value != root_id]
            self.save()
        if self.shared_catalog is not None:
            with self.shared_catalog.connect() as connection:
                connection.execute('UPDATE roots SET removed=1 WHERE id=?', (root_id,))
                connection.execute('UPDATE items SET online=0 WHERE root=?', (root_id,))
        return {'ok': True}

    def roots(self, allowed=None, *, visible_only=False):
        with self.lock:
            rows = [{key: root.get(key) for key in ('id', 'name', 'count', 'status', 'progress')} for root in self.config['roots'] if not root.get('removed') and (not visible_only or not root.get('hidden')) and (allowed is None or root['id'] in allowed)]
        for row in rows:
            if len(str(row['name']))==2 and str(row['name'])[1]==':':
                row['name'] = '参考分区 ' + row['id'][:6]
        return {'schema': 'framekeep.library', 'version': 1, 'device': self.config['device'], 'roots': rows}

    @storage_access
    def folders(self, root_id, folder='', allowed=None):
        root, target = self.resolve(root_id, folder, allowed)
        rows = []
        with os.scandir(target) as entries:
            for item in entries:
                path = Path(item.path)
                if item.name.startswith('.') or not item.is_dir(follow_symlinks=False) or path.is_junction() or self.is_internal(path):
                    continue
                rows.append({'name': item.name, 'folder': path.relative_to(root).as_posix()})
        return {'folders': sorted(rows, key=lambda row: row['name'].casefold())}

    @storage_access
    def _index_paths(self, root_id, paths, metadata=()):
        catalog = self.catalog(root_id)
        accepted = []
        base=Path(self._root(root_id)['path']);known={row['path']:row for row in metadata}
        with catalog.connect() as c:excluded=[row[0] for row in c.execute('SELECT folder FROM exclusions WHERE root=?',(root_id,))]
        for value in paths:
            try:
                path = Path(value)
                relative = path.relative_to(base).as_posix()
                if any(relative==folder or relative.startswith(folder+'/') for folder in excluded):continue
                if any(part.startswith('.') or part in ('#recycle','$RECYCLE.BIN','System Volume Information') for part in Path(relative).parts) or self.is_internal(path) or path.suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                indexed=known.get(str(value))
                if indexed and indexed['size']>=0 and indexed['modified_ns']>0:
                    # SDK 已提供文件属性；搜索阶段无需逐个探测 NAS，预览仍走 resolve 授权校验。
                    size,modified_ns=indexed['size'],indexed['modified_ns']
                else:
                    _,path=self.resolve(root_id,relative)
                    if not path.is_file():continue
                    info=path.stat();size,modified_ns=info.st_size,info.st_mtime_ns
                fingerprint = hashlib.sha256(f'{str(path).casefold()}:{size}:{modified_ns}'.encode()).hexdigest()
                extension=path.suffix.lower()
                accepted.append((_id(str(path).casefold()), root_id, str(path), relative, archive_folder(relative), path.name, extension, size, modified_ns/1e9, fingerprint, pair_key(path), media_type(extension), media_format(extension), time.time()))
            except (ValueError, OSError):
                continue
        if not accepted:
            return []
        with catalog.connect() as connection:
            epoch = connection.execute('SELECT scan_epoch FROM roots WHERE id=?', (root_id,)).fetchone()[0]
            connection.executemany('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,pair_key,media_type,media_format,last_seen,scan_epoch)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,relative=excluded.relative,folder=excluded.folder,name=excluded.name,size=excluded.size,modified=excluded.modified,
                fingerprint=excluded.fingerprint,media_type=excluded.media_type,media_format=excluded.media_format,online=1,last_seen=excluded.last_seen,scan_epoch=excluded.scan_epoch''', [(*row, epoch) for row in accepted])
        catalog.queue_previews({row[10] for row in accepted})
        return [row[10] for row in accepted]

    @storage_access
    def items(self, root_id, folder='', text='', offset=0, cursor='', allowed=None, *, all_rows=False):
        started = time.perf_counter()
        text = str(text).strip()
        if len(text) > 180:
            raise ValueError('搜索文字不能超过 180 字符')
        root_state=self._root(root_id,allowed)
        if text:
            # 名称搜索只读持久索引；离线 NAS 和昂贵的路径探测不在热查询链路。
            folder=str(folder).replace('\\','/')
            if folder.startswith('/') or ':' in folder or any(part in ('.','..') for part in folder.split('/')):raise ValueError('目录路径无效')
            target=Path(root_state['path'])/folder
        else:_,target=self.resolve(root_id,folder,allowed)
        catalog = self.catalog(root_id)
        engine,note='目录数据库','完整索引在后台更新';matched=None
        if self.shared_catalog is None and self.search and not root_state.get('native_complete'):
            page_offset=0;keys=set();complete=False
            while True:
                result=self._sdk_request({'root':str(target),'text':text,'limit':2048,'offset':page_offset,'recursive':bool(text),'metadata':True})
                if not result.get('available'):
                    note=result.get('message','Everything 不可用，使用目录数据库');break
                keys.update(self._index_paths(root_id,result.get('paths',[]),result.get('records',[])))
                if not result.get('has_more'):
                    complete=True;break
                if not result.get('paths'):break
                page_offset+=2048
            if complete and (keys or self._root(root_id).get('index_source')=='everything'):
                matched=keys;engine,note='Everything','直接查询 Everything 已有索引；预览按需加载'
            elif matched is None and time.time()-root_state.get('native_attempted_at',0)>60:
                # Everything 退出后自动补建本机索引，不能永远停留在命中缓存。
                with self.condition:
                    if root_id not in self.index_active and root_id not in self.pending:self.enqueue(root_id)
        # 立即补齐当前层，整盘扫描仍在后台时也能浏览刚打开的目录。
        stamp = time.monotonic()
        if matched is None and not text and stamp-self.folder_refresh.get((root_id, folder), 0)>3:
            batch = []
            with os.scandir(target) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in MEDIA_EXTENSIONS:
                        batch.append(entry.path)
                        if len(batch)>=128:
                            self._index_paths(root_id, batch)
                            batch.clear()
            self._index_paths(root_id, batch)
            self.folder_refresh[(root_id, folder)] = stamp
        # 禁止文本查询被名称排序索引误导为整根扫描；候选由倒排索引驱动。
        clauses, args = ['+root=?' if text else 'root=?'], [root_id]
        if text:
            if folder:
                clauses.append('(folder=? OR substr(folder,1,?)=?)')
                args.extend([folder, len(folder)+1, folder+'/'])
        else:
            clauses.append('folder=?')
            args.append(folder or '/')
        where = ' AND '.join(clauses)
        offset = max(0, int(offset))
        with catalog.connect() as connection:
            if matched is None and text:
                predicate,parameters=catalog.fast_index.condition(connection,text)
                where+=' AND '+predicate;args.extend(parameters)
                engine=catalog.fast_index.engine
                note='本机持久名称索引，不访问原照片；新增变化由后台增量更新'
            if matched is not None:
                connection.execute('CREATE TEMP TABLE reference_matches(pair_key TEXT PRIMARY KEY)')
                connection.executemany('INSERT OR IGNORE INTO reference_matches VALUES (?)',[(key,) for key in matched])
                where+=' AND pair_key IN (SELECT pair_key FROM reference_matches)'
            total = connection.execute('SELECT COUNT(*) FROM capture_items WHERE '+where, args).fetchone()[0]
            if cursor:
                try:
                    name, item_id = json.loads(base64.urlsafe_b64decode(cursor).decode())
                    if not isinstance(name, str) or not isinstance(item_id, str):
                        raise ValueError()
                except (ValueError, TypeError, UnicodeError):
                    raise ValueError('资源分页标记无效') from None
                where += ' AND (name COLLATE NOCASE>? OR (name COLLATE NOCASE=? AND id>?))'
                args += [name, name, item_id]
                offset = 0
            rows = [dict(row) for row in connection.execute('SELECT id,relative,name,folder,fingerprint,media_type,media_format,codec,duration_ms FROM capture_items WHERE '+where+' ORDER BY name COLLATE NOCASE,id'+('' if all_rows else ' LIMIT 61 OFFSET ?'), args if all_rows else [*args, offset])]
        poster_frame=catalog.video.frame
        for row in rows:
            if row.get('media_type')=='video':row['fingerprint']+=f':frame={poster_frame}'
        more = not all_rows and len(rows)>60
        if not all_rows:rows=rows[:60]
        next_cursor = base64.urlsafe_b64encode(json.dumps([rows[-1]['name'], rows[-1]['id']], ensure_ascii=False).encode()).decode() if more else ''
        root_state=self._root(root_id);incomplete=root_state['status']=='error' or bool(self.search and matched is None and root_state.get('index_source')=='everything')
        if incomplete:note+='；当前位置未就绪，显示已缓存索引快照，可刷新索引重试'
        return {'rows': rows, 'total': total, 'has_more': more, 'next_cursor': next_cursor, 'engine': engine, 'note': note, 'incomplete':incomplete,'status': root_state['status'], 'elapsed_ms': round((time.perf_counter()-started)*1000, 1)}

    @storage_access
    def search_all(self, text, cursor='', generation=None):
        started=time.perf_counter();text=str(text).strip()
        if not text or len(text)>180:raise ValueError('请输入 1–180 字符的搜索文字')
        if generation is not None and str(generation)!=str(self.cache.generation):raise ValueError('拍摄任务已切换，请重新搜索参考图片')
        sources=self.sources()['devices'];rows={};errors=[];engines=set();active=False
        scope=hashlib.sha256(json.dumps([text,[(device['id'],[root['id'] for root in device['roots']]) for device in sources]],ensure_ascii=False).encode()).hexdigest()
        after=None
        if cursor:
            try:
                saved=json.loads(base64.urlsafe_b64decode(cursor).decode())
                if saved['scope']!=scope or not isinstance(saved['after'],list) or len(saved['after'])!=4 or not all(isinstance(part,str) for part in saved['after']):raise ValueError()
                after=tuple(saved['after'])
            except (ValueError,KeyError,TypeError,UnicodeError):raise ValueError('搜索范围已变化，请重新搜索') from None
        for device in sources:
            if device.get('error'):errors.append(device['name']+'：'+device['error'])
            for root in device['roots']:
                try:
                    if device['id']=='local':
                        data=self.items(root['id'],text=text,all_rows=True);items=data['rows']
                    else:
                        items=[];page_cursor=''
                        while True:
                            data=self.client_resource(device['id'],'items',{'root':root['id'],'q':text,'cursor':page_cursor,'cache_generation':self.cache.generation})
                            items.extend(data['rows'])
                            if not data.get('has_more'):break
                            next_cursor=data.get('next_cursor')
                            if not next_cursor or next_cursor==page_cursor:raise ValueError('设备分页未前进')
                            page_cursor=next_cursor
                    engines.add(data['engine']);active|=data['status'] in ('queued','scanning')
                    if data.get('incomplete'):errors.append(device['name']+' / '+root['name']+'：'+data['note'])
                    for row in items:
                        rows.setdefault((device['id'],row['id']),{**row,'device':device['id'],'root':root['id'],'source':device['name']+' / '+root['name']})
                except (ValueError,OSError,sqlite3.Error) as error:errors.append(device['name']+' / '+root['name']+'：'+str(error))
        key=lambda row:(row['name'].casefold(),'0' if row['device']=='local' else '1'+row['device'],row['root'],row['id'])
        ordered=sorted(rows.values(),key=key);selected=[row for row in ordered if after is None or key(row)>after];more=len(selected)>60;selected=selected[:60]
        next_cursor=base64.urlsafe_b64encode(json.dumps({'scope':scope,'after':key(selected[-1])},ensure_ascii=False).encode()).decode() if more else ''
        return {'rows':selected,'total':len(ordered),'has_more':more,'next_cursor':next_cursor,'engine':' + '.join(sorted(engines)) or '参考数据库',
                'status':'scanning' if active else 'ready','note':'全部已添加目录 · '+'；'.join(errors) if errors else '全部已添加目录，图片按需加载',
                'errors':errors,'elapsed_ms':round((time.perf_counter()-started)*1000,1)}

    @storage_access
    def preview(self, root_id, relative, size=420, allowed=None):
        _, path = self.resolve(root_id, relative, allowed, exists=False)
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError('此文件不是支持的参考素材')
        catalog = self.catalog(root_id)
        with catalog.connect() as connection:
            row = connection.execute('SELECT id FROM items WHERE path=? AND online=1', (str(path),)).fetchone()
        if row is None:
            self._index_paths(root_id, [path])
        return catalog.preview(row['id'] if row else _id(str(path).casefold()), max(80, min(1800, int(size))), read=True)

    def create_key(self, name, roots=None):
        name = str(name or '').strip()
        if not name or len(name) > 48:
            raise ValueError('请填写 1–48 字符的调用方名称')
        roots = [] if roots is None else roots
        self._validate_grants(roots)
        token = 'fk_' + secrets.token_urlsafe(32)
        row = {'id': secrets.token_hex(8), 'name': name, 'roots': list(dict.fromkeys(roots)), 'hash': hashlib.sha256(token.encode()).hexdigest(), 'secret': protect(token), 'prefix': token[:10], 'disabled': False, 'created': time.strftime('%Y-%m-%d %H:%M')}
        with self.lock:
            self.config['keys'].append(row)
            self.save()
        return {'id': row['id'], 'api_key': token}

    def _validate_grants(self, roots):
        known = {root['id'] for root in self.config['roots'] if not root.get('removed')}
        if not isinstance(roots, list) or not all(isinstance(root, str) and root in known for root in roots):
            raise ValueError('目录分配须为已添加的参考资源位置列表，可留空')

    def key(self, key_id):
        row = next((key for key in self.config['keys'] if key['id'] == key_id), None)
        if row is None:
            raise ValueError('API Key 不存在')
        return row

    def reveal(self, key_id):
        with self.lock:
            row = self.key(key_id)
            if row['disabled']:
                raise ValueError('API Key 已停用')
            return protect(row['secret'], True)

    def assign_key(self, key_id, roots):
        with self.lock:
            self._validate_grants(roots)
            self.key(key_id)['roots'] = list(dict.fromkeys(roots))
            self.save()
        return {'ok': True}

    def disable_key(self, key_id):
        with self.lock:
            self.key(key_id)['disabled'] = True
            self.save()
        return {'ok': True}

    def authorize(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            key = next((row for row in self.config['keys'] if not row['disabled'] and secrets.compare_digest(digest, row['hash'])), None)
            if key is None:
                raise PermissionError('API Key 无效或已停用')
            return set(key['roots'])

    def peer(self, peer_id):
        with self.lock:
            peer = next((dict(row) for row in self.config['peers'] if row['id'] == peer_id), None)
        if peer is None:
            raise ValueError('设备连接已移除')
        return peer

    def remote(self, peer, route, query=None, timeout=8):
        if route not in ('roots', 'folders', 'items', 'preview'):
            raise ValueError('设备接口不存在')
        url = normalize_address(peer['address']) + '/' + route + ('?'+urlencode(query) if query else '')
        request = Request(url, headers={'Authorization': 'Bearer '+protect(peer['secret'], True),'Accept':'image/jpeg' if route=='preview' else 'application/json'})
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
                limit = 12*1024*1024 if route=='preview' else 4*1024*1024
                raw = response.read(limit+1)
                if len(raw)>limit:
                    raise ValueError('设备返回内容过大')
                if route=='preview':
                    from preview_codec import image_mime
                    if response.headers.get_content_type()!=image_mime(raw):
                        raise ValueError('设备没有返回有效图片')
                    return raw
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError('设备响应格式不正确')
                return result
        except HTTPError as error:
            raise ValueError('设备密钥无效、已停用或没有目录权限' if error.code in (401, 403) else '设备目录不可用，请刷新后重试') from None
        except (URLError, TimeoutError, OSError):
            raise ValueError('设备离线或连接超时，请检查 IP、端口及对方共享设置') from None

    def client_resource(self, device, route, query):
        if 'cache_generation' in query and str(query['cache_generation'])!=str(self.cache.generation):
            raise ValueError('拍摄任务已切换，请重新搜索参考图片')
        params = {key: value for key, value in query.items() if key!='cache_generation'}
        root, relative = str(params.get('root', '')), str(params.get('file', ''))
        if device=='local':
            if route!='preview':
                from reference_api import resource
                return resource(self, route, params)
            _, path = self.resolve(root, relative, exists=not bool(params.get('v')))
            if not params.get('v'):
                stat = path.stat()
                params = {**params, 'version': f'{stat.st_size}:{stat.st_mtime_ns}'}
            catalog=self.catalog(root)
            with catalog.connect() as connection:kind=connection.execute('SELECT media_type FROM items WHERE path=?',(str(path),)).fetchone()
            if kind and kind[0]=='video':params={**params,'poster_frame':catalog.video.frame}
            loader = lambda: self.preview(root, relative, params.get('size', 420))
        else:
            peer = self.peer(device)
            if route not in ('items', 'preview'):
                return self.remote(peer, route, params)
            loader = lambda: self.remote(peer, route, params)
        return self.cache.get_or_load(device, root, relative, route, params, loader,
                                      generation=query.get('cache_generation', self.cache.generation))

    def reveal_reference(self, device, root, relative):
        if device!='local':
            raise ValueError('API 参考图片不支持打开本机路径')
        _, path = self.resolve(root, relative)
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError('参考素材已移动或不是支持的文件')
        from file_reveal import reveal_path
        reveal_path(path)
        return {'ok': True}

    def sync_task(self, session_id, project_id):
        task = {'session_id': session_id or '', 'project_id': project_id or ''}
        with self.lock:
            previous = self.config.get('reference_task')
            changed = previous is not None and previous!=task
            if changed:
                self.cache.clear()
                preferences = self.config.get('preferences') or {}
                self.config['preferences'] = {**preferences, 'references': [], 'index': 0, 'initialized': True}
            self.config['reference_task'] = task
            self.save()
            return {'changed': changed, **self.preferences()}

    def clear_reference(self, reference, generation=None):
        if not all(isinstance(reference.get(key), str) and len(reference[key])<=32768 for key in ('device', 'root', 'relative')):
            raise ValueError('参考图片标识无效')
        with self.lock:
            if generation is not None and generation!=self.cache.generation:
                raise ValueError('拍摄任务已切换，旧参考图不能写回')
            result = self.cache.clear(reference=reference)
            saved = self.config.get('preferences') or {}
            remaining = [row for row in saved.get('references', []) if any(row[key]!=reference[key] for key in ('device', 'root', 'relative'))]
            self.config['preferences'] = {**saved, 'references': remaining, 'index': max(0, min(saved.get('index', 0), len(remaining)-1)), 'initialized': True}
            self.save()
            return {'ok': True, **result}

    def add_peer(self, name, address, token):
        name, address = str(name or '').strip()[:48], normalize_address(address)
        if not name or not isinstance(token, str) or not token.startswith('fk_') or len(token)>256:
            raise ValueError('请填写设备名称及对方创建的 API Key')
        row = {'id': secrets.token_hex(8), 'name': name, 'address': address, 'secret': protect(token)}
        result = self.remote(row, 'roots')
        if result.get('schema')!='framekeep.library' or result.get('version')!=1 or not isinstance(result.get('roots'), list):
            raise ValueError('对方不是兼容的末帧文件数据库')
        with self.lock:
            previous = next((peer for peer in self.config['peers'] if peer['address']==address), None)
            if previous:
                self.cache.clear(device=previous['id'])
                previous.update(name=name, secret=row['secret'])
                row = previous
            else:
                self.config['peers'].append(row)
            self.save()
        return {'id': row['id'], **result}

    def remove_peer(self, peer_id):
        with self.lock:
            self.cache.clear(device=peer_id)
            self.config['peers'] = [peer for peer in self.config['peers'] if peer['id']!=peer_id]
            self.save()
        return {'ok': True}

    def sources(self):
        devices = [{'id': 'local', 'name': '本机参考库', **self.roots(visible_only=True)}]
        with self.lock:
            peers = [dict(peer) for peer in self.config['peers']]
        futures = {self.pool.submit(self.remote, peer, 'roots', timeout=4): peer for peer in peers}
        done, pending = wait(futures, timeout=6)
        for future, peer in futures.items():
            try:
                if future not in done:
                    future.cancel()
                    raise ValueError('设备连接超时')
                result = future.result()
                devices.append({**result, 'id': peer['id'], 'name': peer['name'], 'remote': True})
            except (ValueError, OSError) as error:
                devices.append({'id': peer['id'], 'name': peer['name'], 'remote': True, 'roots': [], 'error': str(error)})
        return {'devices': devices}

    def preferences(self):
        with self.lock:
            saved = self.config['preferences'] or {'references': [], 'index': 0, 'collapsed': False, 'split_enabled': False, 'initialized': False}
            return {**saved, 'cache_generation': self.cache.generation}

    def save_preferences(self, body):
        references = body.get('references', [])
        if not isinstance(references, list) or len(references)>2000:
            raise ValueError('参考图配置过大')
        accepted, seen = [], set()
        for row in references:
            if not isinstance(row, dict) or not all(isinstance(row.get(key), str) and len(row[key])<=32768 for key in ('device', 'root', 'relative', 'name')):
                raise ValueError('参考图配置格式不正确')
            key = (row['device'], row['root'], row['relative'])
            if key not in seen:
                accepted.append({field: str(row.get(field, '')) for field in ('device', 'root', 'relative', 'name', 'source', 'fingerprint')})
                seen.add(key)
        result = {'references': accepted, 'index': max(0, min(len(accepted)-1, int(body.get('index', 0)))), 'collapsed': body.get('collapsed') is True, 'split_enabled': body.get('split_enabled') is True, 'initialized': True}
        with self.lock:
            if 'cache_generation' in body and body['cache_generation']!=self.cache.generation:
                raise ValueError('拍摄任务已切换，旧参考图不能写回')
            removed_files = 0
            for reference in (self.config.get('preferences') or {}).get('references', []):
                if (reference['device'], reference['root'], reference['relative']) not in seen:
                    removed_files += self.cache.clear(reference=reference)['removed_files']
            self.config['preferences'] = result
            self.save()
        return {'ok': True, 'removed_files': removed_files}

    def configure_sharing(self, enabled, port):
        if not isinstance(enabled, bool) or isinstance(port, bool) or not str(port).isdigit() or not 1024<=int(port)<=65535:
            raise ValueError('共享端口须为 1024–65535 的整数')
        port = int(port)
        if enabled and (self.shared is None or self.shared.server_port!=port):
            from reference_api import create_shared_server
            replacement = create_shared_server(self, port)
            previous, self.shared = self.shared, replacement
            if previous:
                previous.shutdown()
                previous.server_close()
        elif not enabled and self.shared:
            self.shared.shutdown()
            self.shared.server_close()
            self.shared = None
        with self.lock:
            self.config['sharing'] = {'enabled': enabled, 'port': port}
            self.sharing_error = ''
            self.save()
        return self.public_config()

    def public_config(self):
        port = self.config['sharing']['port']
        addresses = []
        with suppress(OSError):
            addresses = [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if any(ipaddress.ip_address(ip) in network for network in LAN_NETWORKS)]
        addresses.sort(key=lambda ip: (0 if ip.startswith('192.168.') else 1 if ip.startswith('10.') else 2, ip))
        endpoints = [f'http://{ip}:{port}{SUFFIX}' for ip in [*dict.fromkeys(addresses), '127.0.0.1']]
        storage=self.storage.state()
        with self.lock:
            return {'device': self.config['device'], 'roots': [dict(root) for root in self.config['roots'] if not root.get('removed')], 'keys': [{key: value for key, value in row.items() if key not in ('hash', 'secret')} for row in self.config['keys']], 'peers': [{key: value for key, value in row.items() if key!='secret'} for row in self.config['peers']], 'sharing': {**self.config['sharing'], 'running': self.shared is not None, 'error': self.sharing_error, 'addresses': endpoints}, 'endpoint': endpoints[0], 'index_storage':storage, 'everything': self.search.status() if self.search else {'available': False, 'enabled':False,'message': '使用本机目录数据库'}}

    def set_everything(self,enabled):
        if not self.search:raise ValueError('Everything 适配服务未启动')
        self.search.set_enabled(enabled)
        if not enabled:
            with self.lock:
                roots=[row['id'] for row in self.config['roots'] if not row.get('removed') and not row.get('native_complete')]
            for root in roots:self.rescan(root)
        return self.public_config()

    def close(self):
        self.stopping.set()
        self.storage.close()
        with self.condition:
            self.condition.notify_all()
        if self.shared:
            self.shared.shutdown()
            self.shared.server_close()
            self.shared = None
        self.thread.join(timeout=5)
        self.watch_thread.join(timeout=5)
        self.index_pool.shutdown(wait=True,cancel_futures=True)
        self.delta_pool.shutdown(wait=True,cancel_futures=True)
        for catalog in set(self.catalogs.values()):catalog.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
