"""归档按访问读取：浅层目录核对与缩略图缓存，不恢复全库后台任务。"""
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from pathlib import Path
import threading
import time


class ArchiveVisits:
    def __init__(self,catalog):
        self.catalog=catalog;self.lock=threading.RLock();self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='archive-visit')
        # 目录切换共用一个有界池，旧目录慢读不能为每次切换另开一批 NAS 连接。
        self.preview_workers=3
        self.preview_pool=ThreadPoolExecutor(max_workers=self.preview_workers,thread_name_prefix='archive-thumbnail')
        self.generation=0;self.path=None;self.key=None;self.touched=0;self.error='';self.closed=False
        self.running=False;self.cached=0;self.total=0;self.revision=0;self.failed=0;self.indexed=0
        # 直读模式在内存中预热的缩略图数量（显示顺序前若干张）；不写磁盘缓存。
        self.warm_limit=2000;self.warmed=0;self.warm_total=0
        with catalog.connect() as c:c.execute('CREATE TABLE IF NOT EXISTS visited_directories(root TEXT,folder TEXT,parent TEXT,PRIMARY KEY(root,folder))')
        from archive_explorer import ArchiveExplorer
        self.explorer=ArchiveExplorer(catalog)

    def tree(self):
        with self.catalog.connect() as c:
            roots=[dict(r,folders=[]) for r in c.execute("SELECT id,path,'ready' AS status FROM roots WHERE removed=0 ORDER BY path")]
            directories=list(c.execute('SELECT root,folder,NULL FROM visited_directories ORDER BY folder'))
        actual,parents=self.explorer.tree_rows()
        known={(r,f) for r,f,_ in actual}
        directories=[(r,f,n) for r,f,n in directories if (r,f) not in known and (r,'' if Path(f).parent.as_posix()=='.' else Path(f).parent.as_posix()) not in parents]+actual
        by_id={r['id']:r for r in roots}
        for root,folder,count in directories:
            if root in by_id:by_id[root]['folders'].append({'folder':folder,'count':count or 0,'known':count is not None})
        return {'roots':roots,'browser':True}

    def allows(self,path):
        with self.lock:return not self.closed and self.path is not None and time.monotonic()-self.touched<30 and Path(path).parent==self.path

    def touch(self,root,folder='',*,refresh=False):
        # 字符串及索引校验留在请求线程，网络路径解析由工作线程完成。
        from pathlib import PurePosixPath
        if folder=='/':folder=''
        parts=PurePosixPath(str(folder).replace('\\','/'))
        if parts.is_absolute() or any(p in ('.','..') or ':' in p for p in parts.parts):raise ValueError('归档子目录路径无效')
        path=Path(self.catalog._root(root)['path']).joinpath(*parts.parts) if root else None
        key=(root,str(folder).replace('\\','/').strip('/')) if root else None
        with self.lock:
            self.touched=time.monotonic()
            if key==self.key and not refresh:return self.state()
            self.generation+=1;version=self.generation;self.key=key;self.path=path
            self.error='';self.cached=0;self.total=0;self.failed=0;self.indexed=0;self.warmed=0;self.warm_total=0;self.running=bool(root)
        if root:self.pool.submit(self._visit,root,key[1],version)
        return self.state()

    def current(self,version):
        with self.lock:return not self.closed and version==self.generation and time.monotonic()-self.touched<30

    def _visit(self,root,folder,version):
        try:
            directories=[]
            def flushed(count):
                # 每批新入库即递增版本，前端在目录读取期间逐步显示，不等整目录完成。
                with self.lock:
                    if version==self.generation:self.revision+=1;self.indexed+=count
            self.catalog._scan(root,visit_folder=folder,cancelled=lambda:not self.current(version),visit_directories=directories,on_flush=flushed)
            if not self.current(version):return
            with self.catalog.connect() as c:
                old={r[0] for r in c.execute('SELECT folder FROM visited_directories WHERE root=? AND parent=?',(root,folder))}
                for removed in old-set(directories):
                    pattern=removed.replace('\\','\\\\').replace('%',r'\%').replace('_',r'\_')+'/%'
                    c.execute("DELETE FROM visited_directories WHERE root=? AND (folder=? OR folder LIKE ? ESCAPE '\\')",(root,removed,pattern))
                c.execute('DELETE FROM visited_directories WHERE root=? AND parent=?',(root,folder))
                c.executemany('INSERT OR REPLACE INTO visited_directories VALUES(?,?,?)',[(root,d,folder) for d in directories])
                if folder:
                    parent=Path(folder).parent.as_posix()
                    c.execute('INSERT OR IGNORE INTO visited_directories VALUES(?,?,?)',(root,folder,'' if parent=='.' else parent))
                # ORDER BY name 会诱使优化器遍历全库排序索引；先约束当前层。
                rows=[dict(r) for r in c.execute('''SELECT id,path,media_type FROM items INDEXED BY catalog_folder_candidates
                    WHERE root=? AND online=1 AND folder COLLATE NOCASE=? AND folder=? ORDER BY name ASC''',(root,folder or '/',folder or '/'))]
            with self.lock:
                if version==self.generation:self.total=len(rows);self.revision+=1
            for start in range(0,len(rows),200):
                if not self.current(version):return
                self.catalog.metadata.request_many([r['id'] for r in rows[start:start+200]],verify=True)
            self._previews(rows,version)
            # 目录核对已完成：先公布结束，界面不再显示“读取中”；缩略图预热随后在后台进行。
            with self.lock:
                if version==self.generation and self.running:self.running=False;self.revision+=1
            self._warm(rows,version)
        except Exception as exc:
            with self.lock:
                if version==self.generation:self.error=str(exc)[:300]
        finally:
            with self.lock:
                if version==self.generation and self.running:self.running=False;self.revision+=1

    def _cache_preview(self,row,version):
        if not self.current(version) or not self.catalog.thumbnail_settings.cache_enabled:return
        # 缓存命中走独立只读快路，不能为后台核对占用共享缓存写锁/刷访问时间。
        try:self.catalog.preview(row['id'],420,read=True)
        except Exception:
            with self.lock:
                if version==self.generation:self.failed+=1
        else:
            with self.lock:
                if version==self.generation:self.cached+=1

    def _previews(self,rows,version):
        rows=iter(rows);pending=set();exhausted=False
        try:
            while self.current(version) and self.catalog.thumbnail_settings.cache_enabled:
                # 只提交在途窗口，不一次入队整个目录；切换后不会排着数百张旧图。
                while not exhausted and len(pending)<self.preview_workers and self.current(version) and self.catalog.thumbnail_settings.cache_enabled:
                    row=next(rows,None)
                    if row is None:exhausted=True;break
                    pending.add(self.preview_pool.submit(self._cache_preview,row,version))
                if not pending:return
                done,pending=wait(pending,timeout=.1,return_when=FIRST_COMPLETED)
                for future in done:future.result()
        finally:
            for future in pending:future.cancel()

    def _warm_one(self,row,version):
        if not self.current(version):return
        # 与浏览器请求共用同一内存结果与单飞生成；失败交由前端按需重试，不在此记失败。
        try:self.catalog.direct_thumbnail(row['id'],420)
        except Exception:return
        with self.lock:
            if version==self.generation:self.warmed+=1

    def _warm(self,rows,version):
        """直读模式：按显示顺序把当前文件夹缩略图预先读入内存，浏览器请求直接命中。"""
        if self.catalog.thumbnail_settings.cache_enabled:return
        targets=[r for r in rows if r.get('media_type')=='image'][:self.warm_limit]
        with self.lock:
            if version!=self.generation:return
            self.warm_total=len(targets)
        pending=set();source=iter(targets);exhausted=False
        try:
            while self.current(version):
                while not exhausted and len(pending)<self.preview_workers and self.current(version):
                    row=next(source,None)
                    if row is None:exhausted=True;break
                    pending.add(self.preview_pool.submit(self._warm_one,row,version))
                if not pending:return
                _,pending=wait(pending,timeout=.1,return_when=FIRST_COMPLETED)
        finally:
            for future in pending:future.cancel()

    def state(self):
        with self.lock:return {'mode':'visited','root':self.key[0] if self.key else '', 'folder':self.key[1] if self.key else '',
            'running':self.running,'cached':self.cached,'total':self.total,'failed':self.failed,'revision':self.revision,'error':self.error,
            'indexed':self.indexed,'warmed':self.warmed,'warm_total':self.warm_total,'thumbnail_cache_enabled':self.catalog.thumbnail_settings.cache_enabled}

    def close(self):
        with self.lock:self.closed=True;self.generation+=1;self.path=None
        self.pool.shutdown(wait=False,cancel_futures=True)
        self.preview_pool.shutdown(wait=False,cancel_futures=True)
