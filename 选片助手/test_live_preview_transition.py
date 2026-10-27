"""真实目录/HTTP/浏览器：新图先显、清晰图升级、淡入与并发操作保护。"""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from folder_watch import FolderWatch

ROOT=Path(__file__).resolve().parents[1]


class LivePreviewTests(unittest.TestCase):
    def test_progressive_arrival_transition_bursts_and_manual_navigation(self):
        import server
        out=ROOT/'build/live-preview-speed';out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory() as temp,ExitStack() as patches:
            base=Path(temp);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT)
            watch=FolderWatch(store,autostart=False)
            def picture(name,color='green'):
                p=photos/name;Image.new('RGB',(1200,800),color).save(p);return hashlib.sha256(name.casefold().encode()).hexdigest()[:24]
            sid=watch.start(str(photos))['session_id'];store.activate(sid)
            patches.enter_context(patch('media_engine.read_metadata',side_effect=lambda paths,*a,**kw:[{} for _ in paths]))
            picture('FIRST.JPG');watch.tick();watch.thread.start()
            for name,value in dict(STORE=store,DATA=store.data,WATCH=watch,CANON=None,PREVIEWS=None,
                                   MOUSE=None,MODELS=None,SKU=None,ARCHIVE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None).items():
                patches.enter_context(patch.object(server,name,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{http.server_port}'
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':900},reduced_motion='no-preference');errors=[]
                        page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(url,wait_until='domcontentloaded')
                        page.wait_for_function('()=>!FramekeepBoot.loading&&document.querySelector("#photos img")?.naturalWidth>0')
                        page.evaluate('''()=>{window.arrivalOverlays=0;new MutationObserver(ms=>{for(const m of ms)for(const n of m.addedNodes)if(n.classList?.contains('live-photo-outgoing'))arrivalOverlays++}).observe(document.getElementById('photos'),{childList:true})}''')
                        held=[];large=[];hold_id=hashlib.sha256(b'second.jpg').hexdigest()[:24];hold_detail=True
                        def route(request):
                            address=request.request.url
                            if hold_id and hold_id in address and 'size=480' in address:held.append(request)
                            elif hold_detail and hashlib.sha256(b'second.jpg').hexdigest()[:24] in address and 'size=2048' in address:large.append(request)
                            else:request.continue_()
                        page.route('**/api/preview/**',route)
                        picture('SECOND.JPG','red')
                        page.wait_for_function('()=>true');page.wait_for_timeout(300)
                        self.assertTrue(held);self.assertEqual(page.locator('#filename').inner_text(),'FIRST.JPG')
                        self.assertEqual(page.locator('#photos [data-loupe] img').get_attribute('alt'),'FIRST.JPG')
                        hold_id=None
                        for request in held:request.continue_()
                        held.clear()
                        page.wait_for_function('()=>document.getElementById("filename").textContent==="SECOND.JPG"&&document.querySelector("#photos [data-loupe] img")?.naturalWidth>0')
                        self.assertIn('size=480',page.locator('#photos [data-loupe] img').get_attribute('src'))
                        page.wait_for_function('()=>arrivalOverlays===1')
                        page.evaluate('()=>FramekeepViewer.setScale(2)')
                        hold_detail=False
                        for request in large:request.continue_()
                        large.clear()
                        page.wait_for_function('()=>document.querySelector("#photos [data-loupe] img")?.src.includes("size=2048")')
                        page.evaluate('()=>Framekeep.syncSharedRatings({})')
                        self.assertEqual(page.evaluate('FramekeepViewer.getScale()'),2)
                        page.wait_for_function('()=>!document.querySelector(".live-photo-outgoing")')
                        page.emulate_media(reduced_motion='reduce');picture('THIRD.JPG','blue')
                        page.wait_for_function('()=>document.getElementById("filename").textContent==="THIRD.JPG"')
                        self.assertEqual(page.evaluate('arrivalOverlays'),1)
                        for i in range(4):picture(f'BURST_{i}.JPG','navy')
                        page.wait_for_function('()=>DEMO_PHOTOS.length===7&&document.getElementById("filename").textContent==="BURST_3.JPG"')
                        generation=watch.state()['generation'];version=watch.state()['version']
                        with urllib.request.urlopen(url+f'/api/state?session_id={sid}&live_after={version}&live_generation={generation}') as r:delta=json.load(r)
                        self.assertTrue(delta['catalog_delta']);self.assertEqual(delta['catalog'],[])
                        with urllib.request.urlopen(url+f'/api/state?session_id={sid}&live_after=0&live_generation=stale') as r:fallback=json.load(r)
                        self.assertFalse(fallback.get('catalog_delta'));self.assertEqual(len(fallback['catalog']),7)
                        for name,manual in [('MANUAL.JPG',True),('PAUSED.JPG',False)]:
                            hold_id=picture(name,'purple');page.wait_for_timeout(300);self.assertTrue(held)
                            if manual:page.locator('#previous').click()
                            else:page.evaluate('()=>FramekeepWatch.setFollow(false)')
                            selected=page.locator('#filename').inner_text();hold_id=None
                            for request in held:request.continue_()
                            held.clear()
                            expected=8 if manual else 9
                            page.wait_for_function('(n)=>DEMO_PHOTOS.length===n',arg=expected)
                            self.assertEqual(page.locator('#filename').inner_text(),selected)
                        # 从小目录切入分页模式时，仍定位新拍摄图，不能把增量当成完整页。
                        patches.enter_context(patch.object(server.session_browser,'LARGE_SESSION',9))
                        page.evaluate('()=>FramekeepWatch.setFollow(true)');picture('PAGED.JPG','orange')
                        page.wait_for_function('()=>FRAMEKEEP_SESSION.browser?.paged&&document.getElementById("filename").textContent==="PAGED.JPG"')
                        self.assertFalse(errors,errors)
                        page.screenshot(path=str(out/'transition-acceptance.png'))
                        (out/'transition-acceptance.json').write_text(json.dumps({'passed':True,'oldFrameUntilReady':True,'progressivePreview':True,'upgradeKeepsZoom':True,'crossfade':True,'reducedMotion':True,'burstCount':4,'deltaFallback':True,'manualNavigationProtected':True,'pauseProtected':True,'pageErrors':errors}),encoding='utf8')
                    finally:browser.close()
            finally:watch.close();http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
