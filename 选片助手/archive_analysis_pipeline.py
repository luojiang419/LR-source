"""归档解析的阶段、持久断点和手动运行控制；媒体仍由原归档服务管理。"""
import json
import threading
import time

from retrieval_schema import digest, dumps
from vision_scheduler import VisionScheduler


class ArchiveAnalysisPipeline:
    def _init_pipeline(self):
        self.scheduler=getattr(self.sku,'scheduler',None) or VisionScheduler()
        with self.catalog.connect() as c:
            row=c.execute("SELECT value FROM intelligence_settings WHERE key='scan'").fetchone()
        scan=json.loads(row[0]) if row else {}
        self.scan_key=scan.get('config','');self.seed_cursor=scan.get('cursor',0);self.seed_done=scan.get('done',False)

    def _running(self):return self.config['enabled'] or self.config.get('manual_active',False)

    def _preview_blocker(self,refresh=False):
        visits=getattr(self.catalog,'visits',None)
        if visits:
            state=visits.state()
            return '请先打开要解析的文件夹' if not state['root'] else '正在缓存当前文件夹缩略图' if state['running'] and state.get('thumbnail_cache_enabled',True) else ''
        warmer=getattr(self.catalog,'preview_warmer',None)
        if not warmer:return ''
        state=warmer.refresh_phase() if refresh else warmer.phase
        if state.get('checking'):return '正在发现素材并核对缩略图缓存，请完成后手动启动解析'
        if state.get('pending') or warmer.active:return '请先完成缩略图与清晰预览解码，再手动启动模型解析'
        if state.get('failed'):return '缩略图存在失败项，请先重试或移除无法读取的素材'
        if not state.get('ready'):return '正在核对缩略图缓存，请稍后重试'
        return ''

    def _ocr_enabled(self):
        # 旧配置继续跟随现场 OCR；明确设置后归档可独立运行，无需绑定拍摄目录。
        enabled=self.config.get('ocr_enabled')
        return bool(self.sku and getattr(self.sku,'full_auto',{}).get('enabled')) if enabled is None else enabled

    def _ocr_provider(self):
        provider=self.providers.selected('ocr')
        if not provider:raise ValueError('全自动 OCR 已开启，请先选用 OCR 模型')
        self.providers.check_inference(provider)
        return provider

    @staticmethod
    def _ocr_key(provider):
        return digest([provider.get(k) for k in ('id','model','protocol','url','image_size','max_tokens','json_mode')])

    def _save_scan(self,c):
        c.execute("INSERT OR REPLACE INTO intelligence_settings VALUES('scan',?)",
                  (dumps({'config':self.scan_key,'cursor':self.seed_cursor,'done':self.seed_done}),))

    def _dirty_clause(self):
        return '',[]

    def _catalog_scanning(self):
        # 只等待本进程真实在途扫描；崩溃留下的 scanning 字样不能卡住解析恢复。
        return bool(getattr(self.catalog,'scanning_roots',lambda:())())

    def _ocr_backlog(self,c,key,ocr_key):
        if self.scan_key!=key or not self.seed_done:return True
        if self._catalog_scanning():return True
        clause,args=self._dirty_clause()
        if c.execute('SELECT 1 FROM intelligence_dirty d JOIN items i ON i.id=d.item WHERE 1=1'+clause+' LIMIT 1',args).fetchone():return True
        return bool(c.execute('''SELECT 1 FROM intelligence_jobs j LEFT JOIN intelligence_ocr o
            ON o.item=j.item AND o.fingerprint=j.fingerprint AND o.config=?
            WHERE j.config=? AND j.status IN ('queued','running') AND o.item IS NULL LIMIT 1''',(ocr_key,key)).fetchone())

    def _sync_ordering(self):
        enabled=not self.closed.is_set() and self._running() and self._ocr_enabled()
        pending=False
        if enabled and not self.config['paused']:
            try:
                with self.catalog.connect() as c:pending=self._ocr_backlog(c,self._config_key(),self._ocr_key(self._ocr_provider()))
            except (ValueError,KeyError):pending=True
        self.scheduler.order_archive(enabled,pending)

    def _grow_workers(self):
        if not self._running() or self.config['paused']:return
        providers=[self._provider()]
        if self._ocr_enabled():providers.append(self._ocr_provider())
        desired=max(2,max(self.providers.concurrency(p) for p in providers))
        with self.lock:
            while len(self.threads)<desired and not self.closed.is_set():
                worker=threading.Thread(target=self._run,daemon=True,name='catalog-vlm-'+str(len(self.threads)))
                self.threads.append(worker);worker.start()

    def _finish_manual_if_idle(self):
        if self.config['enabled'] or not self.config.get('manual_active') or not self.seed_done:return
        with self.lock,self.catalog.connect() as c:
            if self._catalog_scanning():return
            clause,args=self._dirty_clause()
            if c.execute('SELECT 1 FROM intelligence_dirty d JOIN items i ON i.id=d.item WHERE 1=1'+clause+' LIMIT 1',args).fetchone():return
            if c.execute("SELECT 1 FROM intelligence_jobs WHERE config=? AND status IN ('queued','running') LIMIT 1",(self._config_key(),)).fetchone():return
            self.config={**self.config,'manual_active':False}
            c.execute("INSERT OR REPLACE INTO intelligence_settings VALUES('config',?)",(dumps(self.config),))
        self._sync_ordering()

    def pipeline_state(self):
        preview_blocker=self._preview_blocker()
        if preview_blocker and not self._running():
            # 缓存阶段尚未启动模型，不为显示停止状态遍历几十万模型任务。
            # 未核对的数量明确为未知，不能伪装成零或丢失已保存结果。
            vision=self.providers.purpose('asset_analysis') or self.providers.selected('multimodal') or {}
            ocr=self.providers.selected('ocr') or {}
            return {'enabled':False,'manual_active':False,'paused':self.config['paused'],'running':False,
                'ocr_enabled':self._ocr_enabled(),'phase':'decoding' if getattr(self.catalog,'preview_warmer',None) else 'idle','discovering':False,'counts_ready':False,
                'total':None,'ocr_completed':None,'completed':None,'pending':None,'active':0,'failed':None,'cancelled':None,
                'models':{'vision':vision.get('name',''),'ocr':ocr.get('name','')},'error':'',
                'scheduler':self.scheduler.state(),'scope':'全部已添加归档（图片与视频）','manual_stages':bool(getattr(self.catalog,'preview_warmer',None)),
                'run_stage':self.config.get('run_stage'),'preview_blocker':preview_blocker,'can_start':False,
                'decode':dict(self.catalog.preview_warmer.phase) if getattr(self.catalog,'preview_warmer',None) else {}}
        ocr_enabled=self._ocr_enabled();models={};error='';key='';ocr_key=''
        try:
            provider=self._provider();models['vision']=provider['name'];key=self._config_key()
            if self.config.get('run_stage')=='ocr':
                selected=self.providers.purpose('asset_analysis') or self.providers.selected('multimodal')
                models['vision']=selected['name'] if selected else ''
            if ocr_enabled:
                provider=self._ocr_provider();models['ocr']=provider['name'];ocr_key=self._ocr_key(provider)
            elif self.config.get('run_stage')=='vision':
                provider=self.providers.selected('ocr')
                if provider:models['ocr']=provider['name'];ocr_key=self._ocr_key(provider)
        except (ValueError,KeyError) as exc:error=str(exc);key=self.scan_key
        with self.catalog.connect() as c:
            base=' FROM intelligence_jobs j JOIN items i ON i.id=j.item AND i.fingerprint=j.fingerprint JOIN roots r ON r.id=i.root WHERE r.removed=0 AND i.online=1 AND j.config=?'
            counts={row[0]:row[1] for row in c.execute('SELECT j.status,count(*)'+base+' GROUP BY j.status',(key,))}
            ocr_done=c.execute('SELECT count(*)'+base+' AND EXISTS(SELECT 1 FROM intelligence_ocr o WHERE o.item=j.item AND o.fingerprint=j.fingerprint AND o.config=?)',(key,ocr_key)).fetchone()[0] if ocr_key else 0
            backlog=bool(ocr_enabled and ocr_key and self._ocr_backlog(c,key,ocr_key))
            scanning=self._catalog_scanning()
        total=sum(counts.values());pending=counts.get('queued',0)+counts.get('running',0)
        discovering=self.scan_key!=key or not self.seed_done or scanning
        preview_blocker=self._preview_blocker()
        if preview_blocker:phase='decoding'
        elif self.config['paused']:phase='paused'
        elif error:phase='waiting_model'
        elif not self._running() and pending:phase='stopped'
        elif not self._running() or not discovering and not pending:phase='attention' if counts.get('failed') or counts.get('cancelled') else 'completed' if total else 'idle'
        elif discovering and not pending:phase='discovering'
        elif ocr_enabled and backlog:phase='ocr'
        else:phase='vision'
        return {'enabled':self.config['enabled'],'manual_active':self.config.get('manual_active',False),
            'paused':self.config['paused'],'running':self._running(),'ocr_enabled':ocr_enabled,'phase':phase,
            'discovering':discovering,'total':total,'ocr_completed':ocr_done,'completed':0 if self.config.get('run_stage')=='ocr' else counts.get('completed',0),
            'pending':pending,'active':counts.get('running',0),'failed':counts.get('failed',0),'cancelled':counts.get('cancelled',0),
            'models':models,'error':error or self.last_error,'scheduler':self.scheduler.state(),
            'scope':'全部已添加归档（图片与视频）','manual_stages':bool(getattr(self.catalog,'preview_warmer',None)),
            'run_stage':self.config.get('run_stage'),'preview_blocker':preview_blocker,'can_start':not preview_blocker,
            'decode':dict(self.catalog.preview_warmer.phase) if getattr(self.catalog,'preview_warmer',None) else {}}

    def _store_ocr(self,c,job,provider,frames):
        from catalog_ocr import save_ocr
        row=dict(c.execute('SELECT * FROM items WHERE id=?',(job['item'],)).fetchone())
        save_ocr(c,row,self._ocr_key(provider),provider['model'],frames,self.sku.sku_rules())
        c.execute("UPDATE intelligence_jobs SET status='queued',reason='',attempt=0,due=?,updated=? WHERE id=?",
                  (time.time(),time.time(),job['id']))
        if self.config.get('run_stage')=='ocr':
            c.execute("UPDATE intelligence_jobs SET status='completed' WHERE id=?",(job['id'],))

    def _load_ocr(self,c,job,provider):
        row=c.execute('SELECT frames FROM intelligence_ocr WHERE item=? AND fingerprint=? AND config=?',
                      (job['item'],job['fingerprint'],self._ocr_key(provider))).fetchone()
        return json.loads(row[0]) if row else None
