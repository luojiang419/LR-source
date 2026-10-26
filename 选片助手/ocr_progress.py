"""仅保存当前请求的瞬时进度；请求代次隔离，不改变识别结果或业务数据。"""
from contextlib import contextmanager
import threading
import time


class OcrProgress:
    def __init__(self):
        self.lock=threading.Lock();self.active={}

    @contextmanager
    def track(self,kind,jid,token):
        key=(kind,jid,token)
        state={'phase':'image','started':time.time(),'phase_started':time.time(),'percent':None}
        with self.lock:self.active[key]=state
        def update(phase,provider=None):
            with self.lock:
                if self.active.get(key) is not state:return
                state.update(phase=phase,phase_started=time.time())
                if provider:state.update(model=provider.get('model',''),protocol=provider.get('protocol',''))
        try:yield update
        finally:
            with self.lock:
                if self.active.get(key) is state:self.active.pop(key,None)

    def snapshot(self,kind,jid,token):
        with self.lock:state=dict(self.active.get((kind,jid,token),{}))
        if not state:return None
        state['elapsed_seconds']=max(0,time.time()-state['started'])
        state['phase_elapsed_seconds']=max(0,time.time()-state['phase_started'])
        if state.get('phase')=='loading_model' and state.get('protocol')=='local':
            from local_inference import snapshot
            runtime=snapshot()
            if runtime.get('model')==state.get('model') and runtime['status'] in ('preparing','loading'):
                state.update(percent=runtime.get('percent'),message=runtime.get('message',''),progress_label=runtime.get('progress_label','模型加载进度'))
        return state
