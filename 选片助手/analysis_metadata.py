"""多维度结果使用既有持久写回队列，独立于人工 SKU 和描述字段。"""
from pathlib import Path
import time

from retrieval_schema import digest,dumps
from photo_metadata import sidecar_hashes


def enqueue(archive,asset,value,input_sha):
    oid=digest(['multidimensional',asset,input_sha,value])[:32]
    with archive.lock,archive.connect() as c:
        current=c.execute('SELECT revision FROM assets WHERE id=?',(asset,)).fetchone()
        if not current:raise ValueError('解析资产已不存在')
        if c.execute('SELECT 1 FROM operations WHERE id=?',(oid,)).fetchone():return oid
        files=c.execute("SELECT f.*,l.path FROM files f JOIN locations l ON l.file=f.id WHERE f.asset=? AND l.status='online'",(asset,)).fetchall()
        c.execute("INSERT INTO operations(id,created,kind,source) VALUES (?,?,'description','multidimensional')",(oid,time.time()))
        for file in files:
            path=Path(file['path'])
            if archive.writer.capability(path,'sidecar')=='unsupported':continue
            fields={'Multidimensional':dumps(value),'_descriptive':True,'_sidecar_hashes':sidecar_hashes(path)}
            c.execute('''INSERT INTO metadata_jobs(id,operation,file,asset,revision,path,expected_sha,mode,fields,status,created)
                VALUES (?,?,?,?,?,?,?,'sidecar',?,'queued',?)''',
                (digest([oid,file['id'],str(path)])[:32],oid,file['id'],asset,current[0],str(path),file['sha'],dumps(fields),time.time()))
    return oid
