"""显式启用的本机验收计时，不记录图片正文、OCR文本或模型凭据。"""
import json
import os
from pathlib import Path
import threading
import time

_lock=threading.Lock()


def enabled():return os.environ.get('FRAMEKEEP_TRACE_PIPELINE')=='1' and bool(os.environ.get('FRAMEKEEP_DATA'))


def record(kind,**values):
    if not enabled():return
    try:
        path=Path(os.environ['FRAMEKEEP_DATA'])/'pipeline-metrics.jsonl'
        with _lock:
            with path.open('a',encoding='utf8') as stream:stream.write(json.dumps({'kind':kind,'at':time.time(),**values},ensure_ascii=False)+'\n')
    except OSError:pass # 可观测数据失败不能改变业务事务结果。
