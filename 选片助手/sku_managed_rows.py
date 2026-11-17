"""同步自有现场记录中的已修正款号，不修改导入计划或原识别证据。"""
from collections import defaultdict
import json
from pathlib import Path
from shooting_documents import managed_sheets


def reconcile(sku,c,project,rows,targets,confirmed):
    managed=managed_sheets(project['metadata'])
    if not managed:return False
    by_id={row['id']:row for row in rows}
    replacements=defaultdict(set)
    # 同款多标签只显示最新完成证据，但每个有效标签均可用于修正
    # 自己产生的旧记录，不能只检查最终展示的那一张标签。
    for item in confirmed:
        job=item['job']
        if job['status']=='confirmed':replacements[(job['session_id'],job['capture_id'])].add(item['normalized'])
    for rid,job in targets.items():
        if rid in by_id and job['status']=='confirmed':
            replacements[(job['session_id'],job['capture_id'])].add(by_id[rid]['normalized'])
    # 旧版本没有保存标签 ID。仅在来源目录名 + 完整相对文件路径唯一
    # 对应当前有效确认时恢复身份；同名来源有歧义则不自动处理。
    sources=defaultdict(set)
    origins=set(replacements)
    origins.update((job[0],job[1]) for job in c.execute('SELECT DISTINCT session_id,capture_id FROM jobs WHERE project=?',(project['id'],)))
    for sid in {key[0] for key in origins}:
        session=sku.store.session(sid)
        if not session:continue
        ids=[cid for source,cid in origins if source==sid]
        for record in sku.store.records(sid,ids=ids):
            for key in ('jpg','raw'):
                if record.get(key):sources[(Path(session['root']).name.casefold(),record[key].replace('\\','/').casefold())].add((sid,record['id']))
    obsolete=[]
    for row in rows:
        if row['sheet'] not in managed or row['id'] in targets or row['baseline']=='已拍摄':continue
        values=json.loads(row.get('source_values') or '{}')
        origin=values.get('_label')
        if isinstance(origin,dict):
            identity=(origin.get('session_id'),origin.get('capture_id'))
        elif origin is None:
            candidates=sources.get((str(values.get('C','')).casefold(),str(values.get('B','')).replace('\\','/').casefold()),set())
            if len(candidates)!=1:continue
            identity=next(iter(candidates))
        else:continue
        current=replacements.get(identity,set())
        if len(current)==1 and row['normalized'] not in current:obsolete.append(row['id'])
    if not obsolete:return False
    c.executemany('DELETE FROM sku_rows WHERE id=?',[(rid,) for rid in obsolete])
    removed=set(obsolete);rows[:]=[row for row in rows if row['id'] not in removed]
    # 自有工作表重建时按连续行输出；同步原表行索引，避免导出将状态
    # 和图片写到删除前的旧行号。行 ID、来源时间和其余表格行保持。
    remaining=sorted((row for row in rows if row['sheet'] in managed),key=lambda row:(row['sheet'],row['excel_row']))
    c.executemany('UPDATE sku_rows SET excel_row=? WHERE id=?',[(-row['excel_row'],row['id']) for row in remaining])
    numbers={}
    for row in remaining:
        number=numbers.get(row['sheet'],1)+1;numbers[row['sheet']]=number
        row['excel_row']=number
        c.execute('UPDATE sku_rows SET excel_row=? WHERE id=?',(number,row['id']))
    return True
