"""SKU 元数据库可迁移备份；固定表清单，不执行备份文件提供的 SQL。"""
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from sku_library_management import initialize

SCHEMA = 'framekeep.sku-metadata'
VERSION = 1
MAX_BYTES = 200_000_000
TABLES = {
    'catalog': ('sessions', 'captures', 'settings'),
    'sku': ('projects', 'sku_rows', 'jobs', 'parse_jobs', 'ocr_documents', 'ocr_choices', 'sku_shoot_versions',
            'metadata_versions', 'archive_events', 'library_bins', 'library_items', 'library_renames'),
    'archive': ('assets', 'files', 'locations', 'versions', 'file_fingerprints', 'catalog_links',
                'assignments', 'operations', 'metadata_jobs', 'streams', 'bindings', 'segments',
                'event_receipts', 'audit', 'shared_ratings', 'rating_capture_paths'),
}
SETTING_PREFIXES = ('archive_session:', 'archive_disabled:', 'archive_excluded:', 'shooting_session:', 'removed_session:')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _services(sku):
    if not sku.archive:
        raise ValueError('资产元数据库尚未启动')
    initialize(sku)
    sku.archive.ratings  # 初始化可选的共享星级表。
    return {'catalog': sku.store, 'sku': sku, 'archive': sku.archive}


def _columns(c, alias, table):
    return c.execute(f'PRAGMA {alias}.table_info("{table}")').fetchall()


def export_metadata(sku):
    services = _services(sku)
    payload = {'databases': {}, 'documents': {}}
    with sku.lock, sku.archive.lock, sku.store.lock, ExitStack() as stack:
        # 先建立全部读取快照；写入方仍使用各服务的原有锁。
        connections = {key: stack.enter_context(service.connect()) for key, service in services.items()}
        for c in connections.values():
            c.execute('BEGIN')
            c.execute('SELECT count(*) FROM sqlite_master').fetchone()
        for key, c in connections.items():
            tables = payload['databases'][key] = {}
            for table in TABLES[key]:
                if not _columns(c, 'main', table):
                    continue
                rows = [dict(row) for row in c.execute(f'SELECT * FROM "{table}"')]
                if table == 'settings':
                    rows = [row for row in rows if row['key'].startswith(SETTING_PREFIXES)]
                tables[table] = rows
        for project in payload['databases']['sku']['projects']:
            pid = project['id']
            if not re.fullmatch(r'[0-9a-f]{32}', pid):
                raise ValueError('存在无效的拍摄文档编号，无法导出')
            files = {}
            for name in ('source.xlsx', '拍摄对账.xlsx'):
                path = sku.root / pid / name
                if path.is_file():
                    files[name] = base64.b64encode(path.read_bytes()).decode('ascii')
            if files:
                payload['documents'][pid] = files
    backup = {'schema': SCHEMA, 'version': VERSION, 'id': uuid.uuid4().hex,
              'created_at': datetime.now(timezone.utc).isoformat(),
              'sha256': hashlib.sha256(_canonical(payload)).hexdigest(), 'payload': payload}
    if len(_canonical(backup)) > MAX_BYTES:
        raise ValueError('元数据备份超过 200 MB，请联系维护人员处理')
    return {'name': 'SKU元数据-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.framekeep.json', 'backup': backup}


def _decode(content):
    if len(content) > MAX_BYTES:
        raise ValueError('元数据文件不能超过 200 MB')
    try:
        backup = json.loads(content.decode('utf-8-sig'))
        if not isinstance(backup, dict) or backup.get('schema') != SCHEMA:
            raise ValueError('请选择末帧导出的 SKU 元数据文件')
        if type(backup.get('version')) is not int or backup['version'] != VERSION:
            raise ValueError('元数据版本不兼容，请使用兼容版本的软件')
        payload = backup['payload']
        if hashlib.sha256(_canonical(payload)).hexdigest() != backup.get('sha256'):
            raise ValueError('元数据文件校验失败，文件可能损坏')
        databases = payload['databases']
        if set(databases) != set(TABLES):
            raise ValueError('元数据库不完整')
        for key, tables in databases.items():
            if not isinstance(tables, dict) or set(tables) - set(TABLES[key]):
                raise ValueError('元数据包含不支持的表')
            for rows in tables.values():
                if not isinstance(rows, list) or any(not isinstance(row, dict) or not row for row in rows):
                    raise ValueError('元数据记录无效')
                if any(type(v) not in (str, int, float, type(None)) for row in rows for v in row.values()):
                    raise ValueError('元数据字段类型无效')
        for key, required in {'catalog': ('sessions', 'captures', 'settings'), 'sku': ('projects', 'sku_rows', 'ocr_documents', 'library_bins', 'library_items'), 'archive': ('assets', 'catalog_links', 'assignments')}.items():
            if not set(required) <= set(databases[key]):
                raise ValueError('元数据库缺少必要数据表')
        project_ids = {row['id'] for row in databases['sku']['projects']}
        if any(not isinstance(pid, str) or not re.fullmatch(r'[0-9a-f]{32}', pid) for pid in project_ids):
            raise ValueError('拍摄文档路径无效')
        documents = {}
        for pid, files in payload['documents'].items():
            if not re.fullmatch(r'[0-9a-f]{32}', pid) or pid not in project_ids or not isinstance(files, dict):
                raise ValueError('拍摄文档路径无效')
            for name, encoded in files.items():
                if name not in ('source.xlsx', '拍摄对账.xlsx'):
                    raise ValueError('拍摄文档名称无效')
                documents[(pid, name)] = base64.b64decode(encoded, validate=True)
        return backup, documents
    except (KeyError, TypeError, AttributeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError('元数据文件结构无效') from None
    except (json.JSONDecodeError, base64.binascii.Error):
        raise ValueError('元数据文件内容无效') from None


def _paused(table, row):
    row = dict(row)
    if table == 'projects':
        row['enabled'] = 0
    if table in ('jobs', 'parse_jobs') and row.get('status') in ('queued', 'running', 'waiting_model', 'retry'):
        row.update(status='cancelled', reason='跨机器导入后暂停，请手动重新识别')
    if table == 'metadata_jobs' and row.get('status') in ('queued', 'running'):
        row.update(status='failed', reason='跨机器导入后暂停，请核对照片路径后手动重试')
    if table == 'streams':
        row['state'] = 'paused'
    if table == 'archive_events':
        row['delivered'] = 1
    if table == 'library_renames' and row.get('status') in ('running', 'queued'):
        row['status'] = 'imported'
    return row


def import_metadata(sku, content, *, sync=None):
    backup, documents = _decode(content)
    result = _import_records(sku, backup, documents, sync)
    sku.archive.ratings.invalidate_seeds()
    for session in sku.store.sessions():sku.archive.ratings.seed_session(session['id'])
    if sync:result['sync'] = sync.start(backup['sha256'], backup['payload'])
    return result


def _adopt_scanned_asset(c, old_id, new_id, versions):
    if old_id==new_id:return True
    asset=c.execute('SELECT * FROM archive.assets WHERE id=?',(old_id,)).fetchone()
    if not asset or asset['sku'] or asset['revision']:return False
    for used in ('assignments','metadata_jobs','bindings'):
        if c.execute('SELECT 1 FROM archive.'+used+' WHERE asset=?',(old_id,)).fetchone():return False
    incoming=c.execute('SELECT id,sha FROM archive.files WHERE asset=?',(new_id,)).fetchall()
    known=c.execute('SELECT sha FROM archive.files WHERE asset=?',(old_id,)).fetchall()
    if any(not any(file['sha']==target['sha'] or (target['id'],file['sha']) in versions for target in incoming) for file in known):return False
    c.execute('UPDATE archive.files SET asset=? WHERE asset=?',(new_id,old_id))
    c.execute('UPDATE archive.catalog_links SET asset=? WHERE asset=?',(new_id,old_id))
    c.execute('UPDATE archive.file_fingerprints SET asset=? WHERE asset=?',(new_id,old_id))
    c.execute('DELETE FROM archive.assets WHERE id=?',(old_id,))
    return True


def _merge_discovered(c, alias, table, row, existing, local_sessions, versions):
    """只兼容本机归档扫描得到的事实；人工归属／业务记录仍遵循冲突回滚。"""
    if alias=='catalog' and table=='sessions' and row['id'] in local_sessions and existing['root'].casefold()==row['root'].casefold():return True
    if alias=='catalog' and table=='captures' and row['session'] in local_sessions:
        old,new=json.loads(existing['data']),json.loads(row['data'])
        return any(old.get(k) and old.get(k)==new.get(k) for k in ('raw','jpg'))
    if alias!='archive':return False
    if table=='assets' and existing['source'] in ('','embedded') and not existing['locked']:
        if c.execute('SELECT 1 FROM archive.assignments WHERE asset=?',(row['id'],)).fetchone():return False
        if existing['sku'] and existing['sku']!=row['sku'] and existing['revision']>row['revision']:return False
        fields=[field for field in row if field!='id']
        c.execute('UPDATE archive.assets SET '+','.join('"'+f+'"=?' for f in fields)+' WHERE id=?',[row[f] for f in fields]+[row['id']])
        return True
    if table=='files':return existing['asset']==row['asset'] and existing['component']==row['component'] and (existing['sha']==row['sha'] or (row['id'],existing['sha']) in versions)
    if table=='versions':return True
    if table=='locations':
        if existing['file']==row['file']:return True
        old=c.execute('SELECT * FROM archive.files WHERE id=?',(existing['file'],)).fetchone()
        new=c.execute('SELECT * FROM archive.files WHERE id=?',(row['file'],)).fetchone()
        if not old or not new or (old['sha']!=new['sha'] and (new['id'],old['sha']) not in versions):return False
        if not _adopt_scanned_asset(c,old['asset'],new['asset'],versions):return False
        c.execute('UPDATE archive.locations SET file=? WHERE path=?',(new['id'],row['path']))
        c.execute('UPDATE archive.file_fingerprints SET file=?,asset=? WHERE path=?',(new['id'],new['asset'],row['path']))
        return True
    if table=='file_fingerprints':return existing['asset']==row['asset'] and existing['file']==row['file']
    if table=='catalog_links' and row['session'] in local_sessions:
        return _adopt_scanned_asset(c,existing['asset'],row['asset'],versions)
    if table=='shared_ratings' and not existing['updated']:
        c.execute('UPDATE archive.shared_ratings SET rating=?,updated=? WHERE path_key=?',(row['rating'],row['updated'],row['path_key']))
        return True
    return False


def _import_records(sku, backup, documents, sync):
    services = _services(sku)
    local_sessions=set()
    if sync:
        roots={str(Path(root['path']).resolve()).casefold() for root in sync.catalog.state()['roots']}
        with sku.store.connect() as c:local_sessions={r['id'] for r in c.execute('SELECT id,root FROM sessions') if str(Path(r['root']).resolve()).casefold() in roots}
    versions={(r['file'],r['sha']) for r in backup['payload']['databases']['archive'].get('versions',[])}
    stamp = backup['sha256']
    created = []
    count = 0
    with sku.lock, sku.archive.lock, sku.store.lock, sku.connect() as c:
        for alias in ('catalog', 'archive'):
            c.execute(f'ATTACH DATABASE ? AS {alias}', (str(services[alias].db),))
        c.execute('CREATE TABLE IF NOT EXISTS metadata_imports(sha256 TEXT PRIMARY KEY,created TEXT)')
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM metadata_imports WHERE sha256=?', (stamp,)).fetchone():
            return {'already_imported': True, 'records': 0, 'projects': 0}
        try:
            for key, tables in backup['payload']['databases'].items():
                alias = 'main' if key == 'sku' else key
                for table in TABLES[key]:
                    rows = tables.get(table, [])
                    columns = _columns(c, alias, table)
                    allowed = {col['name'] for col in columns}
                    pk = [col['name'] for col in sorted(columns, key=lambda col: col['pk']) if col['pk']]
                    for original in rows:
                        if not set(original) <= allowed or not set(pk) <= set(original) or not pk:
                            raise ValueError('元数据字段不兼容，请使用相同或更新版本的软件')
                        if table == 'settings' and not original['key'].startswith(SETTING_PREFIXES):
                            raise ValueError('备份包含不支持的机器设置')
                        row = _paused(table, original)
                        if sync and table=='metadata_jobs' and original.get('status') in ('queued','running'):
                            row.update(status='cancelled',reason='旧机器待写入任务已停止，由本机归档迁移队列接续')
                        where = ' AND '.join('"' + field + '" IS ?' for field in pk)
                        existing = c.execute(f'SELECT * FROM {alias}."{table}" WHERE {where}', [row[field] for field in pk]).fetchone()
                        if existing:
                            if table != 'settings' and any(existing[field] != value for field, value in row.items()):
                                if sync and _merge_discovered(c,alias,table,row,existing,local_sessions,versions):continue
                                raise ValueError('备份与本机已有记录冲突，未导入；请在新机器或独立资料库中加载')
                            continue
                        fields = ','.join('"' + field + '"' for field in row)
                        c.execute(f'INSERT INTO {alias}."{table}" ({fields}) VALUES ({",".join("?" for _ in row)})', list(row.values()))
                        count += 1
            # FTS rowid 必须按导入后的本机 rowid 重建。
            c.execute('DELETE FROM ocr_document_search')
            c.execute('INSERT INTO ocr_document_search(rowid,searchable) SELECT rowid,searchable FROM ocr_documents')
            for pid, name in documents:
                path = sku.root / pid / name
                if path.exists():
                    if path.read_bytes() != documents[(pid, name)]:
                        raise ValueError('本机已有不同的同编号拍摄文档，未导入')
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as output:
                    created.append(path)
                    output.write(documents[(pid, name)])
            c.execute('INSERT INTO metadata_imports VALUES (?,?)', (stamp, datetime.now(timezone.utc).isoformat()))
            c.commit()
        except Exception as exc:
            c.rollback()
            for path in created:
                path.unlink(missing_ok=True)
                if not any(path.parent.iterdir()):
                    path.parent.rmdir()
            if isinstance(exc, sqlite3.Error):
                raise ValueError('元数据库记录冲突或无效，已撤销本次导入') from None
            raise
    return {'already_imported': False, 'records': count,
            'projects': len(backup['payload']['databases']['sku']['projects'])}
