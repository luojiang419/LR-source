"""归档复核只约束本次选用且由该数据集管理的文件。"""
from pathlib import Path


def validate_archive_review_scope(capture, plan):
    selected = {Path(row[key]).absolute() for row in plan['items']
                for key in ('source', 'reference') if row.get(key)}
    if not selected:
        return
    for dataset in list(capture.datasets.values()):
        if not any(path.is_relative_to(dataset.root) for path in selected):
            continue
        reviews = [group for group in dataset.groups() if group.get('status') == 'review']
        if not reviews:
            continue
        assets = set(); groups = set()
        for record in dataset.records_snapshot():
            path = (dataset.root / record['relative_path']).absolute()
            if path not in selected or capture.dataset_for_path(path) is not dataset:
                continue
            assets.add(record['asset_id'])
            groups.update(record.get(key) for key in ('group_id', 'inherited_group_id') if record.get(key))
        for group in reviews:
            members = set(group.get('members') or [])
            if group.get('label'):
                members.add(group['label'])
            if group['id'] in groups or assets.intersection(members):
                detail = group.get('sku') or group['id']
                raise ValueError('本次选用来源存在待复核分组，请核对后再归档：' +
                                 str(dataset.root) + '；分组：' + detail +
                                 ('；原因：' + group['reason'] if group.get('reason') else ''))
