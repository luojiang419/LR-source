from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from mouse_native.interception import Interception
from mouse_remote import MouseRemote
import media_engine as media
import test_mouse_remote as fixture
from test_mouse_options import feed

ROOT=Path(__file__).resolve().parents[1]


class MouseOptionsBrowserTests(unittest.TestCase):
    def test_switches_decrease_wheel_order_and_delayed_targets(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos);store.activate(sid)
            for i in range(4):
                p=photos/f'{i}.JPG';Image.new('RGB',(300,200),(50+i*30,90,120)).save(p);store.append_live(sid,[media.capture_record(photos,p,None,{})])
            ids=[r['id'] for r in store.records(sid)];store.save(sid,0,{'view':'single','selectedId':ids[0],'overrides':dict.fromkeys(ids,0)})
            transport=fixture.FakeTransport();remote=MouseRemote(lambda _,click,error:Interception(None,click,error,transport=transport),fixture.FakeHotkey,lease_factory=fixture.FakeLease,legacy_guard=lambda:None,settings_path=store.data/'mouse-remote.json')
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('MOUSE',remote),('WATCH',None),('SKU',None),('ARCHIVE',None),('ANALYSIS',None),('MODELS',None),('RAWLINK',None),('FILESEARCH',None)]]
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle');page.locator('#app-settings-open').click();page.locator('#mouse-remote').click()
                        self.assertEqual(page.locator('#mouse-right-decrease').get_attribute('aria-checked'),'false');self.assertEqual(page.locator('#mouse-wheel-browse').get_attribute('aria-checked'),'false')
                        page.locator('#mouse-start').click();page.wait_for_function('()=>!document.getElementById("mouse-dialog").open')
                        def rating(n):page.wait_for_function('(n)=>document.querySelectorAll("#stars .on").length===n',arg=n)
                        def current(cid):page.wait_for_function('(id)=>Framekeep.remoteTarget().photoId===id',arg=cid)
                        def toggle(id,enabled):
                            page.locator('#app-settings-open').click();page.locator('#mouse-remote').click();page.locator(id).click();page.wait_for_function('(v)=>document.querySelector(v[0]).getAttribute("aria-checked")===v[1]',arg=[id,str(enabled).lower()]);page.locator('#mouse-close').click();page.locator('#app-settings-close').click()
                        page.locator('#stars [data-rating="3"]').click();feed(transport,(4,0),(8,0),(0x400,-120));page.wait_for_timeout(250);rating(3);current(ids[0]);self.assertEqual(remote.sequence,0)
                        # 设置页保留设备连接，但遥控点击不能修改后台照片；返回选片后恢复。
                        page.locator('#app-settings-open').click();feed(transport,(1,0),(2,0));page.wait_for_timeout(250)
                        self.assertEqual(page.evaluate('(id)=>Framekeep.persistedState().overrides[id]',ids[0]),3)
                        page.locator('#app-settings-close').click();rating(3)
                        toggle('#mouse-right-decrease',True);feed(transport,(4,0),(4,0),(4,0));rating(2)
                        feed(transport,(8,0),(4,0),(8,0),(4,0),(8,0));rating(0)
                        feed(transport,(4,0),(8,0));page.wait_for_timeout(200);rating(0);page.keyboard.press('Control+z');rating(1)
                        toggle('#mouse-wheel-browse',True);page.locator('#stars [data-rating="3"]').click()
                        feed(transport,(0x400,-120),(1,0),(2,0),(4,0),(8,0),(1,0),(2,0));current(ids[1]);rating(1)
                        self.assertEqual(page.evaluate('(id)=>Framekeep.persistedState().overrides[id]',ids[0]),3)
                        feed(transport,(0x400,-360));current(ids[3]);feed(transport,(0x400,600));current(ids[0])
                        delayed=[]
                        def delay(route):
                            response=route.fetch()
                            if response.json().get('events') and not delayed:page.evaluate('(id)=>Framekeep.browsePhoto(id)',ids[2]);delayed.append(True)
                            route.fulfill(response=response)
                        page.route('**/api/mouse/poll',delay);feed(transport,(0x400,-120),(1,0),(2,0));page.wait_for_function('(id)=>Framekeep.persistedState().overrides[id]===2',arg=ids[1]);current(ids[2]);rating(0);page.unroute('**/api/mouse/poll',delay);self.assertTrue(delayed)
                        # 输入发生时末张没有下一张：后来入库的新图不能承接旧点击。
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',ids[3]);arrived=[]
                        def arrive(route):
                            response=route.fetch()
                            if response.json().get('events') and not arrived:
                                file=photos/'4.JPG';Image.new('RGB',(300,200),'teal').save(file);record=media.capture_record(photos,file,None,{});store.append_live(sid,[record]);arrived.append(record['id']);page.evaluate('async(v)=>await FramekeepIO.refreshLive(v[0],v[1])',[sid,record['id']])
                            route.fulfill(response=response)
                        page.route('**/api/mouse/poll',arrive);feed(transport,(0x400,-120),(1,0),(2,0));page.wait_for_function('(id)=>Framekeep.persistedState().overrides[id]===1',arg=ids[3]);self.assertTrue(arrived);current(arrived[0]);rating(0);page.unroute('**/api/mouse/poll',arrive)
                        page.evaluate('()=>FramekeepIO.flush()');saved=store.session(sid)['state']['overrides'];self.assertEqual([saved[i] for i in ids],[3,2,0,1])
                        # 评级排序变化即使没有改变当前照片，也须刷新后续滚轮的导航顺序。
                        page.locator('#sort').select_option('rating-desc');page.evaluate('(id)=>Framekeep.browsePhoto(id)',ids[0])
                        page.evaluate('(id)=>{for(let i=0;i<3;i++)Framekeep.incrementRating(id)}',ids[3]);current(ids[0])
                        feed(transport,(0x400,120));current(ids[3]);rating(4)
                        page.locator('#app-settings-open').click();page.locator('#mouse-remote').click();out=ROOT/'build/mouse-options-acceptance';out.mkdir(exist_ok=True);page.screenshot(path=str(out/'options-browser.png'))
                        page.locator('#mouse-stop').click()
                        page.wait_for_function('()=>document.getElementById("mouse-start").disabled===false');page.reload(wait_until='networkidle');page.locator('#app-settings-open').click();page.locator('#mouse-remote').click();page.wait_for_function('()=>document.getElementById("mouse-right-decrease").getAttribute("aria-checked")==="true" && document.getElementById("mouse-wheel-browse").getAttribute("aria-checked")==="true"');self.assertEqual(page.locator('#mouse-right-decrease').get_attribute('aria-checked'),'true');self.assertEqual(page.locator('#mouse-wheel-browse').get_attribute('aria-checked'),'true');self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                remote.close();http.shutdown();http.server_close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
