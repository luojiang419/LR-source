"""首显复发：漏通知、临时读失败与过期请求的真实浏览器恢复验收。"""
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from direct_channel import DirectImageChannel
from direct_jpeg import DirectJpegFeed
from test_capture_notification_recovery import Signal,wait_for
from test_jpeg_receiving_preview import application_runtime,camera_jpeg,configure,ROOT


class RecoveryScanTests(unittest.TestCase):
    def test_history_idle_directories_nested_arrival_and_newest_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);child=root/'nested';child.mkdir();old=child/'OLD.JPG';old.write_bytes(b'history')
            # Windows 文件创建时钟可能只有毫秒精度，明确建立监控前的历史图。
            time.sleep(.02);feed=DirectJpegFeed(None,autostart=False);configure(feed,root)
            try:
                recovery=feed.recovery;info=feed.state();recovery.scan(info)
                self.assertIsNone(feed.state()['receiving'],'初次补偿不能逐张跟随历史图')
                with patch('direct_jpeg_recovery.os.scandir',side_effect=AssertionError('空闲目录不得重复枚举历史照片')):
                    recovery.scan(info)
                missed=child/'MISSED.JPG';missed.write_bytes(b'new arrival');recovery.scan(info)
                self.assertEqual(feed.state()['receiving']['name'],missed.name)
                old.write_bytes(b'metadata changed');older=child/'OLDER.JPG';older.write_bytes(b'older')
                time.sleep(.01);latest=child/'LATEST.JPG';latest.write_bytes(b'latest');feed.announce(latest)
                recovery.scan(info,force=True)
                self.assertEqual(feed.state()['receiving']['name'],latest.name)
                self.assertEqual(feed.state()['recovered_notifications'],1)
                new_dir=root/'new';new_dir.mkdir();new_photo=new_dir/'MOVED.JPG';new_photo.write_bytes(b'moved');recovery.scan(info)
                self.assertEqual(feed.state()['receiving']['name'],new_photo.name)
                configure(feed,root,generation='next-generation');recovery.scan(info,force=True)
                self.assertIsNone(feed.state()['receiving'],'旧目录扫描不能越过代次')
            finally:feed.close()

    def test_forced_audit_recovers_coarse_directory_stamp_and_nonrecursive_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);child=root/'nested';child.mkdir();feed=DirectJpegFeed(None,autostart=False)
            configure(feed,root);feed.configure({'recursive':False})
            try:
                recovery=feed.recovery;info=feed.state();recovery.scan(info);stamp=root.stat()
                missed=root/'MISSED.JPG';missed.write_bytes(b'new');os.utime(root,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
                (child/'EXCLUDED.JPG').write_bytes(b'nested');recovery.scan(info)
                self.assertIsNone(feed.state()['receiving']);recovery.scan(info,force=True)
                self.assertEqual(feed.state()['receiving']['name'],missed.name)
                self.assertEqual(feed.state()['recovered_notifications'],1)
            finally:feed.close()

    def test_offline_subtree_preserves_baseline_and_slow_scan_does_not_block_notification(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);child=root/'nested';child.mkdir();feed=DirectJpegFeed(None,autostart=False);configure(feed,root)
            try:
                info=feed.state();recovery=feed.recovery;recovery.scan(info)
                gate=threading.Event();entered=threading.Event();original=os.scandir
                def slow(path):
                    entered.set();gate.wait(2);return original(path)
                with patch('direct_jpeg_recovery.os.scandir',side_effect=slow):
                    worker=threading.Thread(target=recovery.scan,args=(info,True));worker.start()
                    self.assertTrue(entered.wait(1))
                    try:
                        started=time.perf_counter();self.assertTrue(feed.announce(root/'FAST.JPG'))
                        self.assertLess(time.perf_counter()-started,.1)
                    finally:gate.set();worker.join(2)
                with patch('direct_jpeg_recovery.os.scandir',side_effect=OSError('offline')):recovery.scan(info,force=True)
                self.assertIn(str(child).casefold(),recovery.cache)
                self.assertFalse(worker.is_alive())
            finally:feed.close()

    def test_software_data_under_watched_parent_is_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'app-data';data.mkdir()
            feed=DirectJpegFeed(SimpleNamespace(data=data),autostart=False);configure(feed,root)
            try:
                feed.recovery.scan(feed.state())
                (data/'CACHE.JPG').write_bytes(b'not a shutter')
                feed.recovery.scan(feed.state(),force=True)
                self.assertIsNone(feed.state()['receiving'])
            finally:feed.close()


def boot(page,url):
    page.goto(url,wait_until='domcontentloaded')
    page.wait_for_function('()=>!FramekeepBoot.loading&&document.querySelector("#photos [data-loupe] img")?.naturalWidth>0')


def frame(page,name):
    page.wait_for_function('name=>{const i=document.querySelector(".receiving-photo-preview img");return i?.alt===name&&i.complete&&i.naturalWidth===3600}',arg=name,timeout=1800)
    page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')


class RecoveryBrowserTests(unittest.TestCase):
    def test_lost_reconnect_and_overflow_notifications_bypass_blocked_admission(self):
        from playwright.sync_api import sync_playwright
        opened=[]
        def create(*args):
            signal=Signal();opened.append(signal);return signal
        metrics={};payload=camera_jpeg()
        with patch('direct_jpeg.DirectoryEvents',side_effect=create),patch('directory_events.DirectoryEvents',side_effect=lambda *args:Signal()),application_runtime() as (server,base,photos,store,watch,sid,url),closing(DirectImageChannel(watch,url)) as channel,patch.object(server,'DIRECT',channel),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':960});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)));boot(page,url)
                self.assertTrue(wait_for(lambda:opened and watch.direct.recovery.cache))
                for name,kind in (('SILENT.JPG','lost'),('RECONNECT.JPG','reconnect'),('OVERFLOW.JPG','overflow')):
                    with watch.lock:
                        if kind=='reconnect':opened[-1].handle=None
                        (photos/Path(name).with_suffix('.CR3')).write_bytes(b'raw still transferring')
                        started=time.perf_counter();stamp=photos.stat();(photos/name).write_bytes(payload)
                        if kind=='overflow':
                            os.utime(photos,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
                            opened[-1].changes=None;opened[-1].pending.set()
                        frame(page,name);elapsed=(time.perf_counter()-started)*1000
                        self.assertLess(elapsed,1500);self.assertEqual(len(store.records(sid)),1)
                        metrics[kind]={'jpeg_available_to_painted_ms':elapsed,'formal_monitor_locked':True}
                self.assertTrue(wait_for(lambda:len(opened)>1),'不能靠补发通知显示')
                self.assertFalse(errors,errors)
                page.screenshot(path=str(ROOT/'build/capture-latency-247/recovery.png'))
                self.save('notification_recovery',{'cases':metrics,'page_errors':errors})
            finally:browser.close()

    def test_transient_read_retries_past_three_failures_and_cancels_stale_photo(self):
        from playwright.sync_api import sync_playwright
        with application_runtime() as (server,base,photos,store,watch,sid,url),closing(DirectImageChannel(watch,url)) as channel,patch.object(server,'DIRECT',channel),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':960});errors=[];attempts=[];started=[0];mode=['transient']
                page.on('pageerror',lambda error:errors.append(str(error)));boot(page,url)
                def read(route):
                    elapsed=(time.perf_counter()-started[0])*1000;status=423 if elapsed<800 or mode[0]!='transient' else 200
                    attempts.append({'ms':elapsed,'status':status,'url':route.request.url})
                    if status==423:route.fulfill(status=423,body='injected sharing violation')
                    else:route.continue_()
                page.route(channel.origin+'/api/watch/preview/**',read)
                with watch.lock:
                    started[0]=time.perf_counter();(photos/'TRANSIENT.JPG').write_bytes(camera_jpeg());frame(page,'TRANSIENT.JPG')
                    elapsed=(time.perf_counter()-started[0])*1000
                    self.assertLess(elapsed,1700);self.assertGreaterEqual(sum(a['status']==423 for a in attempts),3)
                    self.assertEqual(len(store.records(sid)),1)
                    metrics={'failure_window_ms':800,'jpeg_available_to_painted_ms':elapsed,'formal_monitor_locked':True,'attempts':list(attempts)}
                    mode[0]='blocked';attempts.clear();started[0]=time.perf_counter()
                    (photos/'CANCEL.JPG').write_bytes(camera_jpeg());page.wait_for_timeout(1600)
                    self.assertGreater(len(attempts),3);self.assertLess(len(attempts),12)
                    page.evaluate('FramekeepWatch.setFollow(false)');page.wait_for_timeout(100);count=len(attempts)
                    page.wait_for_timeout(700);self.assertEqual(len(attempts),count)
                    self.assertFalse(page.evaluate('FramekeepReceiving.active'))
                    page.evaluate('FramekeepWatch.setFollow(true)');page.wait_for_timeout(100)
                    old_url=attempts[-1]['url'].split('?')[0];mode[0]='transient';started[0]=time.perf_counter()-1
                    (photos/'NEWEST.JPG').write_bytes(camera_jpeg());frame(page,'NEWEST.JPG')
                    count=sum(a['url'].split('?')[0]==old_url for a in attempts);page.wait_for_timeout(700)
                    self.assertEqual(sum(a['url'].split('?')[0]==old_url for a in attempts),count)
                    self.assertEqual(page.locator('.receiving-photo-preview img').get_attribute('alt'),'NEWEST.JPG')
                    self.assertFalse(errors,errors)
                    page.screenshot(path=str(ROOT/'build/capture-latency-247/retry.png'))
                    metrics.update(cancel_follow_stops_retries=True,new_photo_cancels_old=True,page_errors=errors)
                    self.save('transient_read_recovery',metrics)
            finally:browser.close()

    @staticmethod
    def save(key,result):
        output=ROOT/'build/capture-latency-247/after-fix.json';output.parent.mkdir(parents=True,exist_ok=True)
        data=json.loads(output.read_text(encoding='utf8')) if output.exists() else {}
        data[key]=result;output.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':unittest.main()
