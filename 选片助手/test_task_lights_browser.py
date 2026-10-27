"""真实任务摘要驱动六个导航灯，覆盖主题、无障碍和请求生命周期。"""
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright
import media_engine as media
import server
from test_task_status import ROOT,TaskStatusFixture


class TaskLightsBrowserTests(TaskStatusFixture):
    def test_queues_operation_lifetime_themes_and_connection_recovery(self):
        photos=self.base/'photos';photos.mkdir();image=photos/'001.JPG'
        Image.new('RGB',(800,600),(126,137,120)).save(image)
        sid=self.store.ensure_live_session(photos);self.store.activate(sid)
        self.store.append_live(sid,[media.capture_record(photos,image,None,{})])
        component=SimpleNamespace(state={'status':'idle'})
        component.snapshot=lambda:{**component.state,'ready':[]}
        values=dict(STORE=self.store,DATA=self.store.data,WATCH=None,MOUSE=None,MODELS=self.providers,SKU=self.sku,ARCHIVE=self.archive,CATALOG=self.catalog,ANALYSIS=self.analysis,RAWLINK=self.rawlink,COMPONENTS=component,UPDATES=None,DRIVER=None,FILESEARCH=None,PREVIEWS=None)
        patches=[patch.object(server,key,value) for key,value in values.items()]
        for item in patches:item.start()
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        threading.Thread(target=http.serve_forever,daemon=True).start()
        out=ROOT/'build/task-light-acceptance';out.mkdir(exist_ok=True)
        release=threading.Event()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1600,'height':1000});errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                    page.wait_for_function("()=>document.querySelectorAll('.workspace-task-light[data-state=idle]').length===6")
                    self.assertEqual(page.locator('.workspace-bottom-bar [role=tab]').count(),7)
                    # GET 轮询以及鼠标输入轮询不应伪造工作状态。
                    page.evaluate("async()=>{await Promise.all([FramekeepIO.api('/api/sku'),FramekeepIO.api('/api/analysis')]);const end=FramekeepTaskStatus.begin('/api/mouse/poll',{});end()}")
                    self.assertEqual(page.locator('.workspace-task-light[data-state=busy]').count(),0)
                    with self.previews.connect() as c:c.execute("INSERT INTO pending(identity,root,record,requested,attempts,error) VALUES ('preview',?,'{}',0,0,NULL)",(str(photos),))
                    with self.sku.connect() as c:
                        c.execute("INSERT INTO jobs(id,status,trigger) VALUES ('ocr','queued','manual')")
                        c.execute("INSERT INTO archive_events(id,delivered) VALUES ('event',0)")
                    with self.archive.connect() as c:c.execute("INSERT INTO metadata_jobs(id,status) VALUES ('write','queued')")
                    with self.analysis.connect() as c:c.execute("INSERT INTO jobs(id,status,trigger) VALUES ('parse','queued','manual')")
                    component.state['status']='working'
                    page.wait_for_function("()=>document.querySelectorAll('.workspace-task-light[data-state=busy]').length===6",timeout=8000)
                    self.assertIn('OCR 排队',page.locator('#sku-open').get_attribute('title'))
                    checks=[]
                    for theme in ('dark','light','pure-white','hardcore'):
                        page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                        for width,height in ((1600,1000),(1000,680),(375,812),(320,568)):
                            page.set_viewport_size({'width':width,'height':height})
                            geometry=page.locator('.workspace-bottom-bar').evaluate('''bar=>({bottom:bar.getBoundingClientRect().bottom,overflow:document.documentElement.scrollWidth>innerWidth,lamps:[...bar.querySelectorAll('[role=tab]')].filter(tab=>tab.querySelector('.workspace-task-light')).map(tab=>{const lamp=tab.querySelector('.workspace-task-light'),r=lamp.getBoundingClientRect(),t=tab.getBoundingClientRect(),s=getComputedStyle(lamp);return {inside:r.left>=t.left&&r.right<=t.right&&r.top>=t.top&&r.bottom<=t.bottom,size:r.width,animation:s.animationName,duration:s.animationDuration,color:s.backgroundColor,description:!!document.getElementById(tab.getAttribute('aria-describedby'))}})})''')
                            self.assertAlmostEqual(geometry['bottom'],height,delta=1)
                            self.assertFalse(geometry['overflow'])
                            self.assertTrue(all(lamp['inside'] and lamp['size']==6 and lamp['description'] and lamp['animation']=='workspace-task-breathe' for lamp in geometry['lamps']),geometry)
                            checks.append([theme,width,geometry])
                            if width in (1600,320):page.screenshot(path=str(out/f'{theme}-{width}.png'))
                    page.emulate_media(reduced_motion='reduce')
                    self.assertEqual(page.locator('.workspace-task-light').first.evaluate('(lamp)=>getComputedStyle(lamp).animationName'),'none')
                    page.set_viewport_size({'width':1600,'height':1000})
                    with self.archive.connect() as c:
                        c.execute("UPDATE metadata_jobs SET status='completed'")
                        c.execute("UPDATE raw_session_work SET status='completed'")
                    self.sku.tasks_paused=True;self.analysis.paused=True
                    page.wait_for_function("()=>document.getElementById('sku-open').dataset.taskState==='paused'&&document.getElementById('asset-analysis-open').dataset.taskState==='paused'",timeout=8000)
                    with self.sku.connect() as c:
                        c.execute("UPDATE jobs SET status='completed'");c.execute('UPDATE archive_events SET delivered=1')
                    with self.analysis.connect() as c:c.execute("UPDATE jobs SET status='completed'")
                    with self.archive.connect() as c:c.execute("UPDATE metadata_jobs SET status='completed'")
                    with self.previews.connect() as c:c.execute('DELETE FROM pending')
                    component.state['status']='idle'
                    with self.archive.connect() as c:c.execute("UPDATE raw_session_work SET status='completed'")
                    page.wait_for_function("()=>document.querySelectorAll('.workspace-task-light[data-state=idle]').length===6",timeout=8000)
                    # 真正的慢操作在共享 api 的 try/finally 内跟踪，异常也必须熄灯。
                    def slow_scan(root_id):
                        release.wait(8);raise ValueError('测试扫描失败')
                    with patch.object(self.catalog,'scan',side_effect=slow_scan):
                        page.evaluate("()=>{window.scanResult=FramekeepIO.api('/api/catalog/scan',{root_id:'test'}).catch(error=>error.message);return true}")
                        page.wait_for_function("()=>document.getElementById('asset-archive-open').dataset.taskState==='busy'")
                        page.locator('#app-settings-open').click()
                        self.assertEqual(page.locator('#asset-archive-open').get_attribute('data-task-state'),'busy')
                        release.set();self.assertEqual(page.evaluate('()=>window.scanResult'),'测试扫描失败')
                        page.wait_for_function("()=>document.getElementById('asset-archive-open').dataset.taskState==='idle'")
                    page.route('**/api/task-status',lambda route:route.abort())
                    page.wait_for_function("()=>document.querySelectorAll('.workspace-task-light[data-state=unknown]').length===6",timeout=8000)
                    page.unroute('**/api/task-status')
                    page.wait_for_function("()=>document.querySelectorAll('.workspace-task-light[data-state=idle]').length===6",timeout=8000)
                    self.assertFalse(errors,errors)
                    (out/'result.json').write_text(json.dumps({'passed':True,'checks':checks,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf8')
                finally:browser.close()
        finally:
            release.set();http.shutdown();http.server_close()
            for item in reversed(patches):item.stop()


if __name__=='__main__':unittest.main()
