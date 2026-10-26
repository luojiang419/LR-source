"""现场大目录的持久轻量索引、全目录筛选和有界照片读取。"""
import json
import hashlib
from functools import lru_cache
from pathlib import Path

LARGE_SESSION=2000
PAGE_SIZE=240


def install(connection):
    connection.executescript('''
CREATE INDEX IF NOT EXISTS captures_position ON captures(session,position,id);
CREATE TABLE IF NOT EXISTS browser_captures(
 session TEXT,id TEXT,position INTEGER,time TEXT,epoch REAL,missing INTEGER,camera TEXT,
 name TEXT,raw TEXT,jpg TEXT,existing INTEGER,live INTEGER,files INTEGER,search TEXT,fingerprint TEXT,folder TEXT,
 PRIMARY KEY(session,id));
CREATE INDEX IF NOT EXISTS browser_time ON browser_captures(session,missing,epoch,position);
CREATE INDEX IF NOT EXISTS browser_position ON browser_captures(session,position,id);
CREATE INDEX IF NOT EXISTS browser_raw ON browser_captures(session,raw COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS browser_jpg ON browser_captures(session,jpg COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS browser_folder ON browser_captures(session,folder,position);
CREATE TABLE IF NOT EXISTS browser_versions(session TEXT PRIMARY KEY,generation INTEGER DEFAULT 0,data_generation INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS browser_groups(session TEXT,id TEXT,group_id INTEGER,group_position INTEGER,group_size INTEGER,PRIMARY KEY(session,id));
CREATE INDEX IF NOT EXISTS browser_group_order ON browser_groups(session,group_id,group_position);
CREATE TABLE IF NOT EXISTS browser_group_state(session TEXT PRIMARY KEY,generation INTEGER,threshold REAL);
CREATE TABLE IF NOT EXISTS browser_shared(session TEXT,capture TEXT,rating INTEGER,PRIMARY KEY(session,capture));
CREATE TABLE IF NOT EXISTS browser_shared_state(session TEXT PRIMARY KEY,source TEXT,version INTEGER);
CREATE TABLE IF NOT EXISTS browser_info(key TEXT PRIMARY KEY,value TEXT);
CREATE TRIGGER IF NOT EXISTS browser_index_insert AFTER INSERT ON browser_captures BEGIN
 INSERT INTO browser_versions(session,generation,data_generation) VALUES(new.session,1,1) ON CONFLICT(session) DO UPDATE SET generation=generation+1,data_generation=data_generation+1;
END;
CREATE TRIGGER IF NOT EXISTS browser_index_delete AFTER DELETE ON browser_captures BEGIN
 INSERT INTO browser_versions(session,generation,data_generation) VALUES(old.session,1,1) ON CONFLICT(session) DO UPDATE SET generation=generation+1,data_generation=data_generation+1;
END;
CREATE TRIGGER IF NOT EXISTS browser_rating_update AFTER UPDATE ON browser_captures
WHEN old.existing IS NOT new.existing OR old.live IS NOT new.live OR old.files IS NOT new.files OR old.raw IS NOT new.raw OR old.jpg IS NOT new.jpg BEGIN
 UPDATE browser_versions SET data_generation=data_generation+1 WHERE session=new.session;
END;
CREATE TRIGGER IF NOT EXISTS browser_index_update AFTER UPDATE ON browser_captures
WHEN old.position IS NOT new.position OR old.time IS NOT new.time OR old.camera IS NOT new.camera OR old.missing IS NOT new.missing BEGIN
 UPDATE browser_versions SET generation=generation+1 WHERE session=new.session;
END;
''')
    columns='session,id,position,time,epoch,missing,camera,name,raw,jpg,existing,live,files,search,fingerprint,folder'
    def projection(prefix):
        d=prefix+'data';j=lambda key:"json_extract("+d+",'$."+key+"')"
        t=j('time');raw=j('raw');jpg=j('jpg')
        date="CASE WHEN substr("+t+",-1) IN ('Z','z') OR instr(substr("+t+",11),'+') OR instr(substr("+t+",11),'-') THEN julianday("+t+") ELSE julianday("+t+",'utc') END"
        path="replace(coalesce("+raw+','+jpg+",''),'\\','/')"
        parts="('['||replace(json_quote("+path+"),'/', '\",\"')||']')"
        folder="coalesce((SELECT group_concat(value,'/') FROM json_each("+parts+") WHERE key<json_array_length("+parts+")-1),'')"
        return ','.join((prefix+'session',prefix+'id',prefix+'position',t,
            '(('+date+')-2440587.5)*86400.0',
            'CASE WHEN '+j('missingTime')+' OR julianday('+t+') IS NULL THEN 1 ELSE 0 END',
            'coalesce(nullif('+j('cameraKey')+",''),"+j('camera')+",'')",j('name'),raw,jpg,
            'coalesce('+j('existingRating')+',0)',j('liveRating'),
            '(CASE WHEN '+raw+' IS NOT NULL THEN 1 ELSE 0 END+CASE WHEN '+jpg+' IS NOT NULL THEN 1 ELSE 0 END)',
            "lower(replace(coalesce("+raw+",'')||' '||coalesce("+jpg+",'')||' '||coalesce("+j('name')+",'')||' '||"+prefix+"id,'\\','/'))",j('fingerprint'),folder))
    updates=','.join(k+'=excluded.'+k for k in columns.split(',')[2:])
    for event in ('INSERT','UPDATE'):
        connection.execute('CREATE TRIGGER IF NOT EXISTS captures_browser_'+event.lower()+' AFTER '+event+' ON captures BEGIN INSERT INTO browser_captures('+columns+') VALUES ('+projection('new.')+') ON CONFLICT(session,id) DO UPDATE SET '+updates+'; END')
    connection.execute('CREATE TRIGGER IF NOT EXISTS captures_browser_delete AFTER DELETE ON captures BEGIN DELETE FROM browser_captures WHERE session=old.session AND id=old.id; END')
    if not connection.execute("SELECT 1 FROM browser_info WHERE key='schema'").fetchone():
        connection.execute('INSERT OR REPLACE INTO browser_captures('+columns+') SELECT '+projection('')+' FROM captures')
        connection.execute("INSERT INTO browser_info VALUES ('schema','1')")


def count(store,sid):
    with store.connect() as db:return db.execute('SELECT count(*) FROM browser_captures WHERE session=?',(sid,)).fetchone()[0]


@lru_cache(maxsize=65536)
def file_pair_key(path):
    from asset_catalog import pair_key
    return pair_key(path)


def ensure_shared(db,sid,archive):
    source=str(archive.db);version=db.execute('SELECT value FROM rating_data.rating_view_version WHERE id=1').fetchone()[0]
    old=db.execute('SELECT source,version FROM browser_shared_state WHERE session=?',(sid,)).fetchone()
    if old and tuple(old)==(source,version):return version
    db.create_function('photo_pair_key',1,file_pair_key,deterministic=True)
    db.execute('DELETE FROM browser_shared WHERE session=?',(sid,))
    db.execute('''INSERT INTO browser_shared
WITH candidates AS (
 SELECT p.capture,r.rating,r.updated,1 AS direct FROM rating_data.rating_capture_paths p
 JOIN rating_data.shared_ratings r ON r.path_key=p.path_key WHERE p.session=:sid
 UNION ALL
 SELECT p.capture,r.rating,r.updated,0 FROM rating_data.catalog_links p
 JOIN rating_data.files f ON f.asset=p.asset JOIN rating_data.locations l ON l.file=f.id
 JOIN rating_data.shared_ratings r ON r.path_key=photo_pair_key(l.path) WHERE p.session=:sid
 UNION ALL
 SELECT p.capture,r.rating,r.updated,0 FROM rating_data.catalog_links p
 JOIN rating_data.catalog_links other ON other.asset=p.asset
 JOIN rating_data.rating_capture_paths paths ON paths.session=other.session AND paths.capture=other.capture
 JOIN rating_data.shared_ratings r ON r.path_key=paths.path_key WHERE p.session=:sid),
ranked AS (SELECT capture,rating,row_number() OVER (PARTITION BY capture ORDER BY updated DESC,direct DESC) AS n FROM candidates)
SELECT :sid,capture,rating FROM ranked WHERE n=1''',{'sid':sid})
    db.execute('INSERT OR REPLACE INTO browser_shared_state VALUES (?,?,?)',(sid,source,version))
    return version


def ensure_groups(db,sid,threshold):
    version=db.execute('SELECT generation FROM browser_versions WHERE session=?',(sid,)).fetchone()
    generation=version[0] if version else 0
    old=db.execute('SELECT generation,threshold FROM browser_group_state WHERE session=?',(sid,)).fetchone()
    if old and tuple(old)==(generation,threshold):return
    db.execute('DELETE FROM browser_groups WHERE session=?',(sid,))
    db.execute('''INSERT INTO browser_groups
WITH previous AS (
 SELECT id,position,epoch,missing,camera,lag(epoch) OVER w AS prior_time,
 lag(missing) OVER w AS prior_missing,lag(camera) OVER w AS prior_camera
 FROM browser_captures WHERE session=:sid WINDOW w AS (ORDER BY position,id)),
boundaries AS (SELECT *,CASE WHEN prior_camera IS NULL OR missing OR prior_missing OR camera!=prior_camera OR epoch-prior_time>:threshold THEN 1 ELSE 0 END AS boundary FROM previous),
grouped AS (SELECT id,position,sum(boundary) OVER (ORDER BY position,id) AS group_id FROM boundaries)
SELECT :sid,id,group_id,row_number() OVER (PARTITION BY group_id ORDER BY position,id),count(*) OVER (PARTITION BY group_id) FROM grouped''',{'sid':sid,'threshold':threshold})
    db.execute('INSERT OR REPLACE INTO browser_group_state VALUES (?,?,?)',(sid,generation,threshold))


def read(store,sid,query=None,archive=None):
    query=query or {};session=store.session(sid)
    if not session or store.is_removed(sid):raise ValueError('文件夹会话不存在或已移除')
    state=session['state'];threshold=float(query.get('threshold',state.get('threshold',4)));keep=int(query.get('keep',state.get('keep',1)))
    if not 0<threshold<=60 or keep not in (1,2):raise ValueError('时间规则无效')
    text=str(query.get('q',state.get('query','')))[:300];folder=str(query.get('folder',state.get('folderPrefix',''))).replace('\\','/').strip('/')
    if '..' in folder.split('/'):raise ValueError('目录路径无效')
    selected_filter=query.get('filter',state.get('filter','all'));sort=query.get('sort',state.get('sort','time-asc'))
    if selected_filter not in ('all','picked','unpicked') or sort not in ('time-asc','time-desc','rating-desc'):raise ValueError('浏览排序或筛选无效')
    offset=max(0,int(query.get('offset',0)));limit=min(PAGE_SIZE,max(1,int(query.get('limit',PAGE_SIZE))))
    params={'sid':sid,'keep':keep,'offset':offset,'limit':limit}
    if archive is not None:archive.ratings  # 首次惰性初始化共享评级表，再附加只读查询。
    with store.browser_lock,store.connect() as db:
        db.create_function('unicode_fold',1,lambda value:str(value or '').casefold(),deterministic=True)
        ensure_groups(db,sid,threshold)
        shared='SELECT NULL AS capture,NULL AS rating WHERE 0'
        shared_version=-1
        if archive is not None:
            db.execute('ATTACH DATABASE ? AS rating_data',(str(archive.db),))
            shared_version=ensure_shared(db,sid,archive)
            shared='SELECT capture,rating FROM browser_shared WHERE session=:sid'
        cte='''WITH manual AS MATERIALIZED (SELECT key,value FROM json_each((SELECT state FROM sessions WHERE id=:sid),'$.overrides')),
shared AS MATERIALIZED ('''+shared+'''), rows AS (
 SELECT p.*,g.group_id,g.group_position,g.group_size,
 s.rating AS shared_rating,
 (s.rating IS NULL AND m.value IS NULL AND p.live IS NULL) AS automatic,
 coalesce(s.rating,m.value,p.live,max(p.existing,CASE WHEN p.missing OR g.group_position>g.group_size-:keep THEN 1 ELSE 0 END)) AS rating
 FROM browser_captures p JOIN browser_groups g ON g.session=p.session AND g.id=p.id
 LEFT JOIN manual m ON m.key=p.id LEFT JOIN shared s ON s.capture=p.id WHERE p.session=:sid) '''
        revisions=tuple(db.execute('SELECT generation,data_generation FROM browser_versions WHERE session=?',(sid,)).fetchone() or (0,0))
        total_key=(revisions,shared_version,threshold,keep,hashlib.sha256(json.dumps(state.get('overrides',{}),ensure_ascii=False).encode()).digest())
        cached=store.browser_totals.get(sid)
        if cached and cached[0]==total_key:totals=cached[1]
        else:
            totals=tuple(db.execute(cte+'SELECT count(*),coalesce(sum(rating>0),0),coalesce(sum(files),0) FROM rows',params).fetchone())
            if len(store.browser_totals)>=16:store.browser_totals.pop(next(iter(store.browser_totals)))
            store.browser_totals[sid]=(total_key,totals)
        conditions=[]
        def escaped(value):return value.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        if text:conditions.append("unicode_fold(search) LIKE :q ESCAPE '\\'");params['q']='%'+escaped(text.replace('\\','/').casefold())+'%'
        if folder:conditions.append("(unicode_fold(replace(coalesce(raw,''),'\\','/')) LIKE :folder ESCAPE '\\' OR unicode_fold(replace(coalesce(jpg,''),'\\','/')) LIKE :folder ESCAPE '\\')");params['folder']=escaped(folder.casefold())+'/%'
        if selected_filter!='all':conditions.append('rating>0' if selected_filter=='picked' else 'rating<=0')
        tail=query.get('tail_capture')
        if tail:
            group=db.execute('SELECT group_id FROM browser_groups WHERE session=? AND id=?',(sid,tail)).fetchone()
            if not group:raise ValueError('照片已不存在')
            conditions=['group_id=:tail_group'];params['tail_group']=group[0];limit=2;params['limit']=2;offset=0
        where=' WHERE '+' AND '.join(conditions) if conditions else ''
        matched=db.execute(cte+'SELECT count(*) FROM rows'+where,params).fetchone()[0] if conditions else totals[0]
        order=('rating DESC,' if sort=='rating-desc' else '')+'missing,epoch '+('DESC' if sort=='time-desc' else 'ASC')+',position,id'
        if tail:order='position DESC,id DESC'
        around=None if tail else query.get('around') or (state.get('selectedId') if 'offset' not in query else None)
        focus=None;step=int(query.get('step',0))
        if step not in (-1,0,1):raise ValueError('照片导航步长无效')
        if around:
            located=db.execute(cte+'SELECT n FROM (SELECT id,row_number() OVER (ORDER BY '+order+')-1 AS n FROM rows'+where+') WHERE id=:around',dict(params,around=around)).fetchone()
            if located:
                target=min(max(0,located[0]+step),max(0,matched-1));offset=target//limit*limit
                if step:
                    focus_row=db.execute(cte+'SELECT id FROM rows'+where+' ORDER BY '+order+' LIMIT 1 OFFSET :target',dict(params,target=target)).fetchone()
                    focus=focus_row[0] if focus_row else None
        if matched:offset=min(offset,(matched-1)//limit*limit)
        else:offset=0
        params['offset']=offset
        rows=db.execute(cte+'SELECT id,rating,shared_rating,automatic,live,group_id,group_position,group_size,position,epoch FROM rows'+where+' ORDER BY '+order+' LIMIT :limit OFFSET :offset',params).fetchall()
        if tail:rows=list(reversed(rows))
        ids=[r['id'] for r in rows]
        records={r['id']:r for r in store.records(sid,ids=ids)}
        result=[]
        for row in rows:
            gap=db.execute('SELECT :epoch-epoch FROM browser_captures WHERE session=:sid AND (position,id)<(:position,:id) ORDER BY position DESC,id DESC LIMIT 1',{'sid':sid,'position':row['position'],'id':row['id'],'epoch':row['epoch']}).fetchone()
            record=records[row['id']];record.update(liveRating=row['rating'],browserLive=row['live'] is not None,browserAutomatic=bool(row['automatic']),browserGroup=row['group_id'],browserGroupSize=row['group_size'],browserPosition=row['position'],browserGap=round(gap[0],3) if gap and gap[0] is not None else None)
            result.append(record)
        session['state']=dict(state)
        for key in ('overrides','rejectedPrevious','skuMarkers'):session['state'][key]={k:v for k,v in state.get(key,{}).items() if k in records}
        session['state'].update(query=text,folderPrefix=folder,filter=selected_filter,sort=sort,keep=keep,threshold=threshold)
        # 当前批次的评级由数据库统一计算，不向浏览器发送整目录共享评级映射。
        session['sharedRatings']={r['id']:r['shared_rating'] for r in rows if r['shared_rating'] is not None}
        session['browser']={'paged':True,'offset':offset,'limit':limit,'total':totals[0],'matched':matched,'picked':totals[1],
                            'files':totals[2],'pagePicked':sum(r['rating']>0 for r in rows),'focusId':focus,'filter':selected_filter,'sort':sort,'query':text,'folderPrefix':folder,
                            'storedOptions':{k:state.get(k,default) for k,default in (('keep',1),('threshold',4),('filter','all'),('query',''),('sort','time-asc'),('folderPrefix',''))}}
    return session,result


def patch_state(store,sid,revision,changes):
    if not isinstance(changes,dict):raise ValueError('会话修改格式无效')
    allowed={'keep','threshold','protectManual','filter','view','selectedId','query','sort','thumbSize','folderPrefix','scope','overrides','rejectedPrevious','skuMarkers'}
    if set(changes)-allowed:raise ValueError('会话包含未知修改项')
    before=store.session(sid)
    if not before or store.is_removed(sid):raise ValueError('文件夹会话不存在或已移除')
    state=dict(before['state'])
    reset_rules=any(key in changes and changes[key]!=state.get(key,default) for key,default in (('keep',1),('threshold',4))) and not changes.get('protectManual',state.get('protectManual',True))
    if reset_rules:
        state['overrides']={};state['rejectedPrevious']={}
    for key,value in changes.items():
        if key in ('overrides','rejectedPrevious','skuMarkers'):
            if not isinstance(value,dict) or len(value)>PAGE_SIZE*2:raise ValueError('一次修改的照片数量过多')
            merged=dict(state.get(key,{}))
            for identity,item in value.items():
                if item is None:merged.pop(identity,None)
                else:merged[identity]=item
            state[key]=merged
        else:state[key]=value
    changed_ids=list(changes.get('overrides',{}))
    if any(type(v) is not int or v not in range(-1,6) for v in state.get('overrides',{}).values()):raise ValueError('评级记录非法')
    if len(store.records(sid,ids=changed_ids))!=len(changed_ids):raise ValueError('修改的照片不存在')
    if state.get('keep',1) not in (1,2) or not 0<float(state.get('threshold',4))<=60:raise ValueError('时间规则无效')
    with store.connect() as db:
        from catalog_store import now
        changed=db.execute('UPDATE sessions SET state=?,revision=revision+1,updated=? WHERE id=? AND revision=?',(json.dumps(state,ensure_ascii=False,separators=(',',':')),now(),sid,revision)).rowcount
        if not changed:raise ValueError('会话已被其他窗口修改，请刷新后再操作。')
    return before,state,revision+1
