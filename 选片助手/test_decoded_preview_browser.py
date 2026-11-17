"""真实浏览器：无 HTTP 缓存下的解码图复用、预取取消及有界资源。"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('FRAMEKEEP_TEST_VIEWER_SCRIPT', ROOT / '演示方案/asset-viewer.js'))
CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'


@contextmanager
def viewer(script=SCRIPT, delay=.12):
    requests = []
    pixels = {}
    for edge in (420, 2048):
        image = Image.new('RGB', (edge, edge * 2 // 3), '#648b88')
        draw = ImageDraw.Draw(image)
        for x in range(0, edge, 20):
            draw.line((x, 0, edge-x, image.height), fill='#e7ca92', width=3)
        output = BytesIO(); image.save(output, 'JPEG', quality=85); pixels[edge] = output.getvalue()
    html = '''<!doctype html><meta charset="utf-8"><style>
      dialog {width:900px;height:700px} #asset-viewer-stage {position:relative;width:600px;height:400px;overflow:hidden}
      #asset-viewer-stage img {position:absolute;left:0;top:0} [hidden] {display:none!important}
      </style><body><script>
      window.createdImages=[];const OriginalImage=Image;
      window.Image=function(...args){const image=new OriginalImage(...args);createdImages.push(image);return image};
      window.Image.prototype=OriginalImage.prototype;
      window.FramekeepIO={api:async()=>({item:{},files:[],metadata_status:'failed'})};
      window.FramekeepAssetLoading={markup:()=>'',pause(){},resume(){}};
      window.rows=Array.from({length:24},(_,i)=>({id:String(i),name:'image-'+i,media_type:'image',preview:'/image/'+i+'?v=1'}));
      </script><script src="/viewer.js"></script>'''

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            if self.path.startswith('/image/'):
                query = parse_qs(urlparse(self.path).query)
                edge = int(query['size'][0]); requests.append(self.path)
                time.sleep(delay)
                body, mime = pixels[edge], 'image/jpeg'
            elif self.path == '/viewer.js': body, mime = script.read_bytes(), 'text/javascript'
            else: body, mime = html.encode(), 'text/html; charset=utf-8'
            try:
                self.send_response(200); self.send_header('Content-Type', mime)
                self.send_header('Cache-Control', 'no-store'); self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler); http.daemon_threads = True
    threading.Thread(target=http.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME, headless=True)
            try:
                page = browser.new_page(viewport={'width':1200, 'height':900}, reduced_motion='reduce')
                errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{http.server_port}', wait_until='load')
                yield page, requests, errors
            finally: browser.close()
    finally: http.shutdown(); http.server_close()


READY = "()=>document.getElementById('asset-viewer-stage').dataset.previewState==='ready'"
OPEN = "i=>FramekeepAssetViewer.open('archive',rows,String(i))"


def measure_navigation(page, button):
    return page.evaluate('''button=>new Promise(resolve=>{
      const start=performance.now();document.getElementById('asset-image-viewer').dispatchEvent(new KeyboardEvent('keydown',{key:button.endsWith('next')?'ArrowRight':'ArrowLeft',bubbles:true}));
      const immediatelyReady=document.getElementById('asset-viewer-stage').dataset.previewState==='ready';
      const wait=()=>{
        if(document.getElementById('asset-viewer-stage').dataset.previewState==='ready')
          requestAnimationFrame(()=>requestAnimationFrame(()=>resolve({ms:performance.now()-start,immediatelyReady})));
        else requestAnimationFrame(wait);
      };wait();
    })''', button)


def benchmark(script):
    with viewer(script) as (page, requests, errors):
        page.evaluate(OPEN, 8); page.wait_for_function(READY); page.wait_for_timeout(650)
        samples = []
        for _ in range(6):
            for button in ('asset-viewer-next', 'asset-viewer-prev'):
                samples.append(measure_navigation(page, button)); page.wait_for_timeout(170)
        return {'samples':samples, 'large_requests':len([url for url in requests if 'size=2048' in url]),
                'errors':errors, 'http_cache':'no-store', 'response_delay_ms':120,
                'measurement':'click → decoded image visible → two requestAnimationFrame callbacks'}


class DecodedPreviewBrowserTests(unittest.TestCase):
    def test_prefetched_and_revisited_images_are_visible_synchronously_without_requests(self):
        with viewer() as (page, requests, errors):
            page.evaluate(OPEN, 8); page.wait_for_function(READY); page.wait_for_timeout(650)
            for button, item in [('asset-viewer-next', '9'), ('asset-viewer-prev', '8')]:
                before = len([url for url in requests if url.startswith('/image/'+item+'?') and 'size=2048' in url])
                result = measure_navigation(page, button)
                self.assertTrue(result['immediatelyReady'], result)
                self.assertEqual(len([url for url in requests if url.startswith('/image/'+item+'?') and 'size=2048' in url]), before)
                self.assertEqual(page.locator('#asset-viewer-image').get_attribute('alt'), 'image-'+item)
            self.assertFalse(errors, errors)

    def test_pool_is_bounded_versioned_and_close_releases_pending_images(self):
        with viewer() as (page, requests, errors):
            page.evaluate(OPEN, 0); page.wait_for_function(READY)
            for _ in range(15):
                page.locator('#asset-viewer-stage').focus();page.keyboard.press('ArrowRight'); page.wait_for_function(READY)
                footprint = page.evaluate('''()=>{const images=createdImages.filter(image=>image.getAttribute('src')?.includes('size=2048'));
                  return {count:images.length,bytes:images.reduce((n,image)=>n+(image.naturalWidth*image.naturalHeight*4||2048*2048*4),0)}}''')
                self.assertLessEqual(footprint['count'], 6, footprint)
                self.assertLessEqual(footprint['bytes'], 96*1024*1024, footprint)
            page.evaluate("()=>{rows[15].preview='/image/15?v=2';FramekeepAssetViewer.open('archive',rows,'15')}")
            self.assertEqual(page.locator('#asset-viewer-stage').get_attribute('data-preview-state'), 'loading')
            page.wait_for_function(READY)
            self.assertTrue(any('/image/15?v=2&size=2048' == url for url in requests), requests)
            page.evaluate("()=>{rows[15].preview='/image/15?v=2&frame=3';FramekeepAssetViewer.open('archive',rows,'15')}")
            self.assertEqual(page.locator('#asset-viewer-stage').get_attribute('data-preview-state'), 'loading')
            page.wait_for_function(READY)
            self.assertIn('frame=3', page.locator('#asset-viewer-image').get_attribute('src'))
            page.locator('#asset-viewer-stage').focus();page.keyboard.press('ArrowRight'); page.locator('#asset-viewer-close').click()
            page.wait_for_timeout(300)
            self.assertEqual(page.evaluate("()=>createdImages.filter(image=>image.getAttribute('src')?.includes('size=2048')).length"), 0)
            self.assertFalse(page.locator('#asset-image-viewer').evaluate('e=>e.open'))
            self.assertFalse(errors, errors)

    def test_unexpected_oversized_current_is_not_retained_or_prefetched(self):
        with viewer() as (page, requests, errors):
            # 注入异常尺寸，不分配真实 256MiB 图片；验证超预算状态的资源释放。
            page.evaluate('''()=>{
              for(const key of ['naturalWidth','naturalHeight']){
                const original=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,key).get;
                Object.defineProperty(HTMLImageElement.prototype,key,{configurable:true,get(){
                  return this.src.includes('/image/8?')&&this.src.includes('size=2048')?8192:original.call(this);
                }});
              }
            }''')
            page.evaluate(OPEN, 8); page.wait_for_function(READY); page.wait_for_timeout(350)
            self.assertEqual(len([url for url in requests if 'size=2048' in url]), 1)
            page.locator('#asset-viewer-stage').focus();page.keyboard.press('ArrowRight'); page.wait_for_function(READY)
            self.assertFalse(page.evaluate("()=>createdImages.some(image=>image.getAttribute('src')?.includes('/image/8?')&&image.src.includes('size=2048'))"))
            page.locator('#asset-viewer-close').click(); page.wait_for_timeout(200)
            self.assertFalse(page.evaluate("()=>createdImages.some(image=>image.getAttribute('src')?.includes('size=2048'))"))
            self.assertFalse(errors, errors)

    def test_fast_navigation_cancels_unneeded_requests_without_late_overwrite(self):
        with viewer(delay=.25) as (page, requests, errors):
            page.evaluate(OPEN, 5); page.wait_for_function(READY); page.wait_for_timeout(100)
            # 邻图预取尚在途时跳到窗口外；旧 Image 必须取消，不能迟到覆盖新图。
            page.evaluate("()=>FramekeepAssetViewer.open('archive',rows,'20')")
            state = page.evaluate("()=>createdImages.filter(image=>image.getAttribute('src')?.includes('size=2048')).map(image=>image.src)")
            self.assertFalse(any('/image/6?' in url for url in state), state)
            page.wait_for_function(READY); page.wait_for_timeout(350)
            self.assertEqual(page.locator('#asset-viewer-image').get_attribute('alt'), 'image-20')
            self.assertIn('/image/20?', page.locator('#asset-viewer-image').get_attribute('src'))
            self.assertFalse(errors, errors)


if __name__ == '__main__': unittest.main()
