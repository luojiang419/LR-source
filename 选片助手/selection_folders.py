"""选片会话目录索引；只查询已添加/联机目录，不遍历整块磁盘。"""
from pathlib import PurePosixPath


def tree(store):
    state=store.folder_state();roots=[]
    for session in state['sessions']:
        if session['id'] in state['archive_sessions']:continue
        folders={'':0}
        with store.connect() as db:
            # 目录树只需要路径，不反序列化照片完整 JSON 和评级数据。
            for relative,total in db.execute('SELECT folder,count(*) FROM browser_captures WHERE session=? AND files>0 GROUP BY folder',(session['id'],)):
                folder=PurePosixPath(relative or '.')
                for parent in (folder,*folder.parents):
                    key='' if str(parent)=='.' else str(parent)
                    folders[key]=folders.get(key,0)+total
        roots.append({k:session[k] for k in ('id','root','name','capture_count')} | {'folders':folders})
    return {'roots':roots}


def files(store,sid,folder='',offset=0):
    if store.is_removed(sid):raise ValueError('文件夹已从软件移除')
    folder=folder.replace('\\','/').strip('/')
    if '..' in folder.split('/'):raise ValueError('目录路径无效')
    offset=max(0,offset)
    predicate='folder=:folder AND files>0'
    params={'sid':sid,'offset':offset,'folder':folder}
    with store.connect() as db:
        total=db.execute('SELECT count(*) FROM browser_captures WHERE session=:sid AND '+predicate,params).fetchone()[0]
        rows=[{'id':r['id'],'paths':[p for p in (r['raw'],r['jpg']) if p]} for r in db.execute('SELECT id,raw,jpg FROM browser_captures WHERE session=:sid AND '+predicate+' ORDER BY position LIMIT 200 OFFSET :offset',params)]
    return {'rows':rows,'total':total,'offset':offset}
