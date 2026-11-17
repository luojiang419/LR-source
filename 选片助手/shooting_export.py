"""显式导出的隔离快照、后台任务与实际工作进度。拍摄不生成图片文档。"""
import copy
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid


def _backup(source,target):
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dst:
        src.backup(dst,pages=256)


def export_snapshot(sku,pid,progress=None,filename=None):
    progress=progress or (lambda **kw:None)
    progress(stage='snapshot',message='正在保存本次导出的记录快照')
    with tempfile.TemporaryDirectory(prefix='framekeep-export-') as folder:
        base=Path(folder);snapshot=copy.copy(sku)
        snapshot.lock=threading.RLock();snapshot.dirty=set();snapshot.root=base/'projects';snapshot.root.mkdir()
        snapshot.db=base/'sku.sqlite3';snapshot.folder_progress_cache={};snapshot.folder_progress_summary={}
        with sku.lock:
            p=sku.project(pid)
            if not p:raise ValueError('拍摄文档不存在')
            _backup(sku.db,snapshot.db)
            for source_id in sku.document_sources(pid):
                dest=snapshot.root/source_id;dest.mkdir()
                shutil.copy2(sku.root/source_id/'source.xlsx',dest/'source.xlsx')
        snapshot.store=copy.copy(sku.store);snapshot.store.db=base/'captures.sqlite3'
        _backup(sku.store.db,snapshot.store.db)
        if sku.archive:
            # 共享评级和区间保护一起冻结；不等待原图写入持有的归档锁。
            ratings=sku.archive.ratings
            snapshot.archive=copy.copy(sku.archive);snapshot.archive.db=base/'archive.sqlite3'
            snapshot.archive.store=snapshot.store;snapshot.archive.lock=threading.RLock()
            snapshot.archive._ratings=copy.copy(ratings)
            snapshot.archive._ratings.archive=snapshot.archive;snapshot.archive._ratings.store=snapshot.store
            _backup(sku.archive.db,snapshot.archive.db)
        output=snapshot._export_document(pid,progress=progress)
        target=sku.root/pid/(filename or output.name)
        temporary=target.with_name('.'+target.name+'.'+uuid.uuid4().hex+'.exporting')
        try:
            shutil.copy2(output,temporary);temporary.replace(target)
        finally:temporary.unlink(missing_ok=True)
        with sku.connect() as c:c.execute("UPDATE projects SET export_error='' WHERE id=?",(pid,))
        progress(stage='complete',message='拍摄文档已生成',complete=True)
        return target


class ShootingExports:
    def __init__(self,sku):
        self.sku=sku;self.lock=threading.Lock();self.jobs={};self.closed=threading.Event()
        self.capacity=threading.Semaphore(1)

    def start(self,pid,name=''):
        p=self.sku.project(pid)
        if not p:raise ValueError('拍摄文档不存在')
        with self.lock:
            if self.closed.is_set():raise ValueError('程序正在关闭，请重新打开后导出')
            for job in self.jobs.values():
                if job['project_id']==pid and job['status'] in ('queued','running'):return self._public(job)
            jid=uuid.uuid4().hex
            job={'id':jid,'project_id':pid,'document':p['name'],'name':name or p['name'],'status':'queued','stage':'queued',
                 'message':'等待导出','images_done':0,'images_total':0,'missing_images':0,'percent':0,
                 'created':time.time(),'source_name':'拍摄记录-'+jid+'.xlsx','error':''}
            self.jobs[jid]=job
            thread=threading.Thread(target=self._run,args=(jid,),daemon=True,name='shooting-export')
            job['_thread']=thread;thread.start();return self._public(job)

    @staticmethod
    def _public(job):return {k:v for k,v in job.items() if not k.startswith('_')}

    def state(self,jid):
        with self.lock:
            if jid not in self.jobs:raise ValueError('导出任务不存在，请重新导出')
            return self._public(self.jobs[jid])

    def output(self,jid):
        job=self.state(jid)
        if job['status']!='complete':raise ValueError('拍摄文档尚未生成完成')
        path=self.sku.root/job['project_id']/job['source_name']
        if not path.is_file():raise ValueError('导出文件已不可用，请重新导出')
        return path,job

    def _run(self,jid):
        with self.capacity:
            with self.lock:
                job=self.jobs[jid];job['status']='running'
            def progress(**values):
                if self.closed.is_set():raise RuntimeError('程序已关闭，导出已停止；拍摄记录保留')
                with self.lock:
                    job.update(values)
                    total=job['images_total']+4
                    completed=job['images_done']+{'snapshot':0,'images':1,'workbook':2,'labels':1,'pack':2,'persist':3,'complete':4}.get(job['stage'],0)
                    job['percent']=100 if values.get('complete') else min(99,int(completed/total*100))
                    if values.get('complete'):job['status']='complete'
            try:
                export_snapshot(self.sku,job['project_id'],progress,job['source_name'])
            except Exception as exc:
                with self.lock:job.update(status='failed',stage='failed',error=str(exc),message='导出未完成，拍摄记录已保留')
                try:
                    with self.sku.connect() as c:c.execute('UPDATE projects SET export_error=? WHERE id=?',('文档导出失败：'+str(exc)[:180],job['project_id']))
                except sqlite3.Error:pass

    def close(self):
        self.closed.set()
        with self.lock:threads=[j['_thread'] for j in self.jobs.values()]
        for thread in threads:thread.join(timeout=2)
