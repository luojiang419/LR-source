"""启动完整正式引擎，以真实HTTP、目录监控与SQLite验收已确认布局。"""
import json
import os
from pathlib import Path
import secrets
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
from test_sku_workflow import fixture_book

ROOT=Path(__file__).resolve().parents[1]


class WorkbenchIntegrationTests(unittest.TestCase):
    def test_real_engine_theme_navigation_watch_rating_and_progress(self):
        out=ROOT/'build/workbench-integration';out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='test-',dir=out) as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            secret=secrets.token_urlsafe(24)
            env={k:v for k,v in os.environ.items() if not k.startswith('FRAMEKEEP_')}
            env.update(FRAMEKEEP_DATA=str(base/'data'),FRAMEKEEP_DESKTOP_TOKEN=secret,
                       FRAMEKEEP_CONFIG=str(base/'no-config.md'))
            engine=os.environ.get('FRAMEKEEP_TEST_ENGINE')
            if engine:env['FRAMEKEEP_APP_ROOT']=str(Path(engine).resolve().parents[1])
            command=[engine] if engine else [sys.executable,'-X','utf8',str(ROOT/'选片助手/server.py')]
            url=f'http://127.0.0.1:{port}'
            with (out/'engine.log').open('w',encoding='utf-8') as log:
                process=subprocess.Popen(command+['--port',str(port),'--no-browser'],cwd=ROOT,env=env,
                                         stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                try:
                    deadline=time.monotonic()+30
                    while True:
                        try:
                            with urllib.request.urlopen(url+'/api/health',timeout=1) as response:
                                self.assertEqual(json.load(response)['application'],'framekeep')
                            break
                        except OSError:
                            if process.poll() is not None or time.monotonic()>deadline:self.fail('引擎未就绪，见engine.log')
                            time.sleep(.1)
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                        try:
                            context=browser.new_context(viewport={'width':1920,'height':1080},reduced_motion='reduce')
                            page=context.new_page();errors=[];failed=[]
                            page.on('pageerror',lambda e:errors.append(str(e)))
                            page.on('response',lambda r:failed.append(f'{r.status} {r.url}') if r.status>=400 else None)
                            page.goto(url,wait_until='networkidle')
                            self.assertEqual(page.locator('.workspace-bottom-bar .workspace-tabs').count(),1)
                            self.assertEqual(page.locator('.app-header>#selection-tools').count(),1)
                            self.assertEqual(page.locator('.workspace-navigation').count(),0)
                            self.assertTrue(page.locator('#export-open').is_hidden())
                            self.assertEqual(page.locator('.selection-sidebar-footer>#send-lr').count(),1)
                            page.locator('#folder-watch').click();page.locator('#watch-path').fill(str(photos));page.locator('#watch-start').click()
                            page.wait_for_function('()=>window.FramekeepLayout?.isMonitoring()')
                            shutil.copy2(ROOT/'演示方案/assets/XSY_0929.jpg',photos/'001.JPG')
                            page.wait_for_function('()=>document.querySelector("#filename").textContent==="001.JPG"')
                            page.locator('#stars [data-rating="4"]').click();page.evaluate('()=>FramekeepIO.flush()')
                            first=page.evaluate('()=>Framekeep.remoteTarget().photoId')
                            Image.new('RGB',(900,600),(90,140,110)).save(photos/'002.JPG')
                            page.wait_for_function('()=>document.querySelector("#filename").textContent==="002.JPG"')
                            self.assertEqual(page.locator('#stars .on').count(),0)
                            page.keyboard.press('ArrowLeft')
                            page.wait_for_function('()=>document.querySelector("#filename").textContent==="001.JPG"')
                            self.assertEqual(page.locator('#stars .on').count(),4)
                            page.evaluate('()=>FramekeepIO.flush()')

                            def choose(mode):
                                page.locator('#theme-open').click();page.locator(f'[data-framekeep-theme-choice="{mode}"]').click()
                                page.locator('[data-theme-done]').click()
                                page.wait_for_function('(mode)=>FramekeepTheme.get()===mode',arg=mode)

                            checks=[]
                            for mode in ['hardcore','light','pure-white','dark']:
                                choose(mode)
                                self.assertEqual(page.locator('#stars .on').count(),4)
                                self.assertEqual(page.locator('#filename').inner_text(),'001.JPG')
                                self.assertEqual(page.locator('.film-panel,#filmstrip,#layout-film').count(),0)
                                self.assertEqual(page.locator('#folder-watch').count(),1)
                                for width in [1920,1280,1024,375,320]:
                                    page.set_viewport_size({'width':width,'height':1000})
                                    check=page.evaluate('()=>({theme:FramekeepTheme.get(),width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth})')
                                    checks.append(check);self.assertFalse(check['overflow'],check)
                                page.set_viewport_size({'width':1920,'height':1080})
                                page.screenshot(path=str(out/f'{mode}.png'))

                            # 刷新及跨窗口主题同步不会重置评级或制造多个入口。
                            choose('hardcore');page.reload(wait_until='domcontentloaded')
                            page.wait_for_function('()=>window.FramekeepLayout?.isMonitoring()')
                            self.assertEqual(page.evaluate('()=>FramekeepTheme.get()'),'hardcore')
                            self.assertEqual(page.locator('#view-open').count(),1)
                            other=context.new_page();other.goto(url,wait_until='domcontentloaded')
                            other.wait_for_function('()=>!!window.FramekeepLayout')
                            choose('light');other.wait_for_function('()=>FramekeepTheme.get()==="light"')
                            self.assertEqual(other.locator('.library-main>.film-panel').count(),0);other.close()

                            def layout(name):
                                page.locator('#view-open').hover();page.locator('#layout-'+name).click()

                            layout('header');self.assertTrue(page.locator('.app-header').is_hidden())
                            self.assertTrue(page.locator('.workspace-bottom-bar>.workspace-tabs').is_visible())
                            page.locator('#sku-open').click();self.assertTrue(page.locator('#sku-dialog').is_visible())
                            page.locator('#selection-tab').click();layout('header')
                            layout('focus');self.assertTrue(page.locator('#view-open').is_visible());layout('focus')
                            self.assertTrue(page.locator('.workspace-bottom-bar .workspace-tabs').is_visible())
                            page.locator('#folder-watch').click();page.locator('#watch-stop').click();page.locator('#watch-close').click()
                            page.wait_for_function('()=>!FramekeepLayout.isMonitoring()')
                            page.locator('#selection-tools>summary').click() if not page.locator('#selection-tools').evaluate('(e)=>e.open') else None
                            for view in ['grid','list','single']:page.locator(f'[data-view="{view}"]').click()
                            page.locator('#search').fill('001');self.assertIn('1',page.locator('#result-count').inner_text())
                            page.locator('#search').fill('');page.locator('#sort').select_option('rating-desc')
                            layout('tools');self.assertFalse(page.locator('#selection-tools').evaluate('(e)=>e.open'));layout('tools')
                            self.assertTrue(page.locator('#selection-tools').evaluate('(e)=>e.open'))
                            self.assertEqual(page.locator('.library-main>.preview-toolbar>.viewbar').count(),1)
                            self.assertEqual(page.locator('#selection-tools .viewbar').count(),0)
                            self.assertEqual(page.locator('#loupe-detail').count(),0)
                            self.assertTrue(page.locator('.preview-toolbar [data-view=grid]').is_visible())
                            page.screenshot(path=str(out/'selection-tools.png'))
                            page.locator('#view-open').hover();page.screenshot(path=str(out/'view-menu.png'))
                            page.locator('#theme-open').click();page.screenshot(path=str(out/'theme-dialog.png'))
                            self.assertEqual(page.locator('#theme-dialog [aria-pressed="true"]').count(),1)
                            page.locator('[data-theme-done]').click()
                            page.locator('#sku-open').click()
                            page.locator('#sku-file').set_input_files({'name':'拍摄清单.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':fixture_book()})
                            page.wait_for_function('()=>document.querySelectorAll("#sku-panel-scope option").length===3')
                            page.locator('#sku-panel-scope').select_option('模特图')
                            page.wait_for_function('()=>document.querySelectorAll("#sku-rows tbody tr").length===2')
                            self.assertEqual(page.locator('#sku-sheet-tabs button').count(),2)
                            page.locator('#sku-search').fill('P905');self.assertEqual(page.locator('#sku-rows tbody tr').count(),1)
                            page.screenshot(path=str(out/'shooting-progress.png'))
                            page.locator('#selection-tab').click();page.evaluate('()=>FramekeepIO.flush()')
                            manifest=page.evaluate('()=>FramekeepIO.manifest(false)')
                            ratings={r['id']:r['rating'] for r in manifest['captures']}
                            self.assertEqual(ratings[first],4)
                            self.assertFalse(errors,errors);self.assertFalse(failed,failed)
                            (out/'verification.json').write_text(json.dumps({'engine':engine or 'source','responsive':checks,'ratings':ratings,'page_errors':errors,'http_errors':failed},ensure_ascii=False,indent=2),encoding='utf-8')
                        finally:browser.close()
                finally:
                    try:
                        request=urllib.request.Request(url+'/api/desktop/shutdown',data=b'{}',headers={'X-Framekeep-Desktop':secret,'Content-Type':'application/json'})
                        urllib.request.urlopen(request,timeout=3).close();process.wait(timeout=10)
                    except (OSError,subprocess.TimeoutExpired):
                        process.terminate();process.wait(timeout=10)


if __name__=='__main__':unittest.main()
