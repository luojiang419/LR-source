"""归档资料持久队列：扫描只排队，详情和搜索只读取数据库。"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time
from catalog_metadata_codec import pack, unpack
import worker_activity as activity

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
        self.foreground_thread = None
        self.error = ''
        self.scheduler = None
        self.fts = False
        self.start_lock = threading.Lock()
        self.compact_lock = threading.Lock()
        self.maintenance_blocked = None
        with catalog.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS catalog_metadata(
                  item TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                  payload TEXT NOT NULL DEFAULT '{}', search_text TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL DEFAULT 0,
                  priority INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS catalog_metadata_queue ON catalog_metadata(status,priority DESC,updated,item);
                CREATE INDEX IF NOT EXISTS catalog_metadata_status_cover ON catalog_metadata(item,status);
                CREATE TABLE IF NOT EXISTS catalog_metadata_maintenance(
                  key TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,
                  done INTEGER NOT NULL DEFAULT 0, rewritten INTEGER NOT NULL DEFAULT 0
                );
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
            # 新库没有历史大字段；后续写入已使用紧凑格式，不必再扫描新入库队列。
            c.execute("""INSERT OR IGNORE INTO catalog_metadata_maintenance(key,done)
                SELECT 'compact-v2',1 WHERE NOT EXISTS(SELECT 1 FROM catalog_metadata LIMIT 1)""")
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
            self.thread = threading.Thread(target=self._run, name='catalog-metadata', daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=35)
        if self.foreground_thread is not None:
            self.foreground_thread.join(timeout=35)

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

    def request_many(self, item_ids, *, verify=False):
        """浏览页只提升现有任务优先级；不在请求线程读取 NAS 或重排已完成项。"""
        ids=list(dict.fromkeys(item_ids))
        if not ids:return
        try:
            with self.catalog.connect() as c:
                c.execute('PRAGMA busy_timeout=0')
                marks=','.join('?' for _ in ids)
                c.execute('''INSERT OR IGNORE INTO catalog_metadata(item,fingerprint,priority)
                    SELECT id,fingerprint,1 FROM items WHERE id IN ('''+marks+')',ids)
                c.execute("UPDATE catalog_metadata SET priority=1 WHERE status='queued' AND priority<1 AND item IN ("+marks+')',ids)
                if verify:
                    c.execute("UPDATE catalog_metadata SET status='queued',priority=1,generation=generation+1 WHERE status IN ('ready','failed') AND item IN ("+marks+')',ids)
        except sqlite3.OperationalError:
            pass  # 页面下次补读时重试，不能让后台写锁挡住首屏。
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
            counts = dict(c.execute('''SELECT coalesce(m.status,'queued'),count(*) FROM items i INDEXED BY catalog_browse_cover
                LEFT JOIN catalog_metadata m INDEXED BY catalog_metadata_status_cover ON i.id=m.item JOIN roots r ON r.id=i.root
                WHERE i.online=1 AND r.removed=0 GROUP BY coalesce(m.status,'queued')'''))
        return {'total':sum(counts.values()), 'ready':counts.get('ready',0),
                'pending':counts.get('queued',0)+counts.get('running',0), 'failed':counts.get('failed',0), 'error':self.error}

    def cached(self, item_id):
        with self.catalog.connect() as c:
            row = c.execute('SELECT * FROM catalog_metadata WHERE item=?', (item_id,)).fetchone()
        if not row:
            return {'status':'queued', 'data':{}, 'error':''}
        return {'status':row['status'], 'data':unpack(row['payload']), 'error':row['error'], 'updated':row['updated']}

    def condition(self, text):
        # 从任一配对成员命中代表图；过滤旧文件版本，短词完整回退。
        args = []
        candidate = ''
        if self.fts and len(text)>=3 and '\0' not in text:
            candidate = 'm.rowid IN (SELECT rowid FROM catalog_metadata_fts WHERE catalog_metadata_fts MATCH ?) AND '
            args.append('"'+text.replace('"','""')+'"')
        args.append(text)
        # 短词也先过滤资料文本，再按主键校验素材版本；否则优化器可能从
        # online/排序索引逐项随机读取大段 payload/search_text，放大磁盘开销。
        return ('''pair_key IN (SELECT i.pair_key FROM catalog_metadata m CROSS JOIN items i ON i.id=m.item
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
            from metadata_writer import metadata_json_types,windows_text
            values = metadata_json_types(json.loads(session.execute(['-charset','filename=UTF8','-fast','-json','-api','StructFormat=JSONQ','-n','-G1','-s','-all:all',str(path)], cancel=self.stop))[0])
        if any(tag in values for tag in ('IFD0:XPSubject','IFD0:XPKeywords')):values.update(windows_text(path))
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
        capture = payload.get('capture', {})
        def text(value):
            if isinstance(value,dict):
                return '\n'.join(str(key)+' '+text(item) for key,item in value.items())
            if isinstance(value,list):
                return '\n'.join(text(item) for item in value)
            return '' if value is None else str(value)
        # 保存原始文字而非 JSON 转义后的字符串，使引号、换行和反斜线可按字面查找。
        description = {key:value for key,value in payload.get('description',{}).items()
                       if key in ('title','description','keywords','label')}
        tags = {key:value for key,value in payload.get('tags',{}).items()
                if key.rsplit(':',1)[-1] in ('SKU','ShootType')}
        search = text({'capture':capture,'media':payload.get('media',{}),'description':description,'tags':tags})
        # 同时支持常用的人类输入形式，如 1/125、f/2.8、ISO 400、日期及尺寸。
        if capture.get('shutter') and isinstance(capture['shutter'],(float,int)) and capture['shutter']<1:
            search += f" 1/{round(1/capture['shutter'])}"
        search += f" ISO {capture.get('iso','')} f/{capture.get('aperture','')} {capture.get('width','')} × {capture.get('height','')}"
        return search

    def compact_batch(self, limit=32):
        """按 rowid 断点压缩；不读原图，不更改任务代际或启动时重写整库。"""
        if self.maintenance_blocked and self.maintenance_blocked():return False
        if not self.compact_lock.acquire(False):return False
        try:
            with self.catalog.connect() as c:
                c.execute('PRAGMA busy_timeout=50')
                progress=c.execute("SELECT cursor,done FROM catalog_metadata_maintenance WHERE key='compact-v2'").fetchone()
                if progress and progress['done']:return False
                cursor=progress['cursor'] if progress else 0
                rows=c.execute('SELECT rowid AS rid,item,fingerprint,generation,payload FROM catalog_metadata WHERE rowid>? ORDER BY rowid LIMIT ?', (cursor,limit)).fetchall()
            changes=[]
            for row in rows:
                payload=json.loads(row['payload'])
                if payload.get('_format')==2 or not payload.get('capture'):continue
                payload=unpack(row['payload'])
                changes.append((pack(payload),self._search_text(payload),row['item'],row['fingerprint'],row['generation'],row['payload']))
            with self.catalog.connect() as c:
                c.execute('PRAGMA busy_timeout=50')
                c.executemany('UPDATE catalog_metadata SET payload=?,search_text=? WHERE item=? AND fingerprint=? AND generation=? AND payload=?',changes)
                c.execute("""INSERT INTO catalog_metadata_maintenance(key,cursor,done,rewritten) VALUES('compact-v2',?,?,?)
                    ON CONFLICT(key) DO UPDATE SET cursor=excluded.cursor,done=excluded.done,rewritten=rewritten+excluded.rewritten""",
                    (rows[-1]['rid'] if rows else cursor,int(len(rows)<limit),len(changes)))
            return bool(rows)
        finally:self.compact_lock.release()

    def process_one(self, *, priority_only=False):
        priority=priority_only or bool(self.scheduler and self.scheduler.background_blocked())
        visits=getattr(self.catalog,'visits',None);scope='';scope_args=[]
        if visits:
            with visits.lock:key=visits.key;path=visits.path
            if not key or path is None or not visits.allows(path/'_scope'):return False
            scope=' AND i.root=? AND i.folder=?';scope_args=[key[0],key[1] or '/']
        with self.catalog.connect() as c:
            if priority_only and not c.execute("SELECT 1 FROM catalog_metadata WHERE status='queued' AND priority>0 LIMIT 1").fetchone():
                return False
            row = c.execute('''SELECT i.*,m.generation FROM catalog_metadata m JOIN items i ON i.id=m.item
                JOIN roots r ON r.id=i.root WHERE m.status='queued' AND i.online=1 AND r.removed=0'''+
                (' AND m.priority>0' if priority else '')+scope+' ORDER BY m.priority DESC,m.updated,m.item LIMIT 1',scope_args).fetchone()
            if row is None:
                return False
            c.execute('BEGIN IMMEDIATE')
            row = dict(row)
            visits=getattr(self.catalog,'visits',None)
            if visits and not visits.allows(row['path']):
                c.execute('UPDATE catalog_metadata SET priority=0 WHERE item=?',(row['id'],))
                return False
            claimed=c.execute("UPDATE catalog_metadata SET status='running' WHERE item=? AND status='queued' AND generation=? AND fingerprint=?", (row['id'],row['generation'],row['fingerprint'])).rowcount
            if not claimed:return False
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
            saved=c.execute('''UPDATE catalog_metadata SET status=?,error=?,updated=?,priority=0,
                payload=coalesce(?,payload),search_text=coalesce(?,search_text)
                WHERE item=? AND fingerprint=? AND generation=?''',
                (status,error,time.time(),pack(payload) if payload is not None else None,search,row['id'],row['fingerprint'],row['generation'])).rowcount
            if saved and status=='ready':
                from sku_match import normalize
                tags=payload.get('tags',{});sku=tags.get('XMP-fk:SKU','')
                subjects=tags.get('XMP-dc:Subject',[])
                subjects=subjects if isinstance(subjects,list) else [subjects]
                codes=list(dict.fromkeys(value[4:] for value in subjects if isinstance(value,str) and value.startswith('SKU:') and value[4:].strip()))
                if not sku and len(codes)==1:sku=codes[0]
                if isinstance(sku,str) and len(sku)<=80 and not any(ord(ch)<32 for ch in sku):
                    sku=sku.strip();key=normalize(sku)
                    if key:
                        c.execute("""UPDATE items SET sku=?,normalized=?,source='embedded',confidence=1
 WHERE id=? AND fingerprint=? AND source IN ('unmatched','review','path_rule','embedded')""",(sku,key,row['id'],row['fingerprint']))
                    elif not codes:
                        c.execute("UPDATE items SET sku='',normalized='',source='unmatched',confidence=NULL WHERE id=? AND fingerprint=? AND source='embedded'",(row['id'],row['fingerprint']))
        if saved and status=='ready' and getattr(self.catalog,'capture_service',None):
            self.catalog.capture_service.observe_metadata(row,payload)
        return True

    def _run_foreground(self):
        # 另一共享盘离线时，其 stat/EXIF 不能堵住用户正在浏览的目录。
        while not self.stop.is_set():
            observed=activity.revision()
            self.wake.clear()
            try:
                if self.process_one(priority_only=True):continue
                if not (self.scheduler and self.scheduler.background_blocked()) and self.compact_batch():
                    self.stop.wait(.1);continue
            except Exception as exc:
                self.error=str(exc)
            activity.wait(self.stop,observed,wake=self.wake)

    def _run(self):
        recovered=False;restored=False
        if getattr(self.catalog,'visits',None):
            # 旧后台队列留存，但本轮只领取用户访问范围内的优先任务。
            with self.catalog.connect() as c:c.execute("UPDATE catalog_metadata SET status='queued' WHERE status='running'")
            self._run_foreground()
            return
        while not self.stop.is_set():
            try:
                if not restored:
                    with self.catalog.connect() as c:c.execute("UPDATE catalog_metadata SET status='queued' WHERE status='running'")
                    restored=True
                    self.foreground_thread=threading.Thread(target=self._run_foreground,name='catalog-metadata-visible',daemon=True)
                    self.foreground_thread.start()
                if not recovered and not (self.scheduler and self.scheduler.background_blocked()):
                    # 全库重启核对属于后台恢复，不能阻塞引擎的 HTTP 就绪。
                    # 同一事务恢复，异常时回滚并由本线程重试。
                    with self.catalog.connect() as c:
                        c.execute("UPDATE catalog_metadata SET status='queued',priority=-1 WHERE status='ready'")
                    recovered=True
                if self.process_one():
                    self.error = ''
                    self.stop.wait(.03)
                    continue
                if self.scheduler and self.scheduler.background_blocked():
                    # OCR 批次期间只响应用户主动查看的资料，不读取 NAS 全库 EXIF。
                    self.stop.wait(.5);continue
                # 分批补齐旧版本存量；新文件和版本变化由扫描触发器立即排队。
                with self.catalog.connect() as c:
                    added=c.execute('''INSERT OR IGNORE INTO catalog_metadata(item,fingerprint)
                        SELECT i.id,i.fingerprint FROM items i LEFT JOIN catalog_metadata m ON m.item=i.id
                        WHERE i.online=1 AND m.item IS NULL LIMIT 512''').rowcount
                if added:
                    continue
                if self.compact_batch():continue
                self.wake.wait(10)
                self.wake.clear()
            except sqlite3.Error as exc:
                self.error = str(exc)
                self.stop.wait(2)
