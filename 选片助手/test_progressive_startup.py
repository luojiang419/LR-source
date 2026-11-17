"""慢库下先显示工作面/EOS照片，恢复不得覆盖用户已打开的会话。"""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
import sys
from unittest.mock import patch,MagicMock
from urllib.request import urlopen,Request

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from startup_gate import StartupGate
import media_engine
import server

ROOT=Path(__file__).resolve().parents[1]


class ProgressiveStartupTests(unittest.TestCase):
    def test_importing_server_does_not_import_archive_models_or_inventory(self):
        result=subprocess.run([sys.executable,'-X','utf8','-c',"import server,sys; names=['asset_catalog','sku_service','local_inference','inventory_service','asset_intelligence']; loaded=[name for name in names if name in sys.modules]; assert not loaded,loaded"],cwd=ROOT/'选片助手',capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        self.image=self.photos/'EOS.JPG';Image.new('RGB',(960,640),'#416c58').save(self.image)
        self.original=self.image.read_bytes()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos)
        record=media_engine.capture_record(self.photos,self.image,None,{'DateTimeOriginal':'2026:11:11 12:00:00'})
        self.store.append_live(self.sid,[record]);self.store.save(self.sid,0,{'overrides':{record['id']:4},'selectedId':record['id']});self.store.activate(self.sid)
        self.record=record;self.gate=StartupGate(self.store.data,ROOT)
        self.patches=ExitStack();self.addCleanup(self.patches.close)
        values=dict(STORE=self.store,DATA=self.store.data,BOOT_GATE=self.gate,BOOT_TASKS=None,PREVIEWS=None,DIRECT=None,
                    ARCHIVE=None,MODELS=None,CATALOG=None,ANALYSIS=None,SKU=None,WATCH=None,MOUSE=None,CANON=None,
                    RAWLINK=None,FILESEARCH=None,REFERENCES=None,UPDATES=None,COMPONENTS=None,DRIVER=None,STORAGE=None,
                    WORKFLOW=None,INVENTORY=None,CAPTURESYNC=None)
        for key,value in values.items():self.patches.enter_context(patch.object(server,key,value))
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.http.daemon_threads=True
        self.worker=threading.Thread(target=self.http.serve_forever,daemon=True);self.worker.start()
        self.addCleanup(self.close);self.url=f'http://127.0.0.1:{self.http.server_port}'

    def close(self):
        self.gate.closed.set();self.gate.ready.set();self.gate.capture_ready.set();self.http.shutdown();self.http.server_close();self.worker.join(2)
        self.assertEqual(self.image.read_bytes(),self.original)

    def browser(self):
        pw=sync_playwright().start();self.addCleanup(pw.stop)
        browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
        self.addCleanup(browser.close)
        return browser.new_page(viewport={'width':1440,'height':900})

    def queue_photo(self):
        inbox=self.store.data/'canon-link/inbox';inbox.mkdir(parents=True)
        path=inbox/'001.json';path.write_text(json.dumps({'paths':[str(self.image)]}),encoding='utf8');return path

    def test_slow_database_does_not_block_shell_or_eos_preview_and_restores_rating(self):
        notification=self.queue_photo();page=self.browser();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        started=time.monotonic();page.goto(self.url,wait_until='domcontentloaded')
        page.wait_for_function('()=>window.FramekeepBoot?.loading===false')
        shell_ms=(time.monotonic()-started)*1000
        page.wait_for_function('()=>document.querySelector("#startup-arrival img")?.naturalWidth>0')
        preview_ms=(time.monotonic()-started)*1000
        self.assertFalse(self.gate.ready.is_set());self.assertTrue(page.evaluate('FramekeepBoot.restoring'))
        self.assertFalse(page.locator('.workbench').evaluate('n=>n.inert'))
        page.screenshot(path=str(ROOT/'build/instant-entry/early-eos-preview.png'))
        self.assertLess(shell_ms,2500);self.assertLess(preview_ms,3000)
        page.evaluate('FramekeepWorkspace.show("settings")');self.assertEqual(page.evaluate('FramekeepWorkspace.current()'),'settings')
        page.evaluate('FramekeepWorkspace.show("selection")')
        # 模拟后台初始化继续阻塞，独立握手和安全检查仍能响应。
        with patch.object(self.store,'records',side_effect=AssertionError('首屏不能读历史照片')):
            with urlopen(self.url+'/api/ready',timeout=1) as response:ready=json.load(response)
            self.assertFalse(ready['services_ready'])
        notification.unlink();self.gate.ready.set();self.gate.capture_ready.set()
        page.wait_for_function('()=>FramekeepBoot.restoring===false&&DEMO_PHOTOS.length===1')
        self.assertEqual(page.evaluate('Framekeep.photoItems()[0].rating'),4)
        self.assertEqual(page.evaluate('Framekeep.persistedState().selectedId'),self.record['id'])
        self.assertEqual(page.evaluate('FRAMEKEEP_SESSION.id'),self.sid);self.assertFalse(errors,errors)
        out=ROOT/'build/instant-entry';out.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(out/'restored.png'))
        (out/'slow-database-result.json').write_text(json.dumps({'shell_ms':shell_ms,'eos_preview_ms':preview_ms,'blocked_database':True,'rating':4,'errors':errors},indent=2),encoding='utf8')

    def test_late_restore_cannot_replace_user_session(self):
        page=self.browser();page.goto(self.url);page.wait_for_function('()=>window.FramekeepBoot?.loading===false')
        page.evaluate('Framekeep.loadSession({id:"user-chosen",name:"用户已选择",root:"X:/chosen",snapshot:"",revision:0,saved:{}},[])')
        self.gate.ready.set();self.gate.capture_ready.set();page.wait_for_function('()=>FramekeepBoot.restoring===false')
        self.assertEqual(page.evaluate('FRAMEKEEP_SESSION.id'),'user-chosen')
        self.assertEqual(page.evaluate('DEMO_PHOTOS.length'),0)

    def test_only_queued_valid_photos_can_be_read_and_notifications_remain_pending(self):
        path=self.queue_photo();photo=self.gate.latest_photo();self.assertEqual(photo['name'],'EOS.JPG')
        with urlopen(self.url+photo['preview'],timeout=2) as response:self.assertEqual(response.headers['Content-Type'],'image/jpeg')
        self.assertEqual(self.gate.latest_photo()['id'],photo['id'])
        with self.assertRaises(ValueError):self.gate.preview('../framekeep.sqlite3')
        with patch.dict('os.environ',{'FRAMEKEEP_DESKTOP_TOKEN':'test'}):
            request=Request(self.url+'/api/desktop/canon',data=b'{}',headers={'X-Framekeep-Desktop':'test'})
            try:urlopen(request,timeout=1)
            except __import__('urllib.error').error.HTTPError as response:self.assertTrue(json.load(response)['pending'])
        self.assertTrue(path.exists())

    def test_early_arrivals_replay_once_and_rejoining_modern_folder_does_not_build_manifest(self):
        self.gate.capture_arrivals(self.sid,[self.record,self.record])
        received=[];self.gate.bind_arrivals(self.store,lambda sid,records:received.extend(r['id'] for r in records))
        self.assertEqual(received,[self.record['id']]);self.assertFalse(self.gate.arrivals)
        with patch.object(self.store,'manifest',side_effect=AssertionError('已迁移现场目录不能重读全量清单')):
            self.assertEqual(self.store.ensure_live_session(self.photos),self.sid)
        self.assertEqual(self.store.session(self.sid)['state']['overrides'][self.record['id']],4)

    def test_staggered_background_stops_without_starting_remaining_services(self):
        from startup_tasks import StartupTasks
        first=threading.Event();second=threading.Event()
        tasks=StartupTasks([('first',first.set),('second',second.set)],stagger_seconds=10)
        tasks.start();tasks.release();self.assertTrue(first.wait(1));tasks.close()
        self.assertFalse(second.is_set());self.assertFalse(tasks.thread.is_alive())

    def test_http_is_available_before_service_construction(self):
        # 使用实际 _run_server 的启动顺序，构造器停住时 HTTP 已可用。
        entered=threading.Event();release=threading.Event();errors=[]
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        def initialize(http,args,progress):
            entered.set();release.wait(5);server.BOOT_GATE.ready.set();http._startup_http_thread.join()
        with patch.object(server,'ThreadingHTTPServer',return_value=http),patch.object(server,'_initialize_server',side_effect=initialize),patch.object(server,'watch_desktop_parent'),patch.object(server.sys,'argv',['server.py','--no-browser']):
            def run():
                try:server._run_server(MagicMock())
                except BaseException as error:errors.append(error)
            worker=threading.Thread(target=run,daemon=True);worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with urlopen(f'http://127.0.0.1:{http.server_port}/api/bootstrap?shell=1',timeout=1) as response:self.assertTrue(json.load(response)['progressive'])
            finally:release.set();http.shutdown();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertFalse(errors,errors)

    def test_eos_enters_real_monitor_and_receives_second_photo_while_catalog_is_blocked(self):
        blocked=threading.Event();release=threading.Event();errors=[];notification=self.queue_photo()
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        def slow_catalog(*args,**kwargs):
            blocked.set();release.wait(20);raise RuntimeError('隔离验收结束')
        url=f'http://127.0.0.1:{http.server_port}'
        with patch.object(server,'ThreadingHTTPServer',return_value=http),patch.object(server,'Store',return_value=self.store),patch.object(server,'AssetCatalog',side_effect=slow_catalog),patch.object(server,'watch_desktop_parent'),patch.dict('os.environ',{'FRAMEKEEP_DESKTOP_TOKEN':'test'}),patch.object(server.sys,'argv',['server.py','--no-browser']):
            def run():
                try:server._run_server(MagicMock())
                except RuntimeError as error:errors.append(str(error))
            worker=threading.Thread(target=run,daemon=True);worker.start()
            try:
                self.assertTrue(blocked.wait(3))
                request=Request(url+'/api/desktop/canon',data=json.dumps({'paths':[str(self.image)]}).encode(),headers={'X-Framekeep-Desktop':'test'})
                started=time.monotonic()
                with urlopen(request,timeout=2) as response:self.assertTrue(json.load(response)['accepted'])
                notification.unlink()
                page=self.browser();js_errors=[];page.on('pageerror',lambda error:js_errors.append(str(error)))
                page.goto(url);page.wait_for_function('()=>FramekeepBoot.loading===false&&FRAMEKEEP_SESSION.id&&document.getElementById("folder-watch").getAttribute("aria-pressed")==="true"')
                monitor_ms=(time.monotonic()-started)*1000
                self.assertFalse(server.BOOT_GATE.ready.is_set())
                second=self.photos/'EOS-连拍-2.JPG';Image.new('RGB',(960,640),'#997d46').save(second)
                second_started=time.monotonic()
                page.wait_for_function('()=>document.getElementById("filename").textContent==="EOS-连拍-2.JPG"&&Array.from(document.querySelectorAll("#photo-stage img")).some(image=>image.alt==="EOS-连拍-2.JPG"&&image.complete&&image.naturalWidth>0&&image.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})&&image.getBoundingClientRect().height>200)',timeout=7000)
                page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                second_ms=(time.monotonic()-second_started)*1000
                self.assertFalse(server.BOOT_GATE.ready.is_set());self.assertTrue(server.WATCH.thread.is_alive())
                self.assertGreaterEqual(len(self.store.records(self.sid)),1);self.assertFalse(js_errors,js_errors)
                out=ROOT/'build/instant-entry';page.screenshot(path=str(out/'early-real-monitor.png'))
                geometry=page.locator('#photo-stage img').evaluate_all('images=>images.map(image=>({alt:image.alt,width:image.getBoundingClientRect().width,height:image.getBoundingClientRect().height,natural:image.naturalWidth,opacity:getComputedStyle(image).opacity}))')
                (out/'early-monitor-result.json').write_text(json.dumps({'monitor_ms':monitor_ms,'second_photo_ms':second_ms,'archive_constructor_still_blocked':True,'images':geometry,'errors':js_errors},indent=2),encoding='utf8')
            finally:
                release.set();worker.join(5);http.shutdown();http.server_close()
        self.assertFalse(worker.is_alive());self.assertEqual(errors,['隔离验收结束'])


if __name__=='__main__':unittest.main()
