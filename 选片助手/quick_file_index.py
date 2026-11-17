"""快捷文件检索的持久轻量副本；不在按键请求里随机读取业务大表。"""
import base64
from collections import OrderedDict
from copy import deepcopy
from contextlib import closing,contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time

from catalog_search_requests import SearchCancelled
from retrieval_schema import digest,dumps
from sku_match import normalize

COLUMNS=('id','root','path','relative','folder','name','extension','size','modified','fingerprint',
         'sku','normalized','source','media_type','media_format','duration_ms','width','height','pair_key','online')
INDEXES={'catalog_items_sku':'quick_sku','intelligence_catalog_order':'quick_time',
         'catalog_items_browse':'quick_root_time','catalog_media_page':'quick_media_time',
         'catalog_root_media_page':'quick_root_media_time'}
APP_ID=0x464B5351
QUERY_BUDGET_SECONDS=2


class QuickFileIndex:
    def __init__(self,catalog,*,folder=None):
        self.catalog=catalog;self.stopped=threading.Event();self.wake=threading.Event()
        self.thread=None;self.start_lock=threading.Lock();self.error='';self.ready=False;self.copied=0
        self.readers=[];self.reader_lock=threading.Lock()
        self.results=OrderedDict();self.result_lock=threading.Lock()
        self.root_lock=threading.RLock();self.scope_sync_lock=threading.Lock();self.known_grams=None;self.generation=0
        self.memory=None;self.memory_error=''
        if folder is None:
            from preview_cache import _LOCATIONS
            folder=_LOCATIONS.get(catalog.data.resolve(),catalog.data)
        self.path=(Path(folder)/'quick_search.sqlite3').resolve();self.path.parent.mkdir(parents=True,exist_ok=True)
        self.source_id=digest([str(catalog.db.resolve()).casefold(),catalog.db.stat().st_ino])
        with catalog.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS quick_file_dirty(item TEXT PRIMARY KEY) WITHOUT ROWID')
            changed=' OR '.join(f'old.{name} IS NOT new.{name}' for name in COLUMNS)
            for event,name,reference in [('INSERT','insert','new'),('UPDATE OF '+','.join(COLUMNS),'update','new'),('DELETE','delete','old')]:
                c.execute(f'''CREATE TRIGGER IF NOT EXISTS quick_file_{name} AFTER {event} ON items
 {'WHEN '+changed if name=='update' else ''} BEGIN
 INSERT INTO quick_file_dirty VALUES({reference}.id) ON CONFLICT(item) DO NOTHING; END''')
            self.has_ocr=bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='intelligence_ocr'").fetchone())
            if self.has_ocr:
                for event,reference in [('INSERT','new'),('UPDATE','new'),('DELETE','old')]:
                    c.execute(f'''CREATE TRIGGER IF NOT EXISTS quick_ocr_{event.lower()} AFTER {event} ON intelligence_ocr BEGIN
 INSERT INTO quick_file_dirty VALUES({reference}.item) ON CONFLICT(item) DO NOTHING; END''')
            roots=[tuple(r) for r in c.execute('SELECT id,removed FROM roots ORDER BY id')]
            exclusions=[tuple(r) for r in c.execute('SELECT root,folder FROM exclusions ORDER BY root,folder')]
        fresh=not self.path.exists()
        with closing(sqlite3.connect(self.path,timeout=5)) as c:
            # 只读接入也可能在最后一个连接关闭时合并历史 WAL；留给后台
            # 正常写连接的检查点处理，不能让启动线程等待历史索引写入。
            c.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,True)
            c.execute('PRAGMA wal_autocheckpoint=0')
            owner=c.execute('PRAGMA application_id').fetchone()[0]
            if not fresh and owner!=APP_ID:raise ValueError('快捷索引文件归属无效，已保留原文件')
            # 成熟索引只核对归属；即使值相同，赋值仍是一次真实写事务。
            # 放在 journal/synchronous 配置前会让首屏承担磁盘同步成本。
            if fresh:c.execute(f'PRAGMA application_id={APP_ID}')
            c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=NORMAL')
            numeric={'size':'INTEGER','modified':'REAL','duration_ms':'INTEGER','width':'INTEGER','height':'INTEGER','online':'INTEGER'}
            fields=','.join(name+' '+numeric.get(name,'TEXT') for name in COLUMNS)
            c.executescript('''CREATE TABLE IF NOT EXISTS quick_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS quick_items(catalog_rowid INTEGER PRIMARY KEY,'''+fields+''');
 CREATE UNIQUE INDEX IF NOT EXISTS quick_id ON quick_items(id);
 CREATE INDEX IF NOT EXISTS quick_pair ON quick_items(pair_key,online,source,extension,id);
 CREATE INDEX IF NOT EXISTS quick_sku ON quick_items(normalized,online,modified DESC,id);
 CREATE INDEX IF NOT EXISTS quick_time ON quick_items(online,modified DESC,id);
 CREATE INDEX IF NOT EXISTS quick_root_time ON quick_items(root,online,modified DESC,id);
 CREATE INDEX IF NOT EXISTS quick_media_time ON quick_items(media_type,online,modified DESC,id);
 CREATE INDEX IF NOT EXISTS quick_root_media_time ON quick_items(root,media_type,online,modified DESC,id);''')
            c.executescript('''CREATE TABLE IF NOT EXISTS roots(id TEXT PRIMARY KEY,removed INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS exclusions(root TEXT NOT NULL,folder TEXT NOT NULL,PRIMARY KEY(root,folder)) WITHOUT ROWID;
 CREATE TABLE IF NOT EXISTS quick_grams(fragment TEXT NOT NULL,gram TEXT NOT NULL,PRIMARY KEY(fragment,gram)) WITHOUT ROWID;
 CREATE INDEX IF NOT EXISTS quick_short ON quick_items(relative,catalog_rowid) WHERE length(relative)<3;
 CREATE VIRTUAL TABLE IF NOT EXISTS quick_path_fts USING fts5(relative,content='quick_items',content_rowid='catalog_rowid',tokenize='trigram');
 CREATE TRIGGER IF NOT EXISTS quick_path_insert AFTER INSERT ON quick_items
 WHEN (SELECT value FROM quick_state WHERE key='local_fts')='1' BEGIN
 INSERT INTO quick_path_fts(rowid,relative) VALUES(new.catalog_rowid,new.relative); END;
 CREATE TRIGGER IF NOT EXISTS quick_path_delete AFTER DELETE ON quick_items
 WHEN (SELECT value FROM quick_state WHERE key='local_fts')='1' BEGIN
 INSERT INTO quick_path_fts(quick_path_fts,rowid,relative) VALUES('delete',old.catalog_rowid,old.relative); END;
 CREATE TRIGGER IF NOT EXISTS quick_path_update AFTER UPDATE OF relative ON quick_items
 WHEN old.relative IS NOT new.relative AND (SELECT value FROM quick_state WHERE key='local_fts')='1' BEGIN
 INSERT INTO quick_path_fts(quick_path_fts,rowid,relative) VALUES('delete',old.catalog_rowid,old.relative);
 INSERT INTO quick_path_fts(rowid,relative) VALUES(new.catalog_rowid,new.relative); END;''')
            c.executescript('''CREATE TABLE IF NOT EXISTS quick_ocr(item TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,text TEXT NOT NULL);
 CREATE VIRTUAL TABLE IF NOT EXISTS quick_ocr_fts USING fts5(text,content='quick_ocr',content_rowid='rowid',tokenize='trigram');
 CREATE TRIGGER IF NOT EXISTS quick_ocr_insert AFTER INSERT ON quick_ocr BEGIN
 INSERT INTO quick_ocr_fts(rowid,text) VALUES(new.rowid,new.text); END;
 CREATE TRIGGER IF NOT EXISTS quick_ocr_delete AFTER DELETE ON quick_ocr BEGIN
 INSERT INTO quick_ocr_fts(quick_ocr_fts,rowid,text) VALUES('delete',old.rowid,old.text); END;
 CREATE TRIGGER IF NOT EXISTS quick_ocr_update AFTER UPDATE OF text ON quick_ocr BEGIN
 INSERT INTO quick_ocr_fts(quick_ocr_fts,rowid,text) VALUES('delete',old.rowid,old.text);
 INSERT INTO quick_ocr_fts(rowid,text) VALUES(new.rowid,new.text); END;''')
            state=dict(c.execute('SELECT key,value FROM quick_state'))
            with c:
                if state.get('source')!=self.source_id:
                    c.execute("UPDATE quick_state SET value='0' WHERE key='local_fts'")
                    c.execute('DELETE FROM quick_items');c.execute('DELETE FROM quick_state')
                    c.execute('DELETE FROM quick_ocr')
                    c.execute('DELETE FROM quick_grams')
                    c.executemany('INSERT INTO quick_state VALUES(?,?)',[('source',self.source_id),('cursor','0'),('generation','0')])
                    state={}
                saved_roots=[tuple(r) for r in c.execute('SELECT id,removed FROM roots ORDER BY id')]
                saved_exclusions=[tuple(r) for r in c.execute('SELECT root,folder FROM exclusions ORDER BY root,folder')]
                if (saved_roots,saved_exclusions)!=(roots,exclusions):
                    c.execute('DELETE FROM roots');c.executemany('INSERT INTO roots VALUES(?,?)',roots)
                    c.execute('DELETE FROM exclusions');c.executemany('INSERT INTO exclusions VALUES(?,?)',exclusions)
                    c.execute("UPDATE quick_state SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
            # 范围变更在上面的短事务中已经提交；内存代次必须读取新值。
            state=dict(c.execute('SELECT key,value FROM quick_state'))
            self.roots_revision=(tuple(roots),tuple(exclusions))
            self.generation=int(state.get('generation','0'))
            self.ready=state.get('ready')=='1' and state.get('local_fts')=='1';self.copied=int(state.get('cursor','0'))
        if self.has_ocr and state.get('ocr_seed')!='1':
            with catalog.connect() as source:source.execute('INSERT INTO quick_file_dirty SELECT item FROM intelligence_ocr WHERE 1 ON CONFLICT(item) DO NOTHING')
            with closing(self._write()) as c,c:c.execute("INSERT OR REPLACE INTO quick_state VALUES('ocr_seed','1')")

    def start(self):
        with self.start_lock:
            if self.stopped.is_set():return False
            if self.thread is None or not self.thread.is_alive():
                self.thread=threading.Thread(target=self._run,name='quick-file-index',daemon=True);self.thread.start()
            return True

    def close(self):
        self.stopped.set();self.wake.set()
        if self.thread and self.thread is not threading.current_thread():self.thread.join(5)
        with self.reader_lock:
            for c in self.readers:c.close()
            self.readers.clear()
        with self.result_lock:self.results.clear()
        with self.root_lock:
            if self.memory is not None:self.memory.close();self.memory=None

    @contextmanager
    def reader(self):
        with self.root_lock:
            if self.memory is not None:
                try:yield self.memory
                finally:self.memory.set_progress_handler(None,0);self.memory.rollback()
                return
        with self.reader_lock:c=self.readers.pop() if self.readers else None
        if c is None:
            c=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=.1,check_same_thread=False)
            c.row_factory=sqlite3.Row;c.execute('PRAGMA mmap_size=2147418112')
        reusable=True
        try:yield c
        except BaseException:reusable=False;raise
        finally:
            # 只复用连接和映射，不保留读事务或旧请求取消回调，避免阻止 WAL 回收。
            c.set_progress_handler(None,0);c.rollback()
            with self.reader_lock:
                if reusable and not self.stopped.is_set() and len(self.readers)<4:self.readers.append(c)
                else:c.close()

    def _run(self):
        while not self.stopped.is_set():
            try:
                if not self.ready:self.build()
                if self.ready and self.memory is None and not self.memory_error:self.build_memory()
                changed=self.sync() if self.ready and not self.stopped.is_set() else False
                self.error=''
                if changed:continue
            except (sqlite3.Error,OSError,ValueError) as error:self.error=str(error)
            self.wake.wait(.1 if not self.error else 1);self.wake.clear()

    def build_memory(self):
        """后台预热独立检索库，之后只重放增量，查询不再竞争缓存盘 I/O。"""
        target=None
        try:
            target=sqlite3.connect(':memory:',check_same_thread=False)
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as source:
                def progress(status,remaining,total):
                    if self.stopped.is_set():raise SearchCancelled()
                source.backup(target,pages=256,progress=progress,sleep=.01)
            target.row_factory=sqlite3.Row
            target.execute('PRAGMA recursive_triggers=ON')
            with self.root_lock:
                # 范围同步可与预热并发；只发布仍对应当前代次的镜像。
                generation=int(target.execute("SELECT value FROM quick_state WHERE key='generation'").fetchone()[0])
                if not self.stopped.is_set() and generation==self.generation and self.memory is None:
                    self.memory=target;target=None
        except (sqlite3.Error,MemoryError) as error:self.memory_error=str(error)
        except SearchCancelled:pass
        finally:
            if target is not None:target.close()

    @contextmanager
    def memory_write(self):
        """发布代次前同步 RAM，失败时丢弃镜像并继续使用已提交的磁盘副本。"""
        with self.root_lock:
            try:
                if self.memory is not None:
                    with self.memory:yield self.memory
                else:yield None
            except (sqlite3.Error,MemoryError) as error:
                self.memory.close();self.memory=None;self.memory_error=str(error)


    def _write(self):
        c=sqlite3.connect(self.path,timeout=2);c.execute('PRAGMA synchronous=NORMAL');c.execute('PRAGMA recursive_triggers=ON');return c

    @staticmethod
    def _save(c,rows):
        if rows:c.executemany('INSERT OR REPLACE INTO quick_items VALUES('+','.join('?' for _ in range(len(COLUMNS)+1))+')',rows)

    def build(self):
        if self.ready:return
        while not self.stopped.is_set():
            with self.catalog.connect() as source:
                rows=[tuple(row) for row in source.execute('SELECT rowid,'+','.join(COLUMNS)+' FROM items WHERE rowid>? ORDER BY rowid LIMIT 2048',(self.copied,))]
            with closing(self._write()) as c,c:
                self._save(c,rows)
                if rows:
                    self.copied=rows[-1][0];c.execute("INSERT OR REPLACE INTO quick_state VALUES('cursor',?)",(str(self.copied),))
                else:
                    c.set_progress_handler(lambda:int(self.stopped.is_set()),5000)
                    c.execute("INSERT INTO quick_path_fts(quick_path_fts) VALUES('rebuild')")
                    c.execute('CREATE VIRTUAL TABLE temp.quick_vocab USING fts5vocab(main,quick_path_fts,row)')
                    grams=[r[0] for r in c.execute('SELECT term FROM quick_vocab')]
                    from literal_search import fragments
                    c.execute('DELETE FROM quick_grams')
                    c.executemany('INSERT OR IGNORE INTO quick_grams VALUES(?,?)',((part,gram) for gram in grams for part in fragments(gram)))
                    c.execute("INSERT OR REPLACE INTO quick_state VALUES('local_fts','1')")
                    c.execute("INSERT OR REPLACE INTO quick_state VALUES('ready','1')")
            if not rows:self.ready=True;return

    def sync_roots(self):
        with self.scope_sync_lock:
            with self.catalog.connect() as source:
                roots=tuple(tuple(r) for r in source.execute('SELECT id,removed FROM roots ORDER BY id'))
                exclusions=tuple(tuple(r) for r in source.execute('SELECT root,folder FROM exclusions ORDER BY root,folder'))
            if (roots,exclusions)==self.roots_revision:return False
            with closing(self._write()) as c,c:
                c.execute('DELETE FROM roots');c.executemany('INSERT INTO roots VALUES(?,?)',roots)
                c.execute('DELETE FROM exclusions');c.executemany('INSERT INTO exclusions VALUES(?,?)',exclusions)
                c.execute("UPDATE quick_state SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
            with self.root_lock:
                with self.memory_write() as memory:
                    if memory is not None:
                        memory.execute('DELETE FROM roots');memory.executemany('INSERT INTO roots VALUES(?,?)',roots)
                        memory.execute('DELETE FROM exclusions');memory.executemany('INSERT INTO exclusions VALUES(?,?)',exclusions)
                self.roots_revision=(roots,exclusions)
                self.generation+=1
                with self.result_lock:self.results.clear()
            return True

    def _update_grams(self,c,rows):
        from literal_search import fragments
        if self.known_grams is None:self.known_grams={r[0] for r in c.execute('SELECT DISTINCT gram FROM quick_grams')}
        grams=set();position=COLUMNS.index('relative')+1
        for row in rows:
            text=row[position].lower();grams.update(text[i:i+3] for i in range(max(0,len(text)-2)))
        grams-=self.known_grams
        c.executemany('INSERT OR IGNORE INTO quick_grams VALUES(?,?)',((part,gram) for gram in grams for part in fragments(gram)))
        return grams

    def sync(self,limit=128):
        # 先读取队列及当前值，提交轻量副本，再条件确认。跨库中途失败会重放，
        # 不先删主库队列；确认前重新比较版本，不能丢掉同步期间的后续修改。
        roots_changed=self.sync_roots()
        with self.catalog.connect() as source:
            pending=[r[0] for r in source.execute('SELECT item FROM quick_file_dirty LIMIT ?',(limit,))]
            if not pending:return roots_changed
            marks=','.join('?' for _ in pending)
            rows={r['id']:tuple(r) for r in source.execute('SELECT rowid,'+','.join(COLUMNS)+' FROM items WHERE id IN ('+marks+')',pending)}
            ocr={r['item']:tuple(r) for r in source.execute('SELECT item,fingerprint,frames FROM intelligence_ocr WHERE item IN ('+marks+')',pending)} if self.has_ocr else {}
        with closing(self._write()) as c,c:
            c.executemany('DELETE FROM quick_items WHERE id=?',[(item,) for item in pending])
            self._save(c,list(rows.values()))
            from catalog_ocr import text as ocr_text
            c.executemany('DELETE FROM quick_ocr WHERE item=?',[(item,) for item in pending])
            c.executemany('INSERT INTO quick_ocr VALUES(?,?,?)',[(item,record[1],ocr_text(json.loads(record[2]))) for item,record in ocr.items()
                          if item in rows and record[1]==rows[item][COLUMNS.index('fingerprint')+1]])
            learned=self._update_grams(c,list(rows.values()))
            c.execute("UPDATE quick_state SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
        with self.root_lock:
            with self.memory_write() as memory:
                if memory is not None:
                    memory.executemany('DELETE FROM quick_items WHERE id=?',[(item,) for item in pending])
                    self._save(memory,list(rows.values()))
                    memory.executemany('DELETE FROM quick_ocr WHERE item=?',[(item,) for item in pending])
                    memory.executemany('INSERT INTO quick_ocr VALUES(?,?,?)',[(item,record[1],ocr_text(json.loads(record[2]))) for item,record in ocr.items()
                                       if item in rows and record[1]==rows[item][COLUMNS.index('fingerprint')+1]])
                    from literal_search import fragments
                    memory.executemany('INSERT OR IGNORE INTO quick_grams VALUES(?,?)',((part,gram) for gram in learned for part in fragments(gram)))
            self.generation+=1
            with self.result_lock:self.results.clear()
        self.known_grams.update(learned)
        with self.catalog.connect() as source:
            source.execute('BEGIN IMMEDIATE')
            current={r['id']:tuple(r) for r in source.execute('SELECT rowid,'+','.join(COLUMNS)+' FROM items WHERE id IN ('+marks+')',pending)}
            current_ocr={r['item']:tuple(r) for r in source.execute('SELECT item,fingerprint,frames FROM intelligence_ocr WHERE item IN ('+marks+')',pending)} if self.has_ocr else {}
            source.executemany('DELETE FROM quick_file_dirty WHERE item=?',[(item,) for item in pending if current.get(item)==rows.get(item) and current_ocr.get(item)==ocr.get(item)])
        return True

    def query(self,text='',*,root='',media='',facets=None,cursor='',limit=60,cancel=None):
        with self.root_lock:
            return self._query(text,root=root,media=media,facets=facets,cursor=cursor,limit=limit,cancel=cancel)

    def _query(self,text='',*,root='',media='',facets=None,cursor='',limit=60,cancel=None):
        if cancel and cancel():raise SearchCancelled()
        if not self.ready:raise ValueError('快捷检索索引正在准备，请稍后重试')
        if not isinstance(text,str) or len(text)>300:raise ValueError('检索条件过长')
        if facets:raise ValueError('快捷文件检索不支持内容维度，请使用资产检索')
        if media and media not in ('image','video','design'):raise ValueError('媒体类型无效')
        text=text.strip()
        from literal_search import page_keys
        from smart_query import CANONICAL
        start=time.perf_counter();limit=max(1,min(100,int(limit)));signature=digest([text,root,media,'quick-files-v1'])
        # 常用输入在内存中直接命中；不能为确认范围和代次先打开 SQLite 读事务。
        with self.root_lock:generation=self.generation;root_revision=self.roots_revision
        cache_key=(signature,generation,root_revision,cursor,limit)
        with self.result_lock:
            cached=self.results.get(cache_key)
            if cached is not None:self.results.move_to_end(cache_key)
        if cached is not None:
            if cancel and cancel():raise SearchCancelled()
            cached=deepcopy(cached);cached['elapsed_ms']=round((time.perf_counter()-start)*1000,2);cached['cache_hit']=True
            return cached
        with self.reader() as c:
            c.execute('BEGIN')
            marker=None
            if cursor:
                try:
                    marker=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
                    if marker['q']!=signature or marker['g']!=generation or len(marker['key'])!=3:raise ValueError()
                except (ValueError,TypeError,KeyError):raise ValueError('快捷索引或条件已更新，请重新搜索') from None
            clauses=['i.online=1','roots.removed=0',CANONICAL.replace('FROM items b','FROM quick_items b'),
                     "NOT EXISTS(SELECT 1 FROM exclusions x WHERE x.root=i.root AND (i.folder=x.folder OR i.folder LIKE replace(replace(replace(x.folder,'\\','\\\\'),'%','\\%'),'_','\\_')||'/%' ESCAPE '\\'))"];args=[]
            if root:clauses.append('i.root=?');args.append(root)
            if media:clauses.append('i.media_type=?');args.append(media)
            cancelled=lambda:bool(cancel and cancel());deadline=time.monotonic()+QUERY_BUDGET_SECONDS
            c.set_progress_handler(lambda:int(cancelled() or time.monotonic()>deadline),2000)
            keys=[];more=False
            try:
                if text.strip():
                    if len(text)>=3 and '\0' not in text:
                        ids='SELECT rowid FROM quick_path_fts WHERE quick_path_fts MATCH ?';values=['"'+text.replace('"','""')+'"']
                    else:
                        grams=[r[0] for r in c.execute('SELECT gram FROM quick_grams WHERE fragment=?',(text.lower(),))]
                        pieces=['SELECT catalog_rowid AS rowid FROM quick_items INDEXED BY quick_short WHERE length(relative)<3 AND instr(lower(relative),lower(?))>0'];values=[text]
                        for offset in range(0,len(grams),128):
                            pieces.append('SELECT rowid FROM quick_path_fts WHERE quick_path_fts MATCH ?')
                            values.append(' OR '.join('"'+g.replace('"','""')+'"' for g in grams[offset:offset+128]))
                        ids=' UNION '.join(pieces)
                    candidates='''SELECT member.id FROM (SELECT p.pair_key FROM ('''+ids+''') hits
 CROSS JOIN quick_items p ON p.catalog_rowid=hits.rowid WHERE p.online=1 AND instr(lower(p.relative),lower(?))>0) pairs
 CROSS JOIN quick_items member INDEXED BY quick_pair ON member.pair_key=pairs.pair_key WHERE member.online=1'''
                    values=values+[text];key=normalize(text)
                    ocr_filter='q.rowid IN (SELECT rowid FROM quick_ocr_fts WHERE quick_ocr_fts MATCH ?) AND ' if len(text)>=3 and '\0' not in text else ''
                    ocr_values=(['"'+text.replace('"','""')+'"'] if ocr_filter else [])+[text]
                    candidates+=''' UNION SELECT member.id FROM quick_ocr q CROSS JOIN quick_items p ON p.id=q.item
 CROSS JOIN quick_items member INDEXED BY quick_pair ON member.pair_key=p.pair_key
 WHERE p.online=1 AND member.online=1 AND q.fingerprint=p.fingerprint AND '''+ocr_filter+'instr(lower(q.text),lower(?))>0'
                    values+=ocr_values
                    if key:
                        candidates+=' UNION SELECT id FROM quick_items INDEXED BY quick_sku WHERE online=1 AND normalized>\'\' AND instr(normalized,?)>0'
                        values += [key]
                    point='''EXISTS(SELECT 1 FROM quick_items p INDEXED BY quick_pair WHERE p.pair_key=i.pair_key
 AND p.online=1 AND instr(lower(p.relative),lower(?))>0) OR EXISTS(SELECT 1 FROM quick_items p INDEXED BY quick_pair
 JOIN quick_ocr q ON q.item=p.id AND q.fingerprint=p.fingerprint WHERE p.pair_key=i.pair_key AND p.online=1 AND instr(lower(q.text),lower(?))>0)'''
                    point_values=[text,text]
                    if key:
                        point+=' OR instr(i.normalized,?)>0';point_values.append(key)
                    keys=page_keys(self.catalog,c,text,clauses,args,candidates,values,limit=limit,ranked=True,
                                   marker=marker['key'] if marker else None,cancel=cancel,deadline=deadline,roots=True,
                                   files_only=True,table='quick_items',index_map=INDEXES,point_override=('('+point+')',point_values),probe_seconds=.003)
                else:
                    if marker:
                        clauses.append('(i.modified<? OR (i.modified=? AND i.id>?))');args += [-marker['key'][1],-marker['key'][1],marker['key'][2]]
                    keys=[dict(r) for r in c.execute('SELECT i.id,i.modified,2 AS priority FROM quick_items i INDEXED BY quick_time CROSS JOIN roots ON roots.id=i.root WHERE '+
                        ' AND '.join(clauses)+' ORDER BY i.modified DESC,i.id LIMIT ?',args+[limit+1])]
                more=len(keys)>limit;keys=keys[:limit];rows=[]
                if keys:
                    marks=','.join('?' for _ in keys)
                    hydrated={r['id']:dict(r) for r in c.execute('''SELECT i.*,
 EXISTS(SELECT 1 FROM quick_items b WHERE b.pair_key=i.pair_key AND b.online=1 AND b.extension IN ('.jpg','.jpeg')) AS has_jpg,
 EXISTS(SELECT 1 FROM quick_items b WHERE b.pair_key=i.pair_key AND b.online=1 AND b.extension NOT IN ('.jpg','.jpeg') AND b.id<>i.id) AS has_raw
 FROM quick_items i WHERE i.id IN ('''+marks+')',[r['id'] for r in keys])}
                    for key in keys:
                        row=hydrated[key['id']];row.pop('catalog_rowid',None)
                        row.update(priority=key['priority'],summary=None,frames=[],preview='/api/catalog/preview/'+row['id'],
                                   reasons=['SKU 精确命中' if key['priority']==0 else 'SKU 前缀命中' if key['priority']==1 else
                                            'SKU 包含命中' if normalize(text) and normalize(text) in row['normalized'] else '文件名 / 路径命中'])
                        rows.append(row)
                if cancelled():raise SearchCancelled()
            except sqlite3.OperationalError as error:
                if cancelled():raise SearchCancelled() from None
                if getattr(error,'sqlite_errorcode',None)==sqlite3.SQLITE_INTERRUPT or 'interrupt' in str(error).lower():
                    raise ValueError('检索超过时间预算，请缩小归档范围或补充关键词后重试') from None
                raise
        next_cursor=''
        if more and keys:
            last=keys[-1];next_cursor=base64.urlsafe_b64encode(dumps({'q':signature,'g':generation,'key':[last['priority'],-last['modified'],last['id']]}).encode()).decode().rstrip('=')
        result={'rows':rows,'has_more':more,'next_cursor':next_cursor,'elapsed_ms':round((time.perf_counter()-start)*1000,2),'scope':'files','engine':'quick-file-index'}
        with self.result_lock:
            self.results[cache_key]=deepcopy(result);self.results.move_to_end(cache_key)
            while len(self.results)>64:self.results.popitem(last=False)
        return result
