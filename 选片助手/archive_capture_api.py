"""归档修正接口；主 Handler 负责既有 Host、令牌及单向同步权限检查。"""
import sys
import threading
from urllib.parse import parse_qs

from archive_capture_service import ArchiveCaptureService

_LOCK = threading.Lock()


def service(handler):
    host = sys.modules[handler.__class__.__module__]
    archive, catalog, store = host.ARCHIVE, host.CATALOG, host.STORE
    if not archive or not catalog or not store:
        raise ValueError('归档修正服务尚未就绪')
    with _LOCK:
        current = getattr(archive, 'capture_service', None)
        if current is None:
            current = ArchiveCaptureService(store, archive, catalog, getattr(host, 'SKU', None), watch=getattr(host, 'WATCH', None))
        if not hasattr(handler.server, 'capture_service'):
            handler.server.capture_service = current
            original_close = handler.server.server_close
            def close():
                try:
                    current.close()
                finally:
                    original_close()
            handler.server.server_close = close
    return current


def get(handler, path, query):
    api = service(handler)
    q = {k: v[0] for k, v in parse_qs(query, max_num_fields=12).items()}
    if path.endswith('/metadata-write-state'):
        return handler.reply(api.metadata_write_state(q.get('root_id',''),q.get('folder','')))
    if path.endswith('/state'):
        return handler.reply(api.state(q.get('root_id', ''), q.get('folder', ''),capture_mode=q.get('capture_mode')=='1' if 'capture_mode' in q else None))
    if path.endswith('/item'):
        dataset, record = api.record_for_item(q.get('item_id', ''),allow_conflict=True)
        group = next((g for g in dataset.groups() if g.get('status') != 'cancelled' and record['asset_id'] in g.get('members', [])), None)
        item = api.catalog.item(q.get('item_id', ''))
        # 只取当前整款成员，不为统计和列出前30项两次解码整个数据集。
        members=dataset.records_for_assets(set(group['members']) if group else {record['asset_id']})
        count=len(members)
        conflict_files=[{'path':r['relative_path'],'archive_sku':r.get('sku',''),'file_sku':r.get('file_sku','')} for r in members][:30]
        # 右栏解析动画按当前图最近一次 OCR 类任务区分排队、解析、应用整款与失败。
        with dataset.connect() as c:
            row=c.execute("SELECT kind,status,reason,revision FROM tasks WHERE record=? AND kind IN ('ocr','ocr_now','ocr_read','ocr_apply') AND status!='cancelled' ORDER BY rowid DESC LIMIT 1",(record['id'],)).fetchone()
        ocr_task={'kind':row[0],'status':row[1],'reason':row[2] or '','revision':row[3]} if row else None
        return handler.reply({'record': record, 'group': group, 'conflict_file_count':count,'conflict_files':conflict_files,'editing': dataset.editing, 'item': item, 'scope': {'root_id': item['root'], 'folder': item.get('folder', '')},'ocr_task':ocr_task})
    if path.endswith('/document'):
        content, name = api.export(q.get('root_id', ''), q.get('folder', ''), q.get('day', ''))
        return handler.reply(content, kind='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', filename=name)
    return handler.reply({'error': '归档修正接口不存在'}, 404)


def post(handler, path, body):
    api = service(handler)
    action = path.rsplit('/', 1)[-1]
    root, folder = body.get('root_id', ''), body.get('folder', '')
    if action == 'edit':
        return handler.reply(api.edit(root, folder, enabled=bool(body.get('enabled', True)), customer=body.get('customer'), brand=body.get('brand')))
    if action == 'rating':
        return handler.reply(api.rate(body['item_id'], body['rating'], correction=bool(body.get('correction',True)),root_id=root or None,folder=folder))
    if action == 'confirm':
        return handler.reply(api.confirm(body['item_id'], body['sku'], whole_group=bool(body.get('whole_group', True)), expected_revision=body.get('expected_revision'),auto_edit=True,root_id=root or None,folder=folder))
    if action == 'resolve':
        return handler.reply(api.resolve_metadata(body['item_id'],body.get('choice'),body.get('conflict_token'),root_id=root or None,folder=folder))
    if action == 'batch-preview':
        return handler.reply(api.batch_preview(root, folder, body.get('item_ids'), body.get('sku')))
    if action == 'batch-apply':
        return handler.reply(api.batch_apply(root, folder, body.get('preview_id', '')))
    if action == 'undo':
        return handler.reply(api.undo(root, folder, body.get('operation_id', '')))
    if action == 'retry':
        return handler.reply(api.retry(root, folder))
    if action == 'write-metadata':
        return handler.reply(api.request_metadata_write(root, folder))
    if action == 'recognize':
        return handler.reply(api.recognize(body['item_id'],auto_edit=True))
    return handler.reply({'error': '归档修正操作不存在'}, 404)
