"""归档资料持久队列：扫描只排队，详情和搜索只读取数据库。"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time

import media_engine as media
from rating_xmp import sidecar_metadata, sidecar_paths, sidecar_fingerprint


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class CatalogMetadata:
    def __init__(self, catalog):
        self.catalog = catalog
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.error = ''
        self.fts = False
        self.start_lock = threading.Lock()
        with catalog.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS catalog_metadata(
                  item TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                  payload TEXT NOT NULL DEFAULT '{}', search_text TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL DEFAULT 0,
                  priority INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS catalog_metadata_queue ON catalog_metadata(status,priority DESC,updated,item);
                CREATE TRIGGER IF NOT EXISTS catalog_metadata_insert AFTER INSERT ON items BEGIN
                  INSERT OR IGNORE INTO catalog_metadata(item,fingerprint) VALUES(new.id,new.fingerprint);
                END;
                CREATE TRIGGER IF NOT EXISTS catalog_metadata_change AFTER UPDATE OF fingerprint,online ON items
                  WHEN old.fingerprint<>new.fingerprint OR (old.online=0 AND new.online=1) BEGIN
                  INSERT INTO catalog_metadata(item,fingerprint) VALUES(new.id,new.fingerprint)
                  ON CONFLICT(item) DO UPDATE SET fingerprint=new.fingerprint,status='queued',error='',
                    generation=generation+1;
                END;
                CREATE TRIGGER IF NOT EXISTS catalog_metadata_delete AFTER DELETE ON items BEGIN
                  DELETE FROM catalog_metadata WHERE item=old.id;
                END;
                CREATE TRIGGER IF NOT EXISTS catalog_metadata_retry AFTER UPDATE OF last_seen ON items
                  WHEN old.last_seen<>new.last_seen BEGIN
                  UPDATE catalog_metadata SET status='queued',generation=generation+1
                    WHERE item=new.id AND status='failed';
                END;
            ''')
            try:
                c.executescript('''
                    CREATE VIRTUAL TABLE IF NOT EXISTS catalog_metadata_fts
                      USING fts5(search_text, content='catalog_metadata', content_rowid='rowid', tokenize='trigram');
                    CREATE TRIGGER IF NOT EXISTS catalog_metadata_search_insert AFTER INSERT ON catalog_metadata BEGIN
                      INSERT INTO catalog_metadata_fts(rowid,search_text) VALUES(new.rowid,new.search_text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS catalog_metadata_search_delete AFTER DELETE ON catalog_metadata BEGIN
                      INSERT INTO catalog_metadata_fts(catalog_metadata_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text);
                    END;
                    CREATE TRIGGER IF NOT EXISTS catalog_metadata_search_update AFTER UPDATE OF search_text ON catalog_metadata
                      WHEN old.search_text IS NOT new.search_text BEGIN
                      INSERT INTO catalog_metadata_fts(catalog_metadata_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text);
                      INSERT INTO catalog_metadata_fts(rowid,search_text) VALUES(new.rowid,new.search_text);
                    END;
                ''')
                self.fts = True
            except sqlite3.OperationalError:
                pass  # 无 trigram 支持的 SQLite 保留完整字面搜索。

    def start(self):
        with self.start_lock:
            if self.thread is not None:
                return
            with self.catalog.connect() as c:
                c.execute("UPDATE catalog_metadata SET status='queued' WHERE status='running'")
                # 重启后低优先级核对文件/XMP 身份；未变化则复用资料，不再次提取。
                c.execute("UPDATE catalog_metadata SET status='queued',priority=-1 WHERE status='ready'")
            self.thread = threading.Thread(target=self._run, name='catalog-metadata', daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=35)

    def request(self, item_id, *, force=False):
        # 不访问路径，也不为每次查看已完成项执行写事务。
        try:
            with self.catalog.connect() as c:
                c.execute('PRAGMA busy_timeout='+('150' if force else '0'))
                row = c.execute('SELECT status FROM catalog_metadata WHERE item=?', (item_id,)).fetchone()
                if force:
                    c.execute("UPDATE catalog_metadata SET status='queued',priority=1,error='',generation=generation+1 WHERE item=?", (item_id,))
                elif row and row['status']=='queued':
                    c.execute('UPDATE catalog_metadata SET priority=1 WHERE item=? AND priority<1', (item_id,))
                elif row is None:
                    c.execute('''INSERT OR IGNORE INTO catalog_metadata(item,fingerprint,priority)
                        SELECT id,fingerprint,1 FROM items WHERE id=?''', (item_id,))
        except sqlite3.OperationalError:
            if force:
                raise ValueError('资料库正在更新，请稍后重新读取') from None
        self.wake.set()

    def changed_path(self, path):
        path = str(path)
        with self.catalog.connect() as c:
            rows = c.execute('SELECT id FROM items WHERE path=? COLLATE NOCASE OR path=? COLLATE NOCASE',
                             (path, str(Path(path).with_suffix('')) if path.lower().endswith('.xmp') else path)).fetchall()
            # foo.xmp 对应 foo.JPG / foo.CR3，foo.JPG.xmp 对应完整文件名。
            if path.lower().endswith('.xmp'):
                rows += c.execute('SELECT id FROM items WHERE pair_key=?', (str(Path(path).with_suffix('')).casefold(),)).fetchall()
            c.executemany("UPDATE catalog_metadata SET status='queued',error='',generation=generation+1 WHERE item=?", [(row[0],) for row in rows])
        self.wake.set()

    def state(self):
        with self.catalog.connect() as c:
            counts = dict(c.execute('''SELECT coalesce(m.status,'queued'),count(*) FROM items i
                LEFT JOIN catalog_metadata m ON i.id=m.item JOIN roots r ON r.id=i.root
                WHERE i.online=1 AND r.removed=0 GROUP BY coalesce(m.status,'queued')'''))
        return {'total':sum(counts.values()), 'ready':counts.get('ready',0),
                'pending':counts.get('queued',0)+counts.get('running',0), 'failed':counts.get('failed',0), 'error':self.error}

    def cached(self, item_id):
        with self.catalog.connect() as c:
            row = c.execute('SELECT * FROM catalog_metadata WHERE item=?', (item_id,)).fetchone()
        if not row:
            return {'status':'queued', 'data':{}, 'error':''}
        return {'status':row['status'], 'data':json.loads(row['payload']), 'error':row['error'], 'updated':row['updated']}

    def condition(self, text):
        # 从任一配对成员命中代表图；过滤旧文件版本，短词完整回退。
        args = []
        candidate = ''
        if self.fts and len(text)>=3 and '\0' not in text:
            candidate = 'm.rowid IN (SELECT rowid FROM catalog_metadata_fts WHERE catalog_metadata_fts MATCH ?) AND '
            args.append('"'+text.replace('"','""')+'"')
        args.append(text)
        return ('''pair_key IN (SELECT i.pair_key FROM catalog_metadata m JOIN items i ON i.id=m.item
            WHERE i.online=1 AND m.fingerprint=i.fingerprint AND '''+candidate+'''instr(lower(m.search_text),lower(?))>0)''', args)

    def _extract(self, row):
        path = Path(row['path'])
        before = path.stat()
        from hashlib import sha256
        fingerprint = sha256(f'{str(path).casefold()}:{before.st_size}:{before.st_mtime_ns}'.encode()).hexdigest()
        if fingerprint!=row['fingerprint']:
            with self.catalog.connect() as c:
                c.execute('''UPDATE items SET fingerprint=?,size=?,modified=? WHERE id=? AND fingerprint=?''',
                          (fingerprint,before.st_size,before.st_mtime,row['id'],row['fingerprint']))
            raise ValueError('文件已变化，等待目录索引刷新')
        signature = sidecar_fingerprint([path])
        stamp = [row['fingerprint'],signature]
        cached = self.cached(row['id'])['data']
        if cached.get('stamp')==stamp:
            return cached, self._search_text(cached)
        tool = media.find_exiftool(Path(__file__).resolve().parents[1])
        # 所有文本标签一次提取；复用常驻 ExifTool，避免每张图片启动新进程。
        with media.preview_exif_session(tool) as session:
            values = json.loads(session.execute(['-charset','filename=UTF8','-fast','-json','-n','-G1','-s','-all:all',str(path)], cancel=self.stop))[0]
        error = values.get('ExifTool:Error') or values.get('Error')
        if error:
            raise ValueError(str(error))
        values.update(sidecar_metadata(path, grouped=True))
        flat = {key.split(':')[-1]:value for key,value in values.items()}
        def get(*keys):
            return next((flat[key] for key in keys if flat.get(key) is not None), None)
        capture = {'time':media.parse_time(flat) or media.parse_time({'DateTimeOriginal':get('CreateDate','MediaCreateDate')}), 'make':get('Make'), 'camera':get('Model'),
                   'lens':get('LensModel','LensID','Lens'), 'iso':get('ISO'), 'aperture':get('FNumber'),
                   'shutter':get('ExposureTime'), 'focal_length':get('FocalLength'),
                   'width':get('ImageWidth','SourceImageWidth'), 'height':get('ImageHeight','SourceImageHeight')}
        if not capture['width'] or not capture['height']:
            if row['media_type']=='image':
                try:
                    from PIL import Image
                    with Image.open(path) as image:
                        capture.update(width=image.width, height=image.height)
                except OSError:
                    pass
        technical = self.catalog._video_details(row,path) if row['media_type']=='video' else {}
        if technical:
            capture.update({key:technical[key] for key in ('width','height') if technical.get(key)})
            if not technical.get('codec'):
                raise ValueError('视频资料探测失败，请检查本机解码组件或文件')
        keywords = values.get('XMP-dc:Subject', get('Keywords','Subject') or [])
        if not isinstance(keywords,list):
            keywords = [keywords]
        managed={f"SKU:{get('SKU')}",f"ShootType:{get('ShootType')}"}
        keywords=[str(value) for value in keywords if value not in managed]
        def string(value):
            if isinstance(value,dict):value=value.get('x-default',next(iter(value.values()),''))
            return '' if value is None else str(value)
        from photo_metadata import token
        description = {'title':string(values.get('XMP-dc:Title',get('Title') or '')),
                       'description':string(values.get('XMP-dc:Description',get('Description','ImageDescription','Caption-Abstract') or '')),
                       'keywords':keywords, 'label':string(values.get('XMP-xmp:Label',get('Label') or '')),
                       'token':token(path) if row['media_type']=='image' else '',
                       'sidecars':[p.name for p in sidecar_paths(path)],
                       'writable':row['media_type']=='image'}
        after = path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or signature!=sidecar_fingerprint([path]):
            raise ValueError('资料读取期间原文件或 XMP 发生变化，等待重新入库')
        # 确认与扫描版本一致，避免将新文件内容保存到旧指纹。
        fingerprint = sha256(f'{str(path).casefold()}:{after.st_size}:{after.st_mtime_ns}'.encode()).hexdigest()
        if fingerprint!=row['fingerprint']:
            raise ValueError('文件已变化，等待目录索引刷新')
        payload = {'capture':capture,'media':technical,'description':description,'tags':values,'stamp':stamp}
        return payload, self._search_text(payload)

    @staticmethod
    def _search_text(payload):
        capture = payload['capture']
        def text(value):
            if isinstance(value,dict):
                return '\n'.join(str(key)+' '+text(item) for key,item in value.items())
            if isinstance(value,list):
                return '\n'.join(text(item) for item in value)
            return '' if value is None else str(value)
        # 保存原始文字而非 JSON 转义后的字符串，使引号、换行和反斜线可按字面查找。
        search = text({key:value for key,value in payload.items() if key!='stamp'})
        # 同时支持常用的人类输入形式，如 1/125、f/2.8、ISO 400、日期及尺寸。
        if capture['shutter'] and isinstance(capture['shutter'],(float,int)) and capture['shutter']<1:
            search += f" 1/{round(1/capture['shutter'])}"
        search += f" ISO {capture['iso']} f/{capture['aperture']} {capture['width']} × {capture['height']}"
        return search

    def process_one(self):
        with self.catalog.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('''SELECT i.*,m.generation FROM catalog_metadata m JOIN items i ON i.id=m.item
                JOIN roots r ON r.id=i.root WHERE m.status='queued' AND i.online=1 AND r.removed=0
                ORDER BY m.priority DESC,m.updated,m.item LIMIT 1''').fetchone()
            if row is None:
                return False
            row = dict(row)
            c.execute("UPDATE catalog_metadata SET status='running' WHERE item=?", (row['id'],))
        try:
            payload, search = self._extract(row)
            status, error = 'ready', ''
        except Exception as exc:
            if self.stop.is_set():
                status = 'queued'
            else:
                status = 'failed'
            payload, search, error = None, None, str(exc)[:1000]
        with self.catalog.connect() as c:
            c.execute('''UPDATE catalog_metadata SET status=?,error=?,updated=?,priority=0,
                payload=coalesce(?,payload),search_text=coalesce(?,search_text)
                WHERE item=? AND fingerprint=? AND generation=?''',
                (status,error,time.time(),dumps(payload) if payload is not None else None,search,row['id'],row['fingerprint'],row['generation']))
        return True

    def _run(self):
        while not self.stop.is_set():
            try:
                if self.process_one():
                    self.error = ''
                    self.stop.wait(.03)
                    continue
                # 分批补齐旧版本存量；新文件和版本变化由扫描触发器立即排队。
                with self.catalog.connect() as c:
                    added=c.execute('''INSERT OR IGNORE INTO catalog_metadata(item,fingerprint)
                        SELECT i.id,i.fingerprint FROM items i LEFT JOIN catalog_metadata m ON m.item=i.id
                        WHERE i.online=1 AND m.item IS NULL LIMIT 512''').rowcount
                if added:
                    continue
                self.wake.wait(2)
                self.wake.clear()
            except sqlite3.Error as exc:
                self.error = str(exc)
                self.stop.wait(2)
