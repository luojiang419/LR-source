"""按确切路径、配对文件及已验证永久身份共享人工评级。"""
from pathlib import Path
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
                CREATE INDEX IF NOT EXISTS rating_capture_path ON rating_capture_paths(path_key);
                CREATE INDEX IF NOT EXISTS rating_location_path ON locations(path COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS rating_locations_file ON locations(file,path);
                CREATE INDEX IF NOT EXISTS rating_files_asset ON files(asset,id);
                CREATE INDEX IF NOT EXISTS rating_links_asset ON catalog_links(asset,session);
            ''')

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
        with self.archive.connect() as c:
            keys = self.related_keys(paths,c)
            stamp = time.time_ns()
            for key in keys:
                c.execute('INSERT INTO shared_ratings VALUES (?,?,?) ON CONFLICT(path_key) DO UPDATE SET rating=excluded.rating,updated=excluded.updated', (key, rating, stamp))
        return rating

    def paths(self, sid, record, root=None):
        root = root or self.store.session(sid)['root']
        return [str(Path(root) / record[kind]) for kind in ('raw', 'jpg') if record.get(kind)]

    def seed_session(self, sid):
        session = self.store.session(sid)
        if not session:
            return
        version=(session['snapshot'],session['revision'])
        if self.seed_versions.get(sid)==version:return
        overrides = session['state'].get('overrides', {})
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
        self.seed_versions[sid]=version

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
        records = {r['id']: r for r in self.store.records(sid)}
        for cid in old.keys() | new.keys():
            if old.get(cid) == new.get(cid):
                continue
            record = records.get(cid)
            if record:
                self.set(self.paths(sid, record), new.get(cid, record.get('liveRating', record.get('existingRating', 0))))

    def decorate(self, rows):
        with self.archive.connect() as c:
            for row in rows:
                row['rating'] = max(0, self.get([row['path']],c) or 0)
        return rows
