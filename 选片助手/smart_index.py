"""帧级语义索引与拍摄时间索引；旧资料仅在后台有界补齐。"""
import json
import re
import sqlite3
import threading
import time

from retrieval_schema import tokens
from search_intent import document

def install(c):
 c.executescript('''
 CREATE TABLE IF NOT EXISTS smart_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
 INSERT OR IGNORE INTO smart_state SELECT 'results_done',CASE WHEN EXISTS(SELECT 1 FROM intelligence_results LIMIT 1) THEN '0' ELSE '1' END;
 INSERT OR IGNORE INTO smart_state SELECT 'dates_done',CASE WHEN
   EXISTS(SELECT 1 FROM catalog_metadata WHERE status='ready' LIMIT 1) OR
   EXISTS(SELECT 1 FROM catalog_metadata INDEXED BY catalog_metadata_queue
     WHERE status IN ('queued','running','failed') AND priority IN (-1,0,1) AND updated>0 LIMIT 1)
   THEN '0' ELSE '1' END;
 CREATE TABLE IF NOT EXISTS smart_frames(id INTEGER PRIMARY KEY,item TEXT NOT NULL,frame INTEGER NOT NULL,
   fingerprint TEXT NOT NULL,result_time REAL NOT NULL,terms TEXT NOT NULL,summary TEXT NOT NULL DEFAULT '',UNIQUE(item,frame));
 CREATE TABLE IF NOT EXISTS smart_pending(item TEXT PRIMARY KEY);
 CREATE VIRTUAL TABLE IF NOT EXISTS smart_fts USING fts5(terms,content='smart_frames',content_rowid='id',tokenize='unicode61');
 CREATE TABLE IF NOT EXISTS smart_wear(frame_id INTEGER NOT NULL,item TEXT NOT NULL,modified REAL NOT NULL,
   person INTEGER NOT NULL,garment INTEGER NOT NULL,gender TEXT NOT NULL,category TEXT NOT NULL,color TEXT NOT NULL,
   PRIMARY KEY(frame_id,person,garment,gender,category,color));
 CREATE INDEX IF NOT EXISTS smart_wear_gender ON smart_wear(gender,category,color,modified DESC,item,frame_id,person);
 CREATE INDEX IF NOT EXISTS smart_wear_color ON smart_wear(category,color,modified DESC,item,frame_id,person);
 CREATE INDEX IF NOT EXISTS smart_wear_category ON smart_wear(category,modified DESC,item,frame_id,person);
 CREATE INDEX IF NOT EXISTS smart_wear_item ON smart_wear(item);
 CREATE TABLE IF NOT EXISTS smart_dates(item TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,shot_time REAL NOT NULL);
 CREATE INDEX IF NOT EXISTS smart_date_time ON smart_dates(shot_time DESC,item);
 CREATE TRIGGER IF NOT EXISTS smart_frame_insert AFTER INSERT ON smart_frames BEGIN
   INSERT INTO smart_fts(rowid,terms) VALUES(new.id,new.terms); END;
 CREATE TRIGGER IF NOT EXISTS smart_frame_delete AFTER DELETE ON smart_frames BEGIN
   INSERT INTO smart_fts(smart_fts,rowid,terms) VALUES('delete',old.id,old.terms);
   DELETE FROM smart_wear WHERE frame_id=old.id; END;
 CREATE TRIGGER IF NOT EXISTS smart_item_delete AFTER DELETE ON items BEGIN
   DELETE FROM smart_frames WHERE item=old.id; DELETE FROM smart_dates WHERE item=old.id; DELETE FROM smart_pending WHERE item=old.id; END;
 CREATE TRIGGER IF NOT EXISTS smart_result_insert AFTER INSERT ON intelligence_results BEGIN
   DELETE FROM smart_frames WHERE item=new.item; INSERT OR IGNORE INTO smart_pending VALUES(new.item);
   UPDATE intelligence_meta SET generation=generation+1 WHERE id=1; END;
 CREATE TRIGGER IF NOT EXISTS smart_result_update AFTER UPDATE ON intelligence_results BEGIN
   DELETE FROM smart_frames WHERE item=new.item; INSERT OR IGNORE INTO smart_pending VALUES(new.item);
   UPDATE intelligence_meta SET generation=generation+1 WHERE id=1; END;
 CREATE TRIGGER IF NOT EXISTS smart_result_delete AFTER DELETE ON intelligence_results BEGIN
   DELETE FROM smart_frames WHERE item=old.item; DELETE FROM smart_pending WHERE item=old.item;
   UPDATE intelligence_meta SET generation=generation+1 WHERE id=1; END;
 CREATE TRIGGER IF NOT EXISTS smart_item_modified AFTER UPDATE OF modified ON items WHEN old.modified<>new.modified BEGIN
   UPDATE smart_wear SET modified=new.modified WHERE item=new.id; END;
 CREATE TRIGGER IF NOT EXISTS smart_item_version AFTER UPDATE OF fingerprint ON items WHEN old.fingerprint<>new.fingerprint BEGIN
   DELETE FROM smart_frames WHERE item=new.id; DELETE FROM smart_dates WHERE item=new.id; END;
 CREATE TRIGGER IF NOT EXISTS smart_date_insert AFTER INSERT ON smart_dates BEGIN
   UPDATE intelligence_meta SET generation=generation+1 WHERE id=1; END;
 CREATE TRIGGER IF NOT EXISTS smart_date_delete AFTER DELETE ON smart_dates BEGIN
   UPDATE intelligence_meta SET generation=generation+1 WHERE id=1; END;
 ''')
 if 'summary' not in {row[1] for row in c.execute('PRAGMA table_info(smart_frames)')}:
  c.execute("ALTER TABLE smart_frames ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
 for name in ('timestamp_ms','source_frame'):
  if name not in {row[1] for row in c.execute('PRAGMA table_info(smart_frames)')}:c.execute(f'ALTER TABLE smart_frames ADD COLUMN {name} INTEGER')
 for event in ('INSERT','UPDATE OF payload,status,fingerprint'):
  name='smart_metadata_insert' if event=='INSERT' else 'smart_metadata_update'
  c.executescript(f'''CREATE TRIGGER IF NOT EXISTS {name} AFTER {event} ON catalog_metadata WHEN new.status='ready' BEGIN
    DELETE FROM smart_dates WHERE item=new.item;
    INSERT INTO smart_dates(item,fingerprint,shot_time)
      SELECT new.item,new.fingerprint,{_time_expression('new.payload')} WHERE {_time_expression('new.payload')} IS NOT NULL;
  END;''')

def _time_expression(payload):
 value=f"(CASE WHEN json_valid({payload}) THEN json_extract({payload},'$.capture.time') END)"
 return f'''CAST(CASE WHEN {value} GLOB '*[+-][0-9][0-9]:[0-9][0-9]' OR lower(substr({value},-1))='z'
   THEN strftime('%s',{value}) ELSE strftime('%s',{value},'-8 hours') END AS REAL)'''

def save_one_frame(c,item,fingerprint,number,frame,row=None):
 row=row or c.execute('SELECT i.modified,i.media_type,r.created FROM items i JOIN intelligence_results r ON r.item=i.id AND r.fingerprint=i.fingerprint WHERE i.id=? AND i.fingerprint=?',(item,fingerprint)).fetchone()
 if not row:return
 c.execute('DELETE FROM smart_frames WHERE item=? AND frame=?',(item,number))
 raw,canonical,wear=document(frame['result'])
 raw+=' '+frame['result'].get('visible_text','')
 words=set(tokens(raw));words.update(re.findall(r'[\u3400-\u9fff]',raw));words.update(canonical)
 stamp=round(float(frame.get('seconds',0))*1000) if row['media_type']=='video' else None
 cursor=c.execute('INSERT INTO smart_frames(item,frame,fingerprint,result_time,terms,summary,timestamp_ms,source_frame) VALUES(?,?,?,?,?,?,?,?)',
   (item,number,fingerprint,row['created'],' '.join(sorted(words)),frame['result'].get('summary','')[:2000],stamp,frame.get('source_frame')))
 c.executemany('INSERT OR IGNORE INTO smart_wear VALUES(?,?,?,?,?,?,?,?)',[(cursor.lastrowid,item,row['modified'],*entry) for entry in wear])

def save_frames(c,item,fingerprint,frames):
 row=c.execute('SELECT i.modified,i.media_type,r.created FROM items i JOIN intelligence_results r ON r.item=i.id AND r.fingerprint=i.fingerprint WHERE i.id=? AND i.fingerprint=?',(item,fingerprint)).fetchone()
 if not row:
  c.execute('DELETE FROM smart_pending WHERE item=?',(item,));return
 c.execute('DELETE FROM smart_frames WHERE item=?',(item,))
 for number,frame in enumerate(frames):
  save_one_frame(c,item,fingerprint,number,frame,row)
 c.execute('DELETE FROM smart_pending WHERE item=?',(item,))

class SearchIndex:
 def __init__(self,catalog,*,autostart=True):
  self.catalog=catalog;self.stop=threading.Event();self.error='';self.thread=None;self.start_lock=threading.Lock()
  with catalog.connect() as c:install(c)
  if autostart:self.start()
 def start(self):
  with self.start_lock:
   if self.thread is not None:return
   self.thread=threading.Thread(target=self._run,daemon=True,name='natural-search-index');self.thread.start()
 def batch(self,limit=32):
  added=0;work=0
  with self.catalog.connect() as c:
   c.execute('PRAGMA busy_timeout=200')
   state=dict(c.execute('SELECT key,value FROM smart_state'))
   if all(state.get(key)=='1' for key in ('results_done','dates_done')) and not c.execute('SELECT 1 FROM smart_pending LIMIT 1').fetchone():return 0
   c.execute('BEGIN IMMEDIATE')
   state=dict(c.execute('SELECT key,value FROM smart_state'))
   pending=c.execute('SELECT r.item,r.fingerprint,r.frames FROM smart_pending p JOIN intelligence_results r ON r.item=p.item ORDER BY p.item LIMIT ?',(limit,)).fetchall()
   work+=len(pending)
   for row in pending:save_frames(c,row['item'],row['fingerprint'],json.loads(row['frames']));added+=1
   if state.get('results_done')!='1':
    rows=c.execute('SELECT item,fingerprint,frames FROM intelligence_results WHERE item>? ORDER BY item LIMIT ?',
                   (state.get('result_cursor',''),limit)).fetchall()
    work+=len(rows) or 1
    for row in rows:
     if not c.execute('SELECT 1 FROM smart_frames WHERE item=? AND fingerprint=? LIMIT 1',(row['item'],row['fingerprint'])).fetchone():
      save_frames(c,row['item'],row['fingerprint'],json.loads(row['frames']));added+=1
    c.execute('INSERT OR REPLACE INTO smart_state VALUES(?,?)',('result_cursor',rows[-1]['item'] if rows else state.get('result_cursor','')))
    if len(rows)<limit:c.execute("UPDATE smart_state SET value='1' WHERE key='results_done'")
   if state.get('dates_done')!='1':
    rows=c.execute('SELECT item,payload,fingerprint,status FROM catalog_metadata WHERE item>? ORDER BY item LIMIT ?',
                   (state.get('date_cursor',''),limit*4)).fetchall()
    work+=len(rows) or 1
    for row in rows:
     # 重启后的 queued 缓存，须由 payload 中的原始指纹确认版本。
     try:valid_cached=json.loads(row['payload']).get('stamp',[None])[0]==row['fingerprint']
     except (ValueError,TypeError,IndexError,AttributeError):valid_cached=False
     if row['status']=='ready' or valid_cached:
      c.execute(f'''INSERT OR IGNORE INTO smart_dates SELECT item,fingerprint,{_time_expression('payload')}
        FROM catalog_metadata WHERE item=? AND {_time_expression('payload')} IS NOT NULL''',(row['item'],));added+=1
    c.execute('INSERT OR REPLACE INTO smart_state VALUES(?,?)',('date_cursor',rows[-1]['item'] if rows else state.get('date_cursor','')))
    if len(rows)<limit*4:c.execute("UPDATE smart_state SET value='1' WHERE key='dates_done'")
   if work:c.execute('UPDATE intelligence_meta SET generation=generation+1 WHERE id=1')
  return work
 def status(self,c):
  return {'ready':not c.execute("SELECT 1 FROM smart_state WHERE key IN ('results_done','dates_done') AND value!='1' LIMIT 1").fetchone() and not c.execute('SELECT 1 FROM smart_pending LIMIT 1').fetchone(),'error':self.error}
 def _run(self):
  while not self.stop.is_set():
   try:
    worked=self.batch();self.error='';self.stop.wait(.01 if worked else .5)
   except (sqlite3.Error,ValueError,KeyError) as error:
    self.error=str(error);self.stop.wait(1)
 def close(self):
  self.stop.set()
  if self.thread:self.thread.join(timeout=2)
