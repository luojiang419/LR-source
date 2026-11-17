"""自然语言条件 -> 参数化索引查询；首屏不读文件、不调用模型、不统计整库。"""
import base64
import json
import math
import sqlite3
import time

from retrieval_schema import digest,normalized,tokens
from catalog_search_requests import SearchCancelled

CANONICAL="""(i.media_type<>'image' OR NOT EXISTS(SELECT 1 FROM items b
 WHERE b.pair_key=i.pair_key AND b.online=1 AND
 (CASE WHEN b.source='manual' THEN 0 ELSE 2 END + CASE WHEN b.extension IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,b.id)
 <(CASE WHEN i.source='manual' THEN 0 ELSE 2 END + CASE WHEN i.extension IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,i.id)))"""

def _expression(intent):
 words=list(intent['terms'])
 for part in intent['residual']:words.extend(tokens(part))
 parts=['"'+word.replace('"','""')+'"' for word in dict.fromkeys(words)]
 if intent.get('clothes_mode')=='any':
  alternatives=[]
  for group in intent['clothes']:
   terms=['category'+group['category']]+(['color'+group['color']] if group['color'] else [])
   alternatives.append('('+' AND '.join('"'+term+'"' for term in terms)+')')
  parts.append('('+' OR '.join(alternatives)+')')
 return ' AND '.join(parts)

def _frame_checks(intent,facets):
 checks=[];args=[]
 for name,value in facets.items():
  if not isinstance(name,str) or not isinstance(value,str) or not value.strip() or len(value)>200:raise ValueError('维度筛选无效')
  checks.append('EXISTS(SELECT 1 FROM intelligence_dimensions z WHERE z.item=f.item AND z.frame=f.frame AND z.key=? AND z.normalized=?)');args.extend([name,normalized(value)])
 clothes=intent['clothes']
 if clothes and intent.get('clothes_mode')=='any':
  options=[]
  for group in clothes:
   terms=['w.frame_id=f.id','w.category=?'];args.append(group['category'])
   if group['color']:terms.append('w.color=?');args.append(group['color'])
   if intent['gender']:terms.append('w.gender=?');args.append(intent['gender'])
   options.append('EXISTS(SELECT 1 FROM smart_wear w WHERE '+' AND '.join(terms)+')')
  checks.append('('+' OR '.join(options)+')');return checks,args
 if clothes:
  first=clothes[0];parts=['w.frame_id=f.id','w.category=?'];values=[first['category']]
  if first['color']:parts.append('w.color=?');values.append(first['color'])
  if intent['gender']:parts.append('w.gender=?');values.append(intent['gender'])
  for group in clothes[1:]:
   more=['z.frame_id=w.frame_id','z.person=w.person','z.category=?'];values.append(group['category'])
   if group['color']:more.append('z.color=?');values.append(group['color'])
   parts.append('EXISTS(SELECT 1 FROM smart_wear z WHERE '+' AND '.join(more)+')')
  checks.append('EXISTS(SELECT 1 FROM smart_wear w WHERE '+' AND '.join(parts)+')');args.extend(values)
 return checks,args

def query(catalog,intent,*,facets=None,root='',media='',cursor='',limit=60,cancel=None,collapse_video=False):
 if cancel and cancel():raise SearchCancelled()
 start=time.perf_counter();facets=facets or {};limit=max(1,min(100,int(limit)))
 if not isinstance(facets,dict) or len(facets)>20 or not isinstance(root,str) or len(root)>1000:raise ValueError('检索条件无效')
 if media and media not in ('image','video','design'):raise ValueError('媒体类型无效')
 if len(intent['dates'])>8:raise ValueError('一次最多使用 8 个时间范围')
 signature=digest([intent,facets,root,media,collapse_video]);marker=None
 common=['i.online=1','roots.removed=0',CANONICAL];common_args=[]
 if root:common.append('i.root=?');common_args.append(root)
 for kind in [media,intent['media']]:
  if kind:common.append('i.media_type=?');common_args.append(kind)
 expression=_expression(intent);frame_parts,frame_args=_frame_checks(intent,facets)
 if expression:frame_parts.insert(0,'f.id IN (SELECT rowid FROM smart_fts WHERE smart_fts MATCH ?)');frame_args.insert(0,expression)
 frame_where=' AND '.join(['f.item=i.id','f.fingerprint=i.fingerprint','EXISTS(SELECT 1 FROM intelligence_results current WHERE current.item=f.item)',*frame_parts])
 frame_exists='EXISTS(SELECT 1 FROM smart_frames f WHERE '+frame_where+')'
 words=list(dict.fromkeys(intent['terms']+[word for part in intent['residual'] for word in tokens(part)]))
 extra,extra_args=_frame_checks(intent,facets)
 point_parts=["instr(' '||f.terms||' ',?)>0"]*len(words)+extra
 point_exists='EXISTS(SELECT 1 FROM smart_frames f WHERE f.item=i.id AND f.fingerprint=i.fingerprint AND EXISTS(SELECT 1 FROM intelligence_results current WHERE current.item=f.item)'+(' AND '+' AND '.join(point_parts) if point_parts else '')+')'
 point_args=[' '+word+' ' for word in words]+extra_args
 semantic=bool(expression or facets or intent['clothes'] or intent.get('target')=='frames')
 keys=[];seen=set();strategy='';index_status={}
 with catalog.connect() as c:
  c.execute('PRAGMA busy_timeout=50');c.execute('BEGIN')
  generation=c.execute('SELECT generation FROM intelligence_meta WHERE id=1').fetchone()[0]
  if cursor:
   try:
    if not isinstance(cursor,str) or len(cursor)>2000:raise ValueError()
    marker=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
    if marker['q']!=signature or marker['g']!=generation or len(marker['k']) not in (2,3) or not math.isfinite(float(marker['k'][0])) or not isinstance(marker['k'][1],str):raise ValueError()
   except (ValueError,TypeError,KeyError):raise ValueError('索引或日期条件已变化，请重新搜索') from None
  search_state=dict(c.execute('SELECT key,value FROM smart_state'))
  index_status={'ready':all(search_state.get(key)=='1' for key in ('results_done','dates_done')) and not c.execute('SELECT 1 FROM smart_pending LIMIT 1').fetchone()}
  index_status['error']=getattr(getattr(getattr(catalog,'intelligence',None),'search_index',None),'error','')
  cancelled=lambda:bool(cancel and cancel())
  deadline=time.monotonic()+.75;c.set_progress_handler(lambda:int(cancelled() or time.monotonic()>deadline),5000)
  marker_frame=marker['k'][2] if marker and len(marker['k'])==3 else -1
  compare='>=' if marker_frame>=0 else '>'
  def collect(sql,args,maximum=limit+1):
   found=[];local_seen=set();result=c.execute(sql,args)
   try:
    for candidate in result:
     item=candidate['id']
     if item in local_seen:continue
     local_seen.add(item);key=dict(candidate)
     media_row=c.execute('SELECT media_type,duration_ms FROM items WHERE id=?',(item,)).fetchone()
     if media_row['media_type']=='video' and not collapse_video:
      parts=['f.item=?','f.fingerprint=i.fingerprint','EXISTS(SELECT 1 FROM intelligence_results current WHERE current.item=f.item)',*point_parts]
      values=[item]+point_args
      if marker and item==marker['k'][1] and key['sort_time']==marker['k'][0]:parts.append('f.frame>?');values.append(marker_frame)
      matches=c.execute('SELECT f.frame,f.timestamp_ms,f.source_frame,f.summary FROM smart_frames f JOIN items i ON i.id=f.item WHERE '+' AND '.join(parts)+' ORDER BY f.frame LIMIT ?',values+[maximum-len(found)]).fetchall()
      for frame in matches:
       frame=dict(frame);stamp=frame['timestamp_ms']
       if stamp is None:
        old=c.execute('SELECT frames FROM intelligence_results WHERE item=?',(item,)).fetchone()
        if old and len(old[0])<512000:
         legacy=json.loads(old[0])
         if frame['frame']<len(legacy):stamp=round(float(legacy[frame['frame']].get('seconds',0))*1000)
       if stamp is None:continue
       found.append({**key,'frame':frame['frame'],'timestamp_ms':stamp,'source_frame':frame['source_frame'],'frame_summary':frame['summary']})
       if len(found)>=maximum:break
      if not matches and not semantic:found.append({**key,'frame':-1})
     else:found.append({**key,'frame':-1})
     if len(found)>=maximum:break
   finally:result.close()
   return found
  try:
   if intent['dates']:
    strategy='capture_date+modified_range'
    for window in intent['dates']:
     for captured in (True,False):
      if not captured and search_state.get('dates_done')!='1' and not search_state.get('date_cursor'):continue
      stamp='d.shot_time' if captured else 'i.modified'
      source='smart_dates d INDEXED BY smart_date_time CROSS JOIN items i ON i.id=d.item AND i.fingerprint=d.fingerprint' if captured else 'items i INDEXED BY intelligence_catalog_order'
      clauses=common+[stamp+'>=?',stamp+'<?'];values=common_args+[window['from'],window['to']]
      if not captured:clauses.append('NOT EXISTS(SELECT 1 FROM smart_dates d WHERE d.item=i.id AND d.fingerprint=i.fingerprint)')
      if not captured and search_state.get('dates_done')!='1':clauses.append('i.id<=?');values.append(search_state['date_cursor'])
      if semantic:clauses.append(point_exists);values.extend(point_args)
      if marker:clauses.append(f'({stamp}<? OR ({stamp}=? AND i.id{compare}?))');values.extend([marker['k'][0],marker['k'][0],marker['k'][1]])
      sql=f'SELECT i.id,{stamp} AS sort_time FROM {source} CROSS JOIN roots ON roots.id=i.root WHERE '+' AND '.join(clauses)+f' ORDER BY {stamp} DESC,'+('d.item' if captured else 'i.id')+' LIMIT ?'
      keys.extend(collect(sql,values+[limit+2]))
    keys=sorted({(row['id'],row['frame']):row for row in keys}.values(),key=lambda row:(-row['sort_time'],row['id'],row['frame']))[:limit+1]
   elif intent['clothes'] and intent.get('clothes_mode')!='any':
    strategy='wear_relationship_time_index';first=intent['clothes'][0]
    driver='smart_wear_gender' if first['color'] and intent['gender'] else 'smart_wear_color' if first['color'] else 'smart_wear_category'
    clauses=common+['seed.category=?'];values=common_args+[first['category']]
    if first['color']:clauses.append('seed.color=?');values.append(first['color'])
    if first['color'] and intent['gender']:clauses.append('seed.gender=?');values.append(intent['gender'])
    # 首个条件走时间有序的关系索引，其余关系仍须在同一个人、同一帧内成立。
    clauses.append(point_exists);values.extend(point_args)
    if marker:clauses.append(f'(seed.modified<? OR (seed.modified=? AND seed.item{compare}?))');values.extend([marker['k'][0],marker['k'][0],marker['k'][1]])
    sql=f'SELECT i.id,seed.modified AS sort_time FROM smart_wear seed INDEXED BY {driver} CROSS JOIN items i ON i.id=seed.item CROSS JOIN roots ON roots.id=i.root WHERE '+' AND '.join(clauses)+' ORDER BY seed.modified DESC,seed.item'
    keys=collect(sql,values)
   else:
    strategy='frame_fts'
    clauses=common+([frame_exists] if semantic else []);values=common_args+(frame_args if semantic else [])
    if marker:clauses.append(f'(i.modified<? OR (i.modified=? AND i.id{compare}?))');values.extend([marker['k'][0],marker['k'][0],marker['k'][1]])
    # 候选由 FTS 先收窄，避免“没有命中”的请求逐行检查整库。
    source='smart_fts JOIN smart_frames candidate ON candidate.id=smart_fts.rowid CROSS JOIN items i ON i.id=candidate.item' if expression else 'items i'
    if expression:clauses.insert(0,'smart_fts MATCH ?');values.insert(0,expression)
    sql='SELECT DISTINCT i.id,i.modified AS sort_time FROM '+source+' CROSS JOIN roots ON roots.id=i.root WHERE '+' AND '.join(clauses)+' ORDER BY i.modified DESC,i.id LIMIT ?'
    keys=None
    if expression and c.execute('SELECT 1 FROM smart_fts WHERE smart_fts MATCH ? LIMIT 1',(expression,)).fetchone():
     # 高频词沿目录时间索引取首屏；低密度/旧时间段达到小预算后回到 FTS 候选排序。
     words=list(dict.fromkeys(intent['terms']+[word for part in intent['residual'] for word in tokens(part)]))
     extra,extra_args=_frame_checks(intent,facets)
     point='EXISTS(SELECT 1 FROM smart_frames f WHERE f.item=i.id AND f.fingerprint=i.fingerprint AND EXISTS(SELECT 1 FROM intelligence_results current WHERE current.item=f.item) AND '+' AND '.join(["instr(' '||f.terms||' ',?)>0"]*len(words)+extra)+')'
     checks=common+[point];parameters=common_args+[' '+word+' ' for word in words]+extra_args
     if marker:checks.append(f'(i.modified<? OR (i.modified=? AND i.id{compare}?))');parameters.extend([marker['k'][0],marker['k'][0],marker['k'][1]])
     quick_deadline=min(deadline,time.monotonic()+.06);c.set_progress_handler(lambda:int(cancelled() or time.monotonic()>quick_deadline),1000)
     try:
      keys=collect('SELECT i.id,i.modified AS sort_time FROM items i INDEXED BY intelligence_catalog_order CROSS JOIN roots ON roots.id=i.root WHERE '+' AND '.join(checks)+' ORDER BY i.modified DESC,i.id LIMIT ?',parameters+[limit+2]);strategy='ordered_point_index'
     except sqlite3.OperationalError as error:
      if cancelled():raise SearchCancelled() from None
      if 'interrupt' not in str(error):raise
     finally:c.set_progress_handler(lambda:int(cancelled() or time.monotonic()>deadline),5000)
    if keys is None:keys=collect(sql,values+[limit+2])
   if cancelled():raise SearchCancelled()
   more=len(keys)>limit;keys=keys[:limit]
   rows=[]
   if keys:
    marks=','.join('?' for _ in keys)
    hydrated={row['id']:dict(row) for row in c.execute('SELECT i.*,(SELECT f.summary FROM smart_frames f WHERE f.item=i.id AND f.fingerprint=i.fingerprint ORDER BY f.frame LIMIT 1) AS summary FROM items i WHERE i.id IN ('+marks+')',[row['id'] for row in keys])}
    for key in keys:
     row=dict(hydrated[key['id']]);row.update(priority=2,frames=[],preview='/api/catalog/preview/'+row['id'],reasons=intent['explanation'] or ['视觉解析资料命中'])
     row['result_id']=row['id']
     if key['frame']>=0:
      stamp=key['timestamp_ms'];duration=row.get('video_duration_ms') or row.get('duration_ms') or stamp+1500;row['summary']=key.get('frame_summary') or row.get('summary')
      row.update(result_id=f"{row['id']}:frame:{key['frame']}",frame_index=key['frame'],timestamp_ms=stamp,source_frame=key['source_frame'],
       clip={'start':max(0,stamp-1500)/1000,'end':min(duration,stamp+1500)/1000,'center':stamp/1000},
       preview=f"/api/intelligence/frame/{row['id']}/{key['frame']}?v={row['fingerprint']}&time={stamp}")
     rows.append(row)
  except sqlite3.OperationalError as error:
   if cancelled():raise SearchCancelled() from None
   if 'interrupt' in str(error):raise ValueError('智能检索达到时间预算，请增加条件或缩小归档范围') from None
   raise
 next_cursor=''
 if more and keys:
  last=keys[-1];next_cursor=base64.urlsafe_b64encode(json.dumps({'q':signature,'g':generation,'k':[last['sort_time'],last['id'],last['frame']]}).encode()).decode().rstrip('=')
 return {'rows':rows,'has_more':more,'next_cursor':next_cursor,'elapsed_ms':round((time.perf_counter()-start)*1000,2),
         'interpretation':{key:value for key,value in intent.items() if key not in ('terms','residual','recognized')},
         'index_status':index_status,'strategy':strategy,'engine':'sqlite-natural','total':len(rows) if not more else None}
