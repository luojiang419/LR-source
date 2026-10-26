"""隔离归档实验：引用、OCR 证据、结束区间、人审与受控元数据。"""
from collections import defaultdict
from hashlib import sha256
from io import BytesIO
import json
import base64
from pathlib import Path
import re
import threading
import time
import uuid
import urllib.request

from PIL import Image, ImageOps
from asset_catalog import AssetCatalog, _json
from metadata_writer import MetadataWriter, sha
from sku_match import normalize, extract_codes
from sku_vision import recognize

PROVIDER = {'id': 'archive-demo-local', 'model': 'glm-ocr-local', 'protocol': 'local',
            'json_mode': False, 'timeout': 180, 'max_tokens': 512}


class ArchiveDemo(AssetCatalog):
    def __init__(self, data, root, base):
        super().__init__(data)
        self.base = Path(base).resolve()
        self.writer = MetadataWriter(Path(__import__('os').environ.get('ARCHIVE_V2_ORIGINAL', root)), data)
        self.gate = threading.RLock()
        self.cancel = threading.Event()
        self.job = {'status': 'idle', 'done': 0, 'total': 0, 'current': '', 'errors': 0}
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS identities(item TEXT PRIMARY KEY, asset_id TEXT NOT NULL, file_id TEXT NOT NULL,
                content_hash TEXT, evidence TEXT NOT NULL DEFAULT '{}', stream TEXT, order_key TEXT, ordering TEXT,
                role TEXT NOT NULL DEFAULT 'photo', write_status TEXT NOT NULL DEFAULT '未写入', written_sku TEXT);
            CREATE TABLE IF NOT EXISTS ocr_cache(hash TEXT PRIMARY KEY, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intervals(id TEXT PRIMARY KEY, label TEXT UNIQUE NOT NULL, sku TEXT NOT NULL,
                members TEXT NOT NULL, status TEXT NOT NULL, ordering TEXT NOT NULL, stream TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS demo_events(id INTEGER PRIMARY KEY, created REAL, kind TEXT, detail TEXT);
            ''')
        if not self.state()['roots'] and (self.base / 'samples').is_dir():
            self.add_root(str(self.base / 'samples'))

    @staticmethod
    def _guess_sku(relative):
        # 目录只能帮助定位，不能冒充读取到了照片上的款号。
        return '', '', 'unmatched', None

    def event(self, kind, detail):
        with self.connect() as c:
            c.execute('INSERT INTO demo_events(created,kind,detail) VALUES (?,?,?)', (time.time(), kind, _json(detail)))

    def _identity(self, row):
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO identities(item,asset_id,file_id) VALUES (?,?,?)',
                      (row['id'], str(uuid.uuid4()), str(uuid.uuid4())))

    def scan(self, root_id):
        result = super().scan(root_id)
        with self.connect() as c:
            rows = c.execute('SELECT * FROM items WHERE root=? AND online=1', (root_id,)).fetchall()
        for row in rows:
            self._identity(row)
        return result

    def enrich(self, row):
        row = dict(row)
        with self.connect() as c:
            extra = c.execute('SELECT * FROM identities WHERE item=?', (row['id'],)).fetchone()
            interval = c.execute('SELECT * FROM intervals WHERE label=?', (row['id'],)).fetchone()
        if extra:
            row.update(dict(extra))
        row['evidence'] = json.loads(row.get('evidence') or '{}')
        row['preview'] = '/api/catalog/preview/' + row['id']
        row['writable'] = Path(row['path']).resolve().is_relative_to(self.base / 'samples')
        if interval:
            row['interval'] = dict(interval)
            row['interval']['members'] = json.loads(interval['members'])
        row['candidate'] = (row['interval']['sku'] if interval else '') or next(iter(row['evidence'].get('codes', [])), '')
        return row

    def get(self, action, query):
        if action == 'state':
            state = self.state()
            with self.connect() as c:
                intervals = [dict(r) for r in c.execute('SELECT * FROM intervals ORDER BY stream,label')]
                events = [dict(r) for r in c.execute('SELECT * FROM demo_events ORDER BY id DESC LIMIT 25')]
                pending = c.execute("SELECT count(*) FROM identities x JOIN items i ON i.id=x.item WHERE i.online=1 AND i.sku='' AND x.role!='photo'").fetchone()[0]
            for interval in intervals:
                interval['members'] = json.loads(interval['members'])
            return {**state, 'demo': 'archive-v2', 'data': str(self.data), 'job': dict(self.job),
                    'intervals': intervals, 'events': events, 'pending_labels': pending, 'tree': self.tree(),
                    'skus': self.sku_groups()['rows']}
        if action == 'items':
            clauses, args = ['i.online=1'], []
            if query.get('root'):
                clauses.append('i.root=?'); args.append(query['root'])
            if query.get('folder'):
                clauses.append("(i.folder=? OR i.folder LIKE ? ESCAPE '!')")
                escaped = query['folder'].replace('!', '!!').replace('%', '!%').replace('_', '!_')
                args.extend([query['folder'], escaped.rstrip('/') + '/%'])
            if query.get('q'):
                clauses.append('i.normalized LIKE ?'); args.append('%' + normalize(query['q']) + '%')
            if query.get('exact'):
                clauses.append('i.normalized=?'); args.append(normalize(query['exact']))
            if query.get('filter') == 'review':
                clauses.append("x.role!='photo' AND i.source!='manual'")
            if query.get('filter') == 'unassigned':
                clauses.append("i.sku=''")
            where = ' WHERE ' + ' AND '.join(clauses)
            joined = ' FROM items i JOIN identities x ON x.item=i.id'
            with self.connect() as c:
                total = c.execute('SELECT count(*)' + joined + where, args).fetchone()[0]
                if query.get('cursor'):
                    try:
                        marker = json.loads(base64.urlsafe_b64decode(query['cursor']).decode())
                        where += ' AND (i.name>? OR (i.name=? AND i.id>?))'
                        args.extend([marker[0], marker[0], marker[1]])
                    except Exception:
                        raise ValueError('分页标记无效') from None
                rows = c.execute('SELECT i.*' + joined + where + ' ORDER BY i.name,i.id LIMIT 241', args).fetchall()
            more = len(rows) > 240
            rows = rows[:240]
            cursor = base64.urlsafe_b64encode(_json([rows[-1]['name'], rows[-1]['id']]).encode()).decode() if rows else ''
            return {'rows': [self.enrich(row) for row in rows], 'has_more': more, 'next_cursor': cursor, 'total': total}
        if action == 'item':
            return self.enrich(self.item(query['id']))
        raise ValueError('未知演示查询')

    def post(self, action, body):
        if action == 'cancel':
            self.cancel.set()
            return {'ok': True, 'message': '当前图片完成后停止；已完成的 OCR 保留缓存'}
        with self.gate:
            if self.job['status'] in ('running', 'indexing'):
                raise ValueError('正在解析，请完成或停止后修改归档')
            if action == 'scan':
                ids = [r['id'] for r in self.state()['roots']]
                if not ids:
                    raise ValueError('请先添加文件夹')
                self.cancel.clear()
                self.job = {'status': 'indexing', 'done': 0, 'total': 0, 'current': '检查归档位置', 'errors': 0}
                threading.Thread(target=self._run, args=(ids,), daemon=True).start()
                return dict(self.job)
            if action == 'add':
                return self.add_root(body['folder'])
            if action == 'assign':
                return self.assign_review(body)
            if action == 'boundary':
                row = self.item(body['id'])
                self._identity(row)
                with self.connect() as c:
                    c.execute("UPDATE identities SET role='label',evidence=? WHERE item=?", (_json({'codes': [body.get('sku', '')], 'text': '用户标记结束标签', 'manual_boundary': True}), row['id']))
                self._intervals()
                return self.enrich(row)
            if action == 'write':
                return self.write_metadata(body['id'])
        raise ValueError('未知演示操作')

    def _image(self, row):
        path = Path(row['path'])
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image).convert('RGB')
                image.thumbnail((1600, 1600))
                output = BytesIO()
                image.save(output, 'JPEG', quality=92)
                return output.getvalue()
        except (OSError, ValueError):
            # 复用正式 RAW 内嵌预览读取，不把 RAW 二进制直接送模型。
            import media_engine as media
            record = {'path': str(path), 'primary': str(path), 'raw': str(path), 'jpeg': None, 'fingerprint': row['fingerprint']}
            return Path(media.preview(record, self.writer.root, self.previews, media.find_exiftool(self.writer.root), size=1600)).read_bytes()

    @staticmethod
    def codes(result):
        values = result.get('codes', []) or extract_codes(result.get('text', ''))
        # 不把米数、尺码、年份当作款号；纯数字款号留在全文供人工指定。
        return list(dict.fromkeys(str(v).strip() for v in values if re.search('[A-Za-z]', str(v)) and re.search('[0-9]', str(v)) and 4 <= len(str(v)) <= 40))

    def _recognize_image(self, image):
        """GLM 使用 OCR 原生任务，避免让无文字照片补全 JSON 模板。"""
        from local_inference import resolve
        with resolve(PROVIDER) as provider:
            body = {'model': provider['model'], 'temperature': 0, 'max_tokens': 512,
                    'messages': [{'role': 'user', 'content': [
                        {'type': 'text', 'text': 'Text Recognition:'},
                        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(image).decode()}}
                    ]}]}
            request = urllib.request.Request(provider['url'] + '/v1/chat/completions', json.dumps(body).encode(),
                        {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + provider['key']})
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = json.load(response)
        text = raw['choices'][0]['message']['content']
        return {'text': str(text), 'codes': self.codes({'text': str(text)}), 'unscored': True,
                'model': 'glm-ocr-local', 'engine': 'local|glm-ocr-local', 'prompt_version': 'text-recognition-v2'}

    def _run(self, roots):
        try:
            for root in roots:
                if self.cancel.is_set():
                    break
                self.scan(root)
            with self.connect() as c:
                rows = [dict(r) for r in c.execute('SELECT * FROM items WHERE online=1 ORDER BY root,folder,name COLLATE NOCASE')]
            self.job.update(status='running', total=len(rows))
            for row in rows:
                if self.cancel.is_set():
                    break
                self.job['current'] = row['name']
                digest = ''
                with self.connect() as c:
                    old = c.execute('SELECT * FROM identities WHERE item=?', (row['id'],)).fetchone()
                cached = None
                try:
                    digest = sha(row['path'])
                    with self.connect() as c:
                        cached = c.execute('SELECT result FROM ocr_cache WHERE hash=?', ('glm-text-v2:' + digest,)).fetchone()
                    metadata = self.writer.read(row['path'], full=True)
                    embedded_asset, embedded_file = metadata.get('XMP-fk:AssetId'), metadata.get('XMP-fk:FileId')
                    if embedded_asset and embedded_file:
                        try:
                            a, f = str(uuid.UUID(str(embedded_asset))), str(uuid.UUID(str(embedded_file)))
                            with self.connect() as c:
                                c.execute('UPDATE identities SET asset_id=?,file_id=? WHERE item=?', (a, f, row['id']))
                        except ValueError:
                            pass
                    camera = str(metadata.get('ExifIFD:BodySerialNumber') or metadata.get('Canon:SerialNumber') or metadata.get('IFD0:Model') or 'unknown-camera')
                    stamp = metadata.get('ExifIFD:DateTimeOriginal')
                    # 样片为不同实拍图组成的流程演示，文件名明确指定示例顺序，不冒充真实连续拍摄。
                    is_sample = Path(row['path']).resolve().is_relative_to(self.base / 'samples')
                    ordering = '示例文件名顺序，需核对' if is_sample or not stamp else 'EXIF 拍摄时间，区间需复核'
                    order = row['name'] if is_sample or not stamp else str(stamp) + ':' + str(metadata.get('ExifIFD:SubSecTimeOriginal', '')) + ':' + row['name']
                    stream = f"{row['root']}|{row['folder']}|{'demo' if is_sample else camera}"
                    embedded = metadata.get('XMP-fk:SKU', '')
                    if embedded and row['source'] != 'manual':
                        super().assign(row['id'], embedded)
                    if old and json.loads(old['evidence']).get('manual_boundary'):
                        result = json.loads(old['evidence'])
                    elif cached:
                        result = json.loads(cached[0])
                    else:
                        result = self._recognize_image(self._image(row))
                        result['codes'] = self.codes(result)
                        with self.connect() as c:
                            c.execute('INSERT OR REPLACE INTO ocr_cache VALUES (?,?)', ('glm-text-v2:' + digest, _json(result)))
                    codes = self.codes(result)
                    role = 'label' if codes or result.get('manual_boundary') else 'photo'
                    result['codes'] = codes
                    if not codes and re.search(r'(?i)sku|款号|貨號|货号', result.get('text', '')):
                        role = 'unclear_label'
                    with self.connect() as c:
                        c.execute('UPDATE identities SET content_hash=?,evidence=?,stream=?,order_key=?,ordering=?,role=? WHERE item=?',
                                  (digest, _json(result), stream, order, ordering, role, row['id']))
                    self.event('ocr', {'file': row['name'], 'codes': codes, 'cached': bool(cached), 'role': role})
                except Exception as error:
                    self.job['errors'] += 1
                    with self.connect() as c:
                        c.execute("UPDATE identities SET role='error',stream=?,order_key=?,evidence=? WHERE item=?", (f"{row['root']}|{row['folder']}|blocked", row['name'], _json({'error': str(error)[:400]}), row['id']))
                    self.event('error', {'file': row['name'], 'error': str(error)[:400]})
                self.job['done'] += 1
            # 只有完整扫描后生成区间，取消或失败都不留下半个流的错误归属。
            if not self.cancel.is_set():
                self._intervals()
            self.job.update(status='cancelled' if self.cancel.is_set() else 'completed', current='')
        except Exception as error:
            self.job.update(status='failed', current=str(error)[:400])
        self.event('scan', dict(self.job))

    def _intervals(self):
        with self.connect() as c:
            rows = [dict(r) for r in c.execute('SELECT i.*,x.stream,x.order_key,x.ordering,x.role,x.evidence FROM items i JOIN identities x ON x.item=i.id WHERE i.online=1 ORDER BY x.stream,x.order_key,i.name')]
        streams = defaultdict(list)
        # 有一张无法解析时该目录的自动区间整体不生成，避免跳过失败标签吞并前款。
        blocked_folders = {(r['root'], r['folder']) for r in rows if r['role'] == 'error'}
        for row in rows:
            if (row['root'], row['folder']) in blocked_folders:
                continue
            streams[row['stream'] or row['root'] + '|' + row['folder']].append(row)
        with self.connect() as c:
            c.execute("DELETE FROM intervals WHERE label IN (SELECT i.id FROM items i JOIN identities x ON x.item=i.id WHERE i.online=1 AND x.role='photo') AND status!='confirmed'")
            for stream, photos in streams.items():
                members = []
                for row in photos:
                    members.append(row['id'])
                    if row['role'] == 'photo':
                        continue
                    result = json.loads(row['evidence'])
                    codes = result.get('codes', [])
                    sku = codes[0] if len(codes) == 1 else ''
                    existing = c.execute('SELECT * FROM intervals WHERE label=?', (row['id'],)).fetchone()
                    if existing and existing['status'] == 'confirmed':
                        # 已确认区间成员冻结；新出现／迟到文件必须重审，不静默吸收。
                        if set(json.loads(existing['members'])) != set(members):
                            c.execute('INSERT INTO demo_events(created,kind,detail) VALUES (?,?,?)', (time.time(), 'interval-change', _json({'previous_members': json.loads(existing['members']), 'proposed_members': members, 'label': row['id']})))
                            c.execute("UPDATE intervals SET status='changed',members=? WHERE id=?", (_json(members), existing['id']))
                    else:
                        c.execute('INSERT OR REPLACE INTO intervals VALUES (?,?,?,?,?,?,?)',
                                  (row['id'], row['id'], sku, _json(members), 'pending', row['ordering'] or '文件名顺序，需核对', stream))
                    members = []

    def assign_review(self, body):
        sku = str(body.get('sku', '')).strip()
        if not normalize(sku) or len(sku) > 80:
            raise ValueError('请填写有效款号')
        row = self.item(body['id'])
        members = [row['id']]
        interval = None
        with self.connect() as c:
            if body.get('scope') == 'interval':
                interval = c.execute('SELECT * FROM intervals WHERE label=?', (row['id'],)).fetchone()
                if not interval:
                    raise ValueError('区间不存在，请先核对边界')
                if interval['status'] == 'changed' and not body.get('overwrite'):
                    raise ValueError('区间成员发生变化，请核对全部成员并明确勾选覆盖')
                members = json.loads(interval['members'])
            before = [dict(r) for r in c.execute('SELECT id,sku,source FROM items WHERE online=1') if r['id'] in set(members)]
            conflicts = [r for r in before if r['source'] == 'manual' and r['sku'] and normalize(r['sku']) != normalize(sku)]
            if conflicts and not body.get('overwrite'):
                raise ValueError(f'有 {len(conflicts)} 张存在其他已确认款号，请勾选覆盖后再提交')
            for item in before:
                c.execute("UPDATE items SET sku=?,normalized=?,source='manual',confidence=1 WHERE id=?", (sku, normalize(sku), item['id']))
                c.execute('INSERT INTO corrections VALUES (?,?,?,?,?,?)', (uuid.uuid4().hex, item['id'], item['sku'], sku, 'human-review', time.time()))
            if interval:
                c.execute("UPDATE intervals SET sku=?,status='confirmed' WHERE id=?", (sku, interval['id']))
        self.event('review', {'sku': sku, 'members': members, 'scope': body.get('scope', 'photo')})
        return {'count': len(members), 'item': self.enrich(self.item(row['id']))}

    def write_metadata(self, item_id):
        row = self.enrich(self.item(item_id))
        if not row['writable']:
            raise ValueError('此演示只对自有样片开放文件写入；外部归档保持引用，可保存数据库校正')
        if not row['sku'] or row['source'] != 'manual':
            raise ValueError('请先确认正确款号')
        fields = {'AssetId': row['asset_id'], 'FileId': row['file_id'], 'SKU': row['sku'],
                  'ShootType': '归档演示', 'ProjectId': 'archive-v2', 'SegmentId': '', 'MetadataRevision': str(uuid.uuid4())}
        temp, target, journal = self.writer.write(Path(row['path']), fields, sha(row['path']))
        try:
            with self.writer.exclusive(target):
                if sha(target) != journal['before_hash']:
                    raise ValueError('文件已变化，未覆盖')
                temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
            temp.parent.rmdir()
        readback = self.writer.read(target)
        if readback.get('XMP-fk:SKU') != row['sku']:
            raise ValueError('文件已写，最终读回失败，请检查文件')
        with self.connect() as c:
            c.execute('UPDATE identities SET write_status=?,written_sku=?,content_hash=? WHERE item=?', ('已写入并读回', row['sku'], sha(target), item_id))
        self.event('metadata', {'item': item_id, 'sku': row['sku'], 'asset_id': row['asset_id'], 'journal': journal})
        return {'ok': True, 'item': self.enrich(self.item(item_id))}

    def preview(self, item_id, size=420):
        row = self.item(item_id)
        source = Path(row['path'])
        if source.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'}:
            size = max(160, min(2000, int(size)))
            target = self.previews / f"{row['fingerprint']}_{size}_oriented.jpg"
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(source) as image:
                    image = ImageOps.exif_transpose(image).convert('RGB')
                    image.thumbnail((size, size))
                    temp = target.with_name(uuid.uuid4().hex + '.jpg')
                    image.save(temp, quality=90)
                    temp.replace(target)
            return target
        import media_engine as media
        record = {'path': str(source), 'primary': str(source), 'raw': str(source), 'jpeg': None, 'fingerprint': row['fingerprint']}
        return media.preview(record, self.writer.root, self.previews, media.find_exiftool(self.writer.root), size=int(size))
