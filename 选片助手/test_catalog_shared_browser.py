"""真实页面只发一次搜索，快速改词中断旧 SQL，两个窗口相互独立。"""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs

from playwright.sync_api import sync_playwright
from asset_catalog import AssetCatalog
from catalog_store import Store
from catalog_search_requests import CatalogSearchRequests
import test_catalog_search_candidates as fixtures
import server

ROOT=Path(__file__).resolve().parents[1]


class CatalogSharedBrowserTests(unittest.TestCase):
    def test_shared_search_cancellation_clear_and_two_page_isolation(self):
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            data=Path(folder);catalog=AssetCatalog(data);stack.callback(catalog.close)
            with catalog.connect() as c:
                c.executemany('INSERT INTO roots(id,path,created) VALUES(?,?,0)',[('one','Z:/one'),('two','Z:/two')])
                for item,root,name in [('b1','one','blue/first.jpg'),('b2','two','blue/second.jpg'),('r1','one','red/third.jpg')]:
                    fixtures.CatalogSearchCandidateTests.add(c,item,root,name,item,'')
            original=catalog._search_query
            started={key:threading.Event() for key in ('slow-a','slow-b','slow-clear','slow-leave','slow-close')}
            ended={key:threading.Event() for key in started}
            def delayed(connection,text):
                if text in started:
                    started[text].set()
                    try:connection.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n').fetchone()
                    finally:ended[text].set()
                return original(connection,text)
            stack.enter_context(patch.object(catalog,'_search_query',side_effect=delayed))
            for key,value in {'STORE':Store(data,ROOT),'DATA':data,'CATALOG':catalog,'CATALOG_SEARCH':CatalogSearchRequests(),
                              'ARCHIVE':None,'SKU':None,'WATCH':None,'MOUSE':None,'MODELS':None,'ANALYSIS':None,
                              'RAWLINK':None,'FILESEARCH':None,'UPDATES':None,'REFERENCES':None,'PREVIEWS':None}.items():
                stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            worker=threading.Thread(target=http.serve_forever,daemon=True);worker.start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        errors=[];requests=[];pages=[browser.new_page(),browser.new_page()]
                        for number,page in enumerate(pages):
                            page.on('pageerror',lambda error:errors.append(str(error)))
                            page.on('request',lambda request,n=number:requests.append((n,urlsplit(request.url))) if '/api/catalog/' in request.url else None)
                            page.route('**/api/catalog/preview/**',lambda route:route.fulfill(status=200,content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"/>'))
                            page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                            page.wait_for_function('()=>window.FramekeepBoot?.loading===false')
                            page.locator('#asset-archive-open').click()
                            page.wait_for_function('()=>document.querySelectorAll("#catalog-grid .catalog-card").length===3')
                        state_requests=sum(n==0 and url.path=='/api/catalog/state' for n,url in requests)
                        pages[0].locator('#catalog-search').fill('slow-a');self.assertTrue(started['slow-a'].wait(5))
                        self.assertEqual(sum(n==0 and url.path=='/api/catalog/state' for n,url in requests),state_requests)
                        pages[1].locator('#catalog-search').fill('slow-b');self.assertTrue(started['slow-b'].wait(5))
                        pages[0].locator('#catalog-search').fill(' blue ')
                        pages[0].wait_for_function('()=>document.querySelector("#catalog-summary").textContent.startsWith("2 项素材")&&document.querySelector("#catalog-count").textContent==="2 项"')
                        self.assertTrue(ended['slow-a'].wait(3));self.assertFalse(ended['slow-b'].is_set())
                        self.assertEqual(set(pages[0].locator('#catalog-grid .catalog-card').evaluate_all('(els)=>els.map(e=>e.dataset.catalogItem)')),{'b1','b2'})
                        pages[0].route('**/api/catalog/items*',lambda route:route.fulfill(status=503,json={'error':'查询暂时不可用'}) if parse_qs(urlsplit(route.request.url).query).get('q')==['broken'] else route.continue_())
                        pages[0].locator('#catalog-search').fill('broken')
                        pages[0].wait_for_function('()=>document.querySelector("#catalog-summary").textContent.includes("读取失败")')
                        self.assertEqual(pages[0].locator('#catalog-grid .catalog-card').count(),2)
                        self.assertNotEqual(pages[0].locator('#catalog-grid').get_attribute('aria-busy'),'true')
                        pages[0].unroute('**/api/catalog/items*')
                        pages[1].locator('#catalog-search').fill('red')
                        pages[1].wait_for_function('()=>document.querySelector("#catalog-summary").textContent.startsWith("1 项素材")')
                        self.assertTrue(ended['slow-b'].wait(3))
                        pages[0].locator('#catalog-search').fill('slow-clear');self.assertTrue(started['slow-clear'].wait(5))
                        pages[0].locator('#catalog-search').fill('')
                        pages[0].wait_for_function('()=>document.querySelectorAll("#catalog-grid .catalog-card").length===3')
                        self.assertTrue(ended['slow-clear'].wait(3))
                        pages[0].locator('#catalog-search').fill('slow-leave');self.assertTrue(started['slow-leave'].wait(5))
                        pages[0].locator('#selection-tab').click()
                        self.assertTrue(ended['slow-leave'].wait(3))
                        pages[1].locator('#catalog-search').fill('slow-close');self.assertTrue(started['slow-close'].wait(5))
                        pages[1].close();self.assertTrue(ended['slow-close'].wait(3))
                        search_requests=[(n,parse_qs(url.query)) for n,url in requests if url.path=='/api/catalog/items' and parse_qs(url.query).get('q')]
                        self.assertTrue(search_requests)
                        self.assertTrue(all(q['tree']==['1'] for _,q in search_requests))
                        self.assertFalse(any(url.path=='/api/catalog/tree' and parse_qs(url.query).get('q') for _,url in requests))
                        clients={n:{q['client'][0] for number,q in search_requests if number==n} for n in (0,1)}
                        self.assertEqual(len({client.removesuffix('_stats') for client in clients[0]}),1)
                        self.assertEqual(len({client.removesuffix('_stats') for client in clients[1]}),1)
                        self.assertTrue(clients[0].isdisjoint(clients[1]));self.assertEqual(errors,[])
                    finally:browser.close()
            finally:
                http.shutdown();http.server_close();worker.join(3)
                for event in ended.values():event.wait(3)
