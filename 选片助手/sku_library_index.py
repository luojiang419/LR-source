"""SKU 浏览的持久读投影；前台不重新划分拍摄区间，也不探测原片。"""
from contextlib import contextmanager,closing
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote,urlencode
import uuid

from file_safety import atomic_write

SCHEMA='framekeep.sku-browser-v5'
READABLE_SCHEMAS={SCHEMA,'framekeep.sku-browser-v1','framekeep.sku-browser-v2','framekeep.sku-browser-v3','framekeep.sku-browser-v4'}
REVISION='sku_browser_revision'
MAX_BYTES=64*1024*1024
_GUARD=threading.Lock()


class ReadConnection(sqlite3.Connection):
    def execute(self,sql,parameters=()):
        if sql.lstrip().upper().startswith('ATTACH DATABASE'):
            alias=sql.rsplit(' ',1)[-1]
            if alias in {r[1] for r in super().execute('PRAGMA database_list')}:return super().execute('SELECT 1 WHERE 0')
            result=super().execute(sql,parameters);super().execute('PRAGMA '+alias+'.mmap_size=2147483648');return result
        return super().execute(sql,parameters)


class Reader:
    def __init__(self,path):self.path=Path(path);self.lock=threading.RLock();self.connection=None
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    @contextmanager
    def connect(self):
        with self.lock:
            if self.connection is None:
                self.connection=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=.2,check_same_thread=False,factory=ReadConnection)
                self.connection.row_factory=sqlite3.Row;self.connection.execute('PRAGMA mmap_size=2147483648')
                self.connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,True)
            yield self.connection
    def close(self):
        with self.lock:
            if self.connection is not None:self.connection.close();self.connection=None


class ReadSku:
    """仅用于投影/查询的只读适配器，不取得业务写入锁。"""
    def __init__(self,sku):
        from catalog_store import Store
        self.readers=[Reader(sku.db),Reader(sku.store.db)];self.db=sku.db;self.connect=self.readers[0].connect
        self.store=SimpleNamespace(db=sku.store.db,data=sku.store.data,connect=self.readers[1].connect)
        self.store.session=lambda sid:Store.session(self.store,sid)
        self.store.records=lambda sid,ids=None:Store.records(self.store,sid,ids)
        self.archive=None;self._library_schema_ready=True
        if sku.archive:
            self.readers.append(Reader(sku.archive.db));self.archive=SimpleNamespace(db=sku.archive.db,connect=self.readers[-1].connect)
            # 复用星级读取算法，使用只读连接；不运行建表、种子评级或文件写回器。
            from shared_ratings import SharedRatings
            ratings=SharedRatings.__new__(SharedRatings);ratings.archive=self.archive;ratings.store=self.store
            self.archive.ratings=ratings
    def close(self):
        for reader in self.readers:reader.close()


def _initialize_source(path,tables):
    with closing(sqlite3.connect(path,timeout=.5)) as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(REVISION,)).fetchone():return
        present={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.execute('BEGIN IMMEDIATE')
        try:
            c.execute(f'CREATE TABLE {REVISION}(id INTEGER PRIMARY KEY,identity TEXT NOT NULL,value INTEGER NOT NULL)')
            c.execute(f'INSERT INTO {REVISION} VALUES(1,?,0)',(uuid.uuid4().hex,))
            for table,condition in tables.items():
                if table not in present:continue
                for event in ('INSERT','DELETE','UPDATE'):
                    test=condition if event=='UPDATE' else ''
                    if table=='settings':
                        ref='old' if event=='DELETE' else 'new'
                        gate=f"({ref}.key LIKE 'archive_%' OR {ref}.key LIKE 'removed_session:%')"
                        test=gate+(' AND ('+test+')' if test else '')
                    when=' WHEN '+test if test else ''
                    c.execute(f'CREATE TRIGGER sku_browser_{table}_{event.lower()} AFTER {event} ON {table}{when} BEGIN UPDATE {REVISION} SET value=value+1 WHERE id=1; END')
            c.commit()
        except BaseException:c.rollback();raise


def _changed(*fields):return ' OR '.join(f'old.{field} IS NOT new.{field}' for field in fields)


class SkuLibraryIndex:
    def __init__(self,sku):
        self.sku=sku;self.reader=ReadSku(sku);self.path=sku.store.data/'sku-browser'/'snapshot.json'
        self.sources=[(Path(sku.db),{'jobs':_changed('status','result','targets','updated'),'parse_jobs':_changed('status','result','updated'),'ocr_choices':'','sku_rows':_changed('sku','normalized'),'metadata_archive_results':''}),
                      (Path(sku.store.db),{'captures':_changed('data','position','session','id'),'sessions':"old.root IS NOT new.root OR json_extract(old.state,'$.overrides') IS NOT json_extract(new.state,'$.overrides')",'settings':"old.value IS NOT new.value AND (new.key LIKE 'archive_%' OR new.key LIKE 'removed_session:%')"})]
        if sku.archive:self.sources.append((Path(sku.archive.db),{'assets':_changed('sku','scope','project','locked','source','segment'),'catalog_links':'','assignments':'','shared_ratings':_changed('rating','updated'),'portable_sku_evidence':_changed('data','revision'),'portable_ratings':_changed('rating','updated')}))
        self.identity=hashlib.sha256('|'.join(str(p.absolute()).casefold()+':'+str(p.stat().st_ino) for p,_ in self.sources).encode()).hexdigest()
        self.version_readers=[Reader(p) for p,_ in self.sources]
        catalog=sku.store.data/'asset_catalog.sqlite3'
        self.catalog_reader=Reader(catalog) if catalog.is_file() else None
        self.lock=threading.RLock();self.build_lock=threading.Lock();self.wake=threading.Event();self.stopped=threading.Event();self.changed=threading.Event()
        self.value=None;self.error='';self.initialized=False;self.thread=None
        self.query_lock=threading.Lock();self.query_cache={};self.query_source=None
        saved=self._load()
        if saved and saved.get('identity')==self.identity:self.value=saved

    def _load(self):
        try:
            with self.path.open('rb') as stream:raw=stream.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES:return None
            data=json.loads(raw)
            # 受管旧格式可先显示并升级；未知文件仍保持原样，不能因版本变化永远拒绝写回。
            if data.get('schema') in READABLE_SCHEMAS and isinstance(data.get('projection',{}).get('rows'),list) and isinstance(data['projection'].get('intervals'),list) and isinstance(data.get('signature'),list) and isinstance(data.get('identity'),str):return data
        except (OSError,ValueError,TypeError):pass
        return None

    def signature(self):
        result=[]
        for reader in self.version_readers:
            with reader.connect() as c:row=c.execute(f'SELECT identity,value FROM {REVISION} WHERE id=1').fetchone()
            result.append(list(row))
        if self.catalog_reader:
            with self.catalog_reader.connect() as c:
                version=c.execute('SELECT value,serial FROM catalog_page_revision WHERE id=1').fetchone()
            result.append(['catalog',*version])
        return result

    def initialize(self):
        if self.initialized:return
        from sku_library_management import initialize
        initialize(self.sku)
        for path,tables in self.sources:_initialize_source(path,tables)
        self.initialized=True

    def refresh(self):
        with self.build_lock:
            self.initialize();signature=self.signature()
            if self.value and self.value['schema']==SCHEMA and self.value['signature']==signature:return True
            from sku_library import project
            from sku_metadata_sync import prefer_local_archives
            reader=ReadSku(self.sku)
            try:
                projection=project(reader)
                projection['rows']=prefer_local_archives(reader,projection['rows']) if reader.archive else projection['rows']
                projection['local_preferred']=True
                records={}
                by_session={}
                for row in projection['rows']:by_session.setdefault(row['session_id'],[]).append(row['capture_id'])
                for sid,ids in by_session.items():
                    session=reader.store.session(sid)
                    records[sid]={'root':session['root'] if session else '', 'captures':{}}
                    if not session:continue
                    for start in range(0,len(ids),400):records[sid]['captures'].update({r['id']:r for r in reader.store.records(sid,ids=ids[start:start+400])})
                for row in projection['rows']:
                    record=records[row['session_id']]['captures'].get(row['capture_id'],{})
                    row['rating']=max(0,record.get('liveRating',record.get('existingRating',row.get('rating',0))))
                if reader.archive:
                    from shared_ratings import SharedRatings
                    try:SharedRatings.decorate(SimpleNamespace(archive=reader.archive),projection['rows'])
                    except sqlite3.Error:pass
                try:self._preview_hints(reader,projection['rows'],records)
                except (OSError,sqlite3.Error):pass # 可选的缓存捷径不能阻断已知款号和图片资料。
                for row in projection['rows']:
                    if row.get('preview','').startswith('/api/preview/'):
                        row['preview']=row['preview'].replace('/api/preview/','/api/sku/library/preview/',1)
            finally:reader.close()
            if self.stopped.is_set() or self.signature()!=signature:return False
            value={'schema':SCHEMA,'identity':self.identity,'signature':signature,'projection':projection,'created':time.time()}
            with self.lock:
                self.value=value;self.error=''
                content=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf8')
                try:
                    if len(content)<=MAX_BYTES:
                        self.path.parent.mkdir(exist_ok=True)
                        if not self.path.parent.is_symlink() and not self.path.parent.is_junction() and not self.path.is_symlink() and (not self.path.exists() or self._load()):atomic_write(self.path,content)
                except OSError as error:self.error=str(error) # 持久化故障不能隐藏已构造的列表。
            self.changed.set();return True

    def _preview_hints(self,reader,rows,records):
        """在后台批量匹配现场和归档缓存；只读取索引，不读取原图。"""
        from preview_cache import registered_recipe
        candidates={};family={}
        if reader.archive:
            with reader.connect() as c:
                for r in c.execute('SELECT session,capture,before,after FROM metadata_versions'):
                    family.setdefault((r['session'],r['capture']),[]).append((r['before'],r['after']))
        for row in rows:
            record=records[row['session_id']]['captures'].get(row['capture_id'],{})
            fingerprints={record.get('fingerprint','')};prior=family.get((row['session_id'],row['capture_id']),[])
            for _ in range(len(prior)+1):
                old=set(fingerprints);fingerprints.update(before for before,after in prior if after in old)
                if old==fingerprints:break
            candidates[row['id']]={'previews':fingerprints}
        catalog=self.sku.store.data/'asset_catalog.sqlite3'
        if catalog.is_file():
            from asset_catalog import _id
            with Reader(catalog) as catalog_reader,catalog_reader.connect() as c:
                mapping={}
                paths=list({r['path'] for r in rows})
                for start in range(0,len(paths),400):
                    batch=paths[start:start+400]
                    mapping.update({r['path']:r['pair_key'] for r in c.execute('SELECT path,pair_key FROM items WHERE path IN ('+','.join('?' for _ in batch)+')',batch)})
                groups={};keys=list(set(mapping.values()))
                for start in range(0,len(keys),400):
                    batch=keys[start:start+400]
                    for r in c.execute('SELECT pair_key,fingerprint FROM items INDEXED BY catalog_pair WHERE online=1 AND pair_key IN ('+','.join('?' for _ in batch)+') ORDER BY extension,path',batch):groups.setdefault(r['pair_key'],[]).append(r['fingerprint'])
            for row in rows:
                members=groups.get(mapping.get(row['path']))
                if members:candidates[row['id']]['catalog_previews']={_id('|'.join(members)+':preview-v2')}
        try:location=json.loads((self.sku.store.data/'image-cache-location.json').read_text(encoding='utf8'))['path']
        except (OSError,ValueError,KeyError):location=self.sku.store.data
        hits={}
        for bucket in ('previews','catalog_previews'):
            db=Path(location)/bucket/'index.sqlite3'
            if not db.is_file():continue
            fingerprints=list({f for values in candidates.values() for f in values.get(bucket,[]) if f})
            cache_reader=Reader(db)
            try:
                with cache_reader.connect() as c:
                    for start in range(0,len(fingerprints),300):
                        batch=fingerprints[start:start+300]
                        for r in c.execute('SELECT * FROM previews WHERE fingerprint IN ('+','.join('?' for _ in batch)+') AND edge>=420 AND decoded=0 ORDER BY edge',batch):
                            if registered_recipe(r) is not None:hits.setdefault((bucket,r['fingerprint']),dict(r))
            finally:cache_reader.close()
        for row in rows:
            record=records[row['session_id']]['captures'].get(row['capture_id'],{})
            choices=[(bucket,hits[bucket,f]) for bucket,values in candidates[row['id']].items() for f in values if (bucket,f) in hits and (bucket=='catalog_previews' or hits[bucket,f]['orientation']==record.get('orientation',1))]
            if choices:
                bucket,hit=min(choices,key=lambda item:item[1]['edge'])
                row['preview']='/api/sku/library/preview/'+quote(row['session_id'])+'/'+quote(row['capture_id'])+'?'+urlencode({'cached':hit['name'],'bucket':bucket,'edge':hit['edge']})

    def start(self):
        with self.lock:
            if self.thread is None:
                self.thread=threading.Thread(target=self._run,name='sku-library-view',daemon=True);self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:self.refresh()
            except (OSError,ValueError,sqlite3.Error) as error:self.error=str(error)
            self.wake.wait(2);self.wake.clear()

    def browse(self,*args,fresh=False,**kwargs):
        self.start()
        if fresh:
            for _ in range(3):
                if self.refresh():break
            else:raise ValueError('图片资料仍在更新，请稍后重新同步清单')
        value=self.value
        if value is None:
            return {'groups':[],'roots':[],'rows':[],'total':0,'offset':int(kwargs.get('offset',0)),'has_more':False,'interval_count':0,'intervals':[],'organization':{'bins':[],'total':0,'ungrouped':0,'removed':0},'loading':True}
        from sku_library import browse
        # 管理操作和辅助OCR检索也依赖业务库；data_version变化即丢弃查询缓存。
        # 每个Reader独立只读，不等待业务写锁；限定缓存数量，避免跨款浏览无限增长。
        with self.query_lock:
            versions=[]
            for reader in self.version_readers:
                with reader.connect() as c:versions.append(c.execute('PRAGMA data_version').fetchone()[0])
            source=(id(value),tuple(versions))
            if source!=self.query_source:self.query_cache.clear();self.query_source=source
            key=(args,tuple(sorted(kwargs.items())))
            data=self.query_cache.get(key)
            if data is None:
                data=browse(self.reader,*args,projection=value['projection'],probe_files=False,**kwargs)
                if len(self.query_cache)>=16:self.query_cache.pop(next(iter(self.query_cache)))
                self.query_cache[key]=data
        try:refreshing=self.signature()!=value['signature']
        except sqlite3.Error:refreshing=True
        return {**data,'loading':False,'refreshing':refreshing or value['schema']!=SCHEMA,'revision':value['created']}

    def close(self):
        self.stopped.set();self.wake.set()
        if self.thread and self.thread.is_alive():self.thread.join(timeout=2)
        self.reader.close()
        for reader in self.version_readers:reader.close()
        if self.catalog_reader:self.catalog_reader.close()


def for_sku(sku):
    with _GUARD:
        if not getattr(sku,'_browse_index',None):sku._browse_index=SkuLibraryIndex(sku)
        return sku._browse_index
