"""真实 FFmpeg 抽帧、逐帧续跑、帧搜索与 3 秒点播边界。"""
from pathlib import Path
import io,json,subprocess,tempfile,unittest
from unittest.mock import patch
from PIL import Image
from asset_catalog import AssetCatalog
from asset_intelligence import AssetIntelligence
from model_providers import Providers
from retrieval_store import save_result
from test_asset_intelligence import result
from video_frames import positions,clip_window
from video_preview import find_tool

def clip(path,seconds=4,fps=10):
 command=[find_tool('ffmpeg'),'-v','error','-f','lavfi','-i',f'color=red:s=160x90:r={fps}:d={seconds/2}',
  '-f','lavfi','-i',f'color=blue:s=160x90:r={fps}:d={seconds/2}','-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[out]',
  '-map','[out]','-c:v','libx264','-pix_fmt','yuv420p','-threads','1','-y',str(path)]
 subprocess.run(command,check=True,capture_output=True,timeout=30)

class FramePlanTests(unittest.TestCase):
 def test_first_last_union_budget_and_clip_bounds(self):
  self.assertEqual(positions(0)[0],[0])
  points,step=positions(40000000,100,100,24);self.assertLessEqual(len(points),100);self.assertEqual((points[0],points[-1]),(0,40000000));self.assertGreater(step,100)
  self.assertEqual(clip_window(5000,12000),{'start':3.5,'end':6.5,'center':5.0})
  self.assertEqual(clip_window(0,12000)['end'],1.5);self.assertEqual(clip_window(11900,12000)['end'],12)

class VideoFrameTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.folder=self.base/'clips';self.folder.mkdir();self.source=self.folder/'colors.mp4';clip(self.source)
  self.catalog=AssetCatalog(self.base/'data');self.catalog.add_root(str(self.folder));self.providers=Providers(self.catalog.data)
  self.providers.save({'name':'fixture','category':'multimodal','protocol':'chat','url':'http://127.0.0.1:1','model':'fixture','select':True})
  self.service=AssetIntelligence(self.catalog,self.providers,autostart=False);self.service.configure({'enabled':True,'auto_group':False,'video_frames':1,'video_interval_ms':1000})
  self.row=self.catalog.items()['rows'][0]
 def tearDown(self):self.service.close();self.catalog.close();self.tmp.cleanup()
 def parse(self,fail_at=None):
  self.service.discover_tick();calls=[]
  def vision(provider,payload,prompt,schema):
   stamp=int(prompt.split('本帧时间：')[1].split(' ms')[0]);calls.append(stamp)
   if stamp==fail_at:raise ValueError('fixture 单帧失败')
   with Image.open(io.BytesIO(payload)) as image:r,g,b=image.convert('RGB').getpixel((image.width//2,image.height//2))
   color,category=('红色','外套') if r>b else ('蓝色','牛仔裤')
   value=result(color);value['summary']='女性身穿'+color+category;value['subjects']=[{'gender':'female','clothing':[{'category':category,'color':color,'description':''}]}]
   return json.dumps(value)
  with patch('asset_intelligence.vision',side_effect=vision):self.assertTrue(self.service.tick())
  return calls
 def test_real_first_tail_cached_thumbnails_and_frame_search(self):
  calls=self.parse();self.assertEqual(calls,[0,1000,2000,3000,3900])
  page=self.service.video_frames.page(self.row['id']);self.assertEqual(page['completed'],5)
  self.assertEqual(page['rows'][-1]['source_frame'],40)
  found=self.service.search(text='包含红色外套的帧',limit=1);self.assertEqual(len(found['rows']),1);self.assertTrue(found['has_more'])
  next_page=self.service.search(text='包含红色外套的帧',limit=1,cursor=found['next_cursor'])
  self.assertEqual([found['rows'][0]['timestamp_ms'],next_page['rows'][0]['timestamp_ms']],[0,1000]);self.assertFalse(next_page['has_more'])
  self.assertNotEqual(found['rows'][0]['result_id'],next_page['rows'][0]['result_id'])
  blue=self.service.search(text='女人穿蓝色牛仔裤');self.assertEqual([r['timestamp_ms'] for r in blue['rows']],[2000,3000,3900])
  collapsed=self.service.search(text='女人穿蓝色牛仔裤',collapse_video=True);self.assertEqual(len(collapsed['rows']),1);self.assertNotIn('frame_index',collapsed['rows'][0])
  similar=self.service.image_search({'item_id':self.row['id']});self.assertIn('frame_index',similar['rows'][0]);self.assertIn('/api/intelligence/frame/',similar['rows'][0]['preview'])
  with patch('video_frames.run',side_effect=AssertionError('缓存命中不应再解码')):
   data=self.service.video_frames.preview(self.row['id'],0,0,self.row['fingerprint']);self.assertTrue(data.startswith(b'\xff\xd8'))
  with self.assertRaises(ValueError):self.service.video_frames.preview(self.row['id'],0,999,self.row['fingerprint'])
 def test_partial_success_is_searchable_and_retry_only_failed_frame(self):
  calls=self.parse(fail_at=1000);self.assertEqual(len(calls),5)
  self.assertEqual(self.service.video_frames.page(self.row['id'])['completed'],4)
  self.assertEqual(len(self.service.search(text='女人穿蓝色牛仔裤')['rows']),3)
  self.service.control('retry');retried=self.parse();self.assertEqual(retried,[1000])
  self.assertEqual(self.service.video_frames.page(self.row['id'])['completed'],5)
 def test_short_video_tail_and_legacy_frames_still_work(self):
  value=result('红色');frames=[{'seconds':0.5,'result':value},{'seconds':3.5,'result':value}]
  with self.catalog.connect() as c:save_result(c,self.row['id'],self.row['fingerprint'],'old','fixture',frames,[])
  response=self.service.search(text='红色');self.assertEqual(len(response['rows']),2)
  self.assertTrue(self.service.video_frames.preview(self.row['id'],0).startswith(b'\xff\xd8'))
 def test_explicit_reparse_resets_frames_but_sampling_changes_reuse_images(self):
  self.parse();self.service.enqueue([self.row['id']],force=True);self.assertEqual(len(self.parse()),5)
  image=self.folder/'still.jpg';Image.new('RGB',(16,16),'red').save(image);self.catalog.add_root(str(self.folder))
  row=next(r for r in self.catalog.items()['rows'] if r['media_type']=='image');old_key=self.service._image_config_key(2)
  with self.catalog.connect() as c:save_result(c,row['id'],row['fingerprint'],old_key,'fixture',[{'seconds':0,'result':result()}],[])
  self.service.configure({'video_interval_ms':2000});self.service.discover_tick()
  with self.catalog.connect() as c:
   job=c.execute('SELECT status FROM intelligence_jobs WHERE item=? AND config=?',(row['id'],self.service._config_key())).fetchone();self.assertEqual(job['status'],'completed')
   self.assertEqual(c.execute('SELECT config FROM intelligence_results WHERE item=?',(row['id'],)).fetchone()[0],old_key)
 def test_ocr_then_vision_uses_same_video_frames_without_repeating_ocr(self):
  self.providers.save({'name':'fixture OCR','category':'ocr','protocol':'chat','url':'http://127.0.0.1:1','model':'fixture-ocr','select':True})
  with patch.object(self.service,'_ocr_enabled',return_value=True),patch('asset_intelligence.recognize_ocr',return_value={'text':'本帧 OCR','codes':[]}) as ocr:
   self.assertEqual(self.parse(),[]);self.assertEqual(ocr.call_count,5)
   self.assertEqual(len(self.parse()),5);self.assertEqual(ocr.call_count,5)
   self.service.configure({'video_interval_ms':2000})
   self.assertEqual(self.parse(),[]);self.assertEqual(ocr.call_count,8)
   self.assertEqual(len(self.parse()),3);self.assertEqual(ocr.call_count,8)
  self.assertEqual(self.service.video_frames.page(self.row['id'])['completed'],3)
 def test_professional_codecs_keep_real_tail_and_source_unchanged(self):
  from test_video_preview import make_clip
  import hashlib
  for codec,suffix in [('prores_ks','.mov'),('dnxhd','.mxf')]:
   with self.subTest(codec=codec):
    source=self.folder/(codec+suffix);make_clip(source,codec);before=hashlib.sha256(source.read_bytes()).hexdigest();self.catalog.add_root(str(self.folder))
    row=next(r for r in self.catalog.items()['rows'] if r['name']==source.name)
    prepared=self.service.video_frames.prepare(row,self.service.config,'codec-fixture');records=list(self.service.video_frames.records(row['id']))
    data,stamp,_,_=self.service.video_frames.image(row,records[-1],json.loads(prepared['metadata']))
    self.assertEqual(stamp,200)
    with Image.open(io.BytesIO(data)) as image:r,g,b=image.convert('RGB').getpixel((image.width//2,image.height//2));self.assertGreater(r,200);self.assertGreater(g,200);self.assertLess(b,40)
    self.assertEqual(before,hashlib.sha256(source.read_bytes()).hexdigest())
 def test_clear_analysis_removes_video_checkpoints_and_can_reparse(self):
  self.parse()
  with self.catalog.connect() as c:c.execute('DELETE FROM intelligence_results WHERE item=?',(self.row['id'],))
  self.assertEqual(self.service.video_frames.page(self.row['id'])['total'],0)
  self.assertEqual(self.service.search(text='蓝色牛仔裤的帧')['rows'],[])
  self.service.enqueue([self.row['id']],force=True);self.assertEqual(len(self.parse()),5)

 def test_manual_video_stages_preserve_ocr_frames_and_existing_vision(self):
  self.providers.save({'name':'fixture OCR','category':'ocr','protocol':'chat','url':'http://127.0.0.1:1','model':'fixture-ocr','select':True})
  self.service.control('start_ocr')
  with patch('asset_intelligence.recognize_ocr',return_value={'text':'逐帧文字','codes':[]}) as ocr:
   self.assertEqual(self.parse(),[]);self.assertEqual(ocr.call_count,5)
  self.service.discover_tick();self.assertFalse(self.service.pipeline_state()['running'])
  self.service.control('start_vision');self.service.discover_tick()
  with patch('asset_intelligence.recognize_ocr') as ocr,patch('asset_intelligence.vision',return_value=json.dumps(result())) as vision:
   self.assertTrue(self.service.tick());ocr.assert_not_called();self.assertEqual(vision.call_count,5)
   self.assertTrue(all('逐帧文字' in call.args[2] for call in vision.call_args_list))
  with self.catalog.connect() as c:before=c.execute('SELECT summary,created FROM intelligence_results WHERE item=?',(self.row['id'],)).fetchone()
  self.service.control('start_ocr')
  with patch('asset_intelligence.recognize_ocr') as ocr,patch('asset_intelligence.vision') as vision:
   self.service.discover_tick();self.service.tick();ocr.assert_not_called();vision.assert_not_called()
  with self.catalog.connect() as c:after=c.execute('SELECT summary,created FROM intelligence_results WHERE item=?',(self.row['id'],)).fetchone()
  self.assertEqual(tuple(before),tuple(after))

if __name__=='__main__':unittest.main()
