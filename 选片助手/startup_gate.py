"""先提供工作面；业务依赖和 EOS 只读预览互不等待。"""
import json
from pathlib import Path
import threading
import uuid


class StartupGate:
    def __init__(self, data, root):
        self.data=Path(data)
        self.root=Path(root)
        self.ready=threading.Event()
        self.capture_ready=threading.Event()
        self.closed=threading.Event()
        self.error=None
        self.lock=threading.Lock()
        self.photo=None
        self.arrivals={}
        self.arrival_callback=None
        self.capture_readonly=False

    def state(self):
        return {'ready':self.ready.is_set(),'capture_ready':self.capture_ready.is_set(), 'failed':self.error is not None,
                'error':'服务初始化失败，请重启并检查 engine.log' if self.error else None}

    def wait(self, timeout=180):
        return self.ready.wait(timeout) and not self.error and not self.closed.is_set()

    def wait_capture(self,timeout=180):
        return self.capture_ready.wait(timeout) and not self.closed.is_set()

    def capture_arrivals(self,sid,records):
        with self.lock:
            callback=self.arrival_callback
            if callback is None:
                self.arrivals.setdefault(sid,{}).update(dict.fromkeys(record['id'] for record in records))
                return
        callback(sid,records)

    def bind_arrivals(self,store,callback):
        with self.lock:
            pending=self.arrivals;self.arrivals={};self.arrival_callback=callback
        for sid,ids in pending.items():
            ids=list(ids)
            for offset in range(0,len(ids),128):callback(sid,store.records(sid,ids=ids[offset:offset+128]))

    def latest_photo(self):
        # 只接受桌面程序已持久化的通知，不接受 HTTP 传入任意文件路径。
        from file_safety import photo_directory
        import media_engine as media
        inbox=self.data/'canon-link/inbox'
        candidates=sorted(inbox.glob('*.json'),reverse=True)
        for notification in candidates[:64]:
            try:
                if notification.stat().st_size>65536:continue
                paths=json.loads(notification.read_text(encoding='utf8')).get('paths',[])
                if not isinstance(paths,list):continue
                for value in reversed(paths[:64]):
                    if not isinstance(value,str) or not Path(value).is_absolute():continue
                    path=Path(value).resolve(strict=True)
                    if path.suffix.lower() not in media.IMAGE_EXTS|media.RAW_EXTS or not path.is_file():continue
                    photo_directory(path.parent,self.data)
                    stamp=path.stat()
                    version=(str(path),stamp.st_mtime_ns,stamp.st_size)
                    with self.lock:
                        if not self.photo or self.photo['version']!=version:
                            self.photo={'version':version,'path':path,'id':uuid.uuid4().hex}
                        return {'id':self.photo['id'],'name':path.name,'folder':str(path.parent),
                                'preview':'/api/startup/photo?id='+self.photo['id']}
            except (OSError,ValueError,TypeError,AttributeError):continue
        return None

    def preview(self, identity):
        with self.lock:
            if not self.photo or self.photo['id']!=identity:raise ValueError('启动预览已更新')
            path=self.photo['path']
        from direct_previews import preview_bytes
        import media_engine as media
        raw=path.suffix.lower() in media.RAW_EXTS
        return preview_bytes({'raw' if raw else 'jpg':path.name},path.parent,lambda:media.find_exiftool(self.root),size=2048)
