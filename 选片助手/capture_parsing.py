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
            for table in ('parse_jobs', 'jobs'):
                row = c.execute(f'SELECT status,result,reason,created FROM {table} WHERE session_id=? AND capture_id=? ORDER BY created DESC LIMIT 1', (sid, cid)).fetchone()
                if row:
                    jobs.append(dict(row))
        if jobs:
            job = max(jobs, key=lambda j: j['created'])
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
