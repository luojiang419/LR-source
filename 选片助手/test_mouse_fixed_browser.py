"""真实鼠标协议包到固定星级、保存和归档共享的浏览器验收。"""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright

from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from mouse_native.interception import Interception
from mouse_remote import MouseRemote
import media_engine as media
import test_mouse_remote as fixture
from test_mouse_options import feed

ROOT=Path(__file__).resolve().parents[1]


class FixedMouseBrowserTests(unittest.TestCase):
    def test_fixed_assignment_repeat_undo_delayed_target_and_shared_archive(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir()
            store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos);store.activate(sid)
            for i in range(3):
                path=photos/f'{i}.JPG';Image.new('RGB',(300,200),(50+i*35,90,140)).save(path)
                store.append_live(sid,[media.capture_record(photos,path,None,{})])
            ids=[r['id'] for r in store.records(sid)]
            store.save(sid,0,{'view':'single','selectedId':ids[0],'overrides':dict.fromkeys(ids,0)})
            archive=AssetArchive(store,autostart=False);archive.ratings.seed_session(sid)
            catalog=AssetCatalog(store.data);catalog.add_root(str(photos))
            transport=fixture.FakeTransport()
            remote=MouseRemote(lambda _,click,error:Interception(None,click,error,transport=transport),fixture.FakeHotkey,lease_factory=fixture.FakeLease,legacy_guard=lambda:None,settings_path=store.data/'mouse-remote.json')
            values={'STORE':store,'DATA':store.data,'MOUSE':remote,'WATCH':None,'SKU':None,'ARCHIVE':archive,'CATALOG':catalog,'ANALYSIS':None,'MODELS':None,'RAWLINK':None,'FILESEARCH':None,'UPDATES':None}
            patches=[patch.object(server,key,value,create=key=='UPDATES') for key,value in values.items()]
            for item in patches:item.start()
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle')
                        def panel():page.locator('#app-settings-open').click();page.locator('#mouse-remote').click()
                        def mode(value):
                            page.locator('#mouse-rating-mode').select_option(value)
                            page.wait_for_function('(value)=>document.getElementById("mouse-rating-mode").value===value&&!document.getElementById("mouse-rating-mode").disabled',arg=value)
                        def rating(value):page.wait_for_function('(value)=>document.querySelectorAll("#stars .on").length===value',arg=value)
                        def close_panel():page.locator('#mouse-close').click();page.locator('#app-settings-close').click()
                        def repeated(packets):
                            sequence=remote.sequence
                            for index in range(0,len(packets),2):
                                page.wait_for_timeout(550);feed(transport,*packets[index:index+2])
                            # 等到协议包已全部解码并且前端有机会确认该批事件。
                            page.wait_for_timeout(400)
                            self.assertGreater(remote.sequence,sequence)
                            page.evaluate('()=>FramekeepIO.flush()')
                        panel();mode('fixed')
                        self.assertFalse(page.locator('#mouse-right-decrease-row').is_visible())
                        page.locator('#mouse-wheel-browse').click();page.wait_for_function('()=>document.getElementById("mouse-wheel-browse").getAttribute("aria-checked")==="true"')
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            for width,height in ((1680,1050),(1000,680),(375,720)):
                                page.set_viewport_size({'width':width,'height':height})
                                self.assertFalse(page.evaluate('()=>document.getElementById("mouse-dialog").scrollWidth>document.getElementById("mouse-dialog").clientWidth'))
                        page.set_viewport_size({'width':1680,'height':1050})
                        out=ROOT/'build/mouse-fixed';out.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(out/'fixed-options.png'))
                        page.locator('#mouse-start').click();page.wait_for_function('()=>!document.getElementById("mouse-dialog").open')
                        feed(transport,(1,0),(1,0),(1,0),(2,0));rating(1);page.evaluate('()=>FramekeepIO.flush()')
                        revision=store.session(sid)['revision'];repeated([(1,0),(2,0)]*3);rating(1);self.assertEqual(store.session(sid)['revision'],revision)
                        feed(transport,(4,0),(4,0),(4,0),(8,0));rating(2);page.evaluate('()=>FramekeepIO.flush()')
                        revision=store.session(sid)['revision'];repeated([(4,0),(8,0)]*3);rating(2);self.assertEqual(store.session(sid)['revision'],revision)
                        # 固定方案快速双击同一键清零；取消也入库并保留撤销。
                        page.wait_for_timeout(550);feed(transport,(4,0),(8,0),(4,0),(8,0));rating(0)
                        page.evaluate('()=>FramekeepIO.flush()');self.assertEqual(archive.ratings.get([photos/'0.JPG']),0)
                        page.keyboard.press('Control+z');rating(2)
                        page.keyboard.press('Control+z');rating(1)
                        feed(transport,(4,0),(8,0));rating(2);feed(transport,(1,0),(2,0));rating(1)
                        page.keyboard.press('5');rating(5);page.wait_for_timeout(550);feed(transport,(1,0),(2,0));rating(1)
                        page.keyboard.press('x');feed(transport,(4,0),(8,0));rating(2)
                        # 同一批先滚轮再右键，把两星写到下一张，上一张保持。
                        feed(transport,(0x400,-120),(4,0),(8,0));page.wait_for_function('(id)=>Framekeep.remoteTarget().photoId===id',arg=ids[1]);rating(2)
                        self.assertEqual(page.evaluate('(id)=>Framekeep.persistedState().overrides[id]',ids[0]),2)
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',ids[0]);page.keyboard.press('1');rating(1)
                        delayed=[]
                        def delay(route):
                            response=route.fetch()
                            if response.json().get('events') and not delayed:page.evaluate('(id)=>Framekeep.browsePhoto(id)',ids[2]);delayed.append(True)
                            route.fulfill(response=response)
                        page.route('**/api/mouse/poll',delay);feed(transport,(4,0),(8,0))
                        page.wait_for_function('(id)=>Framekeep.persistedState().overrides[id]===2',arg=ids[0]);page.unroute('**/api/mouse/poll',delay)
                        self.assertEqual(page.evaluate('()=>Framekeep.remoteTarget().photoId'),ids[2]);rating(0);self.assertTrue(delayed)
                        page.evaluate('()=>FramekeepIO.flush()');self.assertEqual(archive.ratings.get([photos/'0.JPG']),2)
                        page.locator('#asset-archive-open').click()
                        card=page.locator('#catalog-grid .catalog-card').filter(has=page.locator('img[alt="0.JPG"]'));card.locator('.photo-rating-badge').wait_for()
                        self.assertEqual(card.locator('.photo-rating-badge').inner_text(),'★★')
                        feed(transport,(1,0),(2,0));page.wait_for_timeout(300);self.assertEqual(archive.ratings.get([photos/'0.JPG']),2)
                        page.locator('#selection-tab').click();panel();mode('incremental');close_panel()
                        feed(transport,(1,0),(2,0));rating(1);feed(transport,(1,0),(2,0));rating(2)
                        panel();mode('fixed');close_panel();feed(transport,(1,0),(2,0));rating(1)
                        page.wait_for_timeout(550);feed(transport,(1,0),(2,0),(1,0),(2,0));rating(0)
                        page.wait_for_timeout(550);feed(transport,(1,0),(2,0));rating(1)
                        page.evaluate('()=>FramekeepIO.flush()');panel();page.locator('#mouse-stop').click()
                        page.wait_for_function('()=>document.getElementById("mouse-stop").disabled&&!document.getElementById("mouse-start").disabled')
                        self.assertEqual(transport.filters[-1],0)
                        page.reload(wait_until='networkidle');panel();page.wait_for_function('()=>document.getElementById("mouse-rating-mode").value==="fixed"')
                        self.assertFalse(remote.state()['active']);self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:
                remote.close();archive.close();http.shutdown();http.server_close()
                for item in reversed(patches):item.stop()


if __name__=='__main__':unittest.main()
