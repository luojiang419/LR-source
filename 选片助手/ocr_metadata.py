"""持久保存 OCR 辅助资料、全文索引和用户确认的主款号。"""
import hashlib
import json
import re
import unicodedata
import uuid

from sku_match import extract_codes,normalize
from sku_progress import capture_time
import media_engine as media


def search_text(value):return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',str(value)).casefold()).strip()


def record_ocr(c,kind,jid,sid,cid,result,stamp):
    readings=result.get('readings') if isinstance(result,dict) else None
    readings=readings or [result]
    readings=[r for r in readings if isinstance(r,dict) and (r.get('text') or r.get('codes') or r.get('auxiliary'))]
    if not readings:return
    payload=[{k:r[k] for k in ('text','codes','ocr_candidates','auxiliary','model','confidence','unscored') if k in r} for r in readings]
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True);identity=kind+':'+jid+':'+hashlib.sha256(raw.encode()).hexdigest()[:20]
    old=c.execute('SELECT rowid,updated FROM ocr_documents WHERE id=?',(identity,)).fetchone()
    if old and old['updated']>=stamp:return
    text='\n'.join(dict.fromkeys(str(r.get('text','')) for r in readings if r.get('text')))
    searchable=search_text(text+'\n'+json.dumps([{k:r.get(k) for k in ('codes','ocr_candidates','auxiliary')} for r in readings],ensure_ascii=False))
    c.execute('INSERT INTO ocr_documents(id,session_id,capture_id,text,raw,searchable,updated) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated',
        (identity,sid,cid,text,raw,searchable,stamp))
    rowid=c.execute('SELECT rowid FROM ocr_documents WHERE id=?',(identity,)).fetchone()[0]
    c.execute('DELETE FROM ocr_document_search WHERE rowid=?',(rowid,));c.execute('INSERT INTO ocr_document_search(rowid,searchable) VALUES (?,?)',(rowid,searchable))


def initialize_ocr(sku):
    with sku.connect() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS ocr_documents(id TEXT PRIMARY KEY,session_id TEXT,capture_id TEXT,text TEXT,raw TEXT,searchable TEXT,updated REAL);
CREATE INDEX IF NOT EXISTS ocr_documents_capture ON ocr_documents(session_id,capture_id,updated DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS ocr_document_search USING fts5(searchable,tokenize='trigram');
CREATE TABLE IF NOT EXISTS ocr_resets(session_id TEXT,capture_id TEXT,updated REAL,PRIMARY KEY(session_id,capture_id));
CREATE TABLE IF NOT EXISTS ocr_choices(session_id TEXT,capture_id TEXT,sku TEXT,reading_key TEXT,updated REAL,PRIMARY KEY(session_id,capture_id));''')
        from sku_shoot_versions import initialize
        initialize(c)
        for table,kind,status in [('parse_jobs','read',"'completed'"),('jobs','job',"'matched','confirmed','review','unmatched'")]:
            for row in c.execute(f'SELECT id,session_id,capture_id,result,created,updated FROM {table} WHERE status IN ({status})').fetchall():
                try:result=json.loads(row['result'] or '{}')
                except ValueError:continue
                if isinstance(result,dict):record_ocr(c,kind,row['id'],row['session_id'],row['capture_id'],result,row['updated'] or row['created'])
        if sku.require_sku_choice:
            # 已经完成的旧确认沿用，新识别只保存资料，避免一次升级清掉现有区间。
            rows={r['id']:r['sku'] for r in c.execute('SELECT id,sku FROM sku_rows')}
            for job in c.execute("SELECT * FROM jobs WHERE status IN ('matched','confirmed') ORDER BY updated,created").fetchall():
                result=json.loads(job['result'] or '{}')
                if not isinstance(result,dict) or result.get('requires_sku_choice'):continue
                codes={rows[rid] for rid in json.loads(job['targets'] or '[]') if rid in rows}
                if len(codes)==1:c.execute('INSERT OR IGNORE INTO ocr_choices VALUES (?,?,?,?,?)',(job['session_id'],job['capture_id'],next(iter(codes)),'legacy:'+job['id'],job['updated'] or job['created']))


def matching_ocr(sku,query):
    query=search_text(query)
    if not query:return []
    with sku.connect() as c:
        if len(query)>=3:
            ids=[r[0] for r in c.execute('SELECT rowid FROM ocr_document_search WHERE ocr_document_search MATCH ?',('"'+query.replace('"','""')+'"',))]
            if not ids:return []
            rows=[]
            for start in range(0,len(ids),500):
                part=ids[start:start+500];rows.extend(c.execute('SELECT * FROM ocr_documents WHERE rowid IN ('+','.join('?' for _ in part)+')',part).fetchall())
            return [dict(r) for r in rows]
        return [dict(r) for r in c.execute("SELECT * FROM ocr_documents WHERE searchable LIKE ? ESCAPE '\\'",('%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%',))]


class OcrMetadata:
    def _clear_capture_ocr(self,c,sid,cid):
        """清零是识别失效边界，旧请求和全自动扫描均不得复活旧资料。"""
        c.execute('DELETE FROM ocr_document_search WHERE rowid IN (SELECT rowid FROM ocr_documents WHERE session_id=? AND capture_id=?)',(sid,cid))
        c.execute('DELETE FROM ocr_documents WHERE session_id=? AND capture_id=?',(sid,cid))
        c.execute('DELETE FROM ocr_choices WHERE session_id=? AND capture_id=?',(sid,cid))
        c.execute('INSERT OR REPLACE INTO ocr_resets VALUES (?,?,?)',(sid,cid,self.clock()))
        for table in ('jobs','parse_jobs'):
            targets=",targets='[]'" if table=='jobs' else ''
            c.execute(f"UPDATE {table} SET status='cancelled',result='{{}}',run_token='',reason='标签不再是二星，识别已清空，再次二星将重新识别',updated=?{targets} WHERE session_id=? AND capture_id=?",(self.clock(),sid,cid))

    def ocr_details(self,sid,cid):
        session=self.store.session(sid);record=next((r for r in self.store.records(sid) if r['id']==cid),None) if session else None
        if not record:raise ValueError('照片不存在，请重新选择')
        target=cid;source_record=record
        from sku_library import interval_rows
        projected,_=interval_rows(self,sid)
        linked=next((r for r in projected if r['capture_id']==cid and r.get('interval_id')),None)
        if linked:
            target=linked['interval_id'].rsplit(':',1)[-1]
            source_record=next((r for r in self.store.records(sid) if r['id']==target),record)
        with self.connect() as c:
            documents=[dict(r) for r in c.execute('SELECT id,text,raw,updated FROM ocr_documents WHERE session_id=? AND capture_id IN (?,?) ORDER BY updated DESC,id LIMIT 50',(sid,cid,target))]
            chosen=c.execute('SELECT sku FROM ocr_choices WHERE session_id=? AND capture_id=?',(sid,target)).fetchone()
        readings=[];candidates=[]
        for doc in documents:
            for reading in json.loads(doc['raw']):
                readings.append(reading)
                for value in (reading.get('ocr_candidates') or extract_codes(reading.get('text','')))+list(reading.get('codes') or []):
                    if isinstance(value,str) and value.strip() and len(value)<=80 and value not in candidates:candidates.append(value)
        # 后台与手动识别可能重复保存相同资料；仅内容或标签来源改变才使确认失效。
        key=hashlib.sha256(json.dumps([target,sorted({d['raw'] for d in documents})],ensure_ascii=False).encode()).hexdigest()
        paths=list(dict.fromkeys(media.resolved_file(session['root'],r[k]) for r in (record,source_record) for k in ('jpg','raw') if r.get(k)))
        signature=[(str(p),p.stat().st_size,p.stat().st_mtime_ns) for p in paths]
        image_key=hashlib.sha256(json.dumps(signature).encode()).hexdigest()
        recommended=self.recommended_codes(readings)
        version={'base_sku':chosen[0] if chosen else '','shoot_version':''}
        if chosen and self.archive:
            with self.archive.connect() as c:link=c.execute('SELECT asset FROM catalog_links WHERE session=? AND capture=?',(sid,target)).fetchone()
            if link:
                with self.connect() as c:assigned=c.execute('SELECT * FROM sku_shoot_versions WHERE asset=? AND sku=?',(link['asset'],chosen[0])).fetchone()
                if assigned:
                    from sku_shoot_versions import describe
                    version=describe(assigned)
        return {'session_id':sid,'capture_id':cid,'target_capture_id':target,'source_name':source_record['name'],'name':record['name'],'reading_key':key,'image_key':image_key,'readings':readings,
            'candidates':candidates[:40],'recommended':recommended[:12],
            'latest_recommended':self.recommended_codes(json.loads(documents[0]['raw'])) if documents else [],
            'selected_sku':chosen[0] if chosen else '',**version,
            'has_ocr':bool(documents),'text':'\n\n'.join(dict.fromkeys(d['text'] for d in documents if d['text']))}

    def choose_ocr_sku(self,sid,cid,code,reading_key,image_key,scope='',*,allow_manual=False,project_id=None,automatic=False,literal=False):
        if not isinstance(code,str) or not normalize(code) or len(code)>80 or any(ord(ch)<32 for ch in code):raise ValueError('请填写正确 SKU（最多 80 字符）')
        code=code.strip()
        with self.lock:
            details=self.ocr_details(sid,cid)
            if not details['has_ocr'] and not allow_manual:raise ValueError('尚无 OCR 资料，请先识别图片')
            if details['reading_key']!=reading_key or details['image_key']!=image_key:raise ValueError('图片或 OCR 资料已更新，请重新打开资料核对')
            requested=cid;cid=details['target_capture_id']
            project=self.project(project_id)
            if not project or project['session_id']!=sid:project=self._automatic_project(sid)
            scope=scope or project['scope'] or '现场记录'
            if not self.archive:raise ValueError('资产入库服务未就绪')
            record=next(r for r in self.store.records(sid) if r['id']==cid)
            boundary=self._rating(sid,record)==2
            preview=self.archive.preview(sid,[cid],code,scope,project['id'],overwrite=True)
            old=preview['targets'][0]['before']
            version={'base_sku':code,'sku':code,'shoot_version':''}
            with self.connect() as c:
                # 款号行、主款确认、拍摄证据及归档事件必须同成同败。
                c.execute('BEGIN IMMEDIATE')
                if boundary:
                    from sku_shoot_versions import allocate,remember_manual
                    version=(remember_manual(c,preview['targets'][0]['id'],code,self.clock()) if literal
                             else allocate(c,self.archive,preview['targets'][0]['id'],sid,cid,code,self.clock()))
                    if version['sku']!=code:
                        code=version['sku']
                        preview=self.archive.preview(sid,[cid],code,scope,project['id'],overwrite=True)
                        old=preview['targets'][0]['before']
                if literal:
                    c.execute("UPDATE parse_jobs SET status='cancelled',run_token='',reason='手动款号已确认，旧识别任务已取消',updated=? WHERE session_id=? AND capture_id=? AND status IN ('queued','running')",(self.clock(),sid,cid))
                # 历史上已单张人工备案的标签仍允许明确修正；其他人工照片由区间保护。
                if not boundary or old.get('locked'):
                    if old.get('sku')!=code or old.get('scope')!=scope or not old.get('locked'):self.archive.apply(preview['preview_id'])
                base=version['base_sku']
                exact=[dict(r) for r in c.execute('SELECT * FROM sku_rows WHERE project=? AND sheet=? AND normalized=?',(project['id'],scope,normalize(base)))]
                result={'row_ids':[r['id'] for r in exact]} if exact else self._record_unplanned({'project':project['id'],'session_id':sid,'capture_id':cid,'scope':scope},
                    [{'codes':[base],'unscored':True}],{'status':'review','row_ids':[],'candidates':[]},accept_observed=True,connection=c)
                c.execute('INSERT INTO ocr_choices VALUES (?,?,?,?,?) ON CONFLICT(session_id,capture_id) DO UPDATE SET sku=excluded.sku,reading_key=excluded.reading_key,updated=excluded.updated',(sid,cid,code,reading_key,self.clock()))
                targets=json.dumps(result.get('row_ids',[]));stamp,time_source=capture_time(record)
                c.execute("UPDATE jobs SET status='confirmed',targets=?,reason='用户已选择正确 SKU，其他 OCR 资料保留',updated=? WHERE project=? AND session_id=? AND capture_id=? AND scope=? AND status NOT IN ('ignored','cancelled')",(targets,self.clock(),project['id'],sid,cid,scope))
                # 手动解析只产生 parse_jobs；明确选择也必须保存可供文档记账的拍摄证据。
                c.execute('''INSERT INTO jobs(id,project,session_id,capture_id,fingerprint,scope,status,due,created,result,targets,reason,captured_at,capture_time_source,trigger,updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,session_id,capture_id,fingerprint,scope)
                    DO UPDATE SET status='confirmed',targets=excluded.targets,result=excluded.result,reason=excluded.reason,captured_at=excluded.captured_at,capture_time_source=excluded.capture_time_source,trigger=CASE WHEN jobs.trigger='full_auto' THEN 'full_auto' ELSE 'choice' END,updated=excluded.updated''',
                    (uuid.uuid4().hex,project['id'],sid,cid,record['fingerprint'],scope,'confirmed',self.clock(),self.clock(),json.dumps({'codes':[code],'user_sku_choice':True}),targets,'用户已选择正确 SKU，同步拍摄记录',stamp,time_source,'choice',self.clock()))
                if boundary:
                    job={'id':'choice:'+sid+':'+cid,'kind':'ocr_choice','session_id':sid,'capture_id':cid,'project':project['id'],'scope':scope,'status':'confirmed','sku':code}
                    c.execute('INSERT INTO archive_events(id,job,payload) VALUES (?,?,?)',(uuid.uuid4().hex,job['id'],json.dumps(job,ensure_ascii=False)))
                self._refresh_progress(c,project['id'])
                if automatic:c.execute("UPDATE jobs SET reason='公司款号包含规则唯一命中，已自动应用' WHERE session_id=? AND capture_id=? AND status='confirmed'",(sid,cid))
            warnings=[]
            if boundary:
                try:self.deliver_events(self.archive)
                except Exception as exc:
                    from operation_errors import record_error
                    record_error(self.store.data,'sku-choice-archive',exc)
                    warnings.append('SKU 和拍摄记录已保存，整款归档待后台重试')
            try:self.export(project['id'])
            except Exception as exc:
                from operation_errors import record_error
                record_error(self.store.data,'sku-choice-export',exc)
                self.dirty.add(project['id']);self.wake.set()
                warnings.append('SKU 和拍摄记录已保存，Excel 更新待重试')
        from metadata_group import group_context
        operation='';count=1
        try:
            group=group_context(self.archive,self,sid,requested,[requested])
            count=len(group['capture_ids']) if boundary else 1
            with self.archive.connect() as c:
                segment=c.execute('SELECT operation FROM segments WHERE id=?',(group['segment'],)).fetchone()
            operation=segment['operation'] if segment else ''
        except Exception as exc:
            from operation_errors import record_error
            record_error(self.store.data,'sku-choice-result',exc)
            warnings.append('SKU 和拍摄记录已保存，归档信息暂未刷新')
        return {'session_id':sid,'capture_id':requested,'target_capture_id':cid,**version,'saved':True,'automatic':automatic,'warnings':warnings,'capture_count':count,'operation_id':operation}
