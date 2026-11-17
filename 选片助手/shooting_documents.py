"""现场文档、冻结拍摄类型与可恢复的历史任务管理。"""
from datetime import datetime, timedelta, timezone
import json
import re
import uuid
import threading

SHANGHAI=timezone(timedelta(hours=8))


def managed_sheets(metadata):
    return list(dict.fromkeys([*metadata.get('managed_sheets',[]),*([metadata['managed_sheet']] if metadata.get('managed_sheet') else [])]))


def sheet_plan(name):
    return {'name':name,'path':'','header_row':1,'sku_col':1,'status_col':5,'status_letter':'E','new_status':False,
            'time_col':6,'time_letter':'F','columns':[{'index':i,'letter':chr(64+i),'title':title}
            for i,title in enumerate(('款号','来源文件','现场文件夹','入库时间'),1)],'images':[]}


class ShootingDocuments:
    def _init_documents(self):
        self.document_lock=threading.RLock()
        with self.connect() as c:
            existed=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shooting_bindings'").fetchone()
            c.executescript('''CREATE TABLE IF NOT EXISTS shooting_bindings(session TEXT PRIMARY KEY,project TEXT,scope TEXT,updated REAL);
CREATE TABLE IF NOT EXISTS shooting_captures(session TEXT,capture TEXT,project TEXT,scope TEXT,received REAL,PRIMARY KEY(session,capture));
CREATE INDEX IF NOT EXISTS shooting_capture_project ON shooting_captures(project,scope);''')
            c.execute('CREATE TABLE IF NOT EXISTS shooting_stream_queue(session TEXT,project TEXT,scope TEXT,updated REAL,PRIMARY KEY(session,project,scope))')
        # 仅首次升级接续旧版已绑定的导入计划；后续浏览历史不再隐式建立绑定。
        if not existed:
            p=self.project()
            if p and p['enabled'] and p['session_id'] and p['scope'] and not p['metadata'].get('automatic_day') and not p['metadata'].get('removed_at'):
                self._bind_document(p['session_id'],p['id'],p['scope'])
        self._restore_document_selection()

    def _restore_document_selection(self,preferred=None):
        """修复旧版中断后缺失的查看入口；绝不从查看入口重建现场绑定。"""
        sid=self.store.active_id()
        with self.document_lock,self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            active=c.execute("SELECT p.id FROM projects p JOIN settings s ON s.key='active' AND s.value=p.id WHERE json_extract(p.metadata,'$.removed_at') IS NULL").fetchone()
            if active:return active[0]
            row=c.execute("""SELECT p.id FROM projects p
                WHERE json_extract(p.metadata,'$.removed_at') IS NULL
                ORDER BY p.id=? DESC,p.id IN (SELECT project FROM shooting_bindings WHERE session=?) DESC,
                    p.created DESC,p.id LIMIT 1""",(preferred or '',sid)).fetchone()
            if row:c.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)",(row[0],))
            else:c.execute("DELETE FROM settings WHERE key='active'")
            return row[0] if row else None

    def document_day(self,stamp=None):
        return datetime.fromtimestamp(self.clock() if stamp is None else stamp,SHANGHAI).strftime('%Y-%m-%d')

    def shooting_context(self,sid=None,cid=None):
        if not sid:
            session=self.store.session();sid=session['id'] if session else None
        with self.connect() as c:
            row=c.execute('SELECT * FROM shooting_captures WHERE session=? AND capture=?',(sid,cid)).fetchone() if cid else None
            if not row and cid is None:row=c.execute('SELECT * FROM shooting_bindings WHERE session=?',(sid,)).fetchone()
        return dict(row) if row else None

    def _bind_document(self,sid,pid,scope):
        if not sid:return
        with self.document_lock,self.connect() as c:c.execute('INSERT OR REPLACE INTO shooting_bindings VALUES (?,?,?,?)',(sid,pid,scope,self.clock()))

    def _check_type_boundary(self,sid,pid,scope):
        context=self.shooting_context(sid)
        if not context or (context['project'],context['scope'])==(pid,scope):return
        with self.connect() as c:ids=[r[0] for r in c.execute('SELECT capture FROM shooting_captures WHERE session=? AND project=? AND scope=? ORDER BY received,rowid',(sid,context['project'],context['scope']))]
        if not ids:return
        records={r['id']:r for r in self.store.records(sid,ids=ids)}
        # 标签一经标为二星即完成边界；OCR 可以随后返回，继续写原类型。
        last=next((cid for cid in reversed(ids) if cid in records),None)
        if last and self._rating(sid,records[last])!=2:raise ValueError('当前款还有未收尾照片，请先将末张标签标为二星，再切换拍摄类型或文档')

    def live_arrivals(self,sid,records):
        """只由现场监控的新照片调用；普通浏览和历史索引不建档。"""
        with self.document_lock:
            with self.connect() as c:known={r[0] for r in c.execute('SELECT capture FROM shooting_captures WHERE session=?',(sid,))}
            fresh=[r for r in records if r['id'] not in known]
            if not fresh:
                self._restore_document_selection()
                return
            project=self._automatic_project(sid,start_stream=False)
            context=self.shooting_context(sid)
            scope=context['scope'] if context and context['project']==project['id'] else project['scope']
            with self.connect() as c:
                c.executemany('INSERT OR IGNORE INTO shooting_captures VALUES (?,?,?,?,?)',[(sid,r['id'],project['id'],scope,self.clock()) for r in fresh])
                c.execute('INSERT OR REPLACE INTO live_sessions VALUES (?,?)',(sid,self.clock()))
                # 入库前仅冻结归属；历史目录接续不能挡住 append_live/缩略图确认。
                c.execute('INSERT OR REPLACE INTO shooting_stream_queue VALUES (?,?,?,?)',(sid,project['id'],scope,self.clock()))

    def sync_shooting_streams(self):
        if not self.archive:return
        with self.connect() as c:pending=[dict(r) for r in c.execute('SELECT * FROM shooting_stream_queue ORDER BY updated')]
        for item in pending:
            p=self.project(item['project'])
            if p and not p['metadata'].get('removed_at'):
                self.archive.start_stream(item['session'],item['project'],item['scope'])
            with self.connect() as c:c.execute('DELETE FROM shooting_stream_queue WHERE session=? AND project=? AND scope=? AND updated=?',tuple(item[k] for k in ('session','project','scope','updated')))

    def _automatic_project(self,sid,*,start_stream=True):
        from sku_workbook_auto import create_book
        from sku_workbook import inspect_workbook
        import hashlib
        with self.document_lock:
            day=self.document_day();context=self.shooting_context(sid)
            p=self.project(context['project']) if context else None
            if p and not p['metadata'].get('automatic_day') and not p['metadata'].get('removed_at') and p['enabled']:
                scope=context['scope'] if context else p['scope'] or next((s['name'] for s in p['metadata']['sheets'] if not s.get('skipped')),'现场记录')
                if not context:self._bind_document(sid,p['id'],scope)
                self._restore_document_selection(p['id'])
                return dict(p,scope=scope)
            folder=None
            try:
                with self.connect() as c:
                    # 进程内 RLock 无法保护多引擎；查重和登记必须同属写事务。
                    c.execute('BEGIN IMMEDIATE')
                    row=c.execute("SELECT id FROM projects WHERE json_extract(metadata,'$.automatic_day')=? AND json_extract(metadata,'$.removed_at') IS NULL ORDER BY created,id LIMIT 1",(day,)).fetchone()
                    if row:pid=row['id']
                    else:
                        from file_safety import atomic_write
                        pid=uuid.uuid4().hex;folder=self.root/pid;folder.mkdir();content=create_book();atomic_write(folder/'source.xlsx',content)
                        sheets=inspect_workbook(folder/'source.xlsx',allow_empty=True)
                        metadata={'sheets':[{k:v for k,v in s.items() if k!='rows'} for s in sheets],'automatic_day':day,'managed_sheet':'现场记录','managed_sheets':['现场记录'],'progress_version':1,'source_sha256':hashlib.sha256(content).hexdigest()}
                        count=c.execute("SELECT count(*) FROM projects WHERE json_extract(metadata,'$.automatic_day')=?",(day,)).fetchone()[0]
                        name=day+(f'（{count+1}）' if count else '')+'.xlsx'
                        c.execute('INSERT INTO projects(id,name,metadata,session_id,scope,enabled,created) VALUES (?,?,?,?,?,?,?)',(pid,name,json.dumps(metadata,ensure_ascii=False),sid,'现场记录',1,self.clock()))
                    canonical=dict(c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone())
                    canonical['metadata']=json.loads(canonical['metadata'])
                    # 项目、绑定、显示选择和待接续任务必须一并提交或回滚。
                    if p and p['metadata'].get('automatic_day')==day and not p['metadata'].get('removed_at') and context:
                        scope=context['scope']
                        if scope not in {s['name'] for s in canonical['metadata']['sheets']}:canonical=p
                    else:scope=canonical['scope'] or '现场记录'
                    p=dict(canonical,scope=scope)
                    c.execute('INSERT OR REPLACE INTO shooting_bindings VALUES (?,?,?,?)',(sid,p['id'],scope,self.clock()))
                    c.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)",(p['id'],))
                    if self.archive:c.execute('INSERT OR REPLACE INTO shooting_stream_queue VALUES (?,?,?,?)',(sid,p['id'],scope,self.clock()))
            except Exception:
                if folder:
                    (folder/'source.xlsx').unlink(missing_ok=True);folder.rmdir()
                raise
            if folder:self.dirty.add(pid)
        if self.archive and start_stream:self.archive.start_stream(sid,p['id'],scope)
        return p

    def document_inventory(self):
        today=self.document_day()
        with self.connect() as c:
            rows=[dict(r) for r in c.execute('SELECT id,name,scope,created,metadata,session_id,export_error FROM projects ORDER BY created DESC')]
        result=[]
        for row in rows:
            metadata=json.loads(row.pop('metadata'));row['removed']=bool(metadata.get('removed_at'))
            row['day']=metadata.get('automatic_day') or self.document_day(metadata.get('loaded_at',row['created']))
            row['today']=row['day']==today;row['automatic']=bool(metadata.get('automatic_day'))
            row['types']=[s['name'] for s in metadata['sheets'] if not s.get('skipped')]
            result.append(row)
        # 只归并自动日期文档。来源表、作业、照片冻结引用原封保留，读取时汇总。
        groups={};visible=[]
        for row in sorted(result,key=lambda r:(r['created'],r['id'])):
            key=row['day'] if row['automatic'] and not row['removed'] else row['id']
            if key in groups:
                group=groups[key];group['source_ids'].append(row['id'])
                group['types']=list(dict.fromkeys(group['types']+row['types']))
            else:
                row['source_ids']=[row['id']];groups[key]=row;visible.append(row)
        return sorted(visible,key=lambda r:(r['day'],r['created'],r['id']),reverse=True)

    def document_sources(self,pid):
        return next((r['source_ids'] for r in self.document_inventory() if pid in r['source_ids']),[pid])

    def document_overview(self,scope='today'):
        if scope not in ('today','history'):raise ValueError('显示范围无效')
        documents=[p for p in self.document_inventory() if not p['removed'] and (scope=='history' or p['today'])]
        rows=[]
        for document in documents:
            items=self._document_rows(document['source_ids'])
            for row in items:
                row.update(document_id=document['id'],document_name=document['name'],document_day=document['day'])
            rows.extend(items)
        return {'scope':scope,'documents':documents,'rows':rows}

    def create_shooting_type(self,pid,name,sid=None):
        if not isinstance(name,str):raise ValueError('请填写拍摄类型')
        name=name.strip()
        if not name or len(name)>31 or re.search(r'[\\/*?:\[\]\x00-\x1f]',name) or name.startswith("'") or name.endswith("'"):
            raise ValueError('类型名称须为 1–31 个字符，不能含 \\ / * ? : [ ] 或首尾单引号')
        with self.lock:
            p=self.project(pid)
            if not p or p['metadata'].get('removed_at'):raise ValueError('拍摄文档不存在或已移除')
            sid=sid or p['session_id']
            if not sid or not self.store.session(sid):raise ValueError('请先打开现场照片目录')
            self._check_type_boundary(sid,pid,name)
            names={s['name'].casefold():s['name'] for s in p['metadata']['sheets']}
            if name.casefold() in names:return self.configure(pid,sid,names[name.casefold()])
            metadata=p['metadata'];metadata['managed_sheets']=managed_sheets(metadata)+[name];metadata['sheets'].append(sheet_plan(name))
            with self.connect() as c:c.execute('UPDATE projects SET metadata=? WHERE id=?',(json.dumps(metadata,ensure_ascii=False),pid))
            # Excel 被占用时保留已登记类型，可通过导出重试；不创建第二张同名表。
            self.dirty.add(pid)
            return self.configure(pid,sid,name)

    def manage_document(self,pid,action):
        with self.lock,self.document_lock:
            p=self.project(pid)
            if not p:raise ValueError('拍摄文档不存在')
            if action not in ('remove','restore'):raise ValueError('文档管理操作无效')
            sources=self.document_sources(pid) if action=='remove' else p['metadata'].get('removed_group',[pid])
            with self.connect() as c:
                for source_id in sources:
                    metadata=json.loads(c.execute('SELECT metadata FROM projects WHERE id=?',(source_id,)).fetchone()[0])
                    if action=='remove':metadata.update(removed_at=self.clock(),removed_group=sources)
                    else:metadata.pop('removed_at',None);metadata.pop('removed_group',None)
                    c.execute('UPDATE projects SET metadata=? WHERE id=?',(json.dumps(metadata,ensure_ascii=False),source_id))
                    if action=='remove':
                        c.execute('DELETE FROM shooting_bindings WHERE project=?',(source_id,))
                        c.execute("DELETE FROM settings WHERE key='active' AND value=?",(source_id,))
            if action=='remove' and self.archive:
                with self.archive.connect() as c:
                    for source_id in sources:c.execute("UPDATE streams SET state='paused' WHERE project=? AND state='active'",(source_id,))
            # 已冻结的照片、任务和元数据不转移，也不取消用户已提交的写入。
            return self.state()

    def rename_shooting_row(self,pid,rid,old,new):
        from sku_match import normalize
        from sku_library_management import rename_sku
        if not isinstance(new,str) or not new.strip() or len(new.strip())>80 or any(ord(ch)<32 for ch in new) or not normalize(new):raise ValueError('请填写有效 SKU（最多 80 字符）')
        new=new.strip()
        with self.lock:
            p=self.project(pid)
            if not p or p['metadata'].get('removed_at'):raise ValueError('拍摄文档不存在或已移除')
            with self.connect() as c:row=c.execute('SELECT * FROM sku_rows WHERE id=? AND project=?',(rid,pid)).fetchone()
            if not row or row['sku']!=old:raise ValueError('款号已变化，请刷新后重新编辑')
            if old==new:return {'state':self.state(),'operation_ids':[],'capture_count':0}
            with self.connect() as c:
                if c.execute('SELECT 1 FROM sku_rows WHERE project=? AND normalized=? AND normalized!=?',(pid,normalize(new),row['normalized'])).fetchone():raise ValueError('目标款号已存在，不会自动合并两款')
                chosen=c.execute('SELECT o.sku FROM jobs j JOIN ocr_choices o ON o.session_id=j.session_id AND o.capture_id=j.capture_id WHERE j.id=?',(row['completion_job'],)).fetchone()
            photo_code=chosen[0] if chosen else old
            # 有实际照片时复用整款写入器，含 RAW/JPG、副本、人工保护和失败重试。
            known=None
            if self.archive:
                with self.archive.connect() as c:
                    known=c.execute('SELECT sku FROM assets WHERE normalized=? LIMIT 1',(normalize(photo_code),)).fetchone()
            result=rename_sku(self,self.archive,known['sku'],new,defer_io=True) if known else {'operation_ids':[],'capture_count':0}
            with self.connect() as c:
                rows=c.execute('SELECT * FROM sku_rows WHERE project=? AND normalized=?',(pid,row['normalized'])).fetchall()
                for item in rows:
                    if item['id']!=rid:
                        linked=c.execute('SELECT o.sku FROM jobs j JOIN ocr_choices o ON o.session_id=j.session_id AND o.capture_id=j.capture_id WHERE j.id=?',(item['completion_job'],)).fetchone()
                        if not linked or normalize(linked[0])!=normalize(photo_code):continue
                    sheet=next(s for s in p['metadata']['sheets'] if s['name']==item['sheet'])
                    from sku_workbook import col_name
                    values=json.loads(item['source_values'] or '{}');values[col_name(sheet['sku_col'])]=new
                    c.execute('UPDATE sku_rows SET sku=?,normalized=?,source_values=? WHERE id=?',(new,normalize(new),json.dumps(values,ensure_ascii=False),item['id']))
            # 数据库是清单事实源；Excel 只在显式导出快照内更新，提交不解压图片文档。
            self.dirty.add(pid)
            return {**result,'state':self.state()}
