"""本机事务、目录随行事件与快照；共享盘不打开活动 SQLite 工作库。"""
from contextlib import contextmanager, closing, nullcontext
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import socket
import sqlite3
import threading
import time
import uuid

SCHEMA = 1
MAX_EVENT = 32 * 1024 * 1024
BUSINESS_FIELDS=('sku','base_sku','parent_sku','shoot_version','rating','boundary','role','group_id','catalog_scope','customer','brand',
                 'assignment_state','assignment_source','group_revision','expected_products','expected_labels','expected_files',
                 'members_digest','files_digest','shoot_type','shoot_date')


def historical(record):
    return {key:record[key] for key in ('id','asset_id','file_id','decision_id',*BUSINESS_FIELDS) if key in record}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def relative_path(value):
    value = str(value).replace('\\', '/')
    p = PurePosixPath(value)
    if not value or value.startswith('/') or ':' in value or any(x in ('', '.', '..') for x in value.split('/')):
        raise ValueError('无效的档案相对路径')
    return p.as_posix()


def resolve_under(root, value, *, verify_links=True):
    root = Path(root).resolve() if verify_links else Path(root).absolute()
    path = root.joinpath(*PurePosixPath(relative_path(value)).parts)
    if not verify_links:
        if not path.is_relative_to(root):raise ValueError('档案引用超出数据集')
        return path
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if part.is_symlink() or part.is_junction():
            raise ValueError('档案引用包含链接，未越界访问')
    if not path.resolve().is_relative_to(root):
        raise ValueError('档案引用超出数据集')
    return path


def read_json(path):
    path = Path(path)
    if path.is_symlink() or path.is_junction() or path.stat().st_size > MAX_EVENT:
        raise ValueError('随行档案无效或超过大小限制')
    return json.loads(path.read_bytes())


def publish_file(path, content):
    """唯一完成名；调用方只在内容关闭并回读一致后发布引用它的提交。"""
    path = Path(path)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError('已有提交内容冲突，未覆盖')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.writing')
    try:
        with temporary.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_bytes() != content:
            raise OSError('随行记录回读不一致')
        # 不复用另一写入者的文件名；有完整依赖的 commit 才可被读取。
        temporary.rename(path)
    finally:
        temporary.unlink(missing_ok=True)


class DatasetBusy(ValueError):
    pass


class DatasetConnectionBusy(DatasetBusy):
    retryable_metadata = True


class WriteOwnership:
    """整个编辑会话持有 OS/SMB 服务端句柄，禁止心跳超时强制抢占。"""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.folder = self.root / '.shiying'
        self.handle = None
        self.session = uuid.uuid4().hex

    def acquire(self):
        if self.handle is not None:
            self.check()
            return
        self.session = uuid.uuid4().hex
        if self.folder.is_symlink() or self.folder.is_junction():
            raise ValueError('随行目录不能是链接')
        self.folder.mkdir(exist_ok=True)
        path = self.folder / 'write.lock'
        if path.is_symlink() or path.is_junction():
            raise ValueError('占用文件不能是链接')
        if os.name == 'nt':
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            kernel.CreateFileW.restype = wintypes.HANDLE
            handle = kernel.CreateFileW(str(path), 0xC0000000, 1, None, 4, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                error = ctypes.get_last_error()
                if error in (32, 33):
                    raise DatasetConnectionBusy('此数据集正在由其它客户端编辑，请等待正常交接')
                raise ctypes.WinError(error)
            self.handle = handle
            self.kernel = kernel
        else:
            import fcntl
            stream = path.open('a+b')
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                stream.close()
                raise DatasetConnectionBusy('此数据集正在由其它客户端编辑')
            self.handle = stream
        try:
            owner = {'device': socket.gethostname(), 'session': self.session, 'pid': os.getpid(), 'since': time.time()}
            temporary = self.folder / ('owner-' + self.session + '.json')
            publish_file(temporary, encoded(owner))
            self.check()
        except BaseException:
            self.release()
            raise

    def check(self):
        if self.handle is None:
            raise DatasetBusy('请先取得当前数据集的编辑权限')
        if os.name == 'nt':
            from ctypes import wintypes
            self.kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
            self.kernel.FlushFileBuffers.restype = wintypes.BOOL
            if not self.kernel.FlushFileBuffers(self.handle):
                raise DatasetConnectionBusy('数据集写入连接已失效，等待自动重新取得编辑权限')
        else:
            os.fsync(self.handle.fileno())

    def release(self):
        if self.handle is not None:
            if os.name == 'nt':
                from ctypes import wintypes
                self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                self.kernel.CloseHandle(self.handle)
            else:
                self.handle.close()
            self.handle = None


class Dataset:
    def __init__(self, root, local_data):
        self.root = Path(root).resolve(strict=True)
        self.folder = self.root / '.shiying'
        self.local = Path(local_data) / 'portable-capture'
        self.local.mkdir(parents=True, exist_ok=True)
        self.db = self.local / (digest(str(self.root).casefold().encode()) + '.sqlite3')
        self.lock = threading.RLock()
        self.ownership = WriteOwnership(self.root)
        self.last_error = ''
        self.remote_revision = 0
        self.last_snapshot_at = 0
        self.imported_records=[]
        # 只读展示接口的记录快照：触发器维护的版本变化才重新解码。
        self._snapshot_lock=threading.Lock();self._snapshot=(None,[])
        self._publish_state=threading.local()
        # 发布可能同时来自交互请求（锁外）与后台调度；串行后后到者只看到已发布事件。
        self._publish_lock=threading.Lock()
        with self.connect() as c:
            c.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,path TEXT UNIQUE,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS portable_file_id ON records(json_extract(data,'$.file_id'));
CREATE INDEX IF NOT EXISTS portable_record_path_nocase ON records(path COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS records_version(id INTEGER PRIMARY KEY CHECK(id=1),value INTEGER NOT NULL);
INSERT OR IGNORE INTO records_version VALUES(1,0);
CREATE TRIGGER IF NOT EXISTS portable_records_version_insert AFTER INSERT ON records BEGIN UPDATE records_version SET value=value+1 WHERE id=1; END;
CREATE TRIGGER IF NOT EXISTS portable_records_version_update AFTER UPDATE ON records BEGIN UPDATE records_version SET value=value+1 WHERE id=1; END;
CREATE TRIGGER IF NOT EXISTS portable_records_version_delete AFTER DELETE ON records BEGIN UPDATE records_version SET value=value+1 WHERE id=1; END;
CREATE TABLE IF NOT EXISTS synced_records(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS groups_(id TEXT PRIMARY KEY,data TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS portable_groups_version_insert AFTER INSERT ON groups_ BEGIN UPDATE records_version SET value=value+1 WHERE id=1; END;
CREATE TRIGGER IF NOT EXISTS portable_groups_version_update AFTER UPDATE ON groups_ BEGIN UPDATE records_version SET value=value+1 WHERE id=1; END;
CREATE TRIGGER IF NOT EXISTS portable_groups_version_delete AFTER DELETE ON groups_ BEGIN UPDATE records_version SET value=value+1 WHERE id=1; END;
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,revision INTEGER UNIQUE,payload TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,kind TEXT,record TEXT,revision TEXT,status TEXT,reason TEXT DEFAULT '',data TEXT DEFAULT '{}');
CREATE INDEX IF NOT EXISTS portable_task_record_state ON tasks(record,status,kind,revision);
CREATE INDEX IF NOT EXISTS portable_task_ready_order ON tasks(status,CASE kind WHEN 'ocr_now' THEN 0 WHEN 'ocr_read' THEN 0 WHEN 'ocr_apply' THEN 1 WHEN 'ocr' THEN 2 ELSE 3 END);
CREATE TABLE IF NOT EXISTS history(id TEXT PRIMARY KEY,kind TEXT,before_data TEXT,after_data TEXT,created REAL);
CREATE TABLE IF NOT EXISTS history_context(id TEXT PRIMARY KEY,data TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS portable_record_count_insert AFTER INSERT ON records BEGIN INSERT INTO meta(key,value) VALUES('record_count','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1; END;
CREATE TRIGGER IF NOT EXISTS portable_record_count_delete AFTER DELETE ON records BEGIN UPDATE meta SET value=CAST(value AS INTEGER)-1 WHERE key='record_count'; END;
''')
            c.execute("INSERT OR IGNORE INTO meta VALUES('revision','0')")
            c.execute("INSERT OR IGNORE INTO meta VALUES('schema','1')")
            c.execute("INSERT OR IGNORE INTO meta SELECT 'record_count',CAST(COUNT(*) AS TEXT) FROM records")
            if 'revision' not in {r[1] for r in c.execute('PRAGMA table_info(history)')}:
                c.execute('ALTER TABLE history ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def meta(self, key, default=''):
        with self.connect() as c:
            row = c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    @property
    def dataset_id(self):
        return self.meta('dataset_id')

    @property
    def editing(self):
        return self.ownership.handle is not None

    def ensure_ownership(self):
        """只重连本客户端曾取得的编辑会话，重新取系统锁并同步版本后才能写入。"""
        with self.lock:
            try:
                self.ownership.check()
                return
            except DatasetBusy:
                if not self.editing and self.meta('was_editing','0')!='1':raise
            # 只关闭自身失效句柄；其它客户端持锁时 acquire 仍会拒绝，不删锁文件。
            self.ownership.release()
            self.acquire()

    def acquire(self):
        with self.lock:
            self.ownership.acquire()
            try:
                descriptor = self.folder / 'dataset.json'
                if not descriptor.exists():
                    publish_file(descriptor, encoded({'schema': SCHEMA, 'dataset_id': uuid.uuid4().hex, 'timezone': 'Asia/Shanghai'}))
                self.refresh()
                with self.connect() as c:
                    pending=c.execute("SELECT MIN(revision) FROM events WHERE status='pending'").fetchone()[0]
                baseline=int(pending)-1 if pending is not None else int(self.meta('revision'))
                if self.remote_revision < baseline:
                    raise ValueError('共享档案版本回退或尚未完整可见，未取得编辑权，请核对恢复点')
                if getattr(self,'publish_callback',None):self.publish_callback(self)
                else:self.publish()
                with self.connect() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('was_editing','1')")
            except BaseException:
                self.ownership.release()
                raise

    def release(self):
        with self.lock:
            if self.editing:
                self.publish()
                self.snapshot()
                with self.connect() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('was_editing','0')")
            self.ownership.release()

    def close(self):
        resume=self.editing or self.meta('was_editing','0')=='1'
        try:
            self.release()
        finally:
            self.ownership.release()
            if resume:
                with self.connect() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('was_editing','1')")

    def records(self):
        with self.connect() as c:
            return [json.loads(row[0]) for row in c.execute('SELECT data FROM records ORDER BY path')]

    def records_snapshot(self):
        """只读视图专用：同一记录版本复用解码结果，调用方不得修改返回对象。"""
        with self.connect() as c:
            version=c.execute('SELECT value FROM records_version WHERE id=1').fetchone()[0]
            with self._snapshot_lock:
                if self._snapshot[0]==version:return self._snapshot[1]
            # 版本与内容在同一读事务内取得，避免并发写入后把旧内容记成新版本。
            c.execute('BEGIN')
            version=c.execute('SELECT value FROM records_version WHERE id=1').fetchone()[0]
            rows=[json.loads(row[0]) for row in c.execute('SELECT data FROM records ORDER BY path')]
            c.execute('COMMIT')
        with self._snapshot_lock:self._snapshot=(version,rows)
        return rows

    def records_for_assets(self,asset_ids):
        """按资产身份取记录，不解码无关记录。"""
        identities=sorted({str(a) for a in asset_ids if a})
        if not identities:return []
        with self.connect() as c:
            return [json.loads(row[0]) for row in c.execute("SELECT data FROM records WHERE json_extract(data,'$.asset_id') IN (SELECT value FROM json_each(?)) ORDER BY path",(json.dumps(identities),))]

    @contextmanager
    def deferred_publish(self):
        """交互锁内只提交本机库；调用方离开锁后再调用 publish_quietly 发布到共享目录。"""
        depth=getattr(self._publish_state,'depth',0)
        self._publish_state.depth=depth+1
        try:yield
        finally:self._publish_state.depth=depth

    def publish_quietly(self):
        try:
            callback=getattr(self,'publish_callback',None)
            if callback:callback(self)
            else:self.publish()
        except (OSError, ValueError) as exc:self.last_error=str(exc)

    def record(self, *, path=None, file_id=None):
        with self.connect() as c:
            row=c.execute('SELECT data FROM records WHERE '+('id=?' if file_id else 'path=? COLLATE NOCASE'),(file_id or path,)).fetchone()
        return json.loads(row[0]) if row else None

    def file_records(self,file_id):
        with self.connect() as c:
            rows=c.execute("SELECT data FROM records WHERE json_extract(data,'$.file_id')=? OR id=?",(file_id,file_id)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def is_shared(self,record_id):
        with self.connect() as c:return c.execute('SELECT 1 FROM synced_records WHERE id=?',(record_id,)).fetchone() is not None

    def groups(self):
        with self.connect() as c:
            return [json.loads(row[0]) for row in c.execute('SELECT data FROM groups_ ORDER BY id')]

    def _validate(self, value):
        if not isinstance(value, dict) or not isinstance(value.get('id'), str):
            raise ValueError('随行记录身份无效')
        uuid.UUID(value['id'])
        for key in ('asset_id','file_id'):
            if value.get(key):uuid.UUID(str(value[key]))
        if 'rating' in value and (type(value['rating']) is not int or value['rating'] not in range(6)):
            raise ValueError('随行记录星级无效')
        if 'signature' in value and (not isinstance(value['signature'],list) or len(value['signature'])!=2 or any(type(n) is not int or n<0 for n in value['signature'])):
            raise ValueError('随行记录文件版本无效')
        if 'media_type' in value and value['media_type'] not in ('image','video'):
            raise ValueError('随行记录媒体类型无效')
        for key in ('sku','base_sku','parent_sku'):
            if key in value and (not isinstance(value[key],str) or len(value[key])>80 or any(ord(c)<32 for c in value[key])):
                raise ValueError('随行记录款号无效')
        if 'relative_path' in value:
            resolve_under(self.root, value['relative_path'], verify_links=False)
        if len(encoded(value)) > MAX_EVENT:
            raise ValueError('随行记录过大')

    def change(self, updates=(), groups=(), *, kind='update', before=None, tasks=(), context=None):
        with self.lock:
            self.ownership.check()
            updates, groups = list(updates), list(groups)
            for value in updates:
                self._validate(value)
            for value in groups:
                self._validate(value)
            operation = uuid.uuid4().hex
            with self.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                parent = int(c.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
                old_context={key:self.meta(key) for key in ('customer','brand','context_decision')}
                new_context=dict(old_context if context is None else context)
                if context is not None:new_context['context_decision']=operation
                event = {'schema': SCHEMA, 'dataset_id': self.dataset_id, 'id': operation,
                         'revision': parent + 1, 'parent': parent, 'kind': kind,
                         'updates': updates, 'groups': groups, 'context':new_context, 'created': time.time()}
                event['writer']={'device':socket.gethostname(),'session':self.ownership.session}
                if before is not None:event['history']={'before':[historical(r) for r in before],'after':[historical(r) for r in updates]}
                if context is not None and event.get('history') is not None:event['history']['context']={'before':old_context,'after':new_context}
                if len(encoded(event))>MAX_EVENT:raise ValueError('此操作的区间资料过大，请缩小修正范围后重试')
                self._apply(c, event)
                c.execute('INSERT INTO events VALUES(?,?,?,?)', (operation, parent + 1, encoded(event).decode(), 'pending'))
                for task in tasks:
                    c.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', (task.get('id', uuid.uuid4().hex), task['kind'], task['record'], task['revision'], 'queued', '', json.dumps({'_manual_backfill':1,**task.get('data', {})} if task['kind'] in ('metadata','metadata_fill','metadata_sidecar') else task.get('data', {}), ensure_ascii=False)))
            if not getattr(getattr(self,'_publish_state',None),'depth',0):
                self.publish_quietly()
            return operation

    def _apply(self, c, event):
        if event.get('history') is not None:
            history=event['history']
            c.execute('INSERT OR IGNORE INTO history(id,kind,before_data,after_data,created,revision) VALUES(?,?,?,?,?,?)',
                      (event['id'],event['kind'],json.dumps(history['before'],ensure_ascii=False),json.dumps(history['after'],ensure_ascii=False),event.get('created',0),event['revision']))
            if history.get('context') is not None:c.execute('INSERT OR IGNORE INTO history_context VALUES(?,?)',(event['id'],json.dumps(history['context'],ensure_ascii=False)))
        context = event.get('context', {})
        for key in ('customer', 'brand','context_decision'):
            if key in context:
                value = context[key]
                if not isinstance(value, str) or len(value) > 100:
                    raise ValueError('数据集客户／品牌字段无效')
                c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, value))
        for record in event.get('updates', []):
            self._validate(record)
            replaced=record.get('replaces_file_id')
            if replaced and replaced!=record['id']:
                prior=c.execute('SELECT data FROM records WHERE id=? AND path=?',(replaced,record['relative_path'])).fetchone()
                if prior:
                    value=json.loads(prior[0])
                    if value.get('sku') or value.get('decision_id'):raise ValueError('已有确认的文件身份发生变化，请先核对')
                    c.execute('DELETE FROM records WHERE id=?',(replaced,))
            c.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,data=excluded.data',
                      (record['id'], record['relative_path'], encoded(record).decode()))
            c.execute('INSERT OR IGNORE INTO synced_records VALUES(?)',(record['id'],))
        for group in event.get('groups', []):
            self._validate(group)
            c.execute('INSERT INTO groups_ VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (group['id'], encoded(group).decode()))
        c.execute("INSERT OR REPLACE INTO meta VALUES('revision',?)", (str(event['revision']),))

    def publish(self):
        lock=getattr(self,'_publish_lock',None)
        with lock if lock is not None else nullcontext():
            self._publish_pending()

    def _publish_pending(self):
        self.ownership.check()
        with self.connect() as c:
            pending = [dict(row) for row in c.execute("SELECT * FROM events WHERE status='pending' ORDER BY revision")]
        for row in pending:
            self.ownership.check()
            content = row['payload'].encode('utf8')
            filename = row['id'] + '.json'
            publish_file(self.folder / 'events' / filename, content)
            commit = {'schema': SCHEMA, 'dataset_id': self.dataset_id, 'revision': row['revision'], 'event': filename, 'sha256': digest(content)}
            publish_file(self.folder / 'commits' / (f'{row["revision"]:020d}-' + filename), encoded(commit))
            with self.connect() as c:
                c.execute("UPDATE events SET status='published' WHERE id=?", (row['id'],))
        self.last_error = ''
        count=int(self.meta('record_count','0'));previous=int(self.meta('snapshot_record_count','0'))
        delta=int(self.meta('revision'))-int(self.meta('snapshot_revision','0'))
        if pending and time.monotonic()-self.last_snapshot_at>=30 and ((count>=256 and count>=max(256,previous*2)) or delta>=max(1000,count//10)):
            self.snapshot()
        if pending:
            from automatic_backup import dataset_changed
            dataset_changed(self.local.parent,self.root)

    def refresh(self):
        with self.lock:
            if self.folder.is_symlink() or self.folder.is_junction():
                raise ValueError('随行目录不能是链接')
            descriptor = self.folder / 'dataset.json'
            if not descriptor.exists():
                return False
            description = read_json(descriptor)
            if description.get('schema') != SCHEMA:
                raise ValueError('档案格式版本不受支持，请升级后再编辑')
            dataset_id = uuid.UUID(description['dataset_id']).hex
            known = self.dataset_id
            if known and known != dataset_id:
                raise ValueError('此目录的数据集身份发生改变，请重新核对来源')
            with self.connect() as c:
                c.execute("INSERT OR REPLACE INTO meta VALUES('dataset_id',?)", (dataset_id,))
            revision = int(self.meta('revision'))
            if revision == 0:
                self._load_snapshot()
                revision = int(self.meta('revision'))
            commits = sorted((self.folder / 'commits').glob('*.json'))
            with self.connect() as c:
                pending_revisions={r[0] for r in c.execute("SELECT revision FROM events WHERE status='pending'")}
            self.remote_revision = 0
            sources=[]
            for path in commits:
                commit = read_json(path)
                if commit.get('dataset_id') != dataset_id:
                    from portable_capture_recovery import is_recovered
                    if is_recovered(self,commit):continue
                    sources.append(commit)
                    continue
                number = int(commit['revision'])
                if number < revision and number not in pending_revisions:
                    self.remote_revision=max(self.remote_revision,number)
                    continue
                filename = relative_path(commit['event'])
                if '/' in filename:
                    raise ValueError('提交引用的事件路径无效')
                event_path = self.folder / 'events' / filename
                if not event_path.exists():
                    self.last_error = '提交已发现，依赖记录尚未可读'
                    break
                if event_path.stat().st_size > MAX_EVENT:
                    raise ValueError('事件过大，未加载')
                content = event_path.read_bytes()
                if digest(content) != commit['sha256']:
                    raise ValueError('随行事件摘要不一致')
                event = json.loads(content)
                self.remote_revision=max(self.remote_revision,number)
                if event['dataset_id'] != dataset_id or event['revision'] != number or event.get('schema') != SCHEMA:
                    raise ValueError('事件身份或版本不一致')
                with self.connect() as c:
                    prior = c.execute('SELECT id FROM events WHERE revision=?', (number,)).fetchone()
                    if prior:
                        if prior['id'] != event['id']:
                            raise ValueError('同一修订出现分叉，未覆盖本机更正')
                        continue
                    if number <= revision:
                        continue
                    if number != revision + 1 or event['parent'] != revision:
                        self.last_error = '正在等待前序修订，保留上个完整版本'
                        break
                    self._apply(c, event)
                    self.imported_records.extend(event.get('updates',[]))
                    c.execute('INSERT INTO events VALUES(?,?,?,?)', (event['id'], number, content.decode(), 'published'))
                revision = number
            if sources:
                from portable_capture_recovery import project_sources
                project_sources(self,sources)
            return True

    def snapshot(self):
        self.ownership.check()
        revision = int(self.meta('revision'))
        if not revision or revision == int(self.meta('snapshot_revision', '0')):
            return
        with self.connect() as c:
            if c.execute("SELECT 1 FROM events WHERE status='pending' LIMIT 1").fetchone():
                return
        name = f'{revision:020d}-' + uuid.uuid4().hex + '.sqlite3'
        temporary = self.local / name
        try:
            with closing(sqlite3.connect(temporary)) as out, out, self.connect() as source:
                out.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE records(id TEXT PRIMARY KEY,path TEXT UNIQUE,data TEXT);CREATE TABLE groups_(id TEXT PRIMARY KEY,data TEXT);CREATE TABLE history(id TEXT PRIMARY KEY,kind TEXT,before_data TEXT,after_data TEXT,created REAL,revision INTEGER);CREATE TABLE history_context(id TEXT PRIMARY KEY,data TEXT);')
                out.executemany('INSERT INTO meta VALUES(?,?)', [('schema', '1'), ('dataset_id', self.dataset_id), ('revision', str(revision)), ('customer', self.meta('customer')), ('brand', self.meta('brand')),('context_decision',self.meta('context_decision'))])
                out.executemany('INSERT INTO records VALUES(?,?,?)', [tuple(row) for row in source.execute('SELECT * FROM records')])
                out.executemany('INSERT INTO groups_ VALUES(?,?)', [tuple(row) for row in source.execute('SELECT * FROM groups_')])
                out.executemany('INSERT INTO history VALUES(?,?,?,?,?,?)',[tuple(row) for row in source.execute('SELECT id,kind,before_data,after_data,created,revision FROM history')])
                out.executemany('INSERT INTO history_context VALUES(?,?)',[tuple(row) for row in source.execute('SELECT * FROM history_context')])
                if out.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('快照校验未通过')
            content = temporary.read_bytes()
            publish_file(self.folder / 'snapshots' / name, content)
            publish_file(self.folder / 'snapshot-commits' / (name + '.json'), encoded({'schema': SCHEMA, 'dataset_id': self.dataset_id, 'revision': revision, 'file': name, 'sha256': digest(content)}))
            with self.connect() as c:
                c.execute("INSERT OR REPLACE INTO meta VALUES('snapshot_revision',?)", (str(revision),))
                c.execute("INSERT OR REPLACE INTO meta VALUES('snapshot_record_count',?)", (self.meta('record_count','0'),))
            self.last_snapshot_at=time.monotonic()
        finally:
            temporary.unlink(missing_ok=True)

    def _load_snapshot(self):
        errors = []
        for marker in sorted((self.folder / 'snapshot-commits').glob('*.json'), reverse=True):
            temporary = self.local / (uuid.uuid4().hex + '.reading.sqlite3')
            try:
                commit = read_json(marker)
                name = relative_path(commit['file'])
                if '/' in name or commit['dataset_id'] != self.dataset_id:
                    raise ValueError('快照身份不匹配')
                source = self.folder / 'snapshots' / name
                if source.is_symlink() or source.stat().st_size > 512 * 1024 * 1024:
                    raise ValueError('快照路径或大小无效')
                content = source.read_bytes()
                if digest(content) != commit['sha256']:
                    raise ValueError('快照摘要校验失败')
                temporary.write_bytes(content)
                with closing(sqlite3.connect(temporary.resolve().as_uri() + '?mode=ro', uri=True)) as src:
                    src.execute('PRAGMA trusted_schema=OFF')
                    if src.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('快照 SQLite 校验失败')
                    meta = dict(src.execute('SELECT key,value FROM meta'))
                    if meta.get('schema') != '1' or meta.get('dataset_id') != self.dataset_id or int(meta['revision']) != commit['revision']:
                        raise ValueError('快照内部版本不一致')
                    records = [json.loads(row[0]) for row in src.execute('SELECT data FROM records')]
                    groups = [json.loads(row[0]) for row in src.execute('SELECT data FROM groups_')]
                    histories=list(src.execute('SELECT id,kind,before_data,after_data,created,revision FROM history')) if src.execute("SELECT 1 FROM sqlite_master WHERE name='history'").fetchone() else []
                    contexts=list(src.execute('SELECT * FROM history_context')) if src.execute("SELECT 1 FROM sqlite_master WHERE name='history_context'").fetchone() else []
                with self.connect() as c:
                    self._apply(c, {'revision': commit['revision'], 'updates': records, 'groups': groups, 'context': {k: meta[k] for k in ('customer', 'brand','context_decision') if k in meta}})
                    self.imported_records.extend(records)
                    c.executemany('INSERT OR IGNORE INTO history(id,kind,before_data,after_data,created,revision) VALUES(?,?,?,?,?,?)',histories)
                    c.executemany('INSERT OR IGNORE INTO history_context VALUES(?,?)',contexts)
                return
            except (OSError, ValueError, sqlite3.Error, KeyError) as exc:
                errors.append(str(exc))
            finally:
                temporary.unlink(missing_ok=True)
        if errors:
            self.last_error = '快照回退：' + errors[0]
