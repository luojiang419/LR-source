"""模型卡片共享容量：最新二星优先，普通请求 FIFO，可取消等待。"""
from contextlib import contextmanager
import threading

DEFAULT_API_CONCURRENCY=4
MAX_API_CONCURRENCY=64


def concurrency_value(provider):
    return 1 if provider.get('protocol')=='local' else provider.get('concurrency',DEFAULT_API_CONCURRENCY)


class ProviderConcurrency:
    def _init_concurrency(self):
        self.capacity_changed=threading.Condition(self.lock);self.inference_active={};self.inference_waiters={}

    def concurrency(self,provider):
        with self.lock:
            current=next((p for p in self.data['providers'] if p['id']==provider['id']),provider)
            return concurrency_value(current)

    @contextmanager
    def inference_slot(self,provider,*,priority=False,cancel=None,valid=None,background=False):
        pid=provider['id'];ticket=object();acquired=False
        with self.capacity_changed:self.inference_waiters.setdefault(pid,[]).append((ticket,priority))
        try:
            while True:
                if cancel and cancel.is_set():raise ValueError('推理任务已停止')
                if valid and not valid():raise ValueError('任务状态已变化，未发送请求')
                with self.capacity_changed:
                    waiting=self.inference_waiters[pid]
                    urgent=[item for item in waiting if item[1]]
                    next_ticket=max(reversed(urgent),key=lambda item:item[1])[0] if urgent else waiting[0][0]
                    capacity=self.concurrency(provider)
                    # OCR 批量任务预留一个现场名额；二星仍可用满卡片容量，不突破服务器并发上限。
                    if background and not priority and provider.get('category')=='ocr' and capacity>1:capacity-=1
                    if self.inference_active.get(pid,0)<capacity and ticket is next_ticket:
                        self.inference_active[pid]=self.inference_active.get(pid,0)+1
                        waiting.remove((ticket,priority));acquired=True;break
                    self.capacity_changed.wait(.1)
            yield
        finally:
            with self.capacity_changed:
                waiting=self.inference_waiters.get(pid,[])
                if (ticket,priority) in waiting:waiting.remove((ticket,priority))
                if not waiting:self.inference_waiters.pop(pid,None)
                if acquired:
                    self.inference_active[pid]-=1
                    if not self.inference_active[pid]:self.inference_active.pop(pid)
                self.capacity_changed.notify_all()
