"""当前素材的实际缓存覆盖率；历史任务完成状态不等于缓存仍然存在。"""
from contextlib import contextmanager, nullcontext
import os
import sqlite3
import threading
import time


class ArchiveCacheCoverage:
    def __init__(self,cache,catalog):
        self.cache=cache;self.catalog=catalog
        from preview_cache import cache_for
        self.images=cache_for(catalog.previews)
        self.closed=threading.Event();self.guard=threading.Lock();self.thread=None
        self.writer_local=threading.local()
        self.snapshot_lock=threading.Lock();self.saved=None;self.saved_key=None
        self.last_check=0.0
        self.anchor=None;self.anchor_path=None;self.complete=False;self.error=''
        with cache.connect() as c:
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='archive_cache_audit'").fetchone()
            if not exists:
                schema=c.execute('PRAGMA schema_version').fetchone()[0]
                marker=c.execute("SELECT value FROM configuration WHERE key='startup_schema_v3'").fetchone()
                upper=c.execute('SELECT COALESCE(max(rowid),0) FROM archive_jobs').fetchone()[0]
                total=c.execute('SELECT count(*) FROM archive_jobs').fetchone()[0]
                c.executescript(f'''BEGIN IMMEDIATE;
                  CREATE TABLE archive_cache_audit(id INTEGER PRIMARY KEY,revision INTEGER NOT NULL,
                    cursor INTEGER NOT NULL,upper_bound INTEGER NOT NULL,total INTEGER NOT NULL,checked INTEGER NOT NULL,complete INTEGER NOT NULL);
                  INSERT INTO archive_cache_audit VALUES(1,0,0,{upper},{total},0,{int(total==0)});
                  CREATE TABLE archive_cache_members(identity TEXT PRIMARY KEY,root_id TEXT NOT NULL,pair_key TEXT NOT NULL,
                    item TEXT NOT NULL,fingerprint TEXT NOT NULL,state TEXT NOT NULL);
                  CREATE TRIGGER archive_cache_insert AFTER INSERT ON archive_jobs WHEN json_valid(NEW.record) BEGIN
                    INSERT INTO archive_cache_members VALUES(NEW.identity,NEW.root_id,NEW.pair_key,
                      COALESCE(json_extract(NEW.record,'$.id'),''),COALESCE(json_extract(NEW.record,'$.fingerprint'),''),NEW.state)
                    ON CONFLICT(identity) DO UPDATE SET root_id=excluded.root_id,pair_key=excluded.pair_key,
                      item=excluded.item,fingerprint=excluded.fingerprint,state=excluded.state;
                  END;
                  CREATE TRIGGER archive_cache_update AFTER UPDATE OF record,state,root_id,pair_key ON archive_jobs
                    WHEN json_valid(NEW.record) AND (OLD.record IS NOT NEW.record OR OLD.state IS NOT NEW.state OR OLD.root_id IS NOT NEW.root_id OR OLD.pair_key IS NOT NEW.pair_key) BEGIN
                    INSERT INTO archive_cache_members VALUES(NEW.identity,NEW.root_id,NEW.pair_key,
                      COALESCE(json_extract(NEW.record,'$.id'),''),COALESCE(json_extract(NEW.record,'$.fingerprint'),''),NEW.state)
                    ON CONFLICT(identity) DO UPDATE SET root_id=excluded.root_id,pair_key=excluded.pair_key,
                      item=excluded.item,fingerprint=excluded.fingerprint,state=excluded.state
                    WHERE root_id IS NOT excluded.root_id OR pair_key IS NOT excluded.pair_key OR item IS NOT excluded.item
                      OR fingerprint IS NOT excluded.fingerprint OR state IS NOT excluded.state;
                  END;
                  CREATE TRIGGER archive_cache_delete AFTER DELETE ON archive_jobs BEGIN
                    DELETE FROM archive_cache_members WHERE identity=OLD.identity;
                  END;
                  CREATE TRIGGER archive_cache_member_insert AFTER INSERT ON archive_cache_members BEGIN
                    UPDATE archive_cache_audit SET revision=revision+1 WHERE id=1; END;
                  CREATE TRIGGER archive_cache_member_update AFTER UPDATE ON archive_cache_members BEGIN
                    UPDATE archive_cache_audit SET revision=revision+1 WHERE id=1; END;
                  CREATE TRIGGER archive_cache_member_delete AFTER DELETE ON archive_cache_members BEGIN
                    UPDATE archive_cache_audit SET revision=revision+1 WHERE id=1; END;
                  COMMIT;''')
                if marker and marker[0]==str(schema):
                    c.execute("UPDATE configuration SET value=? WHERE key='startup_schema_v3'",(str(c.execute('PRAGMA schema_version').fetchone()[0]),))
            state=c.execute('SELECT * FROM archive_cache_audit WHERE id=1').fetchone()
        self.complete=bool(state['complete'])
        with cache.connect() as c:
            if self.complete:c.execute('CREATE INDEX IF NOT EXISTS archive_cache_item ON archive_cache_members(item,root_id,pair_key,fingerprint,state)')
            elif state['total']>2048:c.execute('DROP INDEX IF EXISTS archive_cache_item')
        # 小型/新库同步完成，生产旧大库在首次查看时后台分批接续。
        if not self.complete and state['total']<=2048:
            while not self._backfill_batch():pass
        with self.images.connect() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='coverage_redirect_insert'").fetchone():
                schema=c.execute('PRAGMA schema_version').fetchone()[0]
                marker=c.execute("SELECT value FROM configuration WHERE key='startup_schema_v3'").fetchone()
                for event in ('INSERT','UPDATE','DELETE'):
                    c.execute(f'''CREATE TRIGGER coverage_redirect_{event.lower()} AFTER {event} ON preview_redirects BEGIN
                        UPDATE cache_totals SET revision=revision+1 WHERE id=1; END''')
                if marker and marker[0]==str(schema):c.execute("UPDATE configuration SET value=? WHERE key='startup_schema_v3'",(str(c.execute('PRAGMA schema_version').fetchone()[0]),))

    def _backfill_batch(self):
        with self.cache.budget.generation() if self.cache.budget else nullcontext(),self._writer() as c:
            state=c.execute('SELECT * FROM archive_cache_audit WHERE id=1').fetchone()
            if state['complete']:self.complete=True;return True
            rows=c.execute('SELECT rowid FROM archive_jobs WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT 512',
                           (state['cursor'],state['upper_bound'])).fetchall()
            if not rows:
                c.execute('BEGIN IMMEDIATE')
                c.execute('CREATE INDEX IF NOT EXISTS archive_cache_item ON archive_cache_members(item,root_id,pair_key,fingerprint,state)')
                c.execute('UPDATE archive_cache_audit SET complete=1 WHERE id=1')
            else:
                last=rows[-1][0]
                c.execute('''INSERT OR IGNORE INTO archive_cache_members
                SELECT identity,root_id,pair_key,COALESCE(json_extract(record,'$.id'),''),
                  COALESCE(json_extract(record,'$.fingerprint'),''),state FROM archive_jobs
                WHERE rowid>? AND rowid<=? AND json_valid(record)''',(state['cursor'],last))
                c.execute('UPDATE archive_cache_audit SET cursor=?,checked=checked+? WHERE id=1',(last,len(rows)))
        self.complete=not rows
        return self.complete

    @contextmanager
    def _writer(self):
        existing=getattr(self.writer_local,'connection',None)
        if existing is not None:
            with existing:yield existing
            return
        c=sqlite3.connect(self.cache.db,timeout=.2);c.row_factory=sqlite3.Row
        try:
            c.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,True)
            c.execute('PRAGMA mmap_size=1073741824');c.execute('PRAGMA synchronous=NORMAL')
            c.execute('PRAGMA cache_size=-131072');c.execute('PRAGMA wal_autocheckpoint=0');c.execute('PRAGMA temp_store=MEMORY')
            c.set_progress_handler(lambda:self.closed.is_set(),1000)
            with c:yield c
        finally:c.close()

    def start(self):
        with self.guard:
            if self.complete or self.closed.is_set() or self.thread and self.thread.is_alive():return
            self.thread=threading.Thread(target=self._run,name='archive-cache-audit',daemon=True);self.thread.start()

    def _run(self):
        try:
            with self._writer() as shared:
                self.writer_local.connection=shared
                try:self._run_batches()
                finally:self.writer_local.connection=None
        except (sqlite3.Error,OSError) as error:self.error=str(error)[:300]

    def _run_batches(self):
        while not self.closed.is_set():
            try:
                if self._backfill_batch():self.error='';return
                self.error=''
            except (sqlite3.Error,OSError) as error:self.error=str(error)[:300];self.closed.wait(.5)
            self.closed.wait(.01)

    def _image_connection(self):
        # 空闲只读连接不持事务，保持WAL共享内存，避免目录mtime被反复创建/删除
        # shm文件扰动。迁移后按实际新路径重新打开，不继续读取原位置。
        if self.anchor_path!=self.images.db:
            if self.anchor:self.anchor.close()
            self.anchor=sqlite3.connect(self.images.db.as_uri()+'?mode=ro',uri=True,timeout=1,check_same_thread=False)
            self.anchor.row_factory=sqlite3.Row
            self.anchor.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,True)
            self.anchor.execute('PRAGMA mmap_size=1073741824');self.anchor.execute('SELECT 1 FROM previews LIMIT 1').fetchone()
            self.anchor.set_progress_handler(lambda:self.closed.is_set(),1000)
            self.anchor_path=self.images.db;self.saved_key=None
        return self.anchor

    @contextmanager
    def source_connection(self):
        c=sqlite3.connect(self.catalog.db.resolve().as_uri()+'?mode=ro',uri=True,timeout=1);c.row_factory=sqlite3.Row
        try:
            c.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,True)
            c.execute('PRAGMA mmap_size=2147418112');c.execute('PRAGMA temp_store=MEMORY')
            c.set_progress_handler(lambda:self.closed.is_set(),1000)
            yield c
        finally:c.close()

    def snapshot(self,roots):
        with self.snapshot_lock,self.cache.budget.generation() if self.cache.budget else nullcontext():
            if self.closed.is_set():
                if self.saved is not None:return dict(self.saved)
                raise RuntimeError('缓存核对服务已关闭')
            with self.cache.read_connection() as c:
                c.set_progress_handler(lambda:self.closed.is_set(),1000)
                audit=c.execute('SELECT * FROM archive_cache_audit WHERE id=1').fetchone()
                if not audit['complete']:
                    self.start()
                    return {'total':0,'ready':0,'pending':0,'failed':0,'ignored':0,'uncached':0,'thumbnails_ready':0,
                            'coverage_initialized':False,'cache_audit_checked':audit['checked'],'cache_audit_total':audit['total'],'cache_audit_error':self.error}
                self.complete=True
                image_connection=self._image_connection()
                image_revision=image_connection.execute('SELECT revision FROM cache_totals WHERE id=1').fetchone()[0]
                with self.source_connection() as source:
                    revision=source.execute('SELECT value FROM catalog_page_revision WHERE id=1').fetchone()[0]
                    dirty=tuple(tuple(row) for row in source.execute('SELECT pair_key,version FROM catalog_preview_changes ORDER BY pair_key'))
                    key=(tuple(sorted(roots)),str(self.cache.db),audit['revision'],revision,dirty,image_revision,self.images.folder.stat().st_mtime_ns)
                    if self.saved is not None and key==self.saved_key and time.monotonic()-self.last_check<60:return dict(self.saved)
                    # 仅覆盖索引归组；decode选择同一配对中字典序最早的扩展名。
                    # 不使用历史任务数作总量，也不随机逐项读取10GB素材资料表。
                    current=list(source.execute('''SELECT root,pair_key,id,min(extension) FROM items INDEXED BY catalog_browse_cover
                        WHERE online=1 AND root IN (SELECT id FROM roots WHERE removed=0)
                        AND root IN ('''+','.join('?' for _ in roots)+''') GROUP BY pair_key''',list(roots)))
                jobs={(row['root_id'],row['pair_key'],row['item']):(row['fingerprint'],row['state']) for row in c.execute(
                    'SELECT item,root_id,pair_key,fingerprint,state FROM archive_cache_members INDEXED BY archive_cache_item')}
            files={entry.name:entry.stat(follow_symlinks=False).st_size for entry in os.scandir(self.images.folder)
                   if entry.is_file(follow_symlinks=False)}
            from preview_cache import registered_recipe
            masks={};valid_names={};alias_orientations=set()
            for row in image_connection.execute('SELECT name,bytes,fingerprint,edge,decoded,orientation,digest FROM previews'):
                if row['decoded'] or row['edge'] not in (420,2048) or not row['digest'] or files.get(row['name'])!=row['bytes'] or registered_recipe(row) is None:continue
                identity=(row['fingerprint'],row['orientation'])
                masks[identity]=masks.get(identity,0)|(1 if row['edge']==420 else 2)
                valid_names[row['name']]=(row['edge'],row['orientation'])
            for row in image_connection.execute('SELECT name,target FROM preview_redirects'):
                if row['target'] in valid_names:
                    valid_names[row['name']]=valid_names[row['target']];alias_orientations.add(valid_names[row['target']][1])
            ready={key[0] for key,mask in masks.items() if mask==3};small={key[0] for key,mask in masks.items() if mask&1}
            if alias_orientations:
                from preview_cache import cache_name
                for fingerprint,_ in jobs.values():
                    if fingerprint in ready:continue
                    for orientation in alias_orientations:
                        record={'fingerprint':fingerprint,'orientation':orientation}
                        present=[valid_names.get(cache_name(record,edge))==(edge,orientation) for edge in (420,2048)]
                        if present[0]:small.add(fingerprint)
                        if all(present):ready.add(fingerprint);break
            result={'total':len(current),'ready':0,'pending':0,'failed':0,'ignored':0,'uncached':0,'thumbnails_ready':0,'coverage_initialized':True}
            changed={pair for pair,_ in dirty}
            for item in current:
                if item['pair_key'] in changed:result['pending']+=1;continue
                job=jobs.get((item['root'],item['pair_key'],'catalog:'+item['id']))
                if not job:result['uncached']+=1;continue
                fingerprint,state=job
                result['thumbnails_ready']+=int(fingerprint in small)
                if fingerprint in ready:result['ready']+=1
                elif state in ('queued','failed','ignored'):result['pending' if state=='queued' else state]+=1
                else:result['uncached']+=1
            result['cache_verified_at']=time.time()
            self.saved_key=key;self.saved=dict(result)
            self.last_check=time.monotonic()
            return result

    def close(self):
        self.closed.set()
        if self.thread:self.thread.join(timeout=3)
        with self.snapshot_lock:
            if self.anchor:self.anchor.close();self.anchor=None
