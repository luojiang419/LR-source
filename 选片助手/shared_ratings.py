"""按确切路径、配对文件及已验证永久身份共享人工评级。"""
from pathlib import Path
import hashlib
import json
import time

from asset_catalog import pair_key


class SharedRatings:
    def __init__(self, archive):
        self.archive = archive
        self.store = archive.store
        self.seed_versions = {}
        with archive.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS shared_ratings(
                  path_key TEXT PRIMARY KEY, rating INTEGER NOT NULL, updated INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS rating_capture_paths(
                  session TEXT,capture TEXT,path_key TEXT,PRIMARY KEY(session,capture,path_key));
                CREATE TABLE IF NOT EXISTS rating_seed_versions(session TEXT PRIMARY KEY,signature TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS rating_capture_path ON rating_capture_paths(path_key);
                CREATE INDEX IF NOT EXISTS rating_location_path ON locations(path COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS rating_locations_file ON locations(file,path);
                CREATE INDEX IF NOT EXISTS rating_files_asset ON files(asset,id);
                CREATE INDEX IF NOT EXISTS rating_links_asset ON catalog_links(asset,session);
            ''')
            c.execute('CREATE TABLE IF NOT EXISTS rating_view_version(id INTEGER PRIMARY KEY,value INTEGER NOT NULL)')
            c.execute('INSERT OR IGNORE INTO rating_view_version VALUES (1,0)')
            for table in ('shared_ratings','rating_capture_paths','catalog_links','locations','files'):
                for event in ('INSERT','DELETE','UPDATE'):
                    condition=''
                    if event=='UPDATE' and table=='files':condition=' WHEN old.asset IS NOT new.asset'
                    if event=='UPDATE' and table=='locations':condition=' WHEN old.path IS NOT new.path OR old.file IS NOT new.file'
                    c.execute('CREATE TRIGGER IF NOT EXISTS rating_view_'+table+'_'+event.lower()+' AFTER '+event+' ON '+table+condition+' BEGIN UPDATE rating_view_version SET value=value+1 WHERE id=1; END')
        from rating_sync import RatingSync
        self.sync = RatingSync(archive)

    def related_keys(self, paths, connection):
        paths = list(dict.fromkeys(str(Path(p).absolute()) for p in paths))
        keys={pair_key(p) for p in paths}
        # 文件名相同不足以关联；跨目录只使用既有匹配器验证过的资产关系。
        c=connection;assets=set()
        for path in paths:
            assets.update(r[0] for r in c.execute('SELECT f.asset FROM locations l JOIN files f ON f.id=l.file WHERE l.path=? COLLATE NOCASE', (path,)))
        for key in keys:
            assets.update(r[0] for r in c.execute('SELECT k.asset FROM rating_capture_paths p JOIN catalog_links k ON k.session=p.session AND k.capture=p.capture WHERE p.path_key=?',(key,)))
        for aid in assets:
            keys.update(pair_key(r[0]) for r in c.execute('SELECT l.path FROM locations l JOIN files f ON f.id=l.file WHERE f.asset=?', (aid,)))
            keys.update(r[0] for r in c.execute('SELECT p.path_key FROM catalog_links k JOIN rating_capture_paths p ON p.session=k.session AND p.capture=k.capture WHERE k.asset=?',(aid,)))
        return keys

    def get(self, paths, connection=None, *, explicit_only=False):
        paths=list(paths)
        direct={pair_key(p) for p in paths}
        def read(c):
            keys = self.related_keys(paths,c)
            rows = [(row,key in direct) for key in keys if (row := c.execute('SELECT rating,updated FROM shared_ratings WHERE path_key=?', (key,)).fetchone()) and (not explicit_only or row['updated']>0)]
            return max(rows, key=lambda entry:(entry[0]['updated'],entry[1]))[0]['rating'] if rows else None
        if connection is not None:return read(connection)
        with self.archive.connect() as c:return read(c)

    def set(self, paths, rating):
        if type(rating) is not int or rating not in range(-1, 6):
            raise ValueError('星级必须为 0–5 的整数')
        paths=list(paths)
        with self.archive.connect() as c:
            keys = self.related_keys(paths,c)
            stamp = time.time_ns()
            for key in keys:
                c.execute('INSERT INTO shared_ratings VALUES (?,?,?) ON CONFLICT(path_key) DO UPDATE SET rating=excluded.rating,updated=excluded.updated', (key, rating, stamp))
            assets=set()
            for path in paths:
                assets.update(r[0] for r in c.execute('SELECT f.asset FROM locations l JOIN files f ON f.id=l.file WHERE l.path=? COLLATE NOCASE',(str(Path(path).absolute()),)))
            related=[]
            for aid in assets:
                related.extend(r[0] for r in c.execute('SELECT l.path FROM locations l JOIN files f ON f.id=l.file WHERE f.asset=?',(aid,)))
            # 覆盖尚未进行资产备案的选片会话及 RAW/JPG 配对路径。
            for row in c.execute('SELECT session,capture FROM rating_capture_paths WHERE path_key IN ('+','.join('?' for _ in keys)+')',tuple(keys)):
                with self.store.connect() as catalog:
                    capture=catalog.execute('SELECT data FROM captures WHERE session=? AND id=?',(row['session'],row['capture'])).fetchone()
                import json
                record=json.loads(capture[0]) if capture else None
                if record:related.extend(self.paths(row['session'],record))
            self.sync.enqueue(c,paths+related,rating,stamp)
        return rating

    def paths(self, sid, record, root=None):
        root = root or self.store.session(sid)['root']
        return [str(Path(root) / record[kind]) for kind in ('raw', 'jpg') if record.get(kind)]

    def observe_sidecars(self,sid,records):
        """外部侧车更新只刷新评级读模型，不回写其他照片；本机待写评级优先。"""
        with self.archive.connect() as c:
            for record in records:
                paths=self.paths(sid,record)
                if any(c.execute("SELECT 1 FROM rating_file_sync WHERE path=? AND status IN ('queued','running','failed')",(str(path),)).fetchone() for path in paths):continue
                rating=record.get('sidecarMetadata',{}).get('Rating')
                if rating is None:continue
                for key in {pair_key(path) for path in paths}:
                    c.execute('INSERT INTO shared_ratings VALUES (?,?,?) ON CONFLICT(path_key) DO UPDATE SET rating=excluded.rating,updated=excluded.updated',(key,max(0,rating),time.time_ns()))

    def seed_session(self, sid):
        session = self.store.session(sid)
        if not session:
            return
        with self.store.connect() as db:structure=tuple(db.execute('SELECT generation,data_generation FROM browser_versions WHERE session=?',(sid,)).fetchone() or (0,0))
        version=(session['snapshot'],session['root'],structure,hashlib.sha256(json.dumps(session['state'].get('overrides',{}),sort_keys=True).encode()).digest())
        if self.seed_versions.get(sid)==version:return
        signature=hashlib.sha256(('paths-v1:'+repr(version)).encode('utf8')).hexdigest()
        with self.archive.connect() as c:cached=c.execute('SELECT signature FROM rating_seed_versions WHERE session=?',(sid,)).fetchone()
        if cached and cached[0]==signature:
            self.seed_versions[sid]=version;return
        import session_browser
        if session_browser.count(self.store,sid)>session_browser.LARGE_SESSION:
            self._seed_large(session)
            self._seeded(sid,version,signature)
            return
        overrides = session['state'].get('overrides', {})
        with self.store.connect() as catalog:
            shooting=bool(catalog.execute('SELECT 1 FROM settings WHERE key=?',('shooting_session:'+sid,)).fetchone())
        rows=[];members=[]
        for record in self.store.records(sid):
            keys={pair_key(p) for p in self.paths(sid,record,session['root'])}
            members.extend((sid,record['id'],key) for key in keys)
            rating = overrides.get(record['id'], record.get('existingRating', 0))
            if record['id'] in overrides or rating > 0:
                rows.extend((key,rating,0) for key in keys)
        with self.archive.connect() as c:
            c.execute('DELETE FROM rating_capture_paths WHERE session=?',(sid,))
            c.executemany('INSERT INTO rating_capture_paths VALUES (?,?,?)',members)
            c.executemany('INSERT INTO shared_ratings VALUES (?,?,?) ON CONFLICT(path_key) DO UPDATE SET rating=excluded.rating WHERE shared_ratings.updated=0',rows)
            for record in self.store.records(sid):
                paths=self.paths(sid,record,session['root'])
                value=self.get(paths,c)
                # 接管其他软件的拍摄目录只读取已有星级；主动标星由 saved/set 排队写入。
                if value is not None and not shooting:self.sync.enqueue(c,paths,value,time.time_ns(),initial=True)
        self._seeded(sid,version,signature)

    def _seeded(self,sid,version,signature):
        # 只在映射完整提交后记录签名；中断时宁可重建，不能跳过未完成的评级恢复。
        with self.archive.connect() as c:c.execute('INSERT OR REPLACE INTO rating_seed_versions VALUES (?,?)',(sid,signature))
        self.seed_versions[sid]=version

    def invalidate_seeds(self):
        self.seed_versions.clear()
        with self.archive.connect() as c:c.execute('DELETE FROM rating_seed_versions')

    def seed_records(self,session,records):
        """局部同步只登记涉及的路径，不清空其他照片的映射。"""
        members=[];seeds=[];overrides=session['state'].get('overrides',{})
        for record in records:
            cid=record['id'];keys={pair_key(p) for p in self.paths(session['id'],record,session['root'])}
            members.extend((session['id'],cid,key) for key in keys)
            rating=overrides.get(cid,record.get('existingRating',0))
            if cid in overrides or rating>0:seeds.extend((key,rating,0) for key in keys)
        with self.archive.connect() as db:
            db.executemany('INSERT OR IGNORE INTO rating_capture_paths VALUES (?,?,?)',members)
            db.executemany('INSERT INTO shared_ratings VALUES (?,?,?) ON CONFLICT(path_key) DO UPDATE SET rating=excluded.rating WHERE shared_ratings.updated=0',seeds)

    def _seed_large(self,session):
        """分批解码、原子发布路径映射；评级查询前必须先登记完整关联。"""
        sid=session['id'];overrides=session['state'].get('overrides',{})
        with self.store.connect() as db:shooting=bool(db.execute('SELECT 1 FROM settings WHERE key=?',('shooting_session:'+sid,)).fetchone())
        with self.archive.connect() as db:
            db.execute('DELETE FROM rating_capture_paths WHERE session=?',(sid,))
            for batch in self.store.iter_records(sid):
                members=[];seeds=[]
                for record in batch:
                    cid=record['id'];keys={pair_key(p) for p in self.paths(sid,record,session['root'])}
                    members.extend((sid,cid,key) for key in keys)
                    value=overrides.get(cid,record.get('existingRating',0))
                    if cid in overrides or value>0:seeds.extend((key,value,0) for key in keys)
                db.executemany('INSERT OR IGNORE INTO rating_capture_paths VALUES (?,?,?)',members)
                db.executemany('INSERT INTO shared_ratings VALUES (?,?,?) ON CONFLICT(path_key) DO UPDATE SET rating=excluded.rating WHERE shared_ratings.updated=0',seeds)
            if shooting:return
            # 第二遍仍只保留一批，避免后续批次的同资产路径尚未登记而漏掉共享评级。
            for batch in self.store.iter_records(sid):
                records={r['id']:r for r in batch};paths={r['id']:self.paths(sid,r,session['root']) for r in batch}
                keys={cid:{pair_key(p) for p in values} for cid,values in paths.items()}
                all_keys=list({k for values in keys.values() for k in values})
                ratings={r['path_key']:r for r in db.execute('SELECT * FROM shared_ratings WHERE path_key IN ('+','.join('?' for _ in all_keys)+')',all_keys)} if all_keys else {}
                linked={r[0] for r in db.execute('SELECT capture FROM catalog_links WHERE session=? AND capture IN ('+','.join('?' for _ in records)+')',(sid,*records))}
                for cid in records:
                    entries=[ratings[k] for k in keys[cid] if k in ratings]
                    value=max(entries,key=lambda r:r['updated'])['rating'] if entries else None
                    if cid in linked:value=self.get(paths[cid],db)
                    if value is not None:self.sync.enqueue(db,paths[cid],value,time.time_ns(),initial=True)

    def session(self, session, records):
        if not session:
            return session
        shared = {}
        with self.archive.connect() as c:
            for record in records:
                value = self.get(self.paths(session['id'], record,session['root']),c)
                if value is not None:
                    shared[record['id']] = value
        return {**session, 'sharedRatings': shared}

    def saved(self, sid, before, after, baseline=None):
        old = baseline if isinstance(baseline,dict) else {**before['state'].get('overrides', {}), **before.get('sharedRatings', {})}
        new = after.get('overrides', {})
        changed=[cid for cid in old.keys() | new.keys() if old.get(cid)!=new.get(cid)]
        for start in range(0,len(changed),256):
            batch=changed[start:start+256]
            records = {r['id']: r for r in self.store.records(sid,ids=batch)}
            for cid in batch:
                record = records.get(cid)
                if record:self.set(self.paths(sid, record), new.get(cid, record.get('liveRating', record.get('existingRating', 0))))

    def decorate(self, rows):
        """为归档分页一次性补齐共享评级，避免每张素材触发多轮关联查询。"""
        if not rows:return rows
        paths=[str(Path(row['path']).absolute()) for row in rows]
        direct={path:pair_key(path) for path in paths}
        keys=set(direct.values());assets_by_key={key:set() for key in keys}
        def batches(values,size=400):
            values=list(values)
            for start in range(0,len(values),size):yield values[start:start+size]
        with self.archive.connect() as c:
            # 目录中已有的最终归档副本通过精确路径关联资产。
            for batch in batches(paths):
                marks=','.join('?' for _ in batch)
                for row in c.execute('SELECT l.path,f.asset FROM locations l JOIN files f ON f.id=l.file WHERE l.path COLLATE NOCASE IN ('+marks+')',batch):
                    key=direct.get(str(Path(row['path']).absolute()))
                    if key is not None:assets_by_key[key].add(row['asset'])
            # 现场拍摄目录使用 RAW/JPG 的配对键关联已有资产。
            for batch in batches(keys):
                marks=','.join('?' for _ in batch)
                for row in c.execute('SELECT p.path_key,k.asset FROM rating_capture_paths p JOIN catalog_links k ON k.session=p.session AND k.capture=p.capture WHERE p.path_key IN ('+marks+')',batch):
                    assets_by_key[row['path_key']].add(row['asset'])
            asset_keys={asset:set() for values in assets_by_key.values() for asset in values}
            for batch in batches(asset_keys):
                marks=','.join('?' for _ in batch)
                for row in c.execute('SELECT f.asset,l.path FROM locations l JOIN files f ON f.id=l.file WHERE f.asset IN ('+marks+')',batch):
                    asset_keys[row['asset']].add(pair_key(row['path']))
                for row in c.execute('SELECT k.asset,p.path_key FROM catalog_links k JOIN rating_capture_paths p ON p.session=k.session AND p.capture=k.capture WHERE k.asset IN ('+marks+')',batch):
                    asset_keys[row['asset']].add(row['path_key'])
            related=keys|{key for values in asset_keys.values() for key in values}
            ratings={}
            for batch in batches(related):
                marks=','.join('?' for _ in batch)
                ratings.update({row['path_key']:(row['rating'],row['updated']) for row in c.execute('SELECT path_key,rating,updated FROM shared_ratings WHERE path_key IN ('+marks+')',batch)})
        for row,path in zip(rows,paths):
            key=direct[path];candidates=[(ratings[entry],entry==key) for entry in {key}|set().union(*(asset_keys.get(asset,set()) for asset in assets_by_key[key])) if entry in ratings]
            row['rating']=max(candidates,key=lambda entry:(entry[0][1],entry[1]))[0][0] if candidates else 0
        return rows
