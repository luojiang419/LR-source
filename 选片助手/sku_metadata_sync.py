"""换机导入后将备份身份关联到本机归档，沿用持久写入队列和冲突保护。"""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import threading
import time

import media_engine as media
from raw_reconcile import compare
from metadata_writer import sha
from asset_catalog import pair_key
from sku_match import normalize


def sources_from_backup(payload):
    db = payload['databases'];archive = db['archive']
    sessions = {r['id']:r for r in db['catalog']['sessions']}
    assets = {r['id']:r for r in archive['assets']}
    links = {(r['session'],r['capture']):r['asset'] for r in archive['catalog_links']}
    by_asset = defaultdict(list)
    for key, aid in links.items():by_asset[aid].append(key)
    files = {r['id']:r['asset'] for r in archive.get('files',[])}
    hashes = defaultdict(set)
    for row in archive.get('versions',[]):
        if row['file'] in files:hashes[files[row['file']]].add(row['sha'])
    for row in archive.get('files',[]):hashes[row['asset']].add(row['sha'])
    labels = {r['id']:r['label'] for r in archive.get('segments',[])}
    documents = defaultdict(list)
    for row in db['sku']['ocr_documents']:documents[(row['session_id'],row['capture_id'])].append(row)
    ratings = {r['path_key']:r for r in archive.get('shared_ratings',[])}
    sources = []
    for row in db['catalog']['captures']:
        sid,cid = row['session'],row['id'];aid = links.get((sid,cid));asset = assets.get(aid)
        if not asset:continue
        session = sessions[sid];record = json.loads(row['data']);state = json.loads(session['state'])
        rating = state.get('overrides',{}).get(cid,record.get('existingRating',0))
        shared = [ratings[pair_key(str(Path(session['root'])/record[k]))] for k in ('jpg','raw') if record.get(k) and pair_key(str(Path(session['root'])/record[k])) in ratings]
        if shared:rating = max(shared,key=lambda r:r['updated'])['rating']
        docs = list(documents[(sid,cid)])
        for label_key in by_asset.get(labels.get(asset.get('segment')),[]):docs.extend(documents[label_key])
        sources.append({'session':sid,'capture':cid,'record':json.dumps(record,ensure_ascii=False),
                        'asset':aid,'rating':max(0,rating),'excluded':rating<0,'hashes':sorted(hashes[aid]),'documents':docs,
                        'sku':asset['sku'],'revision':asset['revision']})
    return sources


class MetadataArchiveSync:
    def __init__(self,sku,catalog,rawlink,*,autostart=True):
        self.sku=sku;self.archive=sku.archive;self.store=sku.store;self.catalog=catalog;self.rawlink=rawlink
        self.lock=threading.RLock();self.closed=threading.Event()
        self.cached_id=None;self.cached_sources=[];self.cached_targets=[]
        with sku.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS metadata_archive_syncs(
                id TEXT PRIMARY KEY,sources TEXT,targets TEXT DEFAULT '[]',status TEXT,report TEXT DEFAULT '{}',created REAL,updated REAL);
            CREATE TABLE IF NOT EXISTS metadata_archive_results(
                sync TEXT,session TEXT,capture TEXT,path TEXT,status TEXT,reason TEXT DEFAULT '',operation TEXT DEFAULT '',
                asset TEXT DEFAULT '',source_session TEXT DEFAULT '',source_capture TEXT DEFAULT '',
                PRIMARY KEY(sync,session,capture));''')
            if 'fingerprint' not in {r['name'] for r in c.execute('PRAGMA table_info(metadata_archive_results)')}:
                c.execute("ALTER TABLE metadata_archive_results ADD COLUMN fingerprint TEXT DEFAULT ''")
            c.execute("UPDATE metadata_archive_syncs SET status='queued' WHERE status='running'")
        self.thread=threading.Thread(target=self._run,name='sku-metadata-migration',daemon=True)
        if autostart:self.thread.start()

    def start(self,stamp,payload):
        sources=sources_from_backup(payload)
        with self.sku.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT status FROM metadata_archive_syncs WHERE id=?',(stamp,)).fetchone()
            if not prior or prior['status'] not in ('queued','running'):
                c.execute("DELETE FROM metadata_archive_results WHERE sync=? AND status!='applied'",(stamp,))
                c.execute('''INSERT INTO metadata_archive_syncs(id,sources,status,created,updated) VALUES (?,?,'queued',?,?)
                    ON CONFLICT(id) DO UPDATE SET sources=excluded.sources,status='queued',updated=excluded.updated''',
                    (stamp,json.dumps(sources,ensure_ascii=False),time.time(),time.time()))
        return self.state(stamp)

    def state(self,stamp=None):
        with self.sku.connect() as c:
            row=c.execute('SELECT id,status,report,updated FROM metadata_archive_syncs '+('WHERE id=?' if stamp else 'ORDER BY updated DESC LIMIT 1'),(stamp,) if stamp else ()).fetchone()
        if not row:return None
        return {**dict(row),'report':json.loads(row['report'])}

    def _targets(self):
        targets=[]
        for root in self.catalog.state()['roots']:
            base=Path(root['path'])
            if not base.is_dir():continue
            with self.catalog.connect() as c:excluded=[r[0] for r in c.execute('SELECT folder FROM exclusions WHERE root=?',(root['id'],))]
            _,shots=media.discover(base,cancel=self.closed)
            shots=[(p,j) for p,j in shots if not any(p.parent.relative_to(base).as_posix()==f or p.parent.relative_to(base).as_posix().startswith(f+'/') for f in excluded)]
            sid=self.store.ensure_live_session(base)
            with self.store.connect() as c:
                c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('archive_session:'+sid,str(base)))
                c.execute('DELETE FROM settings WHERE key=?',('archive_disabled:'+sid,))
            records=[]
            for start in range(0,len(shots),24):
                if self.closed.is_set():return targets
                batch=shots[start:start+24];metadata=media.read_metadata([p for p,_ in batch],media.find_exiftool(self.store.root),cancel=self.closed)
                records.extend(media.capture_record(base,p,j,m) for (p,j),m in zip(batch,metadata))
            self.rawlink.add_target(str(base))
            self.store.append_live(sid,records)
            targets.extend({'session':sid,'capture':r['id'],'record':r,'path':str(base/(r.get('raw') or r['jpg']))} for r in records)
        targets=list({t['path'].casefold():t for t in targets}.values())
        for target in targets:target['fingerprint']=self._fingerprint(target)
        return targets

    def _fingerprint(self,target):
        base=Path(self.store.session(target['session'])['root'])
        paths=[base/target['record'][k] for k in ('raw','jpg') if target['record'].get(k)]
        return hashlib.sha256(json.dumps([(str(p),p.stat().st_size,p.stat().st_mtime_ns) for p in paths]).encode()).hexdigest()

    def _select(self,target,sources):
        record=target['record'];path=Path(target['path']);metadata=self.archive.writer.read_asset(path)
        embedded=str(metadata.get('XMP-fk:AssetId') or '').replace('-','')
        digest=sha(path)
        if embedded:
            candidates=[s for s in sources if s['asset']==embedded]
            if not candidates:return None,'照片包含其他资产身份，未覆盖',False
        else:
            candidates=[s for s in sources if digest in s['hashes']]
            if not candidates:
                candidates=[s for s in sources if (compare(record,json.loads(s['record'])) or {}).get('certain')]
        groups=defaultdict(list)
        for source in candidates:groups[source['asset']].append(source)
        if len(groups)!=1:return None,'多个可靠来源，需复核' if groups else '未找到可靠来源，未写入',False
        rows=next(iter(groups.values()))
        if len({s['rating'] for s in rows})>1:return None,'同一资产的来源星级不一致，需复核',False
        chosen=max(rows,key=lambda s:(bool(s['documents']),s['revision']))
        session=self.store.session(target['session'])
        manual=session['state'].get('overrides',{}).get(target['capture'])
        with self.archive.connect() as c:local=c.execute('SELECT rating,updated FROM shared_ratings WHERE path_key=?',(pair_key(str(path)),)).fetchone()
        if (manual is not None and max(0,manual)!=chosen['rating']) or (local and local['updated']>0 and max(0,local['rating'])!=chosen['rating']):
            return None,'归档已有不同人工星级，需复核',False
        with self.catalog.connect() as c:
            corrected=c.execute("SELECT normalized FROM items WHERE path=? AND source='manual'",(str(path),)).fetchone()
        if corrected and corrected['normalized']!=normalize(chosen['sku']):return None,'归档已有不同人工 SKU，需复核',False
        version=int(metadata.get('XMP-fk:MetadataRevision') or 0)
        overwrite=bool(embedded==chosen['asset'] and version<=chosen['revision'])
        return chosen,'',overwrite

    def _ocr(self,stamp,target,source):
        sid,cid=target['session'],target['capture']
        paths=[str(Path(self.store.session(sid)['root'])/target['record'][k]) for k in ('jpg','raw') if target['record'].get(k)]
        self.archive.ratings.set(paths,-1 if source.get('excluded') else source['rating'])
        with self.store.connect() as c:
            row=c.execute('SELECT data FROM captures WHERE session=? AND id=?',(sid,cid)).fetchone()
            record=json.loads(row['data']);record.update(existingRating=source['rating'],liveRating=source['rating'])
            c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(json.dumps(record,ensure_ascii=False),sid,cid))
            if source.get('excluded'):
                session=c.execute('SELECT state FROM sessions WHERE id=?',(sid,)).fetchone();state=json.loads(session['state'])
                state.setdefault('overrides',{})[cid]=-1
                c.execute('UPDATE sessions SET state=?,revision=revision+1 WHERE id=?',(json.dumps(state,ensure_ascii=False),sid))
        with self.sku.lock,self.sku.connect() as c:
            for doc in source['documents']:
                key='migration:'+hashlib.sha256((stamp+sid+cid+doc['id']).encode()).hexdigest()
                c.execute('INSERT OR IGNORE INTO ocr_documents(id,session_id,capture_id,text,raw,searchable,updated) VALUES (?,?,?,?,?,?,?)',
                          (key,sid,cid,doc['text'],doc['raw'],doc['searchable'],doc['updated']))
                rowid=c.execute('SELECT rowid FROM ocr_documents WHERE id=?',(key,)).fetchone()[0]
                c.execute('DELETE FROM ocr_document_search WHERE rowid=?',(rowid,))
                c.execute('INSERT INTO ocr_document_search(rowid,searchable) VALUES (?,?)',(rowid,doc['searchable']))
            if source['sku']:
                c.execute('INSERT OR IGNORE INTO ocr_choices VALUES (?,?,?,?,?)',(sid,cid,source['sku'],'migration:'+stamp,time.time()))
        with self.catalog.connect() as c:
            for path in paths:
                c.execute("UPDATE items SET sku=?,normalized=?,source='embedded',confidence=1 WHERE path=? AND source!='manual'",(source['sku'],normalize(source['sku']),path))
        return self._fingerprint(target)

    def _step(self,stamp,target,sources):
        sid,cid=target['session'],target['capture']
        source,reason,overwrite=self._select(target,sources)
        status='review' if '复核' in reason or '其他资产' in reason else 'unmatched'
        operation='';aid='';ss='';sc='';fingerprint=''
        if source:
            aid=source['asset'];ss=source['session'];sc=source['capture']
            self.rawlink.match_one(sid,target['record'],sources,chosen=(ss,sc),overwrite=overwrite)
            with self.archive.connect() as c:
                pair=c.execute('SELECT * FROM raw_pairs WHERE session=? AND capture=?',(sid,cid)).fetchone()
            status=pair['status'];reason=pair['reason'];operation=pair['operation']
            if status=='failed' and operation:
                self.archive.retry(operation);status='queued';reason='正在重试本机归档写入'
            if status not in ('queued','applied','review','unmatched','failed'):status='review'
            if status=='applied':fingerprint=self._ocr(stamp,target,source)
        with self.sku.connect() as c:
            c.execute('INSERT OR REPLACE INTO metadata_archive_results VALUES (?,?,?,?,?,?,?,?,?,?,?)',(stamp,sid,cid,target['path'],status,reason,operation,aid,ss,sc,fingerprint))

    def tick(self):
        with self.lock:
            with self.sku.connect() as c:job=c.execute("SELECT * FROM metadata_archive_syncs WHERE status IN ('queued','running') ORDER BY created LIMIT 1").fetchone()
            if not job:return
            stamp=job['id'];cached=self.cached_id==stamp and job['status']!='queued'
            sources=self.cached_sources if cached else json.loads(job['sources'])
            if job['status']=='queued':
                # 仅使用备份来源，不对离线原图重新读取／识别。
                with self.archive.connect() as c:
                    for source in sources:c.execute('INSERT OR REPLACE INTO raw_sources VALUES (?,?,?,?,?)',(source['session'],source['capture'],source['record'],source['rating'],time.time()))
                targets=self._targets()
                with self.sku.connect() as c:
                    for target in targets:
                        c.execute("DELETE FROM metadata_archive_results WHERE sync=? AND session=? AND capture=? AND status='applied' AND fingerprint!=?",(stamp,target['session'],target['capture'],target['fingerprint']))
                    c.execute("UPDATE metadata_archive_syncs SET targets=?,status='running',updated=? WHERE id=?",(json.dumps(targets,ensure_ascii=False),time.time(),stamp))
            else:targets=self.cached_targets if cached else json.loads(job['targets'])
            self.cached_id=stamp;self.cached_sources=sources;self.cached_targets=targets
            if self.closed.is_set():return
            with self.sku.connect() as c:results={(r['session'],r['capture']):dict(r) for r in c.execute('SELECT * FROM metadata_archive_results WHERE sync=?',(stamp,))}
            target=next((t for t in targets if (t['session'],t['capture']) not in results),None)
            if target:
                try:self._step(stamp,target,sources)
                except Exception as exc:
                    with self.sku.connect() as c:c.execute('INSERT OR REPLACE INTO metadata_archive_results(sync,session,capture,path,status,reason) VALUES (?,?,?,? ,\'review\',?)',(stamp,target['session'],target['capture'],target['path'],str(exc)[:300]))
            else:
                pending=[r for r in results.values() if r['status']=='queued']
                for result in pending:
                    with self.archive.connect() as c:
                        jobs=[dict(r) for r in c.execute('SELECT status,reason FROM metadata_jobs WHERE operation=?',(result['operation'],))]
                    if not jobs or any(r['status'] in ('queued','running') for r in jobs):continue
                    status='applied' if all(r['status']=='applied' for r in jobs) else 'failed'
                    t=next(t for t in targets if t['session']==result['session'] and t['capture']==result['capture'])
                    source=next(s for s in sources if s['session']==result['source_session'] and s['capture']==result['source_capture'])
                    fingerprint=self._ocr(stamp,t,source) if status=='applied' else ''
                    with self.sku.connect() as c:c.execute('UPDATE metadata_archive_results SET status=?,reason=?,fingerprint=? WHERE sync=? AND session=? AND capture=?',(status,'；'.join(j['reason'] for j in jobs if j['status']!='applied'),fingerprint,stamp,result['session'],result['capture']))
            with self.sku.connect() as c:rows=[dict(r) for r in c.execute('SELECT * FROM metadata_archive_results WHERE sync=?',(stamp,))]
            current={(t['session'],t['capture']) for t in targets};rows=[r for r in rows if (r['session'],r['capture']) in current]
            counts={key:sum(r['status']==key for r in rows) for key in ('applied','queued','review','unmatched','failed')}
            matched={r['asset'] for r in rows if r['status']=='applied'}
            missing=len({s['asset'] for s in sources}-matched)
            pending=len(rows)<len(targets) or counts['queued']>0
            status='running' if pending else 'waiting_archive' if not targets else 'attention' if missing or any(counts[k] for k in ('review','unmatched','failed')) else 'completed'
            report={'total':len(targets),**counts,'missing_assets':missing,'issues':[{'path':r['path'],'reason':r['reason'],'status':r['status']} for r in rows if r['status'] in ('review','unmatched','failed')][:100]}
            with self.sku.connect() as c:c.execute('UPDATE metadata_archive_syncs SET status=?,report=?,updated=? WHERE id=?',(status,json.dumps(report,ensure_ascii=False),time.time(),stamp))

    def _run(self):
        while not self.closed.is_set():
            try:self.tick()
            except Exception as exc:
                if self.closed.is_set():break
                with self.sku.connect() as c:c.execute("UPDATE metadata_archive_syncs SET status='attention',report=?,updated=? WHERE status IN ('queued','running')",(json.dumps({'error':str(exc)[:300]},ensure_ascii=False),time.time()))
            self.closed.wait(.2)

    def close(self):
        self.closed.set()
        if self.thread.is_alive():self.thread.join(timeout=3)


def attach(sku,catalog,rawlink,*,autostart=True):
    if not catalog or not rawlink:return None
    with sku.lock:
        if not getattr(sku,'metadata_sync',None):sku.metadata_sync=MetadataArchiveSync(sku,catalog,rawlink,autostart=autostart)
    return sku.metadata_sync


def prefer_local_archives(sku,rows):
    """已重新关联的离线历史副本不重复计入同一资产；在线副本仍分别保留。"""
    with sku.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_archive_results'").fetchone():return rows
        restored=[dict(r) for r in c.execute("SELECT asset,path FROM metadata_archive_results WHERE status='applied' AND asset!=''")]
    online={r['asset'] for r in restored if Path(r['path']).is_file()}
    if not online:return rows
    with sku.archive.connect() as c:
        links={(r['session'],r['capture']):r['asset'] for r in c.execute('SELECT * FROM catalog_links') if r['asset'] in online}
    return [row for row in rows if links.get((row['session_id'],row['capture_id'])) not in online or Path(row['path']).is_file()]
