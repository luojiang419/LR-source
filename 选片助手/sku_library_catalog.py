"""补齐已入库归档目录中有明确SKU证据的图片；只查询本机目录索引。"""
from pathlib import Path,PurePosixPath
from urllib.parse import quote
from sku_match import normalize

CONFIRMED=('manual','shooting','embedded','ocr_folder','vlm_group')


def catalog_rows(sku,seeds):
    path=sku.store.data/'asset_catalog.sqlite3'
    if not path.is_file():return []
    from sku_library_index import Reader
    scopes={(r['root_path'].rstrip('/\\').casefold(),r['folder'],r['normalized']):r['sku']
            for r in seeds if r['source']=='archive' and r['normalized']}
    if not scopes:return []
    rows=[]
    with Reader(path) as reader,reader.connect() as c:
        roots={r['path'].rstrip('/\\').casefold():dict(r) for r in c.execute('SELECT id,path FROM roots WHERE removed=0')}
        for base,folder,code in sorted(scopes):
            root=roots.get(base)
            if not root:continue
            # 以小目录索引定位，不能按SKU跨整个历史库随机取数，更不能扫描NAS原片。
            sku_folder=normalize(PurePosixPath(folder).name)==code
            members=c.execute('''SELECT * FROM items INDEXED BY catalog_items_root_folder
                WHERE root=? AND online=1 AND folder=? AND media_type='image'
                ORDER BY name,id''',(root['id'],folder)).fetchall()
            pairs={};formats={}
            for member in members:
                row=dict(member);key=row['pair_key'];prior=pairs.get(key)
                formats.setdefault(key,set()).add(row['extension'].lower())
                rank=lambda r:(r['source']!='manual',r['source'] not in CONFIRMED,r['extension'].lower() not in ('.jpg','.jpeg'),r['id'])
                if prior is None or rank(row)<rank(prior):pairs[key]=row
            for row in pairs.values():
                confirmed=row['source'] in CONFIRMED and bool(row['normalized'])
                inherited=sku_folder and not confirmed and (not row['normalized'] or row['normalized']==code)
                if not inherited and not (confirmed and row['normalized']==code):continue
                extensions=formats[row['pair_key']]
                rows.append({**row,'id':'catalog:'+row['id'],'catalog_item_id':row['id'],
                    'sku':scopes[base,folder,code] if inherited else row['sku'],'normalized':code,
                    'catalog_source':row['source'],'session_id':'catalog:'+row['root'],'capture_id':row['id'],
                    'root_path':root['path'],'source':'catalog','provenance':'archive','provenance_label':'SKU目录' if inherited else '归档已确认',
                    'created':row['last_seen'],'time':'','interval_id':'',
                    'preview':'/api/catalog/preview/'+quote(row['id'])+'?size=420',
                    'has_jpg':bool(extensions&{'.jpg','.jpeg'}),'has_raw':bool(extensions&{'.cr2','.cr3','.nef','.arw','.dng','.raf','.orf','.rw2'})})
    return rows


def ensure_asset(archive,row):
    """仅在用户明确提交修正时，将索引照片登记到已有元数据写入队列。"""
    if row.get('source')!='catalog':return archive.ensure(row['session_id'],row['capture_id'])
    from photo_metadata import ensure_file
    path=Path(row['path']);root=Path(row['root_path'])
    path.relative_to(root)
    for part in (path,*path.parents):
        if part.is_symlink() or part.is_junction():raise ValueError('不编辑文件或目录链接中的元数据')
    if not path.is_file():raise ValueError('原图已离线，恢复连接后可编辑元数据')
    return ensure_file(archive,path,root)['asset']
