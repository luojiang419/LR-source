"""正式归档后台任务：快照、真实进度、暂停续作、重启恢复与报告下载。"""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from folder_archive_sources import FolderArchiveSources
from folder_archive_report import build_report
from sku_folder_archive import SkuFolderArchive


def save(path,value):
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf8')
    for attempt in range(30):
        try:temp.replace(path);return
        except PermissionError:
            if attempt==29:raise
            time.sleep(.02)


class FolderArchiveJobs:
    def __init__(self,store,archive,catalog,watch=None):
        self.store,self.archive,self.catalog=store,archive,catalog
        self.sources=FolderArchiveSources(store,archive,catalog,watch)
        self.root=store.data/'folder-archive-jobs';self.root.mkdir(exist_ok=True)
        self.lock=threading.RLock();self.capacity=threading.Lock();self.jobs={};self.events={};self.closed=threading.Event();self.threads=[]
        self.progress_written={}
        for path in self.root.glob('*/job.json'):
            try:
                job=json.loads(path.read_text(encoding='utf8'))
                plan_path=path.parent/'plan.json'
                if job['status']=='ready' and plan_path.is_file() and not (path.parent/'result.json').exists():
                    plan=json.loads(plan_path.read_text(encoding='utf8'))
                    if plan.get('reference_scope')!='matched_final_only' and not plan.get('preprocess_journal'):
                        job.update(status='interrupted',stage='rules_changed',requires_rematch=True,error='',
                                   message='归档规则已更新，请重新匹配最终大档后确认')
                if job['status'] in ('running','preparing','paused','queued'):
                    job.update(status='interrupted',stage='interrupted',message='任务已保留，可继续未完成的文件',error='')
                self.jobs[job['id']]=job
            except (ValueError,KeyError,OSError):continue

    def _folder(self,jid):
        if jid not in self.jobs:raise ValueError('归档任务不存在')
        return self.root/jid

    def _change(self,jid,**values):
        with self.lock:
            job=self.jobs[jid];job.update(values,updated=time.time());save(self._folder(jid)/'job.json',job)

    def state(self,jid='',offset=0,limit=100):
        with self.lock:
            if not jid:
                return {'jobs':[self.public(j) for j in sorted(self.jobs.values(),key=lambda j:j['created'],reverse=True)[:30]]}
            if jid not in self.jobs:raise ValueError('归档任务不存在')
            job=copy.deepcopy(self.jobs[jid])
        end=time.time() if job['status'] in ('preparing','running','queued','paused') else job['updated']
        job['elapsed_seconds']=max(0,int(end-job.get('attempt_started',job['created'])))
        folder=self._folder(jid)
        file=folder/('result.json' if (folder/'result.json').exists() else 'plan.json')
        plan=json.loads(file.read_text(encoding='utf8')) if file.exists() else {}
        if job.get('requires_rematch'):
            plan={};job.update(expected=0,matched=0,label_count=0,label_file_count=0)
            if job['status']!='preparing':job.update(done=0,total=0,percent=0)
        records=plan.get('items',[])
        offset=max(0,int(offset));limit=min(100,max(1,int(limit)))
        job.update(items=records[offset:offset+limit],offset=offset,has_more=offset+limit<len(records),
                   deleted=plan.get('skipped_reference_records',[])[:30],excluded_records=plan.get('excluded_records',[])[:100])
        return job

    @staticmethod
    def public(job):return {k:v for k,v in job.items() if k!='input'}

    def prepare(self,body):
        scope=self.sources.scope(body.get('root_id',''),body.get('folder',''))
        route=body.get('route','loose')
        if route not in ('loose','folders'):raise ValueError('归档方式无效')
        preprocess=body.get('preprocess',True)
        if type(preprocess) is not bool:raise ValueError('归档前处理开关无效')
        exclude_unregistered=body.get('exclude_unregistered',True)
        if type(exclude_unregistered) is not bool:raise ValueError('未入库排除开关无效')
        fast=body.get('fast_archive',True)
        if type(fast) is not bool:raise ValueError('极速归档开关无效')
        length=body.get('parent_length',4)
        if type(length) is not int or not 2<=length<=6:raise ValueError('母SKU位数无效')
        final=Path(body.get('final_path','')).expanduser() if route=='loose' else scope
        target=Path(body.get('target_path','')).expanduser()
        if not target.is_absolute() or not str(body.get('target_path','')).strip():raise ValueError('请选择完整的归档目标路径')
        if not final.is_absolute() or not final.is_dir():raise ValueError('最终大档目录不存在或已离线')
        jid=uuid.uuid4().hex;folder=self.root/jid;folder.mkdir()
        job={'id':jid,'created':time.time(),'updated':time.time(),'status':'preparing','stage':'sources',
             'message':'正在读取当前文件夹全部下级SKU与相机元数据','error':'','done':0,'total':0,
             'expected':0,'matched':0,'deleted_count':0,'label_count':0,'xmp':0,'percent':0,
             'input':{'source':str(scope),'final':str(final.resolve()),'target':str(target.resolve()),'route':route,'preprocess':preprocess,'exclude_unregistered':exclude_unregistered,'fast_archive':fast,'parent_length':length}}
        with self.lock:
            if any(j['status'] in ('preparing','running','paused','queued') for j in self.jobs.values()):
                raise ValueError('已有归档任务在处理，请先完成或暂停后结束该任务')
            self.jobs[jid]=job;self.events[jid]=threading.Event();self.events[jid].set();save(folder/'job.json',job)
        self._thread(self._prepare,jid)
        return self.public(job)

    def _thread(self,method,jid):
        thread=threading.Thread(target=method,args=(jid,),daemon=True,name='folder-archive')
        self.threads.append(thread);thread.start()

    def _progress(self,jid,**values):
        if self.closed.is_set():raise RuntimeError('程序正在关闭，归档任务已保存')
        if self.jobs[jid].get('cancel_requested'):raise RuntimeError('用户已结束归档任务')
        gate=self.events[jid]
        while not gate.wait(.2):
            if self.closed.is_set():raise RuntimeError('程序正在关闭，归档任务已保存')
            if self.jobs[jid].get('cancel_requested'):raise RuntimeError('用户已结束归档任务')
        labels={'scanning_sources':'扫描来源目录，建立文件清单','scanning_final':'扫描最终大档目录',
                'scanning_verify':'重新核对实际文件清单','source_index':'批量读取已入库SKU归属',
                'source_sidecars':'读取来源配套XMP目录清单','sources':'并行读取来源SKU与元数据','final_metadata':'并行读取最终大档相机身份','matching':'按快门元数据匹配最终大档',
                'label_matching':'核对并补充二星标签图','existing_targets':'核验已存在的归档目标',
                'manifest_listing':'快速核对最终文件版本','reference_listing':'快速核对现场文件版本','copying_fast':'2路流式复制与单次读回校验',
                'reference_hashing':'校验现场来源摘要','sidecar_scanning':'核对配套XMP清单','sidecar_hashing':'校验配套XMP摘要','manifest_hashing':'计算最终大档完整性摘要',
                'verifying_source':'核验来源文件完整性','preprocess_checking':'检查整理路径与来源文件',
                'validating_assignments':'核对当前SKU归属','preprocessing':'整理大小档并同步路径','copying':'复制图片与写入元数据','verifying':'核对全部目标与来源清单'}
        labels.update(sync_asset_paths='来源已整理，正在同步资产位置',sync_session_paths='来源已整理，正在同步拍摄会话',
                      sync_catalog_paths='来源已整理，正在同步资产目录索引',sync_metadata_cache='来源已整理，正在迁移元数据缓存')
        stage=values.get('stage','');done=values.get('done',0);total=values.get('total',0)
        values['message']=labels.get(stage,'正在归档')
        values.setdefault('path','')
        values.setdefault('indeterminate',not bool(total))
        values['percent']=min(100,int(done/total*100)) if total else 0
        now=time.monotonic();last_stage,last_at=self.progress_written.get(jid,('',0))
        if stage==last_stage and now-last_at<.2 and done and (not total or done<total):return
        self.progress_written[jid]=(stage,now)
        self._change(jid,**values)

    def _prepare(self,jid,_frozen=False):
        capture=getattr(self.archive,'capture_service',None)
        if capture and not _frozen:
            try:
                data=self.jobs[jid]['input']
                with capture.freeze_archive([data['source'],data['final']],jid):return self._prepare(jid,True)
            except Exception as error:self._failed(jid,error)
            return
        try:
            with self.capacity:
                job=self.jobs[jid];data=job['input'];folder=self._folder(jid)
                self._change(jid,attempt_started=time.time(),stage='scanning_sources',message='正在扫描来源目录',
                             indeterminate=True,done=0,total=0,discovered=0,directories=0,percent=0,path=data['source'])
                self.sources.guard([data['source'],data['final']])
                database=folder/'sources.sqlite3'
                self.sources.snapshot(Path(data['source']),database,direct=data['route']=='folders',allow_empty=data['route']=='loose',parent_length=data['parent_length'],progress=lambda **v:self._progress(jid,**v))
                if data['route']=='loose':
                    source,final=Path(data['source']),Path(data['final'])
                    if source==final or source in final.parents or final in source.parents:
                        raise ValueError('大小档目录不能相同或互相包含')
                    self.sources.snapshot(final,database,direct=True,allow_unassigned=True,append=True,parent_length=data['parent_length'],progress=lambda **v:self._progress(jid,**v))
                core=SkuFolderArchive(self.archive.writer,database)
                plan=core.plan(data['source'],data['final'],data['target'],preprocess=data['preprocess'],parent_length=data['parent_length'],direct=data['route']=='folders',exclude_unregistered=data.get('exclude_unregistered',True),fast=data.get('fast_archive',True),progress=lambda **v:self._progress(jid,**v))
                save(folder/'plan.json',plan)
                if job.get('cancel_requested'):raise RuntimeError('用户已结束归档任务')
                self._change(jid,status='ready',stage='ready',message='匹配已完成，请核对归档路径后确认',expected=plan['expected'],matched=len(plan['items']),
                             requires_rematch=False,deleted_count=plan['deleted_reference_count'],excluded_count=plan.get('excluded_count',0),unassigned_reference_count=plan.get('unassigned_reference_count',0),label_count=plan.get('supplemental_label_count',0),label_file_count=plan.get('label_file_count',0),xmp=plan['expected_xmp'],percent=100,done=plan['expected'],total=plan['expected'])
        except Exception as error:self._failed(jid,error)

    def _failed(self,jid,error):
        self._change(jid,status='cancelled' if self.jobs[jid].get('cancel_requested') else 'interrupted' if self.closed.is_set() else 'failed',stage='failed',error=str(error),message='归档未完成，已完成文件与恢复日志保留')
        if not self.closed.is_set() and getattr(self.archive,'capture_service',None):self.archive.capture_service.thaw_archive(jid)

    def _synchronize_paths(self,jid,plan):
        if not plan.get('preprocess_moves'):return
        marker=self._folder(jid)/'paths-synchronized.json'
        previous=json.loads(marker.read_text(encoding='utf8')) if marker.exists() else {}
        journal=plan.get('preprocess_journal')
        if journal and previous.get('done') and previous.get('journal')==journal:return
        self.sources.synchronize(plan,progress=lambda **v:self._progress(jid,**v))
        save(marker,{'done':True,'journal':journal})

    def start(self,jid):
        with self.lock:
            job=self.jobs.get(jid)
            if not job or job['status'] not in ('ready','failed','interrupted'):raise ValueError('此任务当前不能开始')
            if job.get('requires_rematch'):return self.resume(jid)
            if not (self._folder(jid)/'plan.json').exists():raise ValueError('匹配未完成，请重新建立归档任务')
            self.events[jid]=threading.Event();self.events[jid].set()
            self._change(jid,status='queued',error='',message='准备执行归档')
        self._thread(self._execute,jid);return self.public(job)

    def _execute(self,jid):
        try:
            with self.capacity:
                folder=self._folder(jid);data=self.jobs[jid]['input'];database=folder/'sources.sqlite3'
                self._change(jid,status='running')
                plan=json.loads((folder/'plan.json').read_text(encoding='utf8'))
                core=SkuFolderArchive(self.archive.writer,database)
                from sku_archive_preprocess import preprocessing_complete
                # 在文件与数据库的中断窗口后，仅接受同一不可变输入计划。
                if self.jobs[jid].get('stage') in ('failed','interrupted'):
                    from folder_archive_commit import recover
                    commit_dir=(Path(data['target']) if database.is_relative_to(Path(data['source'])) else database.parent)/'.归档文件提交日志'
                    recover(commit_dir,Path(data['target']),fast=plan.get('fast',False))
                    if preprocessing_complete(plan,database):
                        self._synchronize_paths(jid,plan)
                    else:
                        refreshed=core.plan(data['source'],data['final'],data['target'],preprocess=data['preprocess'],include_label_images=bool(plan.get('supplemental_label_count')),parent_length=data['parent_length'],direct=data['route']=='folders',exclude_unregistered=data.get('exclude_unregistered',True),fast=plan.get('fast',False))
                        signature=lambda p:sorted((r.get('original_source',r['source']),r.get('input_signature') if p.get('fast') else r['input_sha256'],r['sku']) for r in p['items'])
                        if signature(refreshed)!=signature(plan):raise ValueError('中断后来源清单已改变，请重新匹配确认')
                        plan=refreshed
                        with closing(sqlite3.connect(database)) as c:
                            if c.execute("SELECT 1 FROM sqlite_master WHERE name='archive_source_paths'").fetchone():
                                replay=[{'before':before,'after':after} for after,before in c.execute('SELECT current_path,original_path FROM archive_source_paths') if before!=after]
                                self.sources.synchronize({'preprocess_moves':replay},progress=lambda **v:self._progress(jid,**v))
                def changed(actual):
                    save(folder/'plan.json',actual)
                    self._synchronize_paths(jid,actual)
                self.sources.guard([data['source'],data['final']])
                self.sources.validate_assignments(database,plan=plan,progress=lambda **v:self._progress(jid,**v))
                result=core.execute(plan,progress=lambda **v:self._progress(jid,**v),on_plan_changed=changed,preprocess_context=self.sources.preprocess_guard)
                self.sources.validate_assignments(database,plan=result,progress=lambda **v:self._progress(jid,**v))
                if self.jobs[jid].get('cancel_requested'):raise RuntimeError('用户已结束归档任务')
                result['real_validation']=False
                save(folder/'result.json',result)
                self._change(jid,status='completed',stage='complete',message='归档完成，全部文件已核验',percent=100,done=result['verified'],total=result['expected'])
                if getattr(self.archive,'capture_service',None):self.archive.capture_service.thaw_archive(jid)
        except Exception as error:self._failed(jid,error)

    def pause(self,jid):
        with self.lock:
            job=self.jobs.get(jid)
            if not job or job['status']!='running':raise ValueError('只有正在归档的任务可以暂停')
            self.events[jid].clear();self._change(jid,status='paused',message='将在当前文件安全收尾后暂停')
            return self.public(job)

    def resume(self,jid):
        with self.lock:
            job=self.jobs.get(jid)
            if job and job['status']=='paused':
                self._change(jid,status='running',message='正在继续归档');self.events[jid].set();return self.public(job)
            if job and (job['status'] in ('failed','interrupted') or job['status']=='ready' and job.get('requires_rematch')) and (job.get('requires_rematch') or not (self._folder(jid)/'plan.json').exists()):
                if job.get('requires_rematch'):
                    previous=self._folder(jid)/'plan-before-scope-rules.json'
                    if not previous.exists():previous.write_bytes((self._folder(jid)/'plan.json').read_bytes())
                self.events[jid]=threading.Event();self.events[jid].set()
                self._change(jid,status='preparing',stage='sources',error='',message='正在重新读取并匹配来源')
                self._thread(self._prepare,jid);return self.public(job)
        return self.start(jid)

    def report(self,jid):
        job=self.jobs.get(jid)
        if not job or job['status']!='completed':raise ValueError('归档尚未完成，暂不能导出完成报告')
        result=json.loads((self._folder(jid)/'result.json').read_text(encoding='utf8'))
        return build_report(result)

    def export(self,jid):
        from metadata_writer import sha
        content,name=self.report(jid)
        target=Path(self.jobs[jid]['input']['target'])/name
        exports=self._folder(jid)/'exports.json'
        known=json.loads(exports.read_text(encoding='utf8')) if exports.exists() else {}
        if target.exists():
            if known.get(str(target))!=sha(target):raise ValueError('同分钟的同名报表已存在且不属于此任务，请稍后导出，未覆盖原文件')
        else:
            with target.open('xb') as stream:stream.write(content)
            known[str(target)]=sha(target);save(exports,known)
        return {'path':str(target),'filename':name}

    def cancel(self,jid):
        with self.lock:
            job=self.jobs.get(jid)
            if not job or job['status']=='completed':raise ValueError('此任务不能结束')
            running=job['status'] in ('running','preparing')
            self._change(jid,cancel_requested=True,status='cancelled',message='正在安全结束，已归档文件保留')
            if jid in self.events:self.events[jid].set()
            if not running and getattr(self.archive,'capture_service',None):self.archive.capture_service.thaw_archive(jid)
            return self.public(job)

    def thumbnail(self,jid,item_id):
        folder=self._folder(jid)
        file=folder/('result.json' if (folder/'result.json').exists() else 'plan.json')
        report=json.loads(file.read_text(encoding='utf8'))
        item=next((r for r in report['items'] if str(r['id'])==str(item_id)),None)
        if not item:raise ValueError('图片不存在')
        from sku_folder_archive import make_thumbnail
        from metadata_writer import sha
        path=Path(item['source'])
        if report.get('fast'):
            from folder_archive_metadata import signature
            if signature(path)!=item['input_signature']:raise ValueError('图片已变化，请重新匹配')
        elif sha(path)!=item['input_sha256']:raise ValueError('图片已变化，请重新匹配')
        target=folder/('preview-'+str(item['id'])+'.jpg')
        make_thumbnail(self.archive.writer,path,target)
        return target.read_bytes()

    def close(self):
        self.closed.set()
        for event in self.events.values():event.set()
        for thread in self.threads:thread.join(timeout=2)
