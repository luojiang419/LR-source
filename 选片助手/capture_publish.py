"""本机事件已提交后独立发布随行档案，慢共享盘不占用二星/OCR调度线程。"""
import threading
import time


class CapturePublisher:
    def __init__(self, *, autostart=True):
        self.background=autostart
        self.condition=threading.Condition()
        self.pending={}
        self.closed=False
        self.thread=threading.Thread(target=self._run,daemon=True,name='capture-event-publisher')
        if autostart:self.thread.start()

    def request(self,dataset):
        if not self.background:
            # 手工调度的工具/测试维持同步语义，不隐式启动工作线程。
            return dataset.publish()
        with self.condition:
            if self.closed:return  # events.pending 已持久化，下次取得编辑权自动接续。
            self.pending.setdefault(str(dataset.root),(dataset,time.monotonic()))
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                while not self.closed:
                    if not self.pending:
                        self.condition.wait();continue
                    key,(dataset,due)=min(self.pending.items(),key=lambda item:item[1][1])
                    delay=due-time.monotonic()
                    if delay>0:
                        self.condition.wait(delay);continue
                    self.pending.pop(key)
                    break
                else:return
            try:dataset.publish()
            except Exception as exc:
                dataset.last_error='随行档案待同步：'+str(exc)
                with self.condition:
                    if not self.closed:
                        self.pending[key]=(dataset,time.monotonic()+1)
                        self.condition.notify()

    def close(self):
        with self.condition:
            self.closed=True;self.condition.notify_all()
        if self.thread.is_alive():self.thread.join()
