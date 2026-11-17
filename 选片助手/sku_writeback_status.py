"""现场当前版本的文件写入状态；查询不访问 NAS、不启动模型。"""
labels_for_phase={'waiting_file':'等待同张图片的星级或其他文件写回','preparing':'正在校验文件','waiting_archive':'等待归档处理收尾','committing':'正在保存元数据','verifying':'正在回读校验','refreshing':'正在更新索引'}
def statuses(archive,keys):
    result={};groups={};worker=archive.metadata_queue_state()
    with archive.connect() as c:
        for key in dict.fromkeys(keys):
            asset=c.execute('SELECT a.* FROM assets a JOIN catalog_links k ON k.asset=a.id WHERE k.session=? AND k.capture=?',key).fetchone()
            if not asset or not asset['sku']:
                result[key]={'status':'unassigned','total':0,'applied':0,'operation_ids':[],'jobs':[]};continue
            group=(asset['segment'] or asset['id'],asset['sku'],asset['project'],asset['scope'])
            if group not in groups:
                members=c.execute('SELECT id,revision FROM assets WHERE segment=? AND sku=? AND project=? AND scope=?',group).fetchall() if asset['segment'] else [asset]
                jobs=[]
                for member in members:
                    for file in c.execute('SELECT f.id,l.path FROM files f JOIN locations l ON l.file=f.id WHERE f.asset=?',(member['id'],)).fetchall():
                        job=c.execute("""SELECT id,operation,status,reason,coalesce(json_extract(fields,'$._manual_backfill'),0) AS manual_backfill FROM metadata_jobs WHERE asset=? AND revision=? AND file=? AND path=?
                            AND coalesce(json_extract(fields,'$._descriptive'),0)=0 ORDER BY created DESC,rowid DESC LIMIT 1""",(member['id'],member['revision'],file['id'],file['path'])).fetchone()
                        jobs.append(dict(job,path=file['path']) if job else {'path':file['path'],'operation':'','status':'missing','reason':'款号已入库，尚未创建文件写入任务'})
                done=sum(j['status']=='applied' for j in jobs)
                failed=any(j['status'] in ('failed','cancelled') for j in jobs)
                status='failed' if failed else 'writing' if any(j['status']=='running' for j in jobs) else 'queued' if any(j['status']=='queued' for j in jobs) else 'applied' if jobs and done==len(jobs) else 'missing'
                message=''
                active=next((j for j in jobs if j.get('id')==worker['job_id']),None)
                if status in ('queued','writing','missing'):
                    if any(j.get('manual_backfill')==-1 for j in jobs):message='历史照片等待手动补写，请在资产归档点击补写元数据'
                    elif not worker['alive']:message='文件写入服务尚未启动'
                    elif worker.get('live_session') and key[0]!=worker['live_session']:message='正在拍摄，旧照片写入已暂停；历史照片请在资产归档点击补写元数据'
                    elif worker['phase']=='error':message='写入队列异常：'+worker['error']
                    elif status=='missing' and worker.get('backfill',{}).get('phase')=='error':message='旧记录补齐失败：'+worker['backfill']['error']
                    elif status=='missing':message='正在补齐旧记录文件任务' if worker.get('backfill',{}).get('phase')=='scanning' else '旧记录尚未创建文件任务，请在资产归档点击补写元数据'
                    elif active:
                        message=labels_for_phase.get(worker['phase'],'正在处理文件')+' · '+str(worker['elapsed_seconds'])+' 秒'
                    elif worker['path']:
                        phase=labels_for_phase.get(worker['phase'],'')
                        pending=c.execute("SELECT count(*) FROM metadata_jobs WHERE status IN ('queued','running')").fetchone()[0]
                        message='正在处理其他图片：'+worker['path'].replace('\\','/').rsplit('/',1)[-1]+(' · '+phase if phase else '')+f' · 队列共 {pending} 个待写'
                    else:
                        waiting=next((j['reason'] for j in jobs if j['status']=='queued' and j['reason']),None)
                        if waiting and waiting.startswith('文件暂被占用，'):waiting=waiting.split('：',1)[0]
                        message=waiting or ('正在领取文件任务' if worker['phase']=='claiming' else '文件任务排队中')
                groups[group]={'status':status,'total':len(jobs),'applied':done,'operation_ids':list(dict.fromkeys(j['operation'] for j in jobs if j['operation'])),'jobs':jobs,'worker_message':message}
            result[key]=dict(groups[group],sku=asset['sku'])
    return result
