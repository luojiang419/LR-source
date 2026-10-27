"""照片传入目录监控：等待写完、增量入库、断线重试；不改写原片。"""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import threading
import time
import uuid

from PIL import Image
import media_engine as media
from file_safety import atomic_write,photo_directory


def signature(paths):
    from rating_xmp import sidecar_signature
    return tuple((str(p),p.stat().st_size,p.stat().st_mtime_ns) for p in paths)+sidecar_signature(paths)


def files_ready(paths):
    for path in paths:
        if os.name=='nt':
            kernel=C.WinDLL('kernel32',use_last_error=True)
            kernel.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
            kernel.CreateFileW.restype=W.HANDLE;kernel.CloseHandle.argtypes=[W.HANDLE]
            # 排除仍被写入的文件：允许其他读者，不允许并存写句柄。
            handle=kernel.CreateFileW(str(path),0x80000000,1,None,3,0,None)
            if handle==C.c_void_p(-1).value:return False
            kernel.CloseHandle(handle)
        if path.suffix.lower() in ('.jpg','.jpeg','.png','.tif','.tiff'):
            try:
                with Image.open(path) as image:
                    if image.format=='JPEG':
                        # verify() 不检查 JPEG 像素流，分段传图关闭句柄后仍可能是半张图。
                        image.draft('RGB',(160,160));image.load()
                    else:image.verify()
            except (OSError,ValueError,SyntaxError):return False
    return True


class DirectorySignal:
    """Windows 目录变化通知；不支持的卷/离线目录由定时重扫兜底。"""
    def __init__(self,folder,recursive):
        self.handle=None
        if os.name!='nt':return
        self.kernel=C.WinDLL('kernel32',use_last_error=True)
        self.kernel.FindFirstChangeNotificationW.argtypes=[W.LPCWSTR,W.BOOL,W.DWORD]
        self.kernel.FindFirstChangeNotificationW.restype=W.HANDLE
        self.kernel.FindNextChangeNotification.argtypes=[W.HANDLE]
        self.kernel.FindNextChangeNotification.restype=W.BOOL
        self.kernel.FindCloseChangeNotification.argtypes=[W.HANDLE]
        self.kernel.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD]
        handle=self.kernel.FindFirstChangeNotificationW(str(folder),recursive,0x1|0x2|0x8|0x10)
        if handle not in (None,C.c_void_p(-1).value):self.handle=handle

    def changed(self):
        if self.handle is None:return False
        status=self.kernel.WaitForSingleObject(self.handle,0)
        if status==0:
            if not self.kernel.FindNextChangeNotification(self.handle):self.close()
            return True
        if status==0xffffffff:self.close()
        return False

    def close(self):
        if self.handle is not None:
            self.kernel.FindCloseChangeNotification(self.handle);self.handle=None


class FolderWatch:
    INTERVAL=.08
    STABLE_SECONDS=.08
    RESCAN_SECONDS=2.0

    def __init__(self,store,*,clock=time.monotonic,autostart=True):
        self.store=store;self.clock=clock;self.lock=threading.RLock();self.tick_lock=threading.Lock()
        self.condition=threading.Condition(self.lock);self.wake=threading.Event();self.metadata=None;self.existing=None
        self.closed=threading.Event();self.cancel=threading.Event();self.observed={};self.accepted={};self.retry_after={}
        self.config=store.data/'folder-watch.json'
        self.info={'enabled':False,'folder':'','recursive':True,'session_id':None,'generation':uuid.uuid4().hex,
                   'version':0,'event_id':0,'canon_event':'','arrivals':0,'last_photo_id':None,'pending':0,'initializing':False,'error':'','message':'未开启现场监控','source':'manual'}
        if self.config.is_file():
            try:
                saved=json.loads(self.config.read_text(encoding='utf8'))
                if saved.get('enabled') and saved.get('source')=='canon':
                    self.info.update(folder=saved['folder'],recursive=bool(saved.get('recursive',True)),session_id=saved['session_id'],source='canon',message='联机监控等待手动开启')
                elif saved.get('enabled'):
                    photo_directory(saved['folder'],self.store.data)
                    self.info.update(enabled=True,folder=saved['folder'],recursive=bool(saved.get('recursive',True)),session_id=saved['session_id'],source=saved.get('source','manual'),initializing=True,message='正在恢复现场监控并读取已有照片')
            except (OSError,ValueError,KeyError):self.info['error']='监控配置读取失败，请重新选择文件夹'
        if self.info['enabled'] and self.info['session_id']:
            with self.store.connect() as c:c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('shooting_session:'+self.info['session_id'],self.info['folder']))
        self.thread=threading.Thread(target=self._run,name='folder-watch',daemon=True)
        if autostart:self.thread.start()

    def state(self):
        with self.lock:return dict(self.info)

    def wait_state(self,after,timeout=15):
        with self.condition:
            self.condition.wait_for(lambda:self.closed.is_set() or self.info['event_id']!=after,timeout)
            return dict(self.info)

    def _update(self,**values):
        # 调用方持有 lock；状态无变化时不唤醒客户端，也不发送整库刷新。
        if any(self.info.get(k)!=v for k,v in values.items()):
            self.info.update(values);self.info['event_id']+=1;self.condition.notify_all()

    def _persist(self):
        payload={k:self.info[k] for k in ('enabled','folder','recursive','session_id','source')}
        atomic_write(self.config,json.dumps(payload,ensure_ascii=False).encode('utf8'))

    def set_source(self,source):
        with self.lock:self._update(source=source);self._persist();return self.state()

    def notify_canon(self):
        # 即使照片已入库，也通知当前界面进入联机；不重建会话或持久化通知。
        with self.lock:
            if self.info['enabled'] and self.info['source']=='canon':self._update(canon_event=uuid.uuid4().hex)

    def start(self,folder,recursive=True,*,source='manual'):
        if not isinstance(folder,str) or not folder.strip():raise ValueError('请选择照片传入文件夹。')
        root=Path(folder.strip().strip('"')).resolve(strict=True)
        photo_directory(root,self.store.data)
        for job in self.store.folder_state()['scans']:
            if job['status'] in ('queued','running','cancelling') and str(root).casefold()==str(Path(job['folder'])).casefold():
                raise ValueError('此文件夹正在索引，请等待索引结束后开启现场监控。')
        sid=self.store.ensure_live_session(root)
        with self.store.connect() as c:c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",('shooting_session:'+sid,str(root)))
        with self.lock:
            self.cancel.set();self.cancel=threading.Event();self.observed={};self.accepted={};self.retry_after={};self.existing=None
            self._update(enabled=True,folder=str(root),recursive=bool(recursive),session_id=sid,source=source,
                             generation=uuid.uuid4().hex,canon_event=self.info['canon_event'] if source=='canon' else '',version=0,arrivals=0,last_photo_id=None,pending=0,initializing=True,error='',message='正在读取已有照片，原文件保留')
            self._persist();self.wake.set()
            return self.state()

    def stop(self):
        with self.lock:
            self.cancel.set();self._update(enabled=False,pending=0,initializing=False,message='已停止监控，原文件、已入库照片和评级保留')
            self._persist();self.wake.set();return self.state()

    def tick(self):
        # 独立工作线程串行读取；页面轮询不会启动额外目录扫描。
        with self.tick_lock:
            with self.lock:
                if not self.info['enabled']:return
                info=dict(self.info);cancel=self.cancel;observed=self.observed;accepted=self.accepted;retry_after=self.retry_after
            try:
                photo_directory(info['folder'],self.store.data)
                root,shots=media.discover(info['folder'],info['recursive'],cancel=cancel,exclude=(self.store.data,))
                if not self.store.session(info['session_id']):self.store.ensure_live_session(root)
                if self.existing is None:
                    self.existing={path.casefold():r for r in self.store.records(info['session_id']) for path in (r.get('raw'),r.get('jpg')) if path}
                existing=self.existing
                ready=[];pending=0;current=set()
                for primary,companion in shots:
                    if cancel.is_set():return
                    paths=[primary]+([companion] if companion else []);key=str(primary);current.add(key)
                    try:sig=signature(paths)
                    except OSError:pending+=1;continue
                    if not all(size>0 for _,size,_ in sig):pending+=1;continue
                    if accepted.get(key)==sig:continue
                    if self.clock()<retry_after.get(key,0):pending+=1;continue
                    rel=primary.relative_to(root).as_posix().casefold();old=existing.get(rel)
                    from rating_xmp import sidecar_fingerprint
                    if old and old['fingerprint']==media.fingerprint(paths) and old.get('sidecarFingerprint')==sidecar_fingerprint(paths):accepted[key]=sig;continue
                    previous=observed.get(key)
                    if previous is None or previous[0]!=sig:
                        observed[key]=(sig,self.clock());pending+=1;continue
                    if self.clock()-previous[1]<self.STABLE_SECONDS or not files_ready(paths):pending+=1;continue
                    ready.append((primary,companion,sig))
                for key in list(observed):
                    if key not in current:observed.pop(key,None);accepted.pop(key,None)
                # 每批最多 24 张，坏文件按条重试，不挡住同批其他照片。
                records=[];completed=[];errors=[]
                if ready:
                    # 新文件优先于失败重试，避免一批坏 RAW 持续饿死后来的正常文件。
                    batch=sorted(ready,key=lambda x:(str(x[0]) in retry_after,max(s[2] for s in x[2]),str(x[0])))[:24]
                    exiftool=media.find_exiftool(self.store.root)
                    if self.metadata is None:self.metadata=media.ExifToolSession(exiftool)
                    meta=media.read_metadata([p for p,_,_ in batch],exiftool,cancel=cancel,session=self.metadata)
                    for (primary,companion,sig),m in zip(batch,meta):
                        paths=[primary]+([companion] if companion else [])
                        if m.get('Error') or sig!=signature(paths):
                            retry_after[str(primary)]=self.clock()+.3
                            errors.append(primary.name+' 尚未读取完成，正在重试');pending+=1;continue
                        records.append(media.capture_record(root,primary,companion,m));completed.append((str(primary),sig))
                    pending+=max(0,len(ready)-len(batch))
                with self.lock:
                    if cancel.is_set() or info['generation']!=self.info['generation']:return
                    if records:
                        result=self.store.append_live(info['session_id'],records)
                        for key,sig in completed:accepted[key]=sig;retry_after.pop(key,None)
                        for record in records:
                            for path in (record.get('raw'),record.get('jpg')):
                                if path:existing[path.casefold()]=record
                        values={}
                        if result['changed']:values['version']=self.info['version']+1
                        if result['added']:
                            values.update(arrivals=self.info['arrivals']+len(result['added']),last_photo_id=result['added'][-1])
                        self._update(**values)
                    initializing=self.info['initializing'] and pending>0
                    message=(f'正在读取已有照片 · 待处理 {pending} 张' if initializing else f'正在监控 · 本次载入 {self.info["arrivals"]} 张'+(f' · 等待写完 {pending} 张' if pending else ''))
                    self._update(pending=pending,initializing=initializing,error='；'.join(errors[:2]),message=message)
            except media.ScanCancelled:pass
            except Exception as exc:
                with self.lock:
                    if info['generation']==self.info['generation'] and not cancel.is_set():
                        self._update(error=str(exc)[:250],message='目录或照片暂不可用，正在自动重试')

    def _run(self):
        signal=None;generation=None;next_scan=0
        try:
            while not self.closed.is_set():
                state=self.state();now=self.clock()
                if state['generation']!=generation:
                    if signal:signal.close()
                    signal=DirectorySignal(state['folder'],state['recursive']) if state['enabled'] else None
                    generation=state['generation'];next_scan=0
                notified=signal.changed() if signal else False
                if state['enabled'] and (notified or now>=next_scan):
                    self.tick();state=self.state()
                    # 有待写完/待处理文件时短间隔重试；空闲时只响应内核通知和补偿扫描。
                    delay=self.INTERVAL if state['pending'] or not signal or signal.handle is None else self.RESCAN_SECONDS
                    next_scan=self.clock()+delay
                    if signal and signal.handle is None:signal=DirectorySignal(state['folder'],state['recursive'])
                if not state['enabled'] and self.metadata:self.metadata.close()
                self.wake.wait(.025);self.wake.clear()
        finally:
            if signal:signal.close()
            if self.metadata:self.metadata.close()

    def close(self):
        self.closed.set();self.cancel.set();self.wake.set()
        with self.condition:self.condition.notify_all()
        if self.thread.is_alive():self.thread.join(timeout=3)
        if self.metadata:self.metadata.close()
