"""范围修正使用隔离照片，覆盖人工保护、重复请求和实际浏览器选择。"""
import json
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import test_ocr_metadata as fixtures
from sku_library import browse,labels
from sku_library_management import manage
from metadata_writer import sha


class SkuRangeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.OcrMetadataTests();self.f.setUp();f=self.f
        session=f.store.session(f.sid);state=session['state'];state['overrides']={f.ids[3]:2};f.store.save(f.sid,session['revision'],state)
        f.sku.parse_capture(f.sid,f.ids[3]);f.choose('A-100',f.ids[3]);self.drain()

    def tearDown(self):self.f.tearDown()

    def drain(self):
        for _ in range(200):
            with self.f.archive.connect() as c:
                if not c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued'").fetchone():return
            self.f.archive.process_one()
        self.fail('元数据队列未排完')

    def body(self,indices=(1,2),new='B-200',request='range-test'):
        rows={r['capture_id']:r for r in browse(self.f.sku)['rows']}
        return {'action':'correct_selection','request_id':request,'new_sku':new,'items':[{'id':rows[self.f.ids[i]]['id'],'sku':rows[self.f.ids[i]]['sku']} for i in indices]}

    def act(self,body):return manage(self.f.sku,self.f.archive,body)

    def test_split_updates_both_navigation_projections_and_only_selected_files(self):
        f=self.f;before=[sha(f.photos/f'{i}.JPG') for i in range(4)]
        result=self.act(self.body());self.assertEqual(result['capture_count'],2)
        self.assertEqual(dict((r['sku'],r['count']) for r in browse(f.sku)['groups']),{'A-100':2,'B-200':2})
        folder=browse(f.sku)['roots'][0]['folders'][0]
        self.assertEqual({r['sku']:r['count'] for r in folder['skus']},{'A-100':2,'B-200':2})
        self.drain()
        self.assertEqual([f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'] for i in range(4)],['A-100','B-200','B-200','A-100'])
        self.assertEqual(before[0],sha(f.photos/'0.JPG'));self.assertEqual(before[3],sha(f.photos/'3.JPG'))
        self.assertEqual(sorted(p.name for p in f.photos.glob('*.JPG')),['0.JPG','1.JPG','2.JPG','3.JPG'])
        # 再次识别不会把人工修正扩散到其他图片。
        f.sku.parse_capture(f.sid,f.ids[3]);self.drain();self.assertEqual(labels(f.sku,f.sid)['labels'],dict(zip(f.ids,['A-100','B-200','B-200','A-100'])))
        reopened=fixtures.SkuService(f.store,f.providers,archive=f.archive,autostart=False,require_sku_choice=True)
        try:self.assertEqual(browse(reopened,code='B-200')['total'],2)
        finally:reopened.close()

    def test_selected_end_label_does_not_relabel_unselected_prefix(self):
        f=self.f;self.act(self.body((2,3)));self.drain()
        f.sku.parse_capture(f.sid,f.ids[3]);self.drain()
        self.assertEqual(labels(f.sku,f.sid)['labels'],dict(zip(f.ids,['A-100','A-100','B-200','B-200'])))
        self.assertEqual([f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'] for i in range(4)],['A-100','A-100','B-200','B-200'])

    def test_stale_or_invalid_selection_is_rejected_before_assignment(self):
        f=self.f
        for change in ({'items':[]},{'items':[{'id':'missing','sku':'A-100'}]},{'new_sku':''},{'request_id':'../bad'}):
            body=self.body();body.update(change)
            with self.assertRaises(ValueError):self.act(body)
        stale=self.body();stale['items'][0]['sku']='STALE'
        with self.assertRaisesRegex(ValueError,'已变化'):self.act(stale)
        self.assertEqual(browse(f.sku,code='A-100')['total'],4)

    def test_paired_raw_keeps_bytes_and_writes_sidecar_with_selected_jpg(self):
        f=self.f;raw=f.photos/'1.ARW';raw.write_bytes(b'isolated RAW payload'*100);before=sha(raw)
        record=f.store.records(f.sid,ids=[f.ids[1]])[0];record['raw']=raw.name
        with f.store.connect() as c:c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(json.dumps(record),f.sid,f.ids[1]))
        self.act(self.body((1,)));self.drain()
        self.assertEqual(sha(raw),before)
        self.assertEqual(f.archive.writer.read(raw.with_suffix('.xmp'))['XMP-fk:SKU'],'B-200')
        self.assertEqual(f.archive.writer.read(f.photos/'1.JPG')['XMP-fk:SKU'],'B-200')

    def test_duplicate_request_does_not_write_again_and_can_use_existing_target(self):
        first=self.act(self.body((1,),'B-200'));self.drain()
        repeated=self.act(self.body((2,),'B-200'))
        self.assertEqual(first['operation_ids'],repeated['operation_ids'])
        self.assertEqual(browse(self.f.sku,code='B-200')['total'],1)
        self.act(self.body((2,),'B-200','second-request'));self.drain()
        self.assertEqual(browse(self.f.sku,code='B-200')['total'],2)

    def test_interrupted_multi_scope_request_resumes_without_overwriting_completed_group(self):
        f=self.f;aid=f.archive.ensure(f.sid,f.ids[2])
        with f.archive.connect() as c:c.execute("UPDATE assets SET scope='细节图' WHERE id=?",(aid,))
        body=self.body();apply=f.archive.apply;calls=[]
        def interrupted(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:raise ValueError('隔离提交中断')
            return apply(*args,**kwargs)
        with patch.object(f.archive,'apply',side_effect=interrupted):
            with self.assertRaisesRegex(ValueError,'提交中断'):self.act(body)
        result=self.act({'action':'correct_selection','request_id':body['request_id']});self.drain()
        self.assertEqual(result['capture_count'],2);self.assertEqual(len(result['operation_ids']),2)
        self.assertEqual(browse(f.sku,code='B-200')['total'],2)
        with f.archive.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM operations WHERE source LIKE 'library-correction:%'").fetchone()[0],2)

    def test_browser_shift_drag_cross_page_menu_and_correction(self):
        import server
        f=self.f;http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=f.store,DATA=f.store.data,MODELS=f.providers,SKU=f.sku,ARCHIVE=f.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            reveal=stack.enter_context(patch('file_reveal.reveal_path'))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':900},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                        page.locator('#sku-database-open').click();page.locator('#sku-database-list [data-db-sku="A100"]').click()
                        cards=page.locator('#sku-database-grid .catalog-card');page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===4')
                        cards.nth(1).click();cards.nth(2).click(modifiers=['Shift'])
                        self.assertEqual(page.locator('#sku-database-grid .catalog-card.active').count(),2)
                        cards.nth(1).click(button='right');self.assertEqual(page.locator('#sku-pool-menu button').all_text_contents(),['打开所在路径','修改 SKU 款号'])
                        self.assertEqual(page.locator('#sku-database-grid .catalog-card.active').count(),2)
                        page.locator('[data-pool-action="reveal"]').click();page.wait_for_timeout(100);self.assertTrue(reveal.called)
                        cards.nth(1).click(button='right');page.locator('[data-pool-action="correct_selection"]').click()
                        self.assertIn('已选 2 张',page.locator('#sku-pool-editor-help').inner_text());page.locator('#sku-pool-name').fill('B-200');page.locator('#sku-pool-submit').click()
                        page.wait_for_function('()=>document.getElementById("sku-write-view").dataset.state==="writing"');self.drain()
                        page.wait_for_function('()=>!document.getElementById("sku-pool-editor").open');page.wait_for_selector('#sku-database-list [data-db-sku="B200"]')
                        self.assertEqual(browse(f.sku,code='A-100')['total'],2)
                        page.locator('#sku-view-tree').click();page.wait_for_selector('#sku-database-tree [data-db-sku="B200"]')
                        self.assertEqual(page.locator('#sku-database-tree [data-db-sku="B200"]').count(),1)
                        # 用独立 HTTP 响应夹具覆盖 120 张分页和滚动，不向业务库注入虚构图片。
                        from urllib.parse import urlparse,parse_qs
                        template=browse(f.sku);row=template['rows'][0]
                        rows=[dict(row,id=f'range-{i}',name=f'{i:03}.JPG',sku='A-100',normalized='A100') for i in range(245)]
                        def route_library(route):
                            q=parse_qs(urlparse(route.request.url).query);offset=int(q.get('offset',['0'])[0]);limit=int(q.get('limit',['120'])[0])
                            route.fulfill(json=dict(template,groups=[{'sku':'A-100','normalized':'A100','count':245,'created':row['created']}],roots=[],rows=rows[offset:offset+limit],offset=offset,total=245,has_more=offset+limit<245))
                        page.route('**/api/sku/library?*',route_library)
                        page.locator('#sku-view-flat').click();page.locator('#sku-database-list [data-db-sku="A100"]').click()
                        page.wait_for_selector('[data-catalog-item="range-0"]');page.locator('[data-catalog-item="range-110"]').click()
                        page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===245')
                        self.assertEqual(page.locator('#sku-database-prev,#sku-database-next').count(),0)
                        page.locator('[data-catalog-item="range-125"]').click(modifiers=['Shift'])
                        page.wait_for_function('()=>FramekeepSkuLibrary.selectedRows.length===16')
                        self.assertEqual(page.evaluate('()=>FramekeepSkuLibrary.selectedRows.map(r=>r.id)'),[f'range-{i}' for i in range(110,126)])
                        # Shift 拖动扩展，滚动到底仍持续选取，反向拖动收缩。
                        page.locator('[data-catalog-item="range-120"]').click();first=page.locator('[data-catalog-item="range-121"]').bounding_box();last=page.locator('[data-catalog-item="range-127"]').bounding_box()
                        page.keyboard.down('Shift');page.mouse.move(first['x']+20,first['y']+20);page.mouse.down();page.mouse.move(last['x']+20,last['y']+20,steps=8);page.mouse.up();page.keyboard.up('Shift')
                        page.wait_for_function('()=>FramekeepSkuLibrary.selectedRows.length===8')
                        grid=page.locator('#sku-database-grid').bounding_box()
                        page.keyboard.down('Shift');page.mouse.move(first['x']+20,first['y']+20);page.mouse.down();page.mouse.move(grid['x']+grid['width']/2,grid['y']+grid['height']-8,steps=5);page.wait_for_timeout(450);page.mouse.up();page.keyboard.up('Shift')
                        self.assertGreater(page.locator('#sku-database-grid').evaluate('(e)=>e.scrollTop'),0)
                        self.assertGreater(page.evaluate('()=>FramekeepSkuLibrary.selectedRows.length'),8)
                        out=fixtures.ROOT/'build/sku-range-correction';out.mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(out/'range-selection.png'))
                        active=page.locator('#sku-database-grid .catalog-card.active').last;active.scroll_into_view_if_needed();active.click(button='right');page.screenshot(path=str(out/'image-menu.png'))
                        page.locator('[data-pool-action="correct_selection"]').click();page.screenshot(path=str(out/'correction-dialog.png'));page.locator('[data-pool-close]').first.click()
                        page.unroute('**/api/sku/library?*',route_library)
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
