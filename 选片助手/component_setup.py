"""安装器和运行中共用的可恢复组件准备；仅信任随程序固定的哈希清单。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import threading
import time
import urllib.request
import zipfile
import uuid

MODEL_IDS = ('glm-ocr-local', 'qwen2.5-vl-7b-q4')
DEFAULT_MODEL_IDS = ('glm-ocr-local',)


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    path = Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    try:
        # Windows 的读句柄可能暂时未允许 FILE_SHARE_DELETE；不把读取进度当作准备失败。
        for attempt in range(30):
            try:os.replace(temp,path);return
            except PermissionError:
                if attempt==29:raise
                time.sleep(.02)
    finally:temp.unlink(missing_ok=True)


def inside(root, relative):
    if not isinstance(relative, str) or '\\' in relative or ':' in relative:raise ValueError('组件路径无效')
    parts = PurePosixPath(relative).parts
    if not parts or relative != '/'.join(parts) or any(p in ('.','..') for p in parts):raise ValueError('组件路径无效')
    root = Path(root).resolve();target = root/relative
    if not target.resolve().is_relative_to(root):raise ValueError('组件路径超出目录')
    cursor = target
    while cursor != root:
        if cursor.is_symlink() or cursor.is_junction():raise ValueError('组件路径不能经过链接')
        cursor = cursor.parent
    return target


class ComponentManager:
    def __init__(self, data, seeds=(), manifest=None, on_progress=None):
        self.root = Path(data)/'components';self.root.mkdir(parents=True, exist_ok=True)
        resource = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
        self.manifest = manifest or json.loads((resource/'component-manifest.json').read_text(encoding='utf8'))
        self.seeds = [Path(s) for s in seeds if s] + [resource/'component-seed']
        self.lock = threading.RLock();self.operation = threading.RLock();self.closed = threading.Event()
        self.callback = on_progress;self.thread = None;self.requested = set();self.checked = {}
        self.state = {'status':'idle','message':'识别组件按需自动准备','done':0,'total':0,'error':'','ready':[]}
        self.stamps = self.root/'verified.json'
        try:self.checked = json.loads(self.stamps.read_text(encoding='utf8'))
        except (OSError,ValueError):pass

    def _valid(self, record):
        path = inside(self.root, record['path'])
        try:
            if not path.is_file() or path.stat().st_size != record['size']:return False
            stat = path.stat()
        except OSError:return False
        return self.checked.get(record['path']) == [record['sha256'],stat.st_size,stat.st_mtime_ns]

    def snapshot(self):
        with self.lock:
            ready = [g for g in ('runtime','driver','webview',*MODEL_IDS) if any(r['group']==g for r in self.manifest['assets']) and all(self._valid(r) for r in self.manifest['assets'] if r['group']==g)]
            return {**self.state,'ready':ready}

    def progress(self, message, done=0, total=0, status='working', error=''):
        with self.lock:self.state.update(status=status,message=message,done=done,total=total,error=error)
        if self.callback:self.callback(self.snapshot())

    def _groups(self, groups):
        groups = set(groups)
        if not groups <= {'runtime','driver','webview',*MODEL_IDS}:raise ValueError('未知识别组件')
        if groups.intersection(MODEL_IDS):groups.add('runtime')
        return groups

    @contextmanager
    def _process_lock(self):
        with (self.root/'.prepare.lock').open('a+b') as stream:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0);stream.write(b'0');stream.flush();stream.seek(0)
                while True:
                    try:msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1);break
                    except OSError:
                        self.progress('另一个安装或识别窗口正在准备组件，请稍候')
                        if self.closed.wait(.3):raise InterruptedError('组件准备已停止')
                try:yield
                finally:stream.seek(0);msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:yield

    def _download(self, url, target, expected, size=None):
        target.parent.mkdir(parents=True,exist_ok=True);part=target.with_name(target.name+'.part')
        if target.is_file() and (size is None or target.stat().st_size==size) and digest(target)==expected:return
        last = None
        for attempt in range(3):
            if self.closed.is_set():raise InterruptedError('组件准备已停止')
            try:
                offset = part.stat().st_size if part.exists() else 0
                if offset and size is None and digest(part)==expected:os.replace(part,target);return
                if size and offset >= size:
                    if offset==size and digest(part)==expected:os.replace(part,target);return
                    part.unlink();offset=0
                request = urllib.request.Request(url,headers={'Range':f'bytes={offset}-','User-Agent':'Framekeep-Components/1'})
                with urllib.request.urlopen(request,timeout=45) as response:
                    if response.status==206:
                        content_range=response.headers.get('Content-Range','')
                        if not content_range.startswith(f'bytes {offset}-'):raise ValueError('组件续传范围不匹配')
                    elif response.status==200:offset=0
                    else:raise OSError('组件下载状态异常')
                    total=size or int(response.headers.get('Content-Length',0))+offset
                    last_emit=0
                    with part.open('ab' if offset else 'wb') as output:
                        while chunk:=response.read(1024*1024):
                            if self.closed.is_set():raise InterruptedError('组件准备已停止')
                            output.write(chunk);offset+=len(chunk)
                            if offset>(size or 2*1024**3):raise ValueError('组件下载超过预期大小')
                            if time.monotonic()-last_emit>.3:
                                self.progress('正在下载 '+target.name,offset,total);last_emit=time.monotonic()
                        output.flush();os.fsync(output.fileno())
                self.progress('正在校验 '+target.name,offset,total)
                if size and part.stat().st_size!=size:raise OSError('下载尚未完整，已保留断点')
                if digest(part)!=expected:part.unlink();raise ValueError('组件校验失败，正在重新下载')
                os.replace(part,target);return
            except (OSError,ValueError) as exc:
                last=exc
                if self.closed.is_set():raise
        raise OSError('下载未完成，已保留断点；请检查网络后重试：'+str(last))

    def _seed(self, record, target):
        for seed in self.seeds:
            source = inside(seed,record['path'])
            if not source.is_file() and record['path'].startswith('runtime/'):
                source=inside(seed,'llama.cpp/'+Path(record['path']).name)
            if not source.is_file() or source.stat().st_size!=record['size']:continue
            self.progress('正在准备 '+target.name,0,record['size'])
            part=target.with_name(target.name+'.part');h=hashlib.sha256();done=0
            with source.open('rb') as src,part.open('wb') as out:
                while chunk:=src.read(8*1024*1024):
                    if self.closed.is_set():raise InterruptedError('组件准备已停止')
                    out.write(chunk);h.update(chunk);done+=len(chunk)
                    self.progress('正在准备 '+target.name,done,record['size'])
            if h.hexdigest()!=record['sha256']:part.unlink();continue
            os.replace(part,target);return True
        return False

    def _runtime_archives(self):
        folder=self.root/'downloads'
        for item in self.manifest['archives']:
            archive_path=inside(folder,item['name']);self._download(item['url'],archive_path,item['sha256'],item.get('size'))
            with zipfile.ZipFile(archive_path) as archive:
                for record in self.manifest['assets']:
                    if record.get('archive')!='llama' or self._valid(record):continue
                    matches=[e for e in archive.infolist() if PurePosixPath(e.filename).name==PurePosixPath(record['path']).name]
                    if not matches:continue
                    entry=matches[0]
                    if entry.file_size!=record['size']:raise ValueError('运行时压缩文件大小异常')
                    target=inside(self.root,record['path']);target.parent.mkdir(parents=True,exist_ok=True)
                    temp=target.with_name(target.name+'.part')
                    with archive.open(entry) as src,temp.open('wb') as out:shutil.copyfileobj(src,out,1024*1024)
                    if digest(temp)!=record['sha256']:temp.unlink();raise ValueError('运行时文件校验失败')
                    os.replace(temp,target);self._remember(record)
            # 解压后全部对应资产已验哈希才删除可再生压缩包；失败仍保留断点。
            assets=[r for r in self.manifest['assets'] if r.get('archive')=='llama']
            if assets and all(self._valid(r) for r in assets):archive_path.unlink(missing_ok=True)

    def _remember(self, record):
        stat=inside(self.root,record['path']).stat()
        self.checked[record['path']]=[record['sha256'],stat.st_size,stat.st_mtime_ns]
        write_json(self.stamps,self.checked)

    def ensure(self, groups):
        groups=self._groups(groups)
        with self.operation,self._process_lock():
            try:
                with self.lock:self.state['groups']=sorted(groups)
                # 另一进程可能刚刚完成，重新读取已验证文件的时间戳。
                if self.stamps.is_file():self.checked=json.loads(self.stamps.read_text(encoding='utf8'))
                records=[r for r in self.manifest['assets'] if r['group'] in groups]
                required=sum(r['size'] for r in records if not self._valid(r))
                if required and shutil.disk_usage(self.root).free<required+512*1024**2:raise OSError('模型所在分区空间不足，需要约 '+str(round((required+512*1024**2)/1024**3,1))+' GB 可用空间')
                runtime_missing=False
                for record in records:
                    if self._valid(record):continue
                    target=inside(self.root,record['path']);target.parent.mkdir(parents=True,exist_ok=True)
                    self.progress('正在检查 '+target.name)
                    if target.is_file() and target.stat().st_size==record['size'] and digest(target)==record['sha256']:self._remember(record);continue
                    if self._seed(record,target):self._remember(record);continue
                    if record.get('url'):self._download(record['url'],target,record['sha256'],record['size']);self._remember(record)
                    elif record.get('archive'):runtime_missing=True
                    else:raise ValueError('安装组件缺失，请运行新版完整安装器修复：'+record['path'])
                if runtime_missing:self._runtime_archives()
                if any(not self._valid(r) for r in records):raise ValueError('组件未完整准备，请重试或使用完整安装包修复')
                if groups.intersection(MODEL_IDS):self._configure_models()
                self.progress('识别组件已就绪，正在继续操作',status='ready')
                return self.snapshot()
            except Exception as exc:
                self.progress(str(exc),status='error',error=str(exc));raise

    def _configure_models(self):
        config={'executable':str(self.root/'runtime/llama-server.exe'),'models':{}}
        for group in MODEL_IDS:
            rows=[r for r in self.manifest['assets'] if r['group']==group]
            if not rows or not all(self._valid(r) for r in rows):continue
            config['models'][group]={('mmproj' if Path(r['path']).name.startswith('mmproj') else 'model'):str(self.root/r['path']) for r in rows}
            config['models'][group].update(context=8192,gpu_layers=99)
        write_json(self.root/'local-models.json',config)

    def start(self, groups):
        groups=self._groups(groups)
        with self.lock:
            self.requested.update(groups)
            if self.thread and self.thread.is_alive():return self.snapshot()
            self.progress('正在检查识别组件',status='working')
            def run():
                try:
                    while True:
                        with self.lock:pending=set(self.requested);self.requested.clear()
                        self.ensure(pending)
                        with self.lock:
                            if not self.requested:self.thread=None;return
                except Exception:
                    with self.lock:self.requested.clear();self.thread=None
            self.thread=threading.Thread(target=run,name='component-prepare',daemon=True);self.thread.start()
            return self.snapshot()

    def close(self):
        self.closed.set()
        if self.thread:self.thread.join(timeout=2)
