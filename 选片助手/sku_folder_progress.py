"""将绑定目录的有效二星确认投影到当前清单，不改原任务或照片归属。"""
from datetime import datetime,timezone
import json
from pathlib import Path
import uuid

from sku_match import normalize
from sku_progress import completion_order


def evidence(sku,c,sid):
    session=sku.store.session(sid) if sid else None
    if not session:return [],None
    records=sku.store.records(sid);overrides=session['state'].get('overrides',{})
    starred={r['id']:r for r in records if overrides.get(r['id'],r.get('existingRating',0))==2}
    choices={r['capture_id']:r['sku'] for r in c.execute('SELECT capture_id,sku FROM ocr_choices WHERE session_id=?',(sid,))}
    jobs={}
    for row in c.execute("SELECT * FROM jobs WHERE session_id=? AND status IN ('matched','confirmed') ORDER BY (status='confirmed') DESC,updated DESC,created DESC",(sid,)):
        jobs.setdefault(row['capture_id'],dict(row))
    versions={}
    for row in c.execute('SELECT sku,base_sku FROM sku_shoot_versions'):
        versions.setdefault(row['sku'],set()).add(row['base_sku'])
    result=[]
    for cid,record in starred.items():
        code=choices.get(cid);job=jobs.get(cid)
        if job and not code:
            result_json=json.loads(job['result'] or '{}')
            if not result_json.get('requires_sku_choice'):
                target_codes={r['sku'] for rid in json.loads(job['targets'] or '[]') for r in c.execute('SELECT sku FROM sku_rows WHERE id=?',(rid,))}
                if len(target_codes)==1:code=next(iter(target_codes))
        if not code or not job:continue
        fingerprint=job['fingerprint'];seen=set()
        while fingerprint!=record['fingerprint'] and fingerprint not in seen:
            seen.add(fingerprint)
            change=c.execute('SELECT after FROM metadata_versions WHERE session=? AND capture=? AND before=?',(sid,cid,fingerprint)).fetchone()
            if not change:break
            fingerprint=change[0]
        if fingerprint!=record['fingerprint']:continue
        bases=versions.get(code,set());base=next(iter(bases)) if len(bases)==1 else code
        result.append({'job':job,'record':record,'sku':code,'base':base,'normalized':normalize(base)})
    summary={'session_id':sid,'folder':session['root'],'two_star':len(starred),'confirmed_labels':len(result),
             'confirmed_skus':len({r['normalized'] for r in result}),'pending_labels':len(starred)-len(result)}
    return result,summary


def signature(sku,c,project):
    sid=project.get('session_id');session=sku.store.session(sid) if sid else None
    if not session:return (sid,None)
    with sku.store.connect() as db:
        generation=tuple(db.execute('SELECT generation,data_generation FROM browser_versions WHERE session=?',(sid,)).fetchone() or ())
    choices=tuple(tuple(r) for r in c.execute('SELECT capture_id,sku,updated FROM ocr_choices WHERE session_id=? ORDER BY capture_id',(sid,)))
    jobs=tuple(tuple(r) for r in c.execute('SELECT id,status,fingerprint,scope,updated,captured_at,result,targets FROM jobs WHERE session_id=? ORDER BY id',(sid,)))
    return (sid,session['revision'],session['snapshot'],generation,choices,jobs,
            json.dumps(project['metadata'].get('sheets',[]),sort_keys=True),project['metadata'].get('managed_sheet'),
            c.execute('SELECT count(*) FROM sku_rows WHERE project=?',(project['id'],)).fetchone()[0])


def reconcile(sku,c,project,rows):
    """返回当前目录覆盖的照片身份和行→原作业关联；原作业不移动、不克隆。"""
    sid=project.get('session_id');items,summary=evidence(sku,c,sid)
    targets={};handled={(sid,item['job']['capture_id']) for item in items}
    metadata=project['metadata'];managed=metadata.get('managed_sheet')
    sheets={s['name'] for s in metadata['sheets'] if not s.get('skipped')}
    def put(row,job):
        if row['baseline']=='已拍摄' and not row.get('completion_job') and job['project']!=project['id'] and max(job['created'],job['updated'])<=project['created']:
            return  # 原表已完成和原表选图属于导入基线，旧目录证据无需替换它。
        before=targets.get(row['id'])
        if before is None or completion_order(job)>completion_order(before):targets[row['id']]=job
    # 换绑定目录后保留此前已关联且仍有效的证据；不会新扫描其他历史目录。
    prior_sessions={}
    for row in rows:
        if not row.get('completion_job'):continue
        job=c.execute('SELECT * FROM jobs WHERE id=?',(row['completion_job'],)).fetchone()
        if not job or job['project']==project['id'] or job['session_id']==sid:continue
        other=job['session_id']
        if other not in prior_sessions:prior_sessions[other]={i['job']['capture_id']:i for i in evidence(sku,c,other)[0]}
        item=prior_sessions[other].get(job['capture_id'])
        if item and item['normalized']==row['normalized']:put(row,item['job'])
    changed=False
    for item in items:
        job=item['job'];key=item['normalized'];scope=job['scope']
        planned=[r for r in rows if r['normalized']==key and r['sheet']!=managed]
        scoped=[r for r in planned if r['sheet']==scope]
        if scope in sheets and scope!=managed:
            matches=scoped
        else:
            matches=planned if len({r['sheet'] for r in planned})==1 else []
        if not matches:
            matches=[r for r in rows if r['normalized']==key and r['sheet']==managed]
        if not matches:
            if not managed:
                managed='临时插拍'
                while managed in sheets:managed+='记录'
                metadata['managed_sheet']=managed;sheets.add(managed)
                metadata['sheets'].append({'name':managed,'path':'','header_row':1,'sku_col':1,'status_col':5,'status_letter':'E',
                    'new_status':False,'time_col':6,'time_letter':'F','columns':[{'index':i,'letter':chr(64+i),'title':title}
                    for i,title in enumerate(('款号','来源文件','现场文件夹','记录时间'),1)],'images':[]})
                c.execute('UPDATE projects SET metadata=? WHERE id=?',(json.dumps(metadata,ensure_ascii=False),project['id']))
            number=max([r['excel_row'] for r in rows if r['sheet']==managed]+[1])+1
            stamp=datetime.fromtimestamp(job['created'],timezone.utc).astimezone().isoformat(timespec='seconds')
            values={'A':item['base'],'B':item['record'].get('jpg') or item['record'].get('raw'),'C':Path(summary['folder']).name,'D':stamp}
            row={'id':uuid.uuid4().hex,'project':project['id'],'sheet':managed,'excel_row':number,'sku':item['base'],
                 'normalized':key,'baseline':'未拍摄','status':'未拍摄','baseline_completed_at':'','completed_at':'','completion_job':''}
            c.execute('INSERT INTO sku_rows(id,project,sheet,excel_row,sku,normalized,baseline,status,source_values) VALUES (?,?,?,?,?,?,?,?,?)',
                      (row['id'],project['id'],managed,number,item['base'],key,'未拍摄','未拍摄',json.dumps(values,ensure_ascii=False)))
            rows.append(row);matches=[row];changed=True
        for row in matches:put(row,job)
    return targets,handled,summary,changed
