"""归档专用有界并行批量读取与持久文件版本缓存，不写媒体文件。"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor,TimeoutError
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

TAGS=['-fast','-Model','-SerialNumber','-DateTimeOriginal','-SubSecTimeOriginal','-SubSecDateTimeOriginal',
      '-ShutterCount','-FileNumber','-ImageNumber','-XMP-fk:all','-XMP-xmp:Rating','-XMP-dc:all']
TAGS+=['-XMP-tiff:Model','-XMP-aux:SerialNumber','-XMP-exifEX:BodySerialNumber','-XMP-exif:DateTimeOriginal','-XMP-photoshop:DateCreated']

def signature(path):
    stat=Path(path).stat()
    return [stat.st_size,stat.st_mtime_ns]

def metadata_sidecars(files,*,progress=None,stage='sidecar_scanning'):
    """每目录枚举一次，避免对每张NAS照片发出两次不存在的XMP查询。"""
    progress=progress or (lambda **_:None)
    files=list(files);parents={}
    for path in files:
        path=Path(path);parents.setdefault(path.parent,set()).update((path.with_suffix('.xmp'),Path(str(path)+'.xmp')))
    found={}
    for index,(parent,wanted) in enumerate(parents.items()):
        progress(stage=stage,done=index,total=len(parents),path=str(parent))
        with os.scandir(parent) as entries:
            for entry in entries:
                path=Path(entry.path)
                if path not in wanted:continue
                if entry.is_symlink() or entry.is_junction():raise ValueError('XMP侧车不能是链接：'+str(path))
                if entry.is_file(follow_symlinks=False):found[path]=path
    progress(stage=stage,done=len(parents),total=len(parents),path='')
    return {str(path):list(dict.fromkeys(found[p] for p in (Path(path).with_suffix('.xmp'),Path(str(path)+'.xmp')) if p in found)) for path in files}

def capture_metadata_batches(writer,files,*,progress=None,stage='sources_metadata',workers=4,batch_size=32):
    """缓存仅复用size/mtime完全一致的原文件；侧车业务字段由调用方实时合并。"""
    progress=progress or (lambda **_:None)
    files=list(files);total=len(files);cache=writer.data/'folder-archive-metadata.sqlite3'
    with closing(sqlite3.connect(cache)) as c,c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE IF NOT EXISTS metadata(path TEXT PRIMARY KEY,size INTEGER,mtime INTEGER,payload TEXT)')
    batches=[files[i:i+batch_size] for i in range(0,total,batch_size)]
    def read(paths):
        versions={str(path):signature(path) for path in paths};values={}
        with closing(sqlite3.connect(cache,timeout=30)) as c:
            for name,size,mtime,payload in c.execute('SELECT path,size,mtime,payload FROM metadata WHERE path IN ('+','.join('?' for _ in paths)+')',list(versions)):
                if versions[name]==[size,mtime]:
                    try:record=json.loads(payload)
                    except (TypeError,ValueError):continue
                    if isinstance(record,dict) and Path(record.get('SourceFile',''))==Path(name) and not record.get('ExifTool:Error'):values[name]=record
        missing=[path for path in paths if str(path) not in values]
        if missing:
            raw=writer._read_request(['-G1','-s','-json',*TAGS,*missing])
            records=json.loads(raw.decode('utf-8-sig'))
            if len(records)!=len(missing):raise ValueError('批量归档元数据返回数量不一致')
            updates=[]
            for path,record in zip(missing,records):
                if Path(record.get('SourceFile',''))!=path:raise ValueError('批量归档元数据返回路径不一致')
                if record.get('ExifTool:Error'):raise ValueError('读取归档元数据失败：'+str(path)+' · '+str(record['ExifTool:Error']))
                if signature(path)!=versions[str(path)]:raise ValueError('读取期间来源文件改变，请重新匹配：'+str(path))
                values[str(path)]=record
                updates.append((str(path),*versions[str(path)],json.dumps(record,ensure_ascii=False)))
            with closing(sqlite3.connect(cache,timeout=30)) as c,c:
                c.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?,?,?)',updates)
        output=[]
        for path in paths:
            if signature(path)!=versions[str(path)]:raise ValueError('读取期间来源文件改变，请重新匹配：'+str(path))
            output.append((path,{**values[str(path)],'_archive_source_signature':versions[str(path)]}))
        return output
    progress(stage=stage,done=0,total=total,path=str(files[0]) if files else '',workers=workers)
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='archive-metadata') as pool:
        pending=deque();iterator=iter(batches)
        for batch in batches[:workers*2]:pending.append((batch,pool.submit(read,batch)))
        iterator=iter(batches[workers*2:]);done=0
        while pending:
            batch,future=pending.popleft()
            while True:
                try:records=future.result(timeout=.25);break
                except TimeoutError:
                    if future.done():raise
                    progress(stage=stage,done=done,total=total,path=str(batch[0]),workers=workers)
            for path,record in records:
                yield path,record
                done+=1
            next_batch=next(iterator,None)
            if next_batch is not None:pending.append((next_batch,pool.submit(read,next_batch)))
    progress(stage=stage,done=total,total=total,path='',workers=workers)
