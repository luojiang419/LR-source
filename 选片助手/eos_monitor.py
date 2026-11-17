"""只接受EOS传入的照片路径并开启现场监控；不读取或改写EOS配置。"""
from pathlib import Path
import threading
import media_engine as media
from file_safety import photo_directory


class EosMonitor:
    def __init__(self,store,watch):
        self.store=store;self.watch=watch;self.lock=threading.RLock();self.manual_pause=False

    def pause(self):
        with self.lock:self.manual_pause=True

    def receive(self,paths):
        if not isinstance(paths,list) or not 1<=len(paths)<=64:raise ValueError('请选择有效的EOS照片')
        files=[]
        for value in paths:
            if not isinstance(value,str) or not Path(value).is_absolute():raise ValueError('照片必须是完整路径')
            path=Path(value).resolve(strict=True)
            if path.suffix.lower() not in media.IMAGE_EXTS|media.RAW_EXTS or not path.is_file():raise ValueError('不支持的照片类型')
            photo_directory(path.parent,self.store.data);files.append(path)
        with self.lock:
            if self.manual_pause:return {'accepted':False,'reason':'已手动选择或暂停现场监控'}
            folder=files[-1].parent;state=self.watch.state()
            if not state['enabled'] or Path(state['folder']).resolve()!=folder:
                state=self.watch.start(str(folder),source='eos')
            elif state.get('source')!='eos':state=self.watch.set_source('eos')
            if self.store.active_id()!=state['session_id']:self.store.activate(state['session_id'])
            self.watch.notify_canon(files)
            return {'accepted':True,'session_id':state['session_id'],'folder':str(folder)}
