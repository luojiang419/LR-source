"""新 JPG 原始文件通知；不读取像素，不做图片校验，不等待资料入库。"""
from collections import OrderedDict
import hashlib
from pathlib import Path
import threading
import time
import uuid

from directory_events import DirectoryEvents
from direct_jpeg_recovery import JpegNotificationRecovery,created_ns


class DirectJpegFeed:
    def __init__(self,store,*,autostart=True):
        self.store=store;self.condition=threading.Condition();self.closed=threading.Event();self.wake=threading.Event()
        self.paths=OrderedDict();self.by_path={};self.orders={};self.admitted=set();self.sequence=0
        self.info={'enabled':False,'folder':'','recursive':True,'session_id':None,'source':'manual','generation':'','event_id':0,'receiving':None,'latest':None,'last_photo_id':None,'notification_status':'disabled','notification_restarts':0,'enabled_at_ns':0,'recovered_notifications':0}
        self.thread=threading.Thread(target=self._run,name='direct-jpeg-events',daemon=True)
        self.recovery=JpegNotificationRecovery(self)
        if autostart:self.start()

    def start(self):
        if self.recovery.thread.ident is None:self.recovery.thread.start()
        if self.thread.ident is None:self.thread.start()

    def _update(self,**values):
        if any(self.info.get(key)!=value for key,value in values.items()):
            self.info.update(values);self.info['event_id']+=1;self.condition.notify_all()

    def configure(self,info):
        with self.condition:
            changed=info.get('generation')!=self.info['generation'] or info.get('folder','')!=self.info['folder']
            if changed:
                self.paths.clear();self.by_path.clear();self.orders.clear();self.admitted.clear();self.sequence=0
            values={key:info.get(key,self.info[key]) for key in ('enabled','folder','recursive','session_id','source','generation')}
            if changed or values['enabled'] and not self.info['enabled']:values.update(enabled_at_ns=time.time_ns(),recovered_notifications=0)
            if changed or not values['enabled']:values.update(receiving=None,latest=None,last_photo_id=None,notification_status='starting' if values['enabled'] else 'disabled',notification_restarts=0)
            recovery_changed=changed or values['enabled']!=self.info['enabled'] or values['recursive']!=self.info['recursive']
            self._update(**values);self.wake.set()
            if recovery_changed:self.recovery.wake.set()

    def remember_existing(self,records,generation):
        """复用正式监控已读取的历史记录，不能把旧图元数据变更当作新拍摄。"""
        known={r['id'] for r in records}
        known.update(hashlib.sha256(r['jpg'].casefold().encode()).hexdigest()[:24] for r in records if r.get('jpg'))
        with self.condition:
            if generation!=self.info['generation']:return
            self.admitted.update(known)
            if (self.info['receiving'] or {}).get('id') in known:
                self._update(receiving=None,latest=None)

    def state(self):
        with self.condition:
            result=dict(self.info)
            if result['receiving']:result['receiving']=dict(result['receiving'])
            if result['latest']:result['latest']=dict(result['latest'])
            return result

    def wait_state(self,after,timeout=15):
        with self.condition:
            self.condition.wait_for(lambda:self.closed.is_set() or self.info['event_id']!=after,timeout)
            return self.state()

    def announce(self,path,*,generation=None):
        path=Path(path)
        if path.suffix.lower() not in ('.jpg','.jpeg'):return False
        with self.condition:
            if self.closed.is_set() or not self.info['enabled']:return False
            if generation is not None and generation!=self.info['generation']:return False
            root=Path(self.info['folder'])
            try:relative=path.relative_to(root).as_posix()
            except ValueError:return False
            if '..' in Path(relative).parts:return False
            # 仅沿用现有的文件名编号算法；不读取或散列照片内容。
            cid=hashlib.sha256(relative.casefold().encode()).hexdigest()[:24]
            if cid in self.admitted:return False
            path_key=str(path).casefold()
            if path_key not in self.orders:self.sequence+=1;self.orders[path_key]=self.sequence
            order=self.orders[path_key]
            key=self.by_path.get(path_key)
            if key is None:
                key=uuid.uuid4().hex;self.by_path[path_key]=key
                self.paths[key]={'path':path,'root':root,'id':cid,'order':order,'bytes':0,'revision':0}
                while len(self.paths)>8:
                    _,old=self.paths.popitem(last=False);self.by_path.pop(str(old['path']).casefold(),None)
            current=self.info['receiving']
            if current and current['key']==key:return True
            if order<self.sequence:return True
            entry=self.paths[key]
            self._update(receiving={'id':cid,'name':path.name,'key':key,'url':f"/api/watch/preview/{self.info['generation']}/{key}",
                                    'revision':entry['revision'],'bytes':entry['bytes']},latest=None,last_photo_id=cid)
            self.wake.set();return True

    def recover(self,path,birth,info):
        with self.condition:
            latest=max(self.paths.values(),key=lambda entry:entry['order'],default=None)
            if str(path).casefold() in self.by_path:return
        # 漏报的旧图不能盖住已通知的新图；读属性不占用通知锁。
        try:
            if latest and birth<created_ns(latest['path'].stat()):return
        except OSError:pass
        with self.condition:
            if not self.recovery.valid(info):return
            if latest is not max(self.paths.values(),key=lambda entry:entry['order'],default=None):return
            if self.announce(path,generation=info['generation']):self._update(recovered_notifications=self.info['recovered_notifications']+1)

    def order(self,path):
        with self.condition:return self.orders.get(str(Path(path)).casefold(),0)

    def source_path(self,generation,key):
        with self.condition:
            if self.closed.is_set() or not self.info['enabled'] or generation!=self.info['generation']:return None
            entry=self.paths.get(key)
            return entry['path'] if entry else None

    def source_root(self,generation,key):
        with self.condition:
            if self.closed.is_set() or not self.info['enabled'] or generation!=self.info['generation']:return None
            entry=self.paths.get(key)
            return entry['root'] if entry else None

    def acknowledge(self,ids,aliases=None,latest_id=None):
        with self.condition:
            aliases=aliases or {};self.admitted.update(ids);self.admitted.update(aliases);current=self.info['receiving']
            if current and (current['id'] in self.admitted or current['id'] in aliases):
                formal=aliases.get(current['id'],current['id'])
                # 瞬间写完并入库的 JPG 也要保留可呈现地址，迟到的浏览器查询不能漏掉它。
                self._update(receiving=None,latest=dict(current,formal_id=formal),last_photo_id=formal)
            elif current is None and latest_id and latest_id!=self.info['last_photo_id']:
                self._update(latest=None,last_photo_id=latest_id)

    def _progress(self,key,count,*,modified=False):
        with self.condition:
            entry=self.paths.get(key);current=self.info['receiving']
            if not entry or not current or current['key']!=key:return
            if count==entry['bytes'] and not modified:return
            entry['bytes']=count;entry['revision']+=1
            self._update(receiving=dict(current,bytes=count,revision=entry['revision']))

    def _run(self):
        signal=None;generation=None;opened=None;opened_key=None;retry_at=0
        try:
            while not self.closed.is_set():
                info=self.state()
                if info['generation']!=generation or not info['enabled']:
                    if signal:signal.close()
                    if opened:opened.close()
                    signal=None;opened=None;opened_key=None;generation=info['generation'];retry_at=0
                if info['enabled'] and signal is not None and signal.handle is None:
                    with self.condition:
                        if generation==self.info['generation'] and self.info['enabled']:self._update(notification_status='retrying')
                if info['enabled'] and (signal is None or signal.handle is None) and time.monotonic()>=retry_at:
                    restarting=signal is not None
                    if signal:signal.close()
                    signal=DirectoryEvents(info['folder'],info['recursive']);retry_at=time.monotonic()+.5
                    self.recovery.wake.set()
                    with self.condition:
                        if generation==self.info['generation'] and self.info['enabled']:
                            self._update(notification_status='active' if signal.handle is not None else 'retrying',
                                         notification_restarts=self.info['notification_restarts']+int(restarting))
                modified_keys=set()
                if signal and signal.changed():
                    if not signal.changes:self.recovery.wake.set()  # 溢出/无名通知须补回丢失文件。
                    for action,path in signal.changes or ():
                        path_key=str(path).casefold()
                        if action in (1,5):self.announce(path,generation=generation)
                        elif action==3:
                            with self.condition:key=self.by_path.get(path_key)
                            if key:modified_keys.add(key)
                            else:self.announce(path,generation=generation)  # 缺少新增通知时也接入新 JPG，不等待全目录补偿扫描。
                current=self.state()['receiving']
                if (current or {}).get('key')!=opened_key:
                    if opened:opened.close()
                    opened=None;opened_key=(current or {}).get('key')
                if current:
                    if opened is None:
                        path=self.source_path(generation,current['key'])
                        try:
                            # 字节数只用于传输进度；不判断文件完整性、稳定性或写句柄。
                            if path:opened=path.open('rb')
                        except OSError:pass
                    if opened:
                        try:opened.seek(0,2);self._progress(current['key'],opened.tell(),modified=current['key'] in modified_keys)
                        except OSError:opened.close();opened=None
                    elif current['key'] in modified_keys:self._progress(current['key'],current['bytes'],modified=True)
                if signal and signal.handle is not None and hasattr(signal,'wait'):
                    if not self.wake.is_set():signal.wait(25)
                else:self.wake.wait(.025)
                self.wake.clear()
        finally:
            if opened:opened.close()
            if signal:signal.close()

    def close(self):
        self.closed.set();self.wake.set();self.recovery.wake.set()
        with self.condition:self._update(enabled=False,receiving=None,latest=None,notification_status='disabled');self.condition.notify_all()
        if self.thread.is_alive():self.thread.join(timeout=2)
        if self.recovery.thread.is_alive():self.recovery.thread.join(timeout=2)
