"""真实页面验证选中项定位、虚拟列表回滚及运行期小图共享。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import json
from collections import Counter
import threading
import unittest
from urllib.parse import urlsplit,parse_qs
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
import test_session_browser as fixtures
from folder_watch import FolderWatch

ROOT=Path(__file__).resolve().parents[1]
CHROME='C:/Program Files/Google/Chrome/Application/chrome.exe'


class LiveThumbnailMemoryBrowserTests(unittest.TestCase):
    def test_selected_position_and_no_repeated_reads_across_virtual_views(self):
        import server
        fixture=fixtures.SessionBrowserTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        store=fixture.store;store.activate(fixture.sid)
        image=BytesIO();Image.new('RGB',(120,80),'teal').save(image,'JPEG')
        for record in fixture.records:
            path=fixture.photos/record['jpg'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(image.getvalue())
        watch=FolderWatch(store,autostart=False);watch.start(str(fixture.photos))
        values=dict(STORE=store,DATA=store.data,MODELS=None,ARCHIVE=None,CATALOG=None,ANALYSIS=None,SKU=None,WATCH=watch,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,REFERENCES=None,PREVIEWS=None,DIRECT=None)
        out=ROOT/'build/live-thumbnail-memory';out.mkdir(parents=True,exist_ok=True)
        with ExitStack() as stack:
            for name,value in values.items():stack.enter_context(patch.object(server,name,value))
            stack.enter_context(patch.object(server,'workbench_views',return_value=None))
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            stack.callback(http.server_close);stack.callback(http.shutdown)
            with sync_playwright() as pw, pw.chromium.launch(executable_path=CHROME,headless=True) as browser:
                page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];reads=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def requested(request):
                    url=urlsplit(request.url);query=parse_qs(url.query)
                    if url.path.startswith('/api/preview/') and int(query.get('size',['2048'])[0])<=600:reads.append(request.url)
                page.on('request',requested)
                page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                page.wait_for_function('()=>window.FramekeepBoot?.loading===false&&!!window.FramekeepThumbnailMemory')
                def settled(selector):
                    page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                    try:
                        page.wait_for_function('''selector=>{const images=[...document.querySelectorAll(selector)].filter(i=>{const r=i.getBoundingClientRect(),b=i.closest('#catalog-view,#watch-thumbnails').getBoundingClientRect();return r.width>0&&r.bottom>b.top&&r.top<b.bottom});return images.length>0&&images.every(i=>i.complete&&i.naturalWidth>0)&&!FramekeepThumbnailMemory.stats.active&&!FramekeepThumbnailMemory.stats.queued}''',arg=selector)
                    except Exception:
                        page.screenshot(path=str(out/'failure.png'))
                        (out/'failure.json').write_text(json.dumps(page.evaluate('''()=>({stats:FramekeepThumbnailMemory.stats,images:[...document.querySelectorAll('img[data-memory-thumbnail]')].map(i=>({key:i.dataset.memoryThumbnail,src:i.src,ready:i.complete,width:i.naturalWidth,rect:i.getBoundingClientRect().toJSON()}))})'''),ensure_ascii=False,indent=2),encoding='utf8')
                        raise
                def focused(view):
                    selector='#photo-grid .focused' if view=='grid' else '#photo-list .focused'
                    page.wait_for_function('''selector=>{const el=document.querySelector(selector),box=document.getElementById('catalog-view');if(!el)return false;const r=el.getBoundingClientRect(),b=box.getBoundingClientRect();return r.top>=b.top-1&&r.bottom<=b.bottom+1}''',arg=selector)
                    self.assertEqual(page.locator(selector).get_attribute('data-photo'),'P00180')
                page.wait_for_function('()=>FramekeepLayout.isMonitoring()')
                page.evaluate('Framekeep.browsePhoto("P00180")')
                settled('#watch-thumbnails img')
                for view in ('grid','list','single','grid','list'):
                    page.locator(f'[data-view="{view}"]').click()
                    if view!='single':focused(view)
                # 左栏已加载的选中图在两种中央布局均使用相同blob。
                shared=page.evaluate('''()=>({side:document.querySelector('[data-watch-photo="P00180"] img').src,main:document.querySelector('#photo-list .focused img').src})''')
                self.assertEqual(shared['side'],shared['main']);self.assertTrue(shared['main'].startswith('blob:'))
                checks=[]
                for view,container,images in [('grid','#catalog-view','#photo-grid img'),('list','#catalog-view','#photo-list img'),('sidebar','#watch-thumbnails','#watch-thumbnails img')]:
                    if view!='sidebar':page.locator(f'[data-view="{view}"]').click()
                    else:page.locator('[data-view="single"]').click()
                    page.locator(container).evaluate('(el)=>el.scrollTop=0');settled(images)
                    first=page.locator(images).first.get_attribute('src')
                    page.locator(container).evaluate('(el)=>el.scrollTop=el.scrollHeight');settled(images)
                    count=len(reads)
                    page.locator(container).evaluate('(el)=>el.scrollTop=0');settled(images)
                    self.assertEqual(len(reads),count,(view,'回滚不应重新读取源文件'))
                    self.assertEqual(page.locator(images).first.get_attribute('src'),first)
                    checks.append({'view':view,'reads_before_return':count,'reads_after_return':len(reads),'nodes':page.locator(images).count()})
                    self.assertLess(page.locator(images).count(),100)
                stats=page.evaluate('FramekeepThumbnailMemory.stats')
                page.screenshot(path=str(out/'verified.png'))
                self.assertFalse(errors,errors)
                (out/'reads.json').write_text(json.dumps({'reads':reads,'duplicates':{k:v for k,v in Counter(reads).items() if v>1},'stats':stats},ensure_ascii=False,indent=2),encoding='utf8')
                self.assertEqual(len(reads),len(set(reads)),'同一版本的小图跨布局只读取一次')
                self.assertFalse(list(store.data.rglob('*.webp')))
                page.reload(wait_until='domcontentloaded')
                page.wait_for_function('()=>window.FramekeepBoot?.loading===false')
                settled('#watch-thumbnails img')
                self.assertGreater(len(reads),len(set(reads)),'刷新页面必须重新读取，不能持久保存小图')
                (out/'result.json').write_text(json.dumps({'checks':checks,'stats':stats,'shared':shared,'page_errors':errors,'reload_reads_again':True},ensure_ascii=False,indent=2),encoding='utf8')

    def test_request_merging_versions_failures_and_pagehide_release(self):
        out=BytesIO();Image.new('RGB',(80,120),'coral').save(out,'JPEG');payload=out.getvalue()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=CHROME,headless=True)
            try:
                page=browser.new_page();errors=[];calls={}
                page.on('pageerror',lambda error:errors.append(str(error)))
                def route(request):
                    path=urlsplit(request.request.url).path
                    if path=='/':request.fulfill(content_type='text/html',body='<html><body></body></html>');return
                    calls[path]=calls.get(path,0)+1
                    if path.endswith('/retry') and calls[path]==1:request.fulfill(status=503,body='busy');return
                    if path.endswith('/corrupt') and calls[path]==1:request.fulfill(content_type='image/jpeg',body=b'not an image');return
                    request.fulfill(content_type='image/jpeg',body=payload,headers={'Cache-Control':'no-store'})
                page.route('http://thumbnail.test/**',route)
                page.goto('http://thumbnail.test/');page.add_script_tag(path=str(ROOT/'演示方案/thumbnail-memory.js'))
                page.evaluate('''()=>{for(const source of ['/api/preview/a/shared?size=480&v=1','/api/preview/a/shared?v=1&instant=1&size=480','/api/preview/a/retry?size=480&v=1','/api/preview/a/corrupt?size=480&v=1']){const image=new Image();document.body.append(image);FramekeepThumbnailMemory.bind(image,source)}}''')
                page.wait_for_function('()=>[...document.images].every(i=>i.complete&&i.naturalWidth>0)&&FramekeepThumbnailMemory.stats.entries===3')
                self.assertEqual(calls['/api/preview/a/shared'],1);self.assertEqual(calls['/api/preview/a/retry'],2);self.assertEqual(calls['/api/preview/a/corrupt'],2)
                page.evaluate('''()=>{window.oldSource=document.images[0].src;FramekeepThumbnailMemory.bind(document.images[0],'/api/preview/a/shared?size=480&v=2')}''')
                page.wait_for_function('()=>document.images[0].src!==oldSource&&document.images[0].complete&&document.images[0].naturalWidth>0')
                self.assertEqual(calls['/api/preview/a/shared'],2)
                self.assertNotEqual(page.locator('img').nth(0).get_attribute('src'),page.locator('img').nth(1).get_attribute('src'))
                self.assertIsNone(page.evaluate('FramekeepThumbnailMemory.key("/api/preview/a/shared?size=2048&v=1")'))
                page.evaluate('window.dispatchEvent(new PageTransitionEvent("pagehide"))')
                self.assertEqual(page.evaluate('FramekeepThumbnailMemory.stats.entries'),0)
                self.assertEqual(page.evaluate('FramekeepThumbnailMemory.stats.bytes'),0)
                self.assertEqual(page.evaluate('FramekeepThumbnailMemory.stats.decodedBytes'),0)
                self.assertFalse(errors,errors)
            finally:browser.close()


if __name__=='__main__':unittest.main()
