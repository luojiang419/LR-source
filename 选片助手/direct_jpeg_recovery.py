"""目录通知漏报的独立补偿：只枚举文件名，不进入业务锁或解码队列。"""
import os
from pathlib import Path
import stat
import sys
import threading
import time


def created_ns(value):
    return getattr(value,'st_birthtime_ns',value.st_ctime_ns)


class JpegNotificationRecovery:
    INTERVAL=.25
    AUDIT_SECONDS=2.0

    def __init__(self,feed):
        self.feed=feed;self.wake=threading.Event();self.cache={};self.token=None;self.audit_at=0
        self.thread=threading.Thread(target=self.run,name='direct-jpeg-recovery',daemon=True)

    def valid(self,info):
        current=self.feed.state()
        return not self.feed.closed.is_set() and current['enabled'] and self.identity(current)==self.identity(info)

    def identity(self,info):
        return (info['generation'],info['folder'],info['recursive'],info['enabled_at_ns'])

    def scan(self,info,force=False):
        token=self.identity(info)
        if token!=self.token:self.cache={};self.token=token;self.audit_at=0
        if not self.valid(info):return
        now=time.monotonic();force=force or now>=self.audit_at
        if force:self.audit_at=now+self.AUDIT_SECONDS
        root=Path(info['folder']);stack=[(root,False)];visited=set();failed=False
        excluded={os.path.normcase(os.path.abspath(self.feed.store.data))} if self.feed.store is not None else set()
        previews=sys.modules.get('preview_cache')
        excluded.update(os.path.normcase(os.path.abspath(path)) for path in getattr(previews,'_LOCATIONS',{}).values())
        while stack and self.valid(info):
            folder,new_tree=stack.pop();key=str(folder).casefold();visited.add(key)
            prior=self.cache.get(key)
            try:
                stamp=folder.stat().st_mtime_ns
                if prior and stamp==prior[0] and not force:
                    stack.extend((path,False) for path in prior[2]);continue
                names=set();children=[];offers=[]
                with os.scandir(folder) as entries:
                    for index,entry in enumerate(entries):
                        if index%256==0:
                            if not self.valid(info):return
                            # 大目录分片让步；慢盘枚举始终与即时通知/原图读取分离。
                            if index:self.feed.closed.wait(.001)
                        if entry.name.startswith('.framekeep-') or entry.is_symlink():continue
                        if entry.is_dir(follow_symlinks=False):
                            if info['recursive'] and not entry.name.startswith('.') and os.path.normcase(os.path.abspath(entry.path)) not in excluded and not getattr(entry.stat(follow_symlinks=False),'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT:
                                children.append(Path(entry.path))
                            continue
                        if Path(entry.name).suffix.lower() not in ('.jpg','.jpeg') or not entry.is_file(follow_symlinks=False):continue
                        name=entry.name.casefold();names.add(name)
                        if prior and name in prior[1]:continue
                        value=entry.stat(follow_symlinks=False);birth=created_ns(value)
                        # 首轮只补开启监控之后到达的文件，不能逐张跟随历史照片。
                        if prior or new_tree or birth>info['enabled_at_ns']:offers.append((birth,Path(entry.path)))
                self.cache[key]=(stamp,names,children)
                previous_children={str(path).casefold() for path in prior[2]} if prior else set()
                stack.extend((path,new_tree or bool(prior and str(path).casefold() not in previous_children)) for path in children)
                for birth,path in sorted(offers):
                    if not self.valid(info):return
                    self.feed.recover(path,birth,info)
            except OSError:
                # 离线/独占目录不能清空基线；恢复后继续找遗漏的新文件。
                failed=True
                continue
        if not failed and self.valid(info):self.cache={key:value for key,value in self.cache.items() if key in visited}

    def run(self):
        while not self.feed.closed.is_set():
            force=self.wake.is_set();self.wake.clear();info=self.feed.state()
            if info['enabled']:self.scan(info,force)
            self.wake.wait(self.INTERVAL)
