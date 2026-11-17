"""持久 HTTP 中继、因果事件和真实业务库投影；所有写入限定演示沙箱。"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import io
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler
import uuid
from zoneinfo import ZoneInfo
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '选片助手'))
from catalog_store import Store
from asset_archive import AssetArchive
from model_providers import Providers
from sku_service import SkuService
from sku_match import normalize
from metadata_writer import sha
import media_engine as media
from raw_reconcile import identity as capture_identity, compare as compare_capture

IDENTITY_FIELDS = {'model', 'serial', 'time', 'sub', 'precision', 'stem', 'number', 'unique'}


def visual_fingerprint(path):
    """跨格式的保守视觉复核；感知指纹不是唯一身份，也不能单独授权写入。"""
    path = Path(path)
    try:
        if path.suffix.lower() == '.cr3':
            import rawpy
            with rawpy.imread(str(path)) as raw:
                thumb = raw.extract_thumb()
            image = Image.open(io.BytesIO(thumb.data)) if isinstance(thumb.data, bytes) else Image.fromarray(thumb.data)
        else:
            image = Image.open(path)
        with image:
            oriented = ImageOps.exif_transpose(image)
            aspect = round(oriented.width / oriented.height * 1000)
            values = oriented.convert('L').resize((17, 16), Image.Resampling.LANCZOS).tobytes()
            bits = ''.join('1' if values[y * 17 + x] > values[y * 17 + x + 1] else '0' for y in range(16) for x in range(16))
            return dict(algorithm='dhash256-v1', value=f'{int(bits, 2):064x}', aspect=aspect)
    except Exception:
        return dict(algorithm='unavailable', value='', aspect=0)


def visual_match(raw, jpg):
    if raw['algorithm'] != 'dhash256-v1' or jpg['algorithm'] != 'dhash256-v1':
        return dict(accepted=False, reason='缺少可验证的图像预览', distance=None)
    distance = (int(raw['value'], 16) ^ int(jpg['value'], 16)).bit_count()
    aspect_equal = abs(raw['aspect'] - jpg['aspect']) <= max(1, jpg['aspect'] * .02)
    return dict(accepted=distance <= 8 and aspect_equal, distance=distance, threshold=8,
                reason='视觉复核通过' if distance <= 8 and aspect_equal else '图像内容或画幅不符，禁止自动绑定')


def validate_visual(value):
    if not isinstance(value, dict) or set(value) != {'algorithm', 'value', 'aspect'} or type(value['aspect']) is not int:
        raise ValueError('视觉指纹字段不兼容')
    if value['algorithm'] == 'unavailable' and value['value'] == '' and value['aspect'] == 0:
        return
    if value['algorithm'] != 'dhash256-v1' or len(value['value']) != 64 or any(c not in '0123456789abcdef' for c in value['value']) or not 1 <= value['aspect'] <= 20000:
        raise ValueError('视觉指纹内容无效')


def read_capture_identity(path):
    path = Path(path)
    metadata = media.read_metadata([path], media.find_exiftool(ROOT))[0]
    return capture_identity(media.capture_record(path.parent, path, None, metadata))


def strong_raw_match(raw_identity, jpg_identity):
    match = compare_capture({'_identity': raw_identity}, {'_identity': jpg_identity})
    if not match:
        return None
    # 多机/同型号相机下，缺机身序列号时不采用仅子秒＋文件名的自动规则。
    match['certain'] = match['certain'] and (match['evidence']['serial_equal'] or match['evidence']['unique_id_equal'])
    return match


def validate_identity(value):
    if not isinstance(value, dict) or set(value) != IDENTITY_FIELDS or type(value['precision']) is not int or not 0 <= value['precision'] <= 12:
        raise ValueError('拍摄身份字段不兼容')
    if any(not isinstance(value[k], str) or len(value[k]) > 200 for k in IDENTITY_FIELDS - {'precision'}):
        raise ValueError('拍摄身份字段无效')


def validate_component_change(previous, current, can_bind):
    for key in ('entity', 'origin', 'captured_at', 'capture_identity', 'visual_fingerprint'):
        if previous[key] != current[key]:
            raise ValueError('不可修改证据来源、时间和相机身份')
    old = {p['file_id']: p for p in previous['components']}
    new = {p['file_id']: p for p in current['components']}
    if any(new.get(key) != value for key, value in old.items()):
        raise ValueError('不可删除或改写已有文件组件身份')
    for key in set(new) - set(old):
        component = new[key]
        match = strong_raw_match(component['capture_identity'], current['capture_identity'])
        if not can_bind or component['extension'] != '.cr3' or not match or not match['certain'] or not visual_match(component['visual_fingerprint'], current['visual_fingerprint'])['accepted']:
            raise ValueError('只有归档角色可基于可靠拍摄身份追加 RAW 绑定')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def stable(namespace, value):
    return uuid.uuid5(uuid.UUID(namespace), value).hex


@contextmanager
def connect(path):
    c = sqlite3.connect(path, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def contained(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('路径不在本次演示授权目录内')
    return path


def validate_event(event):
    if set(event) != {'v', 'id', 'device', 'seq', 'workspace', 'project', 'parents', 'data', 'digest', 'signature'}:
        raise ValueError('事件字段不兼容')
    if event['v'] != 1 or type(event['seq']) is not int or event['seq'] < 1:
        raise ValueError('事件版本或序号无效')
    for key in ('device', 'workspace', 'project'):
        if uuid.UUID(event[key]).hex != event[key]:
            raise ValueError('身份须为标准 UUID hex')
    if event['id'] != f"{event['device']}:{event['seq']}":
        raise ValueError('事件身份与设备序号不一致')
    if not isinstance(event['parents'], list) or len(event['parents']) > 32 or len(set(event['parents'])) != len(event['parents']):
        raise ValueError('父版本无效')
    if any(not isinstance(p, str) or len(p) > 64 or p == event['id'] for p in event['parents']):
        raise ValueError('父版本无效')
    data = event['data']
    if set(data) != {'entity', 'origin', 'captured_at', 'active', 'metadata', 'components', 'capture_identity', 'visual_fingerprint'}:
        raise ValueError('拍摄字段不兼容')
    validate_identity(data['capture_identity'])
    validate_visual(data['visual_fingerprint'])
    for key in ('entity', 'origin'):
        if uuid.UUID(data[key]).hex != data[key]:
            raise ValueError('拍摄身份无效')
    stamp = datetime.fromisoformat(data['captured_at'])
    if stamp.tzinfo is None or stamp.utcoffset() is None or type(data['active']) is not bool:
        raise ValueError('拍摄时间必须带时区，状态须为布尔值')
    meta = data['metadata']
    if set(meta) != {'sku', 'shoot_type', 'rating', 'ocr_text', 'description'}:
        raise ValueError('元数据字段不兼容')
    if type(meta['rating']) is not int or meta['rating'] not in range(6):
        raise ValueError('星级无效')
    for key, limit in (('sku', 80), ('shoot_type', 80), ('ocr_text', 16000), ('description', 4000)):
        if not isinstance(meta[key], str) or len(meta[key]) > limit:
            raise ValueError('元数据长度无效')
    if not normalize(meta['sku']) or not meta['shoot_type'].strip():
        raise ValueError('款号和拍摄类型不能为空')
    if any('\n' in meta[k] or '\r' in meta[k] for k in ('sku', 'shoot_type')):
        raise ValueError('款号和拍摄类型不能包含换行')
    if not isinstance(data['components'], list) or not 1 <= len(data['components']) <= 4:
        raise ValueError('组件数量无效')
    ids = set()
    for component in data['components']:
        if set(component) != {'file_id', 'sha256', 'extension', 'filename', 'capture_identity', 'visual_fingerprint'}:
            raise ValueError('组件字段不兼容')
        validate_identity(component['capture_identity'])
        validate_visual(component['visual_fingerprint'])
        if uuid.UUID(component['file_id']).hex != component['file_id'] or component['file_id'] in ids:
            raise ValueError('组件身份无效')
        ids.add(component['file_id'])
        if len(component['sha256']) != 64 or any(x not in '0123456789abcdef' for x in component['sha256']):
            raise ValueError('内容指纹无效')
        if component['extension'] not in ('.jpg', '.cr3') or not isinstance(component['filename'], str) or len(component['filename']) > 255:
            raise ValueError('演示只支持 JPG 和 CR3 组件')
        if '/' in component['filename'] or '\\' in component['filename'] or ':' in component['filename']:
            raise ValueError('事件不能携带文件路径')
    unsigned = {k: v for k, v in event.items() if k not in ('digest', 'signature')}
    if hashlib.sha256(canonical(unsigned).encode()).hexdigest() != event['digest']:
        raise ValueError('事件摘要错误')


def signed_event(identity, seq, workspace, project, data, parents):
    event = dict(v=1, id=f"{identity['id']}:{seq}", device=identity['id'], seq=seq,
                 workspace=workspace, project=project, parents=sorted(parents), data=data)
    event['digest'] = hashlib.sha256(canonical(event).encode()).hexdigest()
    event['signature'] = hmac.new(identity['secret'].encode(), event['digest'].encode(), hashlib.sha256).hexdigest()
    return event


class Relay:
    def __init__(self, path, workspace, project, identities):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace, self.project, self.identities = workspace, project, identities
        if len({i['alias'] for i in identities.values()}) != len(identities):
            raise ValueError('设备显示别名必须唯一')
        self.revoked = set()
        self.lock = threading.RLock()
        with connect(self.path) as c:
            c.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events(offset INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,
                    device TEXT,seq INTEGER,entity TEXT,digest TEXT,payload TEXT,UNIQUE(device,seq));
                CREATE TABLE IF NOT EXISTS device_seq(device TEXT PRIMARY KEY,seq INTEGER);''')
            c.execute('CREATE TABLE IF NOT EXISTS file_bindings(file_id TEXT PRIMARY KEY,entity TEXT,sha TEXT UNIQUE)')

    def authorize(self, token):
        for identity in self.identities.values():
            if hmac.compare_digest(token, 'Bearer ' + identity['token']) and identity['id'] not in self.revoked:
                return identity
        raise PermissionError('设备凭据无效或已吊销')

    def push(self, identity, events):
        if not isinstance(events, list) or not 1 <= len(events) <= 100:
            raise ValueError('每批事件须为 1–100 条')
        receipts = []
        with self.lock, connect(self.path) as c:
            c.execute('BEGIN IMMEDIATE')
            for event in events:
                validate_event(event)
                if event['device'] != identity['id'] or event['workspace'] != self.workspace or event['project'] != self.project:
                    raise PermissionError('设备或共享项目越权')
                expected = hmac.new(identity['secret'].encode(), event['digest'].encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, event['signature']):
                    raise PermissionError('设备签名无效')
                old = c.execute('SELECT offset,digest FROM events WHERE id=?', (event['id'],)).fetchone()
                if old:
                    if old['digest'] != event['digest']:
                        raise ValueError('相同事件编号的内容不同')
                    receipts.append({'id': event['id'], 'offset': old['offset'], 'duplicate': True})
                    continue
                previous = c.execute('SELECT seq FROM device_seq WHERE device=?', (event['device'],)).fetchone()
                if event['seq'] != (previous['seq'] if previous else 0) + 1:
                    raise ValueError('设备序号不连续')
                data = event['data']
                if not event['parents']:
                    if data['origin'] != event['device'] or c.execute('SELECT 1 FROM events WHERE entity=?', (data['entity'],)).fetchone():
                        raise ValueError('拍摄初始身份重复或来源错误')
                    if any(p['extension'] != '.jpg' for p in data['components']) and not identity.get('archive_writer'):
                        raise ValueError('现场角色的初始事件只携带 JPG')
                for parent in event['parents']:
                    row = c.execute('SELECT payload FROM events WHERE id=?', (parent,)).fetchone()
                    if not row:
                        raise ValueError('父事件尚未到达中继')
                    prior = json.loads(row['payload'])['data']
                    validate_component_change(prior, data, identity.get('archive_writer', False))
                for component in data['components']:
                    binding = c.execute('SELECT * FROM file_bindings WHERE file_id=? OR sha=?', (component['file_id'], component['sha256'])).fetchone()
                    if binding and (binding['entity'] != data['entity'] or binding['file_id'] != component['file_id'] or binding['sha'] != component['sha256']):
                        raise ValueError('文件身份或内容指纹已属于其他资产')
                    c.execute('INSERT OR IGNORE INTO file_bindings VALUES(?,?,?)', (component['file_id'], data['entity'], component['sha256']))
                offset = c.execute('INSERT INTO events(id,device,seq,entity,digest,payload) VALUES(?,?,?,?,?,?)',
                                   (event['id'], event['device'], event['seq'], data['entity'], event['digest'], canonical(event))).lastrowid
                c.execute('INSERT INTO device_seq VALUES(?,?) ON CONFLICT(device) DO UPDATE SET seq=excluded.seq',
                          (event['device'], event['seq']))
                receipts.append({'id': event['id'], 'offset': offset, 'duplicate': False})
        return {'receipts': receipts}

    def pull(self, after, limit=100):
        if after < 0 or not 1 <= limit <= 100:
            raise ValueError('游标或分页无效')
        with connect(self.path) as c:
            maximum = c.execute('SELECT COALESCE(MAX(offset),0) FROM events').fetchone()[0]
            if after > maximum:
                raise ValueError('游标超出中继日志')
            rows = c.execute('SELECT offset,payload FROM events WHERE offset>? ORDER BY offset LIMIT ?', (after, limit)).fetchall()
        return {'events': [dict(offset=r['offset'], event=json.loads(r['payload'])) for r in rows],
                'cursor': rows[-1]['offset'] if rows else after}


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200):
        raw = canonical(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_request(self, post=False):
        self.connection.settimeout(10)
        try:
            relay = self.server.relay
            identity = relay.authorize(self.headers.get('Authorization', ''))
            if self.headers.get('Origin'):
                raise PermissionError('同步接口不接受浏览器跨源请求')
            url = urlsplit(self.path)
            if url.path != '/sync/v1/events':
                return self.reply({'error': '接口不存在'}, 404)
            query = parse_qs(url.query, max_num_fields=5)
            if query.get('project', [relay.project])[0] != relay.project:
                raise PermissionError('共享项目越权')
            if post:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1_000_000:
                    raise ValueError('请求大小无效')
                body = json.loads(self.rfile.read(length))
                result = relay.push(identity, body['events'])
            else:
                result = relay.pull(int(query.get('after', ['0'])[0]), int(query.get('limit', ['100'])[0]))
            self.reply(result)
        except PermissionError as exc:
            self.reply({'error': str(exc)}, 403)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            self.reply({'error': str(exc)}, 400)

    def do_POST(self):
        self.handle_request(True)

    def do_GET(self):
        self.handle_request()


class RelayServer:
    def __init__(self, relay):
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), RelayHandler)
        self.http.daemon_threads = True
        self.http.relay = relay
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.http.server_port}'

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)


class SimulatedCrash(RuntimeError):
    pass


class Node:
    def __init__(self, directory, sandbox, identity, workspace, project, identities, plan, *, archive_root=None):
        self.sandbox = Path(sandbox).resolve()
        self.directory = contained(directory, self.sandbox)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.identity, self.workspace, self.project = identity, workspace, project
        self.identities, self.plan = identities, plan
        self.archive_root = contained(archive_root, self.sandbox) if archive_root else None
        if self.archive_root and not self.identity.get('archive_writer'):
            raise ValueError('本机未授予归档写入角色')
        if self.archive_root:
            self.archive_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.store = Store(self.directory, ROOT)
        self.archive = AssetArchive(self.store, autostart=False)
        self.sku = SkuService(self.store, Providers(self.directory), archive=self.archive, autostart=False)
        self.archive.sku = self.sku
        self.db = self.sku.db
        self.writer = self.archive.writer
        with connect(self.db) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS sync_events(id TEXT PRIMARY KEY,entity TEXT,payload TEXT,projected INTEGER DEFAULT 0,outbound INTEGER DEFAULT 0,acked INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sync_settings(key TEXT PRIMARY KEY,value INTEGER);
                INSERT OR IGNORE INTO sync_settings VALUES('seq',0);
                INSERT OR IGNORE INTO sync_settings VALUES('cursor',0);
                CREATE TABLE IF NOT EXISTS sync_entities(id TEXT PRIMARY KEY,data TEXT,heads TEXT,state TEXT,version TEXT,depth INTEGER);
                CREATE TABLE IF NOT EXISTS sync_files(path TEXT PRIMARY KEY,original_sha TEXT,last_sha TEXT,asset_id TEXT,file_id TEXT,capture_identity TEXT,visual_fingerprint TEXT);
                CREATE TABLE IF NOT EXISTS sync_writes(entity TEXT,file_id TEXT,version TEXT,state TEXT,reason TEXT,path TEXT DEFAULT '',journal TEXT DEFAULT '{}',PRIMARY KEY(entity,file_id,version));
                CREATE TABLE IF NOT EXISTS sync_archive_projection(entity TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS sync_search_projection(entity TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS sync_export_queue(project TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS sync_entity_keys(entity TEXT PRIMARY KEY,normalized TEXT,scope TEXT,day TEXT,origin TEXT,sku TEXT,active INTEGER,captured TEXT,conflict INTEGER);
                CREATE INDEX IF NOT EXISTS sync_keys_sku ON sync_entity_keys(normalized,scope,active,entity);
                CREATE INDEX IF NOT EXISTS sync_keys_device_day ON sync_entity_keys(origin,day,normalized,scope,active,entity);
                CREATE INDEX IF NOT EXISTS sync_event_entity ON sync_events(entity,projected);
                CREATE INDEX IF NOT EXISTS sync_event_pending ON sync_events(projected);
                CREATE INDEX IF NOT EXISTS sync_outbox_pending ON sync_events(outbound,acked);
                CREATE INDEX IF NOT EXISTS sync_write_state ON sync_writes(state);
                CREATE TABLE IF NOT EXISTS sync_raw_pairs(entity TEXT PRIMARY KEY,state TEXT,reason TEXT,evidence TEXT);
                CREATE TABLE IF NOT EXISTS sync_binding(slot INTEGER PRIMARY KEY,workspace TEXT,project TEXT,device TEXT);
            ''')
            binding = c.execute('SELECT * FROM sync_binding WHERE slot=1').fetchone()
            if binding and (binding['workspace'], binding['project'], binding['device']) != (workspace, project, identity['id']):
                raise ValueError('已有演示数据库不能改绑设备或共享项目')
            c.execute('INSERT OR IGNORE INTO sync_binding VALUES(1,?,?,?)', (workspace, project, identity['id']))
        from search_index import SearchIndex
        self.search_index = SearchIndex(self.directory / 'collaboration_search.sqlite3')
        self._drain()
        self._materialize()

    def close(self):
        self.sku.close()
        self.archive.close()

    def heads(self, entity):
        with connect(self.db) as c:
            row = c.execute('SELECT heads FROM sync_entities WHERE id=?', (entity,)).fetchone()
        return json.loads(row['heads']) if row else []

    def emit(self, data, *, parents=None):
        with self.lock, connect(self.db) as c:
            c.execute('BEGIN IMMEDIATE')
            seq = c.execute("SELECT value FROM sync_settings WHERE key='seq'").fetchone()[0] + 1
            event = signed_event(self.identity, seq, self.workspace, self.project, data,
                                 self.heads(data['entity']) if parents is None else parents)
            validate_event(event)
            if data['origin'] not in self.identities:
                raise ValueError('未知拍摄来源设备')
            if not event['parents']:
                if data['origin'] != self.identity['id'] or c.execute('SELECT 1 FROM sync_entities WHERE id=?', (data['entity'],)).fetchone():
                    raise ValueError('初始证据必须由来源设备创建且不得重复')
                if any(p['extension'] != '.jpg' for p in data['components']) and not self.identity.get('archive_writer'):
                    raise ValueError('现场角色的初始事件只携带 JPG')
            for parent in event['parents']:
                prior = c.execute('SELECT payload,projected FROM sync_events WHERE id=?', (parent,)).fetchone()
                if not prior or not prior['projected']:
                    raise ValueError('本地缺少已确认父版本')
                previous = json.loads(prior['payload'])['data']
                validate_component_change(previous, data, self.identity.get('archive_writer', False))
            c.execute('INSERT INTO sync_events(id,entity,payload,outbound) VALUES(?,?,?,1)', (event['id'], data['entity'], canonical(event)))
            c.execute("UPDATE sync_settings SET value=? WHERE key='seq'", (seq,))
        self._drain()
        self._materialize()
        return event

    def receive(self, batch):
        with self.lock, connect(self.db) as c:
            c.execute('BEGIN IMMEDIATE')
            current = c.execute("SELECT value FROM sync_settings WHERE key='cursor'").fetchone()[0]
            if batch['cursor'] < current:
                raise ValueError('下行游标不能倒退')
            for item in batch['events']:
                event = item['event']
                validate_event(event)
                if event['workspace'] != self.workspace or event['project'] != self.project or event['device'] not in self.identities:
                    raise ValueError('接收事件越权')
                old = c.execute('SELECT payload FROM sync_events WHERE id=?', (event['id'],)).fetchone()
                if old and json.loads(old['payload'])['digest'] != event['digest']:
                    raise ValueError('接收事件发生身份冲突')
                c.execute('INSERT OR IGNORE INTO sync_events(id,entity,payload) VALUES(?,?,?)', (event['id'], event['data']['entity'], canonical(event)))
            c.execute("UPDATE sync_settings SET value=? WHERE key='cursor'", (batch['cursor'],))
        self._drain()
        self._materialize()

    def _drain(self):
        with self.lock, connect(self.db) as c:
            c.execute('BEGIN IMMEDIATE')
            affected = set()
            while True:
                made_progress = False
                for row in c.execute('SELECT id,entity,payload FROM sync_events WHERE projected=0 ORDER BY rowid').fetchall():
                    event = json.loads(row['payload'])
                    parents = [c.execute('SELECT entity,projected FROM sync_events WHERE id=?', (p,)).fetchone() for p in event['parents']]
                    if any(not p or not p['projected'] for p in parents):
                        continue
                    if any(p['entity'] != row['entity'] for p in parents):
                        raise ValueError('父版本属于其他拍摄证据')
                    c.execute('UPDATE sync_events SET projected=1 WHERE id=?', (row['id'],))
                    affected.update(self._entity(c, row['entity']))
                    made_progress = True
                if not made_progress:
                    break
            initial = not c.execute('SELECT 1 FROM projects WHERE id=?', (stable(self.project, 'all-shoots'),)).fetchone()
            if initial:
                affected.update((normalize(m['sku']), m['shoot_type'], '', '', m['sku']) for m in self.plan)
            self._progress(c, affected)

    def _entity(self, c, entity):
        old = c.execute('SELECT normalized,scope,day,origin,sku FROM sync_entity_keys WHERE entity=?', (entity,)).fetchone()
        events = {r['id']: json.loads(r['payload']) for r in c.execute('SELECT id,payload FROM sync_events WHERE entity=? AND projected=1', (entity,))}
        used = {p for e in events.values() for p in e['parents']}
        heads = sorted(set(events) - used)
        depths = {}

        def ancestors(key):
            result = {key}
            for parent in events[key]['parents']:
                result |= ancestors(parent)
            return result

        def depth(key):
            if key not in depths:
                depths[key] = 1 + max([depth(p) for p in events[key]['parents']] or [0])
            return depths[key]

        conflict = len({canonical(events[key]['data']) for key in heads}) > 1
        choices = set.intersection(*(ancestors(key) for key in heads)) if conflict else set(heads)
        selected = max(choices, key=lambda key: (depth(key), key))
        data = events[selected]['data']
        c.execute('''INSERT INTO sync_entities VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            data=excluded.data,heads=excluded.heads,state=excluded.state,version=excluded.version,depth=excluded.depth''',
                  (entity, canonical(data), canonical(heads), 'conflict' if conflict else 'ready', selected, depth(selected)))
        c.execute('INSERT OR IGNORE INTO sync_archive_projection VALUES(?)', (entity,))
        c.execute('INSERT OR IGNORE INTO sync_search_projection VALUES(?)', (entity,))
        key = (normalize(data['metadata']['sku']), data['metadata']['shoot_type'],
               datetime.fromisoformat(data['captured_at']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),
               data['origin'], data['metadata']['sku'])
        c.execute('''INSERT INTO sync_entity_keys VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(entity) DO UPDATE SET
            normalized=excluded.normalized,scope=excluded.scope,day=excluded.day,origin=excluded.origin,sku=excluded.sku,
            active=excluded.active,captured=excluded.captured,conflict=excluded.conflict''',
                  (entity, *key, int(data['active']), data['captured_at'], int(conflict)))
        return {key, tuple(old) if old else key}

    def _progress(self, c, affected):
        all_id = stable(self.project, 'all-shoots')
        planned = {(normalize(m['sku']), m['shoot_type']) for m in self.plan}
        for normalized, scope, day, origin, sku in sorted(affected):
            groups = [(all_id, '全部拍摄', '', '')]
            if origin:
                groups.append((stable(self.project, f'daily:{day}:{origin}'), f"{day}-{self.identities[origin]['alias']}.xlsx", day, origin))
            for pid, name, group_day, device in groups:
                current = c.execute('SELECT metadata FROM projects WHERE id=?', (pid,)).fetchone()
                metadata = json.loads(current[0]) if current else {'sheets': [], 'progress_version': 1,
                    'collaboration': {'project': self.project, 'origin_device': device, 'day': group_day, 'view': 'all' if pid == all_id else 'daily'}}
                if scope not in {s['name'] for s in metadata['sheets']}:
                    metadata['sheets'].append({'name': scope, 'images': []})
                    metadata['sheets'].sort(key=lambda s: s['name'])
                c.execute('''INSERT INTO projects(id,name,metadata,session_id,scope,enabled,created) VALUES(?,?,?,NULL,?,0,0)
                    ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata''', (pid, name, canonical(metadata), scope))
                where, params = 'normalized=? AND scope=?', [normalized, scope]
                if device:
                    where += ' AND origin=? AND day=?'
                    params += [device, group_day]
                summary = c.execute('SELECT count(*) AS total,COALESCE(sum(active),0) AS shots,MIN(CASE WHEN active=1 THEN captured END) AS stamp,MAX(conflict) AS conflict FROM sync_entity_keys WHERE ' + where, params).fetchone()
                row_id = stable(pid, normalized + ':' + scope)
                if not summary['total'] and (device or (normalized, scope) not in planned):
                    c.execute('DELETE FROM sku_rows WHERE id=?', (row_id,))
                    continue
                sources = [self.identities[r[0]]['alias'] for r in c.execute('SELECT DISTINCT origin FROM sync_entity_keys WHERE ' + where + ' AND active=1', params)]
                evidence = [r[0] for r in c.execute('SELECT entity FROM sync_entity_keys WHERE ' + where + ' AND active=1 ORDER BY entity LIMIT 100', params)]
                values = dict(machines=sorted(sources), evidence=evidence, evidence_truncated=summary['shots'] > 100,
                              shot_count=summary['shots'], conflict=bool(summary['conflict']))
                old = c.execute('SELECT excel_row FROM sku_rows WHERE id=?', (row_id,)).fetchone()
                index = old[0] if old else c.execute('SELECT COALESCE(MAX(excel_row),1)+1 FROM sku_rows WHERE project=? AND sheet=?', (pid, scope)).fetchone()[0]
                c.execute('''INSERT INTO sku_rows(id,project,sheet,excel_row,sku,normalized,baseline,status,completed_at,source_values)
                    VALUES(?,?,?,?,?,?,'未拍摄',?,?,?) ON CONFLICT(id) DO UPDATE SET sku=excluded.sku,status=excluded.status,
                    completed_at=excluded.completed_at,source_values=excluded.source_values''',
                          (row_id, pid, scope, index, sku, normalized, '已拍摄' if summary['shots'] else '未拍摄', summary['stamp'] or '', canonical(values)))
                c.execute('INSERT OR IGNORE INTO sync_export_queue VALUES(?)', (pid,))

    def _materialize(self):
        with self.lock:
            return self._materialize_locked()

    def _materialize_locked(self):
        # 先发布轻量检索，Excel/文件匹配与写入不在查询路径内。
        with connect(self.db) as c:
            dirty = c.execute('SELECT e.* FROM sync_entities e JOIN sync_search_projection p ON p.entity=e.id LIMIT 1000').fetchall()
        if dirty:
            documents = []
            for row in dirty:
                data = json.loads(row['data'])
                meta = data['metadata']
                documents.append(dict(entity=row['id'], project=self.project, normalized=normalize(meta['sku']), sku=meta['sku'],
                    scope=meta['shoot_type'], origin=data['origin'], active=int(data['active']), conflict=int(row['state'] == 'conflict'),
                    version=row['version'], ocr_text=meta['ocr_text'], description=meta['description']))
            self.search_index.publish(documents)
            with connect(self.db) as c:
                c.executemany('DELETE FROM sync_search_projection WHERE entity=?', [(r['id'],) for r in dirty])
        # 归档库是可重建投影，不能把跨 WAL 数据库写入冒充断电原子事务。
        with connect(self.db) as c:
            pending = [r[0] for r in c.execute('SELECT entity FROM sync_archive_projection')]
        for entity in pending:
            with connect(self.db) as c:
                row = c.execute('SELECT * FROM sync_entities WHERE id=?', (entity,)).fetchone()
            data, meta = json.loads(row['data']), json.loads(row['data'])['metadata']
            with self.archive.connect() as c:
                c.execute('''INSERT INTO assets(id,created,captured,sku,normalized,project,scope,segment,source,locked,revision)
                    VALUES(?,0,?,?,?,?,?,?,'collaboration-demo',1,?) ON CONFLICT(id) DO UPDATE SET
                    sku=excluded.sku,normalized=excluded.normalized,scope=excluded.scope,revision=excluded.revision''',
                          (entity, data['captured_at'], meta['sku'], normalize(meta['sku']), self.project, meta['shoot_type'], entity, row['depth']))
                for component in data['components']:
                    c.execute('INSERT OR IGNORE INTO files(id,asset,component,sha) VALUES(?,?,?,?)',
                              (component['file_id'], entity, component['extension'][1:], component['sha256']))
            with connect(self.db) as c:
                c.execute('DELETE FROM sync_archive_projection WHERE entity=?', (entity,))
        # 工作簿是后台可重建投影，由演示调度器显式冲刷持久导出队列。

    def export_workbooks(self):
        from openpyxl import Workbook
        with connect(self.db) as c:
            projects = c.execute('SELECT p.* FROM projects p JOIN sync_export_queue q ON q.project=p.id').fetchall()
            for project in projects:
                folder = self.sku.root / project['id']
                folder.mkdir(exist_ok=True)
                book = Workbook()
                sheet = book.active
                sheet.title = '拍摄进度'
                sheet.append(['款号', '拍摄类型', '拍摄状态', '机器来源', '拍摄次数', '完成时间', '冲突', '共享项目'])
                for row in c.execute('SELECT * FROM sku_rows WHERE project=? ORDER BY sheet,excel_row', (project['id'],)):
                    source = json.loads(row['source_values'])
                    sheet.append([row['sku'], row['sheet'], row['status'], ','.join(source['machines']), source['shot_count'],
                                  row['completed_at'], '待复核' if source['conflict'] else '', self.project])
                sheet.freeze_panes = 'A2'
                sheet.auto_filter.ref = sheet.dimensions
                for column in 'ABCDEFGH':
                    sheet.column_dimensions[column].width = 23 if column != 'H' else 38
                target = folder / (project['name'] if project['name'].endswith('.xlsx') else project['name'] + '.xlsx')
                temporary = target.with_suffix('.pending.xlsx')
                book.save(temporary)
                temporary.replace(target)
                c.execute('DELETE FROM sync_export_queue WHERE project=?', (project['id'],))

    def search(self, query, **options):
        return self.search_index.search(self.project, query, **options)

    def request(self, url, *, events=None, token=None, query=None):
        parsed = urlsplit(url)
        local = parsed.hostname and ipaddress.ip_address(parsed.hostname).is_loopback if parsed.hostname in ('127.0.0.1', '::1') else False
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and local):
            raise ValueError('非回环同步地址必须使用 HTTPS')
        target = url.rstrip('/') + '/sync/v1/events' + ('?' + urlencode(query) if query else '')
        raw = canonical({'events': events}).encode() if events is not None else None
        req = Request(target, data=raw, headers={'Authorization': 'Bearer ' + (self.identity['token'] if token is None else token), 'Content-Type': 'application/json'})
        with build_opener(ProxyHandler({})).open(req, timeout=5) as response:
            return json.load(response)

    def sync(self, url, *, lose_ack=False, reverse=False, upload=True):
        with self.lock:
            if upload:
                while True:
                    with connect(self.db) as c:
                        pending = c.execute('SELECT id,payload FROM sync_events WHERE outbound=1 AND acked=0 ORDER BY rowid LIMIT 100').fetchall()
                    if not pending:
                        break
                    self.request(url, events=[json.loads(r['payload']) for r in pending])
                    if lose_ack:
                        raise ConnectionError('演示：中继已提交，但上传确认丢失')
                    with connect(self.db) as c:
                        c.executemany('UPDATE sync_events SET acked=1 WHERE id=?', [(r['id'],) for r in pending])
            while True:
                with connect(self.db) as c:
                    cursor = c.execute("SELECT value FROM sync_settings WHERE key='cursor'").fetchone()[0]
                batch = self.request(url, query={'after': cursor, 'limit': 100, 'project': self.project})
                if not batch['events']:
                    break
                if reverse:
                    batch['events'].reverse()
                self.receive(batch)
            self.recover_writes()

    def register_file(self, path):
        if not self.archive_root:
            raise ValueError('本机未配置归档连接')
        path = contained(path, self.archive_root)
        meta = self.writer.read_asset(path)
        with connect(self.db) as c:
            c.execute('INSERT OR IGNORE INTO sync_files VALUES(?,?,?,?,?,?,?)',
                      (str(path), sha(path), sha(path), str(meta.get('XMP-fk:AssetId') or '').replace('-', ''),
                       str(meta.get('XMP-fk:FileId') or '').replace('-', ''), canonical(read_capture_identity(path)), canonical(visual_fingerprint(path))))

    def reconcile_raws(self):
        """办公室从本机 RAW 索引配对远端 JPG；不要求现场知道 RAW 名称、哈希或路径。"""
        if not self.archive_root or not self.identity.get('archive_writer'):
            raise ValueError('只有已连接最终归档的办公室角色可配对 RAW')
        with self.lock, connect(self.db) as c:
            rows = [dict(r, data=json.loads(r['data'])) for r in c.execute('SELECT * FROM sync_entities ORDER BY id')]
            raws = [dict(r, capture_identity=json.loads(r['capture_identity']), visual_fingerprint=json.loads(r['visual_fingerprint'])) for r in c.execute("SELECT * FROM sync_files WHERE lower(path) LIKE '%.cr3'")]
        proposals = []
        for row in rows:
            data = row['data']
            if not data['active']:
                continue
            state, reason, evidence = 'waiting_raw', '远端 JPG 元数据已到，等待 RAW 到达最终归档', []
            if row['state'] == 'conflict':
                state, reason = 'review', '拍摄元数据并发冲突，暂不绑定 RAW'
            elif any(p['extension'] == '.cr3' for p in data['components']):
                state, reason = 'bound', 'RAW 已绑定；身份资料已同步给其他机器'
                with connect(self.db) as c:
                    prior = c.execute('SELECT evidence FROM sync_raw_pairs WHERE entity=?', (row['id'],)).fetchone()
                if prior:
                    evidence = json.loads(prior['evidence'])
            else:
                certain = []
                for raw in raws:
                    path = contained(raw['path'], self.archive_root)
                    if not path.is_file():
                        continue
                    match = strong_raw_match(raw['capture_identity'], data['capture_identity'])
                    if match:
                        check = visual_match(raw['visual_fingerprint'], data['visual_fingerprint'])
                        evidence.append(dict(path=str(path), **match, visual=check))
                    if match and match['certain'] and check['accepted']:
                        certain.append(raw)
                if len(certain) > 1:
                    state, reason = 'review', '多个 RAW 候选满足身份条件，禁止自动绑定'
                elif len(certain) == 1:
                    raw = certain[0]
                    owners = [r for r in rows if r['data']['active'] and (strong_raw_match(raw['capture_identity'], r['data']['capture_identity']) or {}).get('certain')]
                    if len(owners) != 1:
                        state, reason = 'review', '同一 RAW 对应多个拍摄证据，需复核'
                    elif sha(raw['path']) != raw['last_sha'] or raw['asset_id'] and raw['asset_id'] != row['id']:
                        state, reason = 'review', 'RAW 已被修改或属于其他资产，禁止绑定'
                    else:
                        component = dict(file_id=stable(row['id'], 'raw:' + raw['original_sha']), sha256=raw['original_sha'],
                                         filename=Path(raw['path']).name, extension='.cr3', capture_identity=raw['capture_identity'],
                                         visual_fingerprint=raw['visual_fingerprint'])
                        updated = json.loads(canonical(data))
                        updated['components'].append(component)
                        # 配对基于读取时的版本；并发编辑应形成分叉，不能认领新头后覆盖旧快照。
                        proposals.append((updated, json.loads(row['heads'])))
                        state, reason = 'proposed', '已生成办公室 RAW 绑定事件，等待中继确认及双向同步'
                elif evidence:
                    state, reason = 'review', '相机身份或视觉证据不足，禁止仅凭同名或同秒自动匹配'
            with connect(self.db) as c:
                c.execute('INSERT INTO sync_raw_pairs VALUES(?,?,?,?) ON CONFLICT(entity) DO UPDATE SET state=excluded.state,reason=excluded.reason,evidence=excluded.evidence',
                          (row['id'], state, reason, canonical(evidence)))
        return [self.emit(data, parents=parents) for data, parents in proposals]

    def _write_state(self, entity, file_id, version, state, reason='', path='', journal=None):
        with connect(self.db) as c:
                c.execute('''INSERT INTO sync_writes VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity,file_id,version)
                DO UPDATE SET state=excluded.state,reason=excluded.reason,path=excluded.path,journal=excluded.journal''',
                      (entity, file_id, version, state, reason, path, canonical(journal or {})))

    def _mark_applied(self, task):
        with connect(self.db) as c:
            c.execute("UPDATE sync_writes SET state='applied',reason='' WHERE entity=? AND file_id=? AND version=?",
                      (task['entity'], task['file_id'], task['version']))
            c.execute('UPDATE sync_files SET last_sha=?,asset_id=?,file_id=? WHERE path=?',
                      (sha(task['path']), task['entity'], task['file_id'], task['path']))
        with self.archive.connect() as c:
            c.execute('INSERT INTO locations(path,file,last_seen,status) VALUES(?,?,?,\'online\') ON CONFLICT(path) DO UPDATE SET file=excluded.file,last_seen=excluded.last_seen',
                      (task['path'], task['file_id'], time.time()))

    def recover_writes(self):
        with self.lock:
            return self._recover_writes_locked()

    def _recover_writes_locked(self):
        with connect(self.db) as c:
            tasks = c.execute("SELECT * FROM sync_writes WHERE state='prepared'").fetchall()
        for task in tasks:
            journal = json.loads(task['journal'])
            try:
                source = contained(task['path'], self.archive_root)
                target = contained(journal['target'], self.archive_root)
                if not source.is_file():
                    continue
                current = sha(target) if target.exists() else None
                if current == journal['after_hash']:
                    if str(target) != str(source) and sha(source) != journal['source_sha']:
                        raise ValueError('RAW 原件已被外部修改')
                    for sidecar in journal.get('sidecars', []):
                        contained(sidecar['target'], self.archive_root)
                    self.writer.commit_sidecars(journal)
                    self._mark_applied(task)
                elif current == journal['before_hash']:
                    self._write_state(task['entity'], task['file_id'], task['version'], 'retry', '文件尚未提交，等待重试', task['path'])
                else:
                    raise ValueError('崩溃恢复发现外部文件修改')
            except (ValueError, OSError) as exc:
                self._write_state(task['entity'], task['file_id'], task['version'], 'review', str(exc), task['path'], journal)

    def write_archive(self, *, crash_after_commit=False):
        with self.lock:
            return self._write_archive_locked(crash_after_commit=crash_after_commit)

    def _write_archive_locked(self, *, crash_after_commit=False):
        self.recover_writes()
        with self.lock, connect(self.db) as c:
            entities = c.execute('SELECT * FROM sync_entities ORDER BY id').fetchall()
        for row in entities:
            data = json.loads(row['data'])
            for component in data['components']:
                args = (row['id'], component['file_id'], row['version'])
                with connect(self.db) as c:
                    done = c.execute('SELECT state FROM sync_writes WHERE entity=? AND file_id=? AND version=?', args).fetchone()
                if done and done['state'] in ('applied', 'review'):
                    continue
                if row['state'] == 'conflict':
                    self._write_state(*args, 'conflict', '并发版本待人工解决')
                    continue
                if component['extension'] == '.cr3':
                    with connect(self.db) as c:
                        binding_event = c.execute('SELECT outbound,acked FROM sync_events WHERE id=?', (row['version'],)).fetchone()
                    if binding_event['outbound'] and not binding_event['acked']:
                        self._write_state(*args, 'waiting_sync_ack', '办公室 RAW 绑定事件尚未由中继确认')
                        continue
                if not data['active']:
                    self._write_state(*args, 'retracted', '拍摄证据已撤销；历史文件不自动清空')
                    continue
                if not self.archive_root:
                    self._write_state(*args, 'waiting_root', '元数据已入库，本机没有归档连接')
                    continue
                with connect(self.db) as c:
                    candidates = c.execute('SELECT * FROM sync_files WHERE original_sha=? OR (asset_id=? AND file_id=?)',
                                           (component['sha256'], row['id'], component['file_id'])).fetchall()
                candidates = [r for r in candidates if Path(r['path']).is_file()]
                if not candidates:
                    self._write_state(*args, 'waiting_file', '元数据已入库，等待对应图片到达')
                    continue
                if len(candidates) != 1:
                    self._write_state(*args, 'review', '多个可靠候选，禁止自动选择')
                    continue
                candidate = candidates[0]
                path = candidate['path']
                try:
                    path = contained(path, self.archive_root)
                    if path.suffix.lower() != component['extension']:
                        raise ValueError('文件格式与组件不符')
                    if sha(path) != candidate['last_sha']:
                        raise ValueError('索引后文件被外部修改，禁止覆盖')
                    before = self.writer.read_asset(path)
                    for key, expected in (('AssetId', row['id']), ('FileId', component['file_id'])):
                        actual = str(before.get('XMP-fk:' + key) or '').replace('-', '')
                        if actual and actual != expected:
                            raise ValueError('嵌入身份不符，禁止覆盖')
                    meta = data['metadata']
                    old_sku = before.get('XMP-fk:SKU')
                    if old_sku and old_sku != meta['sku'] and not candidate['asset_id']:
                        raise ValueError('归档已有其他人工款号，禁止覆盖')
                    fields = {'AssetId': row['id'], 'FileId': component['file_id'], 'ProjectId': self.project,
                              'SegmentId': row['id'], 'SKU': meta['sku'], 'ShootType': meta['shoot_type'],
                              'MetadataRevision': str(row['depth']), 'Rating': meta['rating'], 'Description': meta['description'],
                              'Multidimensional': canonical({'demo': True, 'origin_device': data['origin'],
                                  'origin_alias': self.identities[data['origin']]['alias'], 'sync_event': row['version'], 'ocr_text': meta['ocr_text']})}
                    payload, target, journal = self.writer.write(path, fields, candidate['last_sha'], mode='sidecar')
                    contained(target, self.archive_root)
                    for sidecar in journal.get('sidecars', []):
                        contained(sidecar['target'], self.archive_root)
                    journal['source_sha'] = candidate['last_sha']
                    self._write_state(*args, 'prepared', path=str(path), journal=journal)
                    self.writer.commit(payload, target, journal)
                    self.writer.commit_sidecars(journal)
                    if crash_after_commit:
                        raise SimulatedCrash('演示：文件已提交，数据库完成标记尚未提交')
                    actual = self.writer.read_asset(path)
                    if actual.get('XMP-fk:SKU') != meta['sku'] or actual.get('XMP-xmp:Rating') != meta['rating']:
                        raise ValueError('提交后读回不一致')
                    self._mark_applied(dict(entity=args[0], file_id=args[1], version=args[2], path=str(path)))
                except (ValueError, OSError) as exc:
                    self._write_state(*args, 'review', str(exc), str(path))

    def snapshot(self):
        with self.lock:
            return self._snapshot_locked()

    def _snapshot_locked(self):
        with connect(self.db) as c:
            projects = []
            for p in c.execute("SELECT id,name,metadata FROM projects WHERE json_extract(metadata,'$.collaboration.project')=? ORDER BY name", (self.project,)):
                rows = []
                for r in c.execute('SELECT sku,sheet,status,completed_at,source_values FROM sku_rows WHERE project=? ORDER BY sheet,excel_row', (p['id'],)):
                    rows.append({**dict(r), **json.loads(r['source_values'])})
                projects.append({'id': p['id'], 'name': p['name'], 'rows': rows,
                                 'workbook': str((self.sku.root / p['id'] / (p['name'] if p['name'].endswith('.xlsx') else p['name'] + '.xlsx')).relative_to(self.sandbox))})
            writes = [dict(r) for r in c.execute('SELECT entity,file_id,version,state,reason,path FROM sync_writes ORDER BY entity,file_id,version')]
            entities = [{**dict(r), 'data': json.loads(r['data']), 'heads': json.loads(r['heads'])} for r in c.execute('SELECT * FROM sync_entities ORDER BY id')]
            pending = c.execute('SELECT count(*) FROM sync_events WHERE outbound=1 AND acked=0').fetchone()[0]
            cursor = c.execute("SELECT value FROM sync_settings WHERE key='cursor'").fetchone()[0]
            raw_pairs = [dict(r, evidence=json.loads(r['evidence'])) for r in c.execute('SELECT * FROM sync_raw_pairs ORDER BY entity')]
        with self.archive.connect() as c:
            assets = [dict(r) for r in c.execute('SELECT id,sku,scope,revision FROM assets ORDER BY id')]
        return {'machine': self.identity['alias'], 'projects': projects, 'entities': entities, 'archive_assets': assets,
                'writes': writes, 'raw_pairs': raw_pairs, 'pending_uploads': pending, 'cursor': cursor, 'archive_connected': bool(self.archive_root),
                'role': '办公室归档' if self.identity.get('archive_writer') else '现场拍摄'}
