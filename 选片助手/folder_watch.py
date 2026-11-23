"""照片传入目录监控：等待写完、增量入库、断线重试；不改写原片。"""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import threading
import time
import uuid
import hashlib
from collections import deque

from PIL import Image
import media_engine as media
from file_safety import atomic_write,photo_directory
from direct_jpeg import DirectJpegFeed


def signature(paths,stat_snapshot=None):
    from rating_xmp import sidecar_signature
    if stat_snapshot is not None:
        # 同次 scandir 已拿到文件和侧车属性；不能为每张历史照片再探测两次不存在的 XMP。
        def item(path):
            stat=stat_snapshot[os.path.normcase(str(path))]
            return (str(path),stat.st_size,stat.st_mtime_ns)
        sides=dict.fromkeys(side for path in paths for side in (path.with_suffix('.xmp'),Path(str(path)+'.xmp')))
        return tuple(item(path) for path in paths)+tuple(item(side) for side in sides if os.path.normcase(str(side)) in stat_snapshot)
    return tuple((str(p),p.stat().st_size,p.stat().st_mtime_ns) for p in paths)+sidecar_signature(paths)


def files_ready(paths,*,previews=None):
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
                        keep=previews is not None and len(previews)<24
                        orientation=image.getexif().get(274) if keep else None
                        image.draft('RGB',(480,480) if keep else (160,160));image.load()
                        if keep:previews[str(path)]=(image.convert('RGB'),orientation)
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
    INTERVAL=.025
    STABLE_SECONDS=.08
    RESCAN_SECONDS=2.0

    def __init__(self,store,*,clock=time.monotonic,autostart=True,restore=True):
        self.store=store;self.clock=clock;self.lock=threading.RLock();self.tick_lock=threading.Lock()
        self.on_arrivals=None
        self.condition=threading.Condition(self.lock);self.wake=threading.Event();self.metadata=None;self.existing=None
        self.closed=threading.Event();self.cancel=threading.Event();self.observed={};self.accepted={};self.retry_after={};self.changes=deque(maxlen=128);self.hints={}
        self.direct=DirectJpegFeed(store,autostart=False)
        self.incoming_order={};self.incoming_sequence=0;self.last_ready_order=0;self.initial_paths=None;self.initial_target=None;self.initial_positions={}
        self.config=store.data/'folder-watch.json'
        self.info={'enabled':False,'folder':'','recursive':True,'session_id':None,'generation':uuid.uuid4().hex,
                   'version':0,'event_id':0,'canon_event':'','arrivals':0,'last_photo_id':None,'pending':0,'initializing':False,'error':'','message':'未开启现场监控','source':'manual','receiving':None}
        if restore and self.config.is_file():
            try:
                saved=json.loads(self.config.read_text(encoding='utf8'))
                if saved.get('enabled'):
                    photo_directory(saved['folder'],self.store.data)
                    self.info.update(enabled=True,folder=saved['folder'],recursive=bool(saved.get('recursive',True)),session_id=saved['session_id'],generation=saved.get('generation',self.info['generation']),source='manual',initializing=True,message='正在恢复现场监控并读取已有照片')
            except (OSError,ValueError,KeyError):self.info['error']='监控配置读取失败，请重新选择文件夹'
        if self.info['enabled'] and self.info['session_id']:
            with self.store.connect() as c:c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('shooting_session:'+self.info['session_id'],self.info['folder']))
        self.direct.configure(self.info)
        self.thread=threading.Thread(target=self._run,name='folder-watch',daemon=True)
        if autostart:self.thread.start()

    def state(self):
        with self.lock:return dict(self.info)

    def _receiving(self,info,path,cancel):
        if not cancel.is_set() and info['generation']==self.direct.state()['generation']:self.direct.announce(path)

    def wait_state(self,after,timeout=15):
        with self.condition:
            self.condition.wait_for(lambda:self.closed.is_set() or self.info['event_id']!=after,timeout)
            return dict(self.info)

    def delta(self,sid,generation,after):
        with self.lock:
            if sid!=self.info['session_id'] or generation!=self.info['generation'] or not self.info['enabled']:return None
            if not 0<=after<=self.info['version']:return None
            if after<self.info['version'] and (not self.changes or after<self.changes[0][0]-1):return None
            return list(dict.fromkeys(cid for version,ids in self.changes if version>after for cid in ids))

    def _update(self,**values):
        # 调用方持有 lock；状态无变化时不唤醒客户端，也不发送整库刷新。
        if any(self.info.get(k)!=v for k,v in values.items()):
            self.info.update(values);self.info['event_id']+=1;self.condition.notify_all()

    def _persist(self):
        payload={k:self.info[k] for k in ('enabled','folder','recursive','session_id','source','generation')}
        atomic_write(self.config,json.dumps(payload,ensure_ascii=False).encode('utf8'))

    def set_source(self,source):
        with self.lock:self._update(source=source);self.direct.configure(self.info);self._persist();return self.state()

    def notify_canon(self,paths=()):
        # 即使照片已入库，也通知当前界面进入联机；不重建会话或持久化通知。
        for path in paths:self.direct.announce(path)
        with self.lock:
            if self.info['enabled'] and self.info['source'] in ('canon','eos'):
                root=Path(self.info['folder'])
                for path in paths:
                    path=Path(path)
                    if path.is_relative_to(root):self.hints[path]=None
                while len(self.hints)>128:self.hints.pop(next(iter(self.hints)))
                self._update(canon_event=uuid.uuid4().hex);self.wake.set()

    def start(self,folder,recursive=True,*,source='manual'):
        if not isinstance(folder,str) or not folder.strip():raise ValueError('请选择照片传入文件夹。')
        root=Path(folder.strip().strip('"')).resolve(strict=True)
        photo_directory(root,self.store.data)
        with self.store.lock:scans=[dict(job) for job in self.store.jobs.values()]
        for job in scans:
            if job['status'] in ('queued','running','cancelling') and str(root).casefold()==str(Path(job['folder'])).casefold():
                raise ValueError('此文件夹正在索引，请等待索引结束后开启现场监控。')
        sid=self.store.ensure_live_session(root)
        if getattr(self.store,'capture_service',None):self.store.capture_service.prepare_live(sid)
        with self.store.connect() as c:c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",('shooting_session:'+sid,str(root)))
        with self.lock:
            self.cancel.set();self.cancel=threading.Event();self.observed={};self.accepted={};self.retry_after={};self.existing=None;self.changes.clear();self.hints.clear()
            self.incoming_order.clear();self.incoming_sequence=0;self.last_ready_order=0;self.initial_paths=None;self.initial_target=None;self.initial_positions={}
            self._update(enabled=True,folder=str(root),recursive=bool(recursive),session_id=sid,source=source,
                             generation=uuid.uuid4().hex,canon_event=self.info['canon_event'] if source in ('canon','eos') else '',version=0,arrivals=0,last_photo_id=None,pending=0,initializing=True,error='',message='正在读取已有照片，原文件保留',receiving=None)
            self._persist();self.wake.set()
            self.direct.configure(self.info)
            return self.state()

    def stop(self):
        with self.lock:
            self.cancel.set();self._update(enabled=False,pending=0,initializing=False,receiving=None,message='已停止监控，原文件、已入库照片和评级保留')
            self.direct.configure(self.info)
            self._persist();self.wake.set();return self.state()

    def tick(self,paths=None):
        # 独立工作线程串行读取；页面轮询不会启动额外目录扫描。
        partial=paths is not None
        with self.tick_lock:
            with self.lock:
                if not self.info['enabled']:return
                info=dict(self.info);cancel=self.cancel;observed=self.observed;accepted=self.accepted;retry_after=self.retry_after
                previous_pending={key for key,value in observed.items() if accepted.get(key)!=value[0]}
            try:
                photo_directory(info['folder'],self.store.data)
                stat_snapshot={}
                root,shots=media.discover(info['folder'],info['recursive'],cancel=cancel,exclude=(self.store.data,),stat_snapshot=stat_snapshot,only_paths=paths)
                if not self.store.session(info['session_id']):self.store.ensure_live_session(root)
                if self.existing is None:
                    self.existing={path.casefold():r for r in self.store.records(info['session_id']) for path in (r.get('raw'),r.get('jpg')) if path}
                    self.direct.remember_existing(list(self.existing.values()),info['generation'])
                existing=self.existing
                # 首次完整枚举只选一个末张目标，历史回填不发布逐图接收事件。
                with self.lock:
                    if cancel.is_set() or info['generation']!=self.info['generation']:return
                    if self.initial_paths is None and not partial:
                        self.initial_paths={str(p) for p,c in shots if not any(self.direct.order(path) for path in (p,c) if path)}
                        self.initial_target=str(shots[-1][0]) if shots else None
                        # 预留历史原顺序的位置；末张先入库也不能改变翻图与区间顺序。
                        with self.store.connect() as c:
                            position=c.execute('SELECT coalesce(max(position),-1)+1 FROM browser_captures WHERE session=?',(info['session_id'],)).fetchone()[0]
                        unseen=[p for p,c in shots if str(p) in self.initial_paths and p.relative_to(root).as_posix().casefold() not in existing]
                        self.initial_positions={hashlib.sha256(p.relative_to(root).as_posix().casefold().encode()).hexdigest()[:24]:position+i for i,p in enumerate(unseen)}
                        if shots and not self.direct.state()['last_photo_id']:
                            primary,companion=shots[-1]
                            previous=existing.get(primary.relative_to(root).as_posix().casefold())
                            if previous:self._update(last_photo_id=previous['id'])
                            else:self.direct.announce(companion or primary)
                    initial_paths=self.initial_paths or set();initial_target=self.initial_target
                ready=[];pending=0;current=set();previews={}
                for primary,companion in shots:
                    if cancel.is_set():return
                    paths=[primary]+([companion] if companion else []);key=str(primary);current.add(key)
                    rel=primary.relative_to(root).as_posix().casefold();old=existing.get(rel)
                    if not old and key not in self.incoming_order:
                        if key in initial_paths:
                            self.incoming_order[key]=0 if key==initial_target else -1
                        else:
                            self.direct.announce(primary)
                            self.incoming_sequence=max(self.incoming_sequence+1,self.direct.order(primary));self.incoming_order[key]=self.direct.order(primary) or self.incoming_sequence
                        for finished in list(self.incoming_order) if len(self.incoming_order)>1024 else ():
                            if len(self.incoming_order)<=1024:break
                            if finished in accepted:self.incoming_order.pop(finished,None)
                    try:sig=signature(paths,stat_snapshot)
                    except OSError:pending+=1;continue
                    if not all(size>0 for _,size,_ in sig):
                        # 目录属性可能仍为 0；原始图像通知不依赖这个属性。
                        if key not in observed or observed[key][0]!=sig:observed[key]=(sig,self.clock())
                        if key not in initial_paths and len(paths)==1 and primary.relative_to(root).as_posix().casefold() not in existing:self._receiving(info,primary,cancel)
                        pending+=1;continue
                    if accepted.get(key)==sig:continue
                    if self.clock()<retry_after.get(key,0):pending+=1;continue
                    # 同次枚举已经给出全部侧车属性，NAS 首次接续不能逐图探测不存在的 XMP。
                    side_fingerprint=hashlib.sha256(json.dumps(sig[len(paths):],ensure_ascii=False).encode()).hexdigest()
                    if old and old['fingerprint']==media.fingerprint(paths) and old.get('sidecarFingerprint')==side_fingerprint:accepted[key]=sig;continue
                    # 回退扫描也只提交文件名；直接图像通道不读取/校验像素。
                    if not old and key not in initial_paths and len(paths)==1:self._receiving(info,primary,cancel)
                    previous=observed.get(key)
                    if previous is None or previous[0]!=sig:
                        observed[key]=(sig,self.clock())
                        # 完整 JPEG 且写句柄已释放时立即处理；RAW/其他格式仍等待稳定。
                        if len(paths)!=1 or primary.suffix.lower() not in ('.jpg','.jpeg') or not files_ready(paths,previews=previews):
                            if not old and key not in initial_paths:self._receiving(info,primary,cancel)
                            pending+=1;continue
                    elif self.clock()-previous[1]<self.STABLE_SECONDS or not files_ready(paths,previews=previews):
                        if not old and key not in initial_paths:self._receiving(info,primary,cancel)
                        pending+=1;continue
                    ready.append((primary,companion,sig))
                for key in list(observed) if not partial else ():
                    if key not in current:observed.pop(key,None);accepted.pop(key,None)
                # 每批最多 24 张，坏文件按条重试，不挡住同批其他照片。
                records=[];completed=[];errors=[]
                if ready:
                    # 真正新图与初始末张先于历史回填；失败重试不能饿死正常文件。
                    batch=sorted(ready,key=lambda x:(str(x[0]) in retry_after,-self.incoming_order.get(str(x[0]),0) if info['initializing'] else 0,max(s[2] for s in x[2]),str(x[0])))[:24]
                    exiftool=media.find_exiftool(self.store.root)
                    if self.metadata is None:self.metadata=media.ExifToolSession(exiftool)
                    meta=media.read_metadata([p for p,_,_ in batch],exiftool,cancel=cancel,session=self.metadata)
                    for (primary,companion,sig),m in zip(batch,meta):
                        paths=[primary]+([companion] if companion else [])
                        if m.get('Error') or sig!=signature(paths):
                            retry_after[str(primary)]=self.clock()+.3
                            errors.append(primary.name+' 尚未读取完成，正在重试');pending+=1;continue
                        record=media.capture_record(root,primary,companion,m)
                        if not record.get('raw') and str(primary) in previews:
                            try:media.remember_quick_preview(record,root,*previews[str(primary)])
                            except (OSError,ValueError):pass  # 加速失败仍可使用按需预览。
                        records.append(record);completed.append((str(primary),sig))
                    pending+=max(0,len(ready)-len(batch))
                # 在 watch 锁外登记文档；SKU/归档不得与状态查询形成反向锁等待。
                if records and getattr(self.store,'lan_sync_data',None) and not cancel.is_set():
                    self.store.lan_sync_data.prime_files(root,records)
                if records and self.on_arrivals and not cancel.is_set():
                    self.on_arrivals(info['session_id'],[r for r in records if not any(existing.get(path.casefold()) for path in (r.get('raw'),r.get('jpg')) if path)])
                with self.lock:
                    if cancel.is_set() or info['generation']!=self.info['generation']:return
                    if records:
                        result=self.store.append_live(info['session_id'],records,initial_positions=self.initial_positions)
                        for key,sig in completed:accepted[key]=sig;retry_after.pop(key,None)
                        for record in records:
                            for path in (record.get('raw'),record.get('jpg')):
                                if path:existing[path.casefold()]=record
                        values={}
                        if result['changed']:
                            values['version']=self.info['version']+1
                            self.changes.append((values['version'],tuple(result['changed'])))
                        if result['added']:
                            values['arrivals']=self.info['arrivals']+len(result['added'])
                            newest=max((self.incoming_order.get(str(root/(record.get('raw') or record['jpg'])),0),record['id']) for record in records if record['id'] in result['added'])
                            if newest[0]>=self.last_ready_order:
                                self.last_ready_order=newest[0];values['last_photo_id']=newest[1]
                        aliases={hashlib.sha256(record['jpg'].casefold().encode()).hexdigest()[:24]:record['id'] for record in records if record.get('jpg')}
                        self.direct.acknowledge([record['id'] for record in records],aliases,values.get('last_photo_id'))
                        self._update(**values)
                    if partial:pending=max(pending,self.info['pending']-sum(key in previous_pending for key,_ in completed))
                    initializing=self.info['initializing'] and (partial or pending>0)
                    message=(f'正在读取已有照片 · 待处理 {pending} 张' if initializing else f'正在监控 · 本次载入 {self.info["arrivals"]} 张'+(f' · 等待写完 {pending} 张' if pending else ''))
                    self._update(pending=pending,initializing=initializing,error='；'.join(errors[:2]),message=message)
            except media.ScanCancelled:pass
            except Exception as exc:
                with self.lock:
                    if info['generation']==self.info['generation'] and not cancel.is_set():
                        self._update(error=str(exc)[:250],message='目录或照片暂不可用，正在自动重试')

    def _run(self):
        from directory_events import DirectoryEvents
        self.direct.start()
        signal=None;generation=None;next_scan=0
        try:
            while not self.closed.is_set():
                state=self.state();now=self.clock()
                if state['generation']!=generation:
                    if signal:signal.close()
                    signal=DirectoryEvents(state['folder'],state['recursive']) if state['enabled'] else None
                    generation=state['generation'];next_scan=0
                notified=signal.changed() if signal else False
                with self.lock:hints=list(self.hints);self.hints.clear()
                named=bool(notified and signal.paths and all(p.suffix.lower() in media.RAW_EXTS|media.IMAGE_EXTS|{'.xmp'} for p in signal.paths))
                if named:
                    hints.extend(p.with_suffix('') if p.suffix.lower()=='.xmp' and p.with_suffix('').suffix.lower() in media.RAW_EXTS|media.IMAGE_EXTS else p for p in signal.paths)
                # 未写完文件局部重试；全目录补偿保持独立周期，防止每 25ms 扫描历史。
                if state['pending'] and now<next_scan:
                    hints.extend(Path(key) for key,value in self.observed.items() if self.accepted.get(key)!=value[0])
                full_scan=now>=next_scan or notified and not named or not signal or signal.handle is None
                if state['enabled'] and hints and not full_scan:
                    self.tick(list(dict.fromkeys(hints)))
                elif state['enabled'] and full_scan:
                    self.tick();state=self.state()
                    # 有待写完/待处理文件时短间隔重试；空闲时只响应内核通知和补偿扫描。
                    delay=self.INTERVAL if not signal or signal.handle is None else self.RESCAN_SECONDS
                    next_scan=self.clock()+delay
                    if signal and signal.handle is None:signal.close();signal=DirectoryEvents(state['folder'],state['recursive'])
                if not state['enabled'] and self.metadata:self.metadata.close();self.metadata=None
                if state['enabled'] and self.metadata is None and not state['pending']:
                    try:
                        self.metadata=media.ExifToolSession(media.find_exiftool(self.store.root))
                        self.metadata.execute(['-ver'],cancel=self.cancel)
                    except Exception:
                        if self.metadata:self.metadata.close()
                        self.metadata=None
                self.wake.wait(.025);self.wake.clear()
        finally:
            if signal:signal.close()
            if self.metadata:self.metadata.close()

    def close(self):
        self.direct.close()
        self.closed.set();self.cancel.set();self.wake.set()
        with self.condition:self.condition.notify_all()
        if self.thread.is_alive():self.thread.join(timeout=3)
        if self.metadata:self.metadata.close()
