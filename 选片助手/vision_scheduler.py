"""进程内共享推理容量；标签优先，不按卡片叠加显存并发。"""
from contextlib import contextmanager
import threading

class VisionScheduler:
    def __init__(self,limit=2):self.limit=limit;self.active=0;self.waiting_labels=0;self.condition=threading.Condition()
    def configure(self,value):
        if type(value) is not int or value not in (1,2):raise ValueError('并发数可选 1 或 2')
        with self.condition:self.limit=value;self.condition.notify_all()
    @contextmanager
    def slot(self,label=False,cancel=None):
        with self.condition:
            if label:self.waiting_labels+=1
            try:
                while self.active>=self.limit or (not label and self.waiting_labels):
                    if cancel and cancel.is_set():raise ValueError('推理任务已暂停')
                    self.condition.wait(.15)
                self.active+=1
            finally:
                if label:self.waiting_labels-=1
        try:yield
        finally:
            with self.condition:self.active-=1;self.condition.notify_all()
    def state(self):
        with self.condition:return {'limit':self.limit,'active':self.active,'waiting_labels':self.waiting_labels}
