"""二星超时：慢目录计数不能占用交互锁，超时写入必须核对真实结果。"""
from contextlib import ExitStack
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_archive_capture_service as fixtures


class ArchiveRatingTimeoutTests(unittest.TestCase):
    setUp=fixtures.ArchiveCaptureTests.setUp
    tearDown=fixtures.ArchiveCaptureTests.tearDown
    client=fixtures.ArchiveCaptureTests.client
    item=fixtures.ArchiveCaptureTests.item
    drain=fixtures.ArchiveCaptureTests.drain

    def test_slow_progress_count_does_not_block_rating(self):
        entered=threading.Event();release=threading.Event();saved=threading.Event();errors=[]
        def count(_):
            entered.set()
            if not release.wait(10):raise TimeoutError('test count was not released')
            return 4
        def run(action):
            try:action()
            except BaseException as error:errors.append(error)
        def rate():
            self.api.rate(self.item(2),2,root_id=self.rid)
            saved.set()
        self.api.scan_cursor.clear()
        with patch.object(self.api,'_scan_total',side_effect=count):
            scan=threading.Thread(target=run,args=(lambda:self.api._scan_batches(self.dataset),))
            scan.start();rating=None
            try:
                self.assertTrue(entered.wait(5),'scan never started its progress count')
                rating=threading.Thread(target=run,args=(rate,));rating.start()
                self.assertTrue(saved.wait(3),'rating was blocked by progress-only count')
            finally:
                release.set();scan.join(10)
                if rating:rating.join(10)
        self.assertFalse(errors,errors)
        self.assertFalse(scan.is_alive())
        self.assertEqual(self.dataset.record(path='IMG_0002.JPG')['rating'],2)
        self.drain()
        self.assertEqual(self.dataset.record(path='IMG_0002.JPG')['sku'],'LC123456')

    def test_progress_query_uses_directory_index(self):
        from types import SimpleNamespace
        plans=[];connect=self.catalog.connect
        class Connection:
            def execute(inner,sql,args):
                plans.extend(str(tuple(r)) for r in inner.connection.execute('EXPLAIN QUERY PLAN '+sql,args))
                return inner.connection.execute(sql,args)
            def __enter__(inner):
                inner.context=connect();inner.connection=inner.context.__enter__();return inner
            def __exit__(inner,*args):return inner.context.__exit__(*args)
        visits=SimpleNamespace(lock=threading.RLock(),key=(self.rid,''))
        with patch.object(self.catalog,'visits',visits,create=True),patch.object(self.catalog,'connect',Connection):
            self.assertEqual(self.api._scan_total(self.dataset),4)
        self.assertTrue(any('catalog_items_root_folder' in p and 'root=?' in p and 'folder=?' in p for p in plans),plans)

    def test_batched_projection_does_not_wait_for_catalog_writer(self):
        import time
        record={**self.dataset.record(path='IMG_0001.JPG'),'sku':'TEST-PENDING'}
        item=self.catalog.item(self.item(1))
        with self.catalog.connect() as writer,self.catalog.connect() as reader,self.archive.connect() as archive:
            writer.execute('BEGIN IMMEDIATE')
            try:
                started=time.monotonic()
                self.api._project(self.dataset,record,item,catalog_connection=reader,archive_connection=archive)
                self.assertLess(time.monotonic()-started,1)
                self.assertIn((str(self.dataset.root),record['id']),self.api.projection_pending)
            finally:writer.rollback()

    def test_browser_reconciles_timeout_without_false_rollback(self):
        import server
        from playwright.sync_api import sync_playwright
        with ExitStack() as stack:
            values=dict(STORE=self.store,CATALOG=self.catalog,ARCHIVE=self.archive,SKU=self.sku,MODELS=self.sku.providers,DATA=self.store.data,WATCH=None,MOUSE=None,ANALYSIS=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None,CANON=None,PREVIEWS=None,REFERENCES=None)
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1600,'height':1000});errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded')
                        page.wait_for_function('()=>window.FramekeepArchiveOpenItem&&window.FramekeepArchiveCapture')
                        page.evaluate('(id)=>FramekeepArchiveOpenItem(id)',self.item(2))
                        page.wait_for_function("()=>!document.querySelector('[data-catalog-rating=\"2\"]').disabled")
                        # 响应先超时，服务器随后落库；普通读取先返回旧星级。
                        page.evaluate('''()=>{const api=FramekeepIO.api;window.__ratingWrites=0;
                            FramekeepIO.api=(path,...args)=>{if(path==='/api/archive-capture/rating'){
                                window.__ratingWrites++;return Promise.reject(new DOMException('timeout','TimeoutError'))}
                                return api(path,...args)}}''')
                        page.locator('[data-catalog-rating="2"]').click()
                        page.wait_for_function('()=>document.getElementById("catalog-rating-status").textContent.includes("保存结果尚未确认")')
                        self.assertTrue(page.evaluate('(id)=>FramekeepArchiveCapture.pending(id)',self.item(2)))
                        self.assertNotIn('二星未保存',page.locator('#capture-ocr-feedback').inner_text())
                        self.assertEqual(self.dataset.record(path='IMG_0002.JPG')['rating'],0)
                        self.api.rate(self.item(2),2,root_id=self.rid)
                        page.evaluate('()=>FramekeepArchiveCapture.refresh(true)')
                        page.wait_for_function('()=>document.getElementById("catalog-rating-status").textContent.includes("已核对，已保存 2 星")')
                        self.assertFalse(page.evaluate('(id)=>FramekeepArchiveCapture.pending(id)',self.item(2)))
                        self.assertEqual(page.evaluate('()=>window.__ratingWrites'),1)
                        # 明确拒绝仍回退到实际已确认的二星。
                        page.evaluate('''()=>{const api=FramekeepIO.api;FramekeepIO.api=(path,...args)=>path==='/api/archive-capture/rating'?Promise.reject(new Error('只读测试')):api(path,...args)}''')
                        page.locator('[data-catalog-rating="1"]').click()
                        page.wait_for_function('()=>document.getElementById("catalog-rating-status").textContent.includes("已恢复为2 星：只读测试")')
                        out=Path(__file__).resolve().parents[1]/'build/task227-rating-timeout';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'timeout-reconciled.png'))
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
