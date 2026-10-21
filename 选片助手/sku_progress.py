"""拍摄时间证据与按工作表的款号统计。"""
from datetime import datetime,timezone
import re


def capture_time(record):
    identity=record.get('captureIdentity') or {}
    source='EXIF.SubSecDateTimeOriginal' if identity.get('SubSecDateTimeOriginal') else 'EXIF.DateTimeOriginal'
    value=identity.get('SubSecDateTimeOriginal') or identity.get('DateTimeOriginal')
    if not value and record.get('time') and not record.get('missingTime'):
        value=record['time'];source='catalog.exif_time'
    if not isinstance(value,str):return '', 'missing'
    value=value.strip();value=value[:10].replace(':','-')+'T'+value[11:]
    if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?',value):return '', 'missing'
    try:parsed=datetime.fromisoformat(value)
    except ValueError:return '', 'missing'
    if '.' not in value and str(identity.get('SubSecTimeOriginal','')).isdigit():
        end=len(value) if parsed.tzinfo is None else (len(value)-1 if value.endswith('Z') else len(value)-6)
        value=value[:end]+'.'+str(identity['SubSecTimeOriginal'])+value[end:]
    if parsed.tzinfo is None and re.fullmatch(r'[+-]\d\d:\d\d',str(identity.get('OffsetTimeOriginal',''))):
        candidate=value+identity['OffsetTimeOriginal']
        try:datetime.fromisoformat(candidate);value=candidate
        except ValueError:pass
    return value,source


def statistics(rows,sheets):
    result=[]
    for sheet in sheets:
        if sheet.get('skipped'):continue
        scoped=[r for r in rows if r['sheet']==sheet['name']];groups={}
        for row in scoped:groups[row['normalized']]=groups.get(row['normalized'],False) or row['status']=='已拍摄'
        shot=sum(groups.values())
        result.append({'sheet':sheet['name'],'total':len(groups),'shot':shot,'pending':len(groups)-shot,'rows':len(scoped)})
    return result


def completion_order(job):
    value=job['captured_at']
    try:
        parsed=datetime.fromisoformat(value)
        if parsed.tzinfo is not None:parsed=parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError,TypeError):return (False,datetime.min,job['created'],job['id'])
    return (True,parsed,job['created'],job['id'])
