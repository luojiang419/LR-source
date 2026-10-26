"""图片缓存位置、可校验迁移和可移植备份；只操作登记的生成文件。"""
from contextlib import closing,ExitStack
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile

import preview_cache as previews
from file_safety import atomic_write,publish_new,delete_verified
from preview_codec import validate_stream

SCHEMA='framekeep.image-cache'
VERSION=1
BACKUP_VERSION=3
CHUNK_IMAGES=1024
NAMES=('previews','catalog_previews')
HEX=re.compile(r'[0-9a-f]{64}\Z')


class CacheStorage:
    def __init__(self,data,store=None,catalog=None):
        self.data=Path(data).resolve();self.config=self.data/'image-cache-location.json'
        self.root=self.data;self.store=store;self.catalog=catalog;self.warning='';saved={}
        if self.config.is_file():
            saved=json.loads(self.config.read_text(encoding='utf8'));self.root=Path(saved['path']).resolve()
            if self.root!=self.data and not (self.root/'.framekeep-image-cache.json').is_file():
                self.warning='原缓存设备未连接或缓存标记缺失，临时使用本机缓存；原位置保持登记，连接设备后重启即可恢复。'
                self.root=self.data/'image-cache-offline'
        previews._LOCATIONS[self.data]=self.root
        self.cache=previews.cache_for(self.data/'previews');self.budget=self.cache.budget
        self.preview_upgrade=[]
        for cache in self.budget.caches:
            with cache.connect() as c:policy=c.execute("SELECT value FROM configuration WHERE key='preview_policy'").fetchone()
            if not policy or policy[0]!='embedded-jpeg-2048-v6':
                self.preview_upgrade.append(dict(cache=cache.folder.name,**cache.retire_legacy()))
        if any(r['preserved_files'] for r in self.preview_upgrade):
            self.warning+=' 部分旧预览被占用或已被外部修改，已保留并将在下次启动复核。'
        if saved.get('limit_gb') and self.budget.state()['bytes']<=saved['limit_gb']*1024**3:self.budget.configure(saved['limit_gb'])
        self.previous=saved.get('previous_path','');self.recovery=saved.get('recovery','')
        self.lock=threading.RLock();self.job={'status':'idle','message':'缓存已就绪'};self.thread=None
        startup_target=saved.get('pending_limit_gb') or saved.get('limit_gb') or self.budget.limit//1024**3
        if saved.get('pending_limit_gb') or self.budget.state()['bytes']>startup_target*1024**3:self.start('resize',str(startup_target))

    def state(self):
        with self.lock:job=dict(self.job)
        result=self.budget.state()
        result.update(path=str(self.root),default_path=str(self.data),job=job,preview_policy='embedded-jpeg-2048',preview_upgrade=self.preview_upgrade,
                      previous_path=self.previous,layout_version=VERSION,warning=self.warning,fingerprint='SHA-256（完整文件内容，不含盘符或路径）')
        with self.budget.lock:
            result['portable_images']=0
            for cache in self.budget.caches:
                with cache.connect() as c:result['portable_images']+=c.execute('SELECT portable FROM cache_totals WHERE id=1').fetchone()[0]
        return result

    def configure(self,gb):
        if type(gb) is not int or not 1<=gb<=256:raise ValueError('图片缓存上限须为 1–256 GB 的整数')
        with self.lock:
            if self.job.get('status')=='running':raise ValueError('缓存管理任务进行中，请等待完成后修改上限')
            saved=json.loads(self.config.read_text(encoding='utf8')) if self.config.is_file() else {'schema':SCHEMA,'version':VERSION,'path':str(self.root)}
            if self.budget.state()['bytes']>gb*1024**3:
                saved['pending_limit_gb']=gb
                atomic_write(self.config,json.dumps(saved,ensure_ascii=False).encode('utf8'))
                return self.start('resize',str(gb))
            self.budget.configure(gb);saved['limit_gb']=gb;saved.pop('pending_limit_gb',None)
            atomic_write(self.config,json.dumps(saved,ensure_ascii=False).encode('utf8'))
        return self.state()

    def resize(self,value):
        gb=int(value)
        if not 1<=gb<=256:raise ValueError('缓存上限超出范围')
        while True:
            before=self.budget.state()['bytes']
            with self.budget.lock,self.cache.connect() as c:
                finished=self.budget.trim(self.cache,c,limit=gb*1024**3,max_entries=16)
            after=self.budget.state()['bytes']
            self.progress(f'正在分批整理缓存，完成后应用 {gb} GB 上限…',remaining_bytes=after)
            if finished:
                with self.budget.lock:
                    if self.budget.state()['bytes']>gb*1024**3:continue
                    self.budget.configure(gb)
                    saved=json.loads(self.config.read_text(encoding='utf8')) if self.config.is_file() else {'schema':SCHEMA,'version':VERSION,'path':str(self.root)}
                    saved['limit_gb']=gb;saved.pop('pending_limit_gb',None)
                    atomic_write(self.config,json.dumps(saved,ensure_ascii=False).encode('utf8'))
                return {'limit_gb':gb}
            if after>=before:raise OSError('缓存文件被其他程序占用，原上限保留，请关闭占用后重试')
            time.sleep(.01)

    def start(self,action,path):
        if action not in ('migrate','export','import','prepare','clean_previous','resize'):raise ValueError('未知的缓存管理操作')
        if action not in ('prepare','clean_previous') and (not isinstance(path,str) or not path.strip()):raise ValueError('请填写缓存操作路径')
        with self.lock:
            if self.job['status']=='running':raise ValueError('已有缓存管理任务正在运行，请等待完成')
            self.job={'id':uuid.uuid4().hex,'action':action,'status':'running','message':'正在准备…','done':0,'total':0}
            self.thread=threading.Thread(target=self._run,args=(action,path),daemon=True,name='cache-storage');self.thread.start()
        return self.state()

    def progress(self,message,**values):
        with self.lock:self.job.update(message=message,**values)

    def _run(self,action,path):
        try:
            result=getattr(self,action)(path) or {}
            self.progress('缓存'+{'migrate':'迁移','export':'备份导出','import':'备份导入','prepare':'指纹准备','clean_previous':'旧位置清理','resize':'上限调整'}[action]+'已完成',status='completed',**result)
        except Exception as exc:self.progress(str(exc),status='failed')

    def _registered(self,cache,after_seq=None):
        last='';sequence=after_seq
        while True:
            with cache.lock,cache.connect() as c:
                if after_seq is None:rows=c.execute('SELECT * FROM previews WHERE name>? ORDER BY name LIMIT 256',(last,)).fetchall()
                else:rows=c.execute('SELECT * FROM previews INDEXED BY preview_published WHERE published_seq>? ORDER BY published_seq LIMIT 256',(sequence,)).fetchall()
            if not rows:return
            last=rows[-1]['name'];sequence=rows[-1]['published_seq']
            for row in rows:
                if cache._registered(row) and row['digest']:yield dict(row)

    def prepare(self,_=None):
        """给升级前的已有图片补可移植指纹，不重新解码或修改照片。"""
        prepared=0;unavailable=0
        incomplete=False
        for cache in self.budget.caches:
            with cache.lock,cache.connect() as c:
                totals=c.execute('SELECT images,portable FROM cache_totals WHERE id=1').fetchone()
                incomplete=incomplete or totals['images']>totals['portable']
        if not incomplete:return {'prepared':0,'unavailable':0}
        def enroll(cache,root,record):
            nonlocal prepared,unavailable
            with cache.lock,cache.connect() as c:
                rows=[dict(r) for r in c.execute('SELECT * FROM previews WHERE fingerprint=? AND NOT EXISTS(SELECT 1 FROM portable_names n WHERE n.name=previews.name)',(record['fingerprint'],))]
            if not rows:return
            try:
                with self.budget.generation():content=cache.content_key(record,root)
                for row in rows:
                    source=dict(record,orientation=row['orientation'])
                    recipe=previews.registered_recipe(row)
                    cache.bind_content(content,source,row['edge'],row['decoded'],row['name'],recipe)
                prepared+=len(rows)
            except (OSError,ValueError):unavailable+=len(rows)
            self.progress('正在补齐已有缓存的完整内容指纹…',done=prepared,unavailable=unavailable)
        if self.store:
            for session in self.store.sessions():
                last=''
                while True:
                    with self.store.connect() as c:batch=c.execute('SELECT id,data FROM captures WHERE session=? AND id>? ORDER BY id LIMIT 256',(session['id'],last)).fetchall()
                    if not batch:break
                    last=batch[-1]['id']
                    for row in batch:enroll(self.budget.caches[0],session['root'],json.loads(row['data']))
        if self.catalog:
            last=''
            while True:
                with self.catalog.connect() as c:batch=[dict(r) for r in c.execute('SELECT * FROM capture_items WHERE id>? ORDER BY id LIMIT 240',(last,))]
                if not batch:break
                for row in batch:
                    with self.catalog.connect() as c:
                        members=[dict(r) for r in c.execute('SELECT * FROM items WHERE online=1 AND pair_key=? ORDER BY extension,path',(row['pair_key'],))]
                    root,record=self.catalog.preview_record(row,members);enroll(self.budget.caches[1],root,record)
                last=batch[-1]['id']
        return {'prepared':prepared,'unavailable':unavailable}

    def _destination(self,value):
        path=Path(value.strip().strip('"')).expanduser()
        if not path.is_absolute():raise ValueError('请使用绝对路径')
        if path.is_symlink() or path.is_junction():raise ValueError('缓存根目录不能是目录链接')
        path=path.resolve()
        if path==self.root or path.is_relative_to(self.root):raise ValueError('目标不能位于当前缓存目录内')
        # 默认历史缓存根目录也是资料目录；不能迁入数据库/模型或照片目录。
        if path==self.data or path.is_relative_to(self.data):raise ValueError('请使用独立于资料目录的缓存文件夹')
        if self.data.is_relative_to(path):raise ValueError('缓存目录不能包含软件资料目录')
        if path.is_symlink() or path.is_junction():raise ValueError('缓存根目录不能是目录链接')
        if path.exists() and any(path.iterdir()):raise ValueError('请指定空文件夹，系统不会接管或覆盖已有文件')
        if self.store:
            sources=[Path(s['root']).resolve() for s in self.store.sessions()]
            if self.catalog:sources += [Path(r['path']).resolve() for r in self.catalog.state()['roots']]
            if any(path==p or path.is_relative_to(p) or p.is_relative_to(path) for p in sources):raise ValueError('缓存目录不能与照片目录重叠')
        path.mkdir(parents=True,exist_ok=True)
        return path

    def migrate(self,value):
        destination=self._destination(value);old=self.root
        atomic_write(destination/'.framekeep-image-cache.json',json.dumps({'schema':SCHEMA,'version':VERSION,'id':uuid.uuid4().hex}).encode())
        count=0
        with tempfile.TemporaryDirectory(dir=destination,prefix='.framekeep-migration-') as folder, closing(sqlite3.connect(Path(folder)/'inventory.sqlite3')) as inventory:
            inventory.execute('CREATE TABLE copied(cache TEXT,name TEXT,digest TEXT,bytes INTEGER,PRIMARY KEY(cache,name))')
            marks={}
            for cache in self.budget.caches:
                with cache.lock,cache.connect() as c:marks[cache.folder.name]=c.execute('SELECT revision FROM cache_totals WHERE id=1').fetchone()[0]
            def copy_registered(incremental=False):
                nonlocal count
                for cache in self.budget.caches:
                    for row in self._registered(cache,marks[cache.folder.name] if incremental else None):
                        key=(cache.folder.name,row['name'])
                        previous=inventory.execute('SELECT digest FROM copied WHERE cache=? AND name=?',key).fetchone()
                        if previous and previous[0]==row['digest']:continue
                        with cache.lock,cache.connect() as c:
                            if not cache._owned(c,row['name']):continue
                            target=destination/key[0]/key[1];target.parent.mkdir(parents=True,exist_ok=True)
                            if count%128==0 and shutil.disk_usage(destination).free<row['bytes']+16*1024**2:raise OSError('目标磁盘可用空间不足，原缓存仍保留')
                            shutil.copyfile(cache.folder/row['name'],target)
                            if cache._digest(target)!=row['digest']:raise OSError('迁移缓存校验失败，原目录仍保留')
                        inventory.execute('INSERT OR REPLACE INTO copied VALUES (?,?,?,?)',(*key,row['digest'],row['bytes']))
                        count+=1
                        if count%128==0:inventory.commit();self.progress('正在复制并校验缓存…',done=count)
                inventory.commit()
            copy_registered()
            self.progress('正在提交增量与数据库快照并切换位置…',done=count)
            with self.budget.exclusive():
                # 持久发布序号只检查迁移期间新发布的缓存，不再全盘逐文件核对。
                copy_registered(True)
                for cache in self.budget.caches:
                    target_folder=destination/cache.folder.name;target_folder.mkdir(parents=True,exist_ok=True)
                    with closing(sqlite3.connect(target_folder/'index.sqlite3')) as target:
                        with cache.connect() as source:source.backup(target)
                        if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise OSError('迁移后的缓存数据库校验失败')
                        target.execute('ATTACH DATABASE ? AS migration',(str(Path(folder)/'inventory.sqlite3'),))
                        target.execute('DELETE FROM previews WHERE NOT EXISTS(SELECT 1 FROM migration.copied m WHERE m.cache=? AND m.name=previews.name AND m.digest=previews.digest)',(cache.folder.name,))
                        for name,digest in target.execute('SELECT m.name,m.digest FROM migration.copied m LEFT JOIN previews p ON p.name=m.name WHERE m.cache=? AND p.name IS NULL',(cache.folder.name,)):
                            path=target_folder/name
                            if path.exists():delete_verified(path,digest)
                        target.commit()
                recovery='image-cache-migration-'+uuid.uuid4().hex+'.sqlite3'
                inventory.execute('CREATE TABLE information(key TEXT PRIMARY KEY,value TEXT)')
                inventory.executemany('INSERT INTO information VALUES (?,?)',[('schema',SCHEMA),('previous_path',str(old))]);inventory.commit()
                with closing(sqlite3.connect(self.data/recovery)) as saved:inventory.backup(saved)
                atomic_write(self.config,json.dumps({'schema':SCHEMA,'version':VERSION,'path':str(destination),'limit_gb':self.budget.limit//1024**3,
                    'previous_path':str(old),'recovery':recovery},ensure_ascii=False).encode('utf8'))
                for cache in self.budget.caches:
                    previous=cache.folder;cache.folder=destination/previous.name;cache.db=cache.folder/'index.sqlite3'
                    previews._CACHES[previous]=cache;previews._CACHES[cache.folder]=cache
                previews._BUDGETS[destination]=self.budget;previews._LOCATIONS[self.data]=destination
                self.root=destination;self.warning='';self.previous=str(old);self.recovery=recovery
        return {'path':str(destination),'previous_path':str(old),'copied':count,'source_retained':True}

    def clean_previous(self,_=None):
        if not self.previous or not re.fullmatch(r'image-cache-migration-[0-9a-f]{32}\.(?:json|sqlite3)',self.recovery):raise ValueError('没有可清理的已迁移缓存')
        previous=Path(self.previous)
        if previous.resolve()==self.root.resolve() or previous.is_symlink() or previous.is_junction():raise ValueError('旧缓存位置无效或仍在使用')
        removed=0;retained=0;freed=0
        with ExitStack() as stack:
            file=self.data/self.recovery
            if file.suffix=='.json':
                inventory=json.loads(file.read_text(encoding='utf8'))
                if inventory.get('schema')!=SCHEMA or inventory.get('previous_path')!=self.previous:raise ValueError('迁移清理登记不匹配')
                rows=inventory.get('images',[])
            else:
                recovery=stack.enter_context(closing(sqlite3.connect(file.as_uri()+'?mode=ro',uri=True)))
                recovery.row_factory=sqlite3.Row;info=dict(recovery.execute('SELECT key,value FROM information'))
                if info.get('schema')!=SCHEMA or info.get('previous_path')!=self.previous:raise ValueError('迁移清理登记不匹配')
                rows=recovery.execute('SELECT * FROM copied')
            connections={}
            try:
                for item in rows:
                    item=dict(item)
                    if item.get('cache') not in NAMES or not previews._NAME.fullmatch(item.get('name','')) or not HEX.fullmatch(item.get('digest','')):raise ValueError('迁移清理清单无效')
                    folder=previous/item['cache'];index=folder/'index.sqlite3';path=folder/item['name']
                    if folder.is_symlink() or folder.is_junction() or not index.is_file():retained+=1;continue
                    if folder not in connections:
                        c=stack.enter_context(closing(sqlite3.connect(index)));c.row_factory=sqlite3.Row;connections[folder]=c
                    c=connections[folder];row=c.execute('SELECT * FROM previews WHERE name=?',(item['name'],)).fetchone()
                    if row is None or not previews.PreviewCache._registered(row) or row['digest']!=item['digest']:retained+=int(path.exists());continue
                    try:
                        if not path.exists():c.execute('DELETE FROM previews WHERE name=?',(item['name'],));continue
                        if not delete_verified(path,item['digest']):retained+=1;continue
                    except OSError:retained+=1;continue
                    c.execute('DELETE FROM previews WHERE name=?',(item['name'],));removed+=1;freed+=row['bytes']
                    if removed%128==0:c.commit();self.progress('正在清理旧位置已登记且校验一致的缓存图片…',done=removed)
            finally:
                for c in connections.values():c.commit()
        return {'removed':removed,'retained':retained,'freed_bytes':freed}

    def export(self,value):
        folder=Path(value.strip().strip('"')).expanduser().resolve()
        if not folder.is_dir():raise ValueError('请选择已存在的备份保存文件夹')
        if folder==self.root or folder.is_relative_to(self.root):raise ValueError('备份请保存到缓存目录以外的位置')
        preparation=self.prepare()
        target=folder/('末帧图片缓存-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.framekeep-cache.zip')
        temp=folder/('.framekeep-cache-'+uuid.uuid4().hex+'.tmp')
        manifest={'schema':SCHEMA,'version':BACKUP_VERSION,'fingerprint':'sha256-full-v1','chunks':[]}
        count=0;portable=0
        try:
            with zipfile.ZipFile(temp,'x',compression=zipfile.ZIP_STORED,allowZip64=True) as z:
                for cache in self.budget.caches:
                    # 仅在建立 SQLite 快照时持缓存锁，随后流式处理固定大小索引块。
                    with tempfile.TemporaryDirectory(dir=self.root,prefix='.framekeep-export-') as folder:
                        db=sqlite3.connect(Path(folder)/'index.sqlite3');db.row_factory=sqlite3.Row
                        try:
                            with cache.lock,cache.connect() as source:source.backup(db)
                            cursor=db.execute('SELECT * FROM previews ORDER BY accessed DESC')
                            while True:
                                batch=cursor.fetchmany(CHUNK_IMAGES)
                                if not batch:break
                                metadata={'previews':[],'aliases':[]}
                                for row in batch:
                                    if not cache._registered(row) or not row['digest']:continue
                                    with cache.lock,cache.connect() as c:
                                        if not cache._owned(c,row['name']):continue
                                        z.write(cache.folder/row['name'],cache.folder.name+'/'+row['name'])
                                    metadata['previews'].append(dict(row,cache=cache.folder.name))
                                    aliases=[dict(r,cache=cache.folder.name) for r in db.execute('SELECT * FROM content_previews WHERE name=?',(row['name'],))]
                                    metadata['aliases'].extend(aliases);count+=1;portable+=int(bool(aliases))
                                if not metadata['previews']:continue
                                payload=json.dumps(metadata,ensure_ascii=False,separators=(',',':')).encode('utf8')
                                name=f'indexes/{len(manifest["chunks"]):06d}.json';z.writestr(name,payload)
                                manifest['chunks'].append({'name':name,'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()})
                                self.progress('正在分块导出图片与缓存数据库快照…',done=count)
                        finally:db.close()
                payload=json.dumps(manifest,ensure_ascii=False,separators=(',',':')).encode('utf8')
                z.writestr('manifest.json',payload)
                z.writestr('manifest.sha256',hashlib.sha256(payload).hexdigest())
            publish_new(temp,target)
        finally:temp.unlink(missing_ok=True)
        return dict(preparation,file=str(target),images=count,portable_images=portable)

    def _load_backup_index(self,z,db):
        """将固定格式索引块写入本机自建临时表；不执行备份提供的 SQL。"""
        if len(z.infolist())>2_010_002:raise ValueError('缓存备份条目过多')
        info=z.getinfo('manifest.json')
        if info.file_size>256*1024**2:raise ValueError('缓存数据库清单超过 256 MB')
        if z.getinfo('manifest.sha256').file_size!=64:raise ValueError('缓存清单校验码无效')
        payload=z.read(info)
        if z.read('manifest.sha256').decode('ascii')!=hashlib.sha256(payload).hexdigest():raise ValueError('缓存数据库清单校验失败')
        manifest=json.loads(payload)
        if manifest.get('schema')!=SCHEMA or manifest.get('version') not in (1,2,BACKUP_VERSION) or manifest.get('fingerprint')!='sha256-full-v1':raise ValueError('不支持的缓存备份格式')
        db.executescript("""CREATE TABLE images(cache TEXT,name TEXT,accessed INTEGER,payload TEXT,PRIMARY KEY(cache,name));
            CREATE TABLE aliases(cache TEXT,name TEXT,payload TEXT);
            CREATE INDEX aliases_name ON aliases(cache,name);
            CREATE TABLE admitted(cache TEXT,name TEXT,target_cache TEXT,target_name TEXT,PRIMARY KEY(cache,name));""")
        chunks={'manifest.json','manifest.sha256'}
        def metadata():
            if manifest['version']==1:yield manifest;return
            listing=manifest.get('chunks')
            if not isinstance(listing,list) or len(listing)>10000:raise ValueError('缓存索引块清单无效')
            for chunk in listing:
                name=chunk.get('name','')
                if not re.fullmatch(r'indexes/[0-9]{6}\.json',name) or name in chunks:raise ValueError('缓存索引块名称无效')
                chunks.add(name);info=z.getinfo(name)
                if type(chunk.get('bytes')) is not int or not 0<info.file_size==chunk['bytes']<=4*1024**2:raise ValueError('缓存索引块大小无效')
                payload=z.read(info)
                if hashlib.sha256(payload).hexdigest()!=chunk.get('sha256'):raise ValueError('缓存索引块校验失败')
                yield json.loads(payload)
        for part in metadata():
            rows=part.get('previews');aliases=part.get('aliases')
            if not isinstance(rows,list) or not isinstance(aliases,list):raise ValueError('缓存数据库结构不完整')
            for row in rows:
                if row.get('cache') not in NAMES or not previews.PreviewCache._registered(row) or not HEX.fullmatch(str(row.get('digest',''))):raise ValueError('缓存登记信息无效')
                if type(row['edge']) is not int or not 160<=row['edge']<=4096 or type(row['orientation']) is not int or not 1<=row['orientation']<=8 or row['decoded'] not in (0,1):raise ValueError('缓存生成参数无效')
                if type(row['accessed']) is not int or row['accessed']<0:raise ValueError('缓存访问时间无效')
                if type(row['bytes']) is not int or not 4<=row['bytes']<=64*1024**2:raise ValueError('单张缓存大小无效')
                if z.getinfo(row['cache']+'/'+row['name']).file_size!=row['bytes']:raise ValueError('缓存图片大小不符')
                try:db.execute('INSERT INTO images VALUES (?,?,?,?)',(row['cache'],row['name'],row['accessed'],json.dumps(row)))
                except sqlite3.IntegrityError:raise ValueError('缓存备份包含重复图片') from None
            for row in aliases:db.execute('INSERT INTO aliases VALUES (?,?,?)',(row.get('cache'),row.get('name'),json.dumps(row)))
        for row in z.infolist():
            if row.filename in chunks:continue
            parts=row.filename.split('/')
            if len(parts)!=2 or parts[0] not in NAMES or not db.execute('SELECT 1 FROM images WHERE cache=? AND name=?',parts).fetchone():raise ValueError('缓存备份包含未登记文件或非法路径')
        if len(z.namelist())!=len(set(z.namelist())):raise ValueError('缓存备份包含重复条目')
        for cache,name,payload in db.execute('SELECT * FROM aliases'):
            row=json.loads(payload);photo=db.execute('SELECT payload FROM images WHERE cache=? AND name=?',(cache,name)).fetchone()
            if not HEX.fullmatch(str(row.get('content',''))) or not photo:raise ValueError('内容指纹索引无效')
            photo=json.loads(photo[0])
            if any(row.get(k)!=photo.get(k) for k in ('edge','orientation','decoded')):raise ValueError('内容指纹与预览参数不符')
            recipe=row.get('recipe',4)
            if recipe not in (3,4,5,6) or previews.cache_name({'fingerprint':photo['fingerprint'],'orientation':photo['orientation']},photo['edge'],bool(photo['decoded']),version=recipe)!=photo['name']:raise ValueError('内容指纹与生成版本不符')
            if row.get('probe') is not None and not HEX.fullmatch(str(row['probe'])):raise ValueError('缓存候选预筛指纹无效')
        db.commit()
        return db.execute('SELECT count(*) FROM images').fetchone()[0]

    def import_(self,value):
        source=Path(value.strip().strip('"')).expanduser().resolve()
        if not source.is_file():raise ValueError('缓存备份文件不存在')
        imported=0;skipped=0;conflicts=0;reused=0
        with tempfile.TemporaryDirectory(dir=self.root,prefix='.framekeep-import-') as folder, zipfile.ZipFile(source) as z, closing(sqlite3.connect(Path(folder)/'index.sqlite3')) as db:
            total=self._load_backup_index(z,db);preparation=self.prepare();remaining=self.budget.limit
            for payload, in db.execute('SELECT payload FROM images ORDER BY accessed DESC'):
                row=json.loads(payload)
                if row['bytes']>remaining:skipped+=1;continue
                cache=previews.cache_for(self.data/row['cache']);entry=row['cache']+'/'+row['name']
                if shutil.disk_usage(cache.folder).free<row['bytes']+16*1024**2:raise OSError('缓存磁盘可用空间不足，已导入内容保留，可释放空间后重试')
                with tempfile.NamedTemporaryFile(dir=cache.folder,prefix='.framekeep-import-',suffix='.tmp',delete=False) as stream:
                    temp=Path(stream.name)
                    try:
                        with z.open(entry) as incoming:shutil.copyfileobj(incoming,stream,1024**2)
                    except BaseException:stream.close();temp.unlink(missing_ok=True);raise
                try:
                    if temp.stat().st_size!=row['bytes'] or cache._digest(temp)!=row['digest']:raise ValueError('缓存图片 SHA-256 校验失败')
                    with temp.open('rb') as f:
                        validate_stream(f,row['bytes'])
                    from PIL import Image
                    with Image.open(temp) as image:
                        if image.format not in ('JPEG','WEBP') or max(image.size)>row['edge']:raise ValueError('缓存图片生成参数不符')
                        image.verify()
                    existing=None
                    for alias_payload, in db.execute('SELECT payload FROM aliases WHERE cache=? AND name=?',(row['cache'],row['name'])):
                        alias=json.loads(alias_payload)
                        if alias.get('recipe',4) in (4,5,6):
                            existing=cache.find_content(alias['content'],alias['edge'],alias['orientation'],alias['decoded'])
                            if existing is not None:break
                    if existing is not None:
                        owner=previews.cache_for(existing.parent)
                        db.execute('INSERT INTO admitted VALUES (?,?,?,?)',(row['cache'],row['name'],owner.folder.name,existing.name));imported+=1;reused+=1
                        self.progress('已按完整内容指纹复用现有缓存…',done=imported,total=total)
                        continue
                    with self.budget.generation(),cache.lock,cache.connect() as c:
                        target=cache.folder/row['name']
                        if target.exists():
                            old=c.execute('SELECT digest FROM previews WHERE name=?',(row['name'],)).fetchone()
                            if not old or old[0]!=row['digest'] or not cache._owned(c,row['name']):conflicts+=1;continue
                        else:
                            cache._trim(c,row['bytes']);publish_new(temp,target)
                            fields=('name','bytes','accessed','fingerprint','edge','decoded','orientation','digest')
                            c.execute('INSERT INTO previews('+','.join(fields)+') VALUES (?,?,?,?,?,?,?,?)',tuple(row[k] for k in fields))
                        db.execute('INSERT INTO admitted VALUES (?,?,?,?)',(row['cache'],row['name'],cache.folder.name,row['name']));remaining-=row['bytes'];imported+=1
                    self.progress('正在校验并合并缓存，不覆盖已有文件…',done=imported,total=total)
                finally:temp.unlink(missing_ok=True)
            for payload,folder,name in db.execute('SELECT a.payload,t.target_cache,t.target_name FROM aliases a JOIN admitted t ON a.cache=t.cache AND a.name=t.name'):
                row=json.loads(payload);cache=previews.cache_for(self.data/folder)
                cache.bind_content(row['content'],{'orientation':row['orientation'],'_portable_probe':row.get('probe')},row['edge'],row['decoded'],name)
        return dict(preparation,imported=imported,reused=reused,skipped=skipped,conflicts=conflicts)

    def close(self):
        if self.thread and self.thread.is_alive():self.thread.join(timeout=3)


# Python 关键字不能作为函数定义名称；外部动作仍使用 import。
setattr(CacheStorage,'import',CacheStorage.import_)
