"""实际工作台设置、监控/归档预热与进程重启复用验收。"""
import json
import os
from pathlib import Path
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

class ImageCacheBrowserTests(unittest.TestCase):
    def test_settings_imports_and_restart_reuse(self):
        out=ROOT/'build/image-cache';out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='accept-',dir=out) as folder:
            base=Path(folder);photos=base/'监控照片';photos.mkdir();archive=base/'归档照片';archive.mkdir()
            for target in (photos,archive):
                for i in range(8):
                    exif=Image.Exif();exif[36867]=f'2026:10:23 10:00:{i:02d}'
                    Image.new('RGB',(1800,1200),(60+i*12,120,180 if target==archive else 160)).save(target/f'照片{i:03d}.JPG',exif=exif)
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            url=f'http://127.0.0.1:{port}'
            env={k:v for k,v in os.environ.items() if not k.startswith('FRAMEKEEP_')}
            env.update(FRAMEKEEP_DATA=str(base/'data'),FRAMEKEEP_CONFIG=str(base/'no-config.md'))
            engine=os.environ.get('FRAMEKEEP_TEST_ENGINE')
            if engine:env['FRAMEKEEP_APP_ROOT']=str(Path(engine).resolve().parents[1])
            command=([engine] if engine else [sys.executable,'-X','utf8',str(ROOT/'选片助手/server.py')])+['--port',str(port),'--no-browser']
            def api(path):
                with urllib.request.urlopen(url+path,timeout=5) as response:return json.load(response)
            def start(log):
                proc=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                until=time.monotonic()+35
                while time.monotonic()<until:
                    try:api('/api/health');return proc
                    except OSError:time.sleep(.1)
                    if proc.poll() is not None:break
                proc.kill();proc.wait();self.fail('引擎未就绪，见 engine.log')
            def stop(proc):proc.terminate();proc.wait(timeout=15)
            def wait_cache():
                until=time.monotonic()+30
                while time.monotonic()<until:
                    state=api('/api/preview-cache')
                    # 页面可同时请求其他尺寸；32 是后台必备两档数量，不是总数上限。
                    if state['pending']==0 and state['images']>=32:return state
                    time.sleep(.1)
                self.fail(str(state))
            def wait_job(action):
                until=time.monotonic()+45
                while time.monotonic()<until:
                    job=api('/api/preview-cache')['job']
                    if job.get('action')==action and job['status']=='completed':return job
                    if job.get('action')==action and job['status']=='failed':self.fail(str(job))
                    time.sleep(.1)
                self.fail(str(job))
            with (out/'engine.log').open('w',encoding='utf8') as log,sync_playwright() as pw:
                proc=start(log);browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                errors=[]
                try:
                    page=browser.new_page(viewport={'width':1440,'height':960},reduced_motion='reduce');page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!!window.FramekeepWorkspace')
                    page.locator('#app-settings-open').click();page.locator('[data-settings-section=preview]').click()
                    page.wait_for_function('()=>!document.getElementById("preview-cache-limit").disabled')
                    self.assertEqual(page.locator('#preview-cache-limit').input_value(),'10')
                    for invalid in ('0','257','1.5'):
                        page.locator('#preview-cache-limit').fill(invalid)
                        self.assertFalse(page.locator('#preview-cache-limit').evaluate('(e)=>e.checkValidity()'))
                    for value in (1,256,16):
                        page.locator('#preview-cache-limit').fill(str(value));page.locator('#preview-cache-save').click()
                        page.wait_for_function('()=>document.getElementById("preview-cache-message").textContent.includes("已保存")&&!document.getElementById("preview-cache-save").disabled')
                        self.assertEqual(api('/api/preview-cache')['limit_gb'],value)
                    invalid=page.evaluate("async()=>{try{await FramekeepIO.api('/api/preview-cache',{limit_gb:257});return ''}catch(e){return e.message}}")
                    self.assertIn('1–256',invalid)
                    page.screenshot(path=str(out/'settings.png'))
                    page.set_viewport_size({'width':375,'height':850});page.screenshot(path=str(out/'settings-narrow.png'))
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    page.set_viewport_size({'width':1440,'height':960});page.locator('#app-settings-close').click()
                    page.locator('#folder-watch').click();page.locator('#watch-path').fill(str(photos));page.locator('#watch-start').click()
                    page.wait_for_function('()=>DEMO_PHOTOS.length===8',timeout=30000)
                    monitor=page.evaluate('DEMO_PHOTOS[0].previewLarge')
                    page.locator('#asset-archive-open').click();page.locator('#catalog-add-root').click()
                    page.locator('#catalog-location-path').fill(str(archive));page.locator('#catalog-location-submit').click()
                    page.wait_for_function('()=>!document.getElementById("catalog-location-dialog").open')
                    state=wait_cache();self.assertEqual(state['failed'],0)
                    item=api('/api/catalog/items')['rows'][0];archive_url=item['preview']+'&size=1800'
                    page.wait_for_function('()=>[...document.querySelectorAll(".catalog-card img")].filter(i=>i.complete&&i.naturalWidth).length===8')
                    page.screenshot(path=str(out/'archive.png'))
                    page.locator('#app-settings-open').click();page.locator('[data-settings-section=preview]').click()
                    cache_root=base/'高速缓存';page.locator('#cache-storage-path').fill(str(cache_root));page.locator('#cache-storage-migrate').click()
                    moved=wait_job('migrate');self.assertEqual(api('/api/preview-cache')['path'],str(cache_root))
                    page.wait_for_function('()=>!document.getElementById("cache-backup-export").disabled')
                    backups=base/'backups';backups.mkdir();page.locator('#cache-backup-folder').fill(str(backups));page.locator('#cache-backup-export').click()
                    exported=wait_job('export');self.assertTrue(Path(exported['file']).is_file())
                    page.wait_for_function('()=>!document.getElementById("cache-backup-export").disabled')
                    page.locator('#cache-storage-card').screenshot(path=str(out/'storage-desktop.png'))
                    page.set_viewport_size({'width':375,'height':850});page.locator('#cache-storage-card').screenshot(path=str(out/'storage-narrow.png'))
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    page.set_viewport_size({'width':1440,'height':960})
                    before={str(p.relative_to(cache_root)):p.stat().st_mtime_ns for p in cache_root.rglob('*.webp')}
                    page.close();stop(proc)
                    photos.rename(base/'监控离线');archive.rename(base/'归档离线')
                    proc=start(log);page=browser.new_page(viewport={'width':1440,'height':960});page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!!window.FramekeepIO')
                    results=[]
                    for target in (monitor,archive_url):
                        result=page.evaluate("async url=>{const r=await fetch(url),image=await createImageBitmap(await r.blob());return {status:r.status,width:image.width,height:image.height}}",target)
                        self.assertEqual(result['status'],200);self.assertEqual(result['width'],1800);results.append(result)
                    page.locator('#app-settings-open').click();page.locator('[data-settings-section=preview]').click()
                    page.wait_for_function('()=>!document.getElementById("preview-cache-limit").disabled')
                    self.assertEqual(page.locator('#preview-cache-limit').input_value(),'16')
                    self.assertEqual(api('/api/preview-cache')['path'],str(cache_root))
                    after={str(p.relative_to(cache_root)):p.stat().st_mtime_ns for p in cache_root.rglob('*.webp')}
                    self.assertEqual(before,after,'重启不得重写缓存');self.assertFalse(errors,errors)
                    page.close();stop(proc);env['FRAMEKEEP_DATA']=str(base/'新机器资料')
                    import shutil
                    new_photos=base/'新盘归档';shutil.copytree(base/'归档离线',new_photos)
                    for i,path in enumerate(new_photos.glob('*.JPG')):path.rename(new_photos/f'不同名称{i}.JPG')
                    proc=start(log);page=browser.new_page(viewport={'width':1440,'height':960});page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(url,wait_until='domcontentloaded');page.wait_for_function('()=>!!window.FramekeepWorkspace')
                    page.locator('#app-settings-open').click();page.locator('[data-settings-section=preview]').click()
                    page.wait_for_function('()=>!document.getElementById("cache-backup-import").disabled')
                    page.locator('#cache-backup-file').fill(exported['file']);page.locator('#cache-backup-import').click()
                    imported=wait_job('import');self.assertEqual(imported['conflicts'],0)
                    imported_names={p.name for p in (base/'新机器资料').rglob('*.webp')}
                    page.locator('#asset-archive-open').click();page.locator('#catalog-add-root').click()
                    page.locator('#catalog-location-path').fill(str(new_photos));page.locator('#catalog-location-submit').click()
                    page.wait_for_function('()=>!document.getElementById("catalog-location-dialog").open')
                    page.wait_for_function('()=>[...document.querySelectorAll(".catalog-card img")].filter(i=>i.complete&&i.naturalWidth).length===8')
                    for row in api('/api/catalog/items')['rows']:
                        result=page.evaluate("async url=>{const r=await fetch(url+'&size=1800');return r.status}",row['preview']);self.assertEqual(result,200)
                    self.assertEqual({p.name for p in (base/'新机器资料').rglob('*.webp')},imported_names,'跨机器重新归档不能重新生成缓存文件')
                    self.assertFalse(errors,errors);page.screenshot(path=str(out/'new-machine.png'))
                    (out/'result.json').write_text(json.dumps({'passed':True,'cache':state,'migration':moved,'export':exported,'import':imported,'new_machine_matched':8,'restart_offline':results,'unchanged_cache_files':len(after),'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf8')
                finally:
                    browser.close()
                    if proc.poll() is None:stop(proc)

if __name__=='__main__':unittest.main()
