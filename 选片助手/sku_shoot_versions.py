"""款末标签的稳定重拍版本；原文款号与照片实际 SKU 分开保存。"""
import string

from sku_match import normalize


class VersionLimitError(ValueError):pass


def initialize(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS sku_shoot_versions(
        asset TEXT,normalized TEXT,base_sku TEXT,sku TEXT,version INTEGER,created REAL,
        PRIMARY KEY(asset,normalized));
        CREATE INDEX IF NOT EXISTS shoot_version_base ON sku_shoot_versions(normalized);
        CREATE UNIQUE INDEX IF NOT EXISTS shoot_version_unique ON sku_shoot_versions(normalized,version) WHERE version>0;''')


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
    return {'base_sku':code,'sku':code,'shoot_version':''}


def allocate(c,archive,asset,sid,cid,code,stamp):
    """调用方持有 BEGIN IMMEDIATE；清零保留占号，同一永久标签身份重试不递增。"""
    # 用户重新点击已经分配的带版本款号时，继续使用该标签的原款号。
    assigned=c.execute('SELECT * FROM sku_shoot_versions WHERE asset=? AND sku=?',(asset,code)).fetchone()
    if assigned:return describe(assigned)
    key=normalize(code)
    existing=c.execute('SELECT * FROM sku_shoot_versions WHERE asset=? AND normalized=?',(asset,key)).fetchone()
    if existing:return describe(existing)
    variants=[code]+[code+'-'+letter for letter in string.ascii_uppercase]
    keys=[normalize(value) for value in variants]
    slots=','.join('?' for _ in keys)
    with archive.connect() as db:
        assets=db.execute('SELECT id,sku FROM assets WHERE normalized IN ('+slots+')',keys).fetchall()
    prior=c.execute('SELECT sku FROM ocr_choices WHERE session_id=? AND capture_id=?',(sid,cid)).fetchone()
    # 升级不改名已有确认，也不改写同一照片的历史原 SKU。
    keep=bool(prior and normalize(prior['sku'])==key) or any(row['id']==asset and normalize(row['sku'])==key for row in assets)
    used={normalize(row['sku']) for row in assets if row['id']!=asset}
    used.update(normalize(row['sku']) for row in c.execute('SELECT sku FROM ocr_choices WHERE session_id!=? OR capture_id!=?',(sid,cid)))
    used.update(normalize(row['sku']) for row in c.execute('SELECT sku FROM sku_shoot_versions WHERE normalized=?',(key,)))
    # 文档中真实存在的 -A 等款号同样不能被重拍版本占用。
    used.update(row[0] for row in c.execute('SELECT normalized FROM sku_rows WHERE normalized IN ('+slots+')',keys) if row[0]!=key)
    version=0
    if not keep:
        version=next((index for index,value in enumerate(keys) if value not in used),None)
        if version is None:raise VersionLimitError('此款重拍版本 A–Z 已用完，请使用新的原款号')
    selected=variants[version]
    if len(selected)>80:raise VersionLimitError('原款号过长，追加重拍版本后不能超过 80 字符')
    c.execute('INSERT INTO sku_shoot_versions VALUES (?,?,?,?,?,?)',(asset,key,code,selected,version,stamp))
    return {'base_sku':code,'sku':selected,'shoot_version':string.ascii_uppercase[version-1] if version else ''}
