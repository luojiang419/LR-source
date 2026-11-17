"""监控接入历史目录时直接显示末张，不回放每个历史入库批次。"""
from pathlib import Path
import unittest
from unittest.mock import patch
import test_folder_watch as fixtures


class WatchHistoryTests(unittest.TestCase):
    setUp=fixtures.FolderWatchTests.setUp
    tearDown=fixtures.FolderWatchTests.tearDown
    image=fixtures.FolderWatchTests.image

    def history(self,count=73):
        return [self.image(f'IMG_{i:04d}.JPG') for i in range(count)]

    def test_history_first_batch_contains_tail_and_never_replays_other_batches(self):
        photos=self.history();tail=photos[-1]
        with patch.object(self.watch.direct,'announce',wraps=self.watch.direct.announce) as announce:
            self.watch.tick()
            records=self.store.records(self.sid)
            self.assertEqual(len(records),24)
            self.assertIn(tail.name,[r['jpg'] for r in records])
            target=next(r['id'] for r in records if r['jpg']==tail.name)
            self.assertEqual(self.watch.state()['last_photo_id'],target)
            for _ in range(4):
                self.clock[0]+=2;self.watch.tick()
                self.assertEqual(self.watch.state()['last_photo_id'],target)
            self.assertEqual({Path(call.args[0]).name for call in announce.call_args_list},{tail.name})
        self.assertEqual([r['jpg'] for r in self.store.records(self.sid)],[p.name for p in photos])
        self.assertFalse(self.watch.state()['initializing'])

    def test_live_arrival_during_history_backfill_remains_current(self):
        self.history();self.watch.tick()
        newest=self.image('000_NEW.JPG')
        self.watch.tick([newest])
        target=next(r['id'] for r in self.store.records(self.sid) if r['jpg']==newest.name)
        self.assertEqual(self.watch.state()['last_photo_id'],target)
        for _ in range(4):
            self.clock[0]+=2;self.watch.tick()
            self.assertEqual(self.watch.state()['last_photo_id'],target)
        self.assertEqual([r['jpg'] for r in self.store.records(self.sid)],[f'IMG_{n:04d}.JPG' for n in range(73)]+[newest.name])

    def test_trusted_new_photo_before_first_snapshot_stays_ahead_of_history(self):
        self.history()
        newest=self.image('000_NOTIFIED.JPG');self.watch.set_source('canon')
        self.watch.notify_canon([newest]);self.watch.tick([newest])
        target=self.watch.state()['last_photo_id']
        self.assertEqual(self.store.records(self.sid)[0]['jpg'],newest.name)
        for _ in range(4):
            self.clock[0]+=2;self.watch.tick()
            self.assertEqual(self.watch.state()['last_photo_id'],target)
        self.assertEqual(len(self.store.records(self.sid)),74)

    def test_restart_reuses_known_tail_without_reannouncing_history(self):
        self.history()
        for _ in range(4):
            self.clock[0]+=2;self.watch.tick()
        target=self.watch.state()['last_photo_id']
        self.watch.stop();self.watch.start(str(self.photos))
        with patch.object(self.watch.direct,'announce',wraps=self.watch.direct.announce) as announce:
            self.watch.tick()
            self.assertEqual(self.watch.state()['last_photo_id'],target)
            self.assertEqual(self.watch.state()['arrivals'],0)
            self.assertEqual(announce.call_count,0)


class WatchHistoryBrowserTests(unittest.TestCase):
    setUp=fixtures.FolderWatchTests.setUp
    tearDown=fixtures.FolderWatchTests.tearDown
    def test_manual_folder_and_canon_restart_do_not_play_history(self):
        from contextlib import ExitStack
        import threading
        from playwright.sync_api import sync_playwright
        import server
        other=self.base/'另一个监控目录';other.mkdir()
        from PIL import Image
        for n in range(73):Image.new('RGB',(160,100),(n*3,90,100)).save(other/f'IMG_{n:04d}.JPG')
        self.store.activate(self.sid)
        with ExitStack() as stack:
            for key,value in dict(STORE=self.store,DATA=self.store.data,WATCH=self.watch,CANON=None,PREVIEWS=None,
                                  MOUSE=None,MODELS=None,SKU=None,ARCHIVE=None,ANALYSIS=None,RAWLINK=None,
                                  FILESEARCH=None,UPDATES=None,REFERENCES=None).items():
                stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':960},reduced_motion='reduce');errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepWatch&&!FramekeepBoot.loading')
                        page.locator('#folder-watch').click();page.locator('#watch-path').fill(str(other))
                        page.locator('#watch-start').click()
                        page.wait_for_function('()=>!document.querySelector("#watch-dialog").open')
                        sid=self.watch.state()['session_id']
                        page.wait_for_function('(sid)=>FRAMEKEEP_SESSION.id===sid',arg=sid)
                        page.evaluate('''()=>{window.__history=[];window.__receiving=[];
                            document.addEventListener('framekeep:selection',()=>{const name=document.getElementById('filename').textContent;if(name?.endsWith('.JPG'))__history.push(name)});
                            document.addEventListener('framekeep:receiving-preview',e=>{if(e.detail.active)__receiving.push(e.detail.id)});
                        }''')
                        self.watch.tick()
                        page.wait_for_function('()=>DEMO_PHOTOS.length===24&&document.getElementById("filename").textContent==="IMG_0072.JPG"')
                        out=Path(__file__).resolve().parents[1]/'build/watch-history';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'tail-first-batch.png'))
                        tail=self.watch.state()['last_photo_id']
                        for count in [48,72,73]:
                            self.clock[0]+=2;self.watch.tick()
                            page.wait_for_function('(count)=>DEMO_PHOTOS.length===count',arg=count)
                            self.assertEqual(page.locator('#filename').inner_text(),'IMG_0072.JPG')
                        self.assertEqual(set(page.evaluate('()=>__history')),{'IMG_0072.JPG'})
                        self.assertEqual(page.evaluate('()=>DEMO_PHOTOS.map(p=>p.jpg)'),[f'IMG_{n:04d}.JPG' for n in range(73)])
                        self.assertTrue(all(id==tail for id in page.evaluate('()=>__receiving')))
                        # 相机模式的同目录重启也复用已知末张，不逐张补播。
                        previous=next(r['id'] for r in self.store.records(sid) if r['jpg']=='IMG_0000.JPG')
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',previous)
                        page.evaluate('()=>__history=[]')
                        self.watch.stop();self.watch.start(str(other),source='canon');self.watch.tick()
                        page.wait_for_timeout(1800)
                        self.assertEqual(page.locator('#filename').inner_text(),'IMG_0072.JPG')
                        resumed=page.evaluate('()=>__history')
                        self.assertTrue(all(name in {'IMG_0000.JPG','IMG_0072.JPG'} for name in resumed),resumed)
                        self.assertTrue(all(name=='IMG_0072.JPG' for name in resumed[resumed.index('IMG_0072.JPG'):]),resumed)
                        # 真正新图继续实时跟随；关闭跟随时保持人工选择。
                        incoming=other/'000_NEW.JPG';Image.new('RGB',(160,100),'teal').save(incoming)
                        self.watch.tick([incoming])
                        page.wait_for_function('()=>document.getElementById("filename").textContent==="000_NEW.JPG"')
                        page.evaluate('()=>FramekeepWatch.setFollow(false)')
                        first=next(r['id'] for r in self.store.records(sid) if r['jpg']=='IMG_0000.JPG')
                        page.evaluate('(id)=>Framekeep.browsePhoto(id)',first)
                        name=page.locator('#filename').inner_text()
                        paused=other/'000_PAUSED.JPG';Image.new('RGB',(160,100),'navy').save(paused)
                        self.watch.tick([paused])
                        page.wait_for_function('()=>DEMO_PHOTOS.length===75')
                        self.assertEqual(page.locator('#filename').inner_text(),name)
                        out=Path(__file__).resolve().parents[1]/'build/watch-history';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'verified.png'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
