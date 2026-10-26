"""持久预览索引和有界磁盘缓存；所有删除操作仅针对本目录中的哈希缓存文件。"""
from contextlib import contextmanager,ExitStack
from itertools import islice
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from file_safety import delete_verified,publish_new
from preview_codec import RECIPE,MAX_PREVIEW_EDGE,validate_stream
from archive_resources import PREVIEW_WORKERS

MAX_CACHE_BYTES=10*1024**3
_NAME=re.compile(r'[0-9a-f]{64}\.(?:jpg|webp)\Z')
_CACHES={}
_GUARD=threading.RLock()
_BUDGETS={}
_LOCATIONS={}
WARM_WORKERS=PREVIEW_WORKERS
_BACKGROUND_SLOTS=threading.BoundedSemaphore(WARM_WORKERS)
_CONTENT_LOCKS=[threading.Lock() for _ in range(64)]


def cache_name(record,size,decode=False,*,version=RECIPE):
    return hashlib.sha256((record['fingerprint']+f':{size}:{decode}:{record.get("orientation",1)}:v{version}').encode()).hexdigest()+('.webp' if version==5 else '.jpg')


def registered_recipe(row):
    if row and _NAME.fullmatch(row['name']) and row['fingerprint'] and row['edge']:
        return next((v for v in (RECIPE,5,4,3) if cache_name({'fingerprint':row['fingerprint'],'orientation':row['orientation']},row['edge'],bool(row['decoded']),version=v)==row['name']),None)
    return None


def cache_for(folder):
    folder=Path(folder).resolve()
    with _GUARD:
        if folder.name in ('previews','catalog_previews') and folder.parent in _LOCATIONS:
            folder=_LOCATIONS[folder.parent]/folder.name
        if folder.name in ('previews','catalog_previews'):
            if folder.parent not in _BUDGETS:
                _BUDGETS[folder.parent]=CacheBudget(folder.parent)
            return _CACHES[folder]
        if folder not in _CACHES:_CACHES[folder]=PreviewCache(folder)
        return _CACHES[folder]


class CacheBudget:
    """两种历史缓存共用总额度和 LRU；沿用原文件，不迁移、不重复构建。"""
    def __init__(self,data):
        self.lock=threading.RLock()
        self.condition=threading.Condition();self.readers=0;self.switching=False
        primary=PreviewCache(data/'previews')
        self.caches=[primary,PreviewCache(data/'catalog_previews',limit=primary.limit)]
        self.primary=self.caches[0]
        with self.primary.connect() as c:
            row=c.execute("SELECT value FROM configuration WHERE key='limit_gb'").fetchone()
        self.limit=(int(row[0]) if row else 10)*1024**3
        for cache in self.caches:
            cache.budget=self;cache.lock=self.lock;cache.limit=self.limit;_CACHES[cache.folder]=cache

    def trim(self,current,connection,incoming=0,exclude='',*,limit=None,max_entries=None):
        from contextlib import ExitStack
        from heapq import merge
        with ExitStack() as stack:
            connections={cache:connection if cache is current else stack.enter_context(cache.connect()) for cache in self.caches}
            for cache,c in connections.items():cache._flush_touches(c)
            target_limit=self.limit if limit is None else limit
            total=sum(c.execute('SELECT bytes FROM usage WHERE id=1').fetchone()[0] for c in connections.values())
            old=connection.execute('SELECT bytes FROM previews WHERE name=?',(exclude,)).fetchone()
            if old:total-=old[0]
            if total+incoming<=target_limit:return True
            candidates=[]
            def entries(cache,cursor):
                for r in cursor:yield (r['accessed'],r['name'],cache,r['bytes'])
            for cache,c in connections.items():
                cursor=c.execute('SELECT name,bytes,accessed FROM previews WHERE name!=? ORDER BY accessed,name',
                                 (exclude if cache is current else '',))
                candidates.append(entries(cache,cursor))
            considered=0
            for _,name,cache,count in merge(*candidates,key=lambda r:(r[0],r[1])):
                if cache._remove(connections[cache],name):total-=count
                if total+incoming<=target_limit:return True
                considered+=1
                if max_entries and considered>=max_entries:return False
            if total+incoming>target_limit:raise OSError('预览缓存空间不足或缓存文件被占用')

    def configure(self,gb):
        if type(gb) is not int or not 1<=gb<=256:raise ValueError('图片缓存上限须为 1–256 GB 的整数')
        with self.lock:
            self.limit=gb*1024**3
            for cache in self.caches:cache.limit=self.limit
            with self.primary.connect() as c:
                c.execute("INSERT OR REPLACE INTO configuration VALUES ('limit_gb',?)",(str(gb),))
            with self.primary.connect() as c:self.trim(self.primary,c)
        return self.state()

    def state(self):
        with self.lock:
            states=[cache.local_state() for cache in self.caches]
            result={key:sum(state[key] for state in states) for key in ('bytes','images','pending','failed')}
            return dict(result,limit_bytes=self.limit,limit_gb=self.limit//1024**3,workers=WARM_WORKERS)

    @contextmanager
    def generation(self):
        with self.condition:
            self.condition.wait_for(lambda:not self.switching);self.readers+=1
        try:yield
        finally:
            with self.condition:self.readers-=1;self.condition.notify_all()

    @contextmanager
    def exclusive(self):
        with self.condition:
            self.switching=True;self.condition.wait_for(lambda:self.readers==0)
        try:
            with self.lock:yield
        finally:
            with self.condition:self.switching=False;self.condition.notify_all()


class PreviewCache:
    def __init__(self,folder,limit=MAX_CACHE_BYTES):
        self.folder=Path(folder).resolve();self.folder.mkdir(parents=True,exist_ok=True)
        self.db=self.folder/'index.sqlite3';self.limit=limit;self.lock=threading.RLock();self.budget=None
        self._touches={};self._last_touch_flush=time.monotonic();self._read_leases={}
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''CREATE TABLE IF NOT EXISTS previews(
                name TEXT PRIMARY KEY,bytes INTEGER NOT NULL,accessed INTEGER NOT NULL,
                fingerprint TEXT,edge INTEGER,decoded INTEGER,orientation INTEGER);
                CREATE INDEX IF NOT EXISTS preview_fingerprint ON previews(fingerprint);
                CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY,bytes INTEGER NOT NULL);
                INSERT INTO usage SELECT 1,COALESCE((SELECT SUM(bytes) FROM previews),0)
                  WHERE NOT EXISTS(SELECT 1 FROM usage WHERE id=1);
                CREATE TRIGGER IF NOT EXISTS cache_added AFTER INSERT ON previews BEGIN
                    UPDATE usage SET bytes=bytes+NEW.bytes WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS cache_removed AFTER DELETE ON previews BEGIN
                    UPDATE usage SET bytes=bytes-OLD.bytes WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS cache_resized AFTER UPDATE OF bytes ON previews BEGIN
                    UPDATE usage SET bytes=bytes+NEW.bytes-OLD.bytes WHERE id=1; END;
                CREATE TABLE IF NOT EXISTS pending(
                identity TEXT PRIMARY KEY,root TEXT NOT NULL,record TEXT NOT NULL,
                requested INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT);
                CREATE TABLE IF NOT EXISTS configuration(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS archive_jobs(identity TEXT PRIMARY KEY,root_id TEXT NOT NULL,
                    record TEXT NOT NULL,state TEXT NOT NULL,small_ready INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS archive_job_root ON archive_jobs(root_id,state);
                CREATE TABLE IF NOT EXISTS content_records(fingerprint TEXT PRIMARY KEY,content TEXT NOT NULL,probe TEXT);
                CREATE TABLE IF NOT EXISTS content_files(path TEXT PRIMARY KEY,bytes INTEGER,modified_ns INTEGER,digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS content_previews(content TEXT,edge INTEGER,orientation INTEGER,decoded INTEGER,name TEXT,
                    recipe INTEGER NOT NULL DEFAULT 4,probe TEXT,PRIMARY KEY(content,edge,orientation,decoded));
                CREATE INDEX IF NOT EXISTS content_preview_name ON content_previews(name);
                CREATE TRIGGER IF NOT EXISTS preview_aliases_removed AFTER DELETE ON previews BEGIN
                    DELETE FROM content_previews WHERE name=OLD.name; END;
            ''')
            if 'digest' not in {r['name'] for r in c.execute('PRAGMA table_info(previews)')}:
                c.execute('ALTER TABLE previews ADD COLUMN digest TEXT')
            if 'edge' not in {r['name'] for r in c.execute('PRAGMA table_info(pending)')}:
                c.execute('ALTER TABLE pending ADD COLUMN edge INTEGER NOT NULL DEFAULT 480')
            if 'pair_key' not in {r['name'] for r in c.execute('PRAGMA table_info(archive_jobs)')}:
                c.execute("ALTER TABLE archive_jobs ADD COLUMN pair_key TEXT NOT NULL DEFAULT ''")
            if not c.execute("SELECT 1 FROM configuration WHERE key='archive_progress_v1'").fetchone():
                c.execute("UPDATE archive_jobs SET pair_key=COALESCE(json_extract(record,'$._catalog_pair'),'') WHERE pair_key='' AND json_valid(record)")
                c.execute("INSERT INTO configuration VALUES ('archive_progress_v1','1')")
            c.execute('CREATE INDEX IF NOT EXISTS archive_job_progress ON archive_jobs(root_id,state,small_ready,pair_key)')
            if 'recipe' not in {r['name'] for r in c.execute('PRAGMA table_info(content_previews)')}:
                c.execute('ALTER TABLE content_previews ADD COLUMN recipe INTEGER NOT NULL DEFAULT 4')
            for table in ('content_records','content_previews'):
                if 'probe' not in {r['name'] for r in c.execute('PRAGMA table_info('+table+')')}:
                    c.execute('ALTER TABLE '+table+' ADD COLUMN probe TEXT')
            c.execute('CREATE INDEX IF NOT EXISTS content_probe ON content_previews(probe)')
            c.execute('CREATE INDEX IF NOT EXISTS pending_active_order ON pending(edge,attempts,requested) WHERE attempts<3')
            c.execute('CREATE INDEX IF NOT EXISTS pending_request_order ON pending(attempts,requested) WHERE attempts<3')
            c.execute('CREATE INDEX IF NOT EXISTS preview_lru_order ON previews(accessed,name)')
            c.execute('DROP INDEX IF EXISTS preview_lru')
            c.execute('DROP INDEX IF EXISTS pending_ready')
            c.execute('DROP INDEX IF EXISTS pending_small_first')
            if 'published_seq' not in {r['name'] for r in c.execute('PRAGMA table_info(previews)')}:
                c.execute('ALTER TABLE previews ADD COLUMN published_seq INTEGER NOT NULL DEFAULT 0')
            c.execute('CREATE INDEX IF NOT EXISTS preview_published ON previews(published_seq)')
            c.executescript('''CREATE TABLE IF NOT EXISTS cache_totals(id INTEGER PRIMARY KEY,images INTEGER,pending INTEGER,failed INTEGER,portable INTEGER,revision INTEGER);
                INSERT OR IGNORE INTO cache_totals SELECT 1,
                  (SELECT COUNT(*) FROM previews),(SELECT COUNT(*) FROM pending WHERE attempts<3),
                  (SELECT COUNT(*) FROM pending WHERE attempts>=3),0,0 WHERE NOT EXISTS(SELECT 1 FROM cache_totals WHERE id=1);
                CREATE TABLE IF NOT EXISTS portable_names(name TEXT PRIMARY KEY);
                CREATE TRIGGER IF NOT EXISTS totals_image_added AFTER INSERT ON previews BEGIN
                  UPDATE cache_totals SET images=images+1,revision=revision+1 WHERE id=1;
                  UPDATE previews SET published_seq=(SELECT revision FROM cache_totals WHERE id=1) WHERE name=NEW.name; END;
                CREATE TRIGGER IF NOT EXISTS totals_image_removed AFTER DELETE ON previews BEGIN
                  UPDATE cache_totals SET images=images-1,revision=revision+1 WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS totals_image_changed AFTER UPDATE OF bytes ON previews BEGIN
                  UPDATE cache_totals SET revision=revision+1 WHERE id=1;
                  UPDATE previews SET published_seq=(SELECT revision FROM cache_totals WHERE id=1) WHERE name=NEW.name; END;
                CREATE TRIGGER IF NOT EXISTS totals_pending_added AFTER INSERT ON pending BEGIN
                  UPDATE cache_totals SET pending=pending+(NEW.attempts<3),failed=failed+(NEW.attempts>=3) WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS totals_pending_removed AFTER DELETE ON pending BEGIN
                  UPDATE cache_totals SET pending=pending-(OLD.attempts<3),failed=failed-(OLD.attempts>=3) WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS totals_pending_changed AFTER UPDATE OF attempts ON pending BEGIN
                  UPDATE cache_totals SET pending=pending+(NEW.attempts<3)-(OLD.attempts<3),
                    failed=failed+(NEW.attempts>=3)-(OLD.attempts>=3) WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS totals_portable_added AFTER INSERT ON portable_names BEGIN
                  UPDATE cache_totals SET portable=portable+1 WHERE id=1; END;
                CREATE TRIGGER IF NOT EXISTS totals_portable_removed AFTER DELETE ON portable_names BEGIN
                  UPDATE cache_totals SET portable=portable-1 WHERE id=1; END;
                DROP TRIGGER IF EXISTS portable_alias_added;
                CREATE TRIGGER portable_alias_added AFTER INSERT ON content_previews BEGIN
                  INSERT INTO portable_names SELECT NEW.name WHERE EXISTS(SELECT 1 FROM previews WHERE name=NEW.name)
                    AND NOT EXISTS(SELECT 1 FROM portable_names WHERE name=NEW.name); END;
                CREATE TRIGGER IF NOT EXISTS portable_alias_removed AFTER DELETE ON content_previews BEGIN
                  DELETE FROM portable_names WHERE name=OLD.name AND NOT EXISTS(SELECT 1 FROM content_previews WHERE name=OLD.name); END;
                DROP TRIGGER IF EXISTS portable_alias_changed;
                CREATE TRIGGER portable_alias_changed AFTER UPDATE OF name ON content_previews BEGIN
                  DELETE FROM portable_names WHERE name=OLD.name AND NOT EXISTS(SELECT 1 FROM content_previews WHERE name=OLD.name);
                  INSERT INTO portable_names SELECT NEW.name WHERE EXISTS(SELECT 1 FROM previews WHERE name=NEW.name)
                    AND NOT EXISTS(SELECT 1 FROM portable_names WHERE name=NEW.name); END;
            ''')
            if not c.execute("SELECT 1 FROM configuration WHERE key='stats_v1'").fetchone():
                c.execute('INSERT OR IGNORE INTO portable_names SELECT DISTINCT p.name FROM content_previews p JOIN previews v ON v.name=p.name')
                c.execute("INSERT INTO configuration VALUES ('stats_v1','1')")
            saved=c.execute("SELECT value FROM configuration WHERE key='limit_gb'").fetchone()
            if saved:self.limit=int(saved[0])*1024**3
        self.reconcile()

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.db,timeout=30);c.row_factory=sqlite3.Row
        # 仅可再生预览/队列索引使用 WAL NORMAL，避免每张预览同步刷盘拖住全局锁。
        # 评级、SKU、原片元数据等业务库不受影响；掉电后由缓存核对和队列补齐恢复。
        c.execute('PRAGMA synchronous=NORMAL')
        try:
            with c:yield c
        finally:c.close()

    def reconcile(self,limit=256):
        """只核对登记过的缓存；不能凭哈希文件名接管、删除用户文件。"""
        with self.lock,self.connect() as c:
            cursor=c.execute("SELECT value FROM configuration WHERE key='reconcile_cursor'").fetchone()
            rows=c.execute('SELECT * FROM previews WHERE name>? ORDER BY name LIMIT ?',(cursor[0] if cursor else '',limit)).fetchall()
            for row in rows:
                path=self.folder/row['name']
                if not self._registered(row) or not path.is_file() or path.is_symlink():
                    c.execute('DELETE FROM previews WHERE name=?',(row['name'],));continue
                # 旧生成参数只能证明曾生成过，不能证明磁盘上仍是原缓存。
                if row['digest'] is None:
                    c.execute('DELETE FROM previews WHERE name=?',(row['name'],))
            c.execute("INSERT OR REPLACE INTO configuration VALUES ('reconcile_cursor',?)",(rows[-1]['name'] if len(rows)==limit else '',))
            self._trim(c,max_entries=16)

    @staticmethod
    def _digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _registered(row):
        return registered_recipe(row) is not None

    def _owned(self,c,name):
        row=c.execute('SELECT * FROM previews WHERE name=?',(name,)).fetchone();path=self.folder/name
        return self._registered(row) and bool(row['digest']) and not path.is_symlink() and path.is_file() and self._digest(path)==row['digest']

    def _remove(self,c,name):
        if not _NAME.fullmatch(name):return False
        # 已打开的缓存允许完成本次响应；淘汰改选下一个条目，避免锁外读取时删掉文件。
        if self._read_leases.get(name):return False
        try:
            # 文件被外部替换或索引不足以证明归属时，只移除索引，保留文件。
            if self._owned(c,name):
                row=c.execute('SELECT digest FROM previews WHERE name=?',(name,)).fetchone()
                delete_verified(self.folder/name,row['digest'])
        except OSError:return False  # 被外部程序占用时继续尝试其他缓存。
        c.execute('DELETE FROM previews WHERE name=?',(name,));return True

    def _trim(self,c,incoming=0,exclude='',*,max_entries=None):
        if self.budget:return self.budget.trim(self,c,incoming,exclude,max_entries=max_entries)
        self._flush_touches(c)
        total=c.execute('SELECT bytes FROM usage WHERE id=1').fetchone()[0]
        old=c.execute('SELECT bytes FROM previews WHERE name=?',(exclude,)).fetchone()
        if old:total-=old[0]
        if total+incoming<=self.limit:return
        considered=0
        for row in c.execute('SELECT name,bytes FROM previews WHERE name!=? ORDER BY accessed,name',(exclude,)):
            if self._remove(c,row['name']):total-=row['bytes']
            if total+incoming<=self.limit:return
            considered+=1
            if max_entries and considered>=max_entries:return False
        if total+incoming>self.limit:raise OSError('预览缓存空间不足或缓存文件被占用')

    def get(self,name,*,read=False):
        if read:return self._read(name)
        with self.lock,self.connect() as c:
            row=c.execute('SELECT * FROM previews WHERE name=?',(name,)).fetchone()
            if row is None:return None
            if not self._registered(row) or not row['digest']:self._remove(c,name);return None
            path=self.folder/name
            try:
                if path.stat().st_size!=row['bytes']:raise ValueError('缓存不完整')
                with path.open('rb') as stream:
                    validate_stream(stream,row['bytes'])
                    if read:stream.seek(0);result=stream.read()
                    else:result=path
                if read and hashlib.sha256(result).hexdigest()!=row['digest']:raise ValueError('缓存校验失败')
            except (OSError,ValueError):self._remove(c,name);return None
            self._touches[name]=time.time_ns()
            if len(self._touches)>=256 or time.monotonic()-self._last_touch_flush>=5:self._flush_touches(c)
            return result

    def _read(self,name):
        """读取缓存 payload 时不占用全局索引锁，且让淘汰避开已打开文件。"""
        with self.lock,self.connect() as c:
            row=c.execute('SELECT * FROM previews WHERE name=?',(name,)).fetchone()
            if row is None:return None
            if not self._registered(row) or not row['digest']:
                self._remove(c,name);return None
            path=self.folder/name
            try:
                if path.stat().st_size!=row['bytes']:raise ValueError('缓存不完整')
                stream=path.open('rb')
            except OSError:
                self._remove(c,name);return None
            self._read_leases[name]=self._read_leases.get(name,0)+1
            expected=(row['bytes'],row['digest'])
        payload=None;valid=False
        try:
            with stream:
                validate_stream(stream,expected[0]);stream.seek(0);payload=stream.read()
            if hashlib.sha256(payload).hexdigest()!=expected[1]:raise ValueError('缓存校验失败')
            valid=True
        except (OSError,ValueError):
            pass
        finally:
            with self.lock,self.connect() as c:
                remaining=self._read_leases.get(name,0)-1
                if remaining>0:self._read_leases[name]=remaining
                else:self._read_leases.pop(name,None)
                current=c.execute('SELECT bytes,digest FROM previews WHERE name=?',(name,)).fetchone()
                if not valid:
                    if current and (current['bytes'],current['digest'])==expected:self._remove(c,name)
                elif current and (current['bytes'],current['digest'])==expected:
                    self._touches[name]=time.time_ns()
                    if len(self._touches)>=256 or time.monotonic()-self._last_touch_flush>=5:self._flush_touches(c)
        return payload if valid else None

    def _flush_touches(self,connection):
        """访问时间批量落盘；淘汰前强制提交，缓存命中不逐次同步写 SQLite。"""
        if not self._touches:return
        connection.executemany('UPDATE previews SET accessed=max(accessed,?) WHERE name=?',
            [(stamp,name) for name,stamp in self._touches.items()])
        self._touches.clear();self._last_touch_flush=time.monotonic()

    def contains(self,names,connection=None):
        """入队只读检查，不能在队列写事务内另开连接更新访问时间。"""
        def check(c):
            for name in names:
                row=c.execute('SELECT bytes FROM previews WHERE name=?',(name,)).fetchone()
                try:
                    if row is None or (self.folder/name).stat().st_size!=row['bytes']:return False
                except OSError:return False
            return True
        if connection is not None:return check(connection)
        with self.lock,self.connect() as c:return check(c)

    def larger(self,record,size,decode=False):
        """同一版本和方向的清晰缓存可直接供较小视口使用，不再读取 NAS。"""
        with self.lock,self.connect() as c:
            rows=c.execute('SELECT name FROM previews WHERE fingerprint=? AND orientation=? AND decoded=? AND edge>=? ORDER BY edge',
                (record['fingerprint'],record.get('orientation',1),int(decode),size)).fetchall()
        for row in rows:
            hit=self.get(row['name'])
            if hit is not None:return hit
        return None

    def content_key(self,record,root):
        """完整 SHA-256；路径与盘符只用于本机加速，绝不进入可移植身份。"""
        stripe=int(hashlib.sha256(record['fingerprint'].encode()).hexdigest()[:8],16)%len(_CONTENT_LOCKS)
        with _CONTENT_LOCKS[stripe]:return self._content_key(record,root)

    def known_content(self,record):
        with self.lock,self.connect() as c:
            row=c.execute('SELECT content,probe FROM content_records WHERE fingerprint=?',(record['fingerprint'],)).fetchone()
            if row:
                record['_portable_probe']=row['probe'];return row['content']

    def probe_key(self,record,root):
        parts=[]
        for value in record.get('_portable_paths') or [str(Path(root)/record[key]) for key in ('raw','jpg') if record.get(key)]:
            with Path(value).open('rb') as stream:
                size=stream.seek(0,2);digest=hashlib.sha256(str(size).encode())
                for offset in sorted({0,max(0,size//2-32768),max(0,size-65536)}):
                    stream.seek(offset);digest.update(str(offset).encode()+b':'+stream.read(65536))
                parts.append(digest.hexdigest())
        if not parts:raise ValueError('没有可读取的照片内容')
        probe=hashlib.sha256(('probe-v1:'+':'.join(sorted(set(parts)))).encode()).hexdigest()
        record['_portable_probe']=probe;return probe

    def candidate(self,record,root):
        probe=self.probe_key(record,root)
        for cache in self.budget.caches if self.budget else [self]:
            with cache.lock,cache.connect() as c:
                if c.execute('SELECT 1 FROM content_previews WHERE probe=? OR probe IS NULL LIMIT 1',(probe,)).fetchone():return True
        return False

    def has_portable(self):
        for cache in self.budget.caches if self.budget else [self]:
            with cache.lock,cache.connect() as c:
                if c.execute('SELECT portable FROM cache_totals WHERE id=1').fetchone()[0]:return True
        return False

    def _content_key(self,record,root):
        known=self.known_content(record)
        if known:return known
        paths=record.get('_portable_paths') or [str(Path(root)/record[key]) for key in ('raw','jpg') if record.get(key)]
        if not paths:raise ValueError('照片缺少可校验文件')
        versions=[Path(p).stat() for p in paths]
        expected=record.get('_portable_versions')
        if expected:
            actual=[hashlib.sha256(f'{str(Path(p)).casefold()}:{s.st_size}:{s.st_mtime_ns}'.encode()).hexdigest() for p,s in zip(paths,versions)]
            if actual!=expected:raise ValueError('归档文件已变化，请刷新索引后准备缓存指纹')
        elif not record.get('_catalog') and re.fullmatch(r'[0-9a-f]{64}',record['fingerprint']):
            actual=hashlib.sha256(json.dumps([(p,s.st_size,s.st_mtime_ns) for p,s in zip(paths,versions)],ensure_ascii=False).encode()).hexdigest()
            if actual!=record['fingerprint']:raise ValueError('照片已变化，请刷新索引后准备缓存指纹')
        digests=[]
        for value in paths:
            path=Path(value)
            before=path.stat()
            with self.lock,self.connect() as c:
                old=c.execute('SELECT * FROM content_files WHERE path=?',(str(path),)).fetchone()
            if old and old['bytes']==before.st_size and old['modified_ns']==before.st_mtime_ns:digest=old['digest']
            else:
                with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                after=path.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise OSError('照片指纹读取期间发生变化，请稍后重试')
                with self.lock,self.connect() as c:
                    c.execute('INSERT OR REPLACE INTO content_files VALUES (?,?,?,?)',(str(path),before.st_size,before.st_mtime_ns,digest))
            digests.append(digest)
        if not digests:raise FileNotFoundError('照片已离线，尚无可匹配的内容指纹')
        content=hashlib.sha256(('sha256-full-v1:'+':'.join(sorted(set(digests)))).encode()).hexdigest()
        probe=record.get('_portable_probe') or self.probe_key(record,root)
        with self.lock,self.connect() as c:c.execute('INSERT OR REPLACE INTO content_records(fingerprint,content,probe) VALUES (?,?,?)',(record['fingerprint'],content,probe))
        return content

    def portable_ready(self,record,connection):
        return connection.execute('SELECT 1 FROM content_records WHERE fingerprint=?',(record['fingerprint'],)).fetchone() is not None

    def bind_content(self,content,record,size,decode,name,recipe=None):
        with self.lock,self.connect() as c:
            row=c.execute('SELECT * FROM previews WHERE name=?',(name,)).fetchone()
            if row is None:return
            if recipe is None:recipe=registered_recipe(row)
            if recipe is None:return
            c.execute('''INSERT INTO content_previews(content,edge,orientation,decoded,name,recipe,probe) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(content,edge,orientation,decoded) DO UPDATE SET name=excluded.name,recipe=excluded.recipe,
                probe=COALESCE(excluded.probe,content_previews.probe)''',
                      (content,size,record.get('orientation',1),int(decode),name,recipe,record.get('_portable_probe')))

    def find_content(self,content,size,orientation,decode):
        caches=self.budget.caches if self.budget else [self]
        for cache in caches:
            with cache.lock,cache.connect() as c:
                row=c.execute('SELECT name FROM content_previews WHERE content=? AND edge=? AND orientation=? AND decoded=? AND recipe=6 ORDER BY recipe DESC LIMIT 1',
                              (content,size,orientation,int(decode))).fetchone()
            if row:
                hit=cache.get(row[0])
                if hit is not None:return hit
        return None

    def publish(self,temp,target,record,size,decode):
        with self.lock,self.connect() as c:
            if target.parent.resolve()!=self.folder or target.name!=cache_name(record,size,decode):
                raise ValueError('预览只能写入专用缓存目录')
            if (target.exists() or target.is_symlink()) and not self._owned(c,target.name):
                raise OSError('缓存同名文件归属不明或已被外部修改，已保留原文件')
            count=temp.stat().st_size
            if count>self.limit:raise OSError('单张预览超过缓存容量上限')
            digest=self._digest(temp)
            self._trim(c,count,target.name)
            try:publish_new(temp,target)
            except FileExistsError:
                raise OSError('缓存同名文件在提交时已存在，已保留原文件') from None
            c.execute('''INSERT INTO previews(name,bytes,accessed,fingerprint,edge,decoded,orientation,digest) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                      bytes=excluded.bytes,accessed=excluded.accessed,fingerprint=excluded.fingerprint,
                      edge=excluded.edge,decoded=excluded.decoded,orientation=excluded.orientation,digest=excluded.digest''',
                      (target.name,count,time.time_ns(),record['fingerprint'],size,int(decode),record.get('orientation',1),digest))

    def state(self):
        return self.budget.state() if self.budget else self.local_state()

    def local_state(self):
        with self.lock,self.connect() as c:
            totals=c.execute('SELECT * FROM cache_totals WHERE id=1').fetchone()
            size=c.execute('SELECT bytes FROM usage WHERE id=1').fetchone()[0]
        return {'bytes':size,'limit_bytes':self.limit,'images':totals['images'],'pending':totals['pending'],'failed':totals['failed']}

    def retire_legacy(self):
        """只淘汰已登记且仍匹配原哈希的旧生成图，不遍历或接管未知文件。"""
        if any(p.is_symlink() or p.is_junction() for p in (self.folder,*self.folder.parents)):
            raise ValueError('旧预览清理路径不能经过链接')
        removed=0;freed=0;preserved=0
        with self.lock,self.connect() as c:
            rows=c.execute('SELECT * FROM previews').fetchall()
            for row in rows:
                recipe=registered_recipe(row)
                if recipe is None or recipe==RECIPE:continue
                path=self.folder/row['name']
                if self._read_leases.get(row['name']):preserved+=1;continue
                if path.exists():
                    if not row['digest'] or path.is_symlink() or path.is_junction():preserved+=1;continue
                    try:deleted=delete_verified(path,row['digest'])
                    except OSError:preserved+=1;continue
                    if not deleted:preserved+=1;continue
                    removed+=1;freed+=row['bytes']
                c.execute('DELETE FROM previews WHERE name=?',(row['name'],))
            if preserved:c.execute("DELETE FROM configuration WHERE key='preview_policy'")
            else:c.execute("INSERT OR REPLACE INTO configuration VALUES ('preview_policy','embedded-jpeg-2048-v6')")
        return {'removed_files':removed,'freed_bytes':freed,'preserved_files':preserved}


class PreviewWarmer:
    """持久并行队列；每张一次读取完成小图和最高 2048 预览。"""
    def __init__(self,cache,exiftool,*,autostart=True,catalog=None):
        self.cache=cache;self.exiftool=exiftool;self.closed=threading.Event();self.wake=threading.Event()
        self.catalog=catalog;self.sessions=set();self.lock=threading.RLock();self.active=set()
        self.reconcile_lock=threading.Lock();self.reconcile_requested=threading.Event();self.reconcile_videos=True;self.reconciling=False
        self.reconcile_thread=threading.Thread(target=self._reconcile,name='archive-preview-reconcile',daemon=True)
        with cache.lock,cache.connect() as c:
            # 迁移既有两阶段任务；保留重试次数，取消全库小图阻挡清晰图的旧排序。
            c.execute('UPDATE pending SET edge=2048 WHERE edge>2048')
        self.threads=[threading.Thread(target=self._run,name=f'preview-cache-{i}',daemon=True) for i in range(WARM_WORKERS)]
        self.thread=self.threads[0]
        if autostart:self.start()

    def start(self):
        for thread in self.threads:
            if thread.ident is None:thread.start()

    def enqueue(self,root,records):
        root_key=str(Path(root).absolute()).casefold()
        records=iter(records)
        while batch:=list(islice(records,128)):self._enqueue_batch(root,root_key,batch)
        self.wake.set()

    def _enqueue_batch(self,root,root_key,records):
        def complete(record,index,connection):
            small=420 if record.get('_catalog') else 480
            if record.get('_catalog'):
                cached=connection.execute('SELECT orientation FROM previews WHERE fingerprint=? LIMIT 1',(record['fingerprint'],)).fetchone()
                oriented=dict(record,orientation=cached[0]) if cached else record
                return tuple(index.contains([cache_name(oriented,edge)],connection) for edge in (small,MAX_PREVIEW_EDGE))
            direct=tuple(index.contains([cache_name(record,edge)],connection) for edge in (small,MAX_PREVIEW_EDGE))
            if all(direct):return direct
            content=connection.execute('SELECT content FROM content_records WHERE fingerprint=?',(record['fingerprint'],)).fetchone()
            if not content:return False,False
            aliases=[connection.execute('SELECT name FROM content_previews WHERE content=? AND edge=? AND orientation=? AND decoded=0 AND recipe=6 ORDER BY recipe DESC LIMIT 1',
                (content[0],edge,record.get('orientation',1))).fetchone() for edge in (small,MAX_PREVIEW_EDGE)]
            return tuple(bool(row) and index.contains([row[0]],connection) for row in aliases)
        # 缺缓存的预检不持有全局写锁，一批复用连接，不能让恢复全库队列堵住前台。
        plans=[]
        with ExitStack() as readers:
            connections={}
            for record in records:
                index=cache_for(self.cache.folder.parent/'catalog_previews') if record.get('_catalog') else self.cache
                if index not in connections:connections[index]=readers.enter_context(index.connect())
                plans.append((record,index,complete(record,index,connections[index])))
        with self.cache.lock,self.cache.connect() as c,ExitStack() as readers:
            connections={self.cache:c}
            for record,index,completed in plans:
                identity=root_key+':'+record['id']
                payload=json.dumps(record,ensure_ascii=False,sort_keys=True)
                small=420 if record.get('_catalog') else 480
                large=MAX_PREVIEW_EDGE
                # 只对预检命中的条目在锁内复核，淘汰发生后不能误报 ready。
                if any(completed):
                    if index not in connections:connections[index]=readers.enter_context(index.connect())
                    completed=complete(record,index,connections[index])
                small_ready,large_ready=completed
                ready=small_ready and large_ready
                if record.get('_catalog_root'):
                    c.execute('''INSERT INTO archive_jobs(identity,root_id,record,state,small_ready,pair_key) VALUES (?,?,?,?,?,?)
                        ON CONFLICT(identity) DO UPDATE SET root_id=excluded.root_id,record=excluded.record,
                        state=CASE WHEN archive_jobs.record=excluded.record AND archive_jobs.state='failed' AND excluded.state!='ready' THEN archive_jobs.state ELSE excluded.state END,
                        small_ready=excluded.small_ready,pair_key=excluded.pair_key WHERE archive_jobs.record!=excluded.record OR excluded.state='ready' OR archive_jobs.state='ready' OR archive_jobs.small_ready!=excluded.small_ready ''',
                        (identity,record['_catalog_root'],payload,'ready' if ready else 'queued',int(small_ready),record.get('_catalog_pair','')))
                if ready:
                    c.execute('DELETE FROM pending WHERE identity=?',(identity,));continue
                c.execute('''INSERT INTO pending(identity,root,record,requested,edge) VALUES (?,?,?,?,?)
                    ON CONFLICT(identity) DO UPDATE SET root=excluded.root,record=excluded.record,
                    requested=excluded.requested,edge=excluded.edge,attempts=0,error=NULL WHERE pending.record!=excluded.record''',
                    (identity,str(root),payload,time.time_ns(),large if small_ready else small))

    def reconcile_archive(self,*,videos_only=False):
        if self.catalog is None:return
        with self.reconcile_lock:
            self.reconcile_videos=self.reconcile_videos and videos_only
            self.reconcile_requested.set()
            if not self.reconcile_thread.is_alive():
                self.reconcile_thread=threading.Thread(target=self._reconcile,name='archive-preview-reconcile',daemon=True)
                self.reconcile_thread.start()

    def _reconcile(self):
        while not self.closed.is_set():
            self.reconcile_requested.wait(1)
            with self.reconcile_lock:
                if not self.reconcile_requested.is_set():continue
                videos_only=self.reconcile_videos;self.reconcile_videos=True;self.reconcile_requested.clear()
                self.reconciling=True
            try:self.catalog.queue_all_previews(self.closed.is_set,videos_only=videos_only)
            except (OSError,sqlite3.Error):pass
            finally:self.reconciling=False

    def archive_state(self,root_ids):
        # WAL 的只读统计使用独立快照；全库关联计数不能占住预览发布/读取锁。
        roots=set(root_ids)
        if not roots:return {'total':0,'ready':0,'pending':0,'failed':0,'thumbnails_ready':0,'workers':WARM_WORKERS,'checking':self.reconcile_requested.is_set() or self.reconciling}
        with self.cache.connect() as c:
            where=''
            if self.catalog is not None:
                c.execute('ATTACH DATABASE ? AS source_catalog',(str(self.catalog.db),))
                where=" WHERE EXISTS(SELECT 1 FROM source_catalog.items WHERE root=archive_jobs.root_id AND online=1 AND pair_key=archive_jobs.pair_key)"
            where+=(' AND ' if where else ' WHERE ')+'root_id IN ('+','.join('?' for _ in roots)+')'
            rows=c.execute('SELECT root_id,state,count(*) AS count,sum(small_ready) AS small FROM archive_jobs INDEXED BY archive_job_progress'+where+' GROUP BY root_id,state',list(roots)).fetchall()
        state={'total':0,'ready':0,'pending':0,'failed':0,'thumbnails_ready':0,'workers':WARM_WORKERS}
        for row in rows:
            if row['root_id'] not in roots or row['state']=='cancelled':continue
            state['total']+=row['count'];state['thumbnails_ready']+=row['small']
            state['ready' if row['state']=='ready' else 'failed' if row['state']=='failed' else 'pending']+=row['count']
        state['checking']=self.reconcile_requested.is_set() or self.reconciling
        return state

    def ensure_session(self,session,records):
        with self.lock:
            # 后续增量由 Store 的提交回调排队，避免每次监控刷新重新查询整个目录的缓存。
            if session['id'] in self.sessions:return
            self.enqueue(session['root'],records);self.sessions.add(session['id'])

    def process_one(self):
        import media_engine as media
        with self.lock,self.cache.lock,self.cache.connect() as c:
            active=list(self.active)
            row=c.execute('SELECT * FROM pending INDEXED BY pending_request_order WHERE attempts<3'+
                (' AND identity NOT IN ('+','.join('?' for _ in active)+')' if active else '')+
                ' ORDER BY attempts,requested LIMIT 1',active).fetchone()
            if row is not None:self.active.add(row['identity'])
        if row is None:return False
        current_edge=row['edge'];record={}
        try:
            record=json.loads(row['record'])
            record['_background_preview']=True
            if self.closed.is_set():return False
            if record.get('_catalog'):
                if self.catalog is None:raise RuntimeError('归档预览服务尚未就绪')
                if record.get('_catalog_root') and not self.catalog.preview_source_current(record):
                    with self.cache.lock,self.cache.connect() as c:
                        c.execute('DELETE FROM pending WHERE identity=? AND record=?',(row['identity'],row['record']))
                        c.execute("UPDATE archive_jobs SET state='cancelled' WHERE identity=? AND record=?",(row['identity'],row['record']))
                    return True
            with _BACKGROUND_SLOTS:
                if self.closed.is_set():return False
                small=420 if record.get('_catalog') else 480
                if record.get('_catalog') and self.catalog is not None:
                    self.catalog.render_preview_batch(record,row['root'],self.exiftool)
                else:media.warm_previews(record,row['root'],self.cache.folder,self.exiftool,(small,MAX_PREVIEW_EDGE),cache_index=self.cache)
            with self.cache.lock,self.cache.connect() as c:
                c.execute('DELETE FROM pending WHERE identity=? AND record=? AND edge=?',(row['identity'],row['record'],current_edge))
                c.execute("UPDATE archive_jobs SET state='ready',small_ready=1 WHERE identity=? AND record=?",
                    (row['identity'],row['record']))
        except Exception as exc:
            with self.cache.lock,self.cache.connect() as c:
                c.execute('UPDATE pending SET attempts=attempts+1,error=? WHERE identity=? AND record=? AND edge=?',
                          (str(exc)[:250],row['identity'],row['record'],current_edge))
                c.execute("UPDATE archive_jobs SET state='failed' WHERE identity=? AND record=? AND EXISTS(SELECT 1 FROM pending WHERE identity=? AND attempts>=3)",
                    (row['identity'],row['record'],row['identity']))
                if record.get('_catalog') and self.catalog is not None:
                    index=cache_for(self.catalog.previews)
                    if index.contains([cache_name(record,420)]):
                        c.execute('UPDATE archive_jobs SET small_ready=1 WHERE identity=? AND record=?',(row['identity'],row['record']))
        finally:
            with self.lock:self.active.discard(row['identity'])
            self.wake.set()
        return True

    def _run(self):
        while not self.closed.is_set():
            self.wake.clear()
            try:
                if not self.process_one():self.wake.wait(1)
            except (OSError,sqlite3.Error):self.wake.wait(1)

    def close(self):
        self.closed.set();self.wake.set();self.reconcile_requested.set()
        until=time.monotonic()+3
        for thread in self.threads:
            if thread.is_alive():thread.join(timeout=max(0,until-time.monotonic()))
        if self.reconcile_thread.is_alive():self.reconcile_thread.join(timeout=max(0,until-time.monotonic()))
