"""持久路径倒排索引：后台迁移、增量维护，查询失败时保留普通目录搜索。"""
from __future__ import annotations

from contextlib import suppress
from pathlib import Path
import sqlite3
import threading


class FastPathIndex:
    """在原始 items 表旁保存 FTS5 trigram 候选，不读取照片或修改业务记录。

    索引尚未建成、SQLite 不支持 trigram、查询不足三个字符时，使用原生
    SQLite literal substring 查询。触发器只在完整索引提交后启用，避免迁移
    中对尚未入索引的存量行发出 FTS delete。路径更改才更新 tokens，识别
    结果、在线状态、配对信息等元数据更新无需重建路径索引。
    """

    def __init__(self, db_path, *, background=True):
        self.db_path = Path(db_path)
        self._ready = False
        self._closed = threading.Event()
        self._worker = None
        self._connection_lock = threading.Lock()
        self._building_connection = None
        self.error = ''
        # 长时间的写锁只影响后台工作；不能阻塞首次目录查询。
        try:
            self._prepare(timeout=0.15)
        except sqlite3.Error as error:
            self.error = str(error)
        if not self._ready:
            if background:
                self._worker = threading.Thread(target=self._build, name='path-trigram-index', daemon=True)
                self._worker.start()
            else:
                self._build()

    @property
    def ready(self):
        return self._ready and not self._closed.is_set()

    @property
    def engine(self):
        return '本机路径倒排索引' if self.ready else '本机目录数据库'

    def _connect(self, timeout):
        connection = sqlite3.connect(self.db_path, timeout=timeout)
        connection.set_progress_handler(lambda: int(self._closed.is_set()), 2000)
        return connection

    def _prepare(self, timeout):
        connection = self._connect(timeout)
        try:
            with connection:
                connection.execute('CREATE TABLE IF NOT EXISTS reference_path_index_state(version INTEGER PRIMARY KEY, ready INTEGER NOT NULL)')
                connection.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS reference_path_fts
                    USING fts5(relative, content='items', content_rowid='rowid', tokenize='trigram')''')
                # IF NOT EXISTS 不会重建已完成索引，也不触碰已有 items 行。
                connection.execute('INSERT OR IGNORE INTO reference_path_index_state(version,ready) VALUES(1,0)')
                for sql in (
                    '''CREATE TRIGGER IF NOT EXISTS reference_path_insert AFTER INSERT ON items
                       WHEN (SELECT ready FROM reference_path_index_state WHERE version=1)=1 BEGIN
                       INSERT INTO reference_path_fts(rowid,relative) VALUES(new.rowid,new.relative); END''',
                    '''CREATE TRIGGER IF NOT EXISTS reference_path_delete AFTER DELETE ON items
                       WHEN (SELECT ready FROM reference_path_index_state WHERE version=1)=1 BEGIN
                       INSERT INTO reference_path_fts(reference_path_fts,rowid,relative) VALUES('delete',old.rowid,old.relative); END''',
                    '''CREATE TRIGGER IF NOT EXISTS reference_path_update AFTER UPDATE OF relative ON items
                       WHEN old.relative IS NOT new.relative AND (SELECT ready FROM reference_path_index_state WHERE version=1)=1 BEGIN
                       INSERT INTO reference_path_fts(reference_path_fts,rowid,relative) VALUES('delete',old.rowid,old.relative);
                       INSERT INTO reference_path_fts(rowid,relative) VALUES(new.rowid,new.relative); END''',
                ):
                    connection.execute(sql)
                state = connection.execute('SELECT ready FROM reference_path_index_state WHERE version=1').fetchone()[0]
                if not state and not connection.execute('SELECT 1 FROM items LIMIT 1').fetchone():
                    connection.execute('UPDATE reference_path_index_state SET ready=1 WHERE version=1')
                    state = 1
            self._ready = bool(state)
            self.error = ''
        finally:
            connection.close()

    def _build(self):
        connection = None
        try:
            if self._closed.is_set():
                return
            if not self._ready:
                while not self._closed.is_set():
                    try:
                        self._prepare(timeout=0.5)
                        break
                    except sqlite3.OperationalError as error:
                        if 'locked' not in str(error).lower() and 'busy' not in str(error).lower():
                            raise
                        self._closed.wait(0.1)
            if self._ready or self._closed.is_set():
                return
            connection = self._connect(timeout=0.5)
            with self._connection_lock:
                self._building_connection = connection
            with connection:
                # 同一写事务保证其他进程的写入在 rebuild 前被包含，或者在
                # ready 提交后由触发器维护。读取仍可通过 WAL 使用普通搜索。
                while not self._closed.is_set():
                    try:
                        connection.execute('BEGIN IMMEDIATE')
                        break
                    except sqlite3.OperationalError as error:
                        if 'locked' not in str(error).lower() and 'busy' not in str(error).lower():
                            raise
                        self._closed.wait(0.1)
                if self._closed.is_set():
                    return
                if not connection.execute('SELECT ready FROM reference_path_index_state WHERE version=1').fetchone()[0]:
                    connection.execute("INSERT INTO reference_path_fts(reference_path_fts) VALUES('rebuild')")
                    connection.execute('UPDATE reference_path_index_state SET ready=1 WHERE version=1')
            self._ready = True
            self.error = ''
        except sqlite3.Error as error:
            # 仅索引不可用，不删除/修复业务表；搜索仍能完整回退。
            self.error = str(error)
        finally:
            with self._connection_lock:
                self._building_connection = None
            if connection is not None:
                connection.close()

    def condition(self, connection, text):
        """返回用于 capture_items WHERE 的条件及绑定值。

        pair_key 候选使 .CR3 命中同组 JPG 代表。末尾 instr 复核保证引号、
        通配符及非 ASCII 大小写遵守 SQLite 原有的字面子串语义。
        同一连接内检查 ready，避免调用方较早开启的读快照看到半成品索引。
        调用方搜索主条件使用 +root=?，禁止 root/name 有序索引驱动整库扫描；
        目录浏览无搜索词时仍使用 root=?。候选通过 catalog_pair 精确定位。
        """
        text = str(text)
        # NUL 会截断 FTS MATCH 文本；短词没有 trigram，必须完整回退。
        ready = self.ready and len(text) >= 3 and '\0' not in text
        if ready:
            try:
                ready = bool(connection.execute('SELECT ready FROM reference_path_index_state WHERE version=1').fetchone()[0])
            except sqlite3.Error:
                ready = False
        if ready:
            phrase = '"' + text.replace('"', '""') + '"'
            return ('''pair_key IN (
                SELECT i.pair_key FROM reference_path_fts JOIN items i ON i.rowid=reference_path_fts.rowid
                WHERE reference_path_fts MATCH ? AND i.online=1 AND instr(lower(i.relative),lower(?))>0
            )''', [phrase, text])
        return ('''pair_key IN (
            SELECT i.pair_key FROM items i WHERE i.online=1 AND instr(lower(i.relative),lower(?))>0
        )''', [text])

    def close(self):
        self._closed.set()
        with self._connection_lock:
            if self._building_connection is not None:
                with suppress(sqlite3.Error):
                    self._building_connection.interrupt()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join()
