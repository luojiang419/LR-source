"""可独立启用的全图 OCR；有文档时沿用匹配，无文档时使用内部现场记录。"""
import json
import uuid

from sku_match import normalize
from sku_progress import capture_time


class FullAutoOCR:
    def full_auto_state(self,session_id=None):
        config=self.full_auto;p=self.project();reason=''
        imported=bool(p and not p['metadata'].get('automatic_day'))
        active=self.store.session();sid=session_id or (p['session_id'] if p else None) or (active['id'] if active else None)
        scope=(p['scope'] if p else '') or '现场记录'
        if not sid or not self.store.session(sid) or self.store.is_removed(sid):reason='请先打开可用的照片目录'
        elif imported and not (self.root/p['id']/'source.xlsx').is_file():reason='输入文档已缺失，请重新导入'
        elif imported and (not p['scope'] or not p['session_id']):reason='请选择文档工作表并绑定当前照片目录'
        elif imported and p['session_id']!=sid:reason='请先将拍摄文档绑定当前照片目录'
        context={'project':p['id'] if p else None,'session':sid,'scope':scope}
        enabled=bool(config.get('enabled') and not reason and p and p['enabled'] and all(config.get(k)==v for k,v in context.items()))
        counts={}
        if config.get('project'):
            with self.connect() as c:
                counts={r['status']:r['n'] for r in c.execute("SELECT status,count(*) AS n FROM jobs WHERE project=? AND session_id=? AND scope=? AND trigger='full_auto' GROUP BY status",(config['project'],config.get('session'),config.get('scope')))}
        provider=self.providers.selected('ocr') or self.providers.purpose('sku_label') or self.providers.selected('multimodal')
        model_message='' if provider else '未选用识别模型，请在设置 → AI 助手配置中开启“使用内置模型”或选用 OCR 模型'
        return {'enabled':enabled,'can_enable':not reason,'reason':reason,'document':p['name'] if imported else '无文档 · 现场记录', 'project_id':p['id'] if p else None,
                'scope':scope, 'session_id':sid,'counts':counts,'paused':self.tasks_paused,'without_document':not imported,
                'model_ready':bool(provider),'model_message':model_message,
                'requires_sku_choice':self.require_sku_choice,
                'message':reason or ('逐张保存 OCR 资料，公司前后缀规则唯一命中自动应用，其他款号保留选择' if not imported or self.require_sku_choice else '逐张识别并自动入库；近似候选冲突留待复核')}

    def set_full_auto(self,enabled,session_id=None,project_id=None,scope=None):
        if type(enabled) is not bool:raise ValueError('全自动 OCR 开关必须为布尔值')
        with self.lock:
            state=self.full_auto_state(session_id)
            if enabled and not state['can_enable']:raise ValueError(state['reason'])
            p=self.project()
            if project_id is not None and (not p or p['id']!=project_id):raise ValueError('拍摄文档已变化，请重新开启全自动 OCR')
            if scope is not None and p and (p['scope'] or '现场记录')!=scope:raise ValueError('拍摄工作表已变化，请重新开启全自动 OCR')
            if enabled and (not p or p['metadata'].get('automatic_day') and p['session_id']!=state['session_id']):p=self._automatic_project(state['session_id'],start_stream=False)
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

    def _full_starred(self,context=None):
        context=context or self._full_context()
        if not context:return []
        sid=context['session']
        return [r['id'] for r in self.store.records(sid) if self._rating(sid,r)==2]

    def _prioritize_full_label(self,sid,cid):
        context=self._full_context()
        if not context or context['session']!=sid:return False
        self.full_scan_at=0
        record=next((r for r in self.store.records(sid) if r['id']==cid),None)
        if not record:return False
        with self.lock,self.connect() as c:
            prior=[dict(r) for r in c.execute("SELECT * FROM jobs WHERE project=? AND session_id=? AND scope=? AND capture_id=? AND status NOT IN ('cancelled','ignored')",(context['project'],sid,context['scope'],cid))]
            current=next((job for job in prior if self._valid(job)),None)
            if current:
                c.execute("UPDATE jobs SET status='queued',result='{}',targets='[]',run_token='',due=?,priority=?,updated=?,reason='二星标签并发识别' WHERE id=? AND status!='running'",(self.clock(),self.clock(),self.clock(),current['id']))
            else:self._insert_full_job(c,context,record,priority=self.clock())
            c.execute('DELETE FROM ocr_resets WHERE session_id=? AND capture_id=?',(sid,cid))
        self._wake_label()
        return True

    def _insert_full_job(self,c,context,record,*,priority=0):
        stamp,time_source=capture_time(record)
        c.execute('''INSERT INTO jobs(id,project,session_id,capture_id,fingerprint,scope,status,due,created,result,targets,reason,captured_at,capture_time_source,trigger,priority)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,session_id,capture_id,fingerprint,scope) DO UPDATE SET status='queued',trigger='full_auto',result='{}',targets='[]',run_token='',reason=excluded.reason,due=excluded.due,priority=excluded.priority,updated=excluded.created''',
            (uuid.uuid4().hex,context['project'],context['session'],record['id'],record['fingerprint'],context['scope'],'queued',self.clock(),self.clock(),'{}','[]','二星标签并发识别' if priority else '全自动 OCR：等待逐张扫描',stamp,time_source,'full_auto',priority))

    def _reconcile_full_context(self):
        if self.full_auto.get('enabled') and not self.full_auto_state()['enabled']:self.set_full_auto(False)

    def _queue_full_auto(self):
        self._reconcile_full_context();context=self._full_context()
        if not context or self.tasks_paused or self.clock()<self.full_scan_at:return
        self.full_scan_at=self.clock()+2
        sid=context['session'];records=self.store.records(sid);overrides=self.store.session(sid)['state'].get('overrides',{})
        with self.lock,self.connect() as c:
            if context!=self._full_context():return
            prior={}
            for row in c.execute("SELECT * FROM jobs WHERE project=? AND session_id=? AND scope=? AND status!='cancelled'",(context['project'],sid,context['scope'])):
                prior.setdefault(row['capture_id'],[]).append(dict(row))
            versions={(r['capture'],r['before']):r['after'] for r in c.execute('SELECT * FROM metadata_versions WHERE session=?',(sid,))}
            resets={r[0] for r in c.execute('SELECT capture_id FROM ocr_resets WHERE session_id=?',(sid,))}
            for record in records:
                if record['id'] in resets and overrides.get(record['id'],record.get('existingRating',0))!=2:continue
                processed=False
                for job in prior.get(record['id'],[]):
                    fingerprint=job['fingerprint'];seen=set()
                    while fingerprint!=record['fingerprint'] and fingerprint not in seen:
                        seen.add(fingerprint);fingerprint=versions.get((record['id'],fingerprint))
                        if fingerprint is None:break
                    if fingerprint==record['fingerprint']:processed=True;break
                if processed:continue
                self._insert_full_job(c,context,record,priority=self.clock() if overrides.get(record['id'],record.get('existingRating',0))==2 else 0)

    def _full_job_current(self,job):
        context=self._full_context()
        return bool(context and (job['project'],job['session_id'],job['scope'])==(context['project'],context['session'],context['scope']))

    def _full_commit(self,job,status,result,targets,reason):
        if status not in ('matched','confirmed'):return status,result,targets,reason
        if status!='confirmed' and not self._full_job_current(job):return 'cancelled',result,[],'自动模式或文档已变化，未入库'
        if not self._valid(job):return 'cancelled',result,[],'照片已变化，未入库'
        if not self.archive:return 'review',result,[],'资产入库服务未启动，请重试'
        if result.get('starred_label'):
            record=self._valid(job)
            if not record or self._rating(job['session_id'],record)!=2:return 'cancelled',result,[],'二星标记已变化，未新增入库'
        managed=self.project(job['project'])['metadata'].get('managed_sheet','') if result.get('starred_label') else ''
        row=next((r for r in self.rows(job['project']) if r['id'] in targets and r['sheet'] in (job['scope'],managed)),None)
        if not row:return 'review',result,[],'文档款号已变化，请复核'
        if result.get('starred_label'):
            return status,result,targets,reason+'；款末标签已确认，按完整区间入库'
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
            managed=self.project(job['project'])['metadata'].get('managed_sheet','') if result.get('starred_label') else ''
            if row_id not in {r['id'] for r in self.rows(job['project']) if r['sheet'] in (job['scope'],managed)}:raise ValueError('只能确认原工作表或本次二星标签的临时款号')
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
