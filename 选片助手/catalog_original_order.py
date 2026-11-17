"""归档原顺序：文件名升序；不随评级、OCR或文件修改时间变化。"""
import base64
import json
import sqlite3
from asset_catalog import _id,_json,ARCHIVE_MEDIA_SQL
from sku_match import normalize


def items(catalog,root,folder,sku,offset,limit,cursor,text,media,*,include_tree=False,cancel=None,quick=False,visual_only=False):
    limit=max(1,min(240,int(limit)));offset=max(0,int(offset))
    clauses=['online=1'];args=[]
    if visual_only:clauses.append(ARCHIVE_MEDIA_SQL)
    if root:clauses.append('root=?');args.append(root)
    visited=bool(getattr(catalog,'visits',None))
    if root and visited:
        clauses.append('folder=?');args.append(folder or '/')
    elif folder:
        pattern=folder.rstrip('/').replace('\\','\\\\').replace('%',r'\%').replace('_',r'\_')+'/%'
        clauses.append("(folder=? OR folder LIKE ? ESCAPE '\\')");args.extend([folder,pattern])
    if sku:clauses.append('normalized LIKE ?');args.append('%'+normalize(sku)+'%')
    if media:
        if media not in ('image','video','design'):raise ValueError('素材类型筛选无效')
        clauses.append('media_type=?');args.append(media)
    if cursor:
        try:
            marker=json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if len(marker)!=3 or marker[0]!='original-asc-v1':raise ValueError()
            clauses.append('(name>? OR (name=? AND id>?))');args.extend([str(marker[1]),str(marker[1]),str(marker[2])])
        except (ValueError,TypeError,UnicodeDecodeError):raise ValueError('归档分页标记无效') from None
    with catalog._search_connection(cancel) as c:
        source='capture_items';source_args=[]
        if text:
            source,source_args,condition,values=catalog._search_query(c,text)
            clauses.append(condition);args.extend(values)
        elif root and visited:
            source="""(SELECT id AS folder_item_id FROM items INDEXED BY catalog_folder_candidates
                WHERE root=? AND online=1 AND folder COLLATE NOCASE=?) folder_candidates
                CROSS JOIN capture_items ON capture_items.id=folder_candidates.folder_item_id"""
            source_args=[root,folder or '/']
        elif root and folder:
            source="""(SELECT id AS folder_item_id FROM items INDEXED BY catalog_folder_candidates
                WHERE root=? AND online=1 AND folder COLLATE NOCASE=?
                UNION SELECT id AS folder_item_id FROM items INDEXED BY catalog_folder_candidates
                WHERE root=? AND online=1 AND folder LIKE ? ESCAPE '\\') folder_candidates
                CROSS JOIN capture_items ON capture_items.id=folder_candidates.folder_item_id"""
            source_args=[root,folder,root,pattern]
        where=' WHERE '+' AND '.join(clauses)
        total=None if quick and not visited and (not folder or cursor) else c.execute('SELECT count(*) FROM '+source+where,source_args+args).fetchone()[0]
        rows=[dict(r) for r in c.execute('SELECT capture_items.* FROM '+source+where+' ORDER BY name ASC,id LIMIT ? OFFSET ?',source_args+args+[limit+1,offset if not cursor else 0])]
    more=len(rows)>limit;rows=rows[:limit]
    for row in rows:row['preview']='/api/catalog/preview/'+row['id']+'?v='+_id(row['fingerprint']+':orientation-v1')+('&frame='+str(catalog.video.frame) if row['media_type']=='video' else '')
    try:catalog.cached_preview_hints(rows)
    except (OSError,sqlite3.Error):pass
    next_cursor=base64.urlsafe_b64encode(_json(['original-asc-v1',rows[-1]['name'],rows[-1]['id']]).encode()).decode() if rows else ''
    result={'rows':rows,'total':total,'offset':offset,'has_more':more,'next_cursor':next_cursor,'engine':catalog.fast_index.engine,'media':media,'search':text,'sort':'original'}
    if include_tree:result['tree']=catalog.tree(text,media,cancel=cancel)
    return result
