"""使用正式默认配置检查通知、文件完整性与中央图片完成加载的时延。"""
import io
import json
import os
from pathlib import Path
import statistics
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from PIL import Image
from catalog_store import Store
from folder_watch import DirectorySignal,FolderWatch,files_ready

ROOT=Path(__file__).resolve().parents[1]


class MonitorNotificationTests(unittest.TestCase):
    def test_closed_but_truncated_jpeg_is_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'分段.JPG';out=io.BytesIO()
            Image.new('RGB',(2400,1600),(80,120,100)).save(out,format='JPEG')
            payload=out.getvalue();target.write_bytes(payload[:-200])
            self.assertFalse(files_ready([target]))
            target.write_bytes(payload);self.assertTrue(files_ready([target]))

    def test_ready_pixels_are_reused_with_all_exif_orientations(self):
        import media_engine as media
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for orientation in range(1,9):
                with self.subTest(orientation=orientation):
                    path=root/f'{orientation}.JPG';source=Image.new('RGB',(800,600),'red')
                    source.paste('blue',(0,0,400,300));exif=Image.Exif();exif[274]=orientation
                    source.save(path,exif=exif);previews={}
                    self.assertTrue(files_ready([path],previews=previews))
                    record=media.capture_record(root,path,None,{'Orientation':orientation})
                    media.remember_quick_preview(record,root,*previews[str(path)])
                    with patch.object(media,'_read_preview',side_effect=AssertionError('重复读取原图')):
                        payload=media.quick_preview_bytes(record,root)
                    actual=Image.open(io.BytesIO(payload));expected=media._read_preview(path,480,orientation)
                    expected.thumbnail((480,480))
                    self.assertEqual(actual.size,expected.size)
                    self.assertNotIn(274,actual.getexif())
                    self.assertEqual(actual.getpixel((10,10))[2]>actual.getpixel((10,10))[0],expected.getpixel((10,10))[2]>expected.getpixel((10,10))[0])

    def test_waiter_wakes_on_start_stop_and_close(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();watch=FolderWatch(Store(base/'data',ROOT),autostart=False)
            try:
                def wait_for(action):
                    after=watch.state()['event_id'];received=[]
                    thread=threading.Thread(target=lambda:received.append(watch.wait_state(after,2)));thread.start()
                    action();thread.join(timeout=.5);self.assertFalse(thread.is_alive());return received[0]
                self.assertTrue(wait_for(lambda:watch.start(str(photos)))['enabled'])
                self.assertFalse(wait_for(watch.stop)['enabled'])
                wait_for(watch.close)
            finally:watch.close()

    @unittest.skipUnless(os.name=='nt','Windows 原生目录通知')
    def test_native_notification_reports_nested_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);child=root/'nested';child.mkdir();signal=DirectorySignal(root,True)
            try:
                self.assertIsNotNone(signal.handle)
                (child/'a.JPG').write_bytes(b'photo')
                until=time.monotonic()+1
                while time.monotonic()<until:
                    if signal.changed():break
                    time.sleep(.01)
                else:self.fail('Windows 未收到目录变化通知')
            finally:signal.close()

    @unittest.skipUnless(os.name=='nt','Windows 原生目录名称通知')
    def test_native_named_notification_rearms_and_handles_rename(self):
        from directory_events import DirectoryEvents
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);child=root/'nested';child.mkdir();signal=DirectoryEvents(root,True)
            try:
                self.assertIsNotNone(signal.handle)
                first=child/'第一张.JPG';first.write_bytes(b'photo')
                def wait_for(path):
                    until=time.monotonic()+2;seen=[]
                    while time.monotonic()<until:
                        if signal.changed():
                            self.assertIsNotNone(signal.paths);seen.extend(signal.paths)
                            if path in seen:return
                        time.sleep(.01)
                    self.fail(f'未收到 {path.name} 的名称通知：{seen}')
                wait_for(first)
                second=child/'第二张.JPG';first.rename(second);wait_for(second)
                third=child/'third.JPG';third.write_bytes(b'next');wait_for(third)
            finally:signal.close()


class MonitorLatencyBrowserTests(unittest.TestCase):
    def test_default_pipeline_image_ready_and_unpause(self):
        from playwright.sync_api import sync_playwright
        import server
        chrome=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
        if not chrome.is_file():self.skipTest('未安装测试 Chrome')
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'现场传图';photos.mkdir();store=Store(base/'data',ROOT);watch=FolderWatch(store)
            patches=[patch.object(server,k,v) for k,v in [('STORE',store),('DATA',store.data),('WATCH',watch),('MOUSE',None),('MODELS',None),('SKU',None),('ARCHIVE',None),('ANALYSIS',None),('RAWLINK',None),('FILESEARCH',None)]]
            for p in patches:p.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path=str(chrome),headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.locator('#folder-watch').click();page.locator('#watch-path').fill(str(photos));page.locator('#watch-start').click()
                        page.wait_for_function('()=>!document.getElementById("watch-dialog").open && FRAMEKEEP_SESSION.id!==null')
                        output=io.BytesIO();Image.new('RGB',(6000,4000),(80,120,100)).save(output,format='JPEG');payload=output.getvalue()
                        def loaded(name):
                            page.wait_for_function('(name)=>document.getElementById("filename").textContent===name&&!window.FramekeepReceiving?.active&&document.querySelector("#photos [data-loupe] img")?.alt===name&&document.querySelector("#photos [data-loupe] img")?.complete&&document.querySelector("#photos [data-loupe] img")?.naturalWidth===6000',arg=name)
                        times=[]
                        for n in range(4):
                            name=f'{n:03d}.JPG';(photos/name).write_bytes(payload);started=time.perf_counter();loaded(name)
                            times.append((time.perf_counter()-started)*1000)
                        self.assertLess(statistics.median(times[1:]),1000,times)
                        # 标星更新不能重建已完成解码的中央 img。
                        page.evaluate('()=>window.__central=document.querySelector("#photos img")')
                        page.locator('#stars [data-rating="3"]').click()
                        self.assertTrue(page.evaluate('()=>window.__central===document.querySelector("#photos img")'))
                        # 右键菜单暂停期间新图到达，关闭后无需等下一张图触发。
                        page.evaluate('()=>Framekeep.setInteractionPaused(true)');(photos/'paused.JPG').write_bytes(payload)
                        page.wait_for_function('()=>FramekeepIO.api("/api/watch").then(s=>s.arrivals===5)')
                        page.wait_for_timeout(120);self.assertEqual(page.locator('#filename').inner_text(),'003.JPG')
                        page.evaluate('()=>Framekeep.setInteractionPaused(false)');started=time.perf_counter();loaded('paused.JPG')
                        self.assertLess((time.perf_counter()-started)*1000,1000)
                        self.assertFalse(errors,errors)
                        out=ROOT/'build/monitor-performance';out.mkdir(exist_ok=True)
                        (out/'default-regression.json').write_text(json.dumps({'write_closed_to_image_ready_ms':times,'page_errors':errors},indent=2),encoding='utf8')
                        page.screenshot(path=str(out/'default-regression.png'))
                    finally:browser.close()
            finally:
                watch.close();http.shutdown();http.server_close()
                for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
