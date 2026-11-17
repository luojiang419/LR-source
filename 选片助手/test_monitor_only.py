"""现场监控独立运行；联机拍摄入口和外部EOS控制已移除。"""
from contextlib import ExitStack
from pathlib import Path
import hashlib
import json
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
from folder_watch import FolderWatch
import test_folder_watch as fixtures


class MonitorOnlyTests(unittest.TestCase):
    setUp=fixtures.FolderWatchTests.setUp
    tearDown=fixtures.FolderWatchTests.tearDown
    image=fixtures.FolderWatchTests.image

    def test_legacy_canon_directory_restores_as_manual_monitor(self):
        photo=self.image('EOS_0001.JPG');before=hashlib.sha256(photo.read_bytes()).hexdigest()
        preferences=self.store.data/'canon-link/preferences.json';preferences.parent.mkdir()
        preferences.write_text('{"enabled":true,"destination":"leave-unchanged"}',encoding='utf8')
        original=preferences.read_bytes()
        self.watch.start(str(self.photos),source='canon');self.watch.tick()
        row=self.store.records(self.sid)[0]
        self.store.save(self.sid,0,{'overrides':{row['id']:4}})
        self.watch.close();self.watch=FolderWatch(self.store,clock=lambda:self.clock[0],autostart=False)
        state=self.watch.state()
        self.assertTrue(state['enabled']);self.assertEqual(state['source'],'manual')
        self.assertEqual(state['session_id'],self.sid);self.watch.tick()
        self.assertEqual(self.store.manifest(False,self.sid)['captures'][0]['rating'],4)
        self.assertEqual(hashlib.sha256(photo.read_bytes()).hexdigest(),before)
        self.assertEqual(preferences.read_bytes(),original)

    def test_workbench_exposes_only_monitor_and_receives_external_photos(self):
        from playwright.sync_api import sync_playwright
        import server
        self.watch.stop();self.store.activate(self.sid)
        with ExitStack() as stack:
            for key,value in dict(STORE=self.store,DATA=self.store.data,WATCH=self.watch,CANON=None,PREVIEWS=None,
                                  MOUSE=None,MODELS=None,SKU=None,ARCHIVE=None,ANALYSIS=None,RAWLINK=None,
                                  FILESEARCH=None,UPDATES=None,REFERENCES=None).items():
                stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                url=f'http://127.0.0.1:{http.server_port}'
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':960});errors=[];canon_requests=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.on('request',lambda req:canon_requests.append(req.url) if '/api/canon' in req.url else None)
                        page.goto(url,wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepWatch&&!FramekeepBoot.loading')
                        self.assertEqual(page.locator('#canon-tether-open,#canon-save-dialog,#canon-link-panel').count(),0)
                        self.assertTrue(page.locator('#folder-watch').is_visible())
                        page.locator('#folder-watch').click()
                        self.assertNotIn('EOS',page.locator('#watch-dialog').inner_text())
                        page.locator('#watch-path').fill(str(self.photos));page.locator('#watch-start').click()
                        page.wait_for_function('()=>!document.getElementById("watch-dialog").open')
                        photo=self.image('EOS_0001.JPG');before=hashlib.sha256(photo.read_bytes()).hexdigest()
                        self.watch.tick([photo]);self.clock[0]+=2;self.watch.tick()
                        page.wait_for_function('()=>document.getElementById("filename").textContent==="EOS_0001.JPG"')
                        self.assertEqual(self.watch.state()['source'],'manual')
                        self.assertEqual(hashlib.sha256(photo.read_bytes()).hexdigest(),before)
                        page.wait_for_timeout(1600)
                        self.assertEqual(canon_requests,[])
                        for width in [1440,1024]:
                            page.set_viewport_size({'width':width,'height':960})
                            self.assertTrue(page.locator('#folder-watch').is_visible())
                        out=Path(__file__).resolve().parents[1]/'build/monitor-only';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'verified.png'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
                # 旧界面/旧桌面回调不能重新开启联机服务或修改保存位置。
                generation=self.watch.state()['generation']
                requests=[urllib.request.Request(url+'/api/canon')]
                for path in ['/api/canon','/api/canon/destination']:
                    requests.append(urllib.request.Request(url+path,b'{"enabled":true,"folder":"ignored","paths":[]}',{'Content-Type':'application/json'}))
                for request in requests:
                    with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
                    self.assertEqual(error.exception.code,410)
                self.assertEqual(self.watch.state()['generation'],generation)
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
