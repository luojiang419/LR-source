"""记录当前进程的启动阶段；慢启动保留主线程堆栈供现场定位。"""
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback

from file_safety import atomic_write

MESSAGES={
    'initializing':'正在读取照片资料…',
    'catalog ready':'正在恢复图片缓存…',
    'preview queue ready':'正在恢复选片设置与星级…',
    'ratings ready':'正在接入参考资料…',
    'reference library ready':'正在准备拍摄工作台…',
    'http ready':'正在载入工作台…',
}


class StartupProgress:
    def __init__(self,data):
        self.data=Path(data);self.started=time.monotonic();self.done=threading.Event()
        self.main_thread=threading.get_ident();self.stage='initializing';self.ready=False
        self.worker=threading.Thread(target=self._watch,name='startup-diagnostics',daemon=True)

    def __enter__(self):
        self.checkpoint('initializing');self.worker.start();return self

    def _write(self,status,**extra):
        state={'application':'framekeep','process_id':os.getpid(),
               'desktop_pid':os.environ.get('FRAMEKEEP_DESKTOP_PID',''),
               'stage':self.stage,'message':MESSAGES.get(self.stage,'正在打开工作台…'),
               'status':status,'elapsed_seconds':round(time.monotonic()-self.started,3),**extra}
        try:atomic_write(self.data/'engine-startup.json',json.dumps(state,ensure_ascii=False).encode('utf8'))
        except OSError:pass  # 诊断文件写失败不能阻止正常启动；异常仍进入 engine.log。

    def checkpoint(self,stage):
        self.stage=stage
        print(f'[startup] {time.monotonic()-self.started:.3f}s {stage}',flush=True)
        self.ready=stage=='http ready'
        self._write('ready' if self.ready else 'starting')
        if self.ready:self.done.set()

    def _watch(self):
        while not self.done.wait(15):
            frame=sys._current_frames().get(self.main_thread)
            if frame is None:return
            diagnostic=f'\n[startup] pid={os.getpid()} elapsed={time.monotonic()-self.started:.3f}s stage={self.stage}\n'+''.join(traceback.format_stack(frame))
            try:
                with (self.data/'engine-startup-stacks.log').open('a',encoding='utf8') as output:output.write(diagnostic)
            except OSError:pass
            finally:del frame

    def __exit__(self,kind,error,tb):
        if kind is not None and not self.ready:self._write('failed',error_type=kind.__name__)
        self.done.set();self.worker.join(timeout=1)
