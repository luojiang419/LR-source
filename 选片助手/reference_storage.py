"""参考文件索引的独立存储与在线迁移；原数据库始终保留。"""
from contextlib import contextmanager, closing
from functools import wraps
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import threading
import time

from file_safety import atomic_write


class AccessGate:
    """普通操作可并行，迁移独占；嵌套调用沿用当前读权限。"""
    def __init__(self):
        self.condition=threading.Condition();self.readers=0;self.writer=False
        self.local=threading.local()

    @contextmanager
    def read(self):
        depth=getattr(self.local,'depth',0)
        if not depth:
            with self.condition:
                self.condition.wait_for(lambda:not self.writer)
                self.readers+=1
        self.local.depth=depth+1
        try:yield
        finally:
            self.local.depth=depth
            if not depth:
                with self.condition:self.readers-=1;self.condition.notify_all()

    @contextmanager
    def exclusive(self,before_resume=None):
        with self.condition:
            self.writer=True
            if not self.condition.wait_for(lambda:self.readers==0,timeout=30):
                self.writer=False;self.condition.notify_all()
                raise RuntimeError('仍有目录正在访问，请待其完成后重试迁移')
        try:yield
        finally:
            if before_resume:before_resume()
            with self.condition:self.writer=False;self.condition.notify_all()


def storage_access(method):
    @wraps(method)
    def guarded(self,*args,**kwargs):
        with self.storage.gate.read():return method(self,*args,**kwargs)
    return guarded


class ReferenceStorage:
    marker='.framekeep-file-index.json'

    def __init__(self,library):
        self.library=library;self.gate=AccessGate();self.lock=threading.RLock()
        self.default=library.home/'catalogs'
        saved=library.config.get('index_storage',{})
        self.path=Path(saved.get('path') or self.default).absolute()
        self.identity=saved.get('id') or secrets.token_hex(16)
        self.previous=saved.get('previous','');self.warning=''
        self.job={'status':'idle','percent':0,'message':''};self.worker=None
        self.pausing=threading.Event()
        if self.path==self.default:self.path.mkdir(parents=True,exist_ok=True)
        else:
            try:self.require_available()
            except (OSError,ValueError) as error:self.warning=str(error)

    def require_available(self):
        if self.path==self.default:return
        try:
            if any(part.is_symlink() or part.is_junction() for part in [self.path,*self.path.parents]):raise ValueError('索引位置包含链接')
            marker=json.loads((self.path/self.marker).read_text(encoding='utf8'))
            if not isinstance(marker,dict) or marker.get('id')!=self.identity:raise ValueError('文件索引存储身份不匹配，未建立空库覆盖原配置')
            self.warning=''
        except (OSError,ValueError) as error:
            raise ValueError(f'文件索引位置不可用：{self.path}。请重新连接该磁盘后重启软件；原位置保留。') from error

    def state(self):
        with self.lock:
            return {'path':str(self.path),'default_path':str(self.default),'previous':self.previous,
                    'warning':self.warning,'job':dict(self.job)}

    def _update(self,**values):
        with self.lock:self.job.update(values)

    def _target(self,value):
        if not isinstance(value,str) or not value.strip():raise ValueError('请选择文件索引数据库的目标目录')
        raw=Path(value.strip().strip('"'))
        if not raw.is_absolute():raise ValueError('数据库路径须为完整绝对路径')
        for part in [raw,*raw.parents]:
            if part.is_symlink() or part.is_junction():raise ValueError('数据库位置不能使用链接目录')
        target=raw.resolve()
        from preview_cache import _LOCATIONS
        protected={self.library.data,self.path,*_LOCATIONS.values()}
        protected.update(Path(root['path']) for root in self.library.config['roots'] if not root.get('removed'))
        for folder in protected:
            # 可使用已添加的整个磁盘，但不能在具体照片目录内存放数据库。
            if target==folder or folder.is_relative_to(target) or target.is_relative_to(folder) and folder!=Path(folder.anchor):
                raise ValueError('请选择独立于照片、资料、缓存及当前数据库的空目录')
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError('目标目录必须为空，已有数据库及文件不会被覆盖')
        return target

    def start(self,value):
        with self.lock:
            if self.job['status']=='running':raise ValueError('文件索引数据库正在迁移，请等待完成')
            target=self._target(value)
            if self.library.stopping.is_set():raise ValueError('软件正在退出')
            self.job={'status':'running','percent':0,'message':'正在等待当前索引批次结束','target':str(target)}
            self.worker=threading.Thread(target=self._migrate,args=(target,),name='reference-storage-migrate',daemon=True)
            self.worker.start()
            return self.state()

    def _migrate(self,target):
        library=self.library;source=self.path
        self.pausing.set()
        try:
            with self.gate.exclusive(before_resume=self.pausing.clear):
                self.require_available()
                # 检查与实际创建之间不接管任何新出现的目录。
                if target.exists():
                    if any(target.iterdir()):raise ValueError('目标目录出现了新文件，迁移已停止')
                    # 在用户选择的空目录下独占一个新目录，避免并发写入的冲突。
                target.mkdir(parents=True,exist_ok=True)
                destination=target/('framekeep-index-'+secrets.token_hex(6))
                destination.mkdir()
                self._update(staged_path=str(destination))
                databases=[]
                for folder in source.iterdir():
                    if folder.is_symlink() or folder.is_junction():raise ValueError('原索引位置包含链接，请检查后重试')
                    db=folder/'asset_catalog.sqlite3'
                    if folder.is_dir() and db.is_file():
                        if db.is_symlink():raise ValueError('索引数据库不能是链接')
                        databases.append(db)
                needed=sum(db.stat().st_size+(db.with_name(db.name+'-wal').stat().st_size if db.with_name(db.name+'-wal').exists() else 0) for db in databases)
                if shutil.disk_usage(destination).free<needed+16*1024*1024:raise ValueError('目标磁盘空间不足，未切换位置')
                for catalog in library.catalogs.values():catalog.close()
                library.catalogs.clear()
                total=max(1,len(databases))
                for number,db in enumerate(databases):
                    output=destination/db.parent.name/db.name;output.parent.mkdir()
                    self._update(message=f'正在复制并校验数据库 {number+1} / {len(databases)}')
                    with closing(sqlite3.connect(db)) as original,closing(sqlite3.connect(output)) as copied:
                        def progress(status,remaining,pages):
                            self._update(percent=min(95,int((number+(1-remaining/max(1,pages)))/total*95)))
                        original.backup(copied,pages=256,progress=progress)
                        if copied.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('数据库完整性校验失败，保留原位置')
                        # 验证业务行与原索引快照一致，FTS 和触发器由 backup 整体保留。
                        for table in ('items','roots'):
                            if copied.execute(f'SELECT COUNT(*) FROM {table}').fetchone()!=original.execute(f'SELECT COUNT(*) FROM {table}').fetchone():
                                raise ValueError('数据库记录校验失败，保留原位置')
                atomic_write(destination/self.marker,json.dumps({'id':self.identity,'source':str(source),'created':time.time()},ensure_ascii=False).encode('utf8'))
                with library.lock:
                    old=library.config.get('index_storage')
                    library.config['index_storage']={'id':self.identity,'path':str(destination),'previous':str(source)}
                    try:library.save()
                    except BaseException:
                        if old is None:library.config.pop('index_storage',None)
                        else:library.config['index_storage']=old
                        raise
                    self.path=destination;self.previous=str(source);self.warning=''
                self._update(status='completed',percent=100,message='迁移完成，新位置已生效，原数据库保留',path=str(destination))
        except (OSError,ValueError,RuntimeError,sqlite3.Error) as error:
            self._update(status='error',message=f'迁移失败，继续使用原位置：{error}')
        finally:
            self.pausing.clear()
            # 中断过的完整扫描会自动补齐，已建快照和查询不需要重新建库。
            with library.lock:
                resume=[r['id'] for r in library.config['roots'] if not r.get('removed') and r.get('status')=='queued']
            if not library.stopping.is_set():
                for root in resume:library.enqueue(root)

    def close(self):
        worker=self.worker
        if worker and worker is not threading.current_thread():worker.join()
