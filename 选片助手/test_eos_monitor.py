"""EOS只负责启动/照片路径通知，随后统一使用现场监控。"""
from contextlib import ExitStack
from pathlib import Path
import os
import threading
import unittest
from unittest.mock import patch
import urllib.request
import json
from PIL import Image
from eos_monitor import EosMonitor
import test_folder_watch as fixtures


class EosMonitorTests(unittest.TestCase):
    setUp=fixtures.FolderWatchTests.setUp
    tearDown=fixtures.FolderWatchTests.tearDown
    image=fixtures.FolderWatchTests.image

    def test_notification_activates_monitor_reuses_generation_and_switches_directory(self):
        self.watch.stop();service=EosMonitor(self.store,self.watch)
        one=self.image('EOS_0001.JPG');first=service.receive([str(one)])
        self.assertTrue(first['accepted']);self.assertEqual(self.watch.state()['source'],'eos')
        generation=self.watch.state()['generation'];self.watch.tick([one])
        record=self.store.records(first['session_id'])[0]
        self.store.save(first['session_id'],0,{'overrides':{record['id']:4}})
        two=self.image('EOS_0002.JPG');service.receive([str(two)])
        self.assertEqual(self.watch.state()['generation'],generation)
        other=self.base/'新拍摄日期';other.mkdir();third=other/'EOS_0003.JPG';Image.new('RGB',(80,60),'teal').save(third)
        result=service.receive([str(third)])
        self.assertNotEqual(result['session_id'],first['session_id'])
        self.assertEqual(self.store.active_id(),result['session_id'])
        self.assertEqual(self.store.manifest(False,first['session_id'])['captures'][0]['rating'],4)
        service.pause();self.watch.stop()
        self.assertFalse(service.receive([str(one)])['accepted']);self.assertFalse(self.watch.state()['enabled'])
        self.assertFalse((self.store.data/'canon-link/preferences.json').exists())

    def test_invalid_paths_cannot_change_monitor(self):
        service=EosMonitor(self.store,self.watch);generation=self.watch.state()['generation']
        for paths in [[],['relative.JPG'],[str(self.base/'missing.JPG')]]:
            with self.assertRaises((ValueError,OSError)):service.receive(paths)
            self.assertEqual(self.watch.state()['generation'],generation)

    def test_authenticated_eos_callback_enters_monitor_without_tether_ui(self):
        from playwright.sync_api import sync_playwright
        import server
        self.watch.stop();self.store.activate(self.sid);service=EosMonitor(self.store,self.watch)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,FRAMEKEEP_DESKTOP_TOKEN='eos-monitor-test'))
            for key,value in dict(STORE=self.store,DATA=self.store.data,WATCH=self.watch,CANON=service,PREVIEWS=None,
                                  MOUSE=None,MODELS=None,SKU=None,ARCHIVE=None,ANALYSIS=None,RAWLINK=None,
                                  FILESEARCH=None,UPDATES=None,REFERENCES=None).items():stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            url=f'http://127.0.0.1:{http.server_port}'
            def receive(path,secret='eos-monitor-test'):
                req=urllib.request.Request(url+'/api/desktop/canon',json.dumps({'paths':[str(path)]}).encode(),{'Content-Type':'application/json','X-Framekeep-Desktop':secret})
                with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':960});errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.route('**/api/archive/streams',lambda route:route.fulfill(json={'streams':[],'segments':[],'late':[]}))
                        page.route('**/api/metadata/operations',lambda route:route.fulfill(json={'operations':[]}))
                        page.goto(url,wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepWatch&&!FramekeepBoot.loading')
                        page.locator('#app-settings-open').click()
                        one=self.image('EOS_0001.JPG');result=receive(one)
                        page.wait_for_function('(sid)=>FRAMEKEEP_SESSION.id===sid&&FramekeepWorkspace.current()==="selection"',arg=result['session_id'])
                        self.watch.tick([one]);page.wait_for_function('()=>document.getElementById("filename").textContent==="EOS_0001.JPG"')
                        generation=self.watch.state()['generation']
                        two=self.image('EOS_0002.JPG');receive(two);self.watch.tick([two])
                        page.wait_for_function('()=>document.getElementById("filename").textContent==="EOS_0002.JPG"')
                        self.assertEqual(self.watch.state()['generation'],generation)
                        other=self.base/'新拍摄日期';other.mkdir();third=other/'EOS_0003.JPG';Image.new('RGB',(80,60),'teal').save(third)
                        result=receive(third);self.watch.tick([third])
                        page.wait_for_function('(sid)=>FRAMEKEEP_SESSION.id===sid&&document.getElementById("filename").textContent==="EOS_0003.JPG"',arg=result['session_id'])
                        self.assertEqual(page.locator('#canon-tether-open,#canon-save-dialog').count(),0)
                        self.assertTrue(page.locator('#folder-watch').is_visible())
                        out=Path(__file__).resolve().parents[1]/'build/monitor-only';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'eos-callback.png'))
                        page.evaluate("()=>FramekeepIO.api('/api/watch/stop',{})")
                        self.assertFalse(receive(third)['accepted']);self.assertFalse(self.watch.state()['enabled'])
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
