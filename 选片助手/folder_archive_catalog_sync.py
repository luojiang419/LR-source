"""整理后的目录索引迁移；保留原素材ID并容纳后台提前发现的新路径。"""
from pathlib import Path
import json,time,uuid
from asset_catalog import archive_folder,pair_key


def synchronize_catalog_paths(c,moves):
    # 先取得写事务，保证检查新路径与更新索引之间不会插入另一条扫描记录。
    c.execute('BEGIN IMMEDIATE')
    c.execute('''CREATE TABLE IF NOT EXISTS folder_archive_path_collisions(
      before_path TEXT NOT NULL,after_path TEXT NOT NULL,canonical_item TEXT NOT NULL,
      retained_item TEXT NOT NULL,retained_snapshot TEXT NOT NULL,created REAL NOT NULL,
      PRIMARY KEY(before_path,after_path))''')
    roots={r['id']:dict(r) for r in c.execute('SELECT * FROM roots')}
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def locate(path,preferred):
        value=Path(path)
        if preferred in roots and not roots[preferred].get('removed') and value.is_relative_to(Path(roots[preferred]['path'])):
            return preferred,value.relative_to(Path(roots[preferred]['path'])).as_posix()
        candidates=[r for r in roots.values() if not r.get('removed') and value.is_relative_to(Path(r['path']))]
        if not candidates:raise ValueError('整理后路径不在已登记资产根内：'+str(value))
        root=max(candidates,key=lambda r:len(Path(r['path']).parts))
        return root['id'],value.relative_to(Path(root['path'])).as_posix()

    for move in moves:
        before,after=move['before'],move['after']
        if before==after:continue
        old=c.execute('SELECT * FROM items WHERE path=?',(before,)).fetchone()
        if old is None:continue  # 已同步，或该文件原本未进入资产索引。
        new=c.execute('SELECT * FROM items WHERE path=?',(after,)).fetchone()
        collision=c.execute('SELECT * FROM folder_archive_path_collisions WHERE before_path=? AND after_path=?',(before,after)).fetchone()
        if collision:
            if new and new['id']==collision['canonical_item']:continue
            raise ValueError('已同步路径的素材身份被外部改变，需复核：'+after)
        old=dict(old)
        if new:
            new=dict(new)
            if new['source'] in ('manual','shooting') and new['sku'] and old['sku'] and new['sku']!=old['sku']:
                raise ValueError('新旧索引存在已确认SKU冲突，未覆盖：'+after)
            # 不删除扫描条目及其关联资料，保留为原位置的离线历史。
            # 原素材ID、确认SKU、解析结果和更正记录继续绑定真实新路径。
            c.execute('INSERT INTO folder_archive_path_collisions VALUES(?,?,?,?,?,?)',
                      (before,after,old['id'],new['id'],json.dumps(new,ensure_ascii=False),time.time()))
            temporary=before+'.framekeep-path-sync-'+uuid.uuid4().hex
            c.execute('UPDATE items SET path=? WHERE id=?',(temporary,old['id']))
            root,relative=locate(before,new['root'])
            c.execute('UPDATE items SET path=?,root=?,relative=?,folder=?,name=?,pair_key=?,online=0 WHERE id=?',
                      (before,root,relative,archive_folder(relative),Path(before).name,pair_key(before),new['id']))
            if 'corrections' in tables:
                c.execute('UPDATE corrections SET item=? WHERE item=?',(old['id'],new['id']))
        root,relative=locate(after,old['root'])
        c.execute('UPDATE items SET path=?,root=?,relative=?,folder=?,name=?,pair_key=?,online=1,scan_epoch=? WHERE id=?',
                  (after,root,relative,archive_folder(relative),Path(after).name,pair_key(after),roots[root].get('scan_epoch',0),old['id']))
        if 'shooting_catalog_items' in tables:
            c.execute('UPDATE shooting_catalog_items SET path=? WHERE item=?',(after,old['id']))
