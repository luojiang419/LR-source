"""真实服务：监控预生成、重启离线浏览、各主题滚动条拖拽。"""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]


class PreviewCacheBrowserTests(unittest.TestCase):
    def test_watch_warms_restart_reuses_and_theme_scrollbar_drags(self):
        out=ROOT/'build/preview-cache-acceptance';out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='test-',dir=out) as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();data=base/'data'
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            url=f'http://127.0.0.1:{port}';secret=secrets.token_urlsafe(24)
            env={k:v for k,v in os.environ.items() if not k.startswith('FRAMEKEEP_')}
            env.update(FRAMEKEEP_DATA=str(data),FRAMEKEEP_DESKTOP_TOKEN=secret,FRAMEKEEP_CONFIG=str(base/'no-config.md'))
            engine=os.environ.get('FRAMEKEEP_TEST_ENGINE')
            if engine:env['FRAMEKEEP_APP_ROOT']=str(Path(engine).resolve().parents[1])
            command=([engine] if engine else [sys.executable,'-X','utf8',str(ROOT/'选片助手/server.py')])+['--port',str(port),'--no-browser']
            def api(path):
                with urllib.request.urlopen(url+path,timeout=3) as response:return json.load(response)
            def start(log):
                proc=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    try:api('/api/health');return proc
                    except OSError:time.sleep(.1)
                    if proc.poll() is not None:break
                proc.kill();proc.wait();self.fail('测试引擎未就绪，见 engine.log')
            def stop(proc):
                # 与现有集成测试一致：测试专用进程退出，不触及用户进程。
                proc.terminate();proc.wait(timeout=15)
            with (out/'engine.log').open('w',encoding='utf8') as log,sync_playwright() as pw:
                proc=start(log);browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True,args=['--disable-features=OverlayScrollbar'])
                errors=[];checks=[]
                try:
                    page=browser.new_page(viewport={'width':1440,'height':940},reduced_motion='reduce')
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepIO&&window.FramekeepLayout')
                    page.locator('#folder-watch').click();page.locator('#watch-path').fill(str(photos));page.locator('#watch-start').click()
                    page.wait_for_function('()=>FramekeepLayout.isMonitoring()')
                    for i in range(48):
                        exif=Image.Exif();exif[36867]=f'2026:10:22 10:00:{i:02d}'
                        Image.new('RGB',(1600,2400),(50+i*3,100,150)).save(photos/f'{i:03d}.JPG',exif=exif)
                    page.wait_for_function('()=>DEMO_PHOTOS.length===48',timeout=30000)
                    deadline=time.monotonic()+45
                    while time.monotonic()<deadline:
                        state=api('/api/preview-cache')
                        if state['images']==96 and not state['pending']:break
                        time.sleep(.1)
                    self.assertEqual(state['images'],96,state);self.assertEqual(state['pending'],0,state)
                    self.assertLessEqual(state['bytes'],state['limit_bytes']);self.assertEqual(state['failed'],0)
                    page.locator('#watch-follow-toggle').click();page.keyboard.press('Home')
                    for theme in ['dark','light','pure-white','hardcore']:
                        page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                        page.wait_for_timeout(300);page.locator('#watch-thumbnails').evaluate('(e)=>e.scrollTop=0');page.wait_for_timeout(200)
                        geometry=page.locator('#watch-scrollbar').evaluate('''e=>{const r=e.getBoundingClientRect(),thumb=e.firstElementChild,t=thumb.getBoundingClientRect();return {x:t.x+t.width/2,y:t.y+t.height/2,height:r.height,width:r.width,color:getComputedStyle(thumb).backgroundColor,accent:getComputedStyle(document.documentElement).getPropertyValue('--theme-emphasis').trim()}}''')
                        self.assertGreater(geometry['width'],0,geometry)
                        selected=page.evaluate('Framekeep.remoteTarget().photoId')
                        page.mouse.move(geometry['x'],geometry['y']);page.mouse.down()
                        page.mouse.move(geometry['x'],geometry['y']+geometry['height']*.6,steps=12);page.mouse.up()
                        page.wait_for_timeout(200)
                        scroll=page.locator('#watch-thumbnails').evaluate('(e)=>e.scrollTop')
                        self.assertGreater(scroll,1000,(theme,geometry,scroll))
                        self.assertEqual(page.evaluate('Framekeep.remoteTarget().photoId'),selected)
                        self.assertLess(page.locator('#watch-thumbnails img').count(),20)
                        page.locator('#watch-scrollbar').focus();page.keyboard.press('Home')
                        page.wait_for_function('()=>document.getElementById("watch-thumbnails").scrollTop===0')
                        page.keyboard.press('PageDown');page.wait_for_function('()=>document.getElementById("watch-thumbnails").scrollTop>100')
                        page.screenshot(path=str(out/f'scrollbar-{theme}.png'));checks.append({'theme':theme,'geometry':geometry,'scrollTop':scroll})
                    sample=page.evaluate('DEMO_PHOTOS[0].previewLarge');page.close();stop(proc)
                    photos.rename(base/'offline-photos')
                    proc=start(log)
                    # 新浏览器上下文无 HTTP 缓存；源目录离线，验证跨进程磁盘缓存仍可读取。
                    page=browser.new_page(viewport={'width':1440,'height':940});page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>window.FramekeepIO&&DEMO_PHOTOS.length===48')
                    result=page.evaluate('''async url=>{const r=await fetch(url),blob=await r.blob(),img=await createImageBitmap(blob);return {status:r.status,width:img.width,height:img.height,cache:r.headers.get('Cache-Control')}}''',sample)
                    self.assertEqual(result['status'],200,result);self.assertEqual(result['height'],2048,result)
                    self.assertIn('immutable',result['cache']);self.assertFalse(errors,errors)
                    (out/'result.json').write_text(json.dumps({'passed':True,'cache':state,'restart_offline':result,'scrollbars':checks,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf8')
                finally:
                    browser.close()
                    if proc.poll() is None:stop(proc)


if __name__=='__main__':unittest.main()
