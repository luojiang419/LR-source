"""归档视频的首尾覆盖采样、逐帧检查点与受缓存总额度管理的缩略图。"""
import io,json,math,re,subprocess,time,uuid,threading
from contextlib import nullcontext
from fractions import Fraction
from pathlib import Path
from PIL import Image
from preview_cache import cache_for,cache_name
from retrieval_schema import digest,dumps
from video_preview import find_tool
DECODE_SLOTS=threading.BoundedSemaphore(2)

def run(command,timeout=45):
 try:return subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=True)
 except subprocess.TimeoutExpired:raise ValueError('视频抽帧超时，请检查原文件或解码组件') from None
 except subprocess.CalledProcessError as error:raise ValueError('视频无法解码：'+error.stderr.decode('utf8',errors='replace')[-300:]) from None

def probe(source):
 with DECODE_SLOTS:return _probe(source)

def _probe(source):
 tool=find_tool('ffprobe')
 if not tool:raise ValueError('未找到 ffprobe 视频组件')
 data=json.loads(run([tool,'-v','error','-select_streams','v:0','-show_entries','stream=duration,start_time,avg_frame_rate,r_frame_rate,nb_frames:format=duration,start_time','-of','json',str(source)]).stdout)
 if not data.get('streams'):raise ValueError('文件没有可用视频轨道')
 def number(*values):
  for value in values:
   try:parsed=float(value)
   except (ValueError,TypeError):continue
   if math.isfinite(parsed):return parsed
  return 0.
 stream=data['streams'][0];duration=number(stream.get('duration'),data.get('format',{}).get('duration'));start=number(stream.get('start_time'),data.get('format',{}).get('start_time'),0)
 if not math.isfinite(duration) or not 0<duration<=864000:raise ValueError('视频时长无效')
 # 读取尾部实际展示时间，不把容器时长/音频尾部当作真实尾帧。
 tail=run([tool,'-v','error','-select_streams','v:0','-read_intervals',f'{max(start,start+duration-10):.6f}%',
  '-show_frames','-show_entries','frame=best_effort_timestamp_time','-of','csv=p=0',str(source)],timeout=60).stdout
 if len(tail)>8_000_000:raise ValueError('视频尾帧信息过大，暂无法确认采样尾部')
 stamps=[]
 for line in tail.decode('utf8',errors='replace').splitlines():
  try:value=float(line.split(',')[0])
  except ValueError:continue
  if math.isfinite(value):stamps.append(value-start)
 if not stamps:raise ValueError('无法读取视频真实尾帧时间')
 try:fps=float(Fraction(stream.get('avg_frame_rate','0/1')))
 except (ValueError,ZeroDivisionError):fps=0
 try:count=int(stream.get('nb_frames',0))
 except (ValueError,TypeError):count=0
 cfr=stream.get('r_frame_rate')==stream.get('avg_frame_rate') and count>0 and abs(max(stamps)*fps+1-count)<.25
 format_start=number(data.get('format',{}).get('start_time'),start)
 return {'duration_ms':max(round(duration*1000),round(max(stamps)*1000)+1),'container_ms':round(max(number(data.get('format',{}).get('duration')),duration+max(0,start-format_start))*1000),'offset_ms':(start-format_start)*1000,'tail_ms':max(0,round(max(stamps)*1000)),'tail_seconds':max(stamps),'start':start,'fps':fps,
         'cfr':cfr}

def positions(tail_ms,interval_ms=1000,maximum=5000,uniform=24):
 if not 100<=interval_ms<=240000 or not 24<=maximum<=20000:raise ValueError('视频采样设置无效')
 interval=max(interval_ms,math.ceil(tail_ms/max(1,maximum-uniform-2)))
 values=set(range(0,tail_ms+1,interval));values.update([0,tail_ms])
 count=min(uniform,tail_ms+1)
 if count>1:values.update(round(tail_ms*i/(count-1)) for i in range(count))
 return sorted(values),interval

def clip_window(center_ms,duration_ms):
 if not isinstance(center_ms,(int,float)) or not math.isfinite(center_ms) or not 0<=center_ms<=duration_ms:raise ValueError('视频帧时间无效')
 return {'start':max(0,center_ms-1500)/1000,'end':min(duration_ms,center_ms+1500)/1000,'center':center_ms/1000}

class VideoFrames:
 def __init__(self,catalog):
  self.catalog=catalog
  with catalog.connect() as c:
   for name in ('video_offset_ms','video_duration_ms'):
    if name not in {row[1] for row in c.execute('PRAGMA table_info(items)')}:c.execute(f'ALTER TABLE items ADD COLUMN {name} REAL')
  with catalog.connect() as c:c.executescript('''
   CREATE TABLE IF NOT EXISTS video_analysis_plans(item TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,config TEXT NOT NULL,
     metadata TEXT NOT NULL,interval_ms INTEGER NOT NULL,total INTEGER NOT NULL,updated REAL NOT NULL);
   CREATE TABLE IF NOT EXISTS video_analysis_frames(item TEXT NOT NULL,frame INTEGER NOT NULL,sample_ms INTEGER NOT NULL,
     timestamp_ms INTEGER,source_frame INTEGER,cache TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'queued',
     result TEXT,ocr TEXT,error TEXT NOT NULL DEFAULT '',attempt INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL,
     PRIMARY KEY(item,frame));
   CREATE INDEX IF NOT EXISTS video_frame_status ON video_analysis_frames(item,status,frame);
   CREATE INDEX IF NOT EXISTS video_frame_timestamp ON video_analysis_frames(item,timestamp_ms,frame);
   CREATE TRIGGER IF NOT EXISTS video_analysis_delete AFTER DELETE ON items BEGIN
     DELETE FROM video_analysis_plans WHERE item=old.id;DELETE FROM video_analysis_frames WHERE item=old.id; END;
   CREATE TRIGGER IF NOT EXISTS video_analysis_version AFTER UPDATE OF fingerprint ON items WHEN old.fingerprint<>new.fingerprint BEGIN
     DELETE FROM video_analysis_plans WHERE item=new.id;DELETE FROM video_analysis_frames WHERE item=new.id; END;
   CREATE TRIGGER IF NOT EXISTS video_analysis_clear AFTER DELETE ON intelligence_results BEGIN
     DELETE FROM video_analysis_plans WHERE item=old.item;DELETE FROM video_analysis_frames WHERE item=old.item; END;
   CREATE TRIGGER IF NOT EXISTS video_alignment_version AFTER UPDATE OF fingerprint ON items WHEN old.fingerprint<>new.fingerprint BEGIN
     UPDATE items SET video_offset_ms=NULL,video_duration_ms=NULL WHERE id=new.id; END;
  ''')
 def check(self,row):
  path=Path(row['path']);root=Path(self.catalog._root(row['root'])['path'])
  try:path.resolve(strict=True).relative_to(root.resolve(strict=True))
  except (ValueError,OSError):raise ValueError('视频不在可用归档位置内') from None
  stat=path.stat()
  if stat.st_size!=row['size'] or abs(stat.st_mtime-row['modified'])>.001:raise ValueError('视频文件已变化，请刷新归档索引')
  return path
 def prepare(self,row,config,key,guard=None,lock=None,*,preserve_ocr_key=None):
  source=self.check(row)
  with self.catalog.connect() as c:existing=c.execute('SELECT * FROM video_analysis_plans WHERE item=? AND fingerprint=? AND config=?',(row['id'],row['fingerprint'],key)).fetchone()
  if existing:return dict(existing)
  metadata=probe(source);samples,interval=positions(metadata['tail_ms'],config['video_interval_ms'],config['video_max_frames'],config.get('video_frames',3))
  if metadata['cfr'] and metadata['fps']:
   samples=sorted({min(metadata['tail_ms'],round(round(stamp*metadata['fps']/1000)*1000/metadata['fps'])) for stamp in samples})
  self.check(row)
  created=time.time()
  with lock or nullcontext(),self.catalog.connect() as c:
   if guard and not guard():raise ValueError('视频任务或解析配置已变化')
   previous=c.execute('SELECT fingerprint FROM video_analysis_plans WHERE item=?',(row['id'],)).fetchone()
   same_samples=bool(config.get('run_stage') and previous and previous['fingerprint']==row['fingerprint'] and
    [r[0] for r in c.execute('SELECT sample_ms FROM video_analysis_frames WHERE item=? ORDER BY frame',(row['id'],))]==samples)
   keep_ocr=bool(same_samples and preserve_ocr_key and c.execute('SELECT 1 FROM intelligence_ocr WHERE item=? AND fingerprint=? AND config=?',(row['id'],row['fingerprint'],preserve_ocr_key)).fetchone())
   if same_samples and config.get('run_stage')=='ocr':
    # 独立 OCR 不抹掉同一批帧已经保存的视觉结果；只换 OCR 模型时重做文字。
    if not keep_ocr:
     c.execute('DELETE FROM intelligence_ocr WHERE item=?',(row['id'],))
     c.execute("UPDATE video_analysis_frames SET ocr=NULL,error='',attempt=0 WHERE item=?",(row['id'],))
    c.execute('UPDATE video_analysis_plans SET config=?,updated=? WHERE item=?',(key,created,row['id']))
    return {'metadata':dumps(metadata),'interval_ms':interval,'total':len(samples),'config':key,'fingerprint':row['fingerprint'],'updated':created}
   for table in ('intelligence_ocr','intelligence_dimensions','intelligence_vectors','intelligence_lsh','intelligence_fts'):
    if table=='intelligence_ocr' and keep_ocr:continue
    c.execute(f'DELETE FROM {table} WHERE item=?',(row['id'],))
   c.execute("UPDATE intelligence_results SET config=?,frames='[]',summary='视频抽帧等待解析',created=? WHERE item=?",(key,created,row['id']))
   c.execute('DELETE FROM smart_pending WHERE item=?',(row['id'],))
   c.execute('UPDATE items SET duration_ms=?,video_duration_ms=?,video_offset_ms=? WHERE id=? AND fingerprint=?',(metadata.get('container_ms',metadata['duration_ms']),metadata['duration_ms'],metadata.get('offset_ms',0),row['id'],row['fingerprint']))
   if keep_ocr:c.execute("UPDATE video_analysis_frames SET result=NULL,status='queued',error='',attempt=0,updated=? WHERE item=?",(created,row['id']))
   else:c.execute('DELETE FROM video_analysis_frames WHERE item=?',(row['id'],))
   c.execute('INSERT OR REPLACE INTO video_analysis_plans VALUES(?,?,?,?,?,?,?)',(row['id'],row['fingerprint'],key,dumps(metadata),interval,len(samples),created))
   if not keep_ocr:c.executemany('INSERT INTO video_analysis_frames(item,frame,sample_ms,updated) VALUES(?,?,?,?)',[(row['id'],i,stamp,time.time()) for i,stamp in enumerate(samples)])
  return {'metadata':dumps(metadata),'interval_ms':interval,'total':len(samples),'config':key,'fingerprint':row['fingerprint'],'updated':created}
 def records(self,item):
  after=-1
  while True:
   with self.catalog.connect() as c:rows=[dict(row) for row in c.execute('SELECT * FROM video_analysis_frames WHERE item=? AND frame>? ORDER BY frame LIMIT 64',(item,after))]
   if not rows:return
   for row in rows:yield row
   after=rows[-1]['frame']
 def cache_record(self,row,sample_ms):return {'fingerprint':digest([row['fingerprint'],'video-frame-v1',sample_ms]),'orientation':1}
 def image(self,row,record,metadata,expected_config=None):
  with self.catalog.connect() as c:plan=c.execute('SELECT config,fingerprint FROM video_analysis_plans WHERE item=?',(row['id'],)).fetchone()
  if expected_config is False:
   if plan:raise ValueError('抽帧计划已变化')
   expected_config=None
  else:
   if expected_config is not None and (not plan or plan['config']!=expected_config or plan['fingerprint']!=row['fingerprint']):raise ValueError('抽帧计划已变化')
   expected_config=expected_config or (plan['config'] if plan else None)
  cache=cache_for(self.catalog.previews);recipe=self.cache_record(row,record['sample_ms']);name=cache_name(recipe,1280)
  payload=cache.get(name,read=True)
  if payload and record['timestamp_ms'] is not None:return payload,record['timestamp_ms'],record['source_frame'],name
  seconds=record['sample_ms']/1000
  if metadata['cfr'] and metadata['fps']:seconds=round(seconds*metadata['fps'])/metadata['fps']
  if record['sample_ms']==metadata['tail_ms']:seconds=metadata.get('tail_seconds',seconds)
  source=self.check(row);target=metadata['start']+max(0,seconds-.000001)
  command=[find_tool('ffmpeg'),'-nostdin','-hide_banner','-loglevel','info','-copyts','-ss',f'{max(0,record["sample_ms"]/1000-.25):.6f}',
   '-i',str(source),'-map','0:v:0','-an','-sn','-dn','-vf',f"select=gte(t\\,{target:.6f}),scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease,showinfo",
   '-frames:v','1','-fps_mode','vfr','-threads','1','-f','image2pipe','-c:v','mjpeg','-q:v','3','pipe:1']
  if not command[0]:raise ValueError('未找到 FFmpeg 视频组件')
  with DECODE_SLOTS:result=run(command)
  payload=result.stdout
  times=re.findall(r'pts_time:([0-9.eE+\-]+)',result.stderr.decode('utf8',errors='replace'))
  if not payload or len(payload)>8_000_000 or not times:raise ValueError('视频帧无法读取')
  with Image.open(io.BytesIO(payload)) as image:image.verify()
  actual=max(0,float(times[0])-metadata['start']);stamp=round(actual*1000);number=round(actual*metadata['fps'])+1 if metadata['cfr'] and metadata['fps'] else None
  self.check(row)
  with cache.lock:
   if cache.get(name) is None:
    temp=cache.folder/('video-'+uuid.uuid4().hex+'.tmp')
    try:temp.write_bytes(payload);cache.publish(temp,cache.folder/name,recipe,1280,False)
    finally:
     if temp.exists():temp.unlink()
  if expected_config is not None:
   with self.catalog.connect() as c:
    updated=c.execute('UPDATE video_analysis_frames SET timestamp_ms=?,source_frame=?,cache=? WHERE item=? AND frame=? AND EXISTS(SELECT 1 FROM video_analysis_plans p WHERE p.item=video_analysis_frames.item AND p.config=? AND p.fingerprint=?)',(stamp,number,name,row['id'],record['frame'],expected_config,row['fingerprint'])).rowcount
    if not updated:raise ValueError('抽帧计划已变化')
  return payload,stamp,number,name
 def legacy(self,row):
  with self.catalog.connect() as c:old=c.execute('SELECT frames FROM intelligence_results WHERE item=? AND fingerprint=?',(row['id'],row['fingerprint'])).fetchone()
  if not old or len(old['frames'])>512000:return []
  frames=json.loads(old['frames']);return frames if len(frames)<=8 else []
 def frame_info(self,item,frame):
  row=self.catalog.item(item);self.catalog._root(row['root'])
  if row['media_type']!='video':raise ValueError('所选素材不是视频')
  with self.catalog.connect() as c:
   plan=c.execute('SELECT * FROM video_analysis_plans WHERE item=? AND fingerprint=?',(item,row['fingerprint'])).fetchone()
   record=c.execute('SELECT * FROM video_analysis_frames WHERE item=? AND frame=?',(item,int(frame))).fetchone()
  if not plan:
   old=self.legacy(row);number=int(frame)
   if not 0<=number<len(old):raise ValueError('视频帧尚未就绪或文件已变化')
   if row.get('video_duration_ms') is None or row.get('video_offset_ms') is None:
    technical=probe(self.check(row));row['duration_ms']=technical.get('container_ms',technical['duration_ms']);row['video_duration_ms']=technical['duration_ms'];row['video_offset_ms']=technical.get('offset_ms',0)
    with self.catalog.connect() as c:c.execute('UPDATE items SET duration_ms=?,video_duration_ms=?,video_offset_ms=? WHERE id=? AND fingerprint=?',(row['duration_ms'],row['video_duration_ms'],row['video_offset_ms'],item,row['fingerprint']))
   record={'timestamp_ms':round(float(old[number]['seconds'])*1000),'source_frame':None};metadata={'duration_ms':row['video_duration_ms']}
  else:
   if not record or record['timestamp_ms'] is None:raise ValueError('视频帧尚未就绪或文件已变化')
   metadata=json.loads(plan['metadata'])
  result=dict(row)
  result.update(result_id=f'{item}:frame:{frame}',frame_index=int(frame),timestamp_ms=record['timestamp_ms'],source_frame=record['source_frame'],
    duration_ms=metadata['duration_ms'],preview=f'/api/intelligence/frame/{item}/{frame}?v={row["fingerprint"]}&time={record["timestamp_ms"]}',clip=clip_window(record['timestamp_ms'],metadata['duration_ms']))
  return result
 def preview(self,item,frame,expected_ms=None,expected_fp=None):
  info=self.frame_info(item,frame)
  if expected_ms is not None and int(expected_ms)!=info['timestamp_ms'] or expected_fp is not None and expected_fp!=info['fingerprint']:raise ValueError('视频帧已变化，请重新搜索')
  with self.catalog.connect() as c:
   found=c.execute('SELECT * FROM video_analysis_frames WHERE item=? AND frame=?',(item,int(frame))).fetchone();record=dict(found) if found else None
   plan=c.execute('SELECT metadata,config,fingerprint FROM video_analysis_plans WHERE item=?',(item,)).fetchone()
  if plan and not record:raise ValueError('帧计划已变化，请重新搜索')
  if plan and (plan['fingerprint']!=info['fingerprint'] or record['timestamp_ms']!=info['timestamp_ms']):raise ValueError('帧计划已变化，请重新搜索')
  if not plan:
   metadata=probe(self.check(info));record={'frame':int(frame),'sample_ms':info['timestamp_ms'],'timestamp_ms':info['timestamp_ms'],'source_frame':None}
  else:metadata=json.loads(plan['metadata'])
  # SQL 搜索不解码；只有缩略图缓存被淘汰后的图片请求才会修复该帧。
  return self.image(info,record,metadata,plan['config'] if plan else False)[0]
 def page(self,item,after=-1,limit=60):
  row=self.catalog.item(item);self.catalog._root(row['root']);limit=max(1,min(100,int(limit)));after=int(after)
  with self.catalog.connect() as c:
   plan=c.execute('SELECT * FROM video_analysis_plans WHERE item=? AND fingerprint=?',(item,row['fingerprint'])).fetchone()
   if not plan:
    old=self.legacy(row);records=[{'frame':i,'timestamp_ms':round(f['seconds']*1000),'source_frame':None,'status':'ready','error':'','summary':f['result'].get('summary',''),'preview':f'/api/intelligence/frame/{item}/{i}?v={row["fingerprint"]}&time={round(f["seconds"]*1000)}'} for i,f in enumerate(old) if i>after]
    return {'rows':records[:limit],'has_more':len(records)>limit,'next_cursor':None,'total':len(old),'completed':len(old),'failed':0,'legacy':bool(old)}
   counts=dict(c.execute('SELECT status,count(*) FROM video_analysis_frames WHERE item=? GROUP BY status',(item,)))
   records=[dict(r) for r in c.execute("SELECT frame,timestamp_ms,source_frame,status,error,result FROM video_analysis_frames WHERE item=? AND frame>? AND status<>'duplicate' ORDER BY frame LIMIT ?",(item,after,limit+1))]
  more=len(records)>limit;records=records[:limit]
  for record in records:
   value=json.loads(record.pop('result') or '{}');record['summary']=value.get('summary','');record['preview']=f'/api/intelligence/frame/{item}/{record["frame"]}?v={row["fingerprint"]}&time={record["timestamp_ms"]}' if record['timestamp_ms'] is not None else None
  return {'rows':records,'has_more':more,'next_cursor':records[-1]['frame'] if more and records else None,'total':plan['total']-counts.get('duplicate',0),'duplicates':counts.get('duplicate',0),
          'completed':counts.get('ready',0),'failed':counts.get('failed',0),'interval_ms':plan['interval_ms']}
