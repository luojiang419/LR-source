"""复用现有 trigram 倒排，补齐一、二字检索，避免读取整库 EXIF 文本。"""
import sqlite3
import threading
import time

SOURCES = {'path': ('reference_path_fts', 'items', 'relative'),
           'metadata': ('catalog_metadata_fts', 'catalog_metadata', 'search_text')}


class LiteralSearchIndex:
    def __init__(self, catalog):
        self.catalog = catalog
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.error = ''
        self.known = {}
        with catalog.connect() as c:
            c.executescript('''
CREATE TABLE IF NOT EXISTS literal_search_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS literal_search_grams(
 source TEXT NOT NULL,fragment TEXT NOT NULL,gram TEXT NOT NULL,
 PRIMARY KEY(source,fragment,gram)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS literal_search_short(
 source TEXT NOT NULL,item TEXT NOT NULL,text TEXT NOT NULL,
 PRIMARY KEY(source,item)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS literal_search_dirty(
 source TEXT NOT NULL,item TEXT NOT NULL,PRIMARY KEY(source,item)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS intelligence_catalog_order ON items(online,modified DESC,id);
''')
            for source, (_, table, column) in SOURCES.items():
                identity = 'id' if source == 'path' else 'item'
                for event, name, reference in [('INSERT', 'insert', 'new'),
                                                ('UPDATE OF '+column, 'update', 'new'),
                                                ('DELETE', 'delete', 'old')]:
                    trigger=f'literal_search_{source}_{name}'
                    when=f' WHEN old.{column} IS NOT new.{column}' if name=='update' else ''
                    definition=f'''CREATE TRIGGER IF NOT EXISTS {trigger}
 AFTER {event} ON {table}{when} BEGIN
 INSERT INTO literal_search_dirty VALUES('{source}',{reference}.{identity})
 ON CONFLICT(source,item) DO NOTHING; END'''
                    current=c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(trigger,)).fetchone()
                    if current and when and when.strip() not in current[0]:
                        c.execute('SAVEPOINT literal_trigger_upgrade')
                        c.execute('DROP TRIGGER '+trigger);c.execute(definition)
                        c.execute('RELEASE literal_trigger_upgrade')
                    else:c.execute(definition)

    @staticmethod
    def ready(c):
        return bool(c.execute("SELECT 1 FROM literal_search_state WHERE key='ready' AND value='1'").fetchone())

    def start(self):
        if self.thread is None and not self.stopped.is_set():
            self.thread = threading.Thread(target=self._run, name='literal-search-index', daemon=True)
            self.thread.start()

    def close(self):
        self.stopped.set(); self.wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(5)

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.build()
                while not self.stopped.is_set() and self.process():
                    self.stopped.wait(.01)
                self.error = ''
            except sqlite3.Error as error:
                self.error = str(error)
            self.wake.wait(2); self.wake.clear()

    def build(self):
        if not self.catalog.fast_index.ready or not self.catalog.metadata.fts:
            return
        with self.catalog.connect() as c:
            if self.ready(c):
                return
        # 词典只有数万种三字片段。fts5vocab 仍需解开 posting，故仅在后台
        # 首次读取一次；前台按 fragment 主键查询，不逐请求遍历词典。
        for source, (fts, table, column) in SOURCES.items():
            if self.stopped.is_set():
                return
            with self.catalog.connect() as c:
                if c.execute('SELECT 1 FROM literal_search_state WHERE key=?', ('built_'+source,)).fetchone():
                    continue
                c.execute(f'CREATE VIRTUAL TABLE temp.literal_vocab USING fts5vocab(main,{fts},row)')
                c.set_progress_handler(lambda: int(self.stopped.is_set()), 10000)
                grams = [row[0] for row in c.execute('SELECT term FROM literal_vocab')]
                identity = 'id' if source == 'path' else 'item'
                short = list(c.execute(f'SELECT {identity},{column} FROM {table} WHERE length({column}) BETWEEN 1 AND 2'))
            batch = []
            for gram in grams:
                for fragment in fragments(gram):
                    batch.append((source, fragment, gram))
                if len(batch) >= 2000:
                    self._save(batch); batch.clear()
                    if self.stopped.is_set():
                        return
            self._save(batch)
            with self.catalog.connect() as c:
                c.executemany('INSERT OR REPLACE INTO literal_search_short VALUES(?,?,?)',
                              [(source, item, text) for item, text in short])
                c.execute('INSERT OR REPLACE INTO literal_search_state VALUES(?,?)', ('built_'+source, '1'))
        if not self.stopped.is_set():
            with self.catalog.connect() as c:
                c.execute("INSERT OR REPLACE INTO literal_search_state VALUES('ready','1')")

    def _save(self, values):
        if values:
            with self.catalog.connect() as c:
                c.executemany('INSERT OR IGNORE INTO literal_search_grams VALUES(?,?,?)', values)

    def process(self):
        # 同一小事务读取、更新并清除队列；写入者随后再次排队，不能漏掉新值。
        learned = {}
        with self.catalog.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            pending = c.execute('SELECT source,item FROM literal_search_dirty LIMIT 16').fetchall()
            for source, item in pending:
                _, table, column = SOURCES[source]
                identity = 'id' if source == 'path' else 'item'
                row = c.execute(f'SELECT {column} FROM {table} WHERE {identity}=?', (item,)).fetchone()
                c.execute('DELETE FROM literal_search_short WHERE source=? AND item=?', (source, item))
                if row:
                    text = row[0]
                    if 1 <= len(text) <= 2:
                        c.execute('INSERT INTO literal_search_short VALUES(?,?,?)', (source, item, text))
                    normalized = text.lower()
                    grams = {normalized[i:i+3] for i in range(max(0, len(normalized)-2))}
                    if source not in self.known:
                        self.known[source] = {row[0] for row in c.execute('SELECT DISTINCT gram FROM literal_search_grams WHERE source=?', (source,))}
                    grams -= self.known[source]
                    c.executemany('INSERT OR IGNORE INTO literal_search_grams VALUES(?,?,?)',
                                  ((source, part, gram) for gram in grams for part in fragments(gram)))
                    learned.setdefault(source,set()).update(grams)
                c.execute('DELETE FROM literal_search_dirty WHERE source=? AND item=?', (source, item))
        for source, grams in learned.items():self.known[source].update(grams)
        return bool(pending)


def fragments(gram):
    return {gram[i:i+length] for length in (1, 2) for i in range(len(gram)-length+1)}


def rowids(c, source, text):
    """返回当前快照中的字面匹配候选；尚未索引的变更走有界增量集合。"""
    fts, table, column = SOURCES[source]
    if len(text) >= 3 and '\0' not in text:
        return f'SELECT rowid FROM {fts} WHERE {fts} MATCH ?', ['"'+text.replace('"', '""')+'"']
    if not text or '\0' in text or not LiteralSearchIndex.ready(c):
        return None, []
    # Unicode 大小写扩展可能产生候选超集，最终字面复核仍保留。
    grams = [row[0] for row in c.execute('SELECT gram FROM literal_search_grams WHERE source=? AND fragment=?', (source, text.lower()))]
    identity = 'id' if source == 'path' else 'item'
    parts = [f'''SELECT t.rowid FROM literal_search_short s CROSS JOIN {table} t ON t.{identity}=s.item
 WHERE s.source=? AND instr(lower(s.text),lower(?))>0 AND instr(lower(t.{column}),lower(?))>0''',
             f'''SELECT t.rowid FROM literal_search_dirty d CROSS JOIN {table} t ON t.{identity}=d.item
 WHERE d.source=? AND instr(lower(t.{column}),lower(?))>0''']
    args = [source, text, text, source, text]
    # 一字符宽词可能对应数千片段，分组避免 MATCH 表达式深度限制。
    for start in range(0, len(grams), 128):
        expression = ' OR '.join('"'+gram.replace('"', '""')+'"' for gram in grams[start:start+128])
        parts.append(f'SELECT rowid FROM {fts} WHERE {fts} MATCH ?'); args.append(expression)
    return ' UNION '.join(parts), args


def candidates(catalog, c, text, *, prefix=False, files_only=False):
    """候选直接驱动主键/配对索引，宽泛词也不退回逐行 OR 全库查询。"""
    path, path_args = rowids(c, 'path', text)
    metadata, metadata_args = rowids(c, 'metadata', text) if not files_only else (None,[])
    if path is None or not catalog.fast_index.ready or not files_only and (metadata is None or not catalog.metadata.fts):
        return None, []
    if files_only:
        pairs=f'''SELECT p.pair_key FROM ({path}) hits CROSS JOIN items p ON p.rowid=hits.rowid
 WHERE p.online=1 AND instr(lower(p.relative),lower(?))>0'''
        pair_args=path_args+[text]
    else:
        pairs = f'''SELECT p.pair_key FROM ({path}) hits
 CROSS JOIN items p ON p.rowid=hits.rowid WHERE p.online=1
 UNION SELECT p.pair_key FROM ({metadata}) hits
 CROSS JOIN catalog_metadata m ON m.rowid=hits.rowid
 CROSS JOIN items p ON p.id=m.item WHERE p.online=1 AND p.fingerprint=m.fingerprint'''
    # Trigram 大小写规则比 SQLite lower 宽；字母查询仍复核，数字/中文
    # 无大小写差异时倒排已经证明字面包含，无需展开数 GB 的 EXIF 文本。
    safe = all(ch.isdigit() and ch.isascii() or '\u3400' <= ch <= '\u9fff' or not ch.isalpha() for ch in text)
    if not files_only and not safe:
        pairs = f'''SELECT p.pair_key FROM ({path}) hits
 CROSS JOIN items p ON p.rowid=hits.rowid WHERE p.online=1 AND instr(lower(p.relative),lower(?))>0
 UNION SELECT p.pair_key FROM ({metadata}) hits
 CROSS JOIN catalog_metadata m ON m.rowid=hits.rowid
 CROSS JOIN items p ON p.id=m.item WHERE p.online=1 AND p.fingerprint=m.fingerprint AND instr(lower(m.search_text),lower(?))>0'''
        pair_args = path_args+[text]+metadata_args+[text]
    elif not files_only:
        pair_args = path_args+metadata_args
    lanes = [f'''SELECT matched.id FROM ({pairs}) pairs
 CROSS JOIN items matched INDEXED BY catalog_pair ON matched.pair_key=pairs.pair_key WHERE matched.online=1''']
    args = pair_args
    from sku_match import normalize
    key = normalize(text)
    if key:
        if prefix:
            upper = key[:-1]+chr(ord(key[-1])+1)
            lanes.append('SELECT id FROM items INDEXED BY catalog_items_sku WHERE online=1 AND normalized>=? AND normalized<?')
            args += [key, upper]
        else:
            lanes.append('SELECT id FROM items INDEXED BY catalog_items_sku WHERE online=1 AND normalized LIKE ?')
            args.append('%'+key+'%')
    if not files_only and getattr(catalog, 'intelligence', None):
        from retrieval_store import condition
        visual, visual_args = condition(c, text)
        lanes.append('SELECT id FROM items WHERE '+visual); args += visual_args
    return ' UNION '.join(lanes), args


def point_condition(c, text, *, visual=True, files_only=False):
    """按当前项点查倒排；密集命中沿时间索引取满一页即可停止。"""
    expressions = []
    for source in ('path',) if files_only else SOURCES:
        if len(text) >= 3 and '\0' not in text:
            expressions.append('"'+text.replace('"', '""')+'"')
        elif text and '\0' not in text and LiteralSearchIndex.ready(c):
            grams = [r[0] for r in c.execute('SELECT gram FROM literal_search_grams WHERE source=? AND fragment=?', (source, text.lower()))]
            if len(grams) > 512:
                return None, []
            expressions.append(' OR '.join('"'+gram.replace('"','""')+'"' for gram in grams))
        else:
            return None, []
    path=expressions[0];metadata=expressions[1] if not files_only else ''
    path_sql = 'EXISTS(SELECT 1 FROM reference_path_fts WHERE rowid=p.rowid AND reference_path_fts MATCH ?)' if path else '0'
    metadata_sql = 'EXISTS(SELECT 1 FROM catalog_metadata_fts WHERE rowid=m.rowid AND catalog_metadata_fts MATCH ?)' if metadata else '0'
    if len(text)<3:
        path_sql += " OR EXISTS(SELECT 1 FROM literal_search_short WHERE source='path' AND item=p.id)"
        metadata_sql += " OR EXISTS(SELECT 1 FROM literal_search_short WHERE source='metadata' AND item=m.item)"
    # dirty 检查保证刚写入但词典尚未补齐的新片段立即可搜索。
    path_sql += " OR EXISTS(SELECT 1 FROM literal_search_dirty WHERE source='path' AND item=p.id)"
    metadata_sql += " OR EXISTS(SELECT 1 FROM literal_search_dirty WHERE source='metadata' AND item=m.item)"
    if files_only:
        return f'''EXISTS(SELECT 1 FROM items p INDEXED BY catalog_pair WHERE p.pair_key=i.pair_key AND p.online=1
 AND ({path_sql}) AND instr(lower(p.relative),lower(?))>0)''',([path] if path else [])+[text]
    condition = f'''EXISTS(SELECT 1 FROM items p INDEXED BY catalog_pair WHERE p.pair_key=i.pair_key AND p.online=1 AND (
 (({path_sql}) AND instr(lower(p.relative),lower(?))>0) OR
 EXISTS(SELECT 1 FROM catalog_metadata m WHERE m.item=p.id AND m.fingerprint=p.fingerprint
 AND ({metadata_sql}) AND instr(lower(m.search_text),lower(?))>0)))'''
    args = ([path] if path else [])+[text]+([metadata] if metadata else [])+[text]
    if visual:
        from retrieval_store import row_condition
        visual_sql, visual_args = row_condition(text)
        return '('+condition+' OR '+visual_sql+')', args+visual_args
    return condition, args


def page_keys(catalog, c, text, filters, args, candidate_sql, candidate_args, *, limit,
              ranked=False, marker=None, offset=0, cancel=None, deadline=None, roots=False,files_only=False,
              table='items',index_map=None,point_override=None,probe_seconds=None):
    """小预算尝试密集点查，稀疏词退回倒排候选；两路均保持完整分页语义。"""
    from catalog_search_requests import SearchCancelled
    from sku_match import normalize
    point, point_args = point_override if point_override is not None else point_condition(c, text,visual=bool(getattr(catalog,'intelligence',None)),files_only=files_only)
    if point is None:
        return None
    key = normalize(text); upper = key[:-1]+chr(ord(key[-1])+1) if key else ''
    lanes = []
    if ranked and key:
        lanes += [(0, 'i.normalized=?', [key], '', []),
                  (1, 'i.normalized>? AND i.normalized<?', [key, upper], '', []),
                  (2, '(i.normalized<? OR i.normalized>=?)', [key, upper], point, point_args)]
    else:
        match = '('+point+' OR i.normalized LIKE ?)' if key else point
        lanes = [(2 if ranked else 0, '1', [], match, point_args+(['%'+key+'%'] if key else []))]
    keys = []; deadline = deadline or time.monotonic()+2
    cancelled = lambda: bool(cancel and cancel())
    progress = lambda: int(cancelled() or time.monotonic()>deadline)
    join = ' CROSS JOIN roots ON roots.id=i.root' if roots else ''
    for priority, extra, extra_args, match, match_args in lanes:
        if marker and priority < marker[0]:
            continue
        sku_index=(index_map or {}).get('catalog_items_sku','catalog_items_sku')
        if ranked and key and priority==1 and not c.execute('SELECT 1 FROM '+table+' INDEXED BY '+sku_index+' WHERE normalized>? AND normalized<? AND online=1 LIMIT 1',(key,upper)).fetchone():
            continue
        checks = list(filters)+[extra]; values = list(args)+extra_args
        if marker and priority == marker[0]:
            checks.append('(i.modified<? OR (i.modified=? AND i.id>?))')
            values += [-marker[1], -marker[1], marker[2]]
        count = limit+1-len(keys)
        if count <= 0:
            break
        fast_checks = checks+([match] if match else [])
        # exact SKU 的 (normalized,online,modified,id) 已按结果顺序排列。
        scoped_root = any(value in filters for value in ('i.root=?','root=?'))
        scoped_media = any(value in filters for value in ('i.media_type=?','media_type=?'))
        driver = ('catalog_items_sku' if ranked and key and priority == 0 else
                  'catalog_root_media_page' if scoped_root and scoped_media else
                  'catalog_items_browse' if scoped_root else
                  'catalog_media_page' if scoped_media else 'intelligence_catalog_order')
        driver=(index_map or {}).get(driver,driver)
        quick_deadline = min(deadline, time.monotonic()+(probe_seconds if probe_seconds is not None else .02 if files_only else .08))
        c.set_progress_handler(lambda: int(cancelled() or time.monotonic()>quick_deadline), 1000)
        sql = f'SELECT i.id,i.modified,{priority} AS priority FROM {table} i INDEXED BY {driver}'+join
        try:
            found = [dict(row) for row in c.execute(sql+' WHERE '+' AND '.join(fast_checks)+
                    ' ORDER BY i.modified DESC,i.id LIMIT ? OFFSET ?', values+match_args+[count, offset])]
        except sqlite3.OperationalError as error:
            if cancelled():
                raise SearchCancelled() from None
            if getattr(error, 'sqlite_errorcode', None) != sqlite3.SQLITE_INTERRUPT:
                raise
            c.set_progress_handler(progress, 5000)
            if ranked and key and priority < 2:
                source = table+' i INDEXED BY '+sku_index
                parameters = values
            else:
                source = '('+candidate_sql+') candidates CROSS JOIN '+table+' i ON i.id=candidates.id'
                parameters = candidate_args+values
            fallback = f'SELECT i.id,i.modified,{priority} AS priority FROM '+source+join
            found = [dict(row) for row in c.execute(fallback+' WHERE '+' AND '.join(checks)+
                    ' ORDER BY i.modified DESC,i.id LIMIT ? OFFSET ?', parameters+[count, offset])]
        finally:
            c.set_progress_handler(progress, 5000)
        keys.extend(found)
    return keys
