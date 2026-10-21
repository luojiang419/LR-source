"""现场二星补排队与归档顶级目录的稳定增量监控。"""
import json
from pathlib import Path
import threading
import time

from folder_watch import DirectorySignal,files_ready
import media_engine as media


class ArchivePipeline:
    INTERVAL=2.0
    STABLE_SECONDS=.5
    FALLBACK_SECONDS=15.0

    def __init__(self,catalog,archive,sku,rawlink,*,autostart=True,clock=time.monotonic):
        self.catalog=catalog;self.archive=archive;self.store=archive.store;self.sku=sku;self.rawlink=rawlink
        self.clock=clock;self.closed=threading.Event();self.signals={};self.last_scan={};self.observed={};self.accepted={};self.pending=set()
        with self.store.connect() as c:
            sessions=[r[0].split(':',1)[1] for r in c.execute("SELECT key FROM settings WHERE key LIKE 'shooting_session:%'")]
        for sid in sessions:
            with self.sku.connect() as c:c.execute('INSERT OR REPLACE INTO live_sessions VALUES (?,?)',(sid,time.time()))
            self.sku.request_catalog(sid)
        self.thread=threading.Thread(target=self._run,daemon=True,name='archive-pipeline')
        if autostart:self.thread.start()

    def changed(self,sid):
        with self.store.connect() as c:
            live=c.execute('SELECT 1 FROM settings WHERE key=?',('shooting_session:'+sid,)).fetchone()
        if live:
            with self.sku.connect() as c:c.execute('INSERT OR REPLACE INTO live_sessions VALUES (?,?)',(sid,time.time()))
            self.sku.request_catalog(sid)

    def tick(self):
        roots=self.catalog.state()['roots'];active={r['id'] for r in roots}
        with self.store.connect() as c:
            tracked=[(r['key'].split(':',1)[1],r['value']) for r in c.execute("SELECT key,value FROM settings WHERE key LIKE 'archive_root:%'")]
            for rid,sid in tracked:
                if rid not in active:c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('archive_disabled:'+sid,'1'))
        for rid in set(self.signals)-active:self.signals.pop(rid).close()
        for root in roots:
            rid=root['id'];base=Path(root['path']);now=self.clock()
            signal=self.signals.get(rid)
            if not signal:
                if not base.is_dir():continue
                signal=self.signals[rid]=DirectorySignal(base,True)
            if rid not in self.pending and rid in self.last_scan and not signal.changed() and now-self.last_scan[rid]<self.FALLBACK_SECONDS:continue
            self.last_scan[rid]=now
            try:self._scan(root)
            except (OSError,ValueError,RuntimeError) as exc:
                with self.catalog.connect() as c:c.execute("UPDATE roots SET status='offline' WHERE id=?",(rid,))
                with self.archive.connect() as c:
                    from asset_archive import uid,dumps
                    c.execute('INSERT INTO audit VALUES (?,?,?,?)',(uid(),'archive_watch_error',dumps({'root':rid,'error':str(exc)[:250]}),time.time()))
        with self.archive.connect() as c:
            catalogued=[dict(r) for r in c.execute("SELECT l.path,a.sku,a.normalized FROM locations l JOIN files f ON f.id=l.file JOIN assets a ON a.id=f.asset WHERE l.status='online' AND a.sku!=''")]
        with self.catalog.connect() as c:
            for row in catalogued:c.execute("UPDATE items SET sku=?,normalized=?,source='embedded',confidence=1 WHERE path=? COLLATE NOCASE AND source!='manual' AND (sku!=? OR source!='embedded')",(row['sku'],row['normalized'],row['path'],row['sku']))

    def _scan(self,root):
        base=Path(root['path']);rid=root['id'];ready=[];pending=False;present=set()
        with self.catalog.connect() as c:excluded={r[0] for r in c.execute('SELECT folder FROM exclusions WHERE root=?',(rid,))}
        # discover 保留相对路径，按目录配对 JPG/RAW；排除用户移出的子树。
        _,shots=media.discover(base,cancel=self.closed)
        sid=self.store.ensure_live_session(base)
        with self.store.connect() as c:
            c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('archive_session:'+sid,str(base)))
            c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('archive_root:'+rid,sid))
            c.execute('DELETE FROM settings WHERE key=?',('archive_disabled:'+sid,))
            c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('archive_excluded:'+sid,json.dumps(list(excluded))))
        self.rawlink.add_target(str(base))
        existing={p.casefold():r for r in self.store.records(sid) for p in (r.get('raw'),r.get('jpg')) if p}
        for primary,companion in shots:
            if self.closed.is_set():return
            rel=primary.relative_to(base).as_posix();folder=primary.parent.relative_to(base).as_posix()
            if any(folder==skip or folder.startswith(skip+'/') for skip in excluded):continue
            paths=[primary]+([companion] if companion else []);key=(rid,rel);present.add(key)
            signature=tuple((str(p),p.stat().st_size,p.stat().st_mtime_ns) for p in paths)
            if self.accepted.get(key)==signature:continue
            old=self.observed.get(key)
            if not old or old[0]!=signature:self.observed[key]=(signature,self.clock());pending=True;continue
            if self.clock()-old[1]<self.STABLE_SECONDS or not files_ready(paths):pending=True;continue
            previous=existing.get(rel.casefold())
            if previous and previous['fingerprint']==media.fingerprint(paths):self.accepted[key]=signature;continue
            ready.append((primary,companion,key,signature))
        if ready:
            exif=media.find_exiftool(self.store.root);records=[];embedded={}
            for start in range(0,len(ready),24):
                batch=ready[start:start+24];metadata=media.read_metadata([r[0] for r in batch],exif,cancel=self.closed)
                for (primary,companion,key,signature),info in zip(batch,metadata):
                    paths=[primary]+([companion] if companion else [])
                    current=tuple((str(p),p.stat().st_size,p.stat().st_mtime_ns) for p in paths)
                    if info.get('Error') or current!=signature:pending=True;continue
                    hints=self._index_embedded(paths)
                    if len(hints)==1:embedded[primary.relative_to(base).as_posix().casefold()]=next(iter(hints))
                    records.append(media.capture_record(base,primary,companion,info));self.accepted[key]=signature
            if records:
                self.store.append_live(sid,records)
                for record in self.store.records(sid):
                    hint=embedded.get((record.get('raw') or record.get('jpg')).casefold())
                    if hint:self.archive.ensure(sid,record['id'],asset_hint=hint,inherit=False)
                self.rawlink.request(sid)
        removed=[key for key in self.accepted if key[0]==rid and key not in present]
        for key in removed:self.accepted.pop(key,None);self.observed.pop(key,None)
        if ready or removed or root['status']=='offline':self.catalog.scan(rid)
        if pending:self.pending.add(rid)
        else:self.pending.discard(rid)
        # 已知位置移动后旧位置保留历史，显式标为离线。
        with self.archive.connect() as c:
            for row in c.execute('SELECT path FROM locations WHERE status=\'online\'').fetchall():
                path=Path(row['path'])
                try:path.relative_to(base)
                except ValueError:continue
                if not path.is_file():c.execute("UPDATE locations SET status='offline' WHERE path=?",(str(path),))

    def _index_embedded(self,paths):
        """记录保留身份的 PS/LR 派生版本，不把文件名当成永久身份。"""
        from metadata_writer import sha
        from asset_archive import dumps,uid
        assets=set()
        for path in paths:
            metadata=self.archive.writer.read(path);aid=str(metadata.get('XMP-fk:AssetId') or '').replace('-','');fid=str(metadata.get('XMP-fk:FileId') or '').replace('-','')
            if not aid or not fid:continue
            with self.archive.connect() as c:
                file=c.execute('SELECT * FROM files WHERE id=? AND asset=?',(fid,aid)).fetchone()
                if not file:continue
                digest=sha(path)
                c.execute('INSERT OR IGNORE INTO versions VALUES (?,?,?)',(fid,digest,time.time()))
                c.execute('INSERT OR REPLACE INTO locations VALUES (?,?,?,?)',(str(path),fid,time.time(),'online'))
                c.execute('INSERT INTO audit VALUES (?,?,?,?)',(uid(),'discover_embedded_copy',dumps({'asset':aid,'file':fid,'path':str(path),'sha':digest}),time.time()))
                assets.add(aid)
        return assets

    def _run(self):
        while not self.closed.is_set():
            try:self.tick()
            except Exception:pass
            self.closed.wait(self.INTERVAL)

    def close(self):
        self.closed.set()
        if self.thread.is_alive():self.thread.join(timeout=5)
        for signal in self.signals.values():signal.close()
