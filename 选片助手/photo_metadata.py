"""归档图片描述元数据：侧车优先读取，用户修改通过已有持久队列写回。"""
import hashlib
import json
from pathlib import Path
import time

import media_engine as media
from metadata_writer import sha
from rating_xmp import sidecar_paths


def source(catalog,item_id):
    item=catalog.item(item_id);root=Path(catalog._root(item['root'])['path']);path=Path(item['path'])
    if item['media_type']!='image':raise ValueError('此元数据编辑入口仅支持图片和 RAW')
    path.relative_to(root)
    for part in (path,*path.parents):
        if part.is_symlink() or part.is_junction():raise ValueError('不编辑文件或目录链接中的元数据')
    if not path.is_file():raise ValueError('原图已离线，恢复连接后可编辑元数据')
    return path,root


def sidecar_hashes(path):return {str(side):sha(side) for side in sidecar_paths(path)}


def token(path):
    stat=path.stat()
    return hashlib.sha256(json.dumps([str(path),stat.st_size,stat.st_mtime_ns,sidecar_hashes(path)],sort_keys=True).encode()).hexdigest()


def read(archive,catalog,item_id):
    path,_=source(catalog,item_id);before=token(path);meta=archive.writer.read_asset(path)
    if token(path)!=before:raise ValueError('元数据读取期间发生变化，请重新打开图片')
    keywords=meta.get('XMP-dc:Subject',[]);keywords=keywords if isinstance(keywords,list) else [keywords]
    managed={f'SKU:{meta.get("XMP-fk:SKU")}',f'ShootType:{meta.get("XMP-fk:ShootType")}'}
    return {'title':meta.get('XMP-dc:Title') or '','description':meta.get('XMP-dc:Description') or '',
            'keywords':[value for value in keywords if value not in managed],
            'label':meta.get('XMP-xmp:Label') or '','token':before,
            'sidecars':[side.name for side in sidecar_paths(path)],
            'writable':archive.writer.capability(path,'sidecar')!='unsupported'}


def validate(values):
    if not isinstance(values,dict):raise ValueError('图片元数据须为对象')
    result={}
    for key,limit in (('title',1024),('description',16000),('label',100)):
        value=values.get(key,'')
        if not isinstance(value,str) or len(value)>limit or any(ord(c)<32 and (key!='description' or c not in '\n\r\t') for c in value):
            raise ValueError(f'{key} 内容无效或超过 {limit} 字符')
        result[{'title':'Title','description':'Description','label':'Label'}[key]]=value.strip().replace('\r\n','\n')
    keywords=values.get('keywords',[])
    if not isinstance(keywords,list) or len(keywords)>100:raise ValueError('关键词须为最多 100 项的列表')
    if any(not isinstance(value,str) or len(value)>200 or any(ord(c)<32 for c in value) for value in keywords):raise ValueError('每个关键词最多 200 字符，不能包含控制字符')
    result['Keywords']=list(dict.fromkeys(value.strip() for value in keywords if value.strip()))
    return result


def ensure_file(archive,path,root):
    with archive.lock:
        with archive.connect() as c:file=c.execute('SELECT f.* FROM locations l JOIN files f ON f.id=l.file WHERE l.path=?',(str(path),)).fetchone()
        if file is None:
            sid=archive.store.ensure_live_session(root);relative=path.relative_to(root).as_posix()
            with archive.store.connect() as c:
                record=c.execute("SELECT id FROM captures WHERE session=? AND (json_extract(data,'$.raw')=? OR json_extract(data,'$.jpg')=?) LIMIT 1",(sid,relative,relative)).fetchone()
                c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('archive_session:'+sid,str(root)))
            if record:cid=record['id']
            else:
                info=media.read_metadata([path],media.find_exiftool(archive.store.root))[0]
                if info.get('Error'):raise ValueError('图片元数据无法读取：'+info['Error'])
                capture=media.capture_record(root,path,None,info);archive.store.append_live(sid,[capture]);cid=capture['id']
            archive.ensure(sid,cid,inherit=False)
            with archive.connect() as c:file=c.execute('SELECT f.* FROM locations l JOIN files f ON f.id=l.file WHERE l.path=?',(str(path),)).fetchone()
        return dict(file)


def assign_sku(archive,catalog,item_id,sku):
    path,root=source(catalog,item_id)
    with archive.lock:
        file=ensure_file(archive,path,root);asset=archive.asset(file['asset'])
        preview=archive.preview(None,None,sku,asset['scope'] or '未分类',asset['project'],asset_ids=[file['asset']],overwrite=True)
        return archive.apply(preview['preview_id'])


def save(archive,catalog,item_id,values,expected_token):
    fields=validate(values);path,root=source(catalog,item_id)
    from asset_archive import uid,dumps
    with archive.lock:
        if token(path)!=expected_token:raise ValueError('图片或 XMP 已被修改，请重新打开图片后再编辑')
        file=ensure_file(archive,path,root)
        if archive.writer.capability(path,'sidecar')=='unsupported':raise ValueError('此格式暂不支持元数据写回')
        expected_sha=sha(path)
        if token(path)!=expected_token:raise ValueError('元数据准备期间图片或 XMP 已改变，请重新打开图片')
        fields.update(_descriptive=True,_sidecar_hashes=sidecar_hashes(path))
        oid=uid()
        with archive.connect() as c:
            revision=c.execute('SELECT revision FROM assets WHERE id=?',(file['asset'],)).fetchone()[0]
            c.execute('INSERT INTO operations(id,created,kind,source) VALUES (?,?,?,?)',(oid,time.time(),'description','manual'))
            c.execute('INSERT INTO metadata_jobs(id,operation,file,asset,revision,path,expected_sha,mode,fields,status,created) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                      (uid(),oid,file['id'],file['asset'],revision,str(path),expected_sha,'sidecar',dumps(fields),'queued',time.time()))
        return archive.operation(oid)
