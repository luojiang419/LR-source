"""只引用原图的 LR 式资产浏览与 SKU 指纹索引。"""
from __future__ import annotations

from contextlib import contextmanager,nullcontext,closing
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
import threading
import time

from sku_match import extract_codes, normalize
from media_engine import RAW_EXTS
from fast_path_index import FastPathIndex
from catalog_search_requests import SearchCancelled
from wal_maintenance import WalMaintenance, configure as configure_wal


IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'} | RAW_EXTS
VIDEO_EXTENSIONS = {'.mp4', '.m4v', '.mov', '.mxf', '.mkv', '.avi', '.webm', '.mts', '.m2ts', '.r3d', '.braw'}
ADOBE_EXTENSIONS = {'.psd', '.psb', '.ai', '.eps', '.indd', '.pdf'}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | ADOBE_EXTENSIONS
# 资产归档浏览仅保留图像／视频与图像源文件；PDF、排版文档继续留在原索引和磁盘。
ARCHIVE_MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | {'.psd','.psb','.ai','.eps'}
ARCHIVE_MEDIA_SQL = 'extension IN ('+','.join(repr(ext) for ext in sorted(ARCHIVE_MEDIA_EXTENSIONS))+')'


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


def _ranked_catalog(columns):
    # 全库统计顺序读覆盖索引后选代表图，避免逐图随机探测配对索引。
    # 非图片全部保留，代表图优先级与 capture_items 相同。
    return '''SELECT '''+columns+''',row_number() OVER (PARTITION BY pair_key ORDER BY
        CASE WHEN source='manual' THEN 0 ELSE 2 END+
        CASE WHEN extension IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,id) AS representative
        FROM items INDEXED BY catalog_browse_cover WHERE online=1'''


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

    def __init__(self, data: Path, *, fast_index=None, keep_wal_open=False):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = self.data / 'asset_catalog.sqlite3'
        self.wal_maintenance = WalMaintenance(self.db)
        # 生命周期连接不持有事务，仅保持 WAL 共享内存，避免每次短查询
        # 重新成为首个连接并重读全部 WAL。后台维护负责安全回收。
        self._anchor = None
        if keep_wal_open:
            self._anchor = sqlite3.connect(self.db, check_same_thread=False)
            configure_wal(self._anchor)
            self._anchor.execute('PRAGMA journal_mode').fetchone()
        self.previews = self.data / 'catalog_previews'
        from archive_thumbnails import ArchiveThumbnails
        self.thumbnail_settings=ArchiveThumbnails(self.data)
        from video_preview import VideoPreview
        self.video = VideoPreview(self.data)
        self.on_previews = None
        from catalog_status import CatalogStatus
        self.progress = CatalogStatus(self)
        self.discovery = None
        self._scan_guard=threading.Lock();self._active_scans={}
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
            if c.execute('PRAGMA user_version').fetchone()[0]<2:
                # 分类是旧库迁移，不能每次启动按每个扩展名全表扫描、重写索引。
                # 一次扫描只更新确有差异的行；新扫描已有完整的媒体分类。
                videos=','.join("'"+ext+"'" for ext in sorted(VIDEO_EXTENSIONS))
                designs=','.join("'"+ext+"'" for ext in sorted(ADOBE_EXTENSIONS))
                kind=f"CASE WHEN extension IN ({videos}) THEN 'video' WHEN extension IN ({designs}) THEN 'design' ELSE media_type END"
                format=f"CASE WHEN extension IN ({videos},{designs}) OR media_format='' OR media_format IS NULL THEN upper(ltrim(extension,'.')) ELSE media_format END"
                c.execute(f'UPDATE items SET media_type={kind},media_format={format} WHERE media_type IS NOT ({kind}) OR media_format IS NOT ({format})')
                c.execute('PRAGMA user_version=2')
            c.executescript('''
                CREATE INDEX IF NOT EXISTS catalog_original_order ON items(online,name DESC,id);
                CREATE INDEX IF NOT EXISTS catalog_capture_paths ON items(path,online);
                CREATE INDEX IF NOT EXISTS catalog_pair ON items(pair_key, online);
                CREATE INDEX IF NOT EXISTS catalog_pair_root ON items(pair_key, online, root);
                CREATE INDEX IF NOT EXISTS catalog_media_page ON items(media_type, online, modified DESC, id);
                CREATE INDEX IF NOT EXISTS catalog_root_media_page ON items(root, media_type, online, modified DESC, id);
                CREATE INDEX IF NOT EXISTS catalog_folder_candidates ON items(root, online, folder COLLATE NOCASE, id);
                CREATE INDEX IF NOT EXISTS catalog_pair_cover ON items(pair_key, online, source, extension, id);
                CREATE INDEX IF NOT EXISTS catalog_browse_cover ON items(online, modified DESC, id, media_type, pair_key, source, extension, root, folder, sku);
            ''')
            view_sql = '''
                CREATE VIEW capture_items AS
                SELECT a.*,
                  CASE WHEN a.media_type='image' THEN EXISTS(SELECT 1 FROM items b INDEXED BY catalog_pair_cover WHERE b.pair_key=a.pair_key AND b.online=1 AND b.extension IN ('.jpg','.jpeg')) ELSE 0 END AS has_jpg,
                  CASE WHEN a.media_type='image' THEN EXISTS(SELECT 1 FROM items b INDEXED BY catalog_pair_cover WHERE b.pair_key=a.pair_key AND b.online=1 AND b.extension NOT IN ('.jpg','.jpeg') AND b.id<>a.id) OR a.extension NOT IN ('.jpg','.jpeg','.png','.webp','.tif','.tiff') ELSE 0 END AS has_raw
                FROM items a WHERE a.online=1 AND (a.media_type<>'image' OR NOT EXISTS(
                  SELECT 1 FROM items b INDEXED BY catalog_pair_cover WHERE b.pair_key=a.pair_key AND b.online=1 AND
                  (CASE WHEN b.source='manual' THEN 0 ELSE 2 END + CASE WHEN b.extension IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,
                   b.id) <
                  (CASE WHEN a.source='manual' THEN 0 ELSE 2 END + CASE WHEN a.extension IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,
                   a.id)))'''
            # 成熟资料库只读启动。无条件替换视图会取得写锁，并让最后一个
            # 连接关闭时承担积累 WAL 的 checkpoint；大型库可因此等待数十秒。
            # 对比定义而非仅判断存在，仍能迁移旧版配对规则。
            current = c.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='capture_items'").fetchone()
            if not current or ' '.join(current[0].split()) != ' '.join(view_sql.split()):
                c.execute('BEGIN IMMEDIATE')
                c.execute('DROP VIEW IF EXISTS capture_items')
                c.execute(view_sql)
            from catalog_pages import CatalogPages
            self.pages=CatalogPages(self,c)
            from catalog_decode_changes import initialize as initialize_decode_changes
            initialize_decode_changes(c)
            from catalog_summaries import CatalogSummaries
            self.summaries=CatalogSummaries(self)
            from catalog_startup import CatalogStartup
            self.startup=CatalogStartup(self)
            if self._anchor is not None:self._anchor.execute('PRAGMA journal_mode').fetchone()
        self._owns_fast_index = fast_index is None
        self.fast_index = fast_index or FastPathIndex(self.db)
        from catalog_metadata import CatalogMetadata
        self.metadata = CatalogMetadata(self)
        from literal_search import LiteralSearchIndex
        self.literal_index = LiteralSearchIndex(self)
        from video_playback import VideoPlayback
        self.playback = VideoPlayback(self)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            # 提交和关闭均不合并历史 WAL；HTTP 就绪后由后台维护线程回收。
            configure_wal(c)
            # 映射只读页交给系统页缓存；大库位于慢盘时避免每次关联查找
            # 都进行 4 KiB 随机读取。上限是虚拟地址映射，不预分配物理内存。
            c.execute('PRAGMA mmap_size=2147418112')
            with c:
                yield c
        finally:
            c.close()

    def close(self):
        if getattr(self,'visits',None):self.visits.close()
        self.startup.close()
        self.literal_index.close()
        self.progress.close()
        self.playback.close()
        self.metadata.close()
        if self._owns_fast_index:
            self.fast_index.close()
        self.wal_maintenance.close()
        if self._anchor is not None:self._anchor.close()

    @contextmanager
    def read_connection(self):
        with closing(sqlite3.connect(self.db.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as c:
            c.row_factory=sqlite3.Row;c.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,True)
            c.execute('PRAGMA mmap_size=2147418112')
            yield c

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
        quick=getattr(getattr(self,'intelligence',None),'quick_index',None)
        if quick:quick.sync_roots()
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
        with self._scan_guard:self._active_scans[root_id]=self._active_scans.get(root_id,0)+1
        try:return self._scan(root_id)
        finally:
            with self._scan_guard:
                remaining=self._active_scans[root_id]-1
                if remaining:self._active_scans[root_id]=remaining
                else:self._active_scans.pop(root_id)

    def scanning_roots(self):
        with self._scan_guard:return frozenset(self._active_scans)

    def _scan(self, root_id: str, *, visit_folder=None, cancelled=lambda:False,visit_directories=None,on_flush=None):
        root = self._root(root_id)
        base = Path(root['path'])
        scan_base=self.folder_path(root_id,visit_folder) if visit_folder is not None else base
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
                previous={row['path']:row['fingerprint'] for row in c.execute(
                    'SELECT path,fingerprint FROM items WHERE path IN ('+','.join('?' for _ in accepted)+')',
                    [row[2] for row in accepted])} if accepted else {}
                c.executemany('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,sku,normalized,source,confidence,scan_revision,last_seen,online,scan_epoch,pair_key,media_type,media_format)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,1,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET root=excluded.root,relative=excluded.relative,folder=excluded.folder,name=excluded.name,extension=excluded.extension,
                    size=excluded.size,modified=excluded.modified,fingerprint=excluded.fingerprint,pair_key=excluded.pair_key,
                    media_type=excluded.media_type,media_format=excluded.media_format,
                    sku=CASE WHEN items.source IN ('manual','shooting') OR (items.source IN ('vlm_group','ocr_folder','folder_default','portable','embedded') AND items.fingerprint=excluded.fingerprint) THEN items.sku ELSE excluded.sku END,
                    normalized=CASE WHEN items.source IN ('manual','shooting') OR (items.source IN ('vlm_group','ocr_folder','folder_default','portable','embedded') AND items.fingerprint=excluded.fingerprint) THEN items.normalized ELSE excluded.normalized END,
                    source=CASE WHEN items.source IN ('manual','shooting') OR (items.source IN ('vlm_group','ocr_folder','folder_default','portable','embedded') AND items.fingerprint=excluded.fingerprint) THEN items.source ELSE excluded.source END,
                    confidence=CASE WHEN items.source IN ('manual','shooting') OR (items.source IN ('vlm_group','ocr_folder','folder_default','portable','embedded') AND items.fingerprint=excluded.fingerprint) THEN items.confidence ELSE excluded.confidence END,
                    scan_revision=items.scan_revision+1,last_seen=excluded.last_seen,online=1,scan_epoch=excluded.scan_epoch''', accepted)
            # 目录核对可重复执行，但只有新增或源文件版本变化才需要预览解码。
            changed={row[16] for row in accepted if previous.get(row[2])!=row[9]}
            if self.on_previews and changed:self.queue_previews(changed)
            # 按访问浏览：每批入库即通知，首次进入的大目录可边读边显示。
            if on_flush and accepted and any(previous.get(row[2])!=row[9] for row in accepted):on_flush(len(accepted))
            batch.clear()
        candidates=set();entry_stats={}
        def paths():
            if visit_folder is not None:
                with os.scandir(scan_base) as entries:
                    for entry in entries:
                        if cancelled():return
                        if entry.is_symlink() or entry.is_junction() or entry.name.startswith('.') or entry.name in ('#recycle','$RECYCLE.BIN','System Volume Information'):continue
                        if entry.is_dir(follow_symlinks=False):
                            if visit_directories is not None:visit_directories.append(Path(entry.path).relative_to(base).as_posix())
                            continue
                        if not entry.is_symlink() and not entry.is_junction() and entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in MEDIA_EXTENSIONS:
                            path=Path(entry.path)
                            # Windows 目录枚举已带大小和修改时间；复用可省去每个NAS文件一次往返。
                            try:entry_stats[path]=entry.stat(follow_symlinks=False)
                            except OSError:pass
                            yield path
                return
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
                if cancelled():return {'cancelled':True}
                try:
                    stat = entry_stats.pop(path,None) or path.stat()
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
                # 访问目录首批较小，界面尽快出现第一屏；之后按原批量入库。
                if len(batch) >= (32 if on_flush and indexed < 256 else 128):
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
        if cancelled():return {'cancelled':True}
        if getattr(self, 'on_scan_progress', None):
            self.on_scan_progress(indexed)
        with self.connect() as c:
            if visit_folder is None:
                c.execute('UPDATE items SET online=0 WHERE root=? AND scan_epoch<>?', (root_id, epoch))
            else:
                c.execute('UPDATE items SET online=0 WHERE root=? AND folder=? AND scan_epoch<>?', (root_id,str(visit_folder).replace('\\','/').strip('/') or '/',epoch))
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
                        if entry.name.startswith('.framekeep') or entry.name in ('#recycle','$RECYCLE.BIN','System Volume Information','.shiying','.核查缩略图','.归档文件提交日志'):continue
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

    def roots(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM roots WHERE removed=0 ORDER BY path COLLATE NOCASE')]

    def state(self):
        page_cache=getattr(self,'summaries',None);page_key='counts';revision=None;counts=None
        with self.connect() as c:
            c.execute('BEGIN')
            if page_cache:
                try:
                    revision,counts=page_cache.read(page_key,c)
                except (sqlite3.Error,OSError,ValueError):revision=None
            roots = [dict(r) for r in c.execute('SELECT * FROM roots WHERE removed=0 ORDER BY path COLLATE NOCASE')]
            cached_hit=counts is not None
            if counts is None:
                counts = dict(c.execute('WITH ranked AS ('+_ranked_catalog('sku,source,media_type')+''')
                    SELECT count(*) AS total,sum(sku!='') AS tagged,sum(source='review') AS review,
                    sum(media_type='image') AS images,sum(media_type='video') AS videos,sum(media_type='design') AS designs
                    FROM ranked WHERE media_type<>'image' OR representative=1''').fetchone())
        counts={k: int(v or 0) for k, v in counts.items()}
        if not cached_hit and page_cache and revision is not None:
            page_cache.write(page_key,revision,counts)
        return {'roots': roots, 'counts': counts, 'metadata':self.metadata.state()}

    def _search_parts(self, connection, text):
        path, path_args = self.fast_index.condition(connection, text)
        metadata, metadata_args = self.metadata.condition(text)
        key = normalize(text)
        # 中文和标点不可变成 LIKE '%%'，否则任意中文搜索都会命中整库。
        parts = [('normalized LIKE ?', ['%'+key+'%'], 'catalog_items_sku')] if key else []
        parts.extend([(path,path_args,'catalog_pair'),(metadata,metadata_args,'catalog_pair')])
        visual,visual_args=('0',[])
        if getattr(self,'intelligence',None):
            from retrieval_store import condition
            visual,visual_args=condition(connection,text)
        parts.append((visual,visual_args,''))
        return parts

    def search_condition(self, connection, text):
        parts=self._search_parts(connection,text)
        return '('+' OR '.join(part for part,_,_ in parts)+')', [value for _,values,_ in parts for value in values]

    @staticmethod
    def _search_candidate_overflow(connection,parts,maximum):
        # 仅探测当前谓词实际使用的 FTS，LIMIT 限制读取；不 JOIN 原记录。
        # 陈旧/离线命中只会保守选择原查询，不能改变结果过滤或截断结果。
        for part,args,_ in parts:
            for table in ('reference_path_fts','catalog_metadata_fts','intelligence_fts'):
                if table+' MATCH ?' in part:
                    rows=connection.execute('SELECT 1 FROM '+table+' WHERE '+table+' MATCH ? LIMIT ?',
                                            (args[0],maximum+1)).fetchall()
                    if len(rows)>maximum:return True
        return False

    def _search_query(self, connection, text):
        # ready 判定、候选和最终过滤共用快照，短词回退也保持相同规则。
        if not connection.in_transaction:connection.execute('BEGIN')
        connection.execute('SELECT id FROM roots LIMIT 1').fetchone()
        from literal_search import candidates
        candidate_sql,candidate_args=candidates(self,connection,text)
        if candidate_sql is not None:
            return ('(SELECT id AS search_item_id FROM ('+candidate_sql+')) search_candidates CROSS JOIN capture_items ON capture_items.id=search_candidates.search_item_id',
                    candidate_args,'1',[])
        parts=self._search_parts(connection,text)
        values=[value for _,args,_ in parts for value in args]
        exact='('+' OR '.join(part for part,_,_ in parts)+')'
        # 短词无法使用 trigram；保留原规划，避免重复全表扫描。
        if len(text)<3 or '\0' in text:return 'capture_items',[],exact,values
        # 先由索引候选驱动配对视图，避免 online/排序索引让每个历史素材
        # 都执行代表图相关子查询。宽泛搜索有界探测后回退，不排序数万候选。
        pairs=set();maximum=512
        if self._search_candidate_overflow(connection,parts,maximum):return 'capture_items',[],exact,values
        for part,args,index in parts:
            sql='SELECT DISTINCT pair_key FROM items'+(' INDEXED BY '+index if index else '')+' WHERE +online=1 AND ('+part+') LIMIT ?'
            for row in connection.execute(sql,args+[maximum+1]):
                pairs.add(row[0])
                if len(pairs)>maximum:return 'capture_items',[],exact,values
        pair_values=sorted(pairs)
        candidates=('SELECT column1 AS search_pair FROM (VALUES '+','.join('(?)' for _ in pair_values)+')') if pair_values else 'SELECT NULL AS search_pair WHERE 0'
        source='('+candidates+') search_candidates CROSS JOIN capture_items'
        # 候选仅是超集：必须复核代表图自身的 SKU / visual 条件，不能让
        # 未被选为代表的 RAW 成员的 SKU 或视觉结果改变既有匹配语义。
        return source,pair_values,'pair_key=search_pair AND '+exact,values

    def tree(self,text='',media='',*,cancel=None):
        if media and media not in ('image','video','design'):raise ValueError('素材类型筛选无效')
        page_cache=getattr(self,'summaries',None);page_key='tree:'+media;revision=None;matches=None
        with self._search_connection(cancel) as c:
            if not c.in_transaction:c.execute('BEGIN')
            clauses=['online=1'];args=[];source='capture_items';source_args=[]
            if not text:
                if page_cache:
                    try:
                        revision,matches=page_cache.read(page_key,c)
                    except (sqlite3.Error,OSError,ValueError):revision=None
                if matches is None:
                    source='('+_ranked_catalog('root,folder,media_type')+')'
                    clauses=["(media_type<>'image' OR representative=1)"]
            if media:
                clauses.append('media_type=?');args.append(media)
            if text:
                source,source_args,condition,values=self._search_query(c,text)
                clauses.append(condition);args.extend(values)
            cached_hit=matches is not None
            if matches is None:
                matches={}
                for row in c.execute('SELECT root,folder,count(*) AS count FROM '+source+' WHERE '+' AND '.join(clauses)+' GROUP BY root,folder ORDER BY folder COLLATE NOCASE',source_args+args):
                    matches.setdefault(row['root'],[]).append({'folder':row['folder'],'count':row['count']})
            result=self._search_tree(c,matches,bool(text or media))
        if not text and not cached_hit and page_cache and revision is not None:
            page_cache.write(page_key,revision,matches)
        if not text:self.startup.remember('tree:'+media,result)
        return result

    @staticmethod
    def _search_tree(connection,matches,filtered):
        result=[]
        for root in connection.execute('SELECT * FROM roots WHERE removed=0 ORDER BY path COLLATE NOCASE'):
            folders=matches.get(root['id'],[])
            if filtered and not folders:continue
            result.append({'id':root['id'],'path':root['path'],'status':root['status'],'folders':folders})
        return {'roots': result}

    @contextmanager
    def _search_connection(self,cancel=None):
        with self.connect() as c:
            try:
                if cancel:
                    if cancel():raise SearchCancelled()
                    # 按批次检查，减少频繁进入 Python 的线程调度开销；
                    # 仍可在查询执行中取消，而不是等整个搜索结束。
                    c.set_progress_handler(cancel,20000)
                yield c
                if cancel and cancel():raise SearchCancelled()
            except sqlite3.OperationalError as error:
                if cancel and getattr(error,'sqlite_errorcode',None)==sqlite3.SQLITE_INTERRUPT and cancel():
                    raise SearchCancelled() from None
                raise
            finally:
                if cancel:c.set_progress_handler(None,0)

    @staticmethod
    def _page_rows(connection, ids):
        if not ids:
            return []
        # 用已选页的 ID 驱动主键读取。视图上的 IN + ORDER BY 会让 SQLite
        # 选择 online/时间索引并重新遍历整个归档，LIMIT 阶段的收益因此丢失。
        selected = '(VALUES ' + ','.join('(?)' for _ in ids) + ')'
        return [dict(row) for row in connection.execute(
            'SELECT capture_items.* FROM ' + selected + ' selected CROSS JOIN capture_items '
            'ON capture_items.id=selected.column1 ORDER BY modified DESC,id', ids)]

    def items(self, root='', folder='', sku='', offset=0, limit=120, cursor='', text='', media='', *, include_tree=False, cancel=None, quick=False, sort='modified', visual_only=False):
        if sort=='original':
            from catalog_original_order import items
            return items(self,root,folder,sku,offset,limit,cursor,text,media,include_tree=include_tree,cancel=cancel,quick=quick,visual_only=visual_only)
        if sort!='modified':raise ValueError('归档排序方式无效')
        limit = max(1, min(240, int(limit)))
        offset = max(0, int(offset))
        page_key=None;page_revision=None;page_cache=getattr(self,'pages',None)
        if page_cache and quick and not text and not include_tree:
            if cancel and cancel():raise SearchCancelled()
            page_key=_json(['browse-visited-v1' if getattr(self,'visits',None) else 'browse-v1',root,folder,sku,offset,limit,cursor,media,self.video.frame]+(['visual-only-v1'] if visual_only else []))
            try:
                page_revision,cached=page_cache.read(page_key)
                if cached is not None:
                    self.thumbnail_settings.remember(cached['rows'])
                    if cancel and cancel():raise SearchCancelled()
                    if not sku and not offset:self.startup.remember(self.startup.page_key(root,folder,media,cursor,limit,self.video.frame),cached)
                    return cached
            except (sqlite3.Error,OSError,ValueError):page_key=None
        clauses, args = ['online=1'], []
        if visual_only:clauses.append(ARCHIVE_MEDIA_SQL)
        if root:
            clauses.append('root=?'); args.append(root)
        if getattr(self,'visits',None) and root:
            clauses.append('folder=?');args.append(folder or '/')
        elif folder:
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
        tree_state=None;quick_used=False
        with self._search_connection(cancel) as c:
            if include_tree and not c.in_transaction:c.execute('BEGIN')
            page_where=' WHERE '+' AND '.join(clauses);page_args=list(args)
            source='capture_items';source_args=[]
            if root and getattr(self,'visits',None) and not text and not include_tree:
                source='''(SELECT id AS folder_item_id FROM items INDEXED BY catalog_folder_candidates
                    WHERE root=? AND online=1 AND folder COLLATE NOCASE=?) folder_candidates
                    CROSS JOIN capture_items ON capture_items.id=folder_candidates.folder_item_id'''
                source_args=[root,folder or '/']
            elif root and folder and not text and not include_tree:
                # 先按目录定位候选 ID，再读取代表图。直接在视图上按时间排序
                # 会诱使规划器遍历整个根目录，大库小文件夹也要读几十万行。
                # NOCASE 索引支持原 LIKE 的前缀范围，UNION 去重也兼容
                # 尾部带斜杠的输入；外层保留精确目录及配对、分页规则。
                source='''(SELECT id AS folder_item_id FROM items INDEXED BY catalog_folder_candidates
                    WHERE root=? AND online=1 AND folder COLLATE NOCASE=?
                    UNION SELECT id AS folder_item_id FROM items INDEXED BY catalog_folder_candidates
                    WHERE root=? AND online=1 AND folder LIKE ? ESCAPE '\\') folder_candidates
                    CROSS JOIN capture_items ON capture_items.id=folder_candidates.folder_item_id'''
                source_args=[root,folder,root,pattern]
            search_clauses=['online=1'];search_args=[]
            if visual_only:search_clauses.append(ARCHIVE_MEDIA_SQL)
            if media:search_clauses.append('media_type=?');search_args.append(media)
            if text:
                source,source_args,condition,values = self._search_query(c, text)
                clauses.append(condition)
                args.extend(values)
                search_clauses.append(condition);search_args.extend(values)
            where = ' WHERE ' + ' AND '.join(clauses)
            quick_keys=None
            if quick and not text and not folder and not sku:
                from smart_query import CANONICAL
                CANONICAL=CANONICAL.replace('items b','items b INDEXED BY catalog_pair_cover')
                driver='catalog_root_media_page' if root and media else 'catalog_items_browse' if root else 'catalog_media_page' if media else 'catalog_browse_cover'
                quick_keys=[dict(r) for r in c.execute('SELECT i.id FROM items i INDEXED BY '+driver+
                    ' WHERE '+' AND '.join(clauses+[CANONICAL])+' ORDER BY modified DESC,id LIMIT ? OFFSET ?',
                    args+[limit+1,offset if not cursor else 0])]
            elif quick and text:
                from literal_search import candidates,page_keys
                from smart_query import CANONICAL
                candidate_sql,candidate_args=candidates(self,c,text)
                if candidate_sql is not None:
                    quick_keys=page_keys(self,c,text,clauses+[CANONICAL],args,candidate_sql,candidate_args,
                                         limit=limit,offset=offset if not cursor else 0,cancel=cancel)
            if quick_keys is not None:
                quick_used=True;ids=[row['id'] for row in quick_keys]
                total=len(ids) if not cursor and not offset and len(ids)<=limit else None
                rows=self._page_rows(c,ids)
            elif include_tree:
                # 一次完整搜索，同一快照分别产生全局目录统计和当前筛选页。
                # 只物化轻量字段；游标、目录、SKU 不收窄左侧全局搜索目录。
                query='''WITH search_matches AS MATERIALIZED (
                    SELECT id,modified,root,folder,normalized,online,media_type,extension FROM '''+source+'''
                    WHERE '''+' AND '.join(search_clauses)+'''),
                  page_matches AS MATERIALIZED (SELECT id,modified FROM search_matches'''+page_where+'''),
                  page_ids AS (SELECT id FROM page_matches ORDER BY modified DESC,id LIMIT ? OFFSET ?),
                  folders AS (SELECT root,folder,count(*) AS amount FROM search_matches GROUP BY root,folder ORDER BY folder COLLATE NOCASE)
                  SELECT 'total' AS kind,NULL AS id,NULL AS root,NULL AS folder,count(*) AS amount FROM page_matches
                  UNION ALL SELECT 'folder',NULL,root,folder,amount FROM folders
                  UNION ALL SELECT 'item',id,NULL,NULL,NULL FROM page_ids'''
                records=c.execute(query,source_args+search_args+page_args+[limit+1,offset if not cursor else 0])
                ids=[];matches={};total=0
                for record in records:
                    if record['kind']=='total':total=record['amount']
                    elif record['kind']=='item':ids.append(record['id'])
                    else:matches.setdefault(record['root'],[]).append({'folder':record['folder'],'count':record['amount']})
                rows=self._page_rows(c,ids)
                tree_state=self._search_tree(c,matches,bool(text or media))
            elif text and source=='capture_items':
                # 宽词/短词完整筛选一次，仅物化 ID 与排序时间；总数和当前页
                # 复用该集合，避免两次读取大段元数据。详细字段只取当前页。
                # LEFT JOIN 保证零命中、越界 offset 仍返回准确总数。
                query='''WITH search_matches AS MATERIALIZED (
                    SELECT id,modified FROM capture_items'''+where+'''),
                  search_page AS (
                    SELECT capture_items.* FROM (
                      SELECT id AS matched_id FROM search_matches ORDER BY modified DESC,id LIMIT ? OFFSET ?
                    ) selected CROSS JOIN capture_items WHERE capture_items.id=selected.matched_id
                  )
                  SELECT search_page.*,totals.search_total
                  FROM (SELECT count(*) AS search_total FROM search_matches) totals
                  LEFT JOIN search_page ON 1 ORDER BY search_page.modified DESC,search_page.id'''
                records=c.execute(query,args+[limit+1,offset if not cursor else 0]).fetchall()
                total=records[0]['search_total'];rows=[]
                for record in records:
                    if record['id'] is not None:
                        row=dict(record);row.pop('search_total');rows.append(row)
            else:
                total = c.execute('SELECT count(*) FROM '+source + where, source_args+args).fetchone()[0]
                rows = [dict(r) for r in c.execute('SELECT capture_items.* FROM '+source + where + ' ORDER BY modified DESC,id LIMIT ? OFFSET ?', source_args+args + [limit + 1, offset if not cursor else 0])]
        has_more = len(rows) > limit
        rows = rows[:limit]
        poster_frame = self.video.frame
        for row in rows:
            row['preview'] = '/api/catalog/preview/' + row['id'] + '?v=' + _id(row['fingerprint'] + ':orientation-v1') + ('&frame=' + str(poster_frame) if row['media_type']=='video' else '')
        # 缓存位置暂不可读时仍应显示素材列表，图片请求自行回退原流程。
        try:self.cached_preview_hints(rows)
        except (OSError,sqlite3.Error):pass
        next_cursor = ''
        if rows:
            marker = [rows[-1]['modified'], rows[-1]['id']]
            next_cursor = base64.urlsafe_b64encode(_json(marker).encode()).decode()
        result={'rows': rows, 'total': total, 'offset': offset, 'has_more': has_more, 'next_cursor': next_cursor,
                'engine': self.fast_index.engine, 'media': media, 'search': text,
                **({'tree':tree_state} if include_tree else {}),
                **({'counts_pending':total is None,'tree_deferred':include_tree} if quick_used else {})}
        # 素材页的有效性由索引版本保证，不能因某张缩略图缺失而每次重启
        # 都重查整页。缺少缓存提示的图片沿用按 ID 读取预览的正常入口。
        if page_key and page_revision is not None:
            page_cache.write(page_key,page_revision,result)
        if quick and not text and not include_tree and not sku and not offset:
            self.startup.remember(self.startup.page_key(root,folder,media,cursor,limit,self.video.frame),result)
        return result

    def sku_groups(self, search=''):
        key = normalize(search)
        with self.connect() as c:
            rows = [dict(r) for r in c.execute("SELECT normalized, min(sku) AS sku, count(*) AS count, sum(source='manual') AS corrected FROM capture_items WHERE online=1 AND normalized!='' AND normalized LIKE ? GROUP BY normalized ORDER BY sku COLLATE NOCASE", ('%' + key + '%',))]
        return {'rows': rows}

    def item(self, item_id: str):
        if str(item_id).startswith(('material:','materialv:')):
            return self.intelligence.visual.item(item_id)
        with self.connect() as c:
            row = c.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        if not row:
            raise ValueError('归档图片不存在')
        return dict(row)

    def seed_portable_records(self,records,dataset_root):
        """从已校验的随行快照登记已知位置；真实目录扫描负责核对存在性。"""
        base=Path(dataset_root)
        with self.connect() as c:
            roots=[dict(r) for r in c.execute('SELECT id,path,status,scan_epoch FROM roots WHERE removed=0') if base.is_relative_to(Path(r['path']))]
        if not roots:return []
        root=max(roots,key=lambda r:len(Path(r['path']).parts));now=time.time();rows=[]
        for record in records:
            path=base.joinpath(*record['relative_path'].split('/'));relative=path.relative_to(Path(root['path'])).as_posix()
            signature=record.get('signature') or [0,0];full=str(path);extension=path.suffix.lower()
            fingerprint=sha256(f'{full.casefold()}:{signature[0]}:{signature[1]}'.encode()).hexdigest()
            code=str(record.get('sku') or '')
            rows.append((_id(full.casefold()),root['id'],full,relative,archive_folder(relative),path.name,extension,
                         signature[0],signature[1]/1e9,fingerprint,code,normalize(code),'portable',1 if code else None,
                         now,1 if root['status'] in ('queued','scanning') else 0,root['scan_epoch']-1,pair_key(full),record.get('media_type','image'),media_format(extension)))
        with self.connect() as c:
            c.executemany('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,fingerprint,sku,normalized,source,confidence,last_seen,online,scan_epoch,pair_key,media_type,media_format)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET sku=excluded.sku,normalized=excluded.normalized,source='portable',confidence=excluded.confidence
WHERE items.fingerprint=excluded.fingerprint OR items.online=0''',rows)
            return [dict(r) for r in c.execute('SELECT * FROM items WHERE path IN ('+','.join('?' for _ in rows)+')',[r[2] for r in rows])] if rows else []

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
            ocr=None
            if getattr(self,'intelligence',None):
                from catalog_ocr import details as ocr_details
                ocr=ocr_details(c,row)
        cached=self.metadata.cached(item_id)
        self.metadata.request(item_id)
        data=cached['data']
        return {'item':row,'files':files,'ocr':ocr,'capture':data.get('capture',{}),'media':data.get('media',{}),
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

    def preview_record(self,row,members,frame=None,*,direct=False):
        """只用持久索引确定缓存身份，命中时不访问原图或离线网络路径。"""
        if row['media_type']=='design':
            source=Path(row['path'])
            return str(source.parent),{'id':'catalog:'+row['id'],'_catalog':True,'_catalog_root':row['root'],'_catalog_pair':row['pair_key'],
                '_design':str(source),'fingerprint':_id(row['fingerprint']+':design-preview-v1')}
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
        if not direct and (record['raw'] or record.get('_fallback_raw')):
            from preview_cache import cache_for
            index=cache_for(self.previews)
            with index.read_connection() as c:
                cached=c.execute('SELECT orientation FROM previews WHERE fingerprint=? LIMIT 1',(record['fingerprint'],)).fetchone()
            if cached and cached[0] is not None:record['orientation']=cached[0]
        return str(source.parent),record

    def prepare_preview_record(self,record,exiftool,*,cached_only=False):
        if (record.get('raw') or record.get('_fallback_raw')) and 'orientation' not in record:
            from preview_cache import cache_for
            index=cache_for(self.previews)
            with index.read_connection() as c:
                cached=c.execute('SELECT orientation FROM previews WHERE fingerprint=? LIMIT 1',(record['fingerprint'],)).fetchone()
            if cached and cached[0] is not None:
                record['orientation']=cached[0];return
            if cached_only:return
            try:
                from media_engine import _jpeg_source
                _jpeg_source(record,record['_source_root'],exiftool)
                if 'orientation' not in record:record['orientation']=1
            except (OSError,RuntimeError,ValueError,IndexError,TypeError):
                record['orientation']=1

    def queue_previews(self,keys):
        if not keys or not (self.on_previews or getattr(self,'on_media_changes',None)):return
        groups={}
        with self.read_connection() as c:
            versions=[tuple(row) for row in c.execute('SELECT pair_key,version FROM catalog_preview_changes WHERE pair_key IN ('+','.join('?' for _ in keys)+')',list(keys))]
            for r in c.execute('SELECT * FROM items WHERE online=1 AND root IN (SELECT id FROM roots WHERE removed=0) AND pair_key IN ('+','.join('?' for _ in keys)+') ORDER BY extension,path',list(keys)):
                groups.setdefault(r['pair_key'],[]).append(dict(r))
        by_root={}
        for members in groups.values():
            root,record=self.preview_record(members[0],members);record['_source_root']=root
            by_root.setdefault(root,[]).append(record)
        for root,records in by_root.items():
            if self.on_previews and self.on_previews(root,records) is False:return False
            if getattr(self,'on_media_changes',None):self.on_media_changes(records)
        if self.on_previews:
            from catalog_decode_changes import acknowledge
            acknowledge(self,versions)
        return True

    def pending_preview_changes(self):
        with self.read_connection() as c:return {row[0] for row in c.execute('SELECT pair_key FROM catalog_preview_changes ORDER BY pair_key LIMIT 128')}

    def queue_all_previews(self,stop=lambda:False,*,videos_only=False,resume=None,checkpoint=None):
        """仅遍历本地索引，启动补齐历史归档，绝不等用户进入子目录。"""
        where="online=1 AND root IN (SELECT id FROM roots WHERE removed=0)"
        if videos_only:where+=" AND media_type='video'"
        with self.read_connection() as c:
            upper=resume['upper'] if resume else c.execute('SELECT max(pair_key) FROM items WHERE '+where).fetchone()[0]
        if upper is None:return True
        after=resume.get('after') if resume else None
        if checkpoint and not resume:checkpoint({'upper':upper,'after':after})
        # 不能在游标/读事务中等待缓存入队或暂停恢复；大库恢复可能持续
        # 数小时，旧快照会让检查点无法前进，WAL 随解码写入无限积累。
        # keyset 每批重新打开短快照；新增项仍由索引增量回调入队。
        while not stop():
            clause='' if after is None else ' AND pair_key>?'
            args=[upper] if after is None else [upper,after]
            with self.read_connection() as c:
                rows=c.execute('SELECT DISTINCT pair_key FROM items WHERE '+where+
                               ' AND pair_key<=?'+clause+' ORDER BY pair_key LIMIT 128',args).fetchall()
            if not rows:return True
            after=rows[-1][0]
            if self.queue_previews({row[0] for row in rows}) is False:return False
            if checkpoint:checkpoint({'upper':upper,'after':after})
        return False

    def preview_source_current(self,record):
        # scan_epoch 表示本轮已发现，不表示素材版本。全盘重扫期间旧在线
        # 条目仍有效；只能在扫描完成确认离线/移除后取消其持久预览任务。
        with self.connect() as c:
            row=c.execute('''SELECT items.relative,items.folder FROM items JOIN roots ON roots.id=items.root WHERE roots.removed=0
                AND items.online=1 AND items.root=? AND items.pair_key=? LIMIT 1''',
                (record['_catalog_root'],record['_catalog_pair'])).fetchone()
            if row is None:return False
            if any(part.casefold() in ('#recycle','$recycle.bin','system volume information') for part in row['relative'].replace('\\','/').split('/')):return False
            excluded=[r[0] for r in c.execute('SELECT folder FROM exclusions WHERE root=?',(record['_catalog_root'],))]
            return not any(row['folder']==folder or row['folder'].startswith(folder+'/') for folder in excluded)

    def cached_preview_hints(self,rows):
        """当前页批量关联缩略图，避免命中后仍逐图随机读取素材大库。"""
        self.thumbnail_settings.remember(rows)
        if not self.thumbnail_settings.cache_enabled:return
        images=[row for row in rows if row['media_type'] in ('image','design')]
        if not images:return
        from preview_cache import cache_for
        groups={};keys=list({row['pair_key'] for row in images})
        with self.connect() as c:
            for member in c.execute('SELECT pair_key,fingerprint,extension FROM items INDEXED BY catalog_pair WHERE online=1 AND pair_key IN ('+
                                    ','.join('?' for _ in keys)+') ORDER BY extension,path',keys):
                groups.setdefault(member['pair_key'],[]).append(dict(member))
        fingerprints={key:_id('|'.join(member['fingerprint'] for member in members)+':preview-v2') for key,members in groups.items()}
        fingerprints.update({row['pair_key']:_id(row['fingerprint']+':design-preview-v1') for row in images if row['media_type']=='design'})
        if not fingerprints:return
        index=cache_for(self.previews)
        # 迁移期间与普通缓存读取共用租约；只读索引，不读取NAS或生成图片。
        with index.budget.generation() if index.budget else nullcontext(),index.read_connection() as c:
            # 地址提示是可选捷径。慢缓存盘不能阻塞素材列表的首次显示。
            deadline=time.monotonic()+.2
            c.set_progress_handler(lambda:time.monotonic()>deadline,100)
            try:
                # 首屏只查420缩略图覆盖索引；逐条回大缓存表会超过预算，
                # 导致整页丢掉缓存地址、每张图再随机访问24GB素材库。
                cached=list(c.execute('SELECT name,fingerprint,edge,decoded,orientation FROM previews INDEXED BY preview_hint_v1 WHERE edge=420 AND decoded=0 AND fingerprint IN ('+
                                      ','.join('?' for _ in fingerprints)+') ORDER BY rowid',list(fingerprints.values())))
            finally:c.set_progress_handler(None,0)
        by_fingerprint={}
        for hit in cached:by_fingerprint.setdefault(hit['fingerprint'],[]).append(hit)
        for row in images:
            members=groups.get(row['pair_key'],[]);fingerprint=fingerprints.get(row['pair_key'])
            hits=by_fingerprint.get(fingerprint,[])
            orientation=1
            if not any(member['extension'] in {'.jpg','.jpeg'} for member in members) and hits:
                orientation=hits[0]['orientation'] if hits[0]['orientation'] is not None else 1
            from preview_cache import registered_recipe
            hit=next((hit for hit in hits if hit['edge']==420 and not hit['decoded'] and hit['orientation']==orientation and registered_recipe(hit) in (6,7,8,9,10)),None)
            if hit:row['preview']+=('&' if '?' in row['preview'] else '?')+'cached='+hit['name']

    def rating_rows(self, item_ids):
        """按当前浏览页读取评级所需的最小字段，不重新执行归档列表查询。"""
        ids=list(dict.fromkeys(str(item_id) for item_id in item_ids))
        if not ids:return []
        self.metadata.request_many(ids)
        with self.connect() as c:
            found={row['id']:dict(row) for row in c.execute(
                "SELECT i.id,i.path,i.sku,i.source,coalesce(m.status,'queued') AS metadata_status FROM items i LEFT JOIN catalog_metadata m ON m.item=i.id WHERE i.id IN ("+','.join('?' for _ in ids)+')',ids)}
        return [found[item_id] for item_id in ids if item_id in found]

    def preview(self, item_id: str, size=420, *, read=False, frame=None, cached=None):
        if str(item_id).startswith(('material:','materialv:')):
            return self.intelligence.visual.preview(item_id,size,frame)
        if read and int(size)<=480:return self.direct_thumbnail(item_id,size,frame)
        # 仅接受受管缓存名；尺寸不同或已被淘汰时沿用正常查源/生成路径。
        # URL绑定不可变缓存键；_read仍检查登记、长度、格式和完整哈希。
        if not read and cached and int(size)==420 and re.fullmatch(r'[0-9a-f]{64}\.jpg',cached):
            from preview_cache import cache_for
            index=cache_for(self.previews)
            with index.budget.generation() if index.budget else nullcontext():
                hit=index.get(cached,read=read)
            if hit is not None:return hit
        row=self.item(item_id)
        import media_engine as media
        with self.connect() as c:
            members=[dict(r) for r in c.execute('SELECT * FROM items WHERE pair_key=? AND online=1 ORDER BY extension,path',(row['pair_key'],))]
        root,record=self.preview_record(row,members,frame=frame,direct=read);record['_source_root']=root
        tool=lambda:media.find_exiftool(Path(__file__).resolve().parents[1])
        from preview_errors import PreviewIgnored
        if read:
            from direct_previews import preview_bytes
            return preview_bytes(record,root,tool,size)
        try:return self.render_preview(record,root,size,tool,read=read)
        except PreviewIgnored:
            if row['media_type']=='design':return self._media_placeholder(row,size)
            raise

    def direct_thumbnail(self,item_id,size=420,frame=None):
        from archive_thumbnails import file_thumbnail
        import media_engine as media
        row=self.thumbnail_settings.row(item_id) or self.item(item_id)
        frame=self.video.frame if frame is None else frame
        side=max(160,min(480,int(size)))
        key=(row['path'],row.get('fingerprint'),row['media_type'],side,str(frame) if row['media_type']=='video' else '')
        return self.thumbnail_settings.produce(key,lambda:file_thumbnail(row,size,lambda:media.find_exiftool(Path(__file__).resolve().parents[1]),frame))

    def render_preview(self,record,root,size,exiftool,*,read=False):
        import media_engine as media
        if read:
            from direct_previews import preview_bytes
            return preview_bytes(record,root,exiftool,size)
        self.prepare_preview_record(record,exiftool,cached_only=True)
        hit=media.cached_preview(record,self.previews,size,read=read)
        if hit is not None and not record.get('_portable_prepare'):return hit
        warmer=getattr(self,'preview_warmer',None)
        ignored=warmer.ignored_reason(record,root) if warmer else None
        if ignored:raise ignored
        # 同一源版本的方向解析和两尺寸生成共用一次提取；420/2048 以及
        # 前后台并发请求不能在进入各尺寸缓存锁之前重复读取 RAW 容器。
        with media._preview_lock('catalog-source:'+record['fingerprint']):
            self.prepare_preview_record(record,exiftool)
            if record.get('_source_jpeg') is not None and not getattr(self,'visits',None):
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
        with media._preview_lock('catalog-source:'+record['fingerprint']):
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
