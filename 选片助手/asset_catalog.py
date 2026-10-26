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
import shutil
import sqlite3
import subprocess
import time

from sku_match import extract_codes, normalize
from media_engine import RAW_EXTS
from fast_path_index import FastPathIndex


IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'} | RAW_EXTS
VIDEO_EXTENSIONS = {'.mp4', '.m4v', '.mov', '.mxf', '.mkv', '.avi', '.webm', '.mts', '.m2ts', '.r3d', '.braw'}
ADOBE_EXTENSIONS = {'.psd', '.psb', '.ai', '.eps', '.indd', '.pdf'}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | ADOBE_EXTENSIONS


def media_type(extension):
    extension = str(extension).lower()
    if extension in VIDEO_EXTENSIONS:
        return 'video'
    if extension in ADOBE_EXTENSIONS:
        return 'design'
    return 'image'


def media_format(extension):
    return str(extension).removeprefix('.').upper() or 'FILE'


def pair_key(path):
    path = Path(path)
    return str(path.with_suffix('') if path.suffix.lower() in RAW_EXTS | {'.jpg', '.jpeg'} else path).casefold()


@lru_cache(maxsize=512)
def _raw_orientation(path, size, modified_ns, exiftool):
    """按文件版本缓存 RAW 方向，避免每个预览尺寸重复读取元数据。"""
    from media_engine import read_metadata,preview_exif_session
    try:
        with preview_exif_session(exiftool) as session:
            value = int(read_metadata([Path(path)], exiftool,session=session)[0].get('Orientation', 1))
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

    def __init__(self, data: Path, *, fast_index=None):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = self.data / 'asset_catalog.sqlite3'
        self.previews = self.data / 'catalog_previews'
        from video_preview import VideoPreview
        self.video = VideoPreview(self.data)
        self.on_previews = None
        self.discovery = None
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
                  confidence REAL, scan_revision INTEGER NOT NULL DEFAULT 0, last_seen REAL NOT NULL, online INTEGER NOT NULL DEFAULT 1, scan_epoch INTEGER NOT NULL DEFAULT 0,
                  media_type TEXT NOT NULL DEFAULT 'image', media_format TEXT NOT NULL DEFAULT '', codec TEXT NOT NULL DEFAULT '', duration_ms INTEGER, width INTEGER, height INTEGER, inspected_fingerprint TEXT NOT NULL DEFAULT ''
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
            for name, definition in (
                ('media_type', "TEXT NOT NULL DEFAULT 'image'"), ('media_format', "TEXT NOT NULL DEFAULT ''"),
                ('codec', "TEXT NOT NULL DEFAULT ''"), ('duration_ms', 'INTEGER'), ('width', 'INTEGER'),
                ('height', 'INTEGER'), ('inspected_fingerprint', "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in item_columns:
                    c.execute(f'ALTER TABLE items ADD COLUMN {name} {definition}')
            for extension in VIDEO_EXTENSIONS:
                c.execute("UPDATE items SET media_type='video',media_format=? WHERE extension=?", (media_format(extension), extension))
            for extension in ADOBE_EXTENSIONS:
                c.execute("UPDATE items SET media_type='design',media_format=? WHERE extension=?", (media_format(extension), extension))
            c.execute("UPDATE items SET media_format=upper(ltrim(extension,'.')) WHERE media_format='' OR media_format IS NULL")
            c.executescript('''
                BEGIN IMMEDIATE;
                CREATE INDEX IF NOT EXISTS catalog_pair ON items(pair_key, online);
                CREATE INDEX IF NOT EXISTS catalog_pair_root ON items(pair_key, online, root);
                CREATE INDEX IF NOT EXISTS catalog_media_page ON items(media_type, online, modified DESC, id);
                CREATE INDEX IF NOT EXISTS catalog_root_media_page ON items(root, media_type, online, modified DESC, id);
                DROP VIEW IF EXISTS capture_items;
                CREATE VIEW capture_items AS
                SELECT a.*,
                  CASE WHEN a.media_type='image' THEN EXISTS(SELECT 1 FROM items b WHERE b.pair_key=a.pair_key AND b.online=1 AND b.extension IN ('.jpg','.jpeg')) ELSE 0 END AS has_jpg,
                  CASE WHEN a.media_type='image' THEN EXISTS(SELECT 1 FROM items b WHERE b.pair_key=a.pair_key AND b.online=1 AND b.extension NOT IN ('.jpg','.jpeg') AND b.id<>a.id) OR a.extension NOT IN ('.jpg','.jpeg','.png','.webp','.tif','.tiff') ELSE 0 END AS has_raw
                FROM items a WHERE a.online=1 AND (a.media_type<>'image' OR NOT EXISTS(
                  SELECT 1 FROM items b WHERE b.pair_key=a.pair_key AND b.online=1 AND
                  (CASE WHEN b.source='manual' THEN 0 ELSE 2 END + CASE WHEN b.extension IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,
                   b.id) <
                  (CASE WHEN a.source='manual' THEN 0 ELSE 2 END + CASE WHEN a.extension IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,
                   a.id)));
                COMMIT;
            ''')
        self._owns_fast_index = fast_index is None
        self.fast_index = fast_index or FastPathIndex(self.db)
        from catalog_metadata import CatalogMetadata
        self.metadata = CatalogMetadata(self)
        from video_playback import VideoPlayback
        self.playback = VideoPlayback(self)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def close(self):
        self.playback.close()
        self.metadata.close()
        if self._owns_fast_index:
            self.fast_index.close()

    def add_root(self, folder: str):
        path = archive_path(folder)
        from preview_cache import _LOCATIONS
        active=_LOCATIONS.get(self.data.absolute(),self.data.absolute())
        if path==active or path.is_relative_to(active) or path==self.data.absolute() or path.is_relative_to(self.data.absolute()):raise ValueError('归档目录不能位于图片缓存或软件资料目录内')
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
                c.executemany('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,sku,normalized,source,confidence,scan_revision,last_seen,online,scan_epoch,pair_key,media_type,media_format)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,1,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET root=excluded.root,relative=excluded.relative,folder=excluded.folder,name=excluded.name,extension=excluded.extension,
                    size=excluded.size,modified=excluded.modified,fingerprint=excluded.fingerprint,pair_key=excluded.pair_key,
                    media_type=excluded.media_type,media_format=excluded.media_format,
                    sku=CASE WHEN items.source='manual' THEN items.sku ELSE excluded.sku END,
                    normalized=CASE WHEN items.source='manual' THEN items.normalized ELSE excluded.normalized END,
                    source=CASE WHEN items.source='manual' THEN items.source ELSE excluded.source END,
                    confidence=CASE WHEN items.source='manual' THEN items.confidence ELSE excluded.confidence END,
                    scan_revision=items.scan_revision+1,last_seen=excluded.last_seen,online=1,scan_epoch=excluded.scan_epoch''', accepted)
            if self.on_previews:self.queue_previews({row[-3] for row in accepted})
            batch.clear()
        candidates=set()
        def paths():
            if self.discovery:
                from preview_cache import _LOCATIONS
                for path in self.discovery.candidates(base,exclude=[self.data,*_LOCATIONS.values(),*(base/f for f in excluded)]):
                    if path.suffix.lower() not in MEDIA_EXTENSIONS:continue
                    candidates.add(path);yield path
                # 即使索引只有几张，首批也立即可浏览及预热。
                flush()
            for path in self._media_paths(base,excluded):
                if path not in candidates:yield path
        try:
            for path in paths():
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
                extension=path.suffix.lower()
                batch.append((item_id, root_id, full, relative, folder, path.name, extension, stat.st_size, stat.st_mtime, fingerprint, sku, key, source, confidence, now, epoch, pair_key(full), media_type(extension), media_format(extension)))
                if len(batch) >= 128:
                    flush()
                indexed += 1
                detected += int(bool(sku))
                review += int(source == 'review')
                if indexed % 128 == 0 and getattr(self, 'on_scan_progress', None):
                    self.on_scan_progress(indexed)
        except OSError as exc:
            flush()
            with self.connect() as c:c.execute("UPDATE roots SET status='error' WHERE id=?",(root_id,))
            raise ValueError('扫描归档目录时失败：' + str(exc)) from exc
        flush()
        if getattr(self, 'on_scan_progress', None):
            self.on_scan_progress(indexed)
        with self.connect() as c:
            c.execute('UPDATE items SET online=0 WHERE root=? AND scan_epoch<>?', (root_id, epoch))
            c.execute("UPDATE roots SET scanned=?,status='ready' WHERE id=?", (now, root_id))
        return {'root_id': root_id, 'indexed': indexed, 'detected': detected, 'review': review, 'scanned_at': now,'indexed_candidates':len(candidates),'discovery':'everything+directory' if candidates else 'directory'}

    @staticmethod
    def _media_paths(root: Path, excluded=()):
        """用 scandir 流式遍历，避免百万文件扫描时构建路径列表。"""
        stack = [root]
        from preview_cache import _LOCATIONS
        internal=set(_LOCATIONS.values())
        while stack:
            current = stack.pop()
            try:
                with __import__('os').scandir(current) as entries:
                    for entry in entries:
                        if entry.name.startswith('.framekeep') or entry.name in ('#recycle','$RECYCLE.BIN','System Volume Information'):continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if Path(entry.path).is_junction():continue
                                if Path(entry.path) in internal:continue
                                relative=Path(entry.path).relative_to(root).as_posix()
                                if relative not in excluded:
                                    stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in MEDIA_EXTENSIONS:
                                yield Path(entry.path)
                        except OSError:
                            continue
            except OSError:
                # 不完整扫描不能把无法访问子目录的历史照片批量标成离线。
                raise

    def state(self):
        with self.connect() as c:
            roots = [dict(r) for r in c.execute('SELECT * FROM roots WHERE removed=0 ORDER BY path COLLATE NOCASE')]
            counts = dict(c.execute("SELECT count(*) AS total, sum(sku!='') AS tagged, sum(source='review') AS review, sum(media_type='image') AS images, sum(media_type='video') AS videos, sum(media_type='design') AS designs FROM capture_items WHERE online=1").fetchone())
        return {'roots': roots, 'counts': {k: int(v or 0) for k, v in counts.items()}, 'metadata':self.metadata.state()}

    def search_condition(self, connection, text):
        path, path_args = self.fast_index.condition(connection, text)
        metadata, metadata_args = self.metadata.condition(text)
        key = normalize(text)
        # 中文和标点不可变成 LIKE '%%'，否则任意中文搜索都会命中整库。
        sku = 'normalized LIKE ? OR ' if key else ''
        return '('+sku+path+' OR '+metadata+')', (['%'+key+'%'] if key else [])+path_args+metadata_args

    def tree(self,text='',media=''):
        result = []
        with self.connect() as c:
            clauses=['online=1'];args=[]
            if media:
                if media not in ('image','video','design'):raise ValueError('素材类型筛选无效')
                clauses.append('media_type=?');args.append(media)
            if text:
                condition,values=self.search_condition(c,text)
                clauses.append(condition);args.extend(values)
            matches={}
            for row in c.execute('SELECT root,folder,count(*) AS count FROM capture_items WHERE '+' AND '.join(clauses)+' GROUP BY root,folder ORDER BY folder COLLATE NOCASE',args):
                matches.setdefault(row['root'],[]).append({'folder':row['folder'],'count':row['count']})
            roots = c.execute('SELECT * FROM roots WHERE removed=0 ORDER BY path COLLATE NOCASE').fetchall()
            for root in roots:
                folders=matches.get(root['id'],[])
                if (text or media) and not folders:continue
                result.append({'id': root['id'], 'path': root['path'], 'status': root['status'], 'folders': folders})
        return {'roots': result}

    def items(self, root='', folder='', sku='', offset=0, limit=120, cursor='', text='', media=''):
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
        if media:
            if media not in ('image', 'video', 'design'):
                raise ValueError('素材类型筛选无效')
            clauses.append('media_type=?'); args.append(media)
        if cursor:
            try:
                marker = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                clauses.append('(modified<? OR (modified=? AND id>?))'); args.extend([float(marker[0]), float(marker[0]), str(marker[1])])
            except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError('归档分页标记无效') from None
        with self.connect() as c:
            if text:
                condition, values = self.search_condition(c, text)
                clauses.append(condition)
                args.extend(values)
            where = ' WHERE ' + ' AND '.join(clauses)
            total = c.execute('SELECT count(*) FROM capture_items' + where, args).fetchone()[0]
            rows = [dict(r) for r in c.execute('SELECT * FROM capture_items' + where + ' ORDER BY modified DESC,id LIMIT ? OFFSET ?', args + [limit + 1, offset if not cursor else 0])]
        has_more = len(rows) > limit
        rows = rows[:limit]
        poster_frame = self.video.frame
        for row in rows:
            row['preview'] = '/api/catalog/preview/' + row['id'] + '?v=' + _id(row['fingerprint'] + ':orientation-v1') + ('&frame=' + str(poster_frame) if row['media_type']=='video' else '')
        next_cursor = ''
        if rows:
            marker = [rows[-1]['modified'], rows[-1]['id']]
            next_cursor = base64.urlsafe_b64encode(_json(marker).encode()).decode()
        return {'rows': rows, 'total': total, 'offset': offset, 'has_more': has_more, 'next_cursor': next_cursor,
                'engine': self.fast_index.engine, 'media': media, 'search': text}

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

    def details(self,item_id,app_root=None):
        """仅查询已入库资料；原图访问和外部进程由后台队列完成。"""
        row=self.item(item_id);self._root(row['root'])
        with self.connect() as c:
            files=[dict(r) for r in c.execute('SELECT name,path,extension,size,modified,online FROM items WHERE pair_key=? ORDER BY extension,path',(row['pair_key'],))]
        cached=self.metadata.cached(item_id)
        self.metadata.request(item_id)
        data=cached['data']
        return {'item':row,'files':files,'capture':data.get('capture',{}),'media':data.get('media',{}),
                'description':data.get('description',{}),'tags':data.get('tags',{}),
                'metadata_status':cached['status'],'metadata_updated':cached.get('updated',0),
                'metadata_error':cached['error'] or ('' if row['online'] else '原图已离线，显示现有索引资料')}

    def _video_details(self, row, source):
        if row.get('inspected_fingerprint') == row.get('fingerprint') and row.get('codec'):
            return {key: row.get(key) for key in ('codec', 'duration_ms', 'width', 'height', 'media_format')}
        details={'codec':row.get('codec') or '', 'duration_ms':None, 'width':None, 'height':None, 'media_format':row.get('media_format') or media_format(source.suffix)}
        from video_preview import find_tool
        ffprobe=find_tool('ffprobe')
        if ffprobe:
            try:
                result=subprocess.run([ffprobe,'-v','error','-probesize','1048576','-analyzeduration','1000000','-fpsprobesize','0','-show_entries','format=duration,format_name:stream=codec_name,codec_long_name,width,height','-of','json',str(source)],
                                      capture_output=True,text=True,encoding='utf8',errors='replace',timeout=12,check=True,
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                payload=json.loads(result.stdout);streams=payload.get('streams',[])
                stream=next((value for value in streams if value.get('width') or value.get('height')),next((value for value in streams if value.get('codec_name')), {}))
                details['codec']=stream.get('codec_long_name') or stream.get('codec_name') or ''
                details['width']=stream.get('width');details['height']=stream.get('height')
                duration=payload.get('format',{}).get('duration')
                if duration is not None:details['duration_ms']=round(float(duration)*1000)
                if payload.get('format',{}).get('format_name'):details['media_format']=payload['format']['format_name'].upper()
            except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
                pass
        with self.connect() as c:
            c.execute('''UPDATE items SET codec=?,duration_ms=?,width=?,height=?,media_format=?,inspected_fingerprint=?
                         WHERE id=? AND fingerprint=?''', (details['codec'],details['duration_ms'],details['width'],details['height'],details['media_format'],row['fingerprint'] if details['codec'] else '',row['id'],row['fingerprint']))
        return details

    def preview_record(self,row,members,frame=None):
        """只用持久索引确定缓存身份，命中时不访问原图或离线网络路径。"""
        if row['media_type']=='video':
            from video_preview import validate_frame
            frame=self.video.frame if frame is None else validate_frame(frame)
            source=Path(row['path'])
            return str(source.parent),{'id':'catalog:'+row['id'],'_catalog':True,'_catalog_root':row['root'],'_catalog_pair':row['pair_key'],
                '_video':str(source),'_video_frame':frame,
                'fingerprint':_id(row['fingerprint']+f':video-poster-v1:frame={frame}')}
        jpg=next((r for r in members if r['extension'] in {'.jpg','.jpeg'}),None)
        raw=next((r for r in members if r['extension'] in RAW_EXTS),None)
        source=Path((jpg or raw or row)['path'])
        record={'id':'catalog:'+row['id'],'_catalog':True,'_catalog_root':row['root'],'_catalog_pair':row['pair_key'],
                '_portable_paths':[r['path'] for r in members],
                '_portable_versions':[r['fingerprint'] for r in members],
                'fingerprint':_id('|'.join(r['fingerprint'] for r in members)+':preview-v2'),
                'raw':source.name if not jpg and raw else None,
                'jpg':source.name if jpg or not raw else None}
        if jpg and raw:record['_fallback_raw']=raw['path']
        if record['raw'] or record.get('_fallback_raw'):
            from preview_cache import cache_for
            index=cache_for(self.previews)
            with index.lock,index.connect() as c:
                cached=c.execute('SELECT orientation FROM previews WHERE fingerprint=? LIMIT 1',(record['fingerprint'],)).fetchone()
            if cached and cached[0] is not None:record['orientation']=cached[0]
        return str(source.parent),record

    def prepare_preview_record(self,record,exiftool):
        if (record.get('raw') or record.get('_fallback_raw')) and 'orientation' not in record:
            from preview_cache import cache_for
            index=cache_for(self.previews)
            with index.lock,index.connect() as c:
                cached=c.execute('SELECT orientation FROM previews WHERE fingerprint=? LIMIT 1',(record['fingerprint'],)).fetchone()
            if cached and cached[0] is not None:
                record['orientation']=cached[0];return
            try:
                from media_engine import _jpeg_source
                _jpeg_source(record,record['_source_root'],exiftool)
                if 'orientation' not in record:record['orientation']=1
            except (OSError,RuntimeError,ValueError,IndexError,TypeError):
                record['orientation']=1

    def queue_previews(self,keys):
        if not self.on_previews or not keys:return
        groups={}
        with self.connect() as c:
            for r in c.execute('SELECT * FROM items WHERE online=1 AND scan_epoch=(SELECT scan_epoch FROM roots WHERE id=items.root) AND pair_key IN ('+','.join('?' for _ in keys)+') ORDER BY extension,path',list(keys)):
                groups.setdefault(r['pair_key'],[]).append(dict(r))
        by_root={}
        for members in groups.values():
            if members[0]['media_type']=='design':continue
            root,record=self.preview_record(members[0],members);record['_source_root']=root
            by_root.setdefault(root,[]).append(record)
        for root,records in by_root.items():self.on_previews(root,records)

    def queue_all_previews(self,stop=lambda:False,*,videos_only=False):
        """仅遍历本地索引，启动补齐历史归档，绝不等用户进入子目录。"""
        with self.connect() as c:
            cursor=c.execute('''SELECT DISTINCT pair_key FROM items WHERE online=1 AND media_type!='design'
                AND root IN (SELECT id FROM roots WHERE removed=0)'''+(" AND media_type='video'" if videos_only else '')+' ORDER BY pair_key')
            while not stop():
                rows=cursor.fetchmany(128)
                if not rows:break
                self.queue_previews({row[0] for row in rows})

    def preview_source_current(self,record):
        with self.connect() as c:
            row=c.execute('''SELECT items.relative,items.folder FROM items JOIN roots ON roots.id=items.root WHERE roots.removed=0
                AND items.online=1 AND items.scan_epoch=roots.scan_epoch AND items.root=? AND items.pair_key=? LIMIT 1''',
                (record['_catalog_root'],record['_catalog_pair'])).fetchone()
            if row is None:return False
            if any(part.casefold() in ('#recycle','$recycle.bin','system volume information') for part in row['relative'].replace('\\','/').split('/')):return False
            excluded=[r[0] for r in c.execute('SELECT folder FROM exclusions WHERE root=?',(record['_catalog_root'],))]
            return not any(row['folder']==folder or row['folder'].startswith(folder+'/') for folder in excluded)

    def preview(self, item_id: str, size=420, *, read=False, frame=None):
        row=self.item(item_id)
        if row['media_type'] == 'design':
            return self._media_placeholder(row, size)
        import media_engine as media
        with self.connect() as c:
            members=[dict(r) for r in c.execute('SELECT * FROM items WHERE pair_key=? AND online=1 ORDER BY extension,path',(row['pair_key'],))]
        root,record=self.preview_record(row,members,frame=frame);record['_source_root']=root
        tool=lambda:media.find_exiftool(Path(__file__).resolve().parents[1])
        return self.render_preview(record,root,size,tool,read=read)

    def render_preview(self,record,root,size,exiftool,*,read=False):
        import media_engine as media
        self.prepare_preview_record(record,exiftool)
        if record.get('_source_jpeg') is not None:
            # 前台首次 RAW 请求也保存本次已提取的清晰图，切换尺寸不重开 NAS 容器。
            media.warm_previews(dict(record,_background_preview=True),root,self.previews,exiftool)
        render=media.preview_bytes if read else media.preview
        try:return render(record,root,self.previews,exiftool,size=size,decode=False)
        except RuntimeError:
            # 缓存未命中且配对 JPG 不可读时，沿用 RAW 内嵌图回退；命中无需探测原图。
            fallback=record.get('_fallback_raw')
            if not fallback:raise
            source=Path(fallback);stat=source.stat();tool=exiftool() if callable(exiftool) else exiftool
            record=dict(record,raw=source.name,jpg=None,orientation=_raw_orientation(str(source),stat.st_size,stat.st_mtime_ns,str(tool)))
            return render(record,source.parent,self.previews,tool,size=size,decode=False)

    def render_preview_batch(self,record,root,exiftool):
        import media_engine as media
        self.prepare_preview_record(record,exiftool)
        return media.warm_previews(record,root,self.previews,exiftool)

    @staticmethod
    def _media_placeholder(row, size):
        from PIL import Image, ImageDraw
        side=max(80,min(1200,int(size)))
        image=Image.new('RGB',(side,side),(32,39,48));draw=ImageDraw.Draw(image)
        label='VIDEO' if row['media_type']=='video' else 'ADOBE'
        detail=row.get('media_format') or media_format(row.get('extension',''))
        draw.rounded_rectangle((side*.18,side*.20,side*.82,side*.80),radius=max(4,side//24),outline=(121,181,255),width=max(2,side//80))
        if row['media_type']=='video':
            draw.polygon([(side*.44,side*.40),(side*.44,side*.61),(side*.62,side*.505)],fill=(121,181,255))
        else:
            draw.rectangle((side*.37,side*.34,side*.63,side*.62),outline=(121,181,255),width=max(2,side//90))
        draw.text((side*.5,side*.86),label+' · '+detail,anchor='mm',fill=(224,232,241))
        import io
        output=io.BytesIO();image.save(output,format='JPEG',quality=85);return output.getvalue()
