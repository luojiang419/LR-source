"""目录范围冻结的双图拍摄文档；只在显式导出时生成。"""
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import re

from portable_capture_store import resolve_under


def export_records(dataset, scope, records, day, catalog, *, revision=None):
    records = [dict(r) for r in records]
    revision = dataset.meta('revision') if revision is None else revision
    details = [['实际 SKU', '相对路径', '类型', '角色', '星级', '资产 ID', '文件 ID', '确认版本', '文件元数据', '文件可用',
                '原款号','母 SKU','客户','品牌','拍摄日期','拍摄类型','分组 ID']]
    info = [['项目', '值']]
    now = datetime.now(timezone(timedelta(hours=8)))
    for pair in [('文件夹', scope.name), ('范围', scope.relative_to(dataset.root).as_posix()), ('拍摄日期筛选', day or '全部日期'),
                 ('数据集', dataset.dataset_id), ('业务版本', revision), ('导出时间', now.isoformat()), ('文件记录数', len(records))]:
        info.append(pair)
    groups = defaultdict(list)
    availability={}
    paths={}
    directory_files={}
    for record in records:
        groups[(record.get('group_view_id') or record['group_id']) if record.get('group_id') else 'unclosed:' + record['asset_id']].append(record)
        path = resolve_under(dataset.root, record['relative_path'],verify_links=False)
        paths[record['id']]=path
        if path.parent not in directory_files:
            relative=path.parent.relative_to(dataset.root).as_posix()
            # 每个父目录只校验、枚举一次；文件名与链接属性来自枚举结果。
            try:
                parent=resolve_under(dataset.root,relative) if relative!='.' else dataset.root
                if parent.is_symlink() or parent.is_junction():raise ValueError('导出目录包含链接')
                with os.scandir(parent) as entries:
                    directory_files[path.parent]={e.name.casefold() for e in entries if not e.is_symlink() and not e.is_junction() and e.is_file(follow_symlinks=False)}
            except OSError:
                directory_files[path.parent]=set()
        availability[record['id']]=path.name.casefold() in directory_files[path.parent]
        details.append([record.get('sku', ''), record['relative_path'], record['media_type'], record.get('role', 'product'),
                        record.get('rating', 0), record['asset_id'], record['id'], record.get('decision_id', ''),
                        record.get('write_state', 'unknown'), '存在' if availability[record['id']] else '缺失／离线',
                        record.get('base_sku',''),record.get('parent_sku',''),record.get('customer',''),record.get('brand',''),
                        record.get('shoot_date',''),record.get('shoot_type',''),record.get('group_view_id') or record.get('group_id','')])
    info.append(('当前可用文件数',sum(availability.values())))
    def picture(record, size):
        if not record:
            return
        path = paths[record['id']]
        with catalog.connect() as c:
            item = c.execute('SELECT id FROM items WHERE path=?', (str(path),)).fetchone()
        if not item or not availability[record['id']]:
            return None
        try:
            return catalog.preview(item['id'],size,read=True)
        except Exception:
            return None

    entries=[]
    for gid, rows in groups.items():
        first = rows[0]
        codes = {r.get('sku') for r in rows if r.get('sku')}
        product=next((r for r in rows if r.get('role')!='sku_label' and availability[r['id']]),None)
        label=next((r for r in rows if r.get('role')=='sku_label'),None)
        source=label or product or first
        entries.append({'id':gid,'sku':first.get('sku') or next(iter(codes),'') if len(codes)<=1 else first.get('sku',''),
                        'shoot_type':first.get('shoot_type') or '现场记录','source':Path(source['relative_path']).name,
                        'folder':paths[source['id']].parent.name,'added':first.get('ingested_at') or first.get('imported_at') or '',
                        'completed':label.get('time','') if label else '', 'status':'已拍摄' if len(codes)==1 else '未拍摄',
                        'picture':picture(product,420),'label':picture(label,2048)})
    dates = sorted({r.get('shoot_date', '')[:10] for r in records if r.get('shoot_date')})
    date = day or (dates[0] if len(dates) == 1 else dates[0] + '_' + dates[-1] if dates else now.strftime('%Y-%m-%d'))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', scope.name).rstrip('. ')[:100] or '拍摄记录'
    if name.split('.')[0].upper() in ('CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))):
        name = '_' + name
    filename = name + '_' + re.sub(r'[^0-9_-]', '', date) + '.xlsx'
    from archive_capture_template import workbook
    return workbook(entries,details,info),filename
