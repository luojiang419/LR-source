"""归档库内的版本化多维检索存储；媒体数据不入库。"""
import array
import base64
import heapq
import json
import random
import sqlite3
import time

from retrieval_schema import digest, dumps, normalized, tokens
from sku_match import normalize


def install(c):
    c.executescript('''
CREATE TABLE IF NOT EXISTS intelligence_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intelligence_meta(id INTEGER PRIMARY KEY CHECK(id=1),generation INTEGER NOT NULL);
INSERT OR IGNORE INTO intelligence_meta VALUES(1,0);
CREATE TABLE IF NOT EXISTS intelligence_dirty(item TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS intelligence_jobs(
 id TEXT PRIMARY KEY,item TEXT NOT NULL,fingerprint TEXT NOT NULL,config TEXT NOT NULL,
 status TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '',
 due REAL NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(item,fingerprint,config));
CREATE INDEX IF NOT EXISTS intelligence_queue ON intelligence_jobs(status,due,created);
CREATE INDEX IF NOT EXISTS intelligence_job_item ON intelligence_jobs(item,updated DESC);
CREATE TABLE IF NOT EXISTS intelligence_results(
 item TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,config TEXT NOT NULL,provider TEXT NOT NULL,
 frames TEXT NOT NULL,summary TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS intelligence_dimensions(
 item TEXT NOT NULL,frame INTEGER NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,normalized TEXT NOT NULL,
 PRIMARY KEY(item,frame,key));
CREATE INDEX IF NOT EXISTS intelligence_dimension_lookup ON intelligence_dimensions(key,normalized,item,frame);
CREATE TABLE IF NOT EXISTS intelligence_vectors(
 item TEXT NOT NULL,frame INTEGER NOT NULL,backend TEXT NOT NULL,fingerprint TEXT NOT NULL,
 vector BLOB NOT NULL,PRIMARY KEY(item,frame,backend));
CREATE INDEX IF NOT EXISTS intelligence_vector_backend ON intelligence_vectors(backend,item,frame);
CREATE TABLE IF NOT EXISTS intelligence_lsh(
 backend TEXT NOT NULL,band INTEGER NOT NULL,bucket INTEGER NOT NULL,item TEXT NOT NULL,frame INTEGER NOT NULL,
 PRIMARY KEY(backend,band,bucket,item,frame));
CREATE INDEX IF NOT EXISTS intelligence_lsh_item ON intelligence_lsh(item);
CREATE TABLE IF NOT EXISTS intelligence_groups(
 id TEXT PRIMARY KEY,root TEXT NOT NULL,folder TEXT NOT NULL,signature TEXT NOT NULL,
 sku TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,evidence TEXT NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS intelligence_group_state ON intelligence_groups(status,updated DESC);
CREATE INDEX IF NOT EXISTS intelligence_catalog_order ON items(online,modified DESC,id);
CREATE VIRTUAL TABLE IF NOT EXISTS intelligence_fts USING fts5(item,terms,tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS intelligence_insert AFTER INSERT ON items BEGIN
 INSERT OR IGNORE INTO intelligence_dirty VALUES(new.id); END;
CREATE TRIGGER IF NOT EXISTS intelligence_update AFTER UPDATE OF fingerprint,online ON items
 WHEN old.fingerprint IS NOT new.fingerprint OR old.online IS NOT new.online BEGIN
 INSERT OR IGNORE INTO intelligence_dirty VALUES(new.id);
 UPDATE intelligence_meta SET generation=generation+1 WHERE id=1; END;
CREATE TRIGGER IF NOT EXISTS intelligence_sku AFTER UPDATE OF sku ON items
 WHEN old.sku IS NOT new.sku BEGIN
 UPDATE intelligence_meta SET generation=generation+1 WHERE id=1; END;
CREATE TRIGGER IF NOT EXISTS intelligence_group_invalidate AFTER UPDATE OF fingerprint ON items
 WHEN old.fingerprint IS NOT new.fingerprint AND old.source='vlm_group' BEGIN
 UPDATE items SET sku='',normalized='',source='unmatched',confidence=NULL WHERE id=new.id;
 UPDATE intelligence_groups SET status='review',reason='源文件已变化，等待重新解析与核对'
 WHERE root=new.root AND folder=new.folder AND status='confirmed'; END;
CREATE TRIGGER IF NOT EXISTS intelligence_delete AFTER DELETE ON items BEGIN
 DELETE FROM intelligence_results WHERE item=old.id;
 DELETE FROM intelligence_dimensions WHERE item=old.id;
 DELETE FROM intelligence_vectors WHERE item=old.id;
 DELETE FROM intelligence_lsh WHERE item=old.id;
 DELETE FROM intelligence_fts WHERE item=old.id;
 DELETE FROM intelligence_dirty WHERE item=old.id;
 UPDATE intelligence_meta SET generation=generation+1 WHERE id=1; END;
''')
    # 未发行开发库的第一版 item 未参与倒排。保留结果，升级全文表后按资产 ID 与词交集检索。
    definition = c.execute("SELECT sql FROM sqlite_master WHERE name='intelligence_fts'").fetchone()[0]
    if 'UNINDEXED' in definition.upper():
        c.executescript('''BEGIN IMMEDIATE;
CREATE TABLE intelligence_fts_upgrade AS SELECT item,terms FROM intelligence_fts;
DROP TABLE intelligence_fts;
CREATE VIRTUAL TABLE intelligence_fts USING fts5(item,terms,tokenize='unicode61');
INSERT INTO intelligence_fts(item,terms) SELECT item,terms FROM intelligence_fts_upgrade;
DROP TABLE intelligence_fts_upgrade;
COMMIT;''')


_PLANES = {}


def bands(vector):
    import numpy as np
    size = len(vector)
    if size not in _PLANES:
        rng = np.random.default_rng(419)
        _PLANES[size] = rng.standard_normal((80, size), dtype=np.float32)
    bits = (_PLANES[size] @ np.asarray(vector, dtype=np.float32)) > 0
    return [sum(int(bit) << shift for shift, bit in enumerate(bits[start:start+10]))
            for start in range(0, 80, 10)]


def store_vector(c, item, frame, backend, fingerprint, vector):
    packed = array.array('f', vector).tobytes()
    c.execute('INSERT OR REPLACE INTO intelligence_vectors VALUES(?,?,?,?,?)',
              (item, frame, backend, fingerprint, packed))
    c.execute('DELETE FROM intelligence_lsh WHERE item=? AND frame=? AND backend=?', (item, frame, backend))
    c.executemany('INSERT INTO intelligence_lsh VALUES(?,?,?,?,?)',
                  [(backend, band, bucket, item, frame) for band, bucket in enumerate(bands(vector))])


def save_result(c, item, fingerprint, config, provider, frames, vectors):
    c.execute('DELETE FROM intelligence_fts WHERE item=?', (item,))
    c.execute('DELETE FROM intelligence_dimensions WHERE item=?', (item,))
    c.execute('DELETE FROM intelligence_vectors WHERE item=?', (item,))
    c.execute('DELETE FROM intelligence_lsh WHERE item=?', (item,))
    text = []
    for index, frame in enumerate(frames):
        value = frame['result'];text.extend([value['summary'], value['visible_text'], *value['keywords']])
        for key, content in {**value['attributes'], **value.get('custom', {})}.items():
            c.execute('INSERT INTO intelligence_dimensions VALUES(?,?,?,?,?)',
                      (item, index, key, content, normalized(content)))
            text.append(content)
    summary = ' / '.join(frame['result']['summary'] for frame in frames)
    c.execute('INSERT OR REPLACE INTO intelligence_results VALUES(?,?,?,?,?,?,?)',
              (item, fingerprint, config, provider, dumps(frames), summary, time.time()))
    c.execute('INSERT INTO intelligence_fts(item,terms) VALUES(?,?)', (item, ' '.join(tokens(' '.join(text)))))
    for frame, backend, vector in vectors:
        store_vector(c, item, frame, backend, fingerprint, vector)
    c.execute('UPDATE intelligence_meta SET generation=generation+1 WHERE id=1')


def condition(c, text):
    terms = tokens(text)[:80]
    if not terms:
        return '0', []
    expression = ' AND '.join('"'+term.replace('"', '""')+'"' for term in terms)
    return '''id IN (SELECT r.item FROM intelligence_results r JOIN items current ON current.id=r.item
      WHERE current.fingerprint=r.fingerprint AND r.item IN
      (SELECT item FROM intelligence_fts WHERE intelligence_fts MATCH ?))''', [expression]


def row_condition(text):
    terms = tokens(text)[:80]
    if len(text.strip()) == 1 and '\u3400' <= text.strip() <= '\u9fff':
        return '''EXISTS(SELECT 1 FROM intelligence_results r WHERE r.item=i.id AND r.fingerprint=i.fingerprint
            AND EXISTS(SELECT 1 FROM intelligence_fts WHERE intelligence_fts MATCH ('item:"'||i.id||'"')
            AND instr(terms,?)>0))''', [text.strip()]
    expression = ' AND '.join('terms:"'+term.replace('"','""')+'"' for term in terms)
    if not expression:return '0', []
    return '''EXISTS(SELECT 1 FROM intelligence_results r WHERE r.item=i.id AND r.fingerprint=i.fingerprint
        AND EXISTS(SELECT 1 FROM intelligence_fts WHERE intelligence_fts MATCH
        ('item:"'||i.id||'" AND ('||?||')')))''', [expression]


def query(catalog, text='', facets=None, root='', media='', cursor='', limit=60):
    start = time.perf_counter();facets = facets or {}
    if not isinstance(text, str) or len(text) > 300 or not isinstance(facets, dict) or len(facets) > 20:
        raise ValueError('检索条件过长')
    limit = max(1, min(100, int(limit)))
    signature = digest([text, facets, root, media])
    clauses = ['i.online=1', 'roots.removed=0'];args = []
    if root:
        clauses.append('i.root=?');args.append(root)
    if media:
        if media not in ('image', 'video', 'design'):
            raise ValueError('媒体类型无效')
        clauses.append('i.media_type=?');args.append(media)
    with catalog.connect() as c:
        generation = c.execute('SELECT generation FROM intelligence_meta WHERE id=1').fetchone()[0]
        key = normalize(text)
        rank = "CASE WHEN i.normalized=? AND ?!='' THEN 0 WHEN i.normalized>=? AND i.normalized<? AND ?!='' THEN 1 ELSE 2 END"
        upper = key[:-1]+chr(ord(key[-1])+1) if key else ''
        rank_args = [key, key, key, upper, key]
        if text.strip():
            visual, values = condition(c, text)
            path, path_values = catalog.fast_index.condition(c, text)
            metadata, metadata_values = catalog.metadata.condition(text)
            # 各召回路先利用自身倒排/业务索引，再合并 ID；不让一个 OR 条件驱动整库视图扫描。
            lanes = ['SELECT id FROM items WHERE '+visual,
                     'SELECT id FROM items WHERE '+path,
                     'SELECT id FROM items WHERE '+metadata]
            lane_args = values+path_values+metadata_values
            if key:
                lanes.insert(0, 'SELECT id FROM items INDEXED BY catalog_items_sku WHERE normalized>=? AND normalized<?')
                lane_args = [key, upper]+lane_args
            if not key and len(text) < 3:
                # 两字中文是常见颜色/品类。路径与 EXIF 的 trigram 不覆盖此长度，
                # 按当前有序候选检查字面匹配，避免提前扫描百万条路径/元数据。
                point, point_args = row_condition(text)
                clauses.append('''('''+point+'''
                    OR instr(lower(i.relative),lower(?))>0 OR EXISTS(
                    SELECT 1 FROM items pair JOIN catalog_metadata m ON m.item=pair.id
                    WHERE pair.pair_key=i.pair_key AND pair.online=1 AND m.fingerprint=pair.fingerprint
                    AND instr(lower(m.search_text),lower(?))>0))''')
                args.extend(point_args+[text,text])
            else:
                union = ' UNION ALL '.join(lanes)
                if not cursor and not c.execute('SELECT 1 FROM ('+union+') LIMIT 1', lane_args).fetchone():
                    return {'rows':[], 'has_more':False, 'next_cursor':'', 'elapsed_ms':round((time.perf_counter()-start)*1000,2)}
                clauses.append('(i.id IN ('+union+'))')
                args.extend(lane_args)
        if facets:
            checks = []
            for name, value in facets.items():
                if not isinstance(name, str) or not isinstance(value, str) or len(value) > 200 or not value.strip():
                    raise ValueError('维度筛选无效')
                checks.append('EXISTS(SELECT 1 FROM intelligence_dimensions d WHERE d.item=f.item AND d.frame=f.frame AND d.key=? AND d.normalized=?)')
                args.extend([name, normalized(value)])
            # 各维度必须在同一帧同时成立，避免视频跨帧属性拼接。
            clauses.append('EXISTS(SELECT 1 FROM intelligence_dimensions f JOIN intelligence_results r ON r.item=f.item '
                           'WHERE f.item=i.id AND r.fingerprint=i.fingerprint AND '+' AND '.join(checks)+')')
        marker = None
        if cursor:
            try:
                if len(cursor) > 2000:raise ValueError()
                marker = json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
                if marker['q'] != signature or marker['g'] != generation or len(marker['key']) != 3:raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ValueError('索引或条件已更新，请重新搜索') from None
        # 高频颜色/品类可命中数十万项。沿时间索引流式取一页，不能先排序整批大 JSON 或全部候选。
        ordered_scan = not key
        canonical = '''(i.media_type<>'image' OR NOT EXISTS(SELECT 1 FROM items b
            WHERE b.pair_key=i.pair_key AND b.online=1 AND
            (CASE WHEN b.source='manual' THEN 0 ELSE 2 END + CASE WHEN b.extension IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,b.id)
            <(CASE WHEN i.source='manual' THEN 0 ELSE 2 END + CASE WHEN i.extension IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,i.id)))'''
        clauses.append(canonical)
        source = 'items i INDEXED BY intelligence_catalog_order' if ordered_scan else 'items i'
        sql = 'SELECT i.id AS id,i.modified AS modified,'+rank+' AS priority FROM '+source+' JOIN roots ON roots.id=i.root WHERE '+' AND '.join(clauses)
        all_args = rank_args+args
        if marker:
            if ordered_scan:
                modified = -float(marker['key'][1])
                sql += ' AND i.modified<=? AND (i.modified<? OR i.id>?)'
                all_args.extend([modified,modified,marker['key'][2]])
            else:
                sql = 'SELECT * FROM ('+sql+') WHERE (priority,-modified,id)>(?,?,?)'
                all_args.extend(marker['key'])
        deadline = time.monotonic()+2
        c.set_progress_handler(lambda: int(time.monotonic() > deadline), 5000)
        try:
            ordering = 'modified DESC,id' if ordered_scan else 'priority,modified DESC,id'
            keys = [dict(row) for row in c.execute(sql+' ORDER BY '+ordering+' LIMIT ?', all_args+[limit+1])]
            rows = []
            if keys:
                marks = ','.join('?' for _ in keys)
                hydrated = {row['id']:dict(row) for row in c.execute('''SELECT i.*,r.summary,r.frames FROM capture_items i
                    LEFT JOIN intelligence_results r ON r.item=i.id AND r.fingerprint=i.fingerprint
                    WHERE i.id IN ('''+marks+')', [row['id'] for row in keys])}
                rows = [{**hydrated[row['id']], 'priority':row['priority']} for row in keys]
        except sqlite3.OperationalError as exc:
            if 'interrupt' in str(exc):raise ValueError('检索超过时间预算，请缩小关键词或归档范围') from None
            raise
    more = len(rows) > limit;rows = rows[:limit]
    for row in rows:
        row['reasons'] = ['SKU 精确命中' if row['priority'] == 0 else 'SKU 前缀命中' if row['priority'] == 1 else '关键词 / 多维资料命中' if text else '归档素材']
        if row['source']=='path_rule':row['reasons'].append('路径款号候选 · 尚未内容确认')
        row['preview'] = '/api/catalog/preview/'+row['id']
        row['frames'] = json.loads(row['frames']) if row['frames'] else []
    next_cursor = ''
    if more and rows:
        last = rows[-1]
        next_cursor = base64.urlsafe_b64encode(dumps({'q': signature, 'g': generation,
                           'key': [last['priority'], -last['modified'], last['id']]}).encode()).decode().rstrip('=')
    return {'rows': rows, 'has_more': more, 'next_cursor': next_cursor, 'elapsed_ms': round((time.perf_counter()-start)*1000, 2)}


def image_query(catalog, vector, backend, *, limit=60, root='', threshold=0):
    """小库精确余弦，大库多表 LSH 近似召回后精排，内存与候选数均有上界。"""
    import numpy as np
    start = time.perf_counter();limit = max(1, min(100, int(limit)))
    threshold = float(threshold)
    if not 0 <= threshold <= 1:raise ValueError('相似度阈值应为 0–1')
    query_vector = np.asarray(vector, dtype=np.float32);scope = '';args = [backend]
    if root:scope = ' AND i.root=?';args.append(root)
    with catalog.connect() as c:
        count = c.execute('''SELECT count(*) FROM intelligence_vectors v JOIN capture_items i ON i.id=v.item
            JOIN roots ON roots.id=i.root WHERE v.backend=? AND v.fingerprint=i.fingerprint
            AND i.online=1 AND roots.removed=0'''+scope, args).fetchone()[0]
        approximate = count > 10000
        candidate_sql = '';candidate_args = []
        if approximate:
            buckets = bands(vector);tests = []
            for band, bucket in enumerate(buckets):
                # 探测同桶与一位邻桶，提升相似图召回；候选仍有硬上限。
                probes = [bucket]+[bucket ^ (1 << bit) for bit in range(10)]
                tests.append('(band=? AND bucket IN ('+','.join('?' for _ in probes)+'))')
                candidate_args.extend([band, *probes])
            inner_scope = ' AND li.root=?' if root else ''
            candidate_sql = ''' AND (v.item,v.frame) IN (SELECT l.item,l.frame FROM intelligence_lsh l
                JOIN items li ON li.id=l.item JOIN roots lr ON lr.id=li.root
                WHERE l.backend=? AND li.online=1 AND lr.removed=0'''+inner_scope+''' AND ('''+ ' OR '.join(tests)+''') GROUP BY l.item,l.frame
                ORDER BY count(*) DESC,l.item,l.frame LIMIT 6000)'''
            if root:candidate_args.insert(0, root)
            candidate_args.insert(0, backend)
        sql = '''SELECT v.item,v.frame,v.vector FROM intelligence_vectors v JOIN capture_items i ON i.id=v.item
            JOIN roots ON roots.id=i.root WHERE v.backend=? AND v.fingerprint=i.fingerprint AND i.online=1
            AND roots.removed=0'''+scope+candidate_sql
        best = {};scanned = 0
        deadline = time.monotonic()+3
        c.set_progress_handler(lambda: int(time.monotonic() > deadline), 5000)
        incomplete = False
        try:
            result = c.execute(sql, args+candidate_args)
            while True:
                batch = result.fetchmany(256)
                if not batch:break
                matrix = np.stack([np.frombuffer(row['vector'], dtype=np.float32) for row in batch])
                scores = matrix @ query_vector;scanned += len(batch)
                for row, score in zip(batch, scores):
                    score = float(score)
                    if score >= threshold and (row['item'] not in best or score > best[row['item']][0]):
                        best[row['item']] = (score, row['frame'])
                if time.monotonic() > deadline:incomplete = True;break
        except sqlite3.OperationalError as exc:
            if 'interrupt' not in str(exc):raise
            incomplete = True
        ranked = heapq.nlargest(limit, best, key=lambda item: (best[item][0], item))
        rows = []
        for item in ranked:
            row = dict(c.execute('SELECT i.*,r.summary FROM capture_items i LEFT JOIN intelligence_results r ON r.item=i.id WHERE i.id=?', (item,)).fetchone())
            score, frame = best[item]
            row.update(score=round(score, 5), frame=frame, preview='/api/catalog/preview/'+item,
                       reasons=[('CLIP 语义相似' if backend.startswith('chinese-clip') else '布局与配色相似')+f' {score:.1%}'])
            rows.append(row)
    return {'rows': rows, 'backend': backend, 'approximate': approximate, 'incomplete': incomplete,
            'candidates': scanned, 'indexed_vectors': count, 'has_more': False, 'next_cursor': '',
            'elapsed_ms': round((time.perf_counter()-start)*1000, 2)}
