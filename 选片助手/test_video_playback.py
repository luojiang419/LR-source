"""真实 FFmpeg 视频流、任意位置定位、浏览器快捷键及进程回收。"""
from contextlib import ExitStack
import hashlib
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from PIL import Image
from asset_catalog import AssetCatalog
from video_preview import find_tool
from test_video_preview import make_clip

ROOT=Path(__file__).resolve().parents[1]


def long_clip(path):
    command=[find_tool('ffmpeg'),'-v','error','-f','lavfi','-i','color=red:s=160x90:r=12:d=6',
             '-f','lavfi','-i','color=blue:s=160x90:r=12:d=6','-f','lavfi','-i',
             'sine=frequency=660:sample_rate=48000:duration=12','-filter_complex',
             '[0:v][1:v]concat=n=2:v=1:a=0[out]','-map','[out]','-map','2:a',
             '-c:v','libx264','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-y',str(path)]
    subprocess.run(command,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


class VideoPlaybackTests(unittest.TestCase):
    def setUp(self):
        import server
        from asset_archive import AssetArchive
        from catalog_store import Store
        from model_providers import Providers
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'clips';self.photos.mkdir()
        self.catalog=AssetCatalog(self.base/'data')
        self.store=Store(self.catalog.data,ROOT);self.archive=AssetArchive(self.store,autostart=False)
        self.stack=ExitStack()
        for key,value in dict(STORE=self.store,DATA=self.catalog.data,CATALOG=self.catalog,ARCHIVE=self.archive,
            MODELS=Providers(self.catalog.data),SKU=None,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,
            UPDATES=None,ANALYSIS=None,REFERENCES=None,PREVIEWS=None).items():
            self.stack.enter_context(patch.object(server,key,value))
        self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.http.daemon_threads=True
        threading.Thread(target=self.http.serve_forever,daemon=True).start();self.url=f'http://127.0.0.1:{self.http.server_port}'
        self.number=0

    def tearDown(self):
        self.catalog.close();self.http.shutdown();self.http.server_close();self.archive.close();self.stack.close();self.temp.cleanup()

    def get(self,path):
        with urlopen(self.url+path,timeout=25) as response:return json.load(response)

    def post(self,path,data):
        import server
        request=Request(self.url+path,json.dumps(data).encode(),{'Content-Type':'application/json','Origin':self.url,'X-Framekeep-Token':server.TOKEN})
        with urlopen(request,timeout=10) as response:return json.load(response)

    def indexed(self,path):
        self.catalog.add_root(str(self.photos))
        return next(row for row in self.catalog.items()['rows'] if row['name']==path.name)

    def stream_url(self,row,start=0):
        self.number+=1;session=f'test-playback-{self.number}'
        return f'/api/catalog/video/stream/{row["id"]}?session={session}&start={start}',session

    def probe(self,payload):
        target=self.base/'playback.mp4';target.write_bytes(payload)
        output=subprocess.run([find_tool('ffprobe'),'-v','error','-show_streams','-show_format','-of','json',str(target)],capture_output=True,check=True)
        return json.loads(output.stdout)

    def wait(self,fn,seconds=10):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            if fn():return
            time.sleep(.03)
        self.fail('等待条件超时')

    def test_professional_codecs_stream_as_playable_h264_without_modifying_originals(self):
        for codec,suffix in [('libx264','.mp4'),('libx265','.mp4'),('prores_ks','.mov'),('dnxhd','.mxf')]:
            with self.subTest(codec=codec):
                path=self.photos/(codec+suffix);make_clip(path,codec);original=hashlib.sha256(path.read_bytes()).hexdigest();row=self.indexed(path)
                route,session=self.stream_url(row)
                with urlopen(self.url+route,timeout=20) as response:
                    self.assertEqual(response.headers['Content-Type'],'video/mp4');payload=response.read()
                info=self.probe(payload);video=next(s for s in info['streams'] if s['codec_type']=='video')
                self.assertEqual(video['codec_name'],'h264');self.assertEqual(video['width'],256)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),original)
                self.wait(lambda:self.catalog.playback.active_count()==0)
                self.assertEqual(self.catalog.playback.state(session)['status'],'ended')

    def test_seek_before_buffering_preserves_audio_and_starts_at_requested_picture(self):
        source=self.photos/'audio-and-colors.MOV';long_clip(source);row=self.indexed(source)
        route,_=self.stream_url(row,7)
        with urlopen(self.url+route,timeout=20) as response:payload=response.read()
        info=self.probe(payload)
        self.assertTrue(any(s['codec_type']=='audio' and s['codec_name']=='aac' for s in info['streams']))
        self.assertLess(float(info['format']['duration']),6)
        result=subprocess.run([find_tool('ffmpeg'),'-v','error','-i',str(self.base/'playback.mp4'),'-frames:v','1','-f','image2pipe','-c:v','png','pipe:1'],capture_output=True,check=True)
        with Image.open(io.BytesIO(result.stdout)) as image:
            r,g,b=image.convert('RGB').getpixel((40,40));self.assertGreater(b,220);self.assertLess(r,30)

    def test_cancel_close_invalid_positions_corrupt_file_and_image_rejection(self):
        source=self.photos/'long.MOV';long_clip(source);row=self.indexed(source)
        route,session=self.stream_url(row)
        response=urlopen(self.url+route,timeout=20);self.assertTrue(response.read(32));self.assertEqual(self.catalog.playback.active_count(),1)
        self.post('/api/catalog/video/stop',{'session':session});response.close()
        self.wait(lambda:self.catalog.playback.active_count()==0)
        self.assertEqual(self.catalog.playback.state(session)['status'],'stopped')
        for value in ('nan','-1','inf','1000001','bad'):
            route,session=self.stream_url(row,value)
            with self.assertRaises(HTTPError) as error:urlopen(self.url+route,timeout=5)
            self.assertEqual(error.exception.code,400)
        image=self.photos/'image.JPG';Image.new('RGB',(20,10),'red').save(image);image_row=self.indexed(image)
        route,_=self.stream_url(image_row)
        with self.assertRaises(HTTPError):urlopen(self.url+route,timeout=5)
        broken=self.photos/'broken.MOV';broken.write_bytes(b'invalid video');broken_row=self.indexed(broken)
        route,session=self.stream_url(broken_row)
        with urlopen(self.url+route,timeout=10) as response:self.assertFalse(response.read())
        self.wait(lambda:self.catalog.playback.state(session)['status']=='failed')
        self.assertIn('无法解码',self.get('/api/catalog/video/state?session='+session)['error'])
        self.assertEqual(self.catalog.playback.active_count(),0)

    def test_disconnect_and_concurrent_stream_limit_release_processes(self):
        source=self.photos/'long.MOV';long_clip(source);row=self.indexed(source)
        streams=[]
        try:
            for _ in range(2):
                route,session=self.stream_url(row);response=urlopen(self.url+route,timeout=15)
                self.assertTrue(response.read(32));streams.append((response,session))
            self.assertEqual(self.catalog.playback.active_count(),2)
            route,_=self.stream_url(row)
            with self.assertRaises(HTTPError):urlopen(self.url+route,timeout=5)
            for response,_ in streams:response.close()
            self.wait(lambda:self.catalog.playback.active_count()==0)
        finally:
            for response,session in streams:response.close();self.catalog.playback.stop(session)

    def test_browser_play_pause_drag_skip_image_space_details_and_cleanup(self):
        from playwright.sync_api import sync_playwright
        source=self.photos/'movie.MOV';long_clip(source);row=self.indexed(source)
        image=self.photos/'photo.JPG';Image.new('RGB',(400,300),'teal').save(image);image_row=self.indexed(image)
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.photos.iterdir()}
        self.catalog.metadata.start()
        out=ROOT/'build/video-playback';out.mkdir(parents=True,exist_ok=True)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce');errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                card=page.locator(f'[data-catalog-item="{row["id"]}"]');card.dblclick()
                page.wait_for_function("()=>document.querySelector('#asset-viewer-video')?.currentTime>.2")
                page.wait_for_function("()=>document.querySelector('#asset-viewer-video').webkitAudioDecodedByteCount>0")
                self.assertEqual(page.locator('#asset-viewer-info-title').inner_text(),'视频详细信息')
                page.wait_for_function("()=>!document.querySelector('#asset-video-seek').disabled")
                self.assertIn('视频编码',page.locator('#asset-viewer-fields').inner_text())
                self.assertIn('音频声道',page.locator('#asset-viewer-fields').inner_text())
                page.keyboard.press('Space');self.assertTrue(page.locator('#asset-viewer-video').evaluate('e=>e.paused'))
                position=page.locator('#asset-viewer-video').evaluate('e=>e.currentTime');page.wait_for_timeout(300)
                self.assertAlmostEqual(page.locator('#asset-viewer-video').evaluate('e=>e.currentTime'),position,places=1)
                slider=page.locator('#asset-video-seek');box=slider.bounding_box()
                page.mouse.move(box['x']+box['width']*.1,box['y']+box['height']/2);page.mouse.down()
                page.mouse.move(box['x']+box['width']*.7,box['y']+box['height']/2,steps=8);page.mouse.up()
                page.wait_for_function("()=>Number(new URL(document.querySelector('#asset-viewer-video').src).searchParams.get('start'))>7")
                page.wait_for_function("()=>document.querySelector('#asset-viewer-video').readyState>=2")
                self.assertTrue(page.locator('#asset-viewer-video').evaluate('e=>e.paused'))
                pixel=page.locator('#asset-viewer-video').evaluate("e=>{const c=document.createElement('canvas');c.width=c.height=1;c.getContext('2d').drawImage(e,0,0,1,1);return [...c.getContext('2d').getImageData(0,0,1,1).data]}")
                self.assertGreater(pixel[2],200)
                page.keyboard.press('Space');page.wait_for_function("()=>!document.querySelector('#asset-viewer-video').paused")
                page.keyboard.press('ArrowLeft')
                page.wait_for_function("()=>{const t=Number(new URL(document.querySelector('#asset-viewer-video').src).searchParams.get('start'));return t>2&&t<5}")
                page.wait_for_function("()=>document.querySelector('#asset-viewer-video').currentTime>.1")
                page.keyboard.press('ArrowRight');page.wait_for_function("()=>Number(new URL(document.querySelector('#asset-viewer-video').src).searchParams.get('start'))>7")
                page.wait_for_function("()=>document.querySelector('#asset-viewer-video').readyState>=2")
                page.keyboard.press('Space')
                for theme in ('dark','light','pure-white','hardcore'):
                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme);page.screenshot(path=str(out/f'player-{theme}.png'))
                    page.set_viewport_size({'width':375,'height':950});self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    page.screenshot(path=str(out/f'player-{theme}-narrow.png'));page.set_viewport_size({'width':1440,'height':1000})
                page.keyboard.press('Escape');self.wait(lambda:self.catalog.playback.active_count()==0)
                card.click();page.keyboard.press('Space');page.wait_for_selector('#asset-viewer-video');page.keyboard.press('Escape')
                page.locator(f'[data-catalog-item="{image_row["id"]}"]').click();page.keyboard.press('Space')
                page.wait_for_function("()=>document.querySelector('#asset-viewer-image').naturalWidth>0")
                self.assertEqual(page.locator('#asset-viewer-video').count(),0)
                self.assertTrue(page.locator('.asset-viewer-playback').is_hidden())
                page.keyboard.press('Space');self.assertFalse(page.locator('#asset-image-viewer').evaluate('e=>e.open'))
                page.locator('#catalog-search').fill('hello');page.keyboard.press('Space');self.assertFalse(page.locator('#asset-image-viewer').evaluate('e=>e.open'))
                self.assertEqual(page.locator('#catalog-search').input_value(),'hello ')
                self.assertFalse(errors,errors)
                self.assertEqual(before,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.photos.iterdir()})
                (out/'browser-result.json').write_text(json.dumps({'passed':True,'errors':errors,'audio_stream':True,'play_pause':True,'seek_unbuffered':True,'arrow_skip':True,'space_preview':True,'themes':4,'narrow':375,'originals_unchanged':True},ensure_ascii=False,indent=2),encoding='utf8')
            finally:browser.close()
        self.wait(lambda:self.catalog.playback.active_count()==0)

    def test_browser_professional_sources_decode_and_corrupt_video_can_close(self):
        from playwright.sync_api import sync_playwright
        fixtures=[]
        for codec,suffix in [('libx264','.mp4'),('libx265','.mp4'),('prores_ks','.mov'),('dnxhd','.mxf')]:
            source=self.photos/(codec+suffix);make_clip(source,codec);fixtures.append((codec,self.indexed(source)))
        damaged=self.photos/'damaged.MOV';damaged.write_bytes(b'bad video');broken=self.indexed(damaged)
        self.catalog.metadata.start()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce');errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-archive-open').click()
                for codec,row in fixtures:
                    page.locator(f'[data-catalog-item="{row["id"]}"]').dblclick()
                    page.wait_for_function("()=>document.querySelector('#asset-viewer-video')?.getVideoPlaybackQuality().totalVideoFrames>0")
                    self.assertEqual(page.locator('#asset-viewer-video').evaluate('e=>e.videoWidth'),256)
                    page.wait_for_function("()=>document.getElementById('asset-viewer-info-status').textContent===''")
                    page.screenshot(path=str(ROOT/'build/video-playback'/f'codec-{codec}.png'))
                    page.keyboard.press('Escape');self.wait(lambda:self.catalog.playback.active_count()==0)
                page.locator(f'[data-catalog-item="{broken["id"]}"]').dblclick()
                page.wait_for_function("()=>document.querySelector('#asset-viewer-stage').dataset.previewState==='error'")
                self.assertIn('无法',page.locator('#asset-viewer-image-status').inner_text())
                page.keyboard.press('Escape');self.assertFalse(page.locator('#asset-image-viewer').evaluate('e=>e.open'))
                self.assertFalse(errors,errors)
            finally:browser.close()


if __name__=='__main__':unittest.main()
