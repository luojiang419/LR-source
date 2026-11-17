"""为已确认资产补齐旧版漏建的文件任务；只读事实库，不探测或修改原片。"""
import json
import time
import uuid


def enqueue_missing(archive,limit=128,prefix=''):
    removed=set()
    if archive.sku:
        with archive.sku.connect() as c:
            removed={r[0] for r in c.execute("SELECT id FROM projects WHERE json_extract(metadata,'$.removed_at') IS NOT NULL")}
    count=0
    excluded=' AND a.project NOT IN ('+','.join('?' for _ in removed)+')' if removed else ''
    scope=[]
    if prefix:
        # 只补用户在资产归档选择的范围；LIKE 通配符按字面匹配。
        escaped=prefix.rstrip('\\/').replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        scope=[escaped+'\\\\%']
        excluded+=" AND l.path LIKE ? ESCAPE '\\'"
    with archive.connect() as c:
        c.set_progress_handler(lambda:int(archive.closed.is_set()),1000)
        # 历史候选扫描先只读；不能拿着写事务枚举全库阻挡文件消费者领取任务。
        archive.backfill_unverified=[dict(r) for r in c.execute('''SELECT a.id,a.sku,a.project FROM assets a
 JOIN assignments x ON x.asset=a.id AND x.revision=a.revision
 WHERE a.sku<>'' AND a.source IN ('manual','segment') AND NOT json_valid(x.data) LIMIT 20''')]
        rows=c.execute('''SELECT a.*,x.data AS assignment,f.id AS file_id,f.sha AS file_sha,l.path,
 p.sha AS path_sha FROM assets a JOIN (SELECT asset,revision,CASE WHEN json_valid(data) THEN data ELSE '{}' END AS data FROM assignments) x ON x.asset=a.id AND x.revision=a.revision
 JOIN files f ON f.asset=a.id JOIN locations l ON l.file=f.id
 LEFT JOIN file_fingerprints p ON p.path=l.path
 WHERE a.sku<>'' AND a.source IN ('manual','segment') AND json_extract(x.data,'$.operation') IS NOT NULL
 AND json_extract(x.data,'$.after.sku')=a.sku
 AND coalesce(json_extract(x.data,'$.after.scope'),'')=coalesce(a.scope,'')
 AND coalesce(json_extract(x.data,'$.after.project'),'')=coalesce(a.project,'')
 AND coalesce(json_extract(x.data,'$.after.segment'),'')=coalesce(a.segment,'')
 AND json_extract(x.data,'$.after.source')=a.source'''+excluded+''' AND NOT EXISTS (
 SELECT 1 FROM metadata_jobs j WHERE j.asset=a.id AND j.revision=a.revision AND j.file=f.id AND j.path=l.path
 AND coalesce(json_extract(j.fields,'$._descriptive'),0)=0) ORDER BY a.created,a.id,f.id LIMIT ?''',(*removed,*scope,limit)).fetchall()
        if not rows:return 0
        c.execute('BEGIN IMMEDIATE')
        for candidate in rows:
            row=c.execute('''SELECT a.*,x.data AS assignment,f.id AS file_id,f.sha AS file_sha,l.path,p.sha AS path_sha
 FROM assets a JOIN (SELECT asset,revision,CASE WHEN json_valid(data) THEN data ELSE '{}' END AS data FROM assignments) x ON x.asset=a.id AND x.revision=a.revision
 JOIN files f ON f.asset=a.id JOIN locations l ON l.file=f.id LEFT JOIN file_fingerprints p ON p.path=l.path
 WHERE a.id=? AND a.revision=? AND f.id=? AND l.path=? AND NOT EXISTS (
 SELECT 1 FROM metadata_jobs j WHERE j.asset=a.id AND j.revision=a.revision AND j.file=f.id AND j.path=l.path
 AND coalesce(json_extract(j.fields,'$._descriptive'),0)=0)''',
 (candidate['id'],candidate['revision'],candidate['file_id'],candidate['path'])).fetchone()
            if not row:continue
            if row['project'] in removed:continue
            assignment=json.loads(row['assignment']);after=assignment.get('after',{})
            if any(str(after.get(k) or '')!=str(row[k] or '') for k in ('sku','scope','project','segment','source')):continue
            operation=assignment.get('operation')
            if not operation:continue
            c.execute("INSERT OR IGNORE INTO operations(id,created,kind,source,status) VALUES (?,?,'assign','legacy-metadata-backfill','queued')",(operation,time.time()))
            fields={'AssetId':row['id'],'FileId':row['file_id'],'SKU':row['sku'],'ShootType':row['scope'],
                    'ProjectId':row['project'],'SegmentId':row['segment'],'MetadataRevision':row['revision'],'_manual_backfill':1}
            c.execute('''INSERT INTO metadata_jobs(id,operation,file,asset,revision,path,expected_sha,mode,fields,status,created)
 VALUES (?,?,?,?,?,?,?,'sidecar',?,'queued',?)''',(uuid.uuid4().hex,operation,row['file_id'],row['id'],row['revision'],
 row['path'],row['path_sha'] or row['file_sha'],json.dumps(fields,ensure_ascii=False),time.time()))
            c.execute("UPDATE operations SET status='queued' WHERE id=?",(operation,));count+=1
        if count:c.execute('INSERT INTO audit VALUES (?,?,?,?)',(uuid.uuid4().hex,'legacy_metadata_backfill',json.dumps({'created_jobs':count}),time.time()))
    return count


def pause_pending(c,table,field,*,linked_only=False):
    """只标记历史任务，不删除确认结果；1 为已明确提交写入，-1 为等待手动补写。"""
    assert (table,field) in (('metadata_jobs','fields'),('tasks','data'))
    guard=" AND asset IN (SELECT asset FROM catalog_links)" if linked_only else " AND kind IN ('metadata','metadata_fill','metadata_sidecar')"
    c.execute(f"""UPDATE {table} SET {field}=json_set({field},'$._manual_backfill',-1),reason=CASE WHEN reason='' THEN '历史照片等待手动补写，请在资产归档点击补写元数据' ELSE reason END
        WHERE status IN ('queued','running','failed','waiting_native')
        AND coalesce(json_extract({field},'$._manual_backfill'),0) NOT IN (-1,1)"""+guard)


def release_pending(archive,prefix=''):
    """仅资产归档的手动入口释放所选范围；保留原任务、文件校验与归属版本。"""
    guard='';params=[]
    if prefix:
        prefix=prefix.rstrip('\\/')
        escaped=prefix.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        guard=" AND (path=? OR path LIKE ? ESCAPE '\\')";params=[prefix,escaped+'\\\\%']
    with archive.connect() as c:
        c.execute("""UPDATE metadata_jobs SET fields=json_set(fields,'$._manual_backfill',1),
            status='queued',reason='',retry_at=0,retry_count=0
            WHERE coalesce(json_extract(fields,'$._manual_backfill'),0)!=1
            AND status IN ('queued','failed','waiting_native')"""+guard,params)
