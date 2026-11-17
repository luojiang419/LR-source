"""OCR 文本、目录款号与文件指纹的数据库证据；不写入原图或侧车。"""
import json
from pathlib import Path
import re
import time

from retrieval_schema import digest,dumps,tokens
from sku_match import normalize
from sku_rules import match_rules,reading_tokens,length_allowed,normalized_text

AUTO_SOURCES=('ocr_folder','folder_default')


def install(c):
    c.executescript('''
CREATE TABLE IF NOT EXISTS intelligence_ocr_evidence(
 item TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,root TEXT NOT NULL,folder TEXT NOT NULL,
 rules TEXT NOT NULL,folder_codes TEXT NOT NULL,ocr_codes TEXT NOT NULL,status TEXT NOT NULL,sku TEXT NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS intelligence_ocr_folder ON intelligence_ocr_evidence(root,folder,rules,status);
CREATE TABLE IF NOT EXISTS intelligence_ocr_dirty(root TEXT,folder TEXT,PRIMARY KEY(root,folder));
CREATE TABLE IF NOT EXISTS intelligence_ocr_groups(
 id TEXT PRIMARY KEY,root TEXT NOT NULL,folder TEXT NOT NULL,rules TEXT NOT NULL,status TEXT NOT NULL,
 sku TEXT NOT NULL,reason TEXT NOT NULL,total INTEGER NOT NULL,processed INTEGER NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS intelligence_ocr_group_folder ON intelligence_ocr_groups(root,folder);
CREATE TABLE IF NOT EXISTS catalog_sku_writeback(
 item TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,sku TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',
 operation TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',written_fingerprint TEXT NOT NULL DEFAULT '',request_key TEXT NOT NULL DEFAULT '',updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS catalog_sku_writeback_queue ON catalog_sku_writeback(status,updated);
CREATE TABLE IF NOT EXISTS intelligence_file_fingerprints(
 item TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,input_sha TEXT NOT NULL,content_sha TEXT NOT NULL,
 stage TEXT NOT NULL,created REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS intelligence_ocr_evidence_delete AFTER DELETE ON items BEGIN
 INSERT INTO intelligence_ocr_dirty VALUES(old.root,old.folder) ON CONFLICT(root,folder) DO NOTHING;
 DELETE FROM intelligence_ocr_evidence WHERE item=old.id;
 DELETE FROM intelligence_file_fingerprints WHERE item=old.id; END;
CREATE TRIGGER IF NOT EXISTS intelligence_ocr_version AFTER UPDATE OF fingerprint,online ON items
 WHEN old.fingerprint IS NOT new.fingerprint OR old.online IS NOT new.online BEGIN
 INSERT INTO intelligence_ocr_dirty VALUES(new.root,new.folder) ON CONFLICT(root,folder) DO NOTHING;
 UPDATE items SET sku='',normalized='',source='unmatched',confidence=NULL
 WHERE root=new.root AND online IN (0,1) AND folder=new.folder AND source IN ('ocr_folder','folder_default');
 UPDATE intelligence_ocr_groups SET status='pending',reason='目录图片版本或在线状态已变化，等待重新核验'
 WHERE root=new.root AND folder=new.folder; END;
CREATE TRIGGER IF NOT EXISTS intelligence_ocr_new_member AFTER INSERT ON items
 WHEN EXISTS(SELECT 1 FROM intelligence_ocr_groups WHERE root=new.root AND folder=new.folder AND status IN ('confirmed','folder_default')) BEGIN
 INSERT INTO intelligence_ocr_dirty VALUES(new.root,new.folder) ON CONFLICT(root,folder) DO NOTHING;
 UPDATE items SET sku='',normalized='',source='unmatched',confidence=NULL
 WHERE root=new.root AND online IN (0,1) AND folder=new.folder AND source IN ('ocr_folder','folder_default');
 UPDATE intelligence_ocr_groups SET status='pending',reason='目录新增图片，等待补齐 OCR'
 WHERE root=new.root AND folder=new.folder; END;
''')
    for name in ('intelligence_ocr_version','intelligence_ocr_new_member'):
        sql=c.execute("SELECT sql FROM sqlite_master WHERE name=?",(name,)).fetchone()[0]
        old="WHERE root=new.root AND folder=new.folder AND source IN ('ocr_folder','folder_default')"
        if old in sql:
            c.execute('SAVEPOINT ocr_folder_lookup_upgrade')
            c.execute('DROP TRIGGER '+name);c.execute(sql.replace(old,old.replace('AND folder=', 'AND online IN (0,1) AND folder=')))
            c.execute('RELEASE ocr_folder_lookup_upgrade')
    # 仅本程序已验证的元数据写入可保留 OCR 证据；外部文件变化仍使归属失效。
    trigger=c.execute("SELECT sql FROM sqlite_master WHERE name='intelligence_ocr_version'").fetchone()[0]
    if 'catalog_sku_writeback' not in trigger:
        previous='WHEN old.fingerprint IS NOT new.fingerprint OR old.online IS NOT new.online BEGIN'
        replacement="""WHEN old.online IS NOT new.online OR (old.fingerprint IS NOT new.fingerprint AND
 NOT EXISTS(SELECT 1 FROM catalog_sku_writeback w WHERE w.item=new.id AND w.fingerprint=old.fingerprint
 AND w.written_fingerprint=new.fingerprint)) BEGIN"""
        c.execute('SAVEPOINT ocr_metadata_version_upgrade')
        c.execute('DROP TRIGGER intelligence_ocr_version');c.execute(trigger.replace(previous,replacement))
        c.execute('RELEASE ocr_metadata_version_upgrade')
    if not c.execute("SELECT 1 FROM intelligence_settings WHERE key='ocr_immediate_confirmation_v1'").fetchone():
        c.execute("""INSERT OR IGNORE INTO intelligence_ocr_dirty SELECT g.root,g.folder FROM intelligence_ocr_groups g
 WHERE g.status='pending' AND EXISTS(SELECT 1 FROM intelligence_ocr_evidence e
 WHERE e.root=g.root AND e.folder=g.folder AND e.rules=g.rules AND e.status='matched')""")
        c.execute("INSERT INTO intelligence_settings VALUES('ocr_immediate_confirmation_v1','1')")
    c.execute("""CREATE TRIGGER IF NOT EXISTS catalog_sku_writeback_changed AFTER UPDATE OF sku,source,fingerprint,online ON items
 WHEN (old.sku IS NOT new.sku OR old.source IS NOT new.source OR old.fingerprint IS NOT new.fingerprint OR old.online IS NOT new.online)
 AND NOT EXISTS(SELECT 1 FROM catalog_sku_writeback w WHERE w.item=new.id AND w.fingerprint=old.fingerprint AND w.written_fingerprint=new.fingerprint)
 BEGIN UPDATE catalog_sku_writeback SET status='queued',reason='',updated=strftime('%s','now') WHERE item=new.id; END""")


def readings(frames):
    return [frame.get('reading',{}) for frame in frames if isinstance(frame.get('reading'),dict)]


def text(frames):
    return '\n'.join(str(value) for reading in readings(frames) for value in
                     [reading.get('text',''),*(reading.get('codes') or []),*(reading.get('ocr_candidates') or [])] if value)


def rebuild_search(c,item,*,schema='main'):
    assert schema in ('main','retrieval')
    prefix=schema+'.';values=[]
    for table in ('intelligence_ocr','intelligence_results'):
        row=c.execute('SELECT r.frames FROM '+prefix+table+' r JOIN '+prefix+'items i ON i.id=r.item AND i.fingerprint=r.fingerprint WHERE r.item=?',(item,)).fetchone()
        if row:
            frames=json.loads(row[0])
            values.append(text(frames) if table=='intelligence_ocr' else ' '.join(dumps(frame['result']) for frame in frames))
    expression='item:"'+item.replace('"','""')+'"'
    c.execute('DELETE FROM '+prefix+'intelligence_fts WHERE rowid IN (SELECT rowid FROM '+prefix+
              'intelligence_fts WHERE intelligence_fts MATCH ?) AND item=?',(expression,item))
    if values:c.execute('INSERT INTO '+prefix+'intelligence_fts(item,terms) VALUES(?,?)',(item,' '.join(tokens(' '.join(values)))))


def _codes(values,config,*,lc=False):
    matches=match_rules(values,config['rules'])['exact']
    if lc:
        def lc_codes(tokens):return [code for code in tokens if re.fullmatch(r'LC[A-Z0-9._/-]*\d[A-Z0-9._/-]*',code)]
        matches+=lc_codes(reading_tokens([{'text':v.get('text','')} for v in values])) or lc_codes(reading_tokens(values))
    result={}
    for code in matches:
        if length_allowed(code,config['length_filter']):result.setdefault(normalize(code),code)
    return list(result.values())


def folder_codes(row,config):
    # 目录名只能作独立证据，不能把文件名或相机序号拼成款号。
    path=Path(row['path']).parent
    for part in [path.name,*[p.name for p in path.parents if p.name]]:
        codes=_codes([{'text':normalized_text(part)}],config,lc=True)
        if codes:return codes
    return []


def save_evidence(c,row,frames,config):
    key=digest(config);folder=folder_codes(row,config);codes=_codes(readings(frames),config,lc=True)
    if len(folder)>1 or len(codes)>1:status='conflict';sku=''
    elif folder and codes:
        status='matched' if normalize(folder[0])==normalize(codes[0]) else 'conflict'
        sku=codes[0] if status=='matched' else ''
    else:status='no_label' if not codes else 'review';sku=''
    visual=c.execute('SELECT frames FROM intelligence_results WHERE item=? AND fingerprint=?',(row['id'],row['fingerprint'])).fetchone()
    if visual and len(folder)==1:
        for frame in json.loads(visual[0]):
            value=frame['result'];visible=_codes([{'text':value.get('visible_text',''),'codes':value.get('sku_candidates',[])}],config,lc=True)
            if any(normalize(code)!=normalize(folder[0]) for code in visible):status='conflict';sku=''
    c.execute('INSERT OR REPLACE INTO intelligence_ocr_evidence VALUES(?,?,?,?,?,?,?,?,?,?)',
              (row['id'],row['fingerprint'],row['root'],row['folder'],key,dumps(folder),dumps(codes),status,sku,time.time()))
    rebuild_search(c,row['id'])
    reconcile_folder(c,row['root'],row['folder'],config)


def reconcile_folder(c,root,folder,config):
    key=digest(config)
    rows=[dict(r) for r in c.execute('''SELECT i.id,i.fingerprint,i.sku,i.source,e.folder_codes,e.ocr_codes,e.status,
 e.fingerprint AS evidence_version FROM items i INDEXED BY catalog_items_root_folder LEFT JOIN intelligence_ocr_evidence e ON e.item=i.id AND e.rules=?
 WHERE i.root=? AND i.online=1 AND i.folder=? AND i.media_type='image' ''',(key,root,folder))]
    valid=[r for r in rows if r['evidence_version']==r['fingerprint']]
    directory={normalize(code):code for r in valid for code in json.loads(r['folder_codes'])}
    codes={normalize(code):code for r in valid for code in json.loads(r['ocr_codes'])}
    status='pending';sku='';reason='等待目录内全部图片完成 OCR，尚未采用目录默认款号'
    if len(directory)>1 or len(codes)>1 or any(r['status']=='conflict' for r in valid):
        status='conflict';reason='目录名与 OCR 款号不一致或存在多个款号，请核验'
    elif rows and (len(valid)==len(rows) or len(directory)==1 and codes and set(directory)==set(codes)):
        if len(directory)==1 and (not codes or set(directory)==set(codes)):
            sku=next(iter(directory.values()));status='confirmed' if codes else 'folder_default'
            reason='目录与 OCR 款号一致' if codes else '目录内未识别到款号图，采用目录名 LC 款号'
            if not codes and not normalize(sku).startswith('LC'):status='review';sku='';reason='无款号图时只采用目录名中的唯一 LC 款号'
            if any(r['sku'] and r['source'] not in (*AUTO_SOURCES,'path_rule','unmatched','review') and normalize(r['sku'])!=normalize(sku) for r in rows):
                status='conflict';sku='';reason='目录推断与已有人工或内嵌 SKU 冲突，保留原归属'
        else:status='review';reason='缺少唯一有效目录款号，保留 OCR 文本等待核验'
    # 正向标签与目录互证即可确认；无标签的目录默认值才要求完整覆盖。
    # 迟到冲突仍撤销自动归属，人工值保留。
    if sku:
        source='ocr_folder' if status=='confirmed' else 'folder_default'
        c.execute("UPDATE items INDEXED BY catalog_items_root_folder SET sku=?,normalized=?,source=?,confidence=? WHERE root=? AND folder=? AND online=1 AND media_type='image' AND source IN ('ocr_folder','folder_default','path_rule','unmatched','review') AND (sku<>? OR source<>?)",
                  (sku,normalize(sku),source,1.0 if status=='confirmed' else .78,root,folder,sku,source))
    else:
        c.execute("UPDATE items INDEXED BY catalog_items_root_folder SET sku='',normalized='',source='unmatched',confidence=NULL WHERE root=? AND online IN (0,1) AND folder=? AND source IN ('ocr_folder','folder_default')",(root,folder))
        if status=='conflict':
            c.execute("UPDATE items INDEXED BY catalog_items_root_folder SET sku='',normalized='',source='review',confidence=NULL WHERE root=? AND online IN (0,1) AND folder=? AND source='path_rule'",(root,folder))
    c.execute('INSERT OR REPLACE INTO intelligence_ocr_groups VALUES(?,?,?,?,?,?,?,?,?,?)',
              (digest([root,folder]),root,folder,key,status,sku,reason,len(rows),len(valid),time.time()))
    if sku:
        queue_metadata(c,root,folder)


def queue_metadata(c,root,folder):
    c.execute("""INSERT INTO catalog_sku_writeback(item,fingerprint,sku,status,updated)
 SELECT id,fingerprint,sku,'queued',? FROM items INDEXED BY catalog_items_root_folder
 WHERE root=? AND online=1 AND folder=? AND media_type='image' AND sku<>'' AND source IN ('ocr_folder','folder_default')
 ON CONFLICT(item) DO UPDATE SET status='queued',reason='',updated=excluded.updated
 WHERE catalog_sku_writeback.sku<>excluded.sku OR catalog_sku_writeback.fingerprint<>excluded.fingerprint""",(time.time(),root,folder))


def save_ocr(c,row,config_key,model,frames,config,input_sha=''):
    current=c.execute('SELECT i.fingerprint,i.online FROM items i JOIN roots r ON r.id=i.root AND r.removed=0 WHERE i.id=?',(row['id'],)).fetchone()
    if not current or not current['online'] or current['fingerprint']!=row['fingerprint']:raise ValueError('图片版本已变化，未保存旧 OCR 结果')
    c.execute('INSERT OR REPLACE INTO intelligence_ocr VALUES(?,?,?,?,?,?)',(row['id'],row['fingerprint'],config_key,model,dumps(frames),time.time()))
    if input_sha:
        c.execute('''INSERT INTO intelligence_file_fingerprints VALUES(?,?,?,?,?,?) ON CONFLICT(item) DO UPDATE SET
 fingerprint=excluded.fingerprint,input_sha=excluded.input_sha,content_sha=CASE WHEN fingerprint=excluded.fingerprint THEN content_sha ELSE '' END,
 stage=CASE WHEN fingerprint=excluded.fingerprint AND content_sha<>'' THEN stage ELSE excluded.stage END,created=excluded.created''',
                  (row['id'],row['fingerprint'],input_sha,'','ocr',time.time()))
    save_evidence(c,row,frames,config)
    c.execute('UPDATE intelligence_meta SET generation=generation+1 WHERE id=1')


def backfill(c,config,limit=32):
    key=digest(config)
    if not c.execute("SELECT 1 FROM intelligence_settings WHERE key='ocr_metadata_seed_v1'").fetchone():
        for folder in c.execute("SELECT root,folder FROM intelligence_ocr_groups WHERE status IN ('confirmed','folder_default')").fetchall():
            queue_metadata(c,*folder)
        c.execute("INSERT INTO intelligence_settings VALUES('ocr_metadata_seed_v1','1')")
    folders=c.execute('SELECT root,folder FROM intelligence_ocr_dirty LIMIT 8').fetchall()
    for folder in folders:
        # 未涉及 OCR 的大量老目录不创建无用分组记录。
        if c.execute('SELECT 1 FROM intelligence_ocr_evidence WHERE root=? AND folder=? LIMIT 1',tuple(folder)).fetchone():reconcile_folder(c,*folder,config)
        c.execute('DELETE FROM intelligence_ocr_dirty WHERE root=? AND folder=?',tuple(folder))
    ready=c.execute("SELECT value FROM intelligence_settings WHERE key='ocr_backfill_rules'").fetchone()
    if ready and ready[0]==key:return 0
    rows=[dict(r) for r in c.execute('''SELECT i.*,o.frames FROM intelligence_ocr o CROSS JOIN items i ON i.id=o.item AND i.fingerprint=o.fingerprint
 LEFT JOIN intelligence_ocr_evidence e ON e.item=i.id WHERE i.online=1 AND (e.item IS NULL OR e.fingerprint<>i.fingerprint OR e.rules<>?) LIMIT ?''',(key,limit))]
    for row in rows:save_evidence(c,row,json.loads(row['frames']),config)
    if len(rows)<limit:c.execute("INSERT OR REPLACE INTO intelligence_settings VALUES('ocr_backfill_rules',?)",(key,))
    return len(rows)


def verify_visual(c,row,value,config):
    group=c.execute('SELECT * FROM intelligence_ocr_groups WHERE root=? AND folder=?',(row['root'],row['folder'])).fetchone()
    evidence=c.execute('SELECT * FROM intelligence_ocr_evidence WHERE item=? AND fingerprint=?',(row['id'],row['fingerprint'])).fetchone()
    codes=_codes([{'text':value.get('visible_text',''),'codes':value.get('sku_candidates',[])}],config,lc=True)
    expected=group['sku'] if group and group['rules']==digest(config) else ''
    if expected and codes and any(normalize(code)!=normalize(expected) for code in codes):
        c.execute("UPDATE intelligence_ocr_evidence SET status='conflict',updated=? WHERE item=?",(time.time(),row['id']))
        c.execute("UPDATE intelligence_ocr_groups SET status='conflict',reason='视觉文字与目录/OCR 款号冲突',sku='',updated=? WHERE root=? AND folder=?",(time.time(),row['root'],row['folder']))
        c.execute("UPDATE items INDEXED BY catalog_items_root_folder SET sku='',normalized='',source='unmatched',confidence=NULL WHERE root=? AND online IN (0,1) AND folder=? AND source IN ('ocr_folder','folder_default')",(row['root'],row['folder']))
    return {'sku':expected,'visual_codes':codes,'ocr_status':evidence['status'] if evidence else 'pending'}


def details(c,row):
    ocr=c.execute('SELECT * FROM intelligence_ocr WHERE item=? AND fingerprint=?',(row['id'],row['fingerprint'])).fetchone()
    group=c.execute('SELECT * FROM intelligence_ocr_groups WHERE root=? AND folder=?',(row['root'],row['folder'])).fetchone()
    fingerprint=c.execute('SELECT * FROM intelligence_file_fingerprints WHERE item=? AND fingerprint=?',(row['id'],row['fingerprint'])).fetchone()
    writeback=c.execute('SELECT status,reason,operation FROM catalog_sku_writeback WHERE item=?',(row['id'],)).fetchone()
    frames=json.loads(ocr['frames']) if ocr else []
    return {'text':text(frames),'frames':frames,'model':ocr['model'] if ocr else '',
            'validation':dict(group) if group else None,'fingerprints':dict(fingerprint) if fingerprint else None,
            'writeback':dict(writeback) if writeback else None}
