"""帧命中后真实片段时长与浏览器点击链路。"""
from pathlib import Path
import io,json,hashlib,unittest
from urllib.request import urlopen
from unittest.mock import patch
from PIL import Image
import test_video_playback as fixtures
from asset_intelligence import AssetIntelligence
from model_providers import Providers
from test_asset_intelligence import result

ROOT=Path(__file__).resolve().parents[1]
class FramePlaybackTests(unittest.TestCase):
 indexed=fixtures.VideoPlaybackTests.indexed
 probe=fixtures.VideoPlaybackTests.probe
 def setUp(self):
  import server
  fixtures.VideoPlaybackTests.setUp(self)
  self.providers=Providers(self.catalog.data);self.providers.save({'name':'fixture','category':'multimodal','protocol':'chat','url':'http://127.0.0.1:1','model':'fixture','select':True})
  self.service=AssetIntelligence(self.catalog,self.providers,autostart=False);self.stack.enter_context(patch.object(server,'INTELLIGENCE',self.service))
  self.source=self.photos/'movie.mp4';fixtures.long_clip(self.source);self.row=self.indexed(self.source)
  self.service.configure({'enabled':True,'auto_group':False,'video_interval_ms':3000,'video_frames':1});self.service.discover_tick()
  def vision(provider,payload,prompt,schema):
   with Image.open(io.BytesIO(payload)) as image:r,g,b=image.convert('RGB').getpixel((image.width//2,image.height//2))
   color,category=('红色','外套') if r>b else ('蓝色','牛仔裤');value=result(color);value['summary']='女性身穿'+color+category
   value['subjects']=[{'gender':'female','clothing':[{'category':category,'color':color,'description':''}]}];return json.dumps(value)
  with patch('asset_intelligence.vision',side_effect=vision):self.assertTrue(self.service.tick())
 def tearDown(self):self.service.close();fixtures.VideoPlaybackTests.tearDown(self)
 def test_real_stream_is_three_seconds_and_original_unchanged(self):
  before=hashlib.sha256(self.source.read_bytes()).hexdigest();info=self.service.video_frames.frame_info(self.row['id'],2)
  video_files={str(p) for p in self.catalog.data.rglob('*') if p.is_file() and p.suffix.lower() in ('.mp4','.mov','.mkv','.mxf')}
  self.assertEqual(info['timestamp_ms'],6000)
  route=f'/api/catalog/video/stream/{self.row["id"]}?session=clip-real-check&start=4.5&end=7.5&v={self.row["fingerprint"]}'
  with urlopen(self.url+route,timeout=25) as response:payload=response.read()
  self.assertEqual({str(p) for p in self.catalog.data.rglob('*') if p.is_file() and p.suffix.lower() in ('.mp4','.mov','.mkv','.mxf')},video_files,'播放只能读源文件并流式输出，不能写出片段视频')
  data=self.probe(payload);self.assertAlmostEqual(float(data['format']['duration']),3,delta=.15)
  self.assertEqual(next(s for s in data['streams'] if s['codec_type']=='video')['codec_name'],'h264')
  self.assertEqual(before,hashlib.sha256(self.source.read_bytes()).hexdigest())
 def test_browser_frame_card_plays_centered_clip_and_frame_panel(self):
  from playwright.sync_api import sync_playwright,expect
  self.catalog.metadata.start();out=ROOT/'build/video-frame-search';out.mkdir(parents=True,exist_ok=True)
  with sync_playwright() as pw:
   browser=pw.chromium.launch(channel='msedge',headless=True)
   try:
    page=browser.new_page(viewport={'width':1440,'height':960},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click();panel=page.locator('#asset-retrieval-page')
    panel.locator('.retrieval-query').fill('蓝色牛仔裤的帧');panel.locator('.retrieval-query').press('Enter');expect(panel.locator('.retrieval-card')).to_have_count(3)
    page.wait_for_function('()=>[...document.querySelectorAll("#asset-retrieval-page .retrieval-card img")].every(i=>i.complete&&i.naturalWidth>0)');page.screenshot(path=str(out/'matched-frame-cards.png'));panel.locator('.retrieval-card').first.click();expect(page.locator('#asset-image-viewer')).to_be_visible()
    page.wait_for_function('()=>document.querySelector("#asset-viewer-video")?.currentTime>.1',timeout=20000)
    video=page.locator('#asset-viewer-video');self.assertEqual(video.get_attribute('data-clip-start'),'4.5');self.assertEqual(video.get_attribute('data-clip-end'),'7.5')
    expect(page.locator('#asset-video-frames')).to_be_visible();expect(page.locator('[data-video-frame]')).to_have_count(5)
    page.screenshot(path=str(out/'centered-three-second-clip.png'));page.locator('[data-video-frame="0"]').click()
    page.wait_for_function('()=>document.querySelector("#asset-viewer-video")?.dataset.clipEnd==="1.5"')
    page.locator('#asset-viewer-close').click();self.assertFalse(errors,errors)
   finally:browser.close()
 def test_nonzero_video_track_offset_keeps_source_position_aligned(self):
  import subprocess
  from video_preview import find_tool
  source=self.photos/'offset.mp4'
  subprocess.run([find_tool('ffmpeg'),'-v','error','-f','lavfi','-i','color=red:s=160x90:r=10:d=3','-f','lavfi','-i','sine=duration=3.4',
    '-filter_complex','[0:v]setpts=PTS+0.4/TB[v]','-map','[v]','-map','1:a','-c:v','libx264','-threads','1','-c:a','aac','-fps_mode','vfr','-y',str(source)],check=True,capture_output=True)
  row=self.indexed(source);prepared=self.service.video_frames.prepare(row,self.service.config,'offset-fixture')
  self.service.video_frames.image(row,next(self.service.video_frames.records(row['id'])),json.loads(prepared['metadata']))
  info=self.service.video_frames.frame_info(row['id'],0);self.assertGreater(info['video_offset_ms'],300)
  route=f'/api/catalog/video/stream/{row["id"]}?session=offset-clip-check&start=0&end=1.5&v={row["fingerprint"]}'
  with urlopen(self.url+route,timeout=25) as response:data=self.probe(response.read())
  video=next(s for s in data['streams'] if s['codec_type']=='video')
  self.assertLess(float(video.get('start_time',0)),.1);self.assertAlmostEqual(float(data['format']['duration']),1.5,delta=.15)

if __name__=='__main__':unittest.main()
