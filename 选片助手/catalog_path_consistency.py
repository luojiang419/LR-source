"""按索引绝对路径纠正root对应的相对字段；不访问或移动媒体原件。"""
import json
import os
from pathlib import Path


def repair_relative_paths(catalog, *, root_ids=None, journal=None):
    """只更新已确认仍与读取值一致的relative/folder，其他业务列保持。"""
    changes=[];outside=[];checked=0
    with catalog.connect() as c:
        roots={r['id']:Path(r['path']).absolute() for r in c.execute('SELECT id,path FROM roots WHERE removed=0')}
        if root_ids is not None:roots={key:value for key,value in roots.items() if key in root_ids}
        for root_id,base in roots.items():
            for row in c.execute('SELECT id,path,relative,folder FROM items WHERE root=?',(root_id,)):
                checked+=1
                try:relative=Path(row['path']).absolute().relative_to(base).as_posix()
                except ValueError:
                    outside.append(row['id']);continue
                folder=Path(relative).parent.as_posix()
                if folder=='.':folder='/'
                if relative!=row['relative'] or folder!=row['folder']:
                    changes.append({'id':row['id'],'root':root_id,'path':row['path'],'before_relative':row['relative'],
                                    'before_folder':row['folder'],'relative':relative,'folder':folder})
        if journal:
            path=Path(journal);path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('x',encoding='utf8') as stream:
                json.dump({'changes':changes,'outside_root':outside},stream,ensure_ascii=False,indent=2)
                stream.flush();os.fsync(stream.fileno())
        repaired=0
        for row in changes:
            result=c.execute('UPDATE items SET relative=?,folder=? WHERE id=? AND root=? AND path=? AND relative=? AND folder=?',
                             (row['relative'],row['folder'],row['id'],row['root'],row['path'],row['before_relative'],row['before_folder']))
            repaired+=result.rowcount
    return {'checked':checked,'repaired':repaired,'concurrent_changes':len(changes)-repaired,'outside_root':outside}
