"""只引用原图的 LR 式资产浏览与 SKU 指纹索引。"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import base64
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import os
import re
import sqlite3
import time

from sku_match import extract_codes, normalize
from media_engine import RAW_EXTS


IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'} | RAW_EXTS


def pair_key(path):
    path = Path(path)
    return str(path.with_suffix('') if path.suffix.lower() in RAW_EXTS | {'.jpg', '.jpeg'} else path).casefold()


@lru_cache(maxsize=512)
def _raw_orientation(path, size, modified_ns, exiftool):
    """按文件版本缓存 RAW 方向，避免每个预览尺寸重复读取元数据。"""
    from media_engine import read_metadata
    try:
        value = int(read_metadata([Path(path)], exiftool)[0].get('Orientation', 1))
        return value if 1 <= value <= 8 else 1
    except (OSError, RuntimeError, ValueError, TypeError):
        return 1


def _id(value: str) -> str:
    return sha256(value.encode('utf-8')).hexdigest()[:32]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def archive_path(value):
    """规范化路径拼写，保留用户输入的映射盘符，不解析为 UNC 别名。"""
    if not isinstance(value,str) or not value.strip():raise ValueError('请填写归档文件夹路径')
    value=value.strip().strip('"')
    if value.lower().startswith(('http://','https://','smb://')):raise ValueError('网络共享请使用 \\\\IP地址\\共享名\\文件夹 格式')
    if os.name=='nt':
        value=value.replace('/','\\')
        if value.lower().startswith('\\\\?\\unc\\'):value='\\\\'+value[8:]
        elif value.startswith('\\\\?\\'):value=value[4:]
        if re.match(r'^\d{1,3}(?:\.\d{1,3}){3}\\',value):value='\\\\'+value
    return Path(os.path.abspath(os.path.expanduser(value)))


def archive_folder(relative):
    folder=PurePosixPath(str(relative).replace('\\','/')).parent.as_posix()
    return '/' if folder=='.' else folder


class AssetCatalog:
    """维护原图位置和 SKU 结果；不复制、不移动、不修改拍摄文件。"""

    def __init__(self, data: Path):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = self.data / 'asset_catalog.sqlite3'
        self.previews = self.data / 'catalog_previews'
        with self.connect() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS roots(
                  id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, created REAL NOT NULL, scanned REAL, status TEXT NOT NULL DEFAULT 'ready', scan_epoch INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS items(
                  id TEXT PRIMARY KEY, root TEXT NOT NULL REFERENCES roots(id), path TEXT UNIQUE NOT NULL,
                  relative TEXT NOT NULL, folder TEXT NOT NULL, name TEXT NOT NULL, extension TEXT NOT NULL,
                  size INTEGER NOT NULL, modified REAL NOT NULL, fingerprint TEXT NOT NULL,
                  sku TEXT NOT NULL DEFAULT '', normalized TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'unmatched',
                  confidence REAL, scan_revision INTEGER NOT NULL DEFAULT 0, last_seen REAL NOT NULL, online INTEGER NOT NULL DEFAULT 1, scan_epoch INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS catalog_items_root_folder ON items(root, online, folder);
                CREATE INDEX IF NOT EXISTS catalog_items_sku ON items(normalized, online, modified DESC, id);
                CREATE INDEX IF NOT EXISTS catalog_items_browse ON items(root, online, modified DESC, id);
                CREATE TABLE IF NOT EXISTS corrections(
                  id TEXT PRIMARY KEY, item TEXT NOT NULL REFERENCES items(id), before_sku TEXT NOT NULL,
                  after_sku TEXT NOT NULL, source TEXT NOT NULL, created REAL NOT NULL
                );
            ''')
            root_columns = {row[1] for row in c.execute('PRAGMA table_info(roots)')}
            item_columns = {row[1] for row in c.execute('PRAGMA table_info(items)')}
            if 'scan_epoch' not in root_columns:
                c.execute('ALTER TABLE roots ADD COLUMN scan_epoch INTEGER NOT NULL DEFAULT 0')
            if 'removed' not in root_columns:
                c.execute('ALTER TABLE roots ADD COLUMN removed INTEGER NOT NULL DEFAULT 0')
            c.execute('CREATE TABLE IF NOT EXISTS exclusions(root TEXT,folder TEXT,PRIMARY KEY(root,folder))')
            if 'online' not in item_columns:
                c.execute('ALTER TABLE items ADD COLUMN online INTEGER NOT NULL DEFAULT 1')
            if 'scan_epoch' not in item_columns:
                c.execute('ALTER TABLE items ADD COLUMN scan_epoch INTEGER NOT NULL DEFAULT 0')
            if c.execute('PRAGMA user_version').fetchone()[0]<1:
                # 旧目录字段错误移除了点号并保留 Windows 分隔符；从原始相对路径恢复。
                c.create_function('archive_folder',1,archive_folder)
                c.execute('UPDATE items SET folder=archive_folder(relative) WHERE folder<>archive_folder(relative)')
                c.execute('PRAGMA user_version=1')
            if 'pair_key' not in item_columns:
                c.execute("ALTER TABLE items ADD COLUMN pair_key TEXT NOT NULL DEFAULT ''")
                c.create_function('photo_pair_key', 1, pair_key)
                c.execute('UPDATE items SET pair_key=photo_pair_key(path)')
            c.executescript('''
                CREATE INDEX IF NOT EXISTS catalog_pair ON items(pair_key, online);
                CREATE VIEW IF NOT EXISTS capture_items AS
                SELECT a.*,
                  EXISTS(SELECT 1 FROM items b WHERE b.pair_key=a.pair_key AND b.online=1 AND b.extension IN ('.jpg','.jpeg')) AS has_jpg,
                  EXISTS(SELECT 1 FROM items b WHERE b.pair_key=a.pair_key AND b.online=1 AND b.extension NOT IN ('.jpg','.jpeg') AND b.id<>a.id) OR a.extension NOT IN ('.jpg','.jpeg','.png','.webp','.tif','.tiff') AS has_raw
                FROM items a WHERE a.online=1 AND NOT EXISTS(
                  SELECT 1 FROM items b WHERE b.pair_key=a.pair_key AND b.online=1 AND
                  (CASE WHEN b.source='manual' THEN 0 ELSE 2 END + CASE WHEN b.extension IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,
                   b.id) <
                  (CASE WHEN a.source='manual' THEN 0 ELSE 2 END + CASE WHEN a.extension IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,
                   a.id));
            ''')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def add_root(self, folder: str):
        path = archive_path(folder)
        if not path.is_dir():
            raise ValueError('归档文件夹不可访问，请检查路径、网络连接和共享权限')
        root_id = _id(str(path).casefold())
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO roots(id,path,created) VALUES (?,?,?)', (root_id, str(path), time.time()))
            c.execute('UPDATE roots SET removed=0 WHERE id=?',(root_id,))
            c.execute('DELETE FROM exclusions WHERE root=?',(root_id,))
        return self.scan(root_id)

    def _root(self, root_id: str):
        with self.connect() as c:
            row = c.execute('SELECT * FROM roots WHERE id=? AND removed=0', (root_id,)).fetchone()
        if not row:
            raise ValueError('归档目录不存在')
        return dict(row)

    def folder_path(self, root_id, folder=''):
        base=archive_path(self._root(root_id)['path'])
        if folder in ('','/'):
            return base
        parts=PurePosixPath(str(folder).replace('\\','/'))
        if parts.is_absolute() or any(p in ('.','..') or ':' in p for p in parts.parts):
            raise ValueError('归档子目录路径无效')
        path=base.joinpath(*parts.parts)
        try:
            path.resolve().relative_to(base.resolve())
        except (ValueError,OSError):
            raise ValueError('归档子目录超出已添加位置') from None
        return path

    def open_folder(self, root_id, folder=''):
        path=self.folder_path(root_id,folder)
        if not path.is_dir():
            raise ValueError('归档目录已离线或不可访问')
        if os.name!='nt':
            raise ValueError('打开路径当前仅支持 Windows')
        try:
            os.startfile(str(path))
        except OSError as exc:
            raise ValueError('无法打开归档目录：'+str(exc)) from None
        return {'ok':True}

    def remove(self, root_id, folder=''):
        self._root(root_id)
        if folder not in ('','/'):
            self.folder_path(root_id,folder)
            folder=str(folder).replace('\\','/').strip('/')
        with self.connect() as c:
            if not folder or folder=='/':
                c.execute('UPDATE roots SET removed=1 WHERE id=?',(root_id,))
                c.execute('UPDATE items SET online=0 WHERE root=?',(root_id,))
            else:
                c.execute('INSERT OR IGNORE INTO exclusions VALUES (?,?)',(root_id,folder))
                pattern=folder.replace('\\','\\\\').replace('%',r'\%').replace('_',r'\_')+'/%'
                c.execute("UPDATE items SET online=0 WHERE root=? AND (folder=? OR folder LIKE ? ESCAPE '\\')",(root_id,folder,pattern))
        return {'ok':True,'root_id':root_id,'folder':folder}

    @staticmethod
    def _guess_sku(relative: str):
        path = Path(relative)
        # 文件名优先，随后逐级检查最近目录；不把扩展名或跨层路径拼接成虚假的 SKU。
        parts = [path.stem] + list(reversed(path.parent.parts))
        for part in parts:
            if re.fullmatch(r'(?i)(?:look|detail|image|img|photo|dsc|xsy)[-_ ]*\d+', part.strip()):
                continue
            normalized = []
            for value in extract_codes(part):
                if re.fullmatch(r'\d{4}[-/ ]\d{2}[-/ ]\d{2}', value.strip()):
                    continue
                key = normalize(value)
                if key and key not in {x[1] for x in normalized}:
                    normalized.append((value, key))
            if len(normalized) == 1:
                value, key = normalized[0]
                # 文件名/目录规则提供的是候选证据，仍保留识别来源和置信度供人工核对。
                return value, key, 'path_rule', .78
            if len(normalized) > 1:
                return '', '', 'review', None
        return '', '', 'unmatched', None

    def scan(self, root_id: str):
        root = self._root(root_id)
        base = Path(root['path'])
        if not base.is_dir():
            with self.connect() as c:
                c.execute("UPDATE roots SET status='offline' WHERE id=?", (root_id,))
            raise ValueError('归档目录当前不可访问')
        indexed, detected, review = 0, 0, 0
        now = time.time()
        with self.connect() as c:
            c.execute("UPDATE roots SET status='scanning',scan_epoch=scan_epoch+1 WHERE id=?", (root_id,))
            epoch = c.execute('SELECT scan_epoch FROM roots WHERE id=?', (root_id,)).fetchone()[0]
        batch = []
        with self.connect() as c:
            excluded={r[0] for r in c.execute('SELECT folder FROM exclusions WHERE root=?',(root_id,))}

        def flush():
            if not batch:
                return
            with self.connect() as c:
                current=c.execute('SELECT removed FROM roots WHERE id=?',(root_id,)).fetchone()
                if not current or current['removed']:
                    batch.clear();return
                exclusions=[r[0] for r in c.execute('SELECT folder FROM exclusions WHERE root=?',(root_id,))]
                accepted=[row for row in batch if not any(row[4]==folder or row[4].startswith(folder+'/') for folder in exclusions)]
                c.executemany('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,sku,normalized,source,confidence,scan_revision,last_seen,online,scan_epoch,pair_key)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,1,?,?)
                    ON CONFLICT(path) DO UPDATE SET root=excluded.root,relative=excluded.relative,folder=excluded.folder,name=excluded.name,extension=excluded.extension,
                    size=excluded.size,modified=excluded.modified,fingerprint=excluded.fingerprint,pair_key=excluded.pair_key,
                    sku=CASE WHEN items.source='manual' THEN items.sku ELSE excluded.sku END,
                    normalized=CASE WHEN items.source='manual' THEN items.normalized ELSE excluded.normalized END,
                    source=CASE WHEN items.source='manual' THEN items.source ELSE excluded.source END,
                    confidence=CASE WHEN items.source='manual' THEN items.confidence ELSE excluded.confidence END,
                    scan_revision=items.scan_revision+1,last_seen=excluded.last_seen,online=1,scan_epoch=excluded.scan_epoch''', accepted)
            batch.clear()
        try:
            for path in self._media_paths(base,excluded):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                resolved = archive_path(str(path))
                try:
                    relative = resolved.relative_to(base).as_posix()
                except ValueError:
                    continue
                full = str(resolved)
                item_id = _id(full.casefold())
                sku, key, source, confidence = self._guess_sku(relative)
                fingerprint = sha256(f'{full.casefold()}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest()
                folder = archive_folder(relative)
                batch.append((item_id, root_id, full, relative, folder, path.name, path.suffix.lower(), stat.st_size, stat.st_mtime, fingerprint, sku, key, source, confidence, now, epoch, pair_key(full)))
                if len(batch) >= 1000:
                    flush()
                indexed += 1
                detected += int(bool(sku))
                review += int(source == 'review')
        except OSError as exc:
            flush()
            raise ValueError('扫描归档目录时失败：' + str(exc)) from exc
        flush()
        with self.connect() as c:
            c.execute('UPDATE items SET online=0 WHERE root=? AND scan_epoch<>?', (root_id, epoch))
            c.execute("UPDATE roots SET scanned=?,status='ready' WHERE id=?", (now, root_id))
        return {'root_id': root_id, 'indexed': indexed, 'detected': detected, 'review': review, 'scanned_at': now}

    @staticmethod
    def _media_paths(root: Path, excluded=()):
        """用 scandir 流式遍历，避免百万文件扫描时构建路径列表。"""
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with __import__('os').scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                relative=Path(entry.path).relative_to(root).as_posix()
                                if relative not in excluded:
                                    stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in IMAGE_EXTENSIONS:
                                yield Path(entry.path)
                        except OSError:
                            continue
            except OSError:
                continue

    def state(self):
        with self.connect() as c:
            roots = [dict(r) for r in c.execute('SELECT * FROM roots WHERE removed=0 ORDER BY path COLLATE NOCASE')]
            counts = dict(c.execute("SELECT count(*) AS total, sum(sku!='') AS tagged, sum(source='review') AS review FROM capture_items WHERE online=1").fetchone())
        return {'roots': roots, 'counts': {k: int(v or 0) for k, v in counts.items()}}

    def tree(self):
        result = []
        with self.connect() as c:
            roots = c.execute('SELECT * FROM roots WHERE removed=0 ORDER BY path COLLATE NOCASE').fetchall()
            for root in roots:
                folders = [dict(r) for r in c.execute('SELECT folder,count(*) AS count FROM capture_items WHERE root=? AND online=1 GROUP BY folder ORDER BY folder COLLATE NOCASE', (root['id'],))]
                result.append({'id': root['id'], 'path': root['path'], 'status': root['status'], 'folders': folders})
        return {'roots': result}

    def items(self, root='', folder='', sku='', offset=0, limit=120, cursor=''):
        limit = max(1, min(240, int(limit)))
        offset = max(0, int(offset))
        clauses, args = ['online=1'], []
        if root:
            clauses.append('root=?'); args.append(root)
        if folder:
            pattern=folder.rstrip('/').replace('\\','\\\\').replace('%',r'\%').replace('_',r'\_')+'/%'
            clauses.append("(folder=? OR folder LIKE ? ESCAPE '\\')"); args.extend([folder, pattern])
        if sku:
            clauses.append('normalized LIKE ?'); args.append('%' + normalize(sku) + '%')
        if cursor:
            try:
                marker = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                clauses.append('(modified<? OR (modified=? AND id>?))'); args.extend([float(marker[0]), float(marker[0]), str(marker[1])])
            except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError('归档分页标记无效') from None
        where = ' WHERE ' + ' AND '.join(clauses)
        with self.connect() as c:
            total = c.execute('SELECT count(*) FROM capture_items' + where, args).fetchone()[0]
            rows = [dict(r) for r in c.execute('SELECT * FROM capture_items' + where + ' ORDER BY modified DESC,id LIMIT ? OFFSET ?', args + [limit + 1, offset if not cursor else 0])]
        has_more = len(rows) > limit
        rows = rows[:limit]
        for row in rows:
            row['preview'] = '/api/catalog/preview/' + row['id'] + '?v=' + _id(row['fingerprint'] + ':orientation-v1')
        next_cursor = ''
        if rows:
            marker = [rows[-1]['modified'], rows[-1]['id']]
            next_cursor = base64.urlsafe_b64encode(_json(marker).encode()).decode()
        return {'rows': rows, 'total': total, 'offset': offset, 'has_more': has_more, 'next_cursor': next_cursor}

    def sku_groups(self, search=''):
        key = normalize(search)
        with self.connect() as c:
            rows = [dict(r) for r in c.execute("SELECT normalized, min(sku) AS sku, count(*) AS count, sum(source='manual') AS corrected FROM capture_items WHERE online=1 AND normalized!='' AND normalized LIKE ? GROUP BY normalized ORDER BY sku COLLATE NOCASE", ('%' + key + '%',))]
        return {'rows': rows}

    def item(self, item_id: str):
        with self.connect() as c:
            row = c.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        if not row:
            raise ValueError('归档图片不存在')
        return dict(row)

    def assign(self, item_id: str, sku: str):
        sku = str(sku).strip()
        key = normalize(sku)
        if not key or len(sku) > 80:
            raise ValueError('请填写有效 SKU（不超过 80 个字符）')
        with self.connect() as c:
            row = c.execute('SELECT sku,pair_key FROM items WHERE id=?', (item_id,)).fetchone()
            if not row:
                raise ValueError('归档图片不存在')
            members = c.execute('SELECT id,sku FROM items WHERE pair_key=?', (row['pair_key'],)).fetchall()
            c.execute('UPDATE items SET sku=?,normalized=?,source=?,confidence=? WHERE pair_key=?', (sku, key, 'manual', 1.0, row['pair_key']))
            for member in members:
                c.execute('INSERT INTO corrections(id,item,before_sku,after_sku,source,created) VALUES (?,?,?,?,?,?)', (_id(member['id'] + str(time.time())), member['id'], member['sku'], sku, 'manual', time.time()))
        return self.item(item_id)

    def preview(self, item_id: str, size=420):
        row = self.item(item_id)
        from media_engine import preview, find_exiftool
        with self.connect() as c:
            members = [dict(r) for r in c.execute('SELECT * FROM items WHERE pair_key=? AND online=1 ORDER BY extension,path', (row['pair_key'],))]
        jpg = next((r for r in members if r['extension'] in {'.jpg', '.jpeg'} and Path(r['path']).is_file()), None)
        raw = next((r for r in members if r['extension'] in RAW_EXTS and Path(r['path']).is_file()), None)
        source = Path((jpg or raw or row)['path'])
        if not source.is_file():
            raise ValueError('原图已移动或离线，请重新扫描该目录')
        size = max(160, min(1200, int(size)))
        record = {'fingerprint': _id('|'.join(r['fingerprint'] for r in members) + ':preview-v2'),
                  'raw': source.name if not jpg and raw else None,
                  'jpg': source.name if jpg or not raw else None}
        exiftool = find_exiftool(Path(__file__).resolve().parents[1]) if record['raw'] else None
        if record['raw']:
            stat = source.stat()
            record['orientation'] = _raw_orientation(str(source), stat.st_size, stat.st_mtime_ns, str(exiftool))
        return preview(record, source.parent, self.previews, exiftool, size=size, decode=False)
