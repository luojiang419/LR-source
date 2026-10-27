"""局域网工作台：实际 HTTP、共享数据联动、会话与只读边界。"""
import base64
from contextlib import closing
import http.cookiejar
import io
import json
from pathlib import Path
import socket
import sqlite3
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request,build_opener,HTTPCookieProcessor,ProxyHandler

from PIL import Image
from playwright.sync_api import sync_playwright

import test_asset_retrieval_browser as fixture
from lan_workspace import LanWorkspace


def free_port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


class LanWorkspaceTests(unittest.TestCase):
    def setUp(self):
        fixture.RetrievalBrowserTests.setUp(self)
        with self.sku.connect() as c:
            c.execute('INSERT INTO projects(id,name,metadata,scope,enabled,created) VALUES(?,?,?,?,?,?)',
                      ('plan','秋季拍摄计划',json.dumps({'sheets':[{'name':'模特图','images':[]},{'name':'细节图','images':[]}],'progress_version':1}),'模特图',0,1))
            c.execute("INSERT OR REPLACE INTO settings VALUES('active','plan')")
            c.executemany('''INSERT INTO sku_rows(id,project,sheet,excel_row,sku,normalized,baseline,status,completed_at)
                VALUES(?,'plan',?,?,?,?,?,?,?)''',[
                    ('row1','模特图',2,'LC1234','LC1234','未拍摄','已拍摄','2026-10-26T10:30:00+08:00'),
                    ('row2','模特图',3,'LC1234','LC1234','未拍摄','未拍摄',''),
                    ('row3','模特图',4,'LC5678','LC5678','未拍摄','未拍摄',''),
                    ('row4','细节图',2,'LC5678','LC5678','未拍摄','已拍摄','2026-10-26T11:00:00+08:00')])
        self.lan=LanWorkspace(self.catalog,self.sku,self.service,fixture.ROOT/'演示方案',autostart=False)
        self.port=free_port();self.lan.configure({'enabled':True,'port':self.port});self.lan_url=f'http://127.0.0.1:{self.port}'
        self.jar=http.cookiejar.CookieJar();self.client=build_opener(ProxyHandler({}),HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.lan.close();fixture.RetrievalBrowserTests.tearDown(self)

    def call(self,path,body=None,headers=None,client=None):
        request=Request(self.lan_url+quote(path,safe="/?=&:%"),None if body is None else json.dumps(body).encode('utf8'),
                        {'Content-Type':'application/json',**(headers or {})})
        try:response=(client or self.client).open(request,timeout=10)
        except HTTPError as error:response=error
        with closing(response):
            raw=response.read();kind=response.headers.get('Content-Type','')
            return response.code,json.loads(raw) if 'application/json' in kind else raw,dict(response.headers)

    def login(self):
        status,_,headers=self.call('/api/login',{'code':self.lan.config['access_code']})
        self.assertEqual(status,200);return headers

    def development_access(self):
        self.lan.close()
        self.lan=LanWorkspace(self.catalog,self.sku,self.service,fixture.ROOT/'演示方案',require_access_code=False)

    def test_development_without_code_keeps_readonly_and_default_authentication(self):
        self.development_access()
        self.assertEqual(len(self.jar),0)
        status,state,headers=self.call('/api/state')
        self.assertEqual(status,200);self.assertFalse(state['login_required'])
        self.assertNotIn('Set-Cookie',headers);self.assertEqual(self.lan.state()['access_code'],'')
        for route in ('/api/changes','/api/progress?view=all','/api/search?q=LC1234','/api/archive'):
            self.assertEqual(self.call(route)[0],200,route)
        self.assertEqual(self.call('/api/image',{'item_id':self.rows[0]['id']})[0],200)
        self.assertEqual(self.call('/api/login',{'code':'not-needed'})[0],200)
        self.assertEqual(len(self.jar),0);self.assertEqual(self.lan.sessions,{})
        for route in ('/api/sku/import','/api/catalog/assign','/api/providers','/api/desktop/shutdown'):
            self.assertEqual(self.call(route,{})[0],405,route)
        self.assertEqual(self.call('/api/state',headers={'Origin':'https://evil.invalid'})[0],401)
        self.assertEqual(self.call('/api/state',headers={'Host':'evil.invalid:'+str(self.port)})[0],401)
        self.lan.close()
        self.lan=LanWorkspace(self.catalog,self.sku,self.service,fixture.ROOT/'演示方案')
        self.assertTrue(self.lan.require_access_code)
        self.assertEqual(self.call('/api/state')[0],401)
        self.login();self.assertEqual(self.call('/api/state')[0],200)

    def test_development_browser_enters_and_reloads_without_login_or_cookie(self):
        import server
        from unittest.mock import patch
        self.development_access()
        with patch.object(server,'LANWORKSPACE',self.lan),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                context=browser.new_context();page=context.new_page();errors=[];logins=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda request:logins.append(request.url) if '/api/login' in request.url else None)
                for reload in (False,True):
                    if reload:page.reload(wait_until='domcontentloaded')
                    else:page.goto(self.lan_url,wait_until='domcontentloaded')
                    page.locator('#workspace').wait_for(state='visible')
                    self.assertTrue(page.locator('#login').is_hidden())
                    self.assertTrue(page.locator('#logout').is_hidden())
                    page.locator('#lan-settings-trigger').focus();page.keyboard.press('ArrowUp')
                    self.assertTrue(page.locator('#theme-cycle').evaluate('(node)=>node===document.activeElement'))
                    page.keyboard.press('Escape')
                    page.locator('#tab-search').click();page.wait_for_selector('#search-grid .lan-card')
                    page.locator('#tab-archive').click();page.wait_for_selector('#archive-grid .lan-card')
                self.assertEqual(logins,[]);self.assertEqual(context.cookies(),[])
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click()
                page.locator('#lan-workspace-open').click();page.locator('#lan-sharing-dialog').wait_for(state='visible')
                for control in ('lan-code','lan-reveal','lan-copy-code','lan-rotate'):
                    self.assertTrue(page.locator('#'+control).is_hidden(),control)
                self.assertIn('开发阶段无需访问码',page.locator('#lan-sharing-dialog').inner_text())
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_login_sessions_rotation_and_restart(self):
        self.assertEqual(self.call('/api/state')[0],401)
        self.assertEqual(self.call('/api/login',{'code':'wrong'})[0],401)
        headers=self.login();self.assertIn('HttpOnly',headers['Set-Cookie']);self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        status,state,_=self.call('/api/state');self.assertEqual(status,200);self.assertEqual(len(state['roots']),1)
        serialized=json.dumps(state);self.assertNotIn('access_code',serialized);self.assertNotIn(str(self.base),serialized)
        code=self.lan.config['access_code'];self.lan.configure({'rotate':True});self.assertNotEqual(code,self.lan.config['access_code'])
        self.assertEqual(self.call('/api/state')[0],401)
        self.login();self.assertEqual(self.call('/api/logout',{})[0],200);self.assertEqual(self.call('/api/state')[0],401)
        self.lan.close();self.lan=LanWorkspace(self.catalog,self.sku,self.service,fixture.ROOT/'演示方案')
        self.assertTrue(self.lan.state()['enabled']);self.login();self.assertEqual(self.call('/api/state')[0],200)

    def test_invalid_sharing_config_does_not_block_desktop_startup(self):
        self.lan.close();self.lan.path.write_text('{invalid',encoding='utf8')
        self.lan=LanWorkspace(self.catalog,self.sku,self.service,fixture.ROOT/'演示方案')
        self.assertFalse(self.lan.state()['enabled']);self.assertIn('配置不可读',self.lan.state()['error'])
        self.lan.configure({'enabled':True,'port':self.port});self.login();self.assertEqual(self.call('/api/state')[0],200)

    def test_progress_deduplication_and_desktop_changes_visible(self):
        self.login();_,before,_=self.call('/api/progress?project=plan&sheet=模特图')
        self.assertEqual(before['counts'],{'total':2,'shot':1,'pending':1});self.assertEqual(len(before['rows']),2)
        revision=self.call('/api/changes')[1]['revision']
        with self.sku.connect() as c:c.execute("UPDATE sku_rows SET status='已拍摄',completed_at='2026-10-26T12:00:00+08:00' WHERE id='row3'")
        self.assertNotEqual(self.call('/api/changes')[1]['revision'],revision)
        _,after,_=self.call('/api/progress?project=plan&sheet=模特图');self.assertEqual(after['counts']['shot'],2)
        self.assertEqual(self.call('/api/progress?project=plan&status=pending')[1]['rows'],[])
        self.assertEqual(self.call('/api/progress/image?row=row1')[2]['Content-Type'],'image/jpeg')

    def test_progress_views_keep_source_rows_counts_and_desktop_context(self):
        self.login();before=self.sku.project()
        _,sheet,_=self.call('/api/progress?project=plan&sheet=模特图&view=sheet')
        self.assertEqual(sheet['counts'],{'total':2,'shot':1,'pending':1})
        self.assertEqual([r['number'] for r in sheet['rows']],[2,3,4])
        self.assertEqual(sheet['all_total'],2);self.assertEqual(sheet['common_total'],1)
        _,all_rows,_=self.call('/api/progress?project=plan&view=all')
        self.assertEqual(all_rows['counts'],{'total':2,'shot':1,'pending':1})
        _,common,_=self.call('/api/progress?project=plan&view=common')
        self.assertEqual(common['counts'],{'total':1,'shot':0,'pending':1})
        self.assertEqual(common['rows'][0]['sku'],'LC5678')
        self.assertEqual({r['sheet'] for r in common['rows'][0]['sources']},{'模特图','细节图'})
        _,pending,_=self.call('/api/progress?project=plan&view=sheet&sheet=模特图&status=pending&q=LC1234')
        self.assertEqual([r['id'] for r in pending['rows']],['row2'])
        # 筛选不改变当前工作表统计，与软件端一致。
        self.assertEqual(pending['counts'],sheet['counts'])
        _,page1,_=self.call('/api/progress?project=plan&view=sheet&sheet=模特图&limit=2')
        _,page2,_=self.call('/api/progress?project=plan&view=sheet&sheet=模特图&limit=2&offset=2')
        self.assertTrue(page1['has_more']);self.assertFalse(page2['has_more'])
        self.assertFalse({r['id'] for r in page1['rows']}&{r['id'] for r in page2['rows']})
        self.assertEqual(self.sku.project(),before)
        serialized=json.dumps(sheet,ensure_ascii=False)
        self.assertNotIn(str(self.base),serialized);self.assertNotIn('session_id',serialized)
        for query in ('view=unknown','view=sheet&sheet=不存在','view=all&status=unknown'):
            self.assertEqual(self.call('/api/progress?project=plan&'+query)[0],400)
        for route in ('/api/sku/import','/api/sku/configure','/api/sku/header','/api/sku/cover'):
            self.assertEqual(self.call(route,{})[0],405)

    def test_progress_sidebar_layout_readonly_images_and_source_columns(self):
        import test_sku_workflow as workbook
        # 使用真实工作簿导入的列和图片，覆盖软件端资料在 Web 的还原。
        pid=self.sku.import_file('拍摄清单.xlsx',workbook.fixture_book())['project']['id']
        self.sku.configure(pid,None,'模特图');before=self.sku.project()
        second=self.sku.import_file('另一份拍摄清单.xlsx',workbook.fixture_book())['project']['id']
        self.sku.activate(pid)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1680,'height':1050},reduced_motion='reduce')
                errors=[];writes=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda request:writes.append(request.url) if request.method=='POST' and request.url.endswith(('/import','/configure','/activate','/header','/cover')) else None)
                page.goto(self.lan_url,wait_until='domcontentloaded');page.locator('#access-code').fill(self.lan.config['access_code']);page.locator('#login-submit').click()
                page.wait_for_selector('#workspace:not([hidden])');page.wait_for_selector('#progress-sheets [data-sheet="模特图"]')
                page.locator('#progress-sheets [data-sheet="模特图"]').click()
                page.wait_for_function('()=>document.getElementById("progress-head").textContent.includes("拍摄完成时间")')
                self.assertEqual(page.locator('#progress-head th').all_text_contents(),['序号','原表行','款号','图片','拍摄状态','款号图片','拍摄完成时间','备注'])
                self.assertEqual(page.locator('#progress-rows .lan-sequence').all_text_contents(),['1','2'])
                self.assertEqual(page.locator('#progress-rows .lan-original-row').all_text_contents(),['2','3'])
                self.assertEqual(page.locator('#panel-progress input[type=file],#panel-progress [contenteditable=true]').count(),0)
                self.assertEqual(page.locator('#panel-progress button').filter(has_text='导入').count(),0)
                self.assertEqual(page.locator('#progress-rows [data-header-key],#progress-rows [data-sku-cover]').count(),0)
                page.locator('#progress-rows [data-progress-image]').first.click()
                page.wait_for_selector('#progress-image[open]')
                page.wait_for_function('()=>document.getElementById("progress-image-content").naturalWidth>0')
                page.keyboard.press('Escape');self.assertFalse(page.locator('#progress-image').is_visible())
                page.locator('#progress-query').fill(workbook.CODE)
                page.wait_for_function('()=>document.querySelectorAll("#progress-rows tr[data-progress-row]").length===1')
                self.assertEqual(page.locator('#progress-rows .lan-sequence').inner_text(),'1')
                page.locator('#progress-sheets [data-view=common]').click()
                page.wait_for_function('()=>document.querySelector("#progress-sheets [data-view=common]").getAttribute("aria-selected")==="true"')
                page.locator('#progress-rows [data-view-sheet="细节图"]').click()
                page.wait_for_function('()=>document.getElementById("sheet").value==="细节图"')
                page.wait_for_timeout(3200)
                self.assertEqual(self.sku.project(),before)
                page.locator('#project').select_option(second)
                page.wait_for_function('()=>document.getElementById("progress-project-info").textContent.includes("另一份")')
                self.assertEqual(self.sku.project(),before)
                page.locator('#project').select_option(pid)
                page.locator('#progress-query').fill('')
                page.locator('#sheet').select_option('模特图')
                page.wait_for_function('()=>document.querySelectorAll("#progress-rows tr[data-progress-row]").length===2')
                out=fixture.ROOT/'build/lan-progress-layout';out.mkdir(parents=True,exist_ok=True)
                for theme in ('dark','light','pure-white','hardcore'):
                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                    for width in (1680,1100,760,390,340):
                        page.set_viewport_size({'width':width,'height':1050})
                        if width<=760:page.locator('#progress-sidebar-toggle').click()
                        boxes=page.evaluate('''()=>{const box=id=>{const r=document.querySelector(id).getBoundingClientRect();return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,height:r.height}},table=document.querySelector('.lan-table-wrap');return {list:box('.lan-progress-list'),side:box('#progress-sidebar'),search:box('.lan-progress-search'),tabs:box('#progress-sheets'),footer:box('.lan-footer'),overflow:document.documentElement.scrollWidth-innerWidth,tableHeight:table.clientHeight,tableScroll:table.scrollHeight}}''')
                        self.assertLessEqual(boxes['overflow'],1,(theme,width,boxes))
                        self.assertTrue(page.locator('#progress-sidebar').is_visible())
                        if width>760:
                            self.assertLess(boxes['list']['right'],boxes['side']['x'])
                            self.assertAlmostEqual(boxes['list']['y'],boxes['side']['y'],delta=1)
                            self.assertGreater(boxes['list']['height'],700)
                        self.assertLessEqual(boxes['list']['bottom'],boxes['footer']['y']+1)
                        self.assertLess(boxes['search']['bottom'],boxes['tabs']['y']+1)
                        self.assertLess(boxes['tableHeight'],1000)
                        if width in (1680,390):page.screenshot(path=str(out/f'{theme}-{width}.png'))
                        if width<=760:
                            page.keyboard.press('Escape');self.assertFalse(page.locator('#progress-sidebar').is_visible())
                    page.set_viewport_size({'width':1680,'height':1050})
                self.assertFalse(writes,writes);self.assertFalse(errors,errors)
                (out/'result.json').write_text(json.dumps({'page_errors':errors,'mutation_requests':writes,'desktop_context_preserved':True,'themes':4,'widths':[1680,1100,760,390,340]},ensure_ascii=False,indent=2),encoding='utf-8')
            finally:browser.close()

    def test_progress_completion_image_uses_label_evidence_and_rejects_missing(self):
        import media_engine as media
        label=next(Path(row['path']) for row in self.rows if '蓝色' in row['name'])
        sid=self.store.ensure_live_session(label.parent)
        record=media.capture_record(label.parent,label,None,{})
        self.store.append_live(sid,[record])
        with self.sku.connect() as c:
            c.execute('''INSERT INTO jobs(id,project,session_id,capture_id,scope,status,captured_at,result,targets)
                VALUES('label-proof','plan',?,?,'模特图','confirmed','2026-10-26T10:30:00+08:00','{}','[]')''',(sid,record['id']))
            c.execute("UPDATE sku_rows SET completion_job='label-proof' WHERE id='row1'")
        self.login()
        status,payload,headers=self.call('/api/progress/image?row=row1&kind=completion')
        self.assertEqual(status,200);self.assertEqual(headers['Content-Type'],'image/jpeg')
        with Image.open(io.BytesIO(payload)) as image:
            red,green,blue=image.convert('RGB').getpixel((10,10));self.assertGreater(blue,red+100)
        _,data,_=self.call('/api/progress?project=plan&view=sheet&sheet=模特图')
        row=next(row for row in data['rows'] if row['id']=='row1')
        self.assertTrue(row['completion']);self.assertEqual(row['completed_at'],'2026-10-26T10:30:00+08:00')
        for suffix in ('row=row2&kind=completion','row=missing&kind=completion','row=row1&kind=unknown','row=row1&kind=source:999'):
            self.assertEqual(self.call('/api/progress/image?'+suffix)[0],400)

    def test_search_archive_details_preview_and_same_database(self):
        self.login();_,result,_=self.call('/api/search?q=红色');self.assertEqual(len(result['rows']),2)
        item=result['rows'][0];self.assertNotIn('path',item);self.assertNotIn('fingerprint',item)
        _,details,_=self.call('/api/details?item='+item['id']);self.assertTrue(details['parsed']);self.assertEqual(details['frames'][0]['result']['attributes']['color'],'红色')
        status,payload,headers=self.call('/api/preview?item='+item['id']+'&size=4096');self.assertEqual(status,200)
        with Image.open(io.BytesIO(payload)) as image:self.assertLessEqual(max(image.size),2048)
        self.assertEqual(headers['Content-Type'],'image/jpeg')
        self.catalog.assign(item['id'],'LC9999');self.assertEqual(self.call('/api/search?q=LC9999')[1]['rows'][0]['id'],item['id'])
        self.assertEqual(len(self.call('/api/archive')[1]['rows']),3)

    def test_image_search_and_content_removal(self):
        self.login();item=self.rows[0]
        status,result,_=self.call('/api/image',{'item_id':item['id']});self.assertEqual(status,200);self.assertTrue(result['rows'])
        status,strict,_=self.call('/api/image',{'item_id':item['id'],'threshold':.99,'limit':100})
        self.assertEqual(status,200);self.assertTrue(all(row['score']>=.99 for row in strict['rows']))
        self.assertEqual(self.call('/api/image',{'item_id':item['id'],'threshold':-1})[0],400)
        encoded=base64.b64encode(Path(item['path']).read_bytes()).decode()
        self.assertEqual(self.call('/api/image',{'image':encoded})[0],200)
        self.catalog.remove(item['root'])
        self.assertEqual(self.call('/api/search?q=红色')[1]['rows'],[])
        self.assertEqual(self.call('/api/preview?item='+item['id'])[0],400)

    def test_local_admin_and_files_never_exposed(self):
        self.login()
        for route in ('/api/catalog/assign','/api/lan-workspace','/api/providers','/api/intelligence/control','/api/desktop/shutdown'):
            self.assertEqual(self.call(route,{})[0],405)
        for route in ('/api/state?file=../model-providers.json','/api/search?q=LC1234'):
            self.assertEqual(self.call(route,headers={'Origin':'https://evil.invalid'})[0],401)
        for route in ('/assets/../lan-workspace.json','/assets/desktop.js','/api/providers','/api/preview?item=../../file'):
            self.assertNotEqual(self.call(route)[0],200)
        self.assertEqual(self.call('/api/state',headers={'Host':'evil.invalid:'+str(self.port)})[0],401)
        self.assertEqual(self.call('/api/state',headers={'Cookie':'malformed cookie'})[0],401)

    def test_folder_navigation_pagination_and_filters_bound_cursor(self):
        root=self.rows[0]['root'];photos=Path(self.rows[0]['path']).parent;folder=photos/'子目录';folder.mkdir()
        for number in range(64):Image.new('RGB',(16,16),'green').save(folder/f'{number}.jpg')
        self.catalog.scan(root);self.login()
        _,first,_=self.call('/api/archive?root='+root)
        self.assertIn('子目录',[item['name'] for item in first['folders']]);self.assertTrue(first['has_more'])
        _,second,_=self.call('/api/archive?root='+root+'&cursor='+first['next_cursor'])
        self.assertFalse({item['id'] for item in first['rows']} & {item['id'] for item in second['rows']})
        self.assertEqual(len(first['rows'])+len(second['rows']),67)
        self.assertEqual(self.call('/api/archive?root='+root+'&folder=子目录&cursor='+first['next_cursor'])[0],400)
        self.assertEqual(len(self.call('/api/archive?root='+root+'&folder=子目录')[1]['rows']),60)

    def test_private_interface_access_and_local_admin_settings(self):
        import server
        from unittest.mock import patch
        addresses=[address for address in self.lan.state()['addresses'] if '127.0.0.1' not in address]
        if addresses:
            previous=self.lan_url;self.lan_url=addresses[0].rstrip('/')
            try:self.login();self.assertEqual(self.call('/api/state')[0],200)
            finally:self.lan_url=previous
        with patch.object(server,'LANWORKSPACE',self.lan):
            with closing(build_opener(ProxyHandler({})).open(self.url+'/api/lan-workspace')) as response:
                state=json.load(response);self.assertTrue(state['enabled']);self.assertIn('access_code',state)
            request=Request(self.url+'/api/lan-workspace',json.dumps({'enabled':False}).encode(),{'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as caught:build_opener(ProxyHandler({})).open(request)
            self.assertEqual(caught.exception.code,403);caught.exception.close();self.assertTrue(self.lan.state()['enabled'])

    def test_desktop_sharing_controls(self):
        import server
        from unittest.mock import patch
        with patch.object(server,'LANWORKSPACE',self.lan),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded')
                page.locator('#asset-retrieval-open').click();page.locator('#lan-workspace-open').click()
                page.wait_for_selector('#lan-sharing-dialog[open]');self.assertIn(self.lan_url,page.locator('#lan-addresses').inner_text())
                page.locator('#lan-enabled').uncheck();page.locator('#lan-save').click()
                page.wait_for_function('()=>document.getElementById("lan-sharing-message").textContent.includes("已关闭")')
                self.assertFalse(self.lan.state()['enabled'])
            finally:browser.close()

    def test_browser_live_progress_three_tabs_and_mobile(self):
        out=fixture.ROOT/'build/lan-workspace';out.mkdir(parents=True,exist_ok=True)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                context=browser.new_context(viewport={'width':1440,'height':960});page=context.new_page();errors=[]
                image_requests=[];page.on('request',lambda request:image_requests.append(request) if request.method=='POST' and '/api/image' in request.url else None)
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.lan_url,wait_until='domcontentloaded');page.locator('#access-code').fill(self.lan.config['access_code']);page.locator('#login-submit').click()
                page.wait_for_selector('#workspace:not([hidden])');page.wait_for_function('()=>document.getElementById("count-shot").textContent==="1"')
                self.assertEqual(page.locator('.lan-tabs [role=tab]').count(),3)
                self.assertEqual(page.locator('#workspace h1').count(),0)
                with self.sku.connect() as c:c.execute("UPDATE sku_rows SET status='已拍摄' WHERE id='row3'")
                page.wait_for_function('()=>document.getElementById("count-shot").textContent==="2"',timeout=12000)
                page.screenshot(path=str(out/'progress.png'))
                page.locator('#tab-search').click();page.locator('#search-query').fill('红色');page.locator('#search-form button.primary').click()
                page.wait_for_function('()=>document.querySelectorAll("#search-grid .lan-card").length===2')
                page.locator('#search-grid .lan-card').first.click();page.wait_for_selector('#asset-detail[open]');page.wait_for_function('()=>document.getElementById("detail-content").textContent.includes("主体颜色")')
                self.assertIn('红色',page.locator('#detail-content').inner_text());page.locator('#detail-close').click()
                page.screenshot(path=str(out/'search.png'))
                page.locator('#reference-file').set_input_files(str(self.rows[0]['path']));page.wait_for_function('()=>!document.querySelector(".reference-threshold").disabled');page.locator('.reference-threshold').press('Home');page.wait_for_function('()=>document.querySelectorAll("#search-grid .lan-card").length===3')
                image_count=len(image_requests);page.locator('.reference-threshold').press('End')
                page.wait_for_function('()=>document.querySelectorAll("#search-grid .lan-card").length<3')
                page.locator('.reference-threshold').press('Home');page.wait_for_function('()=>document.querySelectorAll("#search-grid .lan-card").length===3')
                self.assertEqual(len(image_requests),image_count)
                page.locator('#clear-reference').click();page.locator('#tab-archive').click();page.wait_for_selector('#archive-grid .lan-card')
                self.assertEqual(page.locator('#archive-grid .lan-card').count(),3)
                self.catalog.assign(self.rows[0]['id'],'LC8888');page.wait_for_function('()=>document.getElementById("archive-grid").textContent.includes("LC8888")',timeout=12000)
                page.screenshot(path=str(out/'archive.png'))
                page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(out/'mobile.png'))
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                self.lan.configure({'rotate':True});page.locator('#lan-settings-trigger').hover();page.locator('#refresh').click();page.wait_for_selector('#login:not([hidden])')
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_search_live_input_enter_composition_and_latest_query(self):
        self.development_access()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.add_init_script("""
                  window.searchRequests=[];window.holdSearch='';
                  const nativeFetch=window.fetch.bind(window);
                  window.fetch=async(input,options)=>{
                    const url=new URL(typeof input==='string'?input:input.url,location.href);
                    if(url.pathname!=='/api/search')return nativeFetch(input,options);
                    const query=url.searchParams.get('q');searchRequests.push(query);
                    const response=await nativeFetch(input,options);
                    if(holdSearch&&query===holdSearch)await new Promise(resolve=>window.releaseSearch=resolve);
                    return response;
                  };
                """)
                page.goto(self.lan_url,wait_until='domcontentloaded');page.locator('#tab-search').click()
                page.wait_for_function('()=>document.querySelectorAll("#search-grid .lan-card").length===3')
                query=page.locator('#search-query')
                page.evaluate('searchRequests=[]');query.fill('LC1234')
                page.wait_for_function('()=>document.querySelectorAll("#search-grid .lan-card").length===2')
                self.assertEqual(page.evaluate('searchRequests'),['LC1234'])
                page.evaluate('searchRequests=[]');query.fill('LC5678');query.press('Enter')
                page.wait_for_function('()=>document.querySelectorAll("#search-grid .lan-card").length===1')
                page.wait_for_timeout(400)
                self.assertEqual(page.evaluate('searchRequests'),['LC5678'])
                query.fill('');page.wait_for_function('()=>document.querySelectorAll("#search-grid .lan-card").length===3')
                page.evaluate('searchRequests=[]');query.dispatch_event('compositionstart');query.fill('红色');query.press('Enter')
                page.wait_for_timeout(350);self.assertEqual(page.evaluate('searchRequests'),[])
                query.dispatch_event('compositionend')
                page.wait_for_function('()=>searchRequests.length===1')
                self.assertEqual(page.evaluate('searchRequests'),['红色'])
                page.wait_for_function('()=>!document.getElementById("search-message").textContent.includes("正在")')
                page.evaluate("searchRequests=[];holdSearch='LC1234'");query.fill('LC1234');query.press('Enter')
                page.wait_for_function('()=>!!window.releaseSearch')
                page.evaluate("window.rendered=[];new MutationObserver(()=>rendered.push(document.getElementById('search-grid').innerText)).observe(document.getElementById('search-grid'),{childList:true})")
                query.fill('LC5678');query.press('Enter');page.wait_for_timeout(400)
                self.assertEqual(page.evaluate('searchRequests'),['LC1234'])
                page.evaluate('holdSearch="";releaseSearch()')
                page.wait_for_function('()=>document.querySelectorAll("#search-grid .lan-card").length===1&&document.getElementById("search-grid").textContent.includes("LC5678")')
                self.assertEqual(page.evaluate('searchRequests'),['LC1234','LC5678'])
                self.assertTrue(all('LC1234' not in text for text in page.evaluate('rendered')))
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_compact_header_filters_and_hover_settings(self):
        out=fixture.ROOT/'build/lan-header-layout';out.mkdir(parents=True,exist_ok=True)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':950});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.lan_url,wait_until='domcontentloaded');page.locator('#access-code').fill(self.lan.config['access_code']);page.locator('#login-submit').click()
                page.wait_for_selector('#workspace:not([hidden])');page.locator('#tab-search').click();page.wait_for_selector('#search-grid .lan-card')
                self.assertEqual(page.locator('.lan-header .lan-tabs [role=tab]').count(),3)
                self.assertEqual(page.locator('#search-form select').count(),3);self.assertEqual(page.locator('.lan-filters #search-root').count(),0)
                self.assertEqual(page.locator('#upload-button').count(),0)
                positions=page.evaluate('''()=>Object.fromEntries(['search-query','search-submit','search-root','search-media','search-mode'].map(id=>{const r=document.getElementById(id).getBoundingClientRect();return [id,{x:r.x,y:r.y,width:r.width}]}))''')
                self.assertLess(positions['search-query']['x'],positions['search-submit']['x'])
                self.assertLess(positions['search-submit']['x'],positions['search-root']['x'])
                self.assertEqual(positions['search-query']['y'],positions['search-root']['y'])
                self.assertTrue(page.locator('#lan-settings-menu').is_hidden());page.locator('#lan-settings-trigger').hover()
                self.assertTrue(page.locator('#lan-settings-menu').is_visible())
                before=page.evaluate('FramekeepTheme.get()');page.locator('#theme-cycle').click();self.assertNotEqual(before,page.evaluate('FramekeepTheme.get()'))
                self.assertTrue(page.locator('#lan-settings-menu').is_hidden())
                page.locator('#lan-settings-trigger').focus();page.keyboard.press('ArrowDown');self.assertEqual(page.locator('#refresh').evaluate('(node)=>node===document.activeElement'),True)
                page.keyboard.press('Escape');self.assertTrue(page.locator('#lan-settings-menu').is_hidden())
                page.locator('#search-query').click();page.locator('#lan-settings-trigger').hover();page.locator('#search-query').click();self.assertTrue(page.locator('#lan-settings-menu').is_hidden())
                page.locator('#search-media').select_option('image');page.wait_for_selector('#search-grid .lan-card')
                for width in (855,700,390,340):
                    page.set_viewport_size({'width':width,'height':844})
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'),width)
                    self.assertEqual(page.locator('.lan-header [role=tab]').count(),3)
                    self.assertTrue(page.locator('#search-root').is_visible());self.assertTrue(page.locator('#search-mode').is_visible())
                    query=page.locator('#search-query').bounding_box();submit=page.locator('#search-submit').bounding_box()
                    self.assertAlmostEqual(query['y'],submit['y'],delta=1)
                    self.assertAlmostEqual(query['x']+query['width']+8,submit['x'],delta=1)
                    page.screenshot(path=str(out/f'width-{width}.png'))
                touch_context=browser.new_context(viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
                touch=touch_context.new_page();touch.goto(self.lan_url,wait_until='domcontentloaded')
                touch.locator('#access-code').fill(self.lan.config['access_code']);touch.locator('#login-submit').tap();touch.wait_for_selector('#workspace:not([hidden])')
                touch.locator('#lan-settings-trigger').tap();self.assertTrue(touch.locator('#lan-settings-menu').is_visible())
                touch.locator('#lan-settings-trigger').tap();self.assertTrue(touch.locator('#lan-settings-menu').is_hidden())
                touch_context.close()
                self.assertFalse(errors,errors)
            finally:browser.close()


if __name__=='__main__':unittest.main()
