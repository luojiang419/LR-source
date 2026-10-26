"""当前拍摄项的只读解析摘要，不创建资产、不触发模型。"""
import json
from sku_match import extract_codes


def _json(value, fallback):
    try:
        return json.loads(value or '')
    except (ValueError, TypeError):
        return fallback


def capture_status(store, sku, archive, analysis, sid, cid):
    with store.connect() as c:
        if not c.execute('SELECT 1 FROM captures WHERE session=? AND id=?', (sid, cid)).fetchone():
            raise ValueError('当前照片不存在，请重新选择')
    ocr = {'status': 'unparsed', 'codes': [], 'reason': ''}
    if sku:
        jobs = []
        with sku.connect() as c:
            chosen=c.execute('SELECT sku FROM ocr_choices WHERE session_id=? AND capture_id=?',(sid,cid)).fetchone()
            for table in ('parse_jobs', 'jobs'):
                trigger="'parse' AS trigger" if table=='parse_jobs' else 'trigger'
                row = c.execute(f'SELECT id,run_token,created,status,result,reason,{trigger},CASE WHEN updated>0 THEN updated ELSE created END AS evidence_at FROM {table} WHERE session_id=? AND capture_id=? ORDER BY evidence_at DESC LIMIT 1', (sid, cid)).fetchone()
                if row:
                    jobs.append(dict(row,kind=table))
        if jobs:
            job = max(jobs, key=lambda j: j['evidence_at'])
            result = _json(job['result'], {})
            readings = [result] + (result.get('readings', []) if isinstance(result, dict) else [])
            codes = []
            for reading in readings:
                if not isinstance(reading, dict):
                    continue
                for code in reading.get('codes') or extract_codes(reading.get('text', '')):
                    if isinstance(code, str) and code.strip() and len(code) <= 80 and code.strip() not in codes:
                        codes.append(code.strip())
            ocr.update(status=job['status'], codes=codes, reason=job['reason'] or '')
            ocr['job_key']=job['kind']+':'+job['id']+':'+job['run_token']
            if job['status'] in ('running','queued','retry'):
                ocr['progress']=sku.ocr_progress.snapshot(job['kind'],job['id'],job['run_token']) or {'phase':'recognizing' if job['status']=='running' else 'queued','percent':None,'elapsed_seconds':max(0,sku.clock()-job['created'])}
            if job['status'] in ('completed','matched','confirmed','review','unmatched') and result.get('requires_sku_choice'):
                ocr.update(status='review',codes=[],candidates=sku.recommended_codes(readings),reason='OCR 资料已保存，请选择正确 SKU')
            if job['status'] in ('queued','waiting_model','retry'):
                if sku.tasks_paused:ocr.update(status='paused',reason='在解析任务页继续队列后自动恢复')
                elif job['trigger'] not in ('manual','full_auto') and sku.task_mode!='auto':ocr.update(status='manual',reason='在解析任务页开始任务，或切换为自动解析')
        if chosen and ocr['status'] in ('unparsed','completed','matched','confirmed','review','unmatched'):ocr.update(status='confirmed',codes=[chosen[0]],reason='用户已确认主款号，其他 OCR 文字保留为辅助资料')
    multimodal = {'status': 'unparsed', 'reason': ''}
    if archive and analysis:
        with archive.connect() as c:
            link = c.execute('SELECT asset FROM catalog_links WHERE session=? AND capture=?', (sid, cid)).fetchone()
        with analysis.connect() as c:
            jobs = []
            if link:
                row = c.execute('SELECT status,reason,created FROM jobs WHERE asset=? ORDER BY created DESC LIMIT 1', (link['asset'],)).fetchone()
                if row:
                    jobs.append(dict(row))
            row = c.execute("SELECT status,reason,created FROM auto_inputs WHERE session=? AND capture=? AND status IN ('queued','running','failed')", (sid, cid)).fetchone()
            if row:
                jobs.append(dict(row))
        if jobs:
            job = max(jobs, key=lambda j: j['created'])
            multimodal.update(status=job['status'], reason=job['reason'] or '')
    return {'session_id': sid, 'capture_id': cid, 'ocr': ocr, 'multimodal': multimodal}
