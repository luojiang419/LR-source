"""即时预览目录通知异常恢复，不依赖资料入库、图片解码或补偿扫描。"""
from pathlib import Path
from contextlib import closing
import hashlib
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from direct_jpeg import DirectJpegFeed
import test_jpeg_receiving_preview as fixture


class Signal:
    def __init__(self,active=True):
        self.handle=1 if active else None
        self.changes=[];self.pending=threading.Event();self.close_count=0

    def changed(self):
        if not self.pending.is_set():return False
        self.pending.clear();return True

    def emit(self,action,path):
        self.changes=[(action,path)];self.pending.set()

    def close(self):
        self.close_count+=1;self.handle=None


def wait_for(predicate,timeout=1.5):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if predicate():return True
        time.sleep(.01)
    return bool(predicate())


class NotificationRecoveryTests(unittest.TestCase):
    def test_existing_photo_edits_and_stale_generation_do_not_trigger_follow(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);feed=DirectJpegFeed(None,autostart=False);signal=Signal()
            fixture.configure(feed,root)
            path=root/'OLD.JPG';cid=hashlib.sha256(path.name.casefold().encode()).hexdigest()[:24]
            feed.remember_existing([{'id':'raw-id','jpg':path.name}],'direct-generation')
            with patch('direct_jpeg.DirectoryEvents',return_value=signal):
                try:
                    feed.start();path.write_bytes(fixture.camera_jpeg());signal.emit(3,path)
                    time.sleep(.1)
                    self.assertIsNone(feed.state()['receiving'])
                    self.assertIn(cid,feed.admitted)
                    fixture.configure(feed,root,generation='new-generation')
                    feed.remember_existing([{'id':cid,'jpg':path.name}],'direct-generation')
                    self.assertTrue(feed.announce(path),'切目录后旧代次不能把新照片标为历史')
                finally:feed.close()

    def test_modified_only_new_jpeg_is_announced_without_formal_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);feed=DirectJpegFeed(None,autostart=False);signal=Signal()
            fixture.configure(feed,root)
            with patch('direct_jpeg.DirectoryEvents',return_value=signal):
                try:
                    feed.start();photo=root/'NEW.JPG';photo.write_bytes(fixture.camera_jpeg())
                    signal.emit(3,photo)
                    self.assertTrue(wait_for(lambda:feed.state()['receiving'] is not None,.5))
                    self.assertEqual(feed.state()['receiving']['name'],photo.name)
                finally:feed.close()

    def test_invalid_listener_recovers_on_initial_failure_and_later_disconnect(self):
        for failed_at_start in (True,False):
            with self.subTest(failed_at_start=failed_at_start),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);feed=DirectJpegFeed(None,autostart=False)
                first=Signal(not failed_at_start);second=Signal();opened=[]
                def create(*args):
                    signal=first if not opened else second
                    opened.append(signal);return signal
                fixture.configure(feed,root)
                with patch('direct_jpeg.DirectoryEvents',side_effect=create):
                    try:
                        feed.start();self.assertTrue(wait_for(lambda:opened))
                        if not failed_at_start:first.handle=None
                        self.assertTrue(wait_for(lambda:len(opened)>=2))
                        photo=root/'RECOVERED.JPG';photo.write_bytes(fixture.camera_jpeg());second.emit(1,photo)
                        self.assertTrue(wait_for(lambda:feed.state()['receiving'] is not None,.5))
                        self.assertGreaterEqual(first.close_count,1)
                    finally:feed.close()


class NotificationRecoveryBrowserTests(unittest.TestCase):
    def test_modified_only_and_reconnected_listener_bypass_formal_monitor_lock(self):
        from direct_channel import DirectImageChannel
        from playwright.sync_api import sync_playwright
        opened=[];recovery_photo=[]
        def create(*args):
            signal=Signal();opened.append(signal)
            if recovery_photo:signal.emit(3,recovery_photo[0])
            return signal
        metrics={};payload=fixture.camera_jpeg()
        with patch('direct_jpeg.DirectoryEvents',side_effect=create),fixture.application_runtime() as (server,base,photos,store,watch,sid,url),closing(DirectImageChannel(watch,url)) as channel,patch.object(server,'DIRECT',channel),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':960});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url,wait_until='domcontentloaded')
                page.wait_for_function('()=>!FramekeepBoot.loading&&document.querySelector("#photos img")?.naturalWidth>0')
                self.assertTrue(wait_for(lambda:opened))
                for name,reconnect in (('MODIFIED.JPG',False),('RECONNECTED.JPG',True)):
                    with watch.lock:
                        photo=photos/name;started=time.perf_counter();photo.write_bytes(payload)
                        if reconnect:
                            recovery_photo[:]=[photo];opened[-1].handle=None
                        else:opened[-1].emit(3,photo)
                        page.wait_for_function('(name)=>{const i=document.querySelector(".receiving-photo-preview img");return i?.alt===name&&i.naturalWidth===3600}',arg=name,timeout=1500)
                        page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                        metrics[name]=(time.perf_counter()-started)*1000
                        self.assertLess(metrics[name],1500)
                    page.wait_for_function('(name)=>!FramekeepReceiving.active&&document.querySelector("#photos [data-loupe] img")?.alt===name',arg=name)
                self.assertFalse(errors,errors)
                self.assertGreaterEqual(len(opened),2)
                out=fixture.ROOT/'build/capture-notification';out.mkdir(exist_ok=True)
                (out/'browser-result.json').write_text(json.dumps({'file_arrival_to_painted_ms':metrics,'formal_monitor_locked':True,'page_errors':errors},indent=2),encoding='utf-8')
                page.screenshot(path=str(out/'recovered.png'))
            finally:browser.close()


if __name__=='__main__':unittest.main()
