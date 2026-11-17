"""实际跨来源图像通道：业务连接饱和、Canvas、凭证与路径边界。"""
from contextlib import closing
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from direct_channel import DirectImageChannel
from test_jpeg_receiving_preview import application_runtime,camera_jpeg,ROOT


class DirectChannelTests(unittest.TestCase):
    def test_late_notification_still_presents_already_admitted_jpeg(self):
        from playwright.sync_api import sync_playwright
        with application_runtime() as (server,base,photos,store,watch,sid,url),closing(DirectImageChannel(watch,url)) as channel,patch.object(server,'DIRECT',channel),sync_playwright() as pw:
            gate=threading.Event();original=server.Handler.do_GET
            def hold(handler):
                if handler.path.startswith('/api/test-hold'):
                    gate.wait(8);return handler.reply({'done':True})
                return original(handler)
            with patch.object(server.Handler,'do_GET',hold):
                browser=pw.chromium.launch(channel='msedge',headless=True)
                try:
                    page=browser.new_page();pending=[];delayed=[True]
                    def notifications(route):
                        if delayed[0]:pending.append(route)
                        else:route.continue_()
                    page.route(channel.origin+'/api/watch/direct?**',notifications)
                    page.goto(url,wait_until='domcontentloaded')
                    page.wait_for_function('()=>!FramekeepBoot.loading&&document.querySelector("#photos [data-loupe] img")?.naturalWidth>0')
                    page.evaluate('()=>{for(let i=0;i<6;i++)fetch("/api/test-hold?"+i).catch(()=>{})}')
                    page.wait_for_timeout(100)
                    (photos/'ALREADY_READY.JPG').write_bytes(camera_jpeg())
                    deadline=time.monotonic()+3
                    while len(store.records(sid))<2 and time.monotonic()<deadline:page.wait_for_timeout(10)
                    self.assertEqual(len(store.records(sid)),2)
                    state=watch.direct.state();self.assertIsNone(state['receiving']);self.assertEqual(state['latest']['name'],'ALREADY_READY.JPG')
                    self.assertTrue(pending);delayed[0]=False
                    for route in pending:route.continue_()
                    page.wait_for_function('()=>document.querySelector(".receiving-photo-preview img")?.alt==="ALREADY_READY.JPG"&&document.querySelector(".receiving-photo-preview img")?.naturalWidth===3600',timeout=1000)
                    self.assertFalse(gate.is_set())
                finally:gate.set();browser.close()

    def test_credentials_exact_origin_and_scoped_original_bytes(self):
        with application_runtime() as (server,base,photos,store,watch,sid,url),closing(DirectImageChannel(watch,url)) as channel:
            source=photos/'DIRECT.JPG';payload=camera_jpeg();source.write_bytes(payload)
            address=channel.register(photos,source.name)
            self.assertEqual(address,channel.register(photos,source.name))
            with urllib.request.urlopen(urllib.request.Request(address,headers={'Origin':url})) as response:
                self.assertEqual(response.read(),payload);self.assertEqual(response.headers['Access-Control-Allow-Origin'],url)
            state_url=channel.origin+'/api/watch/direct'
            for headers in ({},{'Origin':'https://unrelated.invalid','X-Framekeep-Preview':channel.token}):
                with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(urllib.request.Request(state_url,headers=headers))
                self.assertEqual(caught.exception.code,403)
            with urllib.request.urlopen(urllib.request.Request(state_url,headers={'Origin':url,'X-Framekeep-Preview':channel.token})) as response:
                self.assertTrue(json.load(response)['enabled'])
            with urllib.request.urlopen(urllib.request.Request(state_url,method='OPTIONS',headers={'Origin':url,'Access-Control-Request-Headers':'X-Framekeep-Preview'})) as response:
                self.assertEqual(response.status,204);self.assertEqual(response.headers['Access-Control-Allow-Origin'],url)
            outside=base/'outside.JPG';outside.write_bytes(b'private outside content')
            with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(channel.register(photos,'../outside.JPG'))
            self.assertEqual(caught.exception.code,400)

    def test_saturated_business_connections_cannot_delay_original_frame(self):
        from playwright.sync_api import sync_playwright
        with application_runtime() as (server,base,photos,store,watch,sid,url),closing(DirectImageChannel(watch,url)) as channel,patch.object(server,'DIRECT',channel),sync_playwright() as pw:
            gate=threading.Event();blocked=[];original=server.Handler.do_GET
            def hold(handler):
                if handler.path.startswith('/api/test-hold'):
                    blocked.append(handler.path);gate.wait(8);return handler.reply({'done':True})
                return original(handler)
            with patch.object(server.Handler,'do_GET',hold):
                browser=pw.chromium.launch(channel='msedge',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':960});errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(url,wait_until='domcontentloaded')
                    page.wait_for_function('()=>!FramekeepBoot.loading&&FramekeepReceiving&&document.querySelector("#photos [data-loupe] img")?.naturalWidth>0')
                    page.evaluate('()=>{for(let i=0;i<6;i++)fetch("/api/test-hold?"+i).catch(()=>{})}')
                    deadline=time.monotonic()+3
                    while len(blocked)<4 and time.monotonic()<deadline:page.wait_for_timeout(10)
                    self.assertGreaterEqual(len(blocked),4)
                    with (photos/'PRIORITY.JPG').open('wb') as writer:
                        started=time.perf_counter();writer.write(camera_jpeg());writer.flush()
                        page.wait_for_function('()=>document.querySelector(".receiving-photo-preview img")?.alt==="PRIORITY.JPG"&&document.querySelector(".receiving-photo-preview img")?.naturalWidth===3600',timeout=1000)
                        page.wait_for_function('()=>document.querySelector(".watch-direct-preview img")?.alt==="PRIORITY.JPG"&&document.querySelector(".watch-direct-preview img")?.naturalWidth===3600',timeout=1000)
                        page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
                        elapsed=(time.perf_counter()-started)*1000
                        result=page.evaluate('''()=>{const i=document.querySelector('.receiving-photo-preview img'),c=document.createElement('canvas');c.width=c.height=1;const ctx=c.getContext('2d');ctx.drawImage(i,0,0,1,1);return {source:i.src,crossOrigin:i.crossOrigin,pixel:[...ctx.getImageData(0,0,1,1).data]}}''')
                        self.assertTrue(result['source'].startswith(channel.origin));self.assertEqual(result['crossOrigin'],'anonymous')
                        self.assertFalse(gate.is_set());self.assertEqual(len(store.records(sid)),1)
                        page.screenshot(path=str(ROOT/'build/jpg-sidebar/independent-channel-saturated.png'))
                    gate.set()
                    page.wait_for_function('()=>!FramekeepReceiving.active&&document.querySelector("#photos [data-loupe] img")?.alt==="PRIORITY.JPG"')
                    page.wait_for_function('()=>!document.querySelector(".watch-direct-preview")&&document.querySelector("#watch-thumbnails .active img")?.naturalWidth>0')
                    self.assertFalse(errors,errors)
                    (ROOT/'build/jpg-sidebar/independent-channel-saturated.json').write_text(json.dumps({'painted_ms':elapsed,'blocked_business_connections':len(blocked),'canvas_readable':True,'original_resolution':[3600,2400],'page_errors':errors},indent=2),encoding='utf8')
                finally:gate.set();browser.close()


if __name__=='__main__':unittest.main()
