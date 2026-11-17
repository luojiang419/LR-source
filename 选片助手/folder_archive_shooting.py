"""用已核验的归档移动日志补齐旧版漏记的拍摄指纹版本链。"""
import hashlib
import json
from pathlib import Path


def _fingerprint(entries,field):
    return hashlib.sha256(json.dumps([(e[field],*e['signature']) for e in entries],ensure_ascii=False).encode()).hexdigest()


def recover_versions(sku,c):
    if getattr(sku,'_archive_move_versions_checked',False):return 0
    directory=sku.store.data/'folder-archive-jobs'
    if not directory.is_dir():return 0
    versions={(r['session'],r['capture'],r['before']):r['after'] for r in c.execute('SELECT * FROM metadata_versions')}
    pending={};sessions={}
    for job in c.execute("SELECT session_id,capture_id,fingerprint FROM jobs WHERE status IN ('matched','confirmed')"):
        sid,cid=job['session_id'],job['capture_id']
        if sid not in sessions:
            session=sku.store.session(sid)
            sessions[sid]=(Path(session['root']),{r['id']:r for r in sku.store.records(sid)}) if session else (None,{})
        root,records=sessions[sid];record=records.get(cid)
        if not record:continue
        fingerprint=job['fingerprint'];seen=set()
        while fingerprint!=record['fingerprint'] and fingerprint not in seen:
            seen.add(fingerprint);after=versions.get((sid,cid,fingerprint))
            if not after:break
            fingerprint=after
        if fingerprint!=record['fingerprint']:pending[(sid,cid,fingerprint)]=(root,record)
    if not pending:
        sku._archive_move_versions_checked=True
        return 0
    repaired=0
    for path in directory.glob('*/plan.json'):
        marker=path.parent/'paths-synchronized.json'
        if not marker.is_file():continue
        try:
            checkpoint=json.loads(marker.read_text(encoding='utf8'))
            if not checkpoint.get('done'):continue
            plan=json.loads(path.read_text(encoding='utf8'));journal=plan.get('preprocess_journal')
            if not journal or checkpoint.get('journal')!=journal:continue
            if json.loads(Path(journal).read_text(encoding='utf8')).get('state')!='completed':continue
            moves={r['after']:r for r in plan.get('preprocess_moves',[]) if r.get('signature')}
        except (OSError,ValueError,KeyError,TypeError):continue
        for identity,(root,record) in list(pending.items()):
            paths=[root/record[k] for k in ('raw','jpg') if record.get(k)]
            entries=[moves.get(str(p)) for p in paths]
            if not entries or not all(entries):continue
            before,after=_fingerprint(entries,'before'),_fingerprint(entries,'after')
            if before!=identity[2] or after!=record['fingerprint']:continue
            # 日志和当前会话必须同时匹配磁盘版本；改过内容的照片不补记。
            try:
                if any([s.st_size,s.st_mtime_ns]!=e['signature'] for p,e in zip(paths,entries) for s in [p.stat()]):continue
            except OSError:continue
            c.execute('INSERT OR IGNORE INTO metadata_versions VALUES (?,?,?,?)',(*identity,after))
            del pending[identity];repaired+=1
    # 只缓存未恢复的检查；已补版本若外层事务回滚，下次仍会重试。
    if not repaired:sku._archive_move_versions_checked=True
    return repaired
