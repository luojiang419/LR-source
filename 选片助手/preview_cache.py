"""持久预览索引和有界磁盘缓存；所有删除操作仅针对本目录中的哈希缓存文件。"""
from contextlib import contextmanager,ExitStack,nullcontext,closing
from collections import OrderedDict,deque
from itertools import islice
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from file_safety import delete_verified,publish_new,atomic_write
from preview_codec import RECIPE,MAX_PREVIEW_EDGE,validate_stream
from archive_resources import PREVIEW_WORKERS,MAX_PREVIEW_WORKERS

MAX_CACHE_BYTES=10*1024**3
_NAME=re.compile(r'[0-9a-f]{64}\.(?:jpg|webp)\Z')
_CACHES={}
_GUARD=threading.RLock()
_BUDGETS={}
_LOCATIONS={}
WARM_WORKERS=PREVIEW_WORKERS
MAX_WARM_WORKERS=MAX_PREVIEW_WORKERS
_BACKGROUND_SLOTS=threading.BoundedSemaphore(MAX_WARM_WORKERS)
_CONTENT_LOCKS=[threading.Lock() for _ in range(64)]
_MAX_PENDING_TOUCHES=4096


def cache_name(record,size,decode=False,*,version=RECIPE):
    return hashlib.sha256((record['fingerprint']+f':{size}:{decode}:{record.get("orientation",1)}:v{version}').encode()).hexdigest()+('.webp' if version==5 else '.jpg')


def registered_recipe(row):
    if row and _NAME.fullmatch(row['name']) and row['fingerprint'] and row['edge']:
        return next((v for v in (RECIPE,7,8,9,10,5,4,3) if cache_name({'fingerprint':row['fingerprint'],'orientation':row['orientation']},row['edge'],bool(row['decoded']),version=v)==row['name']),None)
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
        self.resizing=threading.Event()
        primary=PreviewCache(data/'previews',defer_maintenance=True)
        self.caches=[primary,PreviewCache(data/'catalog_previews',limit=primary.limit,defer_maintenance=True)]
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
            target_limit=self.limit if limit is None else limit
            total=sum(c.execute('SELECT bytes FROM usage WHERE id=1').fetchone()[0] for c in connections.values())
            old=connection.execute('SELECT bytes FROM previews WHERE name=?',(exclude,)).fetchone()
            if old:total-=old[0]
            if total+incoming<=target_limit:return True
            # 有容量时发布不应替前台访问记录刷盘；真正选 LRU 候选前再
            # 合并访问时间，既保留最近浏览图片，也避免后台发布反复抢写锁。
            for cache,c in connections.items():cache._flush_touches(c)
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
    def __init__(self,folder,limit=MAX_CACHE_BYTES,*,defer_maintenance=False):
        self.folder=Path(folder).resolve();self.folder.mkdir(parents=True,exist_ok=True)
        self.db=self.folder/'index.sqlite3';self.limit=limit;self.lock=threading.RLock();self.budget=None
        self._touches=OrderedDict();self._last_touch_flush=time.monotonic();self._read_leases={};self._touch_flushes={}
        self._lease_guard=threading.RLock();self._touch_guard=threading.RLock()
        self._connections=threading.local()
        with self.connect() as c:
            schema=c.execute('PRAGMA schema_version').fetchone()[0]
            try:ready=c.execute("SELECT value FROM configuration WHERE key='startup_schema_v3'").fetchone()
            except sqlite3.OperationalError:ready=None
            # schema_version 随真实 DDL 改变；普通缓存写入不使成熟库重复迁移。
            hints=c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='preview_hint_v1'").fetchone()
            if not ready or ready[0]!=str(schema) or not hints:self._initialize(c)
            saved=c.execute("SELECT value FROM configuration WHERE key='limit_gb'").fetchone()
            if saved:self.limit=int(saved[0])*1024**3
        if not defer_maintenance:self.reconcile()

    def _initialize(self,c):
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''CREATE TABLE IF NOT EXISTS previews(
            name TEXT PRIMARY KEY,bytes INTEGER NOT NULL,accessed INTEGER NOT NULL,
            fingerprint TEXT,edge INTEGER,decoded INTEGER,orientation INTEGER);
            CREATE INDEX IF NOT EXISTS preview_fingerprint ON previews(fingerprint);
            CREATE INDEX IF NOT EXISTS preview_hint_v1 ON previews(fingerprint,edge,decoded,orientation,name);
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
        if 'error' not in {r['name'] for r in c.execute('PRAGMA table_info(archive_jobs)')}:
            c.execute('ALTER TABLE archive_jobs ADD COLUMN error TEXT')
        c.executescript('''CREATE TABLE IF NOT EXISTS preview_redirects(name TEXT PRIMARY KEY,target TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS preview_redirect_removed AFTER DELETE ON previews BEGIN
                DELETE FROM preview_redirects WHERE target=OLD.name; END;''')
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
            CREATE TRIGGER IF NOT EXISTS portable_alias_added AFTER INSERT ON content_previews BEGIN
              INSERT INTO portable_names SELECT NEW.name WHERE EXISTS(SELECT 1 FROM previews WHERE name=NEW.name)
                AND NOT EXISTS(SELECT 1 FROM portable_names WHERE name=NEW.name); END;
            CREATE TRIGGER IF NOT EXISTS portable_alias_removed AFTER DELETE ON content_previews BEGIN
              DELETE FROM portable_names WHERE name=OLD.name AND NOT EXISTS(SELECT 1 FROM content_previews WHERE name=OLD.name); END;
            CREATE TRIGGER IF NOT EXISTS portable_alias_changed AFTER UPDATE OF name ON content_previews BEGIN
              DELETE FROM portable_names WHERE name=OLD.name AND NOT EXISTS(SELECT 1 FROM content_previews WHERE name=OLD.name);
              INSERT INTO portable_names SELECT NEW.name WHERE EXISTS(SELECT 1 FROM previews WHERE name=NEW.name)
                AND NOT EXISTS(SELECT 1 FROM portable_names WHERE name=NEW.name); END;
        ''')
        if not c.execute("SELECT 1 FROM configuration WHERE key='stats_v1'").fetchone():
            c.execute('INSERT OR IGNORE INTO portable_names SELECT DISTINCT p.name FROM content_previews p JOIN previews v ON v.name=p.name')
            c.execute("INSERT INTO configuration VALUES ('stats_v1','1')")
        for name,sql in self._portable_triggers().items():
            existing=c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).fetchone()
            normalize=lambda value:' '.join(value.replace('IF NOT EXISTS ','').split()).lower()
            if not existing or normalize(existing[0])!=normalize(sql):
                c.execute('DROP TRIGGER IF EXISTS '+name);c.execute(sql)
        schema=c.execute('PRAGMA schema_version').fetchone()[0]
        c.execute("INSERT OR REPLACE INTO configuration VALUES ('startup_schema_v3',?)",(str(schema),))

    @staticmethod
    def _portable_triggers():
        return {
            'portable_alias_added':"""CREATE TRIGGER portable_alias_added AFTER INSERT ON content_previews BEGIN
                INSERT INTO portable_names SELECT NEW.name WHERE EXISTS(SELECT 1 FROM previews WHERE name=NEW.name)
                  AND NOT EXISTS(SELECT 1 FROM portable_names WHERE name=NEW.name); END""",
            'portable_alias_changed':"""CREATE TRIGGER portable_alias_changed AFTER UPDATE OF name ON content_previews BEGIN
                DELETE FROM portable_names WHERE name=OLD.name AND NOT EXISTS(SELECT 1 FROM content_previews WHERE name=OLD.name);
                INSERT INTO portable_names SELECT NEW.name WHERE EXISTS(SELECT 1 FROM previews WHERE name=NEW.name)
                  AND NOT EXISTS(SELECT 1 FROM portable_names WHERE name=NEW.name); END""",
        }


    @contextmanager
    def connect(self):
        scope=getattr(self._connections,'scope',None)
        reuse=scope is not None and not scope['busy']
        c=scope['connection'] if reuse else None
        if c is None:
            c=sqlite3.connect(self.db,timeout=30);c.row_factory=sqlite3.Row
            # 仅可再生预览/队列索引使用 WAL NORMAL，业务数据库不受影响。
            try:
                c.execute('PRAGMA mmap_size=1073741824')
                c.execute('PRAGMA synchronous=NORMAL')
            except BaseException:c.close();raise
            if reuse:scope['connection']=c
        if reuse:scope['busy']=True
        try:
            # 每个调用仍独立提交或回滚，嵌套调用使用另一连接，不提前提交外层。
            with c:yield c
            flushed=self._touch_flushes.pop(c,None)
            if flushed:
                with self.lock,self._touch_guard:
                    # 只有事务确实提交才确认访问记录；保留刷写期间更新的访问。
                    for name,stamp in flushed.items():
                        if self._touches.get(name)==stamp:self._touches.pop(name,None)
                    self._last_touch_flush=time.monotonic()
        finally:
            self._touch_flushes.pop(c,None)
            if reuse:scope['busy']=False
            else:c.close()

    @contextmanager
    def read_connection(self):
        # 命中读取不设置写连接参数，也不等待全库队列/发布事务的 Python 锁。
        with closing(sqlite3.connect(self.db.as_uri()+'?mode=ro',uri=True,timeout=1)) as c:
            c.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,True)
            c.row_factory=sqlite3.Row;c.execute('PRAGMA mmap_size=1073741824')
            yield c

    @contextmanager
    def connection_scope(self):
        """仅单张预生成期间复用线程私有连接；退出即关闭，不建立长期连接池。"""
        if getattr(self._connections,'scope',None) is not None:
            yield;return
        scope={'connection':None,'busy':False};self._connections.scope=scope
        try:yield
        finally:
            del self._connections.scope
            if scope['connection'] is not None:scope['connection'].close()

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
        with self._lease_guard:return self._remove_unleased(c,name)

    def _remove_unleased(self,c,name):
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
            row=self._entry(c,name)
            if row is None:return None
            name=row['name']
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
            self._touch(name,c)
            return result

    def _read(self,name):
        """独立只读快照和短租约；命中响应不等待批量写入，也不刷访问时间。"""
        with self.read_connection() as c:
            row=self._entry(c,name)
        return self._read_entry(row) if row is not None else None

    def read_registered(self,name,min_edge=0):
        """已登记缩略图的只读快路；不取得写队列锁或探测原片。"""
        with self.read_connection() as c:
            row=self._entry(c,name)
            if row is not None and row['edge']<min_edge:
                row=c.execute('SELECT * FROM previews WHERE fingerprint=? AND orientation=? AND decoded=? AND edge>=? ORDER BY edge LIMIT 1',(row['fingerprint'],row['orientation'],row['decoded'],min_edge)).fetchone()
        return self._read_entry(row) if row is not None else None

    def _read_entry(self,row):
        name=row['name'];expected=(row['bytes'],row['digest'])
        if not self._registered(row) or not row['digest']:
            self._discard_invalid_read(name,expected);return None
        # 先登记租约，再做文件 I/O。一个慢文件不能占着租约锁阻塞其他图片。
        with self._lease_guard:self._read_leases[name]=self._read_leases.get(name,0)+1
        payload=None;valid=False
        try:
            path=self.folder/name
            if path.stat().st_size!=row['bytes']:raise ValueError('缓存不完整')
            with path.open('rb') as stream:
                validate_stream(stream,expected[0]);stream.seek(0);payload=stream.read()
            if hashlib.sha256(payload).hexdigest()!=expected[1]:raise ValueError('缓存校验失败')
            valid=True
        except (OSError,ValueError):
            pass
        finally:
            with self._lease_guard:
                remaining=self._read_leases.get(name,0)-1
                if remaining>0:self._read_leases[name]=remaining
                else:self._read_leases.pop(name,None)
        if valid:self._remember_touch(name)
        else:self._discard_invalid_read(name,expected)
        return payload if valid else None

    def input_bytes(self,record,minimum_edge):
        """模型复用足够清晰的同版本缓存；不进入生成、写锁或原图探测。"""
        with self.read_connection() as c:
            rows=c.execute('''SELECT * FROM previews WHERE fingerprint=? AND decoded=0 AND edge>=?
                '''+(' AND orientation=?' if 'orientation' in record else '')+' ORDER BY edge,name LIMIT 4',
                [record['fingerprint'],minimum_edge]+([record['orientation']] if 'orientation' in record else [])).fetchall()
        for row in rows:
            payload=self._read_entry(row)
            if payload is not None:return payload
        return None

    @staticmethod
    def _entry(connection,name):
        redirected=connection.execute('SELECT p.* FROM preview_redirects r JOIN previews p ON p.name=r.target WHERE r.name=?',(name,)).fetchone()
        return redirected or connection.execute('SELECT * FROM previews WHERE name=?',(name,)).fetchone()

    def _discard_invalid_read(self,name,expected):
        if not self.lock.acquire(blocking=False):return
        try:
            with self.connect() as c:
                current=c.execute('SELECT bytes,digest FROM previews WHERE name=?',(name,)).fetchone()
                if current and tuple(current)==expected:self._remove(c,name)
        finally:self.lock.release()

    def _remember_touch(self,name):
        with self._touch_guard:
            self._touches.pop(name,None);self._touches[name]=time.time_ns()
            while len(self._touches)>_MAX_PENDING_TOUCHES:self._touches.popitem(last=False)

    def flush_deferred_touches(self):
        if not self._touches or time.monotonic()-self._last_touch_flush<5:return
        if not self.lock.acquire(blocking=False):return
        try:
            with self.connect() as c:self._flush_touches(c,nonblocking=True)
        finally:self.lock.release()

    def _touch(self,name,connection):
        self._remember_touch(name)
        # 长期外部写锁下保留最近访问，避免读命中无限积压；此时采用近似 LRU。
        if len(self._touches)>=256 or time.monotonic()-self._last_touch_flush>=5:
            self._flush_touches(connection,nonblocking=True)

    def _flush_touches(self,connection,*,nonblocking=False):
        """命中只尝试零等待写入；淘汰前仍合并访问，提交成功后才清空缓冲。"""
        if not self._touches:return
        with self._touch_guard:touches=dict(self._touches)
        timeout=connection.execute('PRAGMA busy_timeout').fetchone()[0] if nonblocking else None
        if nonblocking:connection.execute('PRAGMA busy_timeout=0')
        connection.execute('SAVEPOINT preview_touches')
        try:
            connection.executemany('UPDATE previews SET accessed=max(accessed,?) WHERE name=?',
                [(stamp,name) for name,stamp in touches.items()])
            connection.execute('RELEASE preview_touches')
        except sqlite3.Error as error:
            connection.execute('ROLLBACK TO preview_touches');connection.execute('RELEASE preview_touches')
            if nonblocking and getattr(error,'sqlite_errorcode',0)&255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):return
            raise
        finally:
            if nonblocking:connection.execute(f'PRAGMA busy_timeout={timeout}')
        self._touch_flushes.setdefault(connection,{}).update(touches)

    def contains(self,names,connection=None):
        """入队只读检查，不能在队列写事务内另开连接更新访问时间。"""
        def check(c):
            for name in names:
                row=self._entry(c,name)
                try:
                    if row is None or not row['digest'] or not self._registered(row) or (self.folder/row['name']).stat().st_size!=row['bytes']:return False
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
                row=c.execute('SELECT name FROM content_previews WHERE content=? AND edge=? AND orientation=? AND decoded=? AND recipe IN (6,7,8,9,10) ORDER BY recipe DESC LIMIT 1',
                              (content,size,orientation,int(decode))).fetchone()
            if row:
                hit=cache.get(row[0])
                if hit is not None:return hit
        return None

    def publish(self,temp,target,record,size,decode):
        with self.lock,self.connect() as c:
            if target.parent.resolve()!=self.folder or target.name!=cache_name(record,size,decode):
                raise ValueError('预览只能写入专用缓存目录')
            count=temp.stat().st_size
            if count>self.limit:raise OSError('单张预览超过缓存容量上限')
            digest=self._digest(temp)
            canonical=target.name
            # v7–v10只作同名冲突恢复槽，不升级全库编码策略，不接管未知文件。
            for name in [canonical,*[cache_name(record,size,decode,version=v) for v in (7,8,9,10)]]:
                target=self.folder/name
                if target.exists() or target.is_symlink():
                    if self._owned(c,name):
                        if name!=canonical:c.execute('INSERT OR REPLACE INTO preview_redirects VALUES (?,?)',(canonical,name))
                        return target
                    continue
                self._trim(c,count,name)
                try:publish_new(temp,target)
                except FileExistsError:continue
                break
            else:raise OSError('缓存恢复槽均被未知文件占用，已保留原文件')
            c.execute('''INSERT INTO previews(name,bytes,accessed,fingerprint,edge,decoded,orientation,digest) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                      bytes=excluded.bytes,accessed=excluded.accessed,fingerprint=excluded.fingerprint,
                      edge=excluded.edge,decoded=excluded.decoded,orientation=excluded.orientation,digest=excluded.digest''',
                      (target.name,count,time.time_ns(),record['fingerprint'],size,int(decode),record.get('orientation',1),digest))
            if target.name!=canonical:c.execute('INSERT OR REPLACE INTO preview_redirects VALUES (?,?)',(canonical,target.name))
            return target

    def state(self):
        return self.budget.state() if self.budget else self.local_state()

    def local_state(self):
        with self.read_connection() as c:
            c.execute('BEGIN')
            totals=c.execute('SELECT * FROM cache_totals WHERE id=1').fetchone()
            size=c.execute('SELECT bytes FROM usage WHERE id=1').fetchone()[0]
        return {'bytes':size,'limit_bytes':self.limit,'images':totals['images'],'pending':totals['pending'],'failed':totals['failed']}

    def retire_legacy(self,*,limit=None,after='',preserved_before=0):
        """只淘汰已登记且仍匹配原哈希的旧生成图，不遍历或接管未知文件。"""
        if any(p.is_symlink() or p.is_junction() for p in (self.folder,*self.folder.parents)):
            raise ValueError('旧预览清理路径不能经过链接')
        removed=0;freed=0;preserved=0
        with self.lock,self.connect() as c:
            rows=c.execute('SELECT * FROM previews WHERE name>? ORDER BY name'+(' LIMIT ?' if limit else ''),
                           (after,limit) if limit else (after,)).fetchall()
            for row in rows:
                recipe=registered_recipe(row)
                if recipe is None or recipe in (RECIPE,7,8,9,10):continue
                path=self.folder/row['name']
                with self._lease_guard:
                    if self._read_leases.get(row['name']):preserved+=1;continue
                    if path.exists():
                        if not row['digest'] or path.is_symlink() or path.is_junction():preserved+=1;continue
                        try:deleted=delete_verified(path,row['digest'])
                        except OSError:preserved+=1;continue
                        if not deleted:preserved+=1;continue
                        removed+=1;freed+=row['bytes']
                    c.execute('DELETE FROM previews WHERE name=?',(row['name'],))
            complete=not limit or len(rows)<limit
            if complete:
                if preserved or preserved_before:c.execute("DELETE FROM configuration WHERE key='preview_policy'")
                else:c.execute("INSERT OR REPLACE INTO configuration VALUES ('preview_policy','embedded-jpeg-2048-v6')")
        result={'removed_files':removed,'freed_bytes':freed,'preserved_files':preserved}
        if limit:result.update(complete=complete,cursor=rows[-1]['name'] if rows else after)
        return result


class PreviewWarmer:
    """持久并行队列；每张一次读取完成小图和最高 2048 预览。"""
    def __init__(self,cache,exiftool,*,autostart=True,catalog=None):
        self.cache=cache;self.exiftool=exiftool;self.closed=threading.Event();self.wake=threading.Event()
        self.catalog=catalog;self.sessions=set();self.lock=threading.RLock();self.active=set();self.foreground_until=0.0;self.foreground_active=0;self.foreground_guard=threading.Lock()
        self.control_lock=threading.RLock()
        self.decode_samples=deque(maxlen=512);self.timing_lock=threading.Lock()
        self.completed_by_root={}
        # 本轮修复只对旧失败执行一次恢复；正常重启仍保留失败上限与手动暂停。
        with cache.connect() as c:
            if not c.execute("SELECT 1 FROM configuration WHERE key='design-preview-repair-v1'").fetchone():
                c.execute("UPDATE pending SET attempts=0,error=NULL WHERE attempts>=3 AND json_extract(record,'$._catalog')=1")
                c.execute("UPDATE archive_jobs SET state='queued',error=NULL WHERE state='failed'")
                c.execute("INSERT INTO configuration VALUES ('design-preview-repair-v1','1')")
        with cache.read_connection() as c:
            speed=c.execute("SELECT value FROM configuration WHERE key='archive_full_speed'").fetchone()
            requested=c.execute("SELECT value FROM configuration WHERE key='archive_workers'").fetchone()
            paused=c.execute("SELECT value FROM configuration WHERE key='archive_decode_paused'").fetchone()
        self.full_speed=bool(speed and speed[0]=='1')
        self.manual_paused=bool(paused and paused[0]=='1')
        self.requested_workers=min(MAX_WARM_WORKERS,max(1,int(requested[0]))) if requested else WARM_WORKERS
        # 控制配置独立于图片/队列数据库，不能让暂停等待发布写锁或检查点。
        self.controls_path=Path(getattr(catalog,'data',cache.folder.parent))/'archive-decode-controls.json'
        try:controls=json.loads(self.controls_path.read_text(encoding='utf8'))
        except FileNotFoundError:controls=None
        self.fill_missing_requested=False
        if controls is not None:
            if controls.get('schema')!='framekeep.archive-decode-controls' or controls.get('version')!=1 or type(controls.get('full_speed')) is not bool or type(controls.get('manual_paused')) is not bool or type(controls.get('workers')) is not int or controls['workers']<1:raise ValueError('解码控制配置无效')
            self.full_speed=controls['full_speed'];self.manual_paused=controls['manual_paused']
            self.requested_workers=min(MAX_WARM_WORKERS,controls['workers'])
            self.fill_missing_requested=controls.get('fill_missing_requested',False) is True
        self.started=False
        self.resize_guard=threading.Lock();self.resize_requested=threading.Event()
        self.resize_thread=threading.Thread(target=self._resize_run,name='preview-worker-resize',daemon=True)
        self.scheduler=None;self.discovering=lambda:False
        self.phase={'checking':True,'pending':0,'failed':0,'ready':False}
        self.phase_thread=threading.Thread(target=self._phase_run,name='preview-phase',daemon=True)
        self.interactive=lambda:False
        self.requests=OrderedDict();self.request_lock=threading.Lock();self.request_wake=threading.Event()
        self.request_thread=threading.Thread(target=self._requests_run,name='foreground-preview-queue',daemon=True)
        self.reconcile_lock=threading.Lock();self.reconcile_requested=threading.Event();self.reconcile_videos=True;self.reconcile_full=False;self.reconciling=False
        self.reconcile_active_all=False;self.reconcile_scope=None;self.scan_progress={'checked':0,'queued':0}
        if self.fill_missing_requested:
            self.reconcile_full=True;self.reconcile_videos=False;self.reconcile_requested.set()
        self.reconcile_error=''
        self.reconcile_thread=threading.Thread(target=self._reconcile,name='archive-preview-reconcile',daemon=True)
        # 旧队列的 edge 仅用于领取结果的版本校验；实际生成尺寸统一在
        # process_one 中使用 MAX_PREVIEW_EDGE，无需启动时扫描、改写全库。
        self.threads=[threading.Thread(target=self._run,name=f'preview-cache-{i}',daemon=True) for i in range(WARM_WORKERS)]
        self.thread=self.threads[0]
        self.design_repair_lock=threading.Lock();self.design_repair_thread=None
        self.design_repair={'id':'','status':'idle','checked':0,'queued':0,'cached':0}
        self.coverage=None
        if catalog is not None and hasattr(catalog,'db'):
            from archive_cache_coverage import ArchiveCacheCoverage
            self.coverage=ArchiveCacheCoverage(cache,catalog)
        if autostart:self.start()

    def start(self):
        if getattr(self.catalog,'visits',None):
            self.started=True
            return
        desired=self.worker_limit()
        with self.request_lock:
            if self.closed.is_set():return
            self.started=True
            self._grow_workers_locked(desired)
            for thread in [self.request_thread,*self.threads[:WARM_WORKERS]]:
                if thread.ident is None:thread.start()
            if self.resize_thread.ident is None:self.resize_thread.start()
            self.resize_requested.set()
            if self.scheduler and self.phase_thread.ident is None:self.phase_thread.start()
        if self.catalog:self.repair_design_thumbnails(automatic=True)
        # 变化日志也包含移除后重新上线的素材；持续确认其实际缓存，
        # 不能只依赖服务启动时额外调用一次reconcile_archive。
        if self.catalog and not self.reconcile_thread.is_alive():self.reconcile_thread.start()

    def design_repair_state(self):
        with self.design_repair_lock:return dict(self.design_repair,paused=self.manual_paused)

    def repair_design_thumbnails(self,*,automatic=False):
        if self.catalog is None:raise ValueError('归档服务尚未就绪')
        with self.design_repair_lock:
            if self.closed.is_set():raise ValueError('预览服务正在关闭')
            if self.design_repair_thread and self.design_repair_thread.is_alive():return dict(self.design_repair)
            if automatic:
                with self.cache.read_connection() as c:
                    if c.execute("SELECT 1 FROM configuration WHERE key='design-thumbnail-seed-v1'").fetchone():return dict(self.design_repair)
            self.design_repair={'id':str(time.time_ns()),'status':'running','checked':0,'queued':0,'cached':0}
            self.design_repair_thread=threading.Thread(target=self._repair_design_thumbnails,name='design-thumbnail-repair',daemon=True)
            self.design_repair_thread.start();return dict(self.design_repair)

    def _repair_design_thumbnails(self):
        after=None
        try:
            index=cache_for(self.catalog.previews)
            while not self.closed.is_set():
                with self.catalog.connect() as c:
                    clause=' AND (modified<? OR (modified=? AND id>?))' if after else ''
                    values=(after[0],after[0],after[1]) if after else ()
                    rows=[dict(row) for row in c.execute("SELECT id,path,fingerprint,pair_key,root,media_type,modified FROM items INDEXED BY catalog_media_page WHERE online=1 AND media_type='design' AND extension IN ('.psd','.psb') AND root IN (SELECT id FROM roots WHERE removed=0)"+clause+" ORDER BY modified DESC,id LIMIT 128",values)]
                if not rows:break
                after=(rows[-1]['modified'],rows[-1]['id'])
                for row in rows:
                    if self.closed.is_set():break
                    root,record=self.catalog.preview_record(row,[row]);record['_source_root']=root
                    with index.budget.generation() if index.budget else nullcontext():hit=index.get(cache_name(record,420),read=True)
                    if hit is None:
                        identity=str(Path(root).absolute()).casefold()+':'+record['id']
                        with self.cache.lock,self.cache.connect() as c:
                            c.execute("UPDATE archive_jobs SET state='queued',error=NULL WHERE identity=? AND state IN ('failed','ignored')",(identity,))
                            c.execute('UPDATE pending SET attempts=0,error=NULL WHERE identity=? AND attempts>=3',(identity,))
                        self.enqueue(root,[record])
                    with self.design_repair_lock:
                        self.design_repair['checked']+=1;self.design_repair['cached' if hit is not None else 'queued']+=1
            if not self.closed.is_set():
                with self.cache.connect() as c:c.execute("INSERT OR REPLACE INTO configuration VALUES ('design-thumbnail-seed-v1','1')")
            with self.design_repair_lock:self.design_repair['status']='cancelled' if self.closed.is_set() else 'completed'
        except Exception as error:
            with self.design_repair_lock:self.design_repair.update(status='failed',error=str(error)[:250])

    def attach_scheduler(self,scheduler,discovering=lambda:False):
        self.scheduler=scheduler;self.discovering=discovering
        self.reconcile_requested.set();scheduler.order_previews(True)

    def _grow_workers_locked(self,desired):
        while len(self.threads)<desired:
            self.threads.append(threading.Thread(target=self._run,name=f'preview-cache-{len(self.threads)}',daemon=True))

    def set_full_speed(self,enabled,workers=None):
        with self.control_lock:self._set_full_speed(enabled,workers)

    def _set_full_speed(self,enabled,workers):
        if type(enabled) is not bool:raise ValueError('解码速度开关无效')
        desired=MAX_WARM_WORKERS if workers=='max' else self.requested_workers if workers is None else workers
        if type(desired) is not int or not 1<=desired<=MAX_WARM_WORKERS:raise ValueError(f'解码并发须为 1–{MAX_WARM_WORKERS}')
        if (self.full_speed,self.requested_workers)!=(enabled,desired):
            self._save_controls(full_speed=enabled,workers=desired)
            self.requested_workers=desired;self.full_speed=enabled
        self._ensure_workers()
        self.wake.set()

    def _save_controls(self,**changes):
        value={'schema':'framekeep.archive-decode-controls','version':1,'full_speed':self.full_speed,
               'manual_paused':self.manual_paused,'workers':self.requested_workers,'fill_missing_requested':self.fill_missing_requested,**changes}
        atomic_write(self.controls_path,json.dumps(value).encode('utf8'))

    def _ensure_workers(self):
        # 构造线程对象不等待 OS 启动；全速/继续的回执不能同步创建上百个
        # 原生线程，也不能持有前台预览请求锁进行扩容。
        with self.resize_guard:self._grow_workers_locked(self.worker_limit())
        if self.started and not self.closed.is_set():self.resize_requested.set()

    def _start_pending_worker(self,thread):thread.start()

    def _resize_run(self):
        while not self.closed.is_set():
            self.resize_requested.wait(.5);self.resize_requested.clear()
            with self.resize_guard:pending=[thread for thread in self.threads if thread.ident is None]
            for thread in pending:
                if self.closed.is_set() or self.manual_paused:break
                self._start_pending_worker(thread)

    def worker_limit(self):
        if self.manual_paused:return 0
        # 用户明确选择的全速不受 WAL 体积或常驻监控开关限制。
        # 真正的前台操作由 foreground() 短暂暂停领取，结束后自动恢复。
        if self.full_speed:return self.requested_workers
        maintenance=getattr(self.catalog,'wal_maintenance',None)
        if maintenance is not None and maintenance.pressure.is_set():
            return min(2,WARM_WORKERS)
        return WARM_WORKERS if not self.interactive() else min(2,WARM_WORKERS)

    def set_paused(self,paused):
        with self.control_lock:self._set_paused(paused)

    def _set_paused(self,paused):
        if type(paused) is not bool:raise ValueError('解码暂停开关无效')
        if self.manual_paused==paused:return
        # 和领取共用锁：返回后不再领取新任务，已领取的文件正常收尾。
        self._save_controls(manual_paused=paused)
        with self.lock:
            self.manual_paused=paused
            with self.timing_lock:self.decode_samples.clear()
        if not paused:self._ensure_workers()
        self.wake.set();self.request_wake.set()

    def refresh_phase(self):
        from contextlib import closing
        with closing(sqlite3.connect(self.cache.db.as_uri()+'?mode=ro',uri=True,timeout=1)) as c:
            c.execute('PRAGMA mmap_size=1073741824')
            pending,failed=c.execute('SELECT pending,failed FROM cache_totals WHERE id=1').fetchone()
        if not pending and failed and self.catalog:
            # 现场旧失败或已经移除的归档不能永久阻挡模型阶段。
            with self.catalog.connect() as c:roots=[row[0] for row in c.execute('SELECT id FROM roots WHERE removed=0')]
            failed=self.archive_state(roots)['failed']
        checking=bool(self.reconcile_requested.is_set() or self.reconciling or self.reconcile_error or self.discovering() or self.requests or self.coverage and not self.coverage.complete)
        if self.coverage and not self.coverage.complete:self.coverage.start()
        ready=not(checking or pending or failed or self.active)
        self.phase={'checking':checking,'pending':pending,'failed':failed,'ready':ready}
        if self.scheduler:self.scheduler.order_previews(not ready)
        return dict(self.phase)

    def _phase_run(self):
        while not self.closed.is_set():
            try:
                self.refresh_phase()
                if self.full_speed and self.phase['ready']:self.set_full_speed(False)
            except (OSError,sqlite3.Error):
                self.phase={**self.phase,'ready':False,'checking':True}
                self.scheduler.order_previews(True)
            for cache in self.cache.budget.caches if self.cache.budget else (self.cache,):
                try:cache.flush_deferred_touches()
                except (OSError,sqlite3.Error):pass  # 访问时间写入失败保留缓冲，不改变解码完成状态。
            self.closed.wait(.5)

    def request(self,root,records):
        if getattr(self.catalog,'visits',None):return
        """前台只递交可再生预览请求，不等待缓存写锁、磁盘检查或持久队列。"""
        root=str(root);root_key=root.casefold()
        with self.request_lock:
            if self.closed.is_set():return
            for record in records:
                key=(root_key,record['id'])
                self.requests[key]=(root,dict(record));self.requests.move_to_end(key)
            # 保留最近浏览的页面；淘汰的只是预热意图，照片和持久缓存任务不受影响。
            while len(self.requests)>4096:self.requests.popitem(last=False)
        self.request_wake.set()

    def defer_background(self,seconds=.4):
        # 前台新图期间暂停领取后续历史任务；已在生成的任务正常收尾。
        self.foreground_until=max(self.foreground_until,time.monotonic()+seconds)

    @contextmanager
    def foreground(self):
        with self.foreground_guard:self.foreground_active+=1
        try:yield
        finally:
            with self.foreground_guard:
                self.foreground_active-=1;self.defer_background(.1)

    def _requests_run(self):
        while not self.closed.is_set():
            self.request_wake.wait(1);self.request_wake.clear()
            if self.manual_paused or self.foreground_active or time.monotonic()<self.foreground_until:
                self.closed.wait(.05);self.request_wake.set();continue
            with self.request_lock:
                batch=[self.requests.popitem(last=False) for _ in range(min(128,len(self.requests)))]
            if not batch:continue
            grouped={}
            for _,(root,record) in batch:grouped.setdefault(root,[]).append(record)
            try:
                for root,records in grouped.items():
                    if self.closed.is_set():return
                    self.enqueue(root,records)
            except (OSError,sqlite3.Error):
                with self.request_lock:
                    for key,value in batch:
                        if key not in self.requests:self.requests[key]=value
                    while len(self.requests)>4096:self.requests.popitem(last=False)
                self.closed.wait(.5)
            self.request_wake.set()

    def enqueue(self,root,records):
        if getattr(self.catalog,'visits',None):return False
        if self.scheduler:self.scheduler.order_previews(True)
        root_key=str(Path(root).absolute()).casefold()
        records=iter(records)
        queued=0
        while batch:=list(islice(records,16)):
            # 恢复可在批次之间暂停；不持有缓存锁等待，也不丢弃仍待恢复的条目。
            if threading.current_thread() is self.reconcile_thread and self._wait_for_background():return False
            queued+=self._enqueue_batch(root,root_key,batch) or 0
        if queued:self.wake.set()
        return True

    def _enqueue_batch(self,root,root_key,records):
        queued=0
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
            aliases=[connection.execute('SELECT name FROM content_previews WHERE content=? AND edge=? AND orientation=? AND decoded=0 AND recipe IN (6,7,8,9,10) ORDER BY recipe DESC LIMIT 1',
                (content[0],edge,record.get('orientation',1))).fetchone() for edge in (small,MAX_PREVIEW_EDGE)]
            return tuple(bool(row) and index.contains([row[0]],connection) for row in aliases)
        # 缺缓存的预检不持有全局写锁，一批复用连接，不能让恢复全库队列堵住前台。
        plans=[]
        with ExitStack() as readers:
            connections={}
            for record in records:
                index=cache_for(self.cache.folder.parent/'catalog_previews') if record.get('_catalog') else self.cache
                if index not in connections:connections[index]=readers.enter_context(index.read_connection())
                plans.append((record,index,complete(record,index,connections[index])))
        # 已有缓存且登记未变时不进入写事务，也不唤醒空闲线程争抢缓存锁。
        if plans and all(all(plan[2]) and plan[0].get('_catalog_root') for plan in plans):
            with self.cache.read_connection() as c:
                unchanged=all((row:=c.execute('SELECT record,state,small_ready FROM archive_jobs WHERE identity=?',
                    (root_key+':'+record['id'],)).fetchone()) and row['state']=='ready' and row['small_ready'] and
                    row['record']==json.dumps(record,ensure_ascii=False,sort_keys=True) for record,_,_ in plans)
            if unchanged:
                self._record_scan_batch(len(records),0);return 0
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
                        state=CASE WHEN archive_jobs.record=excluded.record AND archive_jobs.state IN ('failed','ignored') AND excluded.state!='ready' THEN archive_jobs.state ELSE excluded.state END,
                        error=CASE WHEN archive_jobs.record=excluded.record AND excluded.state!='ready' THEN archive_jobs.error ELSE NULL END,
                        small_ready=excluded.small_ready,pair_key=excluded.pair_key WHERE archive_jobs.record!=excluded.record OR (excluded.state='ready')!=(archive_jobs.state='ready') OR archive_jobs.small_ready!=excluded.small_ready OR archive_jobs.state='cancelled' ''',
                        (identity,record['_catalog_root'],payload,'ready' if ready else 'queued',int(small_ready),record.get('_catalog_pair','')))
                if ready:
                    c.execute('DELETE FROM pending WHERE identity=?',(identity,));continue
                if record.get('_catalog_root') and c.execute("SELECT 1 FROM archive_jobs WHERE identity=? AND record=? AND state='ignored'",(identity,payload)).fetchone():
                    c.execute('DELETE FROM pending WHERE identity=?',(identity,));continue
                inserted=c.execute('''INSERT INTO pending(identity,root,record,requested,edge) VALUES (?,?,?,?,?)
                    ON CONFLICT(identity) DO UPDATE SET root=excluded.root,record=excluded.record,
                    requested=excluded.requested,edge=excluded.edge,attempts=0,error=NULL WHERE pending.record!=excluded.record''',
                    (identity,str(root),payload,time.time_ns(),large if small_ready else small))
                queued+=int(inserted.rowcount>0 and bool(c.execute('SELECT 1 FROM pending WHERE identity=? AND attempts<3',(identity,)).fetchone()))
        self._record_scan_batch(len(records),queued)
        return queued

    def _record_scan_batch(self,checked,queued):
        if threading.current_thread() is self.reconcile_thread and self.reconcile_scope=='archive-scan-all-v1':
            self.scan_progress={'checked':self.scan_progress['checked']+checked,'queued':self.scan_progress['queued']+queued}

    def _queue_full_previews(self,resume,checkpoint):
        # 先从轻量登记筛出没有两档图片索引的候选，避免先花数小时检查8万份
        # 已有缓存。候选仍按当前素材重读并验证，有效缓存不会重新生成。
        if self.coverage and (not resume or resume.get('stage')!='verify'):
            if not resume or resume.get('stage')!='missing':
                with self.cache.read_connection() as c:upper=c.execute('SELECT COALESCE(max(rowid),0) FROM archive_cache_members').fetchone()[0]
                resume={'stage':'missing','missing_after':0,'missing_upper':upper,'verify':resume}
                checkpoint(resume)
            after=resume['missing_after']
            while not self._wait_for_background():
                with self.cache.read_connection() as c:
                    c.execute('ATTACH DATABASE ? AS image_cache',(str(self.coverage.images.db),))
                    c.execute('PRAGMA image_cache.mmap_size=1073741824')
                    rows=c.execute('''SELECT m.rowid,m.pair_key FROM archive_cache_members m
                        WHERE m.rowid>? AND m.rowid<=? AND m.state='ready' AND NOT EXISTS(
                          SELECT 1 FROM image_cache.previews small JOIN image_cache.previews large
                          ON large.fingerprint=small.fingerprint AND large.orientation IS small.orientation
                          AND large.edge=2048 AND large.decoded=0 AND large.digest IS NOT NULL
                          WHERE small.fingerprint=m.fingerprint AND small.edge=420 AND small.decoded=0 AND small.digest IS NOT NULL)
                        ORDER BY m.rowid LIMIT 128''',(after,resume['missing_upper'])).fetchall()
                if not rows:break
                if self.catalog.queue_previews({row['pair_key'] for row in rows}) is False:return False
                after=rows[-1]['rowid'];resume={**resume,'missing_after':after};checkpoint(resume)
            if self.closed.is_set():return False
            verify=resume.get('verify')
            # 即使在两阶段交接时退出，也保留已完成的优先扫描游标。
            checkpoint({**resume,'missing_after':resume['missing_upper']})
            resume=verify
        def verify_checkpoint(value):checkpoint({**value,'stage':'verify'})
        return self.catalog.queue_all_previews(self._wait_for_background,resume=resume,checkpoint=verify_checkpoint)

    def reconcile_archive(self,*,videos_only=False,full=False):
        if getattr(self.catalog,'visits',None):return
        if self.catalog is None:return
        if self.scheduler:self.scheduler.order_previews(True)
        with self.reconcile_lock:
            # 重复点击只恢复当前准备过程，不在完成后再排一次全库核对。
            if full and not videos_only:
                if self.reconcile_active_all or self.reconcile_full and not self.reconcile_videos:return
                if not self.fill_missing_requested:
                    with self.control_lock:
                        self._save_controls(fill_missing_requested=True);self.fill_missing_requested=True
                    self.scan_progress={'checked':0,'queued':0}
            self.reconcile_videos=self.reconcile_videos and videos_only
            # 视频封面帧变更没有文件系统事件，必须在视频范围内主动核对。
            self.reconcile_full=self.reconcile_full or full or videos_only
            self.reconcile_requested.set()
            if not self.reconcile_thread.is_alive():
                self.reconcile_thread=threading.Thread(target=self._reconcile,name='archive-preview-reconcile',daemon=True)
                self.reconcile_thread.start()

    def _restore_archive_jobs(self):
        """重启仅恢复已持久化的未完成任务，不遍历已完成的归档素材。"""
        if not hasattr(self.catalog,'connect'):
            # 仅用于未挂载归档索引的兼容运行时，无法判断持久队列版本。
            return True
        with self.catalog.connect() as source:
            roots=[row[0] for row in source.execute('SELECT id FROM roots WHERE removed=0')]
            indexed=source.execute('SELECT 1 FROM items WHERE online=1 AND root IN ('+','.join('?' for _ in roots)+') LIMIT 1',roots).fetchone() if roots else None
        if not roots:return
        with self.cache.read_connection() as c:
            known=c.execute('SELECT 1 FROM archive_jobs WHERE root_id IN ('+','.join('?' for _ in roots)+') LIMIT 1',roots).fetchone()
        if indexed and not known:return True
        # 已持久入队的条目无需再写。缺失项按短只读快照分页恢复，
        # 不能拿着缓存写锁遍历整个未完成队列或等待归档随机读取。
        while not self._wait_for_background():
            with self.cache.read_connection() as c:
                c.execute('ATTACH DATABASE ? AS source_catalog',(str(self.catalog.db),))
                c.execute('PRAGMA source_catalog.mmap_size=2147418112')
                rows=c.execute('''SELECT identity,record,small_ready FROM archive_jobs
                    WHERE state='queued' AND root_id IN ('''+','.join('?' for _ in roots)+''')
                    AND NOT EXISTS(SELECT 1 FROM pending WHERE pending.identity=archive_jobs.identity AND pending.record=archive_jobs.record)
                    AND EXISTS(SELECT 1 FROM source_catalog.items
                        WHERE root=archive_jobs.root_id AND pair_key=archive_jobs.pair_key AND online=1)
                    LIMIT 128''',roots).fetchall()
            if not rows:break
            with self.cache.lock,self.cache.connect() as c:
                for row in rows:
                    record=json.loads(row['record'])
                    c.execute('''INSERT INTO pending(identity,root,record,requested,edge)
                        SELECT identity,?,record,?,? FROM archive_jobs WHERE identity=? AND record=? AND state='queued'
                        ON CONFLICT(identity) DO UPDATE SET root=excluded.root,record=excluded.record,edge=excluded.edge
                        WHERE pending.record!=excluded.record''',
                        (record.get('_source_root',''),time.time_ns(),MAX_PREVIEW_EDGE if row['small_ready'] else 420,row['identity'],row['record']))
            self.wake.set()
        return False

    def _reconcile(self):
        while not self.closed.is_set():
            self.reconcile_requested.wait(1)
            with self.reconcile_lock:
                requested=self.reconcile_requested.is_set()
                if requested:self.reconciling=True
                videos_only=self.reconcile_videos;full=self.reconcile_full;self.reconcile_videos=True;self.reconcile_full=False;self.reconcile_requested.clear()
                if full and not videos_only:self.reconcile_active_all=True
            try:
                with self.cache.read_connection() as c:
                    scans={row['key']:json.loads(row['value']) for row in c.execute("SELECT key,value FROM configuration WHERE key IN ('archive-scan-all-v1','archive-scan-video-v1')")}
                if 'archive-scan-all-v1' in scans:
                    with self.reconcile_lock:self.reconcile_active_all=True
                changes=self.catalog.pending_preview_changes() if hasattr(self.catalog,'pending_preview_changes') else set()
                if not (requested or scans or changes):continue
                self.reconciling=True
                if not self._wait_for_background():
                    bootstrap=self._restore_archive_jobs() if requested else False
                    if full or bootstrap:scans.setdefault('archive-scan-video-v1' if videos_only else 'archive-scan-all-v1',None)
                    for key,resume in scans.items():
                        self.reconcile_scope=key
                        if key=='archive-scan-all-v1':
                            self.scan_progress={'checked':(resume or {}).get('checked',0),'queued':(resume or {}).get('queued',0)}
                        def checkpoint(value):
                            if key=='archive-scan-all-v1':value={**value,**self.scan_progress}
                            with self.cache.lock,self.cache.connect() as c:c.execute('INSERT OR REPLACE INTO configuration VALUES (?,?)',(key,json.dumps(value)))
                        completed=self._queue_full_previews(resume,checkpoint) if key=='archive-scan-all-v1' else self.catalog.queue_all_previews(self._wait_for_background,videos_only=True,resume=resume,checkpoint=checkpoint)
                        if completed:
                            if key=='archive-scan-all-v1' and self.fill_missing_requested:
                                with self.control_lock:
                                    self._save_controls(fill_missing_requested=False);self.fill_missing_requested=False
                            with self.cache.lock,self.cache.connect() as c:c.execute('DELETE FROM configuration WHERE key=?',(key,))
                        self.reconcile_scope=None
                    if changes and not self._wait_for_background():
                        changes=self.catalog.pending_preview_changes()
                        if changes:self.catalog.queue_previews(changes)
                    self.reconcile_error=''
            except (OSError,sqlite3.Error) as error:
                self.reconcile_error=str(error)[:300]
                # 没有扫描断点时（例如恢复队列失败）也要自动重试，不能静默停住。
                if requested:
                    with self.reconcile_lock:
                        self.reconcile_videos=self.reconcile_videos and videos_only
                        self.reconcile_full=self.reconcile_full or full;self.reconcile_requested.set()
                self.closed.wait(1)
            finally:
                self.reconcile_scope=None
                with self.reconcile_lock:self.reconciling=False;self.reconcile_active_all=False

    def _wait_for_background(self):
        """作为恢复扫描的 stop 检查；手动暂停或前台期间等待，关闭时立即终止。"""
        while not self.closed.is_set():
            if not self.manual_paused and not self.foreground_active and time.monotonic()>=self.foreground_until:return False
            self.closed.wait(.02)
        return True

    def archive_state(self,root_ids=None,*,persisted=False):
        if root_ids is None:
            with self.coverage.source_connection() as c:root_ids=[row[0] for row in c.execute('SELECT id FROM roots WHERE removed=0')]
        roots=set(root_ids)
        if not roots:return self._decode_timing({'total':0,'ready':0,'pending':0,'failed':0,'ignored':0,'uncached':0,'thumbnails_ready':0},roots)
        state=self.coverage.snapshot(roots)
        return self._decode_timing(dict(state,workers=WARM_WORKERS),roots)

    def _decode_timing(self,state,roots):
        # 只用本进程真实发布的完整预览测速；缓存命中和目录扫描不算解码样本。
        # 使用墙钟吞吐，已包含并行收益，不能再次乘以 worker 数。
        now=time.monotonic()
        state['checking']=bool(self.reconcile_requested.is_set() or self.reconciling or self.reconcile_error or state.get('coverage_initialized') is False)
        with self.timing_lock:
            if roots is None:roots=set(self.completed_by_root)
            samples=[(start,end) for root,start,end in self.decode_samples if root in roots and end>now-60]
            completed=sum(self.completed_by_root.get(root,0) for root in roots)
        paused=bool(self.manual_paused or self.foreground_active or now<self.foreground_until)
        elapsed=now-max(now-60,min(start for start,_ in samples)) if samples else 0
        # 短暂让路期间在途任务仍在完成，墙钟吞吐已包含这些停顿；
        # 不因某次前台读取隐藏真实估算，仅手动暂停时停止显示剩余时间。
        rate=len(samples)/elapsed if len(samples)>=2 and elapsed>0 and not self.manual_paused else None
        state.update(decode_rate=rate,eta_seconds=math.ceil(state['pending']/rate) if state['pending'] and rate else None if state['pending'] else 0,paused=paused)
        state.update(completed_this_run=completed,active_workers=len(self.active),effective_workers=self.worker_limit(),full_speed=self.full_speed,
                     maximum_workers=MAX_WARM_WORKERS,cpu_workers=WARM_WORKERS,manual_paused=self.manual_paused,reconcile_error=self.reconcile_error)
        state['fill_scan']={'active':self.fill_missing_requested,**self.scan_progress}
        return state

    def ignored_reason(self,record,root):
        from preview_errors import PreviewIgnored
        identity=str(Path(root).absolute()).casefold()+':'+record['id']
        with self.cache.read_connection() as c:
            row=c.execute("SELECT error FROM archive_jobs WHERE identity=? AND state='ignored' AND json_extract(record,'$.fingerprint')=?",(identity,record['fingerprint'])).fetchone()
        if row and row[0]:
            code,_,reason=row[0].partition(':');return PreviewIgnored(code,reason)
        return None

    def ensure_session(self,session,records):
        with self.lock:
            # 后续增量由 Store 的提交回调排队，避免每次监控刷新重新查询整个目录的缓存。
            if session['id'] in self.sessions:return
            self.sessions.add(session['id'])
        try:self.enqueue(session['root'],records)
        except BaseException:
            with self.lock:self.sessions.discard(session['id'])
            raise

    def process_one(self,*,wait_for_cache=True):
        import media_engine as media
        if self.scheduler and self.scheduler.background_blocked():return False
        if self.cache.budget and self.cache.budget.resizing.is_set():return False
        if self.manual_paused or self.foreground_active or time.monotonic()<self.foreground_until:return False
        limit=self.worker_limit()
        with self.lock:
            # 拍摄期间只让两项历史预生成在途，避免全部 CPU/缓存写锁挤占当前图。
            if len(self.active)>=limit:return False
        with ExitStack() as stack:
            # 大量空闲工作槽不能持有领取锁等待缓存锁，挤在用户暂停之前。
            # 未领取任何项时立即让路，沿用唤醒/轮询机制重试。
            if not self.cache.lock.acquire(blocking=wait_for_cache):return None
            stack.callback(self.cache.lock.release)
            c=stack.enter_context(self.cache.connect())
            # 领取前重新确认前台和暂停状态，不能继续增加历史解码。
            if self.cache.budget and self.cache.budget.resizing.is_set():return False
            if self.closed.is_set() or self.manual_paused or self.foreground_active or time.monotonic()<self.foreground_until:return False
            if len(self.active)>=limit:return False
            with self.lock:active=list(self.active)
            row=c.execute('SELECT * FROM pending INDEXED BY pending_request_order WHERE attempts<3'+
                (' AND identity NOT IN ('+','.join('?' for _ in active)+')' if active else '')+
                ' ORDER BY attempts,requested LIMIT 1',active).fetchone()
            with self.lock:
                if self.closed.is_set() or self.manual_paused or self.foreground_active or time.monotonic()<self.foreground_until:return False
                if len(self.active)>=self.worker_limit():return False
                if row is not None:self.active.add(row['identity'])
        if row is None:return False
        current_edge=row['edge'];record={};decode_started=time.monotonic()
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
            with (self.scheduler.preview_slot(self.closed) if self.scheduler else nullcontext()),_BACKGROUND_SLOTS:
                if self.closed.is_set():return False
                small=420 if record.get('_catalog') else 480
                if record.get('_catalog') and self.catalog is not None:
                    self.catalog.render_preview_batch(record,row['root'],self.exiftool)
                else:media.warm_previews(record,row['root'],self.cache.folder,self.exiftool,(small,MAX_PREVIEW_EDGE),cache_index=self.cache)
            with self.cache.lock,self.cache.connect() as c:
                c.execute('DELETE FROM pending WHERE identity=? AND record=? AND edge=?',(row['identity'],row['record'],current_edge))
                published=c.execute("UPDATE archive_jobs SET state='ready',small_ready=1 WHERE identity=? AND record=? AND state!='ready'",
                    (row['identity'],row['record'])).rowcount
            if published and record.get('_catalog_root'):
                with self.timing_lock:
                    root_id=record['_catalog_root']
                    self.decode_samples.append((root_id,decode_started,time.monotonic()))
                    self.completed_by_root[root_id]=self.completed_by_root.get(root_id,0)+1
        except Exception as exc:
            if self.closed.is_set():return False
            from preview_errors import skip_reason
            ignored=skip_reason(record,row['root'],exc) if record.get('_catalog') else None
            with self.cache.lock,self.cache.connect() as c:
                if ignored:
                    c.execute("UPDATE archive_jobs SET state='ignored',error=? WHERE identity=? AND record=?",
                              (ignored.code+':'+str(ignored),row['identity'],row['record']))
                    c.execute('DELETE FROM pending WHERE identity=? AND record=?',(row['identity'],row['record']))
                    return True
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
                result=self.process_one(wait_for_cache=False)
                # 忙锁只短暂退让，不能与空队列一样等一秒，否则同一批任务
                # 会退化为每秒只领取一项，甚至饿死需要多路同时在途的工作。
                if not result:self.wake.wait(.01 if result is None else 1)
            except (OSError,sqlite3.Error):self.wake.wait(1)

    def close(self):
        self.closed.set();self.wake.set();self.reconcile_requested.set();self.request_wake.set();self.resize_requested.set()
        if self.coverage:self.coverage.close()
        until=time.monotonic()+3
        for thread in [self.phase_thread,self.request_thread,self.resize_thread,*self.threads]:
            if thread.is_alive():thread.join(timeout=max(0,until-time.monotonic()))
        if self.reconcile_thread.is_alive():self.reconcile_thread.join(timeout=max(0,until-time.monotonic()))
        if self.design_repair_thread and self.design_repair_thread.is_alive():self.design_repair_thread.join(timeout=max(0,until-time.monotonic()))
