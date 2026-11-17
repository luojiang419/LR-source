"""选片即时反馈：只读当前目录评级和对应标签任务，不构造整份清单或区间。"""
import json


def feedback(sku,sid=None):
    session=sku.store.session(sid)
    empty={'session_id':sid,'labels':[],'total':0,'confirmed':0,'pending':0,'review':0,'failed':0}
    if not session:return empty
    sid=session['id'];overrides=session['state'].get('overrides',{})
    with sku.store.connect() as c:
        ids={r[0] for r in c.execute("SELECT id FROM captures WHERE session=? AND json_extract(data,'$.existingRating')=2",(sid,))}
    for cid,value in overrides.items():
        if value==2:ids.add(cid)
        else:ids.discard(cid)
    if sku.archive:
        ratings=sku.archive.ratings
        with ratings.archive.connect() as c:
            shared=c.execute('SELECT p.capture,r.rating FROM rating_capture_paths p JOIN shared_ratings r ON r.path_key=p.path_key WHERE p.session=? ORDER BY r.updated',(sid,)).fetchall()
        for cid,value in shared:
            if value==2:ids.add(cid)
            else:ids.discard(cid)
    # 最后一个有效代次优先；已取消旧代次不能覆盖重标后的新请求。
    with sku.connect() as c:
        jobs=c.execute("SELECT capture_id,status,updated,created,targets,trigger,json_extract(result,'$.requires_sku_choice') AS requires_choice FROM jobs WHERE session_id=? AND status NOT IN ('cancelled','ignored') ORDER BY updated DESC,created DESC,rowid DESC",(sid,)).fetchall()
    if sku.archive:
        candidates=ids|{r['capture_id'] for r in jobs if r['trigger']!='full_auto'}
        records=sku.store.records(sid,ids=candidates)
        shared=sku.archive.ratings.session(session,records)['sharedRatings']
        for record in records:
            cid=record['id'];value=shared.get(cid,overrides.get(cid,record.get('existingRating',0)))
            if value==2:ids.add(cid)
            else:ids.discard(cid)
    if not ids:return {**empty,'session_id':sid}
    latest={}
    for job in jobs:
        status='confirmed' if job['status']=='matched' and not job['requires_choice'] and json.loads(job['targets'] or '[]') else job['status']
        latest.setdefault(job['capture_id'],status)
    labels=[{'capture_id':cid,'status':latest.get(cid,'queued')} for cid in sorted(ids)]
    confirmed=sum(r['status']=='confirmed' for r in labels)
    review=sum(r['status'] in ('review','unmatched','matched') for r in labels)
    failed=sum(r['status']=='failed' for r in labels)
    return {'session_id':sid,'labels':labels,'total':len(labels),'confirmed':confirmed,
            'review':review,'failed':failed,'pending':len(labels)-confirmed-review-failed}
