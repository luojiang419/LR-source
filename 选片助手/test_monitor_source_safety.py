"""真实完整引擎与照片副本：历史接续、回看及原文件逐字节保护。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from PIL import Image
from playwright.sync_api import sync_playwright
from catalog_store import Store

ROOT=Path(__file__).resolve().parents[1]
SAMPLE=ROOT/'build/raw-reconcile/r6ii-6402.CR3'


def snapshot(folder):
    return {p.relative_to(folder).as_posix():{
        'bytes':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns,
        'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in folder.rglob('*') if p.is_file()}


class MonitorSourceSafetyTests(unittest.TestCase):
    def test_real_engine_preserves_copied_history_switch_resume_and_restart(self):
        chrome=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
        if not chrome.is_file():self.skipTest('真实浏览器验收需要 Chrome')
        engine=os.environ.get('FRAMEKEEP_TEST_ENGINE')
        label=os.environ.get('FRAMEKEEP_SAFETY_LABEL','source')
        out=ROOT/'build/monitor-source-safety'/label;out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='framekeep-photo-safety-') as folder:
            base=Path(folder);fixtures=base/'副本来源';fixtures.mkdir()
            photos=base/'其他软件已拍摄';photos.mkdir();other=base/'切换目录';other.mkdir();data=base/'data'
            for i in range(30):
                path=fixtures/f'IMG_{i:04d}.JPG';exif=Image.Exif();exif[36867]=f'2026:10:21 12:00:{i:02d}'
                Image.new('RGB',(640,420),(30+i*5,90,120)).save(path,exif=exif)
                destination=photos/('子目录' if i>=28 else '')/path.name;destination.parent.mkdir(exist_ok=True)
                shutil.copy2(path,destination)
            sidecar=photos/'IMG_0003.xmp'
            sidecar.write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:xmp="http://ns.adobe.com/xap/1.0/"><rdf:RDF><rdf:Description xmp:Rating="3" /></rdf:RDF></x:xmpmeta>',encoding='utf8')
            (photos/'其他软件拍摄记录.txt').write_text('保留其他软件的数据',encoding='utf8')
            # 真实公开 CR3 样片只拷贝进临时目录；未准备样片时明确记录。
            raw_checked=SAMPLE.is_file()
            if raw_checked:
                shutil.copy2(SAMPLE,photos/'PAIR.CR3');shutil.copy2(fixtures/'IMG_0000.JPG',photos/'PAIR.JPG')
            expected_count=30+int(raw_checked);before=snapshot(photos)
            store=Store(data,ROOT);sid=store.ensure_live_session(photos)
            store.save(sid,0,{'folderPrefix':'旧软件目录已不存在','filter':'picked','query':'不存在的搜索','overrides':{}})
            store.activate(sid)
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();url=f'http://127.0.0.1:{port}'
            env={**os.environ,'FRAMEKEEP_DATA':str(data),'FRAMEKEEP_CONFIG':str(base/'absent.md'),
                 'FRAMEKEEP_APP_ROOT':str(ROOT),'FRAMEKEEP_DESKTOP_PID':str(os.getpid()),
                 'FRAMEKEEP_DESKTOP_TOKEN':'photo-safety-test','FRAMEKEEP_CANON_REGISTRATION':'0'}
            command=[engine] if engine else [sys.executable,'-X','utf8',str(ROOT/'选片助手/server.py')]
            process=None
            with (out/'engine.log').open('wb') as log:
                def api(path,body=None):
                    request=urllib.request.Request(url+path,None if body is None else json.dumps(body).encode(),
                        {'Content-Type':'application/json','X-Framekeep-Desktop':'photo-safety-test','Origin':url})
                    with urllib.request.urlopen(request,timeout=10) as response:return json.load(response)
                def start():
                    proc=subprocess.Popen(command+['--port',str(port),'--no-browser'],cwd=ROOT,env=env,
                        stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    until=time.monotonic()+40
                    while time.monotonic()<until:
                        if proc.poll() is not None:raise RuntimeError('引擎提前退出，请检查验收日志')
                        try:
                            if api('/api/health')['application']=='framekeep':return proc
                        except OSError:time.sleep(.1)
                    proc.terminate();proc.wait(timeout=10);raise TimeoutError('引擎启动超时')
                def stop():
                    api('/api/desktop/shutdown',{});process.wait(timeout=25)
                try:
                    process=start()
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch(executable_path=str(chrome),headless=True)
                        try:
                            page=browser.new_page(viewport={'width':1440,'height':960},reduced_motion='reduce')
                            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                            page.goto(url,wait_until='domcontentloaded')
                            def monitor(path):
                                pending=[];page.route('**/api/watch',lambda route:pending.append(route))
                                page.locator('#folder-watch').click();page.locator('#watch-path').fill(str(path))
                                # 明确让旧配置在用户输入之后返回，覆盖异步回填竞态。
                                page.wait_for_timeout(50)
                                for route in pending:route.continue_()
                                page.unroute('**/api/watch');page.wait_for_timeout(50)
                                self.assertEqual(page.locator('#watch-path').input_value(),str(path))
                                page.locator('#watch-start').click()
                                page.wait_for_function('()=>!document.getElementById("watch-dialog").open')
                            def loaded(count):
                                page.wait_for_function('(count)=>window.FramekeepBoot?.loading===false&&window.DEMO_PHOTOS?.length===count&&window.Framekeep?.getSnapshot().count===count',arg=count,timeout=45000)
                            def unchanged():self.assertEqual(snapshot(photos),before)
                            monitor(photos);loaded(expected_count)
                            self.assertEqual(page.evaluate('Framekeep.browseContext().folderPrefix'),'')
                            page.wait_for_function('()=>FramekeepIO.api("/api/watch").then(s=>!s.initializing)')
                            rated=page.evaluate('Framekeep.photoItems().find(i=>i.photo.jpg==="IMG_0003.JPG").rating')
                            self.assertEqual(rated,3)
                            unchanged()
                            # 监控中的旧子目录筛选，在重新开始监控时必须回到全部照片。
                            page.evaluate('Framekeep.setFolderPrefix("子目录")');monitor(photos);loaded(expected_count);unchanged()
                            # 新图接续与回看：暂停跟随后浏览最早照片，再来一张也不能抢走当前照片。
                            source=fixtures/'IMG_0000.JPG';shutil.copy2(source,photos/'NEW_0001.JPG')
                            before['NEW_0001.JPG']=snapshot(photos)['NEW_0001.JPG']
                            loaded(expected_count+1)
                            page.wait_for_function('()=>document.getElementById("filename").textContent==="NEW_0001.JPG"')
                            page.evaluate('FramekeepWatch.setFollow(false);Framekeep.browsePhoto(DEMO_PHOTOS.find(p=>p.jpg==="IMG_0000.JPG").id)')
                            shutil.copy2(source,photos/'NEW_0002.JPG');before['NEW_0002.JPG']=snapshot(photos)['NEW_0002.JPG'];loaded(expected_count+2)
                            self.assertEqual(page.locator('#filename').inner_text(),'IMG_0000.JPG');unchanged()
                            page.screenshot(path=str(out/'history-and-arrivals.png'))
                            monitor(other);page.wait_for_function('(path)=>FRAMEKEEP_SESSION.root===path',arg=str(other));unchanged()
                            monitor(photos);loaded(expected_count+2);unchanged()
                            page.locator('#folder-watch').click();page.locator('#watch-stop').click()
                            page.wait_for_function('()=>document.getElementById("watch-stop").disabled');page.locator('#watch-close').click();unchanged()
                            monitor(photos);loaded(expected_count+2);page.evaluate('FramekeepIO.flush()');unchanged()
                            stop();process=start();page.reload(wait_until='domcontentloaded');loaded(expected_count+2);unchanged()
                            self.assertTrue(api('/api/watch')['enabled']);self.assertFalse(errors,errors)
                            # 仅选择/浏览不应产生自动评级文件写入。
                            self.assertEqual(api('/api/ratings/sync')['pending'],0)
                            # 主动标星只长期保留字段档案，成功提交不积累 JPG/XMP 整文件副本。
                            page.evaluate('()=>Framekeep.browsePhoto(DEMO_PHOTOS.find(p=>p.jpg==="IMG_0003.JPG").id)')
                            page.locator('#stars [data-rating="4"]').click();page.evaluate('()=>FramekeepIO.flush()')
                            deadline=time.monotonic()+20
                            while time.monotonic()<deadline:
                                rating_sync=api('/api/ratings/sync')
                                if rating_sync['pending']==0:break
                                time.sleep(.1)
                            self.assertEqual(rating_sync['pending'],0);self.assertEqual(rating_sync['failed'],0,rating_sync)
                            self.assertGreater(rating_sync['applied'],0)
                            self.assertFalse(list(photos.glob('.framekeep-recovery-*/original.bin')))
                            histories=[json.loads(p.read_text('utf8')) for p in (data/'metadata-history').glob('*.json')]
                            recorded={r['journal']['before_hash'] for r in histories if r['status']=='applied'}
                            for name in ('IMG_0003.JPG','IMG_0003.xmp'):self.assertIn(before[name]['sha256'],recorded)
                            after_rating=snapshot(photos)
                            for name,details in before.items():
                                if name not in ('IMG_0003.JPG','IMG_0003.xmp'):self.assertEqual(after_rating[name],details)
                            result={'passed':True,'engine':engine or 'Python完整引擎','raw_sample_copied':raw_checked,
                                'capture_count':expected_count+2,'preserved_files':before,'page_errors':errors,
                                'explicit_rating_small_field_history_verified':True,
                                'checks':['历史照片超过24张分批完整载入','已有侧车星级保留且不写回原图','重入清除旧目录及搜索筛选',
                                          'RAW/JPG配对','新图顺延','暂停跟随后回看历史','切换目录','停止重开','进程重启恢复','原文件路径/大小/时间/哈希全部保持']}
                            (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
                        finally:browser.close()
                finally:
                    if process is not None and process.poll() is None:
                        try:stop()
                        except Exception:process.terminate();process.wait(timeout=10)


if __name__=='__main__':unittest.main()
