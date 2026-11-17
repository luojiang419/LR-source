"""进程内共享推理容量；标签优先，不按卡片叠加显存并发。"""
from contextlib import contextmanager
import threading

class VisionScheduler:
    def __init__(self,limit=2):
        self.limit=limit;self.active=0;self.waiting_labels=0;self.condition=threading.Condition()
        self.ordered=False;self.order_requested=False;self.ocr_pending=False;self.stages={'ocr':0,'vision':0};self.waiting_ocr=0
        self.preview_pending=False;self.preview_active=0
        self.waiting_priority=0
        self.waiting_priority_ocr=0
        self.workflow_owners=set()

    def order_workflow(self,owner,pending):
        with self.condition:
            if pending:self.workflow_owners.add(owner)
            else:self.workflow_owners.discard(owner)
            self.condition.notify_all()

    def background_blocked(self):
        with self.condition:return bool(self.workflow_owners)

    def order_previews(self,pending):
        with self.condition:
            self.preview_pending=bool(pending);self.condition.notify_all()

    @contextmanager
    def preview_slot(self,cancel=None):
        with self.condition:
            while self.active or self.waiting_priority or self.workflow_owners:
                if cancel and cancel.is_set():raise ValueError('解码任务已暂停')
                self.condition.wait(.15)
            if cancel and cancel.is_set():raise ValueError('解码任务已暂停')
            self.preview_active+=1
        try:yield
        finally:
            with self.condition:self.preview_active-=1;self.condition.notify_all()

    def order_archive(self,enabled,pending=False):
        with self.condition:
            self.order_requested=bool(enabled)
            self.ordered=bool(enabled or self.ordered and any(self.stages.values()))
            self.ocr_pending=bool(enabled and pending);self.condition.notify_all()
    def configure(self,value):
        if type(value) is not int or value not in (1,2):raise ValueError('并发数可选 1 或 2')
        with self.condition:self.limit=value;self.condition.notify_all()
    @contextmanager
    def slot(self,label=False,cancel=None,independent=False,stage=None,valid=None,priority=False):
        stage=stage or 'vision'
        with self.condition:
            priority=priority or label
            if priority:self.waiting_priority+=1
            if priority and stage=='ocr':self.waiting_priority_ocr+=1
            if label:self.waiting_labels+=1
            if stage=='ocr':self.waiting_ocr+=1
            try:
                def blocked():
                    capacity=not independent and (self.active>=self.limit or (not label and self.waiting_labels))
                    phase=(self.preview_pending and not priority) or self.preview_active or (stage=='ocr' and self.stages['vision']) or (
                        stage=='vision' and (self.stages['ocr'] or self.waiting_priority_ocr or not priority and (self.waiting_ocr or self.ordered and self.ocr_pending)))
                    return capacity or phase
                while blocked():
                    if cancel and cancel.is_set() or valid and not valid():raise ValueError('推理任务已暂停')
                    self.condition.wait(.15)
                if cancel and cancel.is_set() or valid and not valid():raise ValueError('推理任务已暂停')
                self.active+=1
                if stage in self.stages:self.stages[stage]+=1
            finally:
                if priority:self.waiting_priority-=1
                if priority and stage=='ocr':self.waiting_priority_ocr-=1
                if label:self.waiting_labels-=1
                if stage=='ocr':self.waiting_ocr-=1
                self.condition.notify_all()
        try:yield
        finally:
            with self.condition:
                self.active-=1
                if stage in self.stages:self.stages[stage]-=1
                if not self.order_requested and not any(self.stages.values()):self.ordered=False
                self.condition.notify_all()
    def state(self):
        with self.condition:return {'limit':self.limit,'active':self.active,'waiting_labels':self.waiting_labels,
            'ordered':self.ordered,'ocr_pending':self.ocr_pending,'stages':dict(self.stages),'waiting_ocr':self.waiting_ocr,
            'preview_pending':self.preview_pending,'preview_active':self.preview_active,'workflow_priority':bool(self.workflow_owners)}
