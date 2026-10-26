"""最新二星的独立 API 调度；网络请求并发，持久任务原子领取。"""
import threading
import time
import uuid


class ConcurrentLabels:
    def _init_label_workers(self):
        self.label_wake=threading.Event();self.label_workers=set();self.label_workers_lock=threading.Lock()
        self.label_thread=threading.Thread(target=self._run_labels,name='sku-latest-labels',daemon=True)

    def _wake_label(self):
        self.wake.set();self.label_wake.set()

    def _label_providers(self,job=None):
        selected=[p for p in (self.providers.selected('ocr'),self.providers.purpose('sku_label') or self.providers.selected('multimodal')) if p]
        return selected[:1] if job and job.get('trigger')=='full_auto' else selected

    def _parallel_labels_enabled(self):
        return any(p.get('protocol')!='local' for p in self._label_providers())

    def _parallel_reads_enabled(self):
        selected=self._label_providers()
        return bool(selected and selected[0].get('protocol')!='local')

    def _claim_parse(self,jid):
        if self.closed.is_set() or self.tasks_paused:return None
        token=uuid.uuid4().hex
        with self.connect() as c:
            row=c.execute("UPDATE parse_jobs SET status='running',reason='正在读取图片文字',updated=?,run_token=? WHERE id=? AND status='queued' RETURNING *",(self.clock(),token,jid)).fetchone()
        return dict(row,_kind='read') if row else None

    def _claim_label(self,jid):
        if self.closed.is_set() or self.tasks_paused:return None
        token=uuid.uuid4().hex
        with self.connect() as c:
            row=c.execute("UPDATE jobs SET status='running',reason='正在识别标签',updated=?,run_token=? WHERE id=? AND status IN ('queued','waiting_model') AND due<=? RETURNING *",(self.clock(),token,jid,self.clock())).fetchone()
        return dict(row,_run_token=token) if row else None

    def _label_current(self,job,*,confirmed=False):
        with self.connect() as c:row=c.execute('SELECT status,run_token FROM jobs WHERE id=?',(job['id'],)).fetchone()
        statuses=('running','confirmed') if confirmed else ('running',)
        return bool(row and row['status'] in statuses and row['run_token']==job['_run_token'])

    def _release_label(self,job,reason='等待恢复解析'):
        with self.connect() as c:
            c.execute("UPDATE jobs SET status='queued',reason=? WHERE id=? AND status='running' AND run_token=?",(reason,job['id'],job['_run_token']))

    def _dispatch_labels(self):
        if self.closed.is_set() or self.tasks_paused or not self._parallel_labels_enabled():return
        capacity=max(self.providers.concurrency(p) for p in self._label_providers() if p.get('protocol')!='local')
        with self.label_workers_lock:capacity-=len(self.label_workers)
        if capacity<=0:return
        context=self._full_context()
        with self.connect() as c:
            eligible="status IN ('queued','waiting_model') AND due<=? AND (trigger='manual' OR (trigger='full_auto' AND ?) OR (trigger NOT IN ('manual','full_auto') AND ?))"
            args=(self.clock(),bool(context),self.task_mode=='auto')
            rows=[dict(r,_kind='label') for r in c.execute('SELECT id FROM jobs WHERE priority>0 AND '+eligible+' ORDER BY priority DESC,created DESC LIMIT ?',(*args,capacity))]
            if self._parallel_reads_enabled():
                rows += [dict(r,_kind='read') for r in c.execute("SELECT id FROM parse_jobs WHERE status='queued' ORDER BY created LIMIT ?",(capacity,))]
            rows += [dict(r,_kind='label') for r in c.execute('SELECT id FROM jobs WHERE priority<=0 AND '+eligible+" AND (trigger!='full_auto' OR ?) ORDER BY due,created LIMIT ?",(*args,self._parallel_reads_enabled(),capacity))]
        for row in rows[:capacity]:
            job=self._claim_parse(row['id']) if row['_kind']=='read' else self._claim_label(row['id'])
            if not job:continue
            worker=threading.Thread(target=self._run_parallel_label,args=(job,),name='sku-label-'+job['id'][:8],daemon=True)
            with self.label_workers_lock:self.label_workers.add(worker)
            try:worker.start()
            except Exception:
                with self.label_workers_lock:self.label_workers.discard(worker)
                self._release_label(job);raise

    def _run_parallel_label(self,job):
        try:
            if job.get('_kind')=='read':
                try:self._parse_job(job['id'],job['session_id'],job['capture_id'],job['run_token'])
                except Exception:pass  # 解析失败已保存到当前代次的任务状态。
            else:self._process_label(job,parallel=True)
        finally:
            with self.label_workers_lock:self.label_workers.discard(threading.current_thread())
            self._wake_label()

    def _run_labels(self):
        while not self.closed.is_set():
            self.label_wake.clear()
            try:self._dispatch_labels()
            except Exception:pass  # 任务保留在持久队列；失败不会终止独立调度。
            self.label_wake.wait(.2)

    def _close_label_workers(self):
        self.label_wake.set()
        deadline=time.monotonic()+2
        if self.label_thread.is_alive():self.label_thread.join(timeout=2)
        with self.label_workers_lock:workers=list(self.label_workers)
        for worker in workers:
            if worker is not threading.current_thread():worker.join(timeout=max(0,deadline-time.monotonic()))
