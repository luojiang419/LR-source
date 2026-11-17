"""真实 HTTP 与浏览器操作检验检索、归档入口和快捷弹窗。"""
import base64
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs,urlsplit
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import expect, sync_playwright

import server
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from asset_intelligence import AssetIntelligence
from catalog_store import Store
from model_providers import Providers
from sku_service import SkuService
from test_asset_intelligence import result
from retrieval_store import save_result
from visual_features import APPEARANCE, appearance

ROOT=Path(__file__).resolve().parents[1]


class RetrievalBrowserTests(unittest.TestCase):
    def test_empty_search_stays_blank_in_desktop_dialog_and_quick_modes(self):
        out=ROOT/'build/retrieval-empty-search';out.mkdir(parents=True,exist_ok=True)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                for mode in ('desktop','dialog','full','minimal'):
                    with self.subTest(mode=mode):
                        self.service.config['quick_mode']='minimal' if mode=='minimal' else 'full'
                        page=browser.new_page(viewport={'width':1440,'height':960});calls=[];errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.on('request',lambda request:calls.append(request.url) if urlsplit(request.url).path in ('/api/intelligence/search','/api/intelligence/image') else None)
                        page.goto(self.url if mode in ('desktop','dialog') else self.url+'/quick-search.html',wait_until='domcontentloaded')
                        if mode=='desktop':
                            page.locator('#asset-retrieval-open').click();panel=page.locator('#asset-retrieval-page')
                        elif mode=='dialog':
                            page.wait_for_function('()=>window.FramekeepRetrieval&&!FramekeepBoot.loading')
                            page.keyboard.press('Control+Shift+Space');panel=page.locator('#quick-search-dialog')
                        else:panel=page.locator('#quick-native-shell')
                        query=panel.locator('.retrieval-query');grid=panel.locator('.retrieval-grid')
                        expect(panel.locator('.retrieval-result-status')).to_have_text('')
                        expect(grid).to_have_text('');self.assertEqual(calls,[])
                        if mode!='minimal':
                            panel.locator('[data-action=search]').click()
                            panel.locator('.retrieval-media').select_option('image')
                            panel.locator('.retrieval-mode').select_option('semantic')
                            query.fill('   ');query.press('Enter')
                            expect(grid).to_have_text('');self.assertEqual(calls,[])
                            panel.locator('.retrieval-mode').select_option('index')
                        page.screenshot(path=str(out/f'{mode}-initial.png'))
                        query.fill('LC1234');expect(panel.locator('.retrieval-card')).to_have_count(2)
                        if mode!='minimal':
                            panel.locator('.retrieval-card').first.click()
                            expect(panel.locator('.retrieval-attributes')).to_contain_text('主体颜色')
                        page.screenshot(path=str(out/f'{mode}-results.png'))
                        count=len(calls);query.fill('')
                        expect(grid).to_have_text('');expect(panel.locator('.retrieval-result-status')).to_have_text('')
                        expect(panel.locator('.retrieval-more')).to_be_hidden()
                        self.assertEqual(query.get_attribute('aria-activedescendant'),None)
                        self.assertEqual(panel.locator('.retrieval-detail img').count(),0)
                        query.press('Enter');self.assertEqual(len(calls),count)
                        if mode!='minimal':
                            panel.locator('.retrieval-mode').select_option('semantic')
                            query.fill('待清空的语义搜索');query.fill('')
                            expect(grid).to_have_text('');self.assertEqual(len(calls),count)
                        page.screenshot(path=str(out/f'{mode}-cleared.png'))
                        self.assertEqual(errors,[]);page.close()
            finally:browser.close()

    def test_clear_discards_late_search_and_reference_results(self):
        image_result={'rows':[dict(self.rows[0],score=1)],'elapsed_ms':1,'match_mode':'frame','backend':'fixture'}
        with patch.object(self.service,'image_search',return_value=image_result),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click()
                panel=page.locator('#asset-retrieval-page');query=panel.locator('.retrieval-query')
                expect(panel.locator('.retrieval-result-status')).to_have_text('')
                # 模拟已发出的响应忽略取消并迟到，仍不得回填清空后的界面。
                page.evaluate(r"""()=>{const original=window.fetch;window.holdResults=true;
                  window.fetch=(path,options)=>{
                    if(holdResults&&/\/api\/intelligence\/(search\?|image$)/.test(String(path)))
                      return original(path,{...options,signal:undefined}).then(response=>new Promise(resolve=>{window.releaseResult=()=>{window.releaseResult=null;resolve(response)}}));
                    return original(path,options);
                  };
                }""")
                query.fill('LC1234');page.wait_for_function('()=>!!window.releaseResult')
                query.fill('');expect(panel.locator('.retrieval-grid')).to_have_text('')
                page.evaluate('async()=>{releaseResult();await new Promise(requestAnimationFrame)}')
                expect(panel.locator('.retrieval-grid')).to_have_text('')
                panel.locator('.retrieval-upload').set_input_files(str(self.rows[0]['path']))
                page.wait_for_function('()=>!!window.releaseResult')
                panel.locator('[data-action=clear-image]').click()
                page.evaluate('async()=>{releaseResult();window.holdResults=false;await new Promise(requestAnimationFrame)}')
                expect(panel.locator('.retrieval-grid')).to_have_text('')
                expect(panel.locator('.retrieval-result-status')).to_have_text('')
                expect(panel.locator('[data-reference=generate]')).to_be_disabled()
                query.fill('LC1234');expect(panel.locator('.retrieval-card')).to_have_count(2)
                query.fill('');panel.locator('.retrieval-upload').set_input_files(str(self.rows[0]['path']))
                expect(panel.locator('.reference-threshold')).to_be_enabled()
                panel.locator('.reference-threshold').press('Home')
                expect(panel.locator('.retrieval-card')).not_to_have_count(0)
                panel.locator('[data-action=clear-image]').click()
                expect(panel.locator('.retrieval-grid')).to_have_text('')
                self.assertEqual(errors,[])
            finally:browser.close()

    def test_split_search_isolated_between_browser_pages(self):
        from reference_library import ReferenceLibrary
        library=ReferenceLibrary(self.base/'reference-data')
        clients=[]
        try:
            with patch.object(server,'REFERENCES',library),sync_playwright() as pw:
                browser=pw.chromium.launch(channel='msedge',headless=True)
                try:
                    for _ in range(2):
                        page=browser.new_page()
                        def requested(request):
                            url=urlsplit(request.url)
                            if url.path=='/api/intelligence/search':clients.append(parse_qs(url.query).get('client',[''])[0])
                        page.on('request',requested)
                        page.goto(self.url,wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepRetrieval&&!FramekeepBoot.loading')
                        page.locator('#selection-tab').click()
                        if page.locator('#layout-split').get_attribute('aria-pressed')!='true':
                            page.locator('#view-open').click();page.locator('#layout-split').click();page.keyboard.press('Escape')
                        page.locator('#split-search').fill('1234');page.locator('#split-search').press('Enter')
                        expect(page.locator('#split-preview-grid .split-resource-card')).to_have_count(2)
                    self.assertEqual(len(set(clients)),2)
                finally:browser.close()
        finally:library.close()

    def test_quick_and_split_search_start_deferred_index_and_recover(self):
        from quick_file_index import QuickFileIndex
        from reference_library import ReferenceLibrary
        self.service.quick_index.close()
        deferred=QuickFileIndex(self.catalog,folder=self.base/'deferred-quick')
        self.service.quick_index=deferred
        library=ReferenceLibrary(self.base/'reference-data')
        entered=threading.Event();release=threading.Event();original=deferred.build
        def build():entered.set();release.wait(8);return original()
        try:
            with patch.object(server,'REFERENCES',library),patch.object(deferred,'build',side_effect=build),sync_playwright() as pw:
                browser=pw.chromium.launch(channel='msedge',headless=True)
                try:
                    page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(self.url,wait_until='domcontentloaded')
                    page.wait_for_function('()=>window.FramekeepRetrieval&&!FramekeepBoot.loading')
                    page.keyboard.press('Control+Shift+Space');quick=page.locator('#quick-search-dialog')
                    quick.locator('.retrieval-query').fill('1234');quick.locator('.retrieval-query').press('Enter')
                    expect(quick.locator('.retrieval-result-status')).to_contain_text('正在准备快捷检索索引')
                    self.assertTrue(entered.wait(1))
                    page.keyboard.press('Escape')
                    page.locator('#view-open').click();page.locator('#layout-split').click();page.keyboard.press('Escape')
                    page.locator('#split-search').fill('1234');page.locator('#split-search').press('Enter')
                    expect(page.locator('#split-results')).to_contain_text('正在准备检索索引')
                    release.set()
                    expect(page.locator('#split-preview-grid .split-resource-card')).to_have_count(2)
                    page.locator('#split-exit').click();page.keyboard.press('Control+Shift+Space')
                    expect(quick.locator('.retrieval-card')).to_have_count(2)
                    self.assertEqual(errors,[])
                finally:release.set();browser.close()
        finally:release.set();library.close()

    def test_returning_during_cancelled_bootstrap_recovers_search(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded')
                page.wait_for_function('()=>window.FramekeepRetrieval&&!FramekeepBoot.loading')
                page.evaluate("""()=>{const original=window.fetch;window.bootstrapCalls=0;
                  window.fetch=(path,options)=>{
                    if(String(path)==='/api/intelligence/search-state'&&++window.bootstrapCalls===1)
                      return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(options.signal.reason),{once:true}));
                    return original(path,options);
                  };
                  FramekeepWorkspace.show('retrieval');
                }""")
                page.wait_for_function('()=>bootstrapCalls===1')
                page.evaluate("()=>{FramekeepWorkspace.show('selection');FramekeepWorkspace.show('retrieval')}")
                panel=page.locator('#asset-retrieval-page')
                expect(panel.locator('.retrieval-grid')).to_have_text('')
                expect(panel.locator('.retrieval-result-status')).to_have_text('')
                panel.locator('.retrieval-query').fill('LC1234')
                expect(panel.locator('.retrieval-card')).to_have_count(2)
                expect(panel.locator('.retrieval-result-status')).not_to_contain_text('aborted')
                self.assertEqual(errors,[])
            finally:browser.close()

    def test_split_sku_waits_for_index_then_shows_shared_results(self):
        from reference_library import ReferenceLibrary
        library=ReferenceLibrary(self.base/'reference-data')
        calls=[];original=self.service.search
        def search(**query):
            if query.get('text')=='1234':
                calls.append(query)
                if len(calls)==1:return {'rows':[],'has_more':False,'indexing':True,'elapsed_ms':0}
            return original(**query)
        try:
            with patch.object(server,'REFERENCES',library),patch.object(self.service,'search',side_effect=search),sync_playwright() as pw:
                browser=pw.chromium.launch(channel='msedge',headless=True)
                try:
                    page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(self.url,wait_until='domcontentloaded')
                    page.wait_for_function('()=>window.FramekeepWorkspace&&!FramekeepBoot.loading')
                    page.locator('#view-open').click();page.locator('#layout-split').click();page.keyboard.press('Escape')
                    reference=page.locator('#split-reference')
                    self.assertTrue(reference.evaluate('e=>e.classList.contains("resources-collapsed")'))
                    self.assertFalse(page.locator('.split-tree-panel').is_visible())
                    self.assertTrue(page.locator('#split-search').is_visible())
                    self.assertEqual(page.locator('#split-search').evaluate('e=>e.closest("header").className'),'split-preview-heading')
                    page.locator('#split-show-resources').click();self.assertTrue(page.locator('.split-tree-panel').is_visible())
                    page.locator('#split-resource-rail').click();self.assertFalse(page.locator('.split-tree-panel').is_visible())
                    page.locator('#split-search').fill('1234');page.locator('#split-search').press('Enter')
                    expect(page.locator('#split-preview-grid .split-resource-card')).to_have_count(2)
                    self.assertGreaterEqual(len(calls),2)
                    self.assertTrue(all(query['quick'] for query in calls))
                    self.assertIn('资产检索',page.locator('#split-search-scope').inner_text())
                    page.locator('#split-hide-resources').click()
                    self.assertFalse(page.locator('.split-thumbnails').is_visible())
                    out=ROOT/'build/shooting-label-export-layout';out.mkdir(parents=True,exist_ok=True)
                    for width in [1680,1366,850,650,400]:
                        page.set_viewport_size({'width':width,'height':1000})
                        self.assertTrue(page.locator('#split-search').is_visible())
                        self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'))
                        geometry=page.locator('#split-search').bounding_box();self.assertGreaterEqual(geometry['width'],120)
                        if width in (1680,850,400):page.screenshot(path=str(out/f'split-{width}.png'))
                    page.set_viewport_size({'width':1366,'height':1000})
                    page.locator('#split-show-resources').click()
                    page.locator('#split-exit').click();page.locator('#view-open').click();page.locator('#layout-split').click();page.keyboard.press('Escape')
                    self.assertFalse(page.locator('.split-tree-panel').is_visible())
                    self.assertTrue(page.locator('#split-search').is_visible())
                    self.assertFalse(errors,errors)
                finally:browser.close()
        finally:library.close()

    def test_initial_asset_results_do_not_wait_for_slow_facets(self):
        entered=threading.Event();release=threading.Event();original=self.service.facets
        def slow(*args,**kwargs):
            entered.set();release.wait(6);return original(*args,**kwargs)
        with patch.object(self.service,'facets',side_effect=slow),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();page.goto(self.url,wait_until='domcontentloaded')
                page.wait_for_function('()=>!!window.FramekeepWorkspace');page.locator('#asset-retrieval-open').click()
                self.assertTrue(entered.wait(2))
                page.locator('#asset-retrieval-page .retrieval-query').fill('衬衫')
                expect(page.locator('#asset-retrieval-page .retrieval-card')).to_have_count(3)
                self.assertTrue(entered.is_set());self.assertFalse(release.is_set())
            finally:release.set();browser.close()

    def test_quick_settings_load_and_save_while_search_bootstrap_is_pending(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True);pending=[]
            try:
                page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.route('**/api/intelligence/search-state',lambda route:pending.append(route))
                page.goto(self.url,wait_until='domcontentloaded')
                page.wait_for_function('()=>!!window.FramekeepWorkspace')
                page.locator('#asset-retrieval-open').click()
                page.wait_for_function('()=>document.querySelector(".retrieval-result-status").textContent.includes("连接")')
                page.locator('#app-settings-open').click();page.locator('[data-settings-section=quick]').click()
                controls=page.locator('#quick-appearance-controls')
                expect(controls.locator('[data-config=quick_mode]')).to_be_visible()
                self.assertTrue(pending)
                with patch.object(self.service,'state',side_effect=AssertionError('外观保存不应统计全库')):
                    controls.locator('[data-config=quick_mode]').select_option('minimal')
                    controls.locator('[data-quick-save]').click()
                    expect(controls.locator('.retrieval-message')).to_contain_text('已保存')
                self.assertEqual(self.service.config['quick_mode'],'minimal')
                self.assertEqual(errors,[])
            finally:
                for route in pending:
                    try:route.abort()
                    except Exception:pass
                page.unroute_all(behavior='ignoreErrors');browser.close()

    def test_quick_settings_failed_load_can_retry(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();failed=False
                def config(route):
                    nonlocal failed
                    if not failed:
                        failed=True;route.fulfill(status=503,json={'error':'暂时不可用'})
                    else:route.continue_()
                page.route('**/api/intelligence/config',config)
                page.goto(self.url,wait_until='domcontentloaded');page.wait_for_function('()=>!!window.FramekeepWorkspace')
                page.locator('#app-settings-open').click();page.locator('[data-settings-section=quick]').click()
                controls=page.locator('#quick-appearance-controls')
                expect(controls).to_contain_text('读取设置失败')
                controls.get_by_role('button',name='重新读取').click()
                expect(controls.locator('[data-config=quick_mode]')).to_be_visible()
            finally:browser.close()

    def test_quick_auto_paging_preserves_nodes_retries_and_discards_old_pages(self):
        self.service.config['quick_mode']='minimal'
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True);pending=[]
            try:
                page=browser.new_page(viewport={'width':1440,'height':900});calls=[];errors=[];failed=False
                page.on('pageerror',lambda e:errors.append(str(e)))
                def response(route):
                    nonlocal failed
                    params=parse_qs(urlsplit(route.request.url).query);query=params.get('q',[''])[0];cursor=params.get('cursor',[''])[0]
                    calls.append((query,cursor))
                    def result(numbers,next_cursor=''):
                        route.fulfill(json={'rows':[dict(self.rows[0],result_id=f'{query}-{n}',name=f'{query}-{n}.jpg') for n in numbers],
                                            'has_more':bool(next_cursor),'next_cursor':next_cursor,'elapsed_ms':1})
                    if query=='replacement':result([0]);return
                    if not cursor:result(range(20),'repeat' if query=='repeat' else 'page-1');return
                    if query=='repeat':result(range(20,40),'repeat');return
                    if cursor=='page-1':
                        if not failed:failed=True;route.fulfill(status=503,json={'error':'fixture temporary failure'})
                        else:result(range(19,40),'page-2')
                        return
                    pending.append(route)
                page.route('**/api/intelligence/search?**',response)
                page.goto(self.url+'/quick-search.html',wait_until='domcontentloaded')
                query=page.locator('.retrieval-query');query.fill('paging')
                expect(page.locator('.retrieval-card')).to_have_count(20)
                expect(page.locator('.retrieval-more')).to_be_visible()
                page.locator('.retrieval-card').first.click()
                page.wait_for_function('()=>document.querySelector(".retrieval-card img").naturalWidth>0')
                page.evaluate('window.savedCard=document.querySelector(".retrieval-card");window.savedImage=savedCard.querySelector("img")')
                pane=page.locator('.retrieval-results-area');pane.evaluate('(e)=>e.scrollTop=e.scrollHeight')
                expect(page.locator('.retrieval-more')).to_have_text('重试加载')
                expect(page.locator('.retrieval-card')).to_have_count(20)
                page.wait_for_timeout(150);self.assertEqual(calls.count(('paging','page-1')),1)
                page.locator('.retrieval-more').click()
                expect(page.locator('.retrieval-card')).to_have_count(40)
                self.assertTrue(page.evaluate('savedCard===document.querySelector(".retrieval-card")&&savedImage===savedCard.querySelector("img")&&savedImage.naturalWidth>0'))
                self.assertEqual(page.locator('[aria-selected=true]').get_attribute('data-result'),'0')
                page.evaluate('''()=>{const pane=document.querySelector('.retrieval-results-area');pane.scrollTop=pane.scrollHeight;for(let i=0;i<12;i++)pane.dispatchEvent(new Event('scroll'))}''')
                page.wait_for_function('()=>document.querySelector(".retrieval-grid").getAttribute("aria-busy")==="true"')
                page.wait_for_timeout(100);self.assertEqual(calls.count(('paging','page-2')),1)
                query.fill('replacement');expect(page.locator('.retrieval-card')).to_have_count(1)
                self.assertTrue(pending)
                try:pending.pop().fulfill(json={'rows':[dict(self.rows[1],result_id='stale')],'has_more':False,'next_cursor':''})
                except Exception:pass  # 已被浏览器取消的路由也允许直接结束。
                page.wait_for_timeout(100);expect(page.locator('.retrieval-card')).to_have_count(1)
                expect(page.locator('.retrieval-card strong')).to_have_text(self.rows[0]['sku'] or 'replacement-0.jpg')
                query.fill('repeat');expect(page.locator('.retrieval-card')).to_have_count(20)
                pane.evaluate('(e)=>e.scrollTop=e.scrollHeight');expect(page.locator('.retrieval-card')).to_have_count(40)
                pane.evaluate('(e)=>e.scrollTop=e.scrollHeight');page.wait_for_timeout(200)
                self.assertEqual(calls.count(('repeat','repeat')),1)
                expect(page.locator('.retrieval-more')).to_be_hidden()
                query.fill('');expect(page.locator('.retrieval-card')).to_have_count(0)
                self.assertEqual(errors,[])
            finally:
                for route in pending:
                    try:route.abort()
                    except Exception:pass
                page.unroute_all(behavior='ignoreErrors');browser.close()

    def test_quick_search_retries_short_budget_without_showing_busy_error(self):
        self.service.config['quick_mode']='minimal'
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();calls=[];errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                def response(route):
                    params=parse_qs(urlsplit(route.request.url).query)
                    if params.get('q',[''])[0]!='burst':return route.continue_()
                    calls.append(route.request.url)
                    if len(calls)==1:
                        return route.fulfill(json={'rows':[],'has_more':False,'next_cursor':'','elapsed_ms':9,'scope':'files','pending':True,'retry_after_ms':16})
                    route.fulfill(json={'rows':[dict(self.rows[0],name='burst.jpg',result_id='burst-result')],
                                        'has_more':False,'next_cursor':'','elapsed_ms':1,'scope':'files'})
                page.route('**/api/intelligence/search?**',response)
                page.goto(self.url+'/quick-search.html',wait_until='domcontentloaded')
                page.locator('.retrieval-query').fill('burst')
                expect(page.locator('.retrieval-card')).to_have_count(1)
                self.assertEqual(len(calls),2)
                self.assertNotIn('暂时繁忙',page.locator('.retrieval-result-status').inner_text())
                self.assertEqual(errors,[])
            finally:browser.close()

    def test_new_quick_query_is_not_blocked_by_slow_previous_thumbnails(self):
        folder=self.base/'LC1234'
        for number in range(9):Image.new('RGB',(8,8),'red').save(folder/f'LC1234-extra-{number}.jpg')
        self.catalog.scan(self.rows[0]['root'])
        while self.service.quick_index.sync():pass
        self.service.config['quick_mode']='minimal'
        entered=threading.Event();release=threading.Event();active=[]
        def slow(*args,**kwargs):
            active.append(1)
            if len(active)>=6:entered.set()
            release.wait(8)
            return b''
        with patch.object(self.catalog,'preview',side_effect=slow),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':960})
                page.goto(self.url+'/quick-search.html',wait_until='domcontentloaded')
                query=page.locator('.retrieval-query');query.fill('LC1234')
                page.wait_for_function('()=>document.querySelectorAll("[data-result]").length===11')
                self.assertTrue(entered.wait(4))
                query.fill('UNMATCHED999999')
                page.wait_for_function('()=>document.querySelectorAll("[data-result]").length===0&&document.querySelector(".retrieval-result-status").textContent.includes("0 项")',timeout=2000)
            finally:release.set();browser.close()

    def test_local_fullscreen_releases_slow_grid_image_connections(self):
        folder=self.base/'LC1234'
        for number in range(9):Image.new('RGB',(8,8),'red').save(folder/f'LC1234-extra-{number}.jpg')
        self.catalog.scan(self.rows[0]['root'])
        while self.service.quick_index.sync():pass
        self.service.config['quick_mode']='minimal'
        entered=threading.Event();release=threading.Event();active=[];original=self.catalog.preview
        def slow(*args,**kwargs):
            if len(active)<6:
                active.append(1)
                if len(active)==6:entered.set()
                release.wait(8)
            return original(*args,**kwargs)
        with patch.object(self.catalog,'preview',side_effect=slow),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':960})
                page.goto(self.url+'/quick-search.html',wait_until='domcontentloaded')
                page.locator('.retrieval-query').fill('LC1234')
                page.wait_for_selector('.retrieval-card');self.assertTrue(entered.wait(4))
                page.locator('.retrieval-card').nth(8).dblclick()
                page.wait_for_function('()=>document.querySelector("#asset-viewer-image").naturalWidth>0',timeout=2000)
                self.assertFalse(release.is_set())
            finally:release.set();browser.close()

    def test_startup_refresh_keeps_results_typed_during_filter_loading(self):
        entered=threading.Event();release=threading.Event();original=self.service.facets
        def delayed(*args,**kwargs):entered.set();release.wait(6);return original(*args,**kwargs)
        with patch.object(self.service,'facets',side_effect=delayed),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();requests=[]
                page.on('request',lambda r:requests.append(r.url))
                page.goto(self.url+'/quick-search.html',wait_until='domcontentloaded');self.assertTrue(entered.wait(4))
                query=page.locator('.retrieval-query');query.fill('LC1234');query.press('Enter')
                page.wait_for_function('()=>document.querySelectorAll(".retrieval-card").length===2')
                card=page.locator('.retrieval-card').first.element_handle()
                before=sum('/api/intelligence/search?' in u for u in requests)
                with page.expect_response(lambda r:'/api/intelligence/facets?' in r.url):release.set()
                page.wait_for_timeout(150)
                self.assertEqual(sum('/api/intelligence/search?' in u for u in requests),before)
                self.assertTrue(card.evaluate('(e)=>e.isConnected'))
            finally:release.set();browser.close()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.stack=ExitStack()
        self.store=Store(self.base/'data',ROOT);self.catalog=AssetCatalog(self.store.data)
        self.providers=Providers(self.store.data);self.archive=AssetArchive(self.store,autostart=False)
        self.sku=SkuService(self.store,self.providers,autostart=False)
        self.service=AssetIntelligence(self.catalog,self.providers,self.sku,autostart=False)
        folder=self.base/'LC1234';folder.mkdir()
        for name,color in [('衬衫正面.jpg','red'),('衬衫背面.jpg','red'),('蓝色衬衫.jpg','blue')]:Image.new('RGB',(220,300),color).save(folder/name)
        self.catalog.add_root(str(folder));self.rows=self.catalog.items()['rows']
        for row in self.rows:
            color='蓝色' if '蓝' in row['name'] else '红色';value=result(color)
            payload=Path(row['path']).read_bytes()
            with self.catalog.connect() as c:save_result(c,row['id'],row['fingerprint'],'test','fixture',[{'seconds':0,'result':value}],[(0,APPEARANCE,appearance(payload))])
            self.catalog.assign(row['id'],'LC1234' if color=='红色' else 'LC5678')
        self.service.quick_index.build()
        while self.service.quick_index.sync():pass
        values=dict(STORE=self.store,CATALOG=self.catalog,ARCHIVE=self.archive,SKU=self.sku,MODELS=self.providers,
                    INTELLIGENCE=self.service,LANWORKSPACE=None,DATA=self.store.data,WATCH=None,MOUSE=None,ANALYSIS=None,
                    RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=None)
        for name,value in values.items():self.stack.enter_context(patch.object(server,name,value))
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.http.daemon_threads=True
        threading.Thread(target=self.http.serve_forever,daemon=True).start();self.url=f'http://127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.stack.close();self.service.close()
        self.sku.close();self.archive.close();self.catalog.close();self.tmp.cleanup()

    def test_natural_language_time_and_person_outfit_queries(self):
        from datetime import datetime,timedelta
        from search_intent import LOCAL_TZ
        yesterday=(datetime.now(LOCAL_TZ)-timedelta(days=1)).replace(hour=12,minute=0,second=0,microsecond=0)
        red=next(row for row in self.rows if '背面' in row['name'])
        blue=next(row for row in self.rows if '蓝色' in row['name'])
        for row,color,category in [(red,'红色','外套'),(blue,'蓝色','牛仔裤')]:
            value=result(color);value['summary']='女性身穿'+color+category
            value['subjects']=[{'gender':'female','clothing':[{'category':category,'color':color,'description':''}]}]
            with self.catalog.connect() as c:
                save_result(c,row['id'],row['fingerprint'],'test','fixture',[{'seconds':0,'result':value}],[])
                c.execute("UPDATE catalog_metadata SET status='ready',payload=? WHERE item=?",(json.dumps({'capture':{'time':yesterday.isoformat()}}),row['id']))
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click()
                panel=page.locator('#asset-retrieval-page');query=panel.locator('.retrieval-query')
                query.fill('昨天的图片');query.press('Enter');expect(panel.locator('.retrieval-card')).to_have_count(2)
                expect(panel.locator('.retrieval-intent')).to_contain_text(yesterday.date().isoformat())
                query.fill('昨天女人穿红色外套');query.press('Enter');expect(panel.locator('.retrieval-card')).to_have_count(1)
                expect(panel.locator('.retrieval-intent')).to_contain_text('红色外套')
                query.fill('女人穿着蓝色牛仔裤');query.press('Enter');expect(panel.locator('.retrieval-card')).to_have_count(1)
                page.keyboard.press('Control+Shift+Space');quick=page.locator('#quick-search-dialog')
                quick.locator('.retrieval-query').fill('女人穿红色外套');quick.locator('.retrieval-query').press('Enter')
                expect(quick.locator('.retrieval-card')).to_have_count(1);expect(quick.locator('.retrieval-intent')).to_contain_text('红色外套')
                out=ROOT/'build/smart-search-browser';out.mkdir(exist_ok=True,parents=True);page.screenshot(path=str(out/'native-layout-query.png'))
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_quick_modes_background_settings_and_keyboard_preview(self):
        out=ROOT/'build/quick-search-glass';out.mkdir(parents=True,exist_ok=True)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                context=browser.new_context(viewport={'width':1440,'height':960})
                page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded')
                page.locator('#app-settings-open').click();page.locator('[data-settings-section=quick]').click()
                settings=page.locator('#quick-appearance-controls')
                settings.locator('[data-config=quick_mode]').select_option('minimal')
                settings.locator('[data-config=quick_background]').uncheck()
                self.assertLess(settings.locator('[data-config=quick_background]').bounding_box()['height'],21)
                self.assertTrue(settings.locator('[data-config=quick_blur]').is_disabled())
                settings.locator('[data-quick-save]').click()
                expect(settings.locator('.retrieval-message')).to_contain_text('已保存')
                self.assertEqual(self.service.config['quick_mode'],'minimal')
                page.screenshot(path=str(out/'settings-minimal.png'))
                page.keyboard.press('Control+Shift+Space');quick=page.locator('#quick-search-dialog')
                expect(quick).to_be_visible();expect(quick.locator('.retrieval-body')).to_be_hidden()
                quick.evaluate('(e)=>Promise.all(e.getAnimations().map(a=>a.finished))')
                bar=quick.locator('.retrieval-searchbar').bounding_box()
                self.assertLess(bar['height'],90)
                self.assertAlmostEqual(bar['y']+bar['height']/2,480,delta=2)
                backdrop=quick.evaluate('(e)=>({color:getComputedStyle(e,"::backdrop").backgroundColor,filter:getComputedStyle(e,"::backdrop").backdropFilter})')
                self.assertEqual(backdrop,{'color':'rgba(0, 0, 0, 0)','filter':'none'})
                page.screenshot(path=str(out/'minimal-empty-no-background.png'))
                query=quick.locator('.retrieval-query');query.fill('LC1234')
                expect(quick.locator('.retrieval-card')).to_have_count(2)
                self.assertEqual(quick.locator('.retrieval-grid').evaluate('(e)=>getComputedStyle(e).display'),'grid')
                expect(quick.locator('.retrieval-filters')).to_be_hidden()
                for theme in ('light','pure-white','dark','hardcore'):
                    page.evaluate('(t)=>FramekeepTheme.set(t)',theme)
                    self.assertEqual(query.evaluate('(e)=>getComputedStyle(e).backgroundColor'),'rgba(0, 0, 0, 0)')
                    page.screenshot(path=str(out/(theme+'-minimal-results.png')))
                page.screenshot(path=str(out/'minimal-results-no-background.png'))
                query.press('ArrowDown');query.press('Enter')
                expect(page.locator('#asset-image-viewer')).to_be_visible()
                page.locator('#asset-viewer-close').click()
                query.fill('');expect(quick.locator('.retrieval-body')).to_be_hidden()
                page.keyboard.press('Escape');expect(quick).to_be_hidden()
                settings.locator('[data-config=quick_background]').check()
                settings.locator('[data-config=quick_blur]').fill('37')
                settings.locator('[data-config=quick_brightness]').fill('62')
                settings.locator('[data-quick-save]').click();expect(settings.locator('.retrieval-message')).to_contain_text('已保存')
                page.reload(wait_until='domcontentloaded')
                page.wait_for_function('()=>window.FramekeepRetrieval&&!FramekeepBoot.loading')
                page.keyboard.press('Control+Shift+Space');expect(quick).to_be_visible()
                self.assertEqual(page.evaluate('FramekeepQuickAppearance.get()'),{'quick_mode':'minimal','quick_background':True,'quick_blur':37,'quick_brightness':62})
                quick.evaluate('(e)=>Promise.all(e.getAnimations().map(a=>a.finished))')
                self.assertIn('37px',quick.evaluate('(e)=>getComputedStyle(e,"::backdrop").backdropFilter'))
                self.assertIn('brightness(0.62)',quick.evaluate('(e)=>getComputedStyle(e,"::backdrop").backdropFilter'))
                page.screenshot(path=str(out/'minimal-blurred.png'))
                page.keyboard.press('Escape')
                page.locator('#app-settings-open').click();page.locator('[data-settings-section=quick]').click()
                settings.locator('[data-config=quick_mode]').select_option('full');settings.locator('[data-quick-save]').click()
                expect(settings.locator('.retrieval-message')).to_contain_text('已保存')
                page.keyboard.press('Control+Shift+Space');expect(quick.locator('.retrieval-filters')).to_be_visible()
                self.assertGreater(quick.bounding_box()['height'],600)
                expect(quick.locator('.retrieval-searchbar .retrieval-scope')).to_have_count(3)
                expect(quick.locator('.retrieval-filters .retrieval-scope')).to_have_count(0)
                self.assertEqual(len(quick.locator('.retrieval-body').evaluate('(e)=>getComputedStyle(e).gridTemplateColumns').split()),2)
                expect(quick.locator('.retrieval-detail')).to_be_hidden()
                quick.locator('.retrieval-query').fill('LC1234')
                expect(quick.locator('.retrieval-card')).to_have_count(2)
                page.screenshot(path=str(out/'full-adjusted.png'))
                quick.locator('.retrieval-card').first.click()
                expect(quick.locator('.retrieval-detail')).to_be_visible()
                self.assertEqual(quick.locator('.retrieval-detail').evaluate('(e)=>getComputedStyle(e).position'),'absolute')
                page.screenshot(path=str(out/'full-detail-drawer.png'))
                page.keyboard.press('Escape');expect(quick.locator('.retrieval-detail')).to_be_hidden()
                page.keyboard.press('Escape');expect(quick).to_be_hidden()
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_minimal_heading_and_frost_follow_query_theme_and_background_settings(self):
        self.service.config['quick_mode']='minimal'
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1920,'height':1080});errors=[];requests=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('request',lambda r:requests.append(r.url))
                page.goto(self.url+'/quick-search.html',wait_until='domcontentloaded')
                query=page.locator('.retrieval-query');heading=page.locator('.quick-query-heading')
                query.fill('LC1234');expect(page.locator('.retrieval-card')).to_have_count(2)
                expect(page.locator('.quick-query-summary')).to_have_text('2 项匹配素材')
                for theme in ('dark','light','pure-white','hardcore'):
                    before=sum('/api/intelligence/search?' in u for u in requests)
                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                    frost=page.evaluate('''()=>({
                      color:getComputedStyle(document.body,'::before').backgroundColor,
                      display:getComputedStyle(document.body,'::before').display,
                      card:getComputedStyle(document.querySelector('.retrieval-card')).backgroundColor,
                      blur:getComputedStyle(document.querySelector('.retrieval-searchbar')).backdropFilter
                    })''')
                    self.assertNotEqual(frost['display'],'none')
                    self.assertIn(' / ',frost['color']);self.assertIn(' / ',frost['card'])
                    self.assertIn('blur(36px)',frost['blur'])
                    expect(heading).to_be_visible()
                    page.locator('.retrieval-card').first.click()
                    edge=page.locator('.retrieval-card.active').evaluate('(e)=>({border:getComputedStyle(e).borderColor,expected:getComputedStyle(e).getPropertyValue("--theme-selected-edge").trim()})')
                    self.assertTrue(edge['expected'],edge)
                    self.assertNotEqual(edge['border'],'rgba(255, 255, 255, 0.5)')
                    self.assertEqual(sum('/api/intelligence/search?' in u for u in requests),before)
                page.evaluate("()=>FramekeepQuickAppearance.apply({...FramekeepQuickAppearance.get(),quick_background:false})")
                self.assertEqual(page.evaluate("getComputedStyle(document.body,'::before').display"),'none')
                page.evaluate("()=>FramekeepQuickAppearance.apply({...FramekeepQuickAppearance.get(),quick_background:true,quick_blur:37,quick_brightness:62})")
                page.evaluate("()=>FramekeepQuickAppearance.setBackdrop('data:image/svg+xml,<svg xmlns=\"http://www.w3.org/2000/svg\"/>')")
                self.assertIn('blur(37px) brightness(0.62)',page.locator('#quick-desktop-backdrop').evaluate('(e)=>getComputedStyle(e).filter'))
                unsafe='<img src=x onerror="window.injected=true">'
                query.fill(unsafe)
                expect(page.locator('.quick-query-text')).to_have_text(unsafe)
                expect(page.locator('.quick-query-summary')).to_have_text('0 项匹配素材')
                self.assertEqual(page.locator('.quick-query-text img').count(),0)
                self.assertFalse(page.evaluate('!!window.injected'))
                page.route('**/api/intelligence/search?*',lambda route:route.fulfill(status=503,content_type='application/json',body=json.dumps({'error':'检索暂不可用'})))
                query.fill('ERROR')
                expect(page.locator('.quick-query-summary')).to_have_text('检索暂不可用')
                expect(heading).to_have_class('quick-query-heading error')
                page.unroute('**/api/intelligence/search?*')
                query.fill('LC1234');expect(page.locator('.quick-query-summary')).to_have_text('2 项匹配素材')
                page.emulate_media(reduced_motion='reduce')
                self.assertEqual(page.locator('.retrieval-searchbar').evaluate('(e)=>getComputedStyle(e).transitionProperty'),'none')
                cdp=page.context.new_cdp_session(page)
                cdp.send('Emulation.setEmulatedMedia',{'features':[{'name':'prefers-reduced-transparency','value':'reduce'}]})
                for theme in ('light','hardcore'):
                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                    self.assertEqual(page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--quick-frost-tint').trim()"),'96%')
                    self.assertEqual(page.locator('.retrieval-searchbar').evaluate('(e)=>getComputedStyle(e).backdropFilter'),'none')
                query.fill('');expect(heading).to_be_hidden()
                self.assertEqual(errors,[])
            finally:browser.close()

    def test_minimal_dock_grid_responsive_keyboard_and_clear(self):
        folder=self.base/'LC1234'
        for number in range(60):Image.new('RGB',(24,32),'teal').save(folder/f'LC1234-grid-{number}.jpg')
        self.catalog.scan(self.rows[0]['root'])
        while self.service.quick_index.sync():pass
        self.service.config['quick_mode']='minimal'
        out=ROOT/'build/quick-card-grid';out.mkdir(exist_ok=True,parents=True)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page();errors=[];requests=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('request',lambda r:requests.append(r.url))
                for width,height in ((1920,1080),(1440,900),(1024,768),(640,850),(375,812),(640,480)):
                    page.set_viewport_size({'width':width,'height':height})
                    page.goto(self.url+'/quick-search.html',wait_until='domcontentloaded')
                    query=page.locator('.retrieval-query');bar=page.locator('.retrieval-searchbar')
                    page.wait_for_function('()=>document.documentElement.dataset.quickMode==="minimal"')
                    box=bar.bounding_box();self.assertAlmostEqual(box['y']+box['height']/2,height/2,delta=2)
                    query.fill('LC1234');page.wait_for_selector('.retrieval-card')
                    page.wait_for_function('()=>{const r=document.querySelector(".retrieval-searchbar").getBoundingClientRect(),bottom=innerHeight<=600?32:Math.min(120,Math.max(64,innerHeight*.1112));return Math.abs(r.bottom-(innerHeight-bottom))<1}')
                    grid=page.locator('.retrieval-grid');columns=grid.evaluate('(e)=>getComputedStyle(e).gridTemplateColumns.split(" ").length')
                    self.assertEqual(columns,1 if width<=480 else 2 if width<=760 else 3 if width<=1100 else 4)
                    body=page.locator('.retrieval-body').bounding_box();dock=bar.bounding_box()
                    heading=page.locator('.quick-query-heading').bounding_box()
                    self.assertGreaterEqual(body['y'],heading['y']+heading['height']+12)
                    self.assertLessEqual(body['y']+body['height'],dock['y']-48)
                    self.assertAlmostEqual(dock['x']+dock['width']/2,width/2,delta=2)
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                    first_card=page.locator('.retrieval-card').first.bounding_box()
                    track=grid.evaluate('(e)=>parseFloat(getComputedStyle(e).gridTemplateColumns)')
                    self.assertAlmostEqual(first_card['width'],track,delta=2)
                    if width==1920:
                        self.assertAlmostEqual(first_card['width'],336,delta=2)
                        self.assertEqual(grid.evaluate('(e)=>getComputedStyle(e).gap'),'32px')
                    expect(page.locator('.quick-query-text')).to_have_text('LC1234')
                    expect(page.locator('.quick-query-summary')).to_contain_text('项匹配素材')
                    self.assertEqual(page.locator('.retrieval-card img').first.evaluate('(e)=>getComputedStyle(e).objectFit'),'contain')
                    query.press('ArrowDown');self.assertEqual(page.locator('[aria-selected=true]').get_attribute('data-result'),'0')
                    query.press('ArrowDown');self.assertEqual(page.locator('[aria-selected=true]').get_attribute('data-result'),str(columns))
                    page.locator(f'[data-result="{columns}"]').focus();page.keyboard.press('ArrowRight')
                    self.assertEqual(page.locator('[aria-selected=true]').get_attribute('data-result'),str(columns+1))
                    self.assertFalse(any('/api/intelligence/details?' in u for u in requests))
                    self.assertFalse(any('/api/intelligence/facets?' in u for u in requests))
                    if width==1920:
                        before=page.locator('.retrieval-card').count()
                        expect(page.locator('.retrieval-more')).to_be_visible()
                        page.locator('.retrieval-results-area').evaluate('(e)=>e.scrollTop=e.scrollHeight')
                        page.wait_for_function('(n)=>document.querySelectorAll(".retrieval-card").length>n',arg=before)
                        self.assertEqual(query.get_attribute('aria-activedescendant'),page.locator('[aria-selected=true]').get_attribute('id'))
                    page.screenshot(path=str(out/f'grid-{width}.png'))
                    query.fill('');page.wait_for_function('()=>Math.abs(document.querySelector(".retrieval-searchbar").getBoundingClientRect().top+33-innerHeight/2)<2')
                    self.assertFalse(page.locator('.retrieval-body').is_visible())
                    expect(page.locator('.quick-query-heading')).to_be_hidden()
                self.assertEqual(errors,[])
            finally:browser.close()

    def test_standalone_double_click_previews_locally_and_escape_restores_grid(self):
        folder=self.base/'LC1234'
        for number in range(18):Image.new('RGB',(60,90),'teal').save(folder/f'LC1234-local-{number}.jpg')
        self.catalog.scan(self.rows[0]['root'])
        while self.service.quick_index.sync():pass
        self.service.config['quick_mode']='minimal'
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':900});errors=[];requests=[]
                page.add_init_script("window.nativeCalls=[];window.__TAURI__={core:{invoke:async(name,args)=>{nativeCalls.push(name);return null}}}")
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('request',lambda r:requests.append(r.url))
                page.goto(self.url+'/quick-search.html',wait_until='domcontentloaded')
                query=page.locator('.retrieval-query');query.fill('LC1234');page.wait_for_selector('.retrieval-card')
                page.wait_for_function('()=>document.querySelector(".retrieval-searchbar").getBoundingClientRect().top>innerHeight*.8')
                card=page.locator('.retrieval-card').nth(8);card.scroll_into_view_if_needed();card.click()
                self.assertFalse(page.locator('#asset-image-viewer').is_visible())
                page.evaluate("()=>document.addEventListener('dblclick',()=>{window.beforePreviewScroll=document.querySelector('.retrieval-results-area').scrollTop},{capture:true,once:true})")
                selected=card.locator('img').get_attribute('alt');card.dblclick()
                expect(page.locator('#asset-image-viewer')).to_be_visible()
                scroll=page.evaluate('beforePreviewScroll')
                page.wait_for_function('()=>document.querySelector("#asset-viewer-image").naturalWidth>0')
                self.assertEqual(page.url,self.url+'/quick-search.html');self.assertEqual(len(page.context.pages),1)
                self.assertNotIn('search_open_asset',page.evaluate('nativeCalls'))
                expect(page.locator('.asset-viewer-info')).to_be_visible()
                expect(page.locator('#asset-viewer-fields')).to_contain_text(selected)
                stage_box=page.locator('#asset-viewer-stage').bounding_box();info_box=page.locator('.asset-viewer-info').bounding_box()
                self.assertGreater(stage_box['width'],900)
                self.assertLessEqual(stage_box['x']+stage_box['width'],info_box['x'])
                self.assertEqual(selected,page.locator('#asset-viewer-title').inner_text())
                page.locator('#asset-viewer-stage').focus();page.keyboard.press('Equal');self.assertIn('125',page.locator('#asset-viewer-scale').inner_text())
                for theme in ('dark','light','pure-white','hardcore'):
                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                    expect(page.locator('.asset-viewer-info')).to_be_visible()
                    self.assertFalse(page.locator('#asset-image-viewer').evaluate('(e)=>e.scrollWidth>e.clientWidth'))
                page.locator('#asset-viewer-stage').focus();page.keyboard.press('ArrowRight')
                next_name=page.locator('#asset-viewer-title').inner_text();self.assertNotEqual(next_name,selected)
                expect(page.locator('#asset-viewer-fields')).to_contain_text(next_name)
                page.screenshot(path=str(ROOT/'build/quick-card-grid/local-fullscreen.png'))
                page.keyboard.press('Escape');expect(page.locator('#asset-image-viewer')).to_be_hidden()
                self.assertNotIn('search_hide',page.evaluate('nativeCalls'))
                self.assertEqual(query.input_value(),'LC1234')
                page.wait_for_function('(top)=>Math.abs(document.querySelector(".retrieval-results-area").scrollTop-top)<2',arg=scroll)
                self.assertTrue(any('/api/catalog/details/' in u for u in requests))
                query.focus();query.press('ArrowDown');query.press('Enter');expect(page.locator('#asset-image-viewer')).to_be_visible()
                page.keyboard.press('Escape');page.keyboard.press('Escape');self.assertIn('search_hide',page.evaluate('nativeCalls'))
                self.assertEqual(errors,[])
            finally:browser.close()

    def test_search_facets_preview_quick_dialog_and_archive_reference(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1580,'height':1000});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click()
                panel=page.locator('#asset-retrieval-page');query=panel.locator('.retrieval-query')
                expect(panel.locator('.retrieval-grid')).to_have_text('')
                tabs=page.locator('.workspace-tabs [role=tab]').evaluate_all('(nodes)=>nodes.map(n=>n.id)')
                self.assertEqual(tabs,['selection-tab','sku-open','asset-archive-open','inventory-open','sku-database-open','asset-retrieval-open','app-settings-open'])
                query.fill('LC1234');query.press('Enter');page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===2')
                query.fill('');query.press('Enter');expect(panel.locator('.retrieval-grid')).to_have_text('')
                query.fill('衬衫');expect(panel.locator('.retrieval-card')).to_have_count(3)
                panel.locator('.retrieval-facet-key').select_option('color')
                panel.locator('.retrieval-filter-chips [data-value="蓝色"]').click()
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===1')
                panel.locator('[data-action="clear-facets"]').click()
                query.fill('蓝色');query.press('Enter');page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===1')
                panel.locator('.retrieval-card').click();page.wait_for_function('()=>document.querySelector("#asset-retrieval-page .retrieval-attributes").textContent.includes("主体颜色")')
                self.assertIn('蓝色',panel.locator('.retrieval-detail').inner_text())
                # 手动搜索应取消输入防抖，不能在选中素材后再次清空详情。
                page.wait_for_timeout(350)
                self.assertTrue(panel.locator('[data-detail="similar"]').is_visible())
                panel.locator('[data-detail="preview"]').click();page.wait_for_selector('#asset-image-viewer[open]')
                page.locator('#asset-viewer-close').click()
                page.keyboard.press('Control+Shift+Space');page.wait_for_selector('#quick-search-dialog[open]')
                quick=page.locator('#quick-search-dialog');quick.locator('.retrieval-query').fill('LC1234');quick.locator('[data-action="search"]').click()
                page.wait_for_function('()=>document.querySelectorAll("#quick-search-dialog .retrieval-card").length===2')
                quick.locator('.retrieval-query').press('ArrowDown');self.assertEqual(quick.locator('[aria-selected=true]').count(),1)
                page.keyboard.press('Escape');expect(quick.locator('.retrieval-detail')).to_be_hidden()
                page.keyboard.press('Escape');quick.wait_for(state='hidden',timeout=3000);self.assertFalse(quick.is_visible())
                page.locator('#asset-archive-open').click();page.wait_for_selector('#catalog-grid .catalog-card')
                page.locator('#catalog-grid .catalog-card').first.click();page.locator('#catalog-image-search').click();panel.locator('.reference-threshold').press('Home')
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===3&&document.querySelector("#asset-retrieval-page .retrieval-result-status").textContent.includes("布局与配色")')
                # 归档跳转的页面刷新与参考图搜索都可能更新状态，等待最终文本。
                expect(panel.locator('.retrieval-result-status')).to_contain_text('布局与配色')
                panel.locator('[data-action="clear-image"]').click();query.fill('红色');query.press('Enter')
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===2')
                out=ROOT/'build/multimodal-search';out.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(out/'retrieval-workspace.png'))
                page.locator('.retrieval-actions-menu>summary').click();page.locator('[data-workspace-heading="retrieval"] [data-action="settings"]').click();page.wait_for_selector('#retrieval-settings[open]')
                self.assertIn('同款',page.locator('#retrieval-settings').inner_text())
                page.locator('[data-config="custom_dimensions"]').fill('电商卖点\n搭配建议')
                page.locator('[data-config="save"]').click();page.wait_for_function('()=>document.querySelector("#retrieval-settings .retrieval-message").textContent.includes("已保存")')
                self.assertEqual(self.service.config['custom_dimensions'],['电商卖点','搭配建议'])
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_desktop_bottom_navigation_filters_details_and_resizing(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1366,'height':886});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click()
                panel=page.locator('#asset-retrieval-page');query=panel.locator('.retrieval-query')
                query.fill('衬衫');expect(panel.locator('.retrieval-card')).to_have_count(3)
                self.assertTrue(page.locator('.workspace-bottom-bar').is_visible())
                self.assertEqual(page.locator('.retrieval-navigation').count(),0)
                self.assertTrue(page.locator('[data-workspace-heading="retrieval"] h2').is_visible())
                self.assertEqual(page.locator('#asset-retrieval-open').get_attribute('aria-selected'),'true')
                self.assertEqual(page.locator('.workspace-bottom-bar [role=tab]').count(),8)
                for width in (1366,850,650):
                    page.set_viewport_size({'width':width,'height':886})
                    toolbar=panel.locator('.retrieval-searchbar').bounding_box()
                    for selector in ('.retrieval-root','.retrieval-media','.retrieval-mode'):
                        box=panel.locator(selector).bounding_box()
                        self.assertIsNotNone(box);self.assertGreaterEqual(box['y'],toolbar['y'])
                        self.assertLessEqual(box['y']+box['height'],toolbar['y']+toolbar['height']+1)
                    reference=panel.locator('.reference-dropzone').bounding_box()
                    results=panel.locator('.retrieval-results-area').bounding_box()
                    self.assertLess(reference['x']+reference['width'],results['x'])
                    self.assertTrue(panel.locator('.retrieval-detail').is_visible())
                    bottom=page.locator('.workspace-bottom-bar').bounding_box()
                    content=panel.bounding_box()
                    self.assertIsNotNone(bottom)
                    self.assertLessEqual(content['y']+content['height'],bottom['y']+1)
                    self.assertLessEqual(bottom['y']+bottom['height'],886)
                    self.assertTrue(page.locator('#asset-archive-open').is_visible())
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),width)
                for theme in ('light','pure-white','hardcore','dark'):
                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                    self.assertTrue(panel.locator('.retrieval-root').is_visible())
                    self.assertTrue(panel.locator('.retrieval-detail').is_visible())
                    self.assertTrue(page.locator('.workspace-bottom-bar').is_visible())
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),650)
                page.set_viewport_size({'width':1366,'height':886})
                query.fill('LC5678')
                page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===1')
                panel.locator('.retrieval-media').select_option('video')
                page.wait_for_function('()=>document.querySelector("#asset-retrieval-page .retrieval-grid").textContent.includes("没有找到")')
                panel.locator('.retrieval-media').select_option('image')
                page.wait_for_selector('#asset-retrieval-page .retrieval-card')
                root=self.catalog.tree()['roots'][0]['id']
                with page.expect_response(lambda response:'/api/intelligence/search?' in response.url and 'root='+root in response.url):
                    panel.locator('.retrieval-root').select_option(root)
                panel.locator('.retrieval-card').click()
                page.wait_for_function('()=>document.querySelector("#asset-retrieval-page .retrieval-attributes").textContent.includes("主体颜色")')
                self.assertTrue(panel.locator('[data-detail="assign"]').is_visible())
                panel.locator('[data-detail="close"]').click();self.assertFalse(panel.locator('.retrieval-detail').is_visible())
                query.press('ArrowDown');page.keyboard.press('Escape')
                self.assertFalse(panel.locator('.retrieval-detail').is_visible())
                out=ROOT/'build/desktop-search-layout';out.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(out/'desktop-search.png'))
                page.locator('.retrieval-actions-menu>summary').click()
                page.locator('[data-workspace-heading="retrieval"] [data-action="quick"]').click()
                page.wait_for_selector('#quick-search-dialog[open]');page.keyboard.press('Escape')
                page.locator('#asset-archive-open').click()
                page.wait_for_function('()=>!document.querySelector("#asset-archive-dialog").classList.contains("hidden")')
                self.assertTrue(page.locator('.workspace-bottom-bar').is_visible())
                page.locator('#asset-retrieval-open').click()
                page.locator('#sku-open').click()
                page.wait_for_function('()=>!document.querySelector(".workbench").classList.contains("workspace-retrieval-active")')
                self.assertTrue(page.locator('.workspace-bottom-bar').is_visible())
                page.locator('#asset-retrieval-open').click()
                page.locator('#selection-tab').click()
                self.assertTrue(page.locator('#selection-page').is_visible())
                self.assertEqual(page.locator('#selection-tab').get_attribute('aria-selected'),'true')
                page.locator('#asset-retrieval-open').click()
                self.assertTrue(panel.is_visible())
                self.assertEqual(page.locator('#asset-retrieval-open').get_attribute('aria-selected'),'true')
                self.assertTrue(page.locator('.workspace-bottom-bar').is_visible())
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_desktop_detail_is_pinned_and_remembers_explicit_collapse_across_search_and_restart(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                context=browser.new_context(viewport={'width':1440,'height':950});page=context.new_page();errors=[];requests=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('request',lambda r:requests.append(r.url))
                def open_search():
                    page.goto(self.url,wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click()
                    page.locator('#asset-retrieval-page .retrieval-query').fill('衬衫')
                    page.wait_for_selector('#asset-retrieval-page .retrieval-card')
                    page.wait_for_function('()=>!document.getElementById("startup-screen")')
                open_search();panel=page.locator('#asset-retrieval-page');detail=panel.locator('.retrieval-detail');toggle=panel.locator('.retrieval-detail-toggle')
                expect(detail).to_be_visible();expect(detail).to_contain_text('选择资产查看资料')
                result_box=panel.locator('.retrieval-results-area').bounding_box();detail_box=detail.bounding_box()
                self.assertLessEqual(result_box['x']+result_box['width'],detail_box['x']+1)
                panel.locator('.retrieval-card').first.click();expect(detail.locator('.retrieval-attributes')).to_contain_text('主体颜色')
                detail.locator('[data-detail=close]').click();expect(detail).to_be_hidden();expect(toggle).to_be_visible()
                closed_width=panel.locator('.retrieval-results-area').bounding_box()['width']
                self.assertGreater(closed_width,result_box['width']+200)
                requests.clear();panel.locator('.retrieval-card').nth(1).click()
                page.wait_for_timeout(100);expect(detail).to_be_hidden()
                self.assertFalse(any('/api/intelligence/details?' in u for u in requests))
                panel.locator('.retrieval-query').fill('LC1234');page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===2')
                panel.locator('.retrieval-card').first.click();expect(detail).to_be_hidden()
                page.reload(wait_until='domcontentloaded');page.locator('#asset-retrieval-open').click();page.locator('#asset-retrieval-page .retrieval-query').fill('衬衫');page.wait_for_selector('#asset-retrieval-page .retrieval-card')
                expect(detail).to_be_hidden();panel.locator('.retrieval-card').last.click();expect(detail).to_be_hidden()
                chosen=panel.locator('.retrieval-card[aria-selected=true] strong').inner_text()
                toggle.focus();toggle.press('Enter');expect(detail).to_be_visible();expect(detail.locator('h3')).to_have_text(chosen)
                panel.locator('.retrieval-query').fill('NO-SUCH-SKU-987654')
                expect(panel.locator('.retrieval-grid')).to_contain_text('没有找到');expect(detail).to_be_visible()
                page.locator('#selection-tab').click();page.locator('#asset-retrieval-open').click();expect(detail).to_be_visible()
                panel.locator('.retrieval-query').fill('LC1234');page.wait_for_function('()=>document.querySelectorAll("#asset-retrieval-page .retrieval-card").length===2')
                panel.locator('.retrieval-card').first.click();expect(detail).to_be_visible()
                for theme in ('dark','light','pure-white','hardcore'):
                    page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                    expect(detail).to_be_visible()
                    box=panel.locator('.retrieval-results-area').bounding_box();side=detail.bounding_box()
                    self.assertLessEqual(box['x']+box['width'],side['x']+1)
                out=ROOT/'build/retrieval-detail-persistence';out.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(out/'expanded.png'))
                persisted=context.storage_state();context.close()
                context=browser.new_context(storage_state=persisted,viewport={'width':1440,'height':950});page=context.new_page();open_search()
                expect(page.locator('#asset-retrieval-page .retrieval-detail')).to_be_visible()
                page.locator('#asset-retrieval-page [data-detail=close]').click()
                persisted=context.storage_state();context.close()
                context=browser.new_context(storage_state=persisted,viewport={'width':1440,'height':950});page=context.new_page();open_search()
                expect(page.locator('#asset-retrieval-page .retrieval-detail')).to_be_hidden()
                page.locator('#asset-retrieval-page .retrieval-card').first.click()
                expect(page.locator('#asset-retrieval-page .retrieval-detail')).to_be_hidden()
                page.wait_for_function('()=>[...document.querySelectorAll("#asset-retrieval-page .retrieval-card img")].every(img=>img.complete&&img.naturalWidth>0)')
                page.screenshot(path=str(out/'collapsed.png'));self.assertFalse(errors,errors)
                context.close()
            finally:browser.close()

    def test_standalone_quick_page_upload_and_request_token(self):
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':1440,'height':900});errors=[];writes=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda req:writes.append(req.headers.get('x-framekeep-token')) if req.method=='POST' else None)
                page.goto(self.url+'/quick-search.html',wait_until='domcontentloaded')
                page.locator('.retrieval-upload').set_input_files(str(self.rows[0]['path']));page.wait_for_function('()=>!document.querySelector(".reference-threshold").disabled');page.locator('.reference-threshold').press('Home')
                page.wait_for_function('()=>document.querySelector(".retrieval-result-status").textContent.includes("布局与配色相似")')
                self.assertEqual(writes[-1],server.TOKEN)
                page.locator('.retrieval-card').first.click();page.wait_for_selector('[data-detail="folder"]')
                out=ROOT/'build/multimodal-search';out.mkdir(parents=True,exist_ok=True);page.wait_for_function('()=>[...document.querySelectorAll(".retrieval-card img")].every(img=>img.naturalWidth>0)');page.wait_for_function('()=>document.querySelector(".retrieval-attributes").textContent.includes("主体颜色")');page.screenshot(path=str(out/'quick-search.png'))
                self.assertFalse(errors,errors)
            finally:browser.close()

    def test_fixed_reference_area_auto_search_threshold_drop_and_paste(self):
        # 直接测试共用组件的状态与回调；完整页面和真实模型由端到端用例验证。
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                page=browser.new_page(viewport={'width':900,'height':850});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.set_content('<html data-hardcore-layout><body><main id="reference" style="width:280px"></main></body></html>')
                page.add_style_tag(path=str(ROOT/'演示方案/reference-search.css'))
                page.add_script_tag(path=str(ROOT/'演示方案/reference-search.js'))
                page.evaluate("""()=>{window.changes=[];window.thresholds=[];window.refreshes=[];
                  window.reference=FramekeepReferenceSearch.mount(document.getElementById('reference'),{
                    inputClass:'retrieval-upload',onChange:value=>changes.push(value),
                    onThreshold:value=>thresholds.push(value),onRefresh:value=>refreshes.push(value)});
                }""")
                reference=page.locator('#reference')
                red=next(row for row in self.rows if '正面' in row['name']);blue=next(row for row in self.rows if '蓝' in row['name'])
                reference.locator('.retrieval-upload').set_input_files(red['path'])
                page.wait_for_function('()=>changes.length===1&&reference.value.active')
                self.assertIn('85%',reference.locator('output').inner_text())
                self.assertEqual(page.evaluate('reference.value.matchMode'),'frame')
                reference.locator('.reference-threshold').press('Home')
                self.assertEqual(page.evaluate('thresholds.at(-1).threshold'),0)
                reference.locator('.reference-threshold').press('End')
                self.assertEqual(page.evaluate('thresholds.at(-1).threshold'),100)
                reference.locator('[data-reference="generate"]').click()
                self.assertEqual(page.evaluate('refreshes.length'),1)
                reference.locator('.reference-match-mode').select_option('semantic')
                self.assertEqual(page.evaluate('refreshes.at(-1).matchMode'),'semantic')
                encoded=base64.b64encode(Path(blue['path']).read_bytes()).decode()
                page.evaluate("""encoded=>{const data=new DataTransfer();data.items.add(new File([Uint8Array.from(atob(encoded),c=>c.charCodeAt(0))],'粘贴参考.jpg',{type:'image/jpeg'}));document.querySelector('.reference-dropzone').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}))}""",encoded)
                page.wait_for_function('()=>changes.length===2')
                self.assertEqual(page.evaluate('changes.at(-1).label'),'粘贴参考.jpg')
                encoded=base64.b64encode(Path(red['path']).read_bytes()).decode()
                page.evaluate("""encoded=>{const data=new DataTransfer();data.items.add(new File([Uint8Array.from(atob(encoded),c=>c.charCodeAt(0))],'拖入参考.jpg',{type:'image/jpeg'}));document.querySelector('.reference-dropzone').dispatchEvent(new DragEvent('drop',{dataTransfer:data,bubbles:true,cancelable:true}))}""",encoded)
                page.wait_for_function('()=>changes.length===3')
                self.assertEqual(page.evaluate('changes.at(-1).label'),'拖入参考.jpg')
                reference.locator('[data-reference="clear"]').click();self.assertFalse(page.evaluate('reference.value.active'))
                page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:undefined})")
                reference.locator('[data-reference="paste"]').click();self.assertIn('Ctrl+V',reference.locator('.reference-help').inner_text())
                self.assertFalse(errors,errors)
            finally:browser.close()


if __name__=='__main__':unittest.main()
