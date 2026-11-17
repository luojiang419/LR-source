"""归档进度的单任务后台快照，HTTP 轮询不重复聚合整库或等待缓存写入。"""
import threading
import time


class CatalogStatus:
    def __init__(self, catalog):
        self.catalog = catalog
        self.lock = threading.Lock()
        self.worker = None
        self.updated = 0
        self.error = ''
        self.value = None
        self.preview_value = None
        self.preview_worker = None
        self.preview_updated = 0
        self.preview_error = ''
        self.closed = False

    def close(self):
        with self.lock:
            self.closed = True
            worker = self.worker
            preview_worker = self.preview_worker
        if worker:worker.join(timeout=3)
        if preview_worker:preview_worker.join(timeout=3)

    def snapshot(self, previews=None):
        with self.lock:
            running = bool(self.worker and self.worker.is_alive())
            if not self.closed and not running and time.monotonic() - self.updated >= 2:
                self.worker = threading.Thread(target=self._refresh,
                                               name='catalog-status', daemon=True)
                self.worker.start()
                running = True
            if previews and not self.closed and not (self.preview_worker and self.preview_worker.is_alive()) and time.monotonic() - self.preview_updated >= 2:
                self.preview_worker = threading.Thread(target=self._refresh_previews, args=(previews,),
                                                      name='catalog-decode-status', daemon=True)
                self.preview_worker.start()
            result={**(self.value or {'roots': [], 'counts': {}, 'metadata': {}}),
                    'initialized': self.value is not None, 'updating': running,
                    'status_error': self.error, 'previews_initialized': self.preview_value is not None and self.preview_value.get('coverage_initialized',True),
                    'preview_status_error': self.preview_error}
            if self.preview_value is not None:result['previews']=dict(self.preview_value)
        # 整库核对变慢时也返回本进程实时完成数、实际并发和暂停状态。
        # 累计 ready/total 仍采用已核对的快照，不伪造增量总数。
        if previews and hasattr(previews,'_decode_timing'):
            progress=dict(result.get('previews') or {'total':0,'ready':0,'pending':0,'failed':0,'checking':True})
            result['previews']=previews._decode_timing(progress,None)
        return result

    def _refresh_previews(self, previews):
        try:
            value = previews.archive_state(persisted=True)
            with self.lock:
                self.preview_value = value
                self.preview_error = ''
        except Exception as error:
            with self.lock:self.preview_error = str(error)[:300]
        finally:
            with self.lock:self.preview_updated = time.monotonic()

    def _refresh(self):
        try:
            value = self.catalog.state()
            with self.lock:
                self.value = value
                self.error = ''
        except Exception as error:
            with self.lock:
                self.error = str(error)[:300]
        finally:
            with self.lock:
                self.updated = time.monotonic()
