"""页面展示快照与缩略图：独立本地文件、会话隔离、后台更新，不缓存写入结果。"""
import base64
import hashlib
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit,parse_qsl,urlencode

from file_safety import atomic_write

PATHS={'/api/sku','/api/sku/library','/api/sku/documents/overview','/api/parsing',
       '/api/selection/tree','/api/folders','/api/inventory/records','/api/providers',
       '/api/video-preview','/api/gpu-media','/api/preview-cache',
       '/api/intelligence/config','/api/intelligence/state','/api/intelligence/search-state','/api/intelligence/facets'}


def view_key(url):
    value=urlsplit(url)
    if value.path not in PATHS:return None
    query=[(k,v) for k,v in parse_qsl(value.query,keep_blank_values=True)
           if k not in ('client','sequence','background','fresh')]
    return value.path+('?' + urlencode(sorted(query)) if query else '')


def affected(path):
    if path is None:return PATHS
    if path in ('/api/inventory/usb','/api/archive/selection','/api/archive/preview','/api/providers/models','/api/providers/test'):return set()
    if path.startswith('/api/inventory/'):return {'/api/inventory/records'}
    if path.startswith('/api/video-preview'):return {'/api/video-preview'}
    if path.startswith('/api/preview-cache'):return {'/api/preview-cache'}
    if path.startswith('/api/providers/'):return {'/api/providers','/api/parsing'}
    if path.startswith('/api/intelligence/'):return {p for p in PATHS if p.startswith('/api/intelligence/')}
    if path.startswith('/api/catalog/'):return {'/api/sku/library'}|{p for p in PATHS if p.startswith('/api/intelligence/')}
    return {p for p in PATHS if p.startswith(('/api/sku','/api/parsing','/api/folders','/api/selection/'))}


class WorkbenchViews:
    def __init__(self,data,db):
        self.folder=Path(data)/'workbench-views';self.db=Path(db)
        self.identity=hashlib.sha256((str(self.db.resolve()).casefold()+':'+str(self.db.stat().st_ino)).encode()).hexdigest()
        self.lock=threading.Lock();self.epoch=0;self.context='';self.values={};self.images={}
        self._load()

    def _load(self):
        try:
            path=self.folder/'views.json'
            if self.folder.is_symlink() or self.folder.is_junction() or path.is_symlink() or path.stat().st_size>4*1024*1024:return
            value=json.loads(path.read_bytes())
            if value.get('schema')==1 and value.get('identity')==self.identity and isinstance(value.get('values'),dict):
                for key,payload in value['values'].items():
                    try:
                        pair=json.loads(key)
                        if isinstance(pair,list) and len(pair)==2 and all(isinstance(v,str) for v in pair) and view_key(pair[1]) and isinstance(payload,dict):self.values[key]=payload
                    except (ValueError,TypeError):continue
        except (OSError,ValueError,TypeError,AttributeError):pass

    def _save(self):
        self.folder.mkdir(exist_ok=True)
        if self.folder.is_symlink() or self.folder.is_junction():return
        path=self.folder/'views.json'
        if path.is_symlink() or path.is_junction():return
        while len(self.values)>32:self.values.pop(next(iter(self.values)))
        content=json.dumps({'schema':1,'identity':self.identity,'values':self.values},ensure_ascii=False,separators=(',',':')).encode()
        while len(content)>4*1024*1024 and self.values:
            self.values.pop(next(iter(self.values)))
            content=json.dumps({'schema':1,'identity':self.identity,'values':self.values},ensure_ascii=False,separators=(',',':')).encode()
        atomic_write(path,content)

    def remember(self,url,value,scope,epoch):
        key=view_key(url)
        if key is None or not isinstance(value,dict) or value.get('error') or value.get('loading') or not self.lock.acquire(False):return
        try:
            if epoch!=self.epoch:return
            value={k:v for k,v in value.items() if k!='request_token'}
            raw=json.dumps(value,ensure_ascii=False,separators=(',',':'))
            if len(raw.encode())>512*1024:return
            full=json.dumps([scope or '',key],ensure_ascii=False)
            if self.values.get(full)==value:return
            self.values.pop(full,None);self.values[full]=json.loads(raw);self._save()
        except (OSError,ValueError,TypeError):pass
        finally:self.lock.release()

    def invalidate(self,path=None):
        # 先推进代际，迟到的旧查询不得在写操作之后重新发布旧快照。
        with self.lock:
            paths=affected(path)
            if not paths:return
            self.epoch+=1
            # 日常评级/视图保存不撤销页面成员或当前文档。保留上次展示，
            # 新进程先恢复后核对；只推进代际，禁止迟到旧读取覆盖已保存状态。
            if not path or not path.startswith('/api/session'):
                self.values={key:value for key,value in self.values.items() if urlsplit(json.loads(key)[1]).path not in paths}
            try:self._save()
            except OSError:pass

    @staticmethod
    def image_key(url):
        value=urlsplit(url);query=dict(parse_qsl(value.query,keep_blank_values=True))
        if value.path.startswith('/api/inventory/image'):
            query={k:v for k,v in query.items() if k in ('id','kind','size')}
        elif value.path.startswith(('/api/preview/','/api/sku/library/preview/')):
            if not query.get('v') and not query.get('cached'):return None
            if query.get('v'):query.pop('cached',None)
            try:
                if int(query.get('size','480'))>600:return None
            except ValueError:return None
        else:return None
        return value.path+'?'+urlencode(sorted(query.items()))

    def image(self,url,payload):
        key=self.image_key(url)
        if not key or not isinstance(payload,bytes) or len(payload)>128*1024 or not self.lock.acquire(False):return
        try:
            from preview_codec import image_mime
            mime=image_mime(payload)
            self.folder.mkdir(exist_ok=True)
            if self.folder.is_symlink() or self.folder.is_junction():return
            category='inventory' if key.startswith('/api/inventory/') else 'sku' if key.startswith('/api/sku/') else 'selection'
            path=self.folder/('image-'+category+'-'+hashlib.sha256(key.encode()).hexdigest()+'.json')
            value={'identity':self.identity,'key':key,'digest':hashlib.sha256(payload).hexdigest(),'mime':mime,'data':base64.b64encode(payload).decode()}
            if path.is_symlink() or path.is_junction():return
            if path.exists():return
            atomic_write(path,json.dumps(value).encode())
            files=sorted(self.folder.glob('image-'+category+'-*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
            for old in files[24:]:old.unlink(missing_ok=True)
        except (OSError,ValueError,TypeError):pass
        finally:self.lock.release()

    def bootstrap(self,scope):
        self.context=scope or '';values={};budget=2*1024*1024
        # 用副本读取，避免持锁做JSON编码或阻塞正在返回的页面。
        for full,value in reversed(list(self.values.copy().items())):
            try:sid,key=json.loads(full)
            except (ValueError,TypeError):continue
            if sid!=self.context:continue
            size=len(json.dumps(value,ensure_ascii=False).encode())
            if size<=budget:values[key]=value;budget-=size
        images={}
        try:files=[] if self.folder.is_symlink() or self.folder.is_junction() else sorted(self.folder.glob('image-*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)[:72]
        except OSError:files=[]
        for path in files:
            try:
                if path.is_symlink() or path.stat().st_size>180*1024:continue
                value=json.loads(path.read_bytes());payload=base64.b64decode(value['data'],validate=True)
                if value['identity']==self.identity and value['digest']==hashlib.sha256(payload).hexdigest():
                    from preview_codec import image_mime
                    images[value['key']]='data:'+image_mime(payload)+';base64,'+value['data']
            except (OSError,ValueError,KeyError,TypeError):continue
        return {'identity':self.identity,'scope':self.context,'values':values,'images':images}
