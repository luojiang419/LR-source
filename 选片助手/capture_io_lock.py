"""业务变更独占，独立文件提交共享；保留短时记录检查互斥与可重入语义。"""
from contextlib import contextmanager
import threading


class CaptureIOLock:
    def __init__(self):
        self.condition=threading.Condition(threading.RLock())
        self.mutex=threading.RLock()
        self.writer=None;self.depth=0;self.waiting=0;self.readers={}

    def __enter__(self):
        ident=threading.get_ident()
        with self.condition:
            if self.writer==ident:self.depth+=1
            else:
                if self.readers.get(ident):raise RuntimeError('并行提交区内不能修改业务归属')
                self.waiting+=1
                try:
                    while self.writer is not None or self.readers:self.condition.wait()
                    self.writer=ident;self.depth=1
                finally:self.waiting-=1
        self.mutex.acquire()
        return self

    def __exit__(self,*args):
        self.mutex.release()
        with self.condition:
            self.depth-=1
            if not self.depth:self.writer=None;self.condition.notify_all()

    @contextmanager
    def parallel(self):
        ident=threading.get_ident()
        with self.condition:
            while (self.writer not in (None,ident) or (self.waiting and self.writer!=ident and not self.readers.get(ident))):self.condition.wait()
            self.readers[ident]=self.readers.get(ident,0)+1
        try:yield
        finally:
            with self.condition:
                self.readers[ident]-=1
                if not self.readers[ident]:self.readers.pop(ident)
                self.condition.notify_all()

    @contextmanager
    def inspect(self):
        # parallel保持业务版本稳定，此处仅串行读取当前确认及准备恢复日志。
        with self.mutex:yield
