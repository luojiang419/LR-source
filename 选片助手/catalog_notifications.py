"""合并联机目录通知；后台业务消费者不阻塞照片入库与状态长轮询。"""
import threading


class CatalogNotifications:
    MAX_RECORDS=256

    def __init__(self,callback,*,autostart=True):
        self.callback=callback;self.condition=threading.Condition();self.pending={}
        self.closed=False;self.active=False;self.error=''
        self.thread=threading.Thread(target=self._run,name='live-catalog-notify',daemon=True)
        if autostart:self.thread.start()

    def request(self,sid,records=None):
        with self.condition:
            if self.closed:return
            if sid not in self.pending:self.pending[sid]={}
            current=self.pending[sid]
            if records is None:self.pending[sid]=None
            elif current is not None:
                current.update((record['id'],dict(record)) for record in records)
                # 大批积压退为完整通知，不丢最后状态，也不无限保留历史照片对象。
                if len(current)>self.MAX_RECORDS:self.pending[sid]=None
            self.condition.notify_all()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.closed or self.pending)
                if self.closed:return
                sid=next(iter(self.pending));records=self.pending.pop(sid);self.active=True
            try:
                self.callback(sid,None if records is None else list(records.values()))
                self.error=''
            except Exception as exc:
                # 提交已成功；消费者失败仍须重试，不能吞掉最后一张通知。
                self.error=str(exc)[:250]
                with self.condition:
                    self.pending[sid]=None
                    self.condition.wait_for(lambda:self.closed,timeout=.5)
            finally:
                with self.condition:self.active=False;self.condition.notify_all()

    def wait_idle(self,timeout=3):
        with self.condition:
            return self.condition.wait_for(lambda:not self.pending and not self.active,timeout)

    def close(self):
        self.wait_idle(timeout=1)
        with self.condition:self.closed=True;self.condition.notify_all()
        if self.thread.is_alive():self.thread.join(timeout=1)
