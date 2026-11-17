"""款末标签的稳定重拍版本；原文款号与照片实际 SKU 分开保存。"""
import string
import re
from datetime import datetime,timedelta,timezone

from sku_match import normalize


class VersionLimitError(ValueError):pass


def initialize(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS sku_shoot_versions(
        asset TEXT,normalized TEXT,base_sku TEXT,sku TEXT,version INTEGER,created REAL,
        PRIMARY KEY(asset,normalized));
        CREATE INDEX IF NOT EXISTS shoot_version_base ON sku_shoot_versions(normalized);
        CREATE TABLE IF NOT EXISTS sku_shoot_days(asset TEXT,normalized TEXT,shoot_day TEXT,origin TEXT DEFAULT 'ocr',PRIMARY KEY(asset,normalized));
        CREATE INDEX IF NOT EXISTS shoot_day_scope ON sku_shoot_days(shoot_day);
        DROP INDEX IF EXISTS shoot_version_unique;''')


def record_day(record, fallback=''):
    """拍摄日期来自照片；缺日期时只在当前会话内比较，不猜成今天。"""
    for name in ('shoot_date','time'):
        raw=str(record.get(name) or '')
        value=raw[:10].replace(':','-')
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            try:
                parsed=datetime.fromisoformat(value+raw[10:])
                if name=='time' and parsed.tzinfo:return parsed.astimezone(timezone(timedelta(hours=8))).date().isoformat()
                return parsed.date().isoformat()
            except ValueError:continue
    return fallback


def capture_day(archive,sid,cid):
    records=archive.store.records(sid,[cid])
    return record_day(records[0], 'session:'+sid) if records else 'session:'+sid


def _asset_day(archive,asset):
    with archive.connect() as db:links=list(db.execute('SELECT session,capture FROM catalog_links WHERE asset=?',(asset,)))
    days={capture_day(archive,row['session'],row['capture']) for row in links}
    known={day for day in days if not day.startswith('session:')}
    return next(iter(known)) if len(known)==1 else next(iter(days)) if len(days)==1 else ''


def describe(row):
    return {'base_sku':row['base_sku'],'sku':row['sku'],
            'shoot_version':string.ascii_uppercase[row['version']-1] if row['version'] else ''}


def remember_manual(c,asset,code,stamp):
    """明确输入以当前文字为准；已有同字版本仍保留其原款号关联。"""
    assigned=c.execute('SELECT * FROM sku_shoot_versions WHERE asset=? AND sku=?',(asset,code)).fetchone()
    if assigned:return describe(assigned)
    c.execute('''INSERT INTO sku_shoot_versions VALUES (?,?,?,?,?,?)
        ON CONFLICT(asset,normalized) DO UPDATE SET base_sku=excluded.base_sku,sku=excluded.sku,version=0''',
        (asset,normalize(code),code,code,0,stamp))
    c.execute("INSERT OR REPLACE INTO sku_shoot_days VALUES(?,?,?,'manual')",(asset,normalize(code),''))
    return {'base_sku':code,'sku':code,'shoot_version':''}


def allocate(c,archive,asset,sid,cid,code,stamp):
    """调用方持有 BEGIN IMMEDIATE；清零保留占号，同一永久标签身份重试不递增。"""
    # 用户重新点击已经分配的带版本款号时，继续使用该标签的原款号。
    assigned=c.execute('SELECT * FROM sku_shoot_versions WHERE asset=? AND sku=?',(asset,code)).fetchone()
    if assigned:return describe(assigned)
    key=normalize(code)
    existing=c.execute('SELECT * FROM sku_shoot_versions WHERE asset=? AND normalized=?',(asset,key)).fetchone()
    if existing and existing['base_sku']==code:return describe(existing)
    day=capture_day(archive,sid,cid)
    variants=[code]+[code+'-'+letter for letter in string.ascii_uppercase]
    keys=[normalize(value) for value in variants]
    slots=','.join('?' for _ in keys)
    # 只补当前原款号相关的旧本机记录；不访问历史图片或导入文档。
    related=list(c.execute('SELECT v.* FROM sku_shoot_versions v LEFT JOIN sku_shoot_days d ON d.asset=v.asset AND d.normalized=v.normalized WHERE (v.base_sku=? OR v.sku IN ('+slots+")) AND coalesce(d.shoot_day,'')=''",[code,*variants]))
    for row in related:
        known=_asset_day(archive,row['asset'])
        if known:c.execute("INSERT INTO sku_shoot_days VALUES(?,?,?,'legacy') ON CONFLICT(asset,normalized) DO UPDATE SET shoot_day=excluded.shoot_day",(row['asset'],row['normalized'],known))
    scoped=list(c.execute('SELECT v.*,d.origin FROM sku_shoot_versions v JOIN sku_shoot_days d ON d.asset=v.asset AND d.normalized=v.normalized WHERE d.shoot_day=? AND (v.base_sku=? OR v.sku IN ('+slots+'))',[day,code,*variants]))
    with archive.connect() as db:
        assets=db.execute('SELECT id,sku FROM assets WHERE normalized IN ('+slots+')',keys).fetchall()
    assets=[row for row in assets if row['sku'] in variants]
    assets=[row for row in assets if row['id']==asset or _asset_day(archive,row['id'])==day]
    prior=c.execute('SELECT sku FROM ocr_choices WHERE session_id=? AND capture_id=?',(sid,cid)).fetchone()
    # 升级不改名已有确认，也不改写同一照片的历史原 SKU。
    keep=bool(prior and prior['sku']==code) or any(row['id']==asset and row['sku']==code for row in assets)
    duplicate=any(row['asset']!=asset and row['base_sku']==code for row in scoped) or any(row['id']!=asset and row['sku']==code for row in assets)
    used={row['sku'] for row in assets if row['id']!=asset}|{row['sku'] for row in scoped if row['asset']!=asset}
    version=0
    if not keep and duplicate:
        version=next((index for index,value in enumerate(variants) if index and value not in used),None)
        if version is None:raise VersionLimitError('此款重拍版本 A–Z 已用完，请使用新的原款号')
    selected=variants[version]
    if len(selected)>80:raise VersionLimitError('原款号过长，追加重拍版本后不能超过 80 字符')
    c.execute('INSERT INTO sku_shoot_versions VALUES (?,?,?,?,?,?) ON CONFLICT(asset,normalized) DO UPDATE SET base_sku=excluded.base_sku,sku=excluded.sku,version=excluded.version,created=excluded.created',(asset,key,code,selected,version,stamp))
    c.execute("INSERT OR REPLACE INTO sku_shoot_days VALUES(?,?,?,'ocr')",(asset,key,day))
    return {'base_sku':code,'sku':selected,'shoot_version':string.ascii_uppercase[version-1] if version else ''}
