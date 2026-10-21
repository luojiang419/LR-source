"""款末标签区间：只使用已有边界，按拍摄顺序重新划分，无模型调用。"""
from datetime import datetime, timezone
from pathlib import PurePosixPath


def _time(record):
    try:
        value=datetime.fromisoformat(record.get('time') or '')
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError,TypeError,OverflowError):
        return None


def partition(records, boundaries):
    """返回互不重叠的结束区间；缺少可信时间时保留整个相机流的入库顺序。"""
    streams={}
    for position,record in enumerate(records):
        relative=str(record.get('jpg') or record.get('raw') or '').replace('\\','/')
        key=(PurePosixPath(relative).parent.as_posix(),record.get('cameraKey') or record.get('camera') or '')
        streams.setdefault(key,[]).append((position,record,_time(record)))
    intervals=[]
    for key,entries in streams.items():
        timed=all(value is not None for _,_,value in entries)
        if timed:
            entries.sort(key=lambda entry:(entry[2],entry[0]))
        start=0
        for index,(position,record,captured) in enumerate(entries):
            if record['id'] not in boundaries:
                continue
            intervals.append({'label':record['id'],'boundary':boundaries[record['id']],
                'members':[entry[1]['id'] for entry in entries[start:index+1]],'position':position,
                'stream':key,'order_source':'capture_time' if timed else 'catalog_order'})
            start=index+1
    return intervals
