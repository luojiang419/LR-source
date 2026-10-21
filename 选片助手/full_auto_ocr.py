"""仅绑定用户导入计划的全图 OCR；复用任务、匹配、备案与拍摄记录。"""
import json
import uuid

from sku_match import normalize
from sku_progress import capture_time


class FullAutoOCR:
    def full_auto_state(self):
        config=self.full_auto;p=self.project();reason=''
        if not p or p['metadata'].get('automatic_day'):reason='请先导入包含 SKU 款号的 Excel 文档'
        elif not (self.root/p['id']/'source.xlsx').is_file():reason='输入文档已缺失，请重新导入'
        elif not p['scope'] or not p['session_id']:reason='请选择文档工作表并绑定当前照片目录'
        elif not self.store.session(p['session_id']) or self.store.is_removed(p['session_id']):reason='绑定的照片目录已不可用，请重新绑定'
        elif not self.rows(p['id'],p['scope']):reason='当前工作表没有可匹配的 SKU 款号'
        context={'project':p['id'],'session':p['session_id'],'scope':p['scope']} if p else {}
        enabled=bool(config.get('enabled') and not reason and p['enabled'] and all(config.get(k)==v for k,v in context.items()))
        counts={}
        if config.get('project'):
            with self.connect() as c:
                counts={r['status']:r['n'] for r in c.execute("SELECT status,count(*) AS n FROM jobs WHERE project=? AND session_id=? AND scope=? AND trigger='full_auto' GROUP BY status",(config['project'],config.get('session'),config.get('scope')))}
        return {'enabled':enabled,'can_enable':not reason,'reason':reason,'document':p['name'] if p else '', 'project_id':p['id'] if p else None,
                'scope':p['scope'] if p else '', 'session_id':p['session_id'] if p else None,'counts':counts,'paused':self.tasks_paused,
                'message':reason or ('逐张识别并自动入库；近似候选冲突留待复核' if enabled else '开启后扫描绑定目录全部已有和后续图片')}

    def set_full_auto(self,enabled,session_id=None,project_id=None,scope=None):
        if type(enabled) is not bool:raise ValueError('全自动 OCR 开关必须为布尔值')
        with self.lock:
            state=self.full_auto_state()
            if enabled and not state['can_enable']:raise ValueError(state['reason'])
            p=self.project()
            if project_id is not None and (not p or p['id']!=project_id):raise ValueError('拍摄文档已变化，请重新开启全自动 OCR')
            if scope is not None and (not p or p['scope']!=scope):raise ValueError('拍摄工作表已变化，请重新开启全自动 OCR')
            if enabled and session_id and p['session_id']!=session_id:raise ValueError('请先将拍摄文档绑定当前照片目录')
            config={'enabled':enabled}
            if enabled:config.update(project=p['id'],session=p['session_id'],scope=p['scope'])
            with self.connect() as c:
                c.execute("INSERT OR REPLACE INTO settings VALUES ('full_auto_ocr',?)",(json.dumps(config),))
                if enabled:c.execute('UPDATE projects SET enabled=1 WHERE id=?',(p['id'],))
                if not enabled:c.execute("UPDATE jobs SET status='cancelled',reason='全自动 OCR 已关闭，未入库' WHERE trigger='full_auto' AND status IN ('queued','waiting_model','running')")
            self.full_auto=config;self.full_scan_at=0
            if enabled:self.configure_tasks(paused=False)
            return self.full_auto_state()

    def _full_context(self):
        return self.full_auto if self.full_auto.get('enabled') and self.full_auto_state()['enabled'] else None

    def _reconcile_full_context(self):
        if self.full_auto.get('enabled') and not self.full_auto_state()['enabled']:self.set_full_auto(False)

    def _queue_full_auto(self):
        self._reconcile_full_context();context=self._full_context()
        if not context or self.tasks_paused or self.clock()<self.full_scan_at:return
        self.full_scan_at=self.clock()+2
        sid=context['session'];records=self.store.records(sid)
        with self.lock,self.connect() as c:
            if context!=self._full_context():return
            prior={}
            for row in c.execute("SELECT * FROM jobs WHERE project=? AND session_id=? AND scope=? AND status!='cancelled'",(context['project'],sid,context['scope'])):
                prior.setdefault(row['capture_id'],[]).append(dict(row))
            versions={(r['capture'],r['before']):r['after'] for r in c.execute('SELECT * FROM metadata_versions WHERE session=?',(sid,))}
            for record in records:
                processed=False
                for job in prior.get(record['id'],[]):
                    fingerprint=job['fingerprint'];seen=set()
                    while fingerprint!=record['fingerprint'] and fingerprint not in seen:
                        seen.add(fingerprint);fingerprint=versions.get((record['id'],fingerprint))
                        if fingerprint is None:break
                    if fingerprint==record['fingerprint']:processed=True;break
                if processed:continue
                stamp,time_source=capture_time(record)
                c.execute('''INSERT INTO jobs(id,project,session_id,capture_id,fingerprint,scope,status,due,created,result,targets,reason,captured_at,capture_time_source,trigger)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,session_id,capture_id,fingerprint,scope) DO UPDATE SET status='queued',trigger='full_auto',reason=excluded.reason''',
                    (uuid.uuid4().hex,context['project'],sid,record['id'],record['fingerprint'],context['scope'],'queued',self.clock(),self.clock(),'{}','[]','全自动 OCR：等待逐张扫描',stamp,time_source,'full_auto'))

    def _full_job_current(self,job):
        context=self._full_context()
        return bool(context and (job['project'],job['session_id'],job['scope'])==(context['project'],context['session'],context['scope']))

    def _full_commit(self,job,status,result,targets,reason):
        if status not in ('matched','confirmed'):return status,result,targets,reason
        if status!='confirmed' and not self._full_job_current(job):return 'cancelled',result,[],'自动模式或文档已变化，未入库'
        if not self._valid(job):return 'cancelled',result,[],'照片已变化，未入库'
        if not self.archive:return 'review',result,[],'资产入库服务未启动，请重试'
        row=next((r for r in self.rows(job['project'],job['scope']) if r['id'] in targets),None)
        if not row:return 'review',result,[],'文档款号已变化，请复核'
        preview=self.archive.preview(job['session_id'],[job['capture_id']],row['sku'],job['scope'],job['project'],source='full_auto')
        before=preview['targets'][0]['before']
        existing=normalize(before.get('sku',''))==row['normalized'] and before.get('scope')==job['scope'] and before.get('project') in ('',job['project'])
        if existing:
            previous=json.loads(job.get('result') or '{}');result['operation']=previous.get('operation','');result['already_in_library']=True
            return status,result,targets,'SKU 已在库，已自动更新拍摄记录'
        if any(not t['apply'] or t['before'].get('project') not in ('',job['project']) for t in preview['targets']):
            return 'review',result,[],'已有人工或其他归属，已保留，请复核'
        operation=self.archive.apply(preview['preview_id'])
        result['operation']=operation['operation']['id']
        return status,result,targets,reason+'；已自动入库，文件写入按队列完成'

    def review_full_auto(self,job,action,row_id=None):
        result=json.loads(job['result'] or '{}')
        if action=='confirm':
            if not self._valid(job):raise ValueError('照片已变化，请重新扫描')
            if row_id not in {r['id'] for r in self.rows(job['project'],job['scope'])}:raise ValueError('只能确认原文档工作表的款号')
            self._finish(job,'confirmed',result,[row_id],'人工确认，自动入库')
        elif action=='ignore':
            if result.get('operation') and self.archive:self.archive.revert(result['operation'])
            self._finish(job,'ignored',result,[],'已撤销本次全自动入库')
        elif action=='retry':
            if not self._full_job_current(job):raise ValueError('请先启用原文档的全自动 OCR 模式')
            if not self._valid(job):raise ValueError('照片已变化，请重新扫描')
            self._finish(job,'queued',result,[],'等待全自动 OCR 重试')
        else:raise ValueError('无效复核操作')
        return self.state()
