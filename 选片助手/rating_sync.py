"""持久星级同步队列：数据库为可靠副本，失败保留并自动重试。"""
from pathlib import Path
import threading
import time


class RatingSync:
    def __init__(self, archive):
        self.archive = archive
        self.wake = threading.Event()
        with archive.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS rating_file_sync(
                path TEXT PRIMARY KEY,rating INTEGER NOT NULL,revision INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',reason TEXT NOT NULL DEFAULT '',
                retry_at REAL NOT NULL DEFAULT 0)''')
            c.execute("UPDATE rating_file_sync SET status='queued' WHERE status='running'")
        self.thread = threading.Thread(target=self.run, daemon=True, name='rating-xmp-sync')
        if archive.autostart:self.thread.start()

    def enqueue(self, c, paths, rating, revision, *, initial=False):
        for path in dict.fromkeys(str(Path(p).absolute()) for p in paths):
            c.execute('''INSERT INTO rating_file_sync(path,rating,revision) VALUES (?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                rating=excluded.rating,revision=excluded.revision,status='queued',reason='',retry_at=0''' +
                (' WHERE rating_file_sync.rating!=excluded.rating' if initial else ''),
                (path, max(0, rating), revision))
        self.wake.set()

    def state(self):
        with self.archive.connect() as c:
            counts = {r[0]:r[1] for r in c.execute('SELECT status,count(*) FROM rating_file_sync GROUP BY status')}
            issues = [dict(r) for r in c.execute("SELECT path,reason FROM rating_file_sync WHERE status='failed' LIMIT 20")]
        return {'pending':counts.get('queued',0)+counts.get('running',0),
                'failed':counts.get('failed',0), 'applied':counts.get('applied',0), 'issues':issues}

    def retry(self):
        with self.archive.connect() as c:
            c.execute("UPDATE rating_file_sync SET status='queued',retry_at=0 WHERE status='failed'")
        self.wake.set()
        return self.state()

    def process_one(self):
        # 与 SKU 写入共用归档锁及 writer 锁，避免两条写入链互相覆盖。
        a=self.archive
        with a.lock:
            with a.connect() as c:
                row=c.execute("SELECT * FROM rating_file_sync WHERE status='queued' OR (status='failed' AND retry_at<=?) ORDER BY revision LIMIT 1",(time.time(),)).fetchone()
                if not row:return False
                job=dict(row)
                c.execute("UPDATE rating_file_sync SET status='running' WHERE path=? AND revision=?",(job['path'],job['revision']))
            try:
                a.writer.write_rating(job['path'],job['rating'])
                # JPG 内嵌更新后的校验值登记为同一文件版本，维护归档匹配及待写入任务。
                from metadata_writer import sha
                path=job['path'];digest=sha(path)
                with a.connect() as c:
                    files=c.execute('SELECT file FROM locations WHERE path=? COLLATE NOCASE',(path,)).fetchall()
                    for f in files:
                        c.execute('UPDATE files SET sha=? WHERE id=?',(digest,f[0]))
                        c.execute('INSERT OR IGNORE INTO versions VALUES (?,?,?)',(f[0],digest,time.time()))
                    c.execute('UPDATE file_fingerprints SET sha=? WHERE path=? COLLATE NOCASE',(digest,path))
                    c.execute("UPDATE metadata_jobs SET expected_sha=? WHERE path=? COLLATE NOCASE AND status='queued'",(digest,path))
                    c.execute("UPDATE rating_file_sync SET status='applied',reason='' WHERE path=? AND revision=?",(path,job['revision']))
                self.refresh_captures(path)
            except Exception as exc:
                with a.connect() as c:
                    c.execute("UPDATE rating_file_sync SET status='failed',reason=?,retry_at=? WHERE path=? AND revision=?",(str(exc)[:350],time.time()+30,job['path'],job['revision']))
        return True

    def refresh_captures(self,path):
        """评级只改元数据；延续 OCR 指纹身份，避免监控将其当成新照片。"""
        import json
        import media_engine as media
        from asset_catalog import pair_key
        a=self.archive
        with a.connect() as c:
            links=c.execute('SELECT session,capture FROM rating_capture_paths WHERE path_key=?',(pair_key(path),)).fetchall()
        for link in links:
            session=a.store.session(link['session'])
            if not session:continue
            with a.store.connect() as c:
                row=c.execute('SELECT data FROM captures WHERE session=? AND id=?',(link['session'],link['capture'])).fetchone()
            if not row:continue
            record=json.loads(row[0])
            try:
                paths=[media.resolved_file(session['root'],record[k]) for k in ('raw','jpg') if record.get(k)]
                for p in paths:media._STATS[str(p)]=p.stat()
                old=record['fingerprint'];record['fingerprint']=media.fingerprint(paths)
                if a.sku:a.sku.metadata_changed(link['session'],link['capture'],old,record['fingerprint'])
                with a.store.connect() as c:
                    c.execute('UPDATE captures SET data=? WHERE session=? AND id=? AND data=?',(json.dumps(record,ensure_ascii=False),link['session'],link['capture'],row[0]))
            except (OSError,ValueError):continue

    def run(self):
        while not self.archive.closed.is_set():
            try:
                if self.process_one():continue
            except Exception:
                pass  # 单个异常不终止持久队列；下一轮继续，任务状态仍可查阅。
            self.wake.wait(1);self.wake.clear()

    def close(self):
        self.wake.set()
        if self.thread.is_alive():self.thread.join(timeout=5)
