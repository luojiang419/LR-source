"""实时 JPG 直通：通知不读图，首个可见帧使用主图原始字节和分辨率。"""
from contextlib import ExitStack,contextmanager
from pathlib import Path
import hashlib
import io
import json
import struct
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

from PIL import Image,ImageDraw
from catalog_store import Store
from folder_watch import FolderWatch

ROOT=Path(__file__).resolve().parents[1]
SIZE=(3600,2400)


def camera_jpeg(orientation=1):
    """主图为细线绿色画面，缩略图为红色；加载错层级时尺寸和像素都会暴露。"""
    image=Image.new('RGB',SIZE,'#24b060');draw=ImageDraw.Draw(image)
    for x in range(0,SIZE[0],24):draw.line((x,0,x,SIZE[1]),fill='#173251',width=3)
    original=io.BytesIO();image.save(original,format='JPEG',quality=94)
    small=io.BytesIO();Image.new('RGB',(160,100),'red').save(small,format='JPEG')
    thumbnail=small.getvalue()
    tiff=b'II'+struct.pack('<HI',42,8)+struct.pack('<H',1)+struct.pack('<HHI',0x112,3,1)+struct.pack('<H',orientation)+b'\0\0'+struct.pack('<I',26)
    tiff+=struct.pack('<H',2)+struct.pack('<HHII',0x201,4,1,56)+struct.pack('<HHII',0x202,4,1,len(thumbnail))+struct.pack('<I',0)+thumbnail
    exif=b'Exif\0\0'+tiff
    return b'\xff\xd8\xff\xe1'+struct.pack('>H',len(exif)+2)+exif+original.getvalue()[2:]


def configure(feed,photos,sid='direct-session',generation='direct-generation'):
    feed.configure({'enabled':True,'folder':str(photos),'recursive':True,'source':'manual',
                    'session_id':sid,'generation':generation})


class DirectJpegNotificationTests(unittest.TestCase):
    def test_announce_does_not_open_decode_or_validate_image(self):
        from direct_jpeg import DirectJpegFeed
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT)
            feed=DirectJpegFeed(store,autostart=False)
            try:
                configure(feed,photos)
                target=photos/'not-yet-written.JPG'
                with patch.object(Path,'open',side_effect=AssertionError('通知不得读取图像')),patch.object(Image,'open',side_effect=AssertionError('通知不得解码或校验图像')):
                    feed.announce(target)
                state=feed.state();receiving=state['receiving']
                self.assertIsNotNone(receiving)
                self.assertEqual(receiving['name'],target.name)
                self.assertEqual(receiving['id'],hashlib.sha256(b'not-yet-written.jpg').hexdigest()[:24])
                self.assertGreater(feed.order(target),0)
                # 不是 JPEG 内容也照样注册；浏览器自行负责图像解码结果。
                target.write_bytes(b'producer has not written a JPEG yet')
                with patch.object(Image,'open',side_effect=AssertionError('不得做图像预检')):feed.announce(target)
                self.assertEqual(feed.source_path(state['generation'],receiving['key']),target)
            finally:feed.close()

    def test_sources_are_scoped_and_old_admission_cannot_replace_latest_notice(self):
        from direct_jpeg import DirectJpegFeed
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT)
            feed=DirectJpegFeed(store,autostart=False)
            try:
                configure(feed,photos)
                first=photos/'001.JPG';second=photos/'002.JPG'
                first.write_bytes(b'first');second.write_bytes(b'second')
                feed.announce(first);old=feed.state();feed.announce(second);latest=feed.state()
                self.assertEqual(latest['receiving']['name'],'002.JPG')
                self.assertGreater(feed.order(second),feed.order(first))
                feed.acknowledge([old['receiving']['id']])
                self.assertEqual(feed.state()['receiving']['id'],latest['receiving']['id'])
                # RAW/JPG 正式合并后，JPG 别名也已入库，迟到的重复通知不得重盖界面。
                feed.acknowledge(['raw-capture-id'],{latest['receiving']['id']:'raw-capture-id'})
                self.assertIsNone(feed.state()['receiving']);self.assertFalse(feed.announce(second))
                before=feed.state()['event_id'];feed.announce(base/'outside.JPG')
                self.assertEqual(feed.state()['event_id'],before)
                self.assertIsNone(feed.source_path('wrong-generation',latest['receiving']['key']))
                configure(feed,photos,generation='new-generation')
                self.assertIsNone(feed.state()['receiving'])
                self.assertIsNone(feed.source_path(old['generation'],old['receiving']['key']))
                feed.close()
                self.assertIsNone(feed.source_path(latest['generation'],latest['receiving']['key']))
            finally:feed.close()


@contextmanager
def application_runtime():
    import server
    with tempfile.TemporaryDirectory() as folder,ExitStack() as patches:
        base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT)
        Image.new('RGB',(600,400),'navy').save(photos/'FIRST.JPG')
        watch=FolderWatch(store,autostart=False)
        sid=watch.start(str(photos))['session_id'];store.activate(sid)
        def metadata(paths,*args,**kwargs):
            return [{'Orientation':1,'ImageWidth':600 if p.name=='FIRST.JPG' else SIZE[0],
                     'ImageHeight':400 if p.name=='FIRST.JPG' else SIZE[1]} for p in paths]
        patches.enter_context(patch('media_engine.read_metadata',side_effect=metadata))
        watch.tick();watch.thread.start()
        for name,value in dict(STORE=store,DATA=store.data,WATCH=watch,CANON=None,PREVIEWS=None,
                               MOUSE=None,MODELS=None,SKU=None,ARCHIVE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None).items():
            patches.enter_context(patch.object(server,name,value))
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        threading.Thread(target=http.serve_forever,daemon=True).start()
        try:yield server,base,photos,store,watch,sid,f'http://127.0.0.1:{http.server_port}'
        finally:watch.close();http.shutdown();http.server_close()


class DirectJpegHttpTests(unittest.TestCase):
    def test_original_bytes_are_returned_without_preview_pipeline(self):
        with application_runtime() as (server,base,photos,store,watch,sid,url):
            payload=camera_jpeg();target=photos/'EXACT.JPG';target.write_bytes(payload)
            watch.direct.announce(target);state=watch.direct.state();receiving=state['receiving']
            self.assertEqual(watch.direct.source_path(state['generation'],receiving['key']),target)
            with patch.object(Image,'open',side_effect=AssertionError('显示请求不得预解码')),patch('media_engine.preview_bytes',side_effect=AssertionError('不得生成预览缓存')),patch('media_engine.quick_preview_bytes',side_effect=AssertionError('不得生成480图')):
                with urllib.request.urlopen(url+receiving['url'],timeout=5) as response:
                    actual=response.read();kind=response.headers['Content-Type']
            self.assertEqual(actual,payload,'原始 JPEG 压缩数据、EXIF 和像素必须逐字节直通')
            self.assertTrue(kind.startswith('image/jpeg'))
            # 资料就绪后使用 previewFull，仍然不能走2048/480解码与重编码。
            until=time.monotonic()+3
            while not any(r['jpg']=='EXACT.JPG' for r in store.records(sid)):
                if time.monotonic()>until:self.fail('正式记录未接续')
                time.sleep(.02)
            photo=next(r for r in server.ui_photos(sid) if r['jpg']=='EXACT.JPG')
            self.assertIn('full=1',photo['previewFull'])
            with patch.object(Image,'open',side_effect=AssertionError('正式首帧不得预解码')),patch('media_engine.preview_bytes',side_effect=AssertionError('不得等待预览缓存')):
                with urllib.request.urlopen(url+photo['previewFull'],timeout=5) as response:self.assertEqual(response.read(),payload)


class DirectJpegBrowserTests(unittest.TestCase):
    def test_sidebar_arrival_bypasses_slow_cache_and_recovers_image_failure(self):
        from playwright.sync_api import sync_playwright
        with application_runtime() as (server,base,photos,store,watch,sid,url),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':960});errors=[];instant=[];failed=[False]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def route(request):
                    address=request.request.url
                    if 'size=480' in address:
                        if 'instant=1' not in address:return request.fulfill(status=503,body='cache queue blocked')
                        instant.append(address)
                        if hashlib.sha256(b'sidebar_0.jpg').hexdigest()[:24] in address and not failed[0]:
                            failed[0]=True;return request.fulfill(status=503,body='one transient failure')
                    request.continue_()
                page.route('**/api/preview/**',route)
                page.goto(url,wait_until='domcontentloaded')
                page.wait_for_function('()=>!FramekeepBoot.loading&&FramekeepLayout.isMonitoring()')
                # 磁盘缓存整条路径不能服务；监控侧栏应复用当前 JPG 的内存像素。
                with patch('media_engine.preview_bytes',side_effect=AssertionError('监控侧栏不得等待持久缓存')):
                    for index in range(3):
                        name=f'SIDEBAR_{index}.JPG';(photos/name).write_bytes(camera_jpeg())
                        page.wait_for_function('(name)=>document.querySelector("#photos [data-loupe] img")?.alt===name&&!FramekeepReceiving.active',arg=name,timeout=3000)
                        page.wait_for_function('()=>{const img=document.querySelector("#watch-thumbnails .active img");return img?.complete&&img.naturalWidth>0&&img.src.startsWith("blob:")&&img.dataset.previewSource.includes("instant=1")}',timeout=1500)
                        self.assertTrue(page.locator('#watch-thumbnails .active img').evaluate('(img)=>{const r=img.getBoundingClientRect(),s=document.getElementById("watch-thumbnails").getBoundingClientRect();return r.top<s.bottom&&r.bottom>s.top}'))
                    page.locator('#stars [data-rating="3"]').click()
                    page.wait_for_function('()=>document.querySelectorAll("#stars .on").length===3')
                    self.assertGreater(page.locator('#watch-thumbnails .active img').evaluate('(img)=>img.naturalWidth'),0)
                self.assertTrue(failed[0]);self.assertTrue(any('retry=1' in address for address in instant));self.assertFalse(errors,errors)
                page.screenshot(path=str(ROOT/'build/dpp-sharp-direct/sidebar-direct.png'))
            finally:browser.close()

    def test_growing_file_retries_native_decode_without_waiting_for_close(self):
        from playwright.sync_api import sync_playwright
        payload=camera_jpeg()
        with application_runtime() as (server,base,photos,store,watch,sid,url),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(url,wait_until='domcontentloaded')
                page.wait_for_function('()=>!FramekeepBoot.loading&&document.querySelector("#photos img")?.naturalWidth>0')
                with (photos/'GROWING.JPG').open('wb') as writer:
                    writer.write(payload[:100]);writer.flush();page.wait_for_timeout(300)
                    self.assertFalse(page.evaluate('FramekeepReceiving.active'))
                    writer.write(payload[100:]);writer.flush()
                    page.wait_for_function('(bytes)=>{const node=document.querySelector(".receiving-photo-preview");return node?.querySelector("img")?.alt==="GROWING.JPG"&&node.dataset.bytes===String(bytes)&&node.querySelector("img").naturalWidth===3600}',arg=len(payload),timeout=1500)
                    self.assertEqual(len(store.records(sid)),1)
                page.wait_for_function('()=>!FramekeepReceiving.active&&document.querySelector("#photos [data-loupe] img")?.alt==="GROWING.JPG"')
            finally:browser.close()

    def test_new_jpeg_cancels_old_request_and_late_admission(self):
        from playwright.sync_api import sync_playwright
        payload=camera_jpeg()
        with application_runtime() as (server,base,photos,store,watch,sid,url),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(url,wait_until='domcontentloaded')
                page.wait_for_function('()=>!FramekeepBoot.loading&&document.querySelector("#photos img")?.naturalWidth>0')
                held=[];first_key=[None]
                def route(request):
                    if first_key[0] is None:first_key[0]=request.request.url.split('?')[0]
                    if request.request.url.split('?')[0]==first_key[0]:held.append(request)
                    else:request.continue_()
                page.route('**/api/watch/preview/**',route)
                with (photos/'SLOW_OLD.JPG').open('wb') as writer:
                    writer.write(payload);writer.flush();page.wait_for_timeout(200);self.assertTrue(held)
                    (photos/'FAST_NEW.JPG').write_bytes(payload)
                    page.wait_for_function('()=>!FramekeepReceiving.active&&document.querySelector("#photos [data-loupe] img")?.alt==="FAST_NEW.JPG"&&document.querySelector("#photos [data-loupe] img")?.naturalWidth===3600',timeout=3000)
                    for request in held:request.continue_()
                page.wait_for_function('()=>DEMO_PHOTOS.length===3')
                page.wait_for_timeout(150)
                self.assertEqual(page.locator('#filename').inner_text(),'FAST_NEW.JPG')
                self.assertFalse(page.evaluate('FramekeepReceiving.active'))
            finally:browser.close()

    def test_first_frame_is_original_resolution_before_writer_closes(self):
        from playwright.sync_api import sync_playwright
        out=ROOT/'build/dpp-sharp-direct';out.mkdir(parents=True,exist_ok=True)
        payload=camera_jpeg()
        with application_runtime() as (server,base,photos,store,watch,sid,url),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':960});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url,wait_until='domcontentloaded')
                page.wait_for_function('()=>!FramekeepBoot.loading&&document.querySelector("#photos img")?.naturalWidth>0')
                first=store.records(sid)[0]['id'];timings=[]
                for index,hold in enumerate((1,3,5)):
                    name=f'DIRECT_{index}.JPG';target=photos/name
                    page.evaluate('''name=>{window.directFrames=[];const capture=()=>{for(const img of document.querySelectorAll('#photos img,.receiving-photo-preview img'))if(img.alt===name&&img.complete&&img.naturalWidth)directFrames.push({width:img.naturalWidth,height:img.naturalHeight,src:img.src})};window.directFrameObserver?.disconnect();window.directFrameObserver=new MutationObserver(capture);directFrameObserver.observe(document.getElementById('photo-stage'),{subtree:true,childList:true,attributes:true,attributeFilter:['src']});document.getElementById('photo-stage').addEventListener('load',capture,true)}''',name)
                    with target.open('wb') as writer:
                        started=time.perf_counter();writer.write(payload);writer.flush()
                        page.wait_for_function('(name)=>document.querySelector(".receiving-photo-preview img")?.alt===name&&document.querySelector(".receiving-photo-preview img")?.naturalWidth===3600',arg=name,timeout=1500)
                        shown=(time.perf_counter()-started)*1000
                        frame=page.locator('.receiving-photo-preview img')
                        self.assertEqual(frame.evaluate('(img)=>[img.naturalWidth,img.naturalHeight]'),list(SIZE))
                        self.assertEqual(len(store.records(sid)),index+1,'写句柄尚未释放，不应提前写正式资料')
                        self.assertTrue(page.evaluate('FramekeepReceiving.active'))
                        page.keyboard.press('2');self.assertFalse(page.evaluate('(id)=>Framekeep.assignRemoteRating(id,2)',first))
                        self.assertFalse(store.session(sid)['state'].get('overrides'))
                        if hold==5:page.screenshot(path=str(out/'direct-original-during-write.png'))
                        page.wait_for_timeout(hold*1000)
                        self.assertEqual(frame.evaluate('(img)=>[img.naturalWidth,img.naturalHeight]'),list(SIZE))
                        self.assertEqual(len(store.records(sid)),index+1)
                    page.wait_for_function('(name)=>document.getElementById("filename").textContent===name&&!FramekeepReceiving.active&&document.querySelector("#photos [data-loupe] img")?.naturalWidth===3600',arg=name)
                    page.wait_for_timeout(250)
                    self.assertEqual(page.locator('#photos [data-loupe] img').evaluate('(img)=>[img.naturalWidth,img.naturalHeight]'),list(SIZE))
                    widths=page.evaluate('directFrames.map(frame=>frame.width)')
                    self.assertTrue(widths,'没有记录到可见帧')
                    self.assertEqual(set(widths),{SIZE[0]},'不允许先480/2048再原图，或原图后再降清晰度')
                    timings.append({'writer_hold_seconds':hold,'jpeg_bytes_available_to_first_frame_ms':shown,'first_frame_pixels':list(SIZE)})
                self.assertFalse(errors,errors)
                page.screenshot(path=str(out/'direct-original-complete.png'))
                (out/'direct-browser.json').write_text(json.dumps({'times':timings,'page_errors':errors,'no_resolution_downgrade':True,'rating_protected':True},ensure_ascii=False,indent=2),encoding='utf8')
            finally:browser.close()

    def test_formal_monitor_lock_cannot_block_direct_notification_or_image(self):
        from playwright.sync_api import sync_playwright
        with application_runtime() as (server,base,photos,store,watch,sid,url),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':960});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url,wait_until='domcontentloaded')
                page.wait_for_function('()=>!FramekeepBoot.loading&&document.querySelector("#photos img")?.naturalWidth>0')
                # 这一把锁原本会挡住 FolderWatch.state/正式入库；独立显示必须继续。
                with watch.lock:
                    target=photos/'LOCKED.JPG';target.write_bytes(camera_jpeg())
                    page.wait_for_function('()=>document.querySelector(".receiving-photo-preview img")?.alt==="LOCKED.JPG"&&document.querySelector(".receiving-photo-preview img")?.naturalWidth===3600',timeout=1500)
                    direct=watch.direct.state()
                    with urllib.request.urlopen(url+'/api/watch/direct?after=-1&wait=0',timeout=.5) as response:state=json.load(response)
                    self.assertEqual(state['receiving']['id'],direct['receiving']['id'])
                    with urllib.request.urlopen(url+state['receiving']['url'],timeout=2) as response:self.assertEqual(response.read(),target.read_bytes())
                    self.assertEqual(len(store.records(sid)),1)
                page.wait_for_function('()=>!FramekeepReceiving.active&&document.getElementById("filename").textContent==="LOCKED.JPG"')
                self.assertFalse(errors,errors)
            finally:browser.close()


if __name__=='__main__':unittest.main()
