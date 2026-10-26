"""轻量字段恢复档案：只记录本软件修改的元数据，不包含照片像素。"""
import json
from pathlib import Path
import re

from file_safety import atomic_write


def save_record(data,identity,journal,*,source='',status='prepared'):
    if not re.fullmatch(r'[0-9a-f]{32}',identity):raise ValueError('元数据档案编号无效')
    path=Path(data)/'metadata-history'/(identity+'.json')
    record={'schema':'framekeep.metadata-history','version':1,'id':identity,
            'source':str(source),'status':status,
            'journal':{k:v for k,v in journal.items() if k not in ('backup','prepared')}}
    atomic_write(path,json.dumps(record,ensure_ascii=False,sort_keys=True,indent=2).encode('utf8'))
    return path
