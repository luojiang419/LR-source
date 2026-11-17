"""启动时先读有界本地快照；真实目录查询在前端独立刷新。

快照只用于临时展示，不代替业务查询。根目录与排除规则始终从当前数据库
读取，禁止恢复已移除的位置；不读取原片、预览索引或扫描文件系统。
"""
from contextlib import closing
from collections import OrderedDict
import hashlib
import base64
import json
import sqlite3
import threading

from file_safety import atomic_write


class CatalogStartup:
    def __init__(self, catalog):
        self.catalog = catalog
        self.folder = catalog.data / 'catalog-startup'
        self.database = catalog.summaries.database
        self.lock = threading.Lock()
        self.thumbnail_lock=threading.Lock();self.thumbnail_pending=OrderedDict();self.thumbnail_thread=None;self.thumbnail_closed=False

    @staticmethod
    def page_key(root='', folder='', media='', cursor='', limit=120, frame=3):
        return json.dumps([root, folder, media, cursor, int(limit), frame], ensure_ascii=False)

    def revision(self):
        with closing(sqlite3.connect(self.catalog.db.as_uri()+'?mode=ro', uri=True, timeout=.1)) as c:
            return list(c.execute('SELECT value,serial FROM catalog_page_revision WHERE id=1').fetchone())

    def _path(self, key):
        return self.folder / (hashlib.sha256(key.encode()).hexdigest()+'.json')

    def _read(self, key):
        try:
            path=self._path(key)
            if self.folder.is_symlink() or self.folder.is_junction() or path.is_symlink() or path.is_junction():return None
            with path.open('rb') as f:
                raw = f.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:return None
            value = json.loads(raw)
            if value.get('schema')==1 and value.get('database')==self.database and value.get('key')==key:
                return value['payload']
        except (OSError, ValueError, TypeError, KeyError, AttributeError):pass

    def remember(self, key, payload):
        # 记录失败或另一请求正在记录时，不让显示等待磁盘写锁。
        if not self.lock.acquire(blocking=False):return
        try:
            if self._read(key)==payload:
                self._remember_view(key)
                return
            raw = json.dumps(dict(schema=1,database=self.database,key=key,payload=payload),
                             ensure_ascii=False,separators=(',',':')).encode()
            if len(raw)>(4*1024*1024 if key.startswith('tree:') else 512*1024):return
            self.folder.mkdir(exist_ok=True)
            if self.folder.is_symlink() or self.folder.is_junction():return
            path = self._path(key)
            if path.is_symlink() or path.is_junction():return
            atomic_write(path,raw)
            self._remember_view(key)
            # 目录与素材页合计最多12份快照，不清理其他文件。
            files = sorted(self.folder.glob('[0-9a-f]'*64+'.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
            for old in files[12:]:old.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):pass
        finally:self.lock.release()

    def _remember_view(self,key):
        try:view=json.loads(key)
        except ValueError:return
        if isinstance(view,list) and len(view)>=3:
            atomic_write(self.folder/'last-view.json',json.dumps({'database':self.database,'view':view[:3]}).encode())

    @staticmethod
    def thumbnail_key(preview):
        from urllib.parse import urlsplit,parse_qsl,urlencode
        url=urlsplit(preview)
        version=urlencode(sorted((k,v) for k,v in parse_qsl(url.query) if k in ('v','frame')))
        return hashlib.sha256((url.path+'?'+version).encode()).hexdigest()+'.jpg'

    def thumbnail(self, name, payload):
        import re
        if not name or not re.fullmatch(r'[0-9a-f]{64}\.jpg',name) or not isinstance(payload,bytes) or len(payload)>128*1024:return
        if not self.lock.acquire(blocking=False):return
        try:
            self.folder.mkdir(exist_ok=True)
            if self.folder.is_symlink() or self.folder.is_junction():return
            path=self.folder/('thumb-'+name+'.json')
            if path.exists():
                try:
                    old=json.loads(path.read_bytes())
                    if old.get('database')==self.database and old.get('digest')==hashlib.sha256(payload).hexdigest() and old.get('data')==base64.b64encode(payload).decode():return
                except (OSError,ValueError,AttributeError):pass
            value=dict(database=self.database,digest=hashlib.sha256(payload).hexdigest(),data=base64.b64encode(payload).decode())
            atomic_write(path,json.dumps(value).encode())
            files=sorted(self.folder.glob('thumb-*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
            for old in files[24:]:old.unlink(missing_ok=True)
        except (OSError,ValueError):pass
        finally:self.lock.release()

    def queue_thumbnail(self,name,payload):
        """可选启动副本不阻挡 HTTP 图片响应；仅保留最近24项，最多约3MiB。"""
        if not isinstance(payload,bytes) or len(payload)>128*1024:return
        with self.thumbnail_lock:
            if self.thumbnail_closed:return
            self.thumbnail_pending.pop(name,None);self.thumbnail_pending[name]=payload
            while len(self.thumbnail_pending)>24:self.thumbnail_pending.popitem(last=False)
            if self.thumbnail_thread is None:
                self.thumbnail_thread=threading.Thread(target=self._thumbnails,name='archive-startup-thumbnails',daemon=True)
                self.thumbnail_thread.start()

    def _thumbnails(self):
        while True:
            with self.thumbnail_lock:
                if self.thumbnail_closed or not self.thumbnail_pending:
                    self.thumbnail_thread=None;return
                name,payload=self.thumbnail_pending.popitem(last=False)
            self.thumbnail(name,payload)

    def close(self):
        with self.thumbnail_lock:
            self.thumbnail_closed=True;self.thumbnail_pending.clear();thread=self.thumbnail_thread
        if thread is not None:thread.join(timeout=5)

    def boot(self,*,include_thumbnails=True):
        """工作台释放重后台任务之前读取首屏，缩略图使用已验证的本地副本。"""
        try:
            view=json.loads((self.folder/'last-view.json').read_bytes())
            view=view['view'] if view.get('database')==self.database else ['','','']
        except (OSError,ValueError,TypeError,KeyError):view=['','','']
        try:
            result=self.read(*view);result['view']=view;result['thumbnails']={}
            if not include_thumbnails:return result
            from preview_codec import image_mime
            groups={}
            for row in (result.get('items') or {}).get('rows',[]):groups.setdefault((row['root'],row['folder']),[]).append(row)
            for row in [r for group in groups.values() for r in group][:24]:
                name=self.thumbnail_key(row['preview'])
                path=self.folder/('thumb-'+name+'.json')
                try:
                    if path.is_symlink() or path.is_junction() or path.stat().st_size>180*1024:continue
                    value=json.loads(path.read_bytes());payload=base64.b64decode(value['data'],validate=True)
                    if value['database']==self.database and hashlib.sha256(payload).hexdigest()==value['digest']:
                        result['thumbnails'][row['preview']]='data:'+image_mime(payload)+';base64,'+value['data']
                except (OSError,ValueError,KeyError,TypeError):continue
            return result
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):return None

    def read(self, root='', folder='', media='', cursor='', limit=120):
        with closing(sqlite3.connect(self.catalog.db.as_uri()+'?mode=ro',uri=True,timeout=.1)) as c:
            c.row_factory=sqlite3.Row
            c.execute('BEGIN')
            revision = list(c.execute('SELECT value,serial FROM catalog_page_revision WHERE id=1').fetchone())
            roots = [dict(row) for row in c.execute('SELECT id,path,status FROM roots WHERE removed=0 ORDER BY path COLLATE NOCASE')]
            exclusions = [(row['root'], row['folder'].strip('/')) for row in c.execute('SELECT root,folder FROM exclusions')]
        allowed = {row['id'] for row in roots}
        def visible(r, f):
            return r in allowed and not any(r==er and (not ef or f.strip('/')==ef or f.strip('/').startswith(ef+'/')) for er,ef in exclusions)
        cached_tree = self._read('tree:'+media)
        # 升级首次运行亦可复用已有严格目录快照，后台会重新校验版本。
        if not isinstance(cached_tree,dict):
            old = self.catalog.summaries._load('tree:'+media) if 'tree:'+media in self.catalog.summaries.FILES else None
            matches = old.get('payload',{}) if old and old.get('database')==self.database else {}
        else:matches = {r['id']:r.get('folders',[]) for r in cached_tree.get('roots',[])}
        for r in roots:r['folders'] = [f for f in matches.get(r['id'],[]) if visible(r['id'],f['folder'])]
        data = self._read(self.page_key(root,folder,media,cursor,limit,self.catalog.video.frame))
        if data is None:
            # 首次升级还没有新启动文件时，直接读取已有持久页，绝不回退全库查询。
            key=json.dumps(['browse-v1',root,folder,'',0,int(limit),cursor,media,self.catalog.video.frame],ensure_ascii=False,sort_keys=True)
            try:_,data=self.catalog.pages.read(key)
            except (sqlite3.Error,OSError,ValueError):pass
        if not isinstance(data,dict) or not isinstance(data.get('rows'),list):data = None
        if data is not None:
            from asset_catalog import ARCHIVE_MEDIA_EXTENSIONS
            rows=[r for r in data['rows'] if visible(r['root'],r['folder']) and str(r.get('extension') or Path(r.get('name','')).suffix).lower() in ARCHIVE_MEDIA_EXTENSIONS]
            data = {**data,'rows':rows,**({'total':None} if len(rows)!=len(data['rows']) else {})}
        return dict(database=self.database,revision=revision,tree={'roots':roots},items=data)
