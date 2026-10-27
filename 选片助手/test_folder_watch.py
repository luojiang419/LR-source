import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

from PIL import Image
from catalog_store import Store
from folder_watch import FolderWatch

ROOT=Path(__file__).resolve().parents[1]


class FolderWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'传入';self.photos.mkdir()
        self.store=Store(self.base/'data',ROOT);self.clock=[0.]
        self.watch=FolderWatch(self.store,clock=lambda:self.clock[0],autostart=False)
        self.meta=patch('media_engine.read_metadata',side_effect=lambda paths,*a,**kw:[{} for _ in paths]);self.read=self.meta.start()
        self.exif=patch('media_engine.find_exiftool',return_value=Path('test'));self.exif.start()
        self.sid=self.watch.start(str(self.photos))['session_id']

    def tearDown(self):
        self.watch.close();self.meta.stop();self.exif.stop();self.temp.cleanup()

    def image(self,name='a.JPG'):
        path=self.photos/name;Image.new('RGB',(160,100),(70,90,100)).save(path);return path

    def settle(self):
        self.watch.tick();self.clock[0]+=2;self.watch.tick()

    def test_empty_then_incremental_save_revision_and_no_duplicate(self):
        self.watch.tick();self.assertEqual(self.store.records(self.sid),[])
        self.image();self.watch.tick();self.assertEqual(self.store.records(self.sid),[])
        self.clock[0]+=2;self.watch.tick();rows=self.store.records(self.sid);self.assertEqual(len(rows),1)
        self.assertEqual(self.store.manifest(False,self.sid)['captures'][0]['rating'],0)
        self.store.save(self.sid,0,{'overrides':{rows[0]['id']:4}})
        self.image('b.JPG');self.settle()
        self.assertEqual(self.store.session(self.sid)['revision'],1)
        self.store.save(self.sid,1,{'overrides':{rows[0]['id']:5}})
        self.watch.tick();self.assertEqual(len(self.store.records(self.sid)),2)
        self.assertEqual(self.watch.state()['arrivals'],2)
        self.assertEqual(self.store.manifest(False,self.sid)['captures'][0]['rating'],5)
        # 源文件被移走不删除现场记录和评级。
        (self.photos/'a.JPG').unlink();self.watch.tick();self.assertEqual(len(self.store.records(self.sid)),2)

    def test_internal_cache_cannot_be_selected_or_restored_as_photo_folder(self):
        cache=self.store.data/'previews';cache.mkdir();path=cache/'customer.JPG'
        Image.new('RGB',(80,60)).save(path);before=path.read_bytes()
        for folder in (self.store.data,cache):
            with self.assertRaisesRegex(ValueError,'缓存目录'):self.watch.start(str(folder))
        self.assertEqual(self.watch.state()['folder'],str(self.photos));self.assertEqual(path.read_bytes(),before)
        saved={'enabled':True,'source':'manual','folder':str(cache),'session_id':self.sid}
        self.watch.config.write_text(json.dumps(saved),encoding='utf8')
        restarted=FolderWatch(self.store,autostart=False)
        try:self.assertFalse(restarted.state()['enabled']);self.assertTrue(restarted.state()['error'])
        finally:restarted.close()

    def test_recursive_parent_excludes_software_data_and_preserves_fixed_temp(self):
        cache=self.store.data/'previews';cache.mkdir();Image.new('RGB',(80,60)).save(cache/'cache.JPG')
        self.image();temporary=self.watch.config.with_suffix('.tmp');temporary.write_bytes(b'user data')
        sid=self.watch.start(str(self.base))['session_id'];self.settle()
        self.assertEqual(len(self.store.records(sid)),1)
        self.assertEqual(temporary.read_bytes(),b'user data')

    def test_partial_and_open_writer_wait_then_import(self):
        output=io.BytesIO();Image.new('RGB',(160,100)).save(output,format='JPEG');payload=output.getvalue();path=self.photos/'copy.JPG'
        with path.open('wb') as writer:
            writer.write(payload[:100]);writer.flush();self.settle()
            self.assertEqual(self.store.records(self.sid),[])
            writer.write(payload[100:]);writer.flush();self.settle()
            if __import__('os').name=='nt':self.assertEqual(self.store.records(self.sid),[])
        self.clock[0]+=2;self.watch.tick();self.assertEqual(len(self.store.records(self.sid)),1)

    def test_late_raw_and_companion_preserve_id_and_rating(self):
        self.image('pair.JPG');self.settle();first=self.store.records(self.sid)[0]
        self.store.save(self.sid,0,{'overrides':{first['id']:5}})
        (self.photos/'pair.CR3').write_bytes(b'fake RAW for metadata-mocked pairing test')
        self.settle();rows=self.store.records(self.sid)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['id'],first['id']);self.assertEqual(rows[0]['raw'],'pair.CR3');self.assertEqual(rows[0]['fileCount'],2)
        self.assertEqual(self.store.manifest(False,self.sid)['captures'][0]['rating'],5)
        self.assertEqual(self.watch.state()['arrivals'],1)
        self.watch.stop()
        with patch('catalog_store.threading.Thread'):
            job=self.store.scan(self.photos);self.store._scan(job,self.photos,True)
        self.assertEqual(self.store.jobs[job]['status'],'completed')
        self.assertEqual(self.store.records(self.sid)[0]['id'],first['id'])
        self.assertEqual(self.store.manifest(False,self.sid)['captures'][0]['rating'],5)

    def test_existing_rating_frozen_and_bad_file_does_not_block(self):
        self.image();self.settle();first=self.store.records(self.sid)[0]
        self.store.save(self.sid,0,{'overrides':{first['id']:3}})
        (self.photos/'bad.CR3').write_bytes(b'incomplete RAW')
        self.image('good.JPG')
        self.read.side_effect=lambda paths,*a,**kw:[{'Error':'not ready'} if p.name=='bad.CR3' else {} for p in paths]
        self.settle();self.assertEqual(len(self.store.records(self.sid)),2)
        self.assertEqual(self.store.manifest(False,self.sid)['captures'][0]['rating'],3)
        self.assertIn('重试',self.watch.state()['error'])

    def test_stop_restore_and_directory_recovery(self):
        self.image();self.settle();self.watch.close()
        resumed=FolderWatch(self.store,clock=lambda:self.clock[0],autostart=False)
        try:
            self.assertTrue(resumed.state()['enabled']);resumed.tick();self.assertEqual(len(self.store.records(self.sid)),1)
            moved=self.base/'offline';self.photos.rename(moved);resumed.tick();self.assertIn('重试',resumed.state()['message'])
            moved.rename(self.photos);resumed.tick();self.assertFalse(resumed.state()['error'])
            resumed.stop();self.image('not-imported.JPG');self.clock[0]+=2;resumed.tick()
            self.assertEqual(len(self.store.records(self.sid)),1)
        finally:resumed.close()

    def test_switch_or_stop_during_metadata_does_not_publish_old_batch(self):
        self.image();self.watch.tick();self.clock[0]+=2
        other=self.base/'新目录';other.mkdir()
        def switch(paths,*args,**kwargs):
            self.watch.start(str(other));return [{} for _ in paths]
        self.read.side_effect=switch;self.watch.tick()
        self.assertEqual(self.store.records(self.sid),[])
        self.assertEqual(self.store.records(self.watch.state()['session_id']),[])
        self.watch.start(str(self.photos));self.watch.tick();self.clock[0]+=2
        def stop(paths,*args,**kwargs):
            self.watch.stop();return [{} for _ in paths]
        self.read.side_effect=stop;self.watch.tick()
        self.assertEqual(self.store.records(self.sid),[])

    def test_start_on_existing_session_keeps_effective_stars(self):
        other=self.base/'已有';other.mkdir();Image.new('RGB',(100,100)).save(other/'old.JPG')
        with patch('catalog_store.threading.Thread'):
            job=self.store.scan(other);self.store._scan(job,other,True)
        sid=self.store.session()['id'];self.assertEqual(self.store.manifest(False,sid)['captures'][0]['rating'],1)
        self.watch.start(str(other));Image.new('RGB',(100,100)).save(other/'new.JPG');self.settle()
        ratings={r['relativePaths'][0]:r['rating'] for r in self.store.manifest(False,sid)['captures']}
        self.assertEqual(ratings,{'old.JPG':1,'new.JPG':0})

    def test_raw_first_then_jpg_and_overwrite_keep_rating(self):
        (self.photos/'pair.CR3').write_bytes(b'fake RAW; metadata is mocked')
        self.settle();first=self.store.records(self.sid)[0]
        self.store.save(self.sid,0,{'overrides':{first['id']:4}})
        self.image('pair.JPG');self.settle()
        self.assertEqual(len(self.store.records(self.sid)),1)
        self.assertEqual(self.store.records(self.sid)[0]['fileCount'],2)
        self.assertEqual(self.store.manifest(False,self.sid)['captures'][0]['rating'],4)

    def test_recursive_option_and_bad_batch_does_not_starve_good_file(self):
        child=self.photos/'nested';child.mkdir();Image.new('RGB',(100,100)).save(child/'child.JPG')
        self.watch.start(str(self.photos),False);self.settle();self.assertEqual(self.store.records(self.sid),[])
        self.watch.start(str(self.photos),True);self.settle();self.assertEqual(len(self.store.records(self.sid)),1)
        for i in range(24):(self.photos/f'bad{i:02d}.CR3').write_bytes(b'broken')
        self.image('later.JPG')
        self.read.side_effect=lambda paths,*a,**kw:[{'Error':'bad'} if p.suffix=='.CR3' else {} for p in paths]
        self.settle();self.clock[0]+=.5;self.watch.tick()
        self.assertEqual(len(self.store.records(self.sid)),2)


class LiveBrowserTests(unittest.TestCase):
    def test_real_folder_arrival_and_remote_rating_without_reload(self):
        from playwright.sync_api import sync_playwright
        from mouse_remote import MouseRemote
        from mouse_native.interception import Interception
        from test_mouse_remote import FakeTransport,FakeHotkey,FakeLease
        import server
        chrome=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
        if not chrome.is_file():self.skipTest('测试用 Chrome 未安装')
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'现场传图';photos.mkdir();store=Store(base/'data',ROOT)
            watch=FolderWatch(store,autostart=False);watch.STABLE_SECONDS=.25;watch.INTERVAL=.1
            transport=FakeTransport();remote=MouseRemote(lambda _,click,error:Interception(None,click,error,transport=transport),FakeHotkey,lease_factory=FakeLease,legacy_guard=lambda:None)
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            with patch.object(server,'STORE',store),patch.object(server,'DATA',base/'data'),patch.object(server,'MOUSE',remote),patch.object(server,'WATCH',watch):
                thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start();watch.thread.start()
                url=f'http://127.0.0.1:{http.server_port}'
                try:
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        urllib.request.urlopen(urllib.request.Request(url+'/api/watch/start',b'{}',{'Content-Type':'application/json'}))
                    self.assertEqual(denied.exception.code,403)
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch(executable_path=str(chrome),headless=True)
                        try:
                            page=browser.new_page(viewport={'width':1600,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                            page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading');page.evaluate('() => window.__samePage=true')
                            page.locator('#folder-watch').click();page.locator('#watch-path').fill(str(photos));page.locator('#watch-start').click()
                            page.wait_for_function('() => !document.getElementById("watch-dialog").open && FRAMEKEEP_SESSION.id!==null')
                            page.locator('#app-settings-open').click();page.locator('[data-settings-section=remote]').click();page.locator('#mouse-remote').click();page.locator('#mouse-start').click()
                            page.wait_for_function('() => !document.getElementById("mouse-dialog").open')
                            def wait_photo(name):
                                page.wait_for_function('(name) => document.getElementById("filename").textContent===name',arg=name)
                            def stars(n):page.wait_for_function('(n) => document.querySelectorAll("#stars .on").length===n',arg=n)
                            def image(name,color):Image.new('RGB',(600,400),color).save(photos/name)
                            image('001.JPG',(80,100,120));wait_photo('001.JPG');stars(0)
                            transport.feed(1,2);stars(1)
                            # 评级尚在 450ms 保存窗口内就传入下一张。
                            image('002.JPG',(120,80,100));wait_photo('002.JPG');stars(0)
                            for _ in range(7):transport.feed(1,2)
                            stars(5);page.evaluate('() => FramekeepIO.flush()')
                            self.assertTrue(page.evaluate('() => window.__samePage'))
                            self.assertTrue(remote.state()['active'])
                            sid=watch.state()['session_id'];saved=store.manifest(False,sid)
                            self.assertEqual([r['rating'] for r in saved['captures']],[1,5])
                            # 关闭自动跟随，新照片仍入库，但当前照片不改变。
                            page.locator('#folder-watch').click();page.locator('#watch-follow').uncheck();page.locator('#watch-close').click()
                            image('003.JPG',(100,120,80))
                            page.wait_for_function('() => DEMO_PHOTOS.length===3');wait_photo('002.JPG');stars(5)
                            # 恢复跟随并分段写文件，不能出现半张图。
                            page.locator('#folder-watch').click();page.locator('#watch-follow').check();page.locator('#watch-close').click();wait_photo('003.JPG')
                            output=io.BytesIO();Image.new('RGB',(600,400),(70,80,90)).save(output,format='JPEG');payload=output.getvalue()
                            with (photos/'004.JPG').open('wb') as writer:
                                writer.write(payload[:100]);writer.flush();page.wait_for_timeout(850)
                                self.assertEqual(page.locator('#filename').inner_text(),'003.JPG')
                                writer.write(payload[100:]);writer.flush()
                            wait_photo('004.JPG');stars(0);transport.feed(1,2);stars(1)
                            page.evaluate('() => FramekeepIO.flush()')
                            page.locator('#folder-watch').click();page.locator('#watch-stop').click()
                            page.wait_for_function('() => document.getElementById("watch-stop").disabled')
                            page.locator('#watch-close').click();image('005.JPG',(130,140,150));page.wait_for_timeout(850)
                            self.assertEqual(page.evaluate('() => DEMO_PHOTOS.length'),4)
                            page.reload(wait_until='domcontentloaded');wait_photo('004.JPG');stars(1)
                            self.assertFalse(errors,errors)
                            out=ROOT/'build/watch-acceptance';out.mkdir(parents=True,exist_ok=True)
                            page.screenshot(path=str(out/'live-browser.png'))
                            (out/'browser-result.json').write_text(json.dumps({'passed':True,'page_errors':errors,'checks':['empty folder','actual JPG arrivals','automatic single photo','remote packet rating','5 star cap','save race','no reload','remote remains active','pause/resume follow','partial file','stop','reload persistence']},indent=2),encoding='utf8')
                        finally:browser.close()
                finally:
                    watch.close();remote.close();http.shutdown();http.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
