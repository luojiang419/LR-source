"""首屏不等待预览补建；异步加载期间动画可见，真实数据与交互随后恢复。"""
from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store
from preview_cache import cache_for,PreviewWarmer
from startup_tasks import StartupTasks
import media_engine
import server

ROOT=Path(__file__).resolve().parents[1]


class StartupTasksTests(unittest.TestCase):
    def test_foreground_release_fallback_and_early_close(self):
        ran=threading.Event();tasks=StartupTasks([('work',ran.set)],fallback_seconds=5)
        tasks.start()
        try:
            self.assertFalse(ran.wait(.03));tasks.release();tasks.release();self.assertTrue(ran.wait(1))
        finally:tasks.close()
        ran.clear();tasks=StartupTasks([('work',ran.set)],fallback_seconds=.03);tasks.start()
        try:self.assertTrue(ran.wait(1))
        finally:tasks.close()
        ran.clear();tasks=StartupTasks([('work',ran.set)],fallback_seconds=5);tasks.start();tasks.close()
        self.assertFalse(ran.is_set())


class FastStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        image=self.photos/'one.JPG';Image.new('RGB',(100,80),'#6e8872').save(image)
        self.jpeg=image.read_bytes()
        self.store=Store(self.base/'data',ROOT);self.sid=self.store.ensure_live_session(self.photos)
        self.record=media_engine.capture_record(self.photos,image,None,{'DateTimeOriginal':'2026:10:26 12:00:00'})
        self.store.append_live(self.sid,[self.record]);self.store.save(self.sid,0,{'overrides':{self.record['id']:4}})
        self.store.activate(self.sid)
        self.cache=cache_for(self.store.data/'previews');self.warmer=PreviewWarmer(self.cache,None,autostart=False)
        self.addCleanup(self.warmer.close)
        self.gate=threading.Event();self.gate.set();self.entered=threading.Event()
        self.background=threading.Event();self.tasks=StartupTasks([('background',self.background.set)])
        self.tasks.start();self.addCleanup(self.tasks.close)
        self.patches=ExitStack();self.addCleanup(self.patches.close)
        for name,value in dict(STORE=self.store,DATA=self.store.data,PREVIEWS=self.warmer,BOOT_TASKS=self.tasks,
                               ARCHIVE=None,MODELS=None,CATALOG=None,ANALYSIS=None,SKU=None,WATCH=None,MOUSE=None,
                               CANON=None,RAWLINK=None,FILESEARCH=None,REFERENCES=None,UPDATES=None,COMPONENTS=None,
                               DRIVER=None,STORAGE=None).items():self.patches.enter_context(patch.object(server,name,value))
        original=server.bootstrap_state
        def bootstrap():
            self.entered.set();self.gate.wait(10);return original()
        self.patches.enter_context(patch.object(server,'bootstrap_state',side_effect=bootstrap))
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.http.daemon_threads=True
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close_http)
        self.url=f'http://127.0.0.1:{self.http.server_port}'

    def close_http(self):
        self.gate.set();self.http.shutdown();self.http.server_close();self.thread.join(2)

    def test_bootstrap_returns_while_preview_cache_lock_is_held(self):
        with self.cache.lock:
            started=time.monotonic()
            with urlopen(self.url+'/api/bootstrap',timeout=1) as response:data=json.load(response)
            self.assertLess(time.monotonic()-started,.5)
        self.assertEqual(data['session']['saved']['overrides'][self.record['id']],4)
        self.assertEqual(len(data['photos']),1);self.assertEqual(len(self.warmer.requests),1)
        self.assertFalse(self.background.is_set())
        request=Request(self.url+'/api/boot/complete',data=b'{}',headers={'Content-Type':'application/json','X-Framekeep-Token':data['request_token']})
        with urlopen(request,timeout=1) as response:self.assertTrue(json.load(response)['ok'])
        self.assertTrue(self.background.wait(1))

    def test_preview_request_keeps_latest_version_and_persists_in_background(self):
        self.warmer.request(self.photos,[self.record])
        changed=dict(self.record,fingerprint='new-version')
        self.warmer.request(self.photos,[changed]);self.assertEqual(len(self.warmer.requests),1)
        self.warmer.threads=[];self.warmer.start()
        until=time.monotonic()+2
        while time.monotonic()<until:
            with self.cache.connect() as c:row=c.execute('SELECT record FROM pending').fetchone()
            if row:break
            time.sleep(.01)
        self.assertIsNotNone(row);self.assertEqual(json.loads(row[0])['fingerprint'],'new-version')

    def test_animation_is_visible_before_data_then_real_workbench_is_interactive(self):
        self.gate.clear();out=ROOT/'build/fast-startup-acceptance';out.mkdir(exist_ok=True)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='msedge',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':900},reduced_motion='reduce')
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.route('**/api/preview/**',lambda route:route.fulfill(status=200,content_type='image/jpeg',body=self.jpeg))
                    started=time.monotonic();page.goto(self.url,wait_until='domcontentloaded')
                    self.assertLess(time.monotonic()-started,2)
                    self.assertTrue(self.entered.wait(1));self.assertFalse(self.background.is_set())
                    self.assertTrue(page.locator('#startup-screen').is_visible());self.assertTrue(page.locator('.workbench').evaluate('(node)=>node.inert'))
                    page.wait_for_selector('svg.startup-art')
                    page.screenshot(path=str(out/'startup-desktop.png'))
                    page.set_viewport_size({'width':390,'height':780});page.screenshot(path=str(out/'startup-narrow.png'))
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    page.set_viewport_size({'width':1440,'height':900});page.emulate_media(reduced_motion='no-preference')
                    page.wait_for_timeout(1000);page.screenshot(path=str(out/'startup-animation-focus.png'))
                    page.wait_for_timeout(2800);page.screenshot(path=str(out/'startup-animation-selected.png'))
                    self.gate.set()
                    page.wait_for_function('()=>window.FramekeepBoot?.loading===false',timeout=10000)
                    page.wait_for_selector('#startup-screen',state='detached')
                    self.assertFalse(page.locator('.workbench').evaluate('(node)=>node.inert'))
                    self.assertEqual(page.evaluate('Framekeep.photoItems()[0].rating'),4)
                    self.assertEqual(page.locator('#check-update').count(),1);self.assertEqual(page.locator('#theme-open').count(),1)
                    self.assertTrue(self.background.wait(1));self.assertFalse(errors,errors)
                    page.screenshot(path=str(out/'workbench.png'))
                    (out/'browser-result.json').write_text(json.dumps({'passed':True,'injected_bootstrap_delay_for_visual_qa':True,'errors':errors,'timings':page.evaluate('FramekeepBoot'),'manual_rating':4},ensure_ascii=False,indent=2),encoding='utf8')
                finally:browser.close()
        finally:self.gate.set()


if __name__=='__main__':unittest.main()
