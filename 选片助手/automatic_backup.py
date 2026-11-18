"""元数据成功后的文件备份：本地持久队列、完整版本发布及失败接续。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import unquote, urlsplit
import uuid
import weakref

from file_safety import atomic_write, publish_new

_SERVICES = weakref.WeakValueDictionary()
DEFAULTS = {'enabled': False, 'folder': '', 'interval_seconds': 5}


def key(path):
    return os.path.normcase(os.path.abspath(path))


def folder_path(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if any(ord(c) < 32 for c in value):
        raise ValueError('备份路径不能包含控制字符')
    if '://' in value:
        url = urlsplit(value)
        if url.username or url.password or url.query or url.fragment:
            raise ValueError('路径中不能保存账号、密码或查询参数，请使用系统已授权的挂载目录')
        if url.scheme.lower() not in ('file', 'smb'):
            raise ValueError('此协议请先挂载为系统文件夹或映射盘，再选择挂载后的路径')
        if url.scheme.lower() == 'smb' and not url.netloc:
            raise ValueError('SMB 路径需要服务器及共享名')
        value = ('//' + url.netloc if url.netloc and url.netloc != 'localhost' else '') + unquote(url.path)
        if os.name == 'nt' and len(value) > 3 and value[0] == '/' and value[2] == ':':
            value = value[1:]
    path = Path(value)
    if not path.is_absolute() or os.name == 'nt' and str(path).startswith(('\\\\?\\', '\\\\.\\')):
        raise ValueError('请选择本地绝对路径、NAS 共享路径或映射盘文件夹')
    if os.name == 'nt' and ':' in str(path)[2:]:
        raise ValueError('备份目标必须是普通文件夹')
    return os.path.normpath(str(path))


def applied(data, source, journal):
    """只由 applied 档案触发；这里不读取照片、不访问备份盘。"""
    service = _SERVICES.get(key(data))
    if service and source:
        try:
            service.notify(source, journal)
        except Exception as exc:
            # 原图已经提交成功，备份故障不能把元数据回执改成失败。
            # applied 档案仍保留，后台历史核对会重新接入。
            service.error = '备份登记待重试：' + str(exc)


def dataset_changed(data, root):
    service = _SERVICES.get(key(data))
    if service:
        try:
            service.touch(root)
        except Exception as exc:
            service.error = '目录备份登记待重试：' + str(exc)


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class AutomaticBackup:
    def __init__(self, data, writer, *, roots=lambda: [], autostart=True):
        self.data = Path(data)
        self.writer, self.roots = writer, roots
        self.source_roots = tuple(str(p) for p in roots() if p)
        self.config_path = self.data / 'automatic-backup.json'
        self.db = self.data / 'automatic-backup.sqlite3'
        self.lock = threading.RLock()
        self.processing = threading.Lock()
        self.closed = threading.Event()
        self.error = self.active = ''
        self.progress = {}
        self.validated = {}
        self.config = {**DEFAULTS, **(json.loads(self.config_path.read_text('utf8')) if self.config_path.exists() else {})}
        self.next_sync = time.time() + self.config['interval_seconds']
        with self.connect() as c:
            c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS folder_jobs(root TEXT PRIMARY KEY,generation INTEGER DEFAULT 1,status TEXT DEFAULT 'queued',
 retry_at REAL DEFAULT 0,attempts INTEGER DEFAULT 0,reason TEXT DEFAULT '',output TEXT DEFAULT '',waiting INTEGER DEFAULT 0,updated REAL);
CREATE TABLE IF NOT EXISTS copied_files(destination TEXT,relative TEXT,sha TEXT,size INTEGER,PRIMARY KEY(destination,relative));
CREATE TABLE IF NOT EXISTS verified_files(source TEXT PRIMARY KEY,journal TEXT,stamp REAL);
CREATE TABLE IF NOT EXISTS transfer_staging(path TEXT PRIMARY KEY,destination TEXT,identity TEXT,created_ns TEXT);
''')
            if 'metadata_request' not in {r[1] for r in c.execute('PRAGMA table_info(folder_jobs)')}:
                c.execute("ALTER TABLE folder_jobs ADD COLUMN metadata_request TEXT NOT NULL DEFAULT ''")
            c.execute("UPDATE folder_jobs SET status='queued' WHERE status='running'")
        _SERVICES[key(self.data)] = self
        self.thread = threading.Thread(target=self._run, daemon=True, name='automatic-folder-backup')
        if autostart:
            self.thread.start()

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def configure(self, body):
        roots = self._read_roots()
        with self.lock:
            config = {**self.config, **{k: body[k] for k in DEFAULTS if k in body}}
            if type(config['enabled']) is not bool:
                raise ValueError('自动备份开关无效')
            if type(config['interval_seconds']) is not int or not 1 <= config['interval_seconds'] <= 86400:
                raise ValueError('同步间隔须为 1–86400 的整数，单位为秒')
            config['folder'] = folder_path(config['folder'])
            if config['enabled'] and not config['folder']:
                raise ValueError('请先选择备份文件夹')
            if config['folder']:
                target = Path(key(config['folder']))
                with self.connect() as c:
                    known = [r[0] for r in c.execute('SELECT root FROM folder_jobs')]
                for root in [self.data, *roots, *known]:
                    source = Path(key(root))
                    if target == source or target.is_relative_to(source) or (target / source.name) == source:
                        raise ValueError('备份目标不能位于拍摄源目录或软件资料目录内，也不能指向源目录的上级')
            atomic_write(self.config_path, json.dumps(config, ensure_ascii=False).encode('utf8'))
            changed = config['folder'] != self.config['folder']
            self.config = config
            if not self.active:
                self.progress = {}
            self.next_sync = time.time() + config['interval_seconds']
            with self.connect() as c:
                c.execute("UPDATE folder_jobs SET status='queued',generation=generation+1,retry_at=0,reason='',output=CASE WHEN ? THEN '' ELSE output END", (changed,))
        for root in roots:
            self.touch(root)
        return self.state()

    def _read_roots(self):
        # 先读监控状态再进入备份锁；写入回执只读此缓存，避免反向等待监控／数据集锁。
        roots = tuple(str(p) for p in self.roots() if p)
        self.source_roots = roots
        return roots

    def touch(self, root, *, changed=True):
        root = key(root)
        # 只接受现场监控根目录；浏览历史归档或修改其它位置不会自动外传。
        allowed = {key(p) for p in self.source_roots}
        with self.lock, self.connect() as c:
            if not self.config['enabled'] or root not in allowed:
                return
            c.execute('''INSERT INTO folder_jobs(root,updated) VALUES(?,?) ON CONFLICT(root) DO UPDATE SET
 generation=generation+?,status=CASE WHEN status='running' THEN status ELSE 'queued' END,
 retry_at=CASE WHEN ? THEN 0 ELSE retry_at END,updated=CASE WHEN ? THEN excluded.updated ELSE updated END''',
                      (root, time.time(), int(changed), changed, changed))

    def notify(self, source, journal):
        source = key(source)
        candidates = [Path(key(p)) for p in self.source_roots if Path(source).is_relative_to(Path(key(p)))]
        if not candidates:
            return
        root = max(candidates, key=lambda p: len(p.parts))
        # 成功回执仅存本机库，不探测照片或目标共享盘。
        payload = json.dumps({k: journal[k] for k in ('target', 'after_hash') if k in journal})
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO verified_files VALUES(?,?,?)', (source, payload, time.time()))
        self.touch(root)

    @staticmethod
    def _ordinary(path):
        if path.is_symlink() or path.is_junction():
            raise ValueError('路径存在链接／重解析点，未访问：' + str(path))

    def _parent(self, root, relative):
        path = root
        self._ordinary(path)
        for part in Path(relative).parts[:-1]:
            path /= part
            self._ordinary(path)
            path.mkdir(exist_ok=True)
        return root / relative

    def _check_enabled(self, destination):
        if self.closed.is_set() or not self.config['enabled'] or key(self.config['folder']) != key(destination.parent):
            raise InterruptedError('备份已暂停或目标已改变，待处理记录保留')

    def _progress(self, **values):
        # 状态接口只读取内存快照；进度锁内不做文件或网络盘 I/O。
        with self.lock:
            self.progress.update(values)
            self.progress['updated_at'] = time.time()

    def _hash(self, path, phase):
        self._progress(phase=phase, stage_bytes=0, stage_total=path.stat().st_size)
        checksum = hashlib.sha256()
        done, reported = 0, time.monotonic()
        with path.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                checksum.update(block)
                done += len(block)
                now = time.monotonic()
                if now - reported >= .1:
                    self._progress(stage_bytes=done)
                    reported = now
        self._progress(stage_bytes=done)
        return checksum.hexdigest()

    def _copy(self, source, target, previous, destination):
        """仅替换校验一致的受管副本，未知同名文件及外部修改保持原样。"""
        self._ordinary(source)
        self._ordinary(target)
        before = source.stat()
        expected = self._hash(source, 'checking_source')
        current = self._hash(target, 'checking_target') if target.exists() else None
        if current == expected:
            return expected, before.st_size
        if current is not None and current != previous:
            raise ValueError('目标同名文件已被其它用户修改，未覆盖：' + str(target))
        temporary = target.parent / ('.' + target.name + '.' + uuid.uuid4().hex + '.writing')
        try:
            with source.open('rb') as stream, temporary.open('xb') as out:
                stat = os.fstat(out.fileno())
                with self.connect() as c:
                    c.execute('INSERT OR REPLACE INTO transfer_staging VALUES(?,?,?,?)', (str(temporary), key(destination), str(stat.st_ino), str(stat.st_ctime_ns)))
                self._progress(phase='copying', stage_bytes=0, stage_total=before.st_size)
                transferred, reported = 0, time.monotonic()
                while block := stream.read(1024 * 1024):
                    self._check_enabled(destination)
                    out.write(block)
                    transferred += len(block)
                    now = time.monotonic()
                    if now - reported >= .1:
                        self._progress(stage_bytes=transferred)
                        reported = now
                out.flush()
                os.fsync(out.fileno())
            self._progress(stage_bytes=transferred)
            if self._hash(temporary, 'verifying_copy') != expected or self._hash(source, 'verifying_source') != expected:
                raise OSError('源文件变化或传输校验失败，未发布：' + str(source))
            self._check_enabled(destination)
            if (self._hash(target, 'checking_target') if target.exists() else None) != current:
                raise ValueError('提交前目标发生变化，未覆盖：' + str(target))
            os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
            self._progress(phase='publishing_file')
            if current is None:
                publish_new(temporary, target)
            else:
                os.replace(temporary, target)
            return expected, before.st_size
        finally:
            try:
                temporary.unlink(missing_ok=True)
                with self.connect() as c:
                    c.execute('DELETE FROM transfer_staging WHERE path=?', (str(temporary),))
            except OSError:
                pass

    def _cleanup_staging(self, destination):
        with self.connect() as c:
            entries = c.execute('SELECT * FROM transfer_staging WHERE destination=?', (key(destination),)).fetchall()
        for entry in entries:
            path = Path(entry['path'])
            if not path.is_relative_to(destination) or not path.name.startswith('.') or not path.name.endswith('.writing'):
                continue
            for part in (path, *path.parents):
                if part == destination.parent:
                    break
                self._ordinary(part)
            if path.exists():
                stat = path.stat()
                if str(stat.st_ino) == entry['identity'] and str(stat.st_ctime_ns) == entry['created_ns']:
                    path.unlink()
            with self.connect() as c:
                c.execute('DELETE FROM transfer_staging WHERE path=?', (str(path),))

    def _source_dataset(self, root):
        capture = getattr(self.writer, 'capture_service', None)
        dataset = capture.dataset_for_path(root / '__backup_probe__') if capture else None
        # 重启／另一台安装的本机工作库可能尚未加载；磁盘随行档案才是交付依据。
        if capture and (root / '.shiying/dataset.json').is_file() and (not dataset or key(dataset.root) != key(root)):
            dataset = capture._dataset(root)
        if dataset and not dataset.editing:
            dataset.refresh()
        return dataset

    def _prepare_metadata(self, root):
        """仅由立即同步触发，在备份后台补齐当前确认版本，沿用原有写入保护。"""
        capture = getattr(self.writer, 'capture_service', None)
        if not capture:
            raise OSError('拍摄元数据服务正在启动，稍后自动补写并同步')
        dataset = self._source_dataset(root)
        if dataset and (root / '.shiying/dataset.json').is_file():
            records = dataset.records_snapshot()
            with dataset.connect() as c:
                failed_receipt = c.execute("""SELECT 1 FROM tasks t JOIN records r ON r.id=t.record
 WHERE t.kind='metadata_sidecar' AND t.status='failed' AND t.revision=json_extract(r.data,'$.decision_id') LIMIT 1""").fetchone()
                unpublished = c.execute('SELECT 1 FROM records r LEFT JOIN synced_records s ON s.id=r.id WHERE s.id IS NULL LIMIT 1').fetchone()
            if records and all(r.get('write_state') in ('verified', 'sku_verified') for r in records) and not failed_receipt and not unpublished:
                # 全部已回读时只备份，不为手动同步额外取得源目录编辑权。
                return
        if capture.readonly():
            raise OSError('正在单向接收，无法补写元数据，请断开同步后重试')
        if not dataset or not (root / '.shiying/dataset.json').is_file():
            session = next((s for s in capture.store.sessions() if key(s['root']) == key(root)), None)
            if session:
                capture.prepare_live(session['id'])
                dataset = self._source_dataset(root)
        if not dataset:
            with capture.catalog.connect() as c:
                registered = c.execute('SELECT 1 FROM roots WHERE path=? COLLATE NOCASE AND removed=0', (str(root),)).fetchone()
            if registered:
                dataset = capture._dataset(root)
        if not dataset:
            raise OSError('未找到现场拍摄会话，无法补写元数据，请重新开启此目录的现场监控')
        records = dataset.records_snapshot()
        # 在备份线程完成首次读取，不依赖用户打开归档页，也不切换当前浏览目录。
        if not records or capture.loading.get(str(dataset.root)):
            if not capture.scan_backup_metadata(dataset):
                raise OSError('正在读取拍摄元数据，完成后自动补写并同步')
        with capture.catalog.connect() as c:
            registered = [dict(r) for r in c.execute('SELECT id,path FROM roots WHERE removed=0')]
        candidates = [r for r in registered if Path(key(root)).is_relative_to(Path(key(r['path'])))]
        if not candidates:
            raise OSError('拍摄目录尚未登记，等待现场监控恢复后自动补写并同步')
        selected = max(candidates, key=lambda r: len(Path(r['path']).parts))
        folder = root.relative_to(Path(selected['path'])).as_posix()
        with capture.io_lock:
            result = capture.write_metadata(selected['id'], '' if folder == '.' else folder, activate=False)
            # 已写照片也可能有失败的摘要刷新任务；补写入口会跳过 verified，须单独接续回执。
            scoped, _ = capture._scope_records(dataset, root)
            with dataset.connect() as c:
                c.executemany("""UPDATE tasks SET status='queued',reason='',data=json_remove(data,'$._retry_at','$._retry_count')
 WHERE kind='metadata_sidecar' AND status='failed' AND record=? AND revision=?""",
                              [(r['id'], r.get('decision_id', '')) for r in scoped if r.get('available') and not r.get('metadata_conflict') and not r.get('identity_conflict')])
                recovered = [json.loads(r[0]) for r in c.execute('SELECT r.data FROM records r LEFT JOIN synced_records s ON s.id=r.id WHERE s.id IS NULL ORDER BY r.path')]
            # 从照片恢复的只读记录还只在本机库；分批发布为随行事件，才能被另一安装恢复。
            if recovered:
                for start in range(0, len(recovered), 128):
                    dataset.change(recovered[start:start+128], kind='backup_metadata_recovery')
                groups = dataset.groups()
                for start in range(0, len(groups), 128):
                    dataset.change(groups=groups[start:start+128], kind='backup_metadata_recovery')
        capture.metadata_wake.set()
        if result.get('skipped'):
            raise OSError(f"{result['skipped']} 张照片存在冲突或离线，未覆盖；处理后重试同步")

    def _dataset(self, root):
        dataset = self._source_dataset(root)
        if not dataset or not (root / '.shiying/dataset.json').is_file():
            raise OSError('现场随行元数据尚未就绪，等待拍摄元数据写入完成')
        # 不获取源目录编辑权，不触发识别，不复制活动本机 SQLite／WAL。
        with dataset.connect() as c:
            c.execute('BEGIN')
            records = [json.loads(r[0]) for r in c.execute('SELECT data FROM records ORDER BY path')]
            pending = {r[0] for r in c.execute("""SELECT DISTINCT t.record FROM tasks t JOIN records r ON r.id=t.record
 WHERE t.kind IN ('metadata','metadata_fill','metadata_sidecar') AND t.status IN ('queued','running','waiting_native','failed')
 AND t.revision=json_extract(r.data,'$.decision_id')
 AND (t.status!='failed' OR t.kind='metadata_sidecar' OR COALESCE(json_extract(r.data,'$.write_state'),'') NOT IN ('verified','sku_verified'))""")}
            waiting = len(pending | {r['id'] for r in records if r.get('sku') and r.get('write_state') not in ('verified', 'sku_verified')})
            self._progress(waiting=waiting)
            with self.connect() as backup_db:
                backup_db.execute('UPDATE folder_jobs SET waiting=? WHERE root=?', (waiting, key(root)))
            if c.execute("SELECT 1 FROM events WHERE status='pending' LIMIT 1").fetchone():
                raise OSError('随行元数据尚未发布完成，等待自动重试')
            if pending:
                raise OSError('照片元数据仍在写入或等待处理，完成后自动备份')
            revision = int(c.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
        if any(r.get('sku') and r.get('write_state') not in ('verified', 'sku_verified') for r in records):
            raise OSError('款号文件尚未全部回读验证，完成后自动备份')
        return dataset, records, revision

    @staticmethod
    def _unchanged(dataset, revision):
        with dataset.connect() as c:
            if int(c.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]) != revision or c.execute("SELECT 1 FROM events WHERE status='pending' LIMIT 1").fetchone():
                raise OSError('拍摄资料已更新，等待下一轮同步最新完整修订')

    @staticmethod
    def _signature(path):
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino

    def _files(self, root, records):
        import media_engine as media
        from asset_catalog import VIDEO_EXTENSIONS
        verified = {str(r.get('relative_path') or r.get('path')).casefold() for r in records if r.get('write_state') in ('verified', 'sku_verified')}
        sidecars = {}
        for r in records:
            path = Path(str(r.get('relative_path') or r['path']))
            for side in (path.with_suffix('.xmp'), Path(str(path) + '.xmp')):
                sidecars.setdefault(side.as_posix().casefold(), []).append(path.as_posix().casefold())
        ready = []
        waiting = 0
        scanned, reported = 0, time.monotonic()
        # 记录本次全部相对路径；不跟随源目录中的链接或联接。
        for directory, dirs, files in os.walk(root, followlinks=False):
            parent = Path(directory)
            for name in list(dirs):
                self._ordinary(parent / name)
            for name in files:
                scanned += 1
                if time.monotonic() - reported >= .1:
                    self._progress(scanned_files=scanned, waiting=waiting)
                    reported = time.monotonic()
                path = parent / name
                self._ordinary(path)
                relative = path.relative_to(root).as_posix()
                if relative.startswith('.shiying/') and (name == 'write.lock' or name.startswith('owner-')):
                    continue
                if name.endswith(('.writing', '-wal', '-shm')) or name == '.shiying-backup.json':
                    continue
                if path.suffix.lower() in media.IMAGE_EXTS | media.RAW_EXTS | VIDEO_EXTENSIONS:
                    if relative.casefold() not in verified:
                        waiting += 1
                        continue
                elif path.suffix.lower() == '.xmp':
                    # 未完成照片的侧车不能抢先发布。
                    candidates = sidecars.get(relative.casefold(), [])
                    if candidates and any(p not in verified for p in candidates):
                        continue
                ready.append(relative)
        # 依赖内容先到，提交标记最后到；读者继续沿用上一完整随行修订。
        def order(relative):
            if relative.startswith('.shiying/commits/'):
                return 3, relative
            if relative.startswith('.shiying/snapshot-commits/'):
                return 4, relative
            if relative.startswith('.shiying/'):
                return 2, relative
            return 1, relative
        self._progress(scanned_files=scanned, waiting=waiting)
        return sorted(ready, key=order), waiting

    def _refresh_receipt(self, dataset, record, acknowledgement, checksum):
        """手动标题等写入后复用现有只校验任务，刷新随行文件回执，不重新识别。"""
        evidence = json.loads(acknowledgement) if acknowledgement else {}
        source = dataset.root / str(record.get('relative_path') or record['path'])
        capture = getattr(self.writer, 'capture_service', None)
        archive = getattr(capture, 'archive', None)
        if archive and (not evidence or evidence.get('after_hash') != checksum):
            # 首次开启／关闭期间手动写入也可从原有成功任务恢复回执。
            with archive.connect() as c:
                row = c.execute("SELECT journal FROM metadata_jobs WHERE path=? COLLATE NOCASE AND status='applied' ORDER BY created DESC LIMIT 1", (str(source),)).fetchone()
            if row:
                evidence = json.loads(row[0])
        if not dataset.editing or key(evidence.get('target', '')) != key(source) or evidence.get('after_hash') != checksum:
            return
        task_id = uuid.uuid5(uuid.NAMESPACE_URL, str(source) + ':' + record.get('decision_id', '') + ':' + checksum).hex
        with dataset.connect() as c:
            c.execute("""INSERT INTO tasks VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
 status='queued',reason='',data='{}' WHERE tasks.status NOT IN ('queued','running')""",
                      (task_id, 'metadata_sidecar', record['id'], record.get('decision_id', ''), 'queued', '', '{}'))
        if getattr(capture, 'metadata_wake', None):
            capture.metadata_wake.set()

    def _transfer(self, job, folder):
        root = Path(job['root'])
        self._ordinary(root)
        if not root.is_dir():
            raise OSError('现场拍摄目录离线，等待恢复')
        self._progress(phase='checking_metadata')
        dataset, records, revision = self._dataset(root)
        self._progress(phase='scanning')
        relatives, waiting = self._files(root, records)
        sizes = {relative: (root / relative).stat().st_size for relative in relatives}
        self._progress(phase='connecting', total_files=len(relatives), total_bytes=sum(sizes.values()),
                       done_files=0, done_bytes=0, waiting=waiting)
        with self.connect() as c:
            c.execute('UPDATE folder_jobs SET waiting=? WHERE root=?', (waiting, key(root)))
        folder = Path(folder)
        if not folder.is_dir():
            raise OSError('备份文件夹离线或不存在，请连接目标盘／NAS')
        destination = folder / root.name
        # resolve 与 samefile 同时覆盖映射盘／UNC 的同一目录别名。
        actual = destination.resolve()
        if actual == root.resolve() or actual.is_relative_to(root.resolve()) or destination.exists() and os.path.samefile(root, destination):
            raise ValueError('备份目录不能位于现场拍摄目录内')
        self._ordinary(destination)
        marker = destination / '.shiying-backup.json'
        if destination.exists() and not marker.exists() and any(destination.iterdir()):
            raise ValueError('目标已有同名文件夹且不是本功能创建的备份，请更换目标；原内容已保留')
        destination.mkdir(exist_ok=True)
        self._ordinary(marker)
        identity = {'schema': 'shiying.folder-backup.v1', 'dataset_id': dataset.dataset_id, 'source': str(root), 'files': {}}
        if marker.exists():
            identity = json.loads(marker.read_text('utf8'))
            if identity.get('schema') != 'shiying.folder-backup.v1' or identity.get('dataset_id') != dataset.dataset_id:
                raise ValueError('目标同名文件夹属于其它拍摄数据集，未覆盖')
        from portable_capture_store import WriteOwnership
        ownership = WriteOwnership(destination)
        ownership.acquire()
        try:
            self._cleanup_staging(destination)
            # 首次占用信息不继承自源盘；它属于当前备份进程的真实 OS 锁。
            if not marker.exists():
                payload = json.dumps(identity, ensure_ascii=False).encode('utf8')
                from portable_capture_store import publish_file
                publish_file(marker, payload)
            with self.connect() as c:
                copied = {r['relative']: r['sha'] for r in c.execute('SELECT relative,sha FROM copied_files WHERE destination=?', (key(destination),))}
                acknowledgements = {r['source']: r['journal'] for r in c.execute('SELECT source,journal FROM verified_files')}
            copied = {**identity.get('files', {}), **copied}
            source_hashes = {str(r.get('relative_path') or r['path']): r.get('file_sha256') for r in records if r.get('file_sha256') and r.get('write_state') == 'verified'}
            records_by_path = {str(r.get('relative_path') or r['path']): r for r in records}
            done_bytes = 0
            for index, relative in enumerate(relatives):
                self._progress(current_file=relative, phase='checking_source', stage_bytes=0, stage_total=sizes[relative])
                self._check_enabled(destination)
                ownership.check()
                # 新版本提交标记只在媒体与依赖齐全、源修订仍稳定时发布。
                if relative.startswith(('.shiying/commits/', '.shiying/snapshot-commits/')):
                    self._unchanged(dataset, revision)
                source = root / relative
                target = self._parent(destination, relative)
                with self.writer.file_lock(source, timeout=.2):
                    cache_key = (key(destination), relative)
                    version = (self._signature(source), self._signature(target) if target.exists() else None,
                               source_hashes.get(relative), acknowledgements.get(key(source)))
                    # 已校验版本只核对属性与成功写入回执；修改、重启及手动重试重新做完整 SHA-256。
                    if self.validated.get(cache_key) == version:
                        done_bytes += sizes[relative]
                        self._progress(done_files=index + 1, done_bytes=done_bytes)
                        continue
                    record = records_by_path.get(relative, {})
                    if record.get('write_state') == 'sku_verified' and record.get('signature') and tuple(record['signature']) != version[0][:2]:
                        raise OSError('照片已变化，等待最新元数据回读校验：' + relative)
                    if relative in source_hashes:
                        checksum = self._hash(source, 'checking_source')
                        if checksum != source_hashes[relative]:
                            self._refresh_receipt(dataset, records_by_path[relative], acknowledgements.get(key(source)), checksum)
                            raise OSError('照片已变化，等待最新元数据回读校验：' + relative)
                    checksum, size = self._copy(source, target, copied.get(relative), destination)
                    self.validated[cache_key] = (self._signature(source), self._signature(target),
                                                 source_hashes.get(relative), acknowledgements.get(key(source)))
                copied[relative] = checksum
                with self.connect() as c:
                    c.execute('INSERT OR REPLACE INTO copied_files VALUES(?,?,?,?)', (key(destination), relative, checksum, size))
                done_bytes += sizes[relative]
                self._progress(done_files=index + 1, done_bytes=done_bytes)
            self._progress(phase='publishing', current_file='', stage_bytes=0, stage_total=0)
            _, _, latest = self._dataset(root)
            if latest != revision:
                raise OSError('拍摄期间资料已更新，下一轮自动补齐')
            identity.update(files=copied, source=str(root), revision=revision, updated=time.time())
            atomic_write(marker, json.dumps(identity, ensure_ascii=False).encode('utf8'))
            return str(destination), waiting
        finally:
            ownership.release()

    def process_one(self, *, force=False):
        if not self.processing.acquire(blocking=False):
            return False
        job = None
        try:
            with self.lock:
                if not self.config['enabled'] or not force and time.time() < self.next_sync:
                    return False
                folder = self.config['folder']
                with self.connect() as c:
                    row = c.execute("SELECT * FROM folder_jobs WHERE status='queued' AND retry_at<=? ORDER BY updated LIMIT 1", (time.time(),)).fetchone()
                    if not row:
                        return False
                    job = dict(row)
                    c.execute("UPDATE folder_jobs SET status='running' WHERE root=?", (job['root'],))
                self.active = job['root']
                self.progress = {'source': job['root'], 'phase': 'preparing', 'started_at': time.time(),
                                 'done_files': 0, 'done_bytes': 0, 'total_files': None, 'total_bytes': None,
                                 'current_file': '', 'stage_bytes': 0, 'stage_total': 0, 'waiting': 0, 'scanned_files': 0}
            if job['metadata_request']:
                self._progress(phase='preparing_metadata')
                self._prepare_metadata(Path(job['root']))
                with self.connect() as c:
                    c.execute("UPDATE folder_jobs SET metadata_request='' WHERE root=? AND metadata_request=?", (job['root'], job['metadata_request']))
            output, waiting = self._transfer(job, folder)
            with self.lock, self.connect() as c:
                changed = folder != self.config['folder']
                c.execute("UPDATE folder_jobs SET status=CASE WHEN generation=? AND ?=0 THEN 'complete' ELSE 'queued' END,output=?,waiting=?,reason='',attempts=0 WHERE root=?", (job['generation'], changed, '' if changed else output, waiting, job['root']))
            self.error = ''
            self._progress(phase='complete', finished_at=time.time(), waiting=waiting)
            return True
        except Exception as exc:
            self.error = str(exc)
            if job:
                attempts = job['attempts'] + 1
                delay = min(300, 2 ** min(attempts, 8))
                with self.connect() as c:
                    c.execute("UPDATE folder_jobs SET status='queued',attempts=?,reason=?,retry_at=? WHERE root=?", (attempts, str(exc)[:1000], time.time() + delay, job['root']))
                self._progress(phase='retrying', failed_phase=self.progress.get('phase'), finished_at=time.time())
            return False
        finally:
            self.active = ''
            self.next_sync = time.time() + self.config['interval_seconds']
            self.processing.release()

    def retry(self):
        self.validated.clear()
        roots = self._read_roots()
        for root in roots:
            self.touch(root)
        with self.connect() as c:
            c.execute("UPDATE folder_jobs SET status='queued',retry_at=0,reason='',metadata_request=CASE WHEN root IN (SELECT value FROM json_each(?)) THEN ? ELSE metadata_request END",
                      (json.dumps([key(root) for root in roots]), uuid.uuid4().hex))
        self.error = ''
        with self.lock:
            if not self.active:
                self.progress = {}
        self.next_sync = 0
        return self.state()

    def state(self):
        sources = self._read_roots()
        with self.lock, self.connect() as c:
            jobs = [dict(r) for r in c.execute('SELECT * FROM folder_jobs ORDER BY updated DESC')]
            outputs = [key(r['output']) for r in jobs if r['output']]
            count = c.execute('SELECT count(*) FROM copied_files WHERE destination IN (SELECT value FROM json_each(?))', (json.dumps(outputs),)).fetchone()[0]
            progress = dict(self.progress)
            if progress:
                progress['elapsed_seconds'] = max(0, progress.get('finished_at', time.time()) - progress['started_at'])
            retry_at = min((max(r['retry_at'], self.next_sync) for r in jobs if r['status'] == 'queued'), default=0)
            return {'config': dict(self.config), 'pending': sum(r['status'] != 'complete' for r in jobs), 'completed': sum(r['status'] == 'complete' for r in jobs),
                    'waiting': sum(r['waiting'] for r in jobs), 'copied_files': count, 'retrying': sum(bool(r['reason']) for r in jobs),
                    'active': self.active, 'issues': [{'source': r['root'], 'reason': r['reason']} for r in jobs if r['reason']][:20],
                    'sources': list(sources), 'error': self.error,
                    'last_output': next((r['output'] for r in jobs if r['output']), ''), 'next_sync': self.next_sync,
                    'retry_at': retry_at, 'progress': progress}

    def _run(self):
        while not self.closed.wait(.5):
            try:
                if not self.config['enabled'] or time.time() < self.next_sync:
                    continue
                # 周期核对同时补接进程退出窗口及未生成文件写入事件的区间／元数据变化。
                for root in self._read_roots():
                    self.touch(root, changed=False)
                self.process_one()
            except Exception as exc:
                self.error = str(exc)
                self.closed.wait(2)

    def close(self):
        self.closed.set()
        if self.thread.is_alive():
            self.thread.join(2)
        if _SERVICES.get(key(self.data)) is self:
            _SERVICES.pop(key(self.data), None)
