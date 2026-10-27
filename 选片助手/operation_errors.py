"""保存最近一次操作失败的堆栈，不记录请求体、API 配置或照片内容。"""
from datetime import datetime,timezone
import json
import traceback
import uuid

from file_safety import atomic_write


def record_error(data,operation,error):
    identity=uuid.uuid4().hex[:12]
    report={'id':identity,'time':datetime.now(timezone.utc).isoformat(),'operation':operation,
            'type':type(error).__name__,'message':str(error)[:1000],
            'traceback':''.join(traceback.format_exception(error))[-20000:]}
    try:atomic_write(data/'last-operation-error.json',json.dumps(report,ensure_ascii=False,indent=2).encode('utf8'))
    except OSError:pass
    return identity
