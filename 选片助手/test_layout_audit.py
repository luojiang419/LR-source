"""正式六页、长内容和主要弹窗的四主题布局审查；只使用隔离数据。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import copy
import io
import json
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as E
from zipfile import ZipFile

from PIL import Image
from playwright.sync_api import sync_playwright
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from asset_analysis import AssetAnalysis
from catalog_store import Store
from model_providers import Providers
from sku_service import SkuService
from vision_scheduler import VisionScheduler
from test_sku_workflow import fixture_book,NS
import media_engine as media

ROOT=Path(__file__).resolve().parents[1]
TABS={'selection':'selection-tab','progress':'sku-open','archive':'asset-archive-open','database':'sku-database-open','analysis':'asset-analysis-open','settings':'app-settings-open'}


def long_workbook():
    output=io.BytesIO()
    with ZipFile(io.BytesIO(fixture_book())) as source,ZipFile(output,'w') as target:
        for name in source.namelist():
            raw=source.read(name)
            if name.startswith('xl/worksheets/'):
                tree=E.fromstring(raw);data=tree.find('{'+NS+'}sheetData');template=list(data)[-1]
                for i in range(4,85):
                    row=copy.deepcopy(template);row.set('r',str(i))
                    for cell in row:cell.set('r',cell.get('r')[0]+str(i))
                    row.find('.//{'+NS+'}t').text='SKU-LAYOUT-'+str(i);data.append(row)
                tree.find('{'+NS+'}dimension').set('ref','A1:C84');raw=E.tostring(tree)
            target.writestr(name,raw)
    return output.getvalue()


class LayoutAuditTests(unittest.TestCase):
    def test_all_pages_dialogs_and_scroll_regions(self):
        import server
        packaged=os.environ.get('FRAMEKEEP_LAYOUT_PACKAGED')
        out=ROOT/('build/layout-audit-packaged' if packaged else 'build/layout-audit');out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos'/'SKU-LAYOUT-0001';photos.mkdir(parents=True)
            store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos);store.activate(sid)
            for i in range(65):
                photo=photos/(f'{i:03}-'+'long-product-description-'*2+'.jpg')
                Image.new('RGB',(180,240),(50+i*2,100,160)).save(photo)
                store.append_live(sid,[media.capture_record(photos,photo,None,{})])
            providers=Providers(store.data)
            for i in range(15):providers.save({'name':'较长的模型配置名称 '+str(i),'category':'multimodal','protocol':'chat','model':'test-model-'+str(i),'url':'https://example.invalid/v1'})
            archive=AssetArchive(store,autostart=False);catalog=AssetCatalog(store.data);catalog.add_root(str(base/'photos'))
            scheduler=VisionScheduler();sku=SkuService(store,providers,autostart=False,archive=archive,scheduler=scheduler)
            pid=sku.import_file('布局测试.xlsx',long_workbook(),sid)['project']['id'];sku.configure(pid,sid,'模特图')
            analysis=AssetAnalysis(archive,providers,scheduler,autostart=False)
            for item in catalog.items(limit=120)['rows']:catalog.assign(item['id'],'SKU-LAYOUT-0001')
            with sku.connect() as c:
                for i,record in enumerate(store.records(sid)):
                    c.execute('INSERT INTO parse_jobs(id,session_id,capture_id,status,created,result) VALUES (?,?,?,?,?,?)',(f'layout-ocr-{i}',sid,record['id'],'completed',i,json.dumps({'codes':['SKU-LAYOUT-0001']})))
            schema=analysis.save_schema('布局审查','只返回 color。',{'type':'object','properties':{'color':{'type':'string'}}})['schemas'][0]
            assets=[archive.ensure(sid,r['id']) for r in store.records(sid)[:8]]
            analysis.create_batch(assets,schema['id'],schema['version'],providers.public()['providers'][0]['id'])
            patches=[patch.object(server,k,v) for k,v in dict(STORE=store,DATA=store.data,MODELS=providers,ARCHIVE=archive,CATALOG=catalog,SKU=sku,ANALYSIS=analysis,WATCH=None,MOUSE=None,RAWLINK=None,FILESEARCH=None,UPDATES=None).items()]
            for item in patches:item.start()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
            url=f'http://127.0.0.1:{http.server_port}';process=None;token=secrets.token_urlsafe(24)
            if packaged:
                analysis.control(paused=True)
                app=Path(packaged).resolve()
                with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                env=os.environ.copy();env.update(FRAMEKEEP_DATA=str(store.data),FRAMEKEEP_APP_ROOT=str(app),FRAMEKEEP_DESKTOP_TOKEN=token,FRAMEKEEP_CONFIG=str(base/'no-api-config.md'))
                process=subprocess.Popen([str(app/'engine/framekeep-engine.exe'),'--no-browser','--port',str(port)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));url=f'http://127.0.0.1:{port}'
                for _ in range(100):
                    try:urllib.request.urlopen(url+'/api/health',timeout=.5).close();break
                    except OSError:time.sleep(.1)
            findings=[];checks=[];errors=[]
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1366,'height':768},reduced_motion='reduce');page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(url,wait_until='networkidle')
                        for theme in ('dark','light','pure-white','hardcore'):
                            page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                            for width,height in ((2560,1440),(1920,1080),(1366,768),(1000,680),(1000,480),(375,812)):
                                page.set_viewport_size({'width':width,'height':height})
                                for name,tab in TABS.items():
                                    page.locator('#'+tab).click()
                                    if name=='archive':page.wait_for_selector('#catalog-grid .catalog-card')
                                    if name=='database':
                                        page.wait_for_selector('[data-db-sku]');page.locator('[data-db-sku]').first.click();page.wait_for_selector('#sku-database-grid .catalog-card')
                                    box=page.evaluate('''()=>{const panel=document.querySelector('.workbench>[role=tabpanel]:not(.hidden)'),r=panel.getBoundingClientRect(),nav=document.querySelector('.workspace-bottom-bar').getBoundingClientRect();const children=[...panel.children].filter(e=>getComputedStyle(e).display!=='none').map(e=>{const r=e.getBoundingClientRect(),c=getComputedStyle(e);return {id:e.id||e.className,x:r.x,right:r.right,y:r.y,bottom:r.bottom,width:r.width,height:r.height,scrollHeight:e.scrollHeight,clientHeight:e.clientHeight,overflow:c.overflowY}});return {page:{x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width,height:r.height,overflow:getComputedStyle(panel).overflowY},nav:{top:nav.top,bottom:nav.bottom},children,overflow:document.documentElement.scrollWidth>innerWidth}}''')
                                    tag=[theme,width,height,name];checks.append({'case':tag,**box})
                                    if abs(box['page']['width']-width)>2:findings.append([tag,'page-width',box])
                                    if abs(box['nav']['bottom']-height)>2 or box['page']['bottom']>box['nav']['top']+2:findings.append([tag,'bottom-navigation',box])
                                    if box['page']['height']<100 or box['overflow']:findings.append([tag,'viewport',box])
                                    for child in box['children']:
                                        if child['bottom']>box['page']['bottom']+2 and child['overflow'] not in ('auto','scroll') and box['page']['overflow'] not in ('auto','scroll'):
                                            findings.append([tag,'child-outside-page',child])
                                    if name=='analysis' and width>=1000:
                                        progress=page.locator('#task-progress-panel').bounding_box()
                                        if progress['y']+progress['height']>box['page']['bottom']+1:findings.append([tag,'progress-panel-clipped',progress])
                                    if name in ('archive','database'):
                                        grid=page.locator('#catalog-grid' if name=='archive' else '#sku-database-grid').evaluate('''e=>{const cards=[...e.children],columns=getComputedStyle(e).gridTemplateColumns.split(' ').length,first=cards[0].getBoundingClientRect(),next=cards[columns]?.getBoundingClientRect(),thumb=cards[0].querySelector('.catalog-thumb').getBoundingClientRect(),caption=cards[0].querySelector('.catalog-card-name').getBoundingClientRect();return {firstBottom:first.bottom,nextTop:next?.top,thumbBottom:thumb.bottom,captionBottom:caption.bottom,scrollHeight:e.scrollHeight,clientHeight:e.clientHeight,overflow:getComputedStyle(e).overflowY}}''')
                                        if grid['nextTop']<grid['thumbBottom'] or grid['captionBottom']>grid['firstBottom']+1:findings.append([tag,'overlapping-thumbnails',grid])
                                        if grid['scrollHeight']>grid['clientHeight'] and grid['overflow'] not in ('auto','scroll'):findings.append([tag,'gallery-not-scrollable',grid])
                                        if width>=1000:
                                            selector='#catalog-grid' if name=='archive' else '#sku-database-grid'
                                            visible=page.locator(selector).evaluate('''e=>{e.scrollTop=e.scrollHeight;const last=e.lastElementChild.getBoundingClientRect(),parent=e.getBoundingClientRect();const result=last.top<parent.bottom&&last.bottom>parent.top;e.scrollTop=0;return result}''')
                                            if not visible:findings.append([tag,'last-photo-unreachable'])
                                    if width==1366 or (theme=='dark' and width in (1000,375)):
                                        page.screenshot(path=str(out/f'{theme}-{width}-{height}-{name}.png'))
                        page.set_viewport_size({'width':1000,'height':480});page.evaluate("FramekeepTheme.set('dark')");page.locator('#app-settings-open').click()
                        for section in ('models','remote','files','streams'):
                            page.locator(f'[data-settings-section="{section}"]').click();target=page.locator('#settings-'+section)
                            r=target.bounding_box();viewport=page.locator('.app-settings-grid').bounding_box()
                            if r['y']>=viewport['y']+viewport['height'] or r['y']+r['height']<=viewport['y']:findings.append([section,'settings-category-unreachable',r])
                        page.locator('#asset-close-stream').scroll_into_view_if_needed()
                        action=page.locator('#asset-close-stream').bounding_box()
                        if action['y']+action['height']>page.locator('.workspace-bottom-bar').bounding_box()['y']:findings.append(['settings','last-action-under-navigation',action])
                        for opener,dialog in (('#assistant-settings','#assistant-settings-dialog'),('#mouse-remote','#mouse-dialog')):
                            page.locator(opener).scroll_into_view_if_needed();page.locator(opener).click()
                            for width,height in ((1366,768),(1000,480),(375,812)):
                                page.set_viewport_size({'width':width,'height':height})
                                d=page.locator(dialog).evaluate('''e=>{const r=e.getBoundingClientRect(),c=getComputedStyle(e);return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,height:r.height,scrollHeight:e.scrollHeight,clientHeight:e.clientHeight,overflow:c.overflowY}}''')
                                checks.append({'case':[dialog,width,height],**d})
                                if d['x']<0 or d['y']<0 or d['right']>width+1 or d['bottom']>height+1:findings.append([[dialog,width,height],'dialog-outside-viewport',d])
                                page.screenshot(path=str(out/f'dialog-{dialog[1:]}-{width}-{height}.png'))
                            if dialog=='#assistant-settings-dialog':
                                page.set_viewport_size({'width':1000,'height':480});page.locator('#model-add').click();page.locator('#model-save').scroll_into_view_if_needed()
                                save=page.locator('#model-save').bounding_box()
                                if save['y']+save['height']>480:findings.append(['model-editor','save-button-unreachable',save])
                                page.screenshot(path=str(out/'model-editor-1000-480.png'));page.locator('#model-cancel').click()
                            page.locator(dialog).evaluate('e=>e.close()');page.set_viewport_size({'width':1000,'height':480})
                        page.locator('#selection-tab').click()
                        for opener,dialog in (('#folder-watch','#watch-dialog'),('#open-folder','#folder-dialog'),('#send-lr','#bridge-dialog')):
                            page.locator(opener).click()
                            d=page.locator(dialog).bounding_box();checks.append({'case':[dialog,1000,480],**d})
                            if d['x']<0 or d['y']<0 or d['x']+d['width']>1001 or d['y']+d['height']>481:findings.append([dialog,'dialog-outside-viewport',d])
                            page.locator(dialog+' .dialog-actions button').last.scroll_into_view_if_needed()
                            button=page.locator(dialog+' .dialog-actions button').last.bounding_box()
                            if button['y']+button['height']>480:findings.append([dialog,'footer-action-unreachable',button])
                            page.screenshot(path=str(out/f'{dialog[1:]}-1000-480.png'));page.locator(dialog).evaluate('e=>e.close()')
                    finally:browser.close()
            finally:
                if process:
                    try:urllib.request.urlopen(urllib.request.Request(url+'/api/desktop/shutdown',b'{}',headers={'X-Framekeep-Desktop':token}),timeout=5).close();process.wait(timeout=15)
                    except Exception:process.terminate();process.wait(timeout=10)
                http.shutdown();http.server_close();analysis.close();sku.close();archive.close()
                for item in reversed(patches):item.stop()
            result={'packaged':bool(packaged),'checks':checks,'findings':findings,'errors':errors};(out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
            print('layout checks',len(checks),'findings',len(findings),'errors',errors,flush=True)
            self.assertFalse(findings,findings[:8]);self.assertFalse(errors,errors)


if __name__=='__main__':unittest.main()
