"""同步元数据的独立增量检索投影；查询不取得业务/预览/归档写锁。"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

from core import canonical, connect, normalize


class SearchIndex:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        with connect(self.path) as c:
            c.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS docs(rowid INTEGER PRIMARY KEY,entity TEXT UNIQUE,project TEXT,
                    normalized TEXT,sku TEXT,scope TEXT,origin TEXT,active INTEGER,conflict INTEGER,version TEXT,ocr_text TEXT,description TEXT);
                CREATE INDEX IF NOT EXISTS docs_project_sku ON docs(project,normalized,entity);
                CREATE INDEX IF NOT EXISTS docs_scope_sku ON docs(project,scope,normalized,entity);
                CREATE VIRTUAL TABLE IF NOT EXISTS terms USING fts5(normalized,ocr_text,description,content='docs',content_rowid='rowid',tokenize='trigram');
                CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
                    INSERT INTO terms(rowid,normalized,ocr_text,description) VALUES(new.rowid,new.normalized,new.ocr_text,new.description); END;
                CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
                    INSERT INTO terms(terms,rowid,normalized,ocr_text,description) VALUES('delete',old.rowid,old.normalized,old.ocr_text,old.description); END;
                CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
                    INSERT INTO terms(terms,rowid,normalized,ocr_text,description) VALUES('delete',old.rowid,old.normalized,old.ocr_text,old.description);
                    INSERT INTO terms(rowid,normalized,ocr_text,description) VALUES(new.rowid,new.normalized,new.ocr_text,new.description); END;
            ''')

    def publish(self, rows):
        with self.lock, connect(self.path) as c:
            c.executemany('''INSERT INTO docs(entity,project,normalized,sku,scope,origin,active,conflict,version,ocr_text,description)
                VALUES(:entity,:project,:normalized,:sku,:scope,:origin,:active,:conflict,:version,:ocr_text,:description)
                ON CONFLICT(entity) DO UPDATE SET normalized=excluded.normalized,sku=excluded.sku,scope=excluded.scope,origin=excluded.origin,
                    active=excluded.active,conflict=excluded.conflict,version=excluded.version,ocr_text=excluded.ocr_text,description=excluded.description''', rows)

    def search(self, project, query, *, mode='sku', limit=60, after=0):
        started = time.perf_counter()
        if not isinstance(query, str) or len(query) > 200 or mode not in ('sku', 'exact', 'prefix', 'text'):
            raise ValueError('搜索条件无效')
        query = query.strip()
        key = normalize(query)
        if not query or not key and mode != 'text':
            return dict(rows=[], elapsed_ms=0, has_more=False)
        limit = max(1, min(100, int(limit)))
        with connect(self.path) as c:
            deadline = time.monotonic() + 1
            c.set_progress_handler(lambda: int(time.monotonic() > deadline), 5000)
            columns = 'entity,sku,scope,origin,active,conflict,version'
            if mode == 'exact':
                sql = f'SELECT {columns} FROM docs WHERE project=? AND normalized=? ORDER BY entity LIMIT ?'
                rows = c.execute(sql, (project, key, limit + 1)).fetchall()
            elif mode == 'prefix' or len(key) < 3 and mode == 'sku':
                upper = key[:-1] + chr(ord(key[-1]) + 1)
                sql = f'SELECT {columns} FROM docs WHERE project=? AND normalized>=? AND normalized<? ORDER BY normalized,entity LIMIT ?'
                rows = c.execute(sql, (project, key, upper, limit + 1)).fetchall()
            else:
                # FTS 行号键集分页：不按全命中集重排，不使用 OFFSET/COUNT 或 %LIKE%。
                term = ('normalized : ' if mode == 'sku' else '') + '"' + (key if mode == 'sku' else query).replace('"', '""') + '"'
                sql = f'SELECT d.rowid,{",".join("d."+p for p in columns.split(","))} FROM terms t JOIN docs d ON d.rowid=t.rowid WHERE terms MATCH ? AND d.project=? AND t.rowid>? ORDER BY t.rowid LIMIT ?'
                rows = c.execute(sql, (term, project, int(after), limit + 1)).fetchall()
            output = [dict(r) for r in rows[:limit]]
        return dict(rows=output, has_more=len(rows) > limit, next_rowid=output[-1].get('rowid', 0) if output else 0,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 3))


class SearchServer:
    """只用于本机验证的真实 HTTP 搜索入口，不是生产公网服务。"""
    def __init__(self, index, project):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                try:
                    url = urlsplit(self.path)
                    if url.path != '/search':
                        self.send_error(404)
                        return
                    q = parse_qs(url.query, max_num_fields=5)
                    result = index.search(project, q.get('q', [''])[0], mode=q.get('mode', ['sku'])[0])
                    raw = canonical(result).encode()
                    self.send_response(200)
                except (ValueError, sqlite3.Error) as exc:
                    raw = canonical({'error': str(exc)}).encode()
                    self.send_response(400)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.http.server_port}'

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)
