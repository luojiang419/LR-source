"""SKU 媒体池持久管理与整款改名，所有文件来自隔离夹具。"""
import copy
import json
import unittest
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import threading
from unittest.mock import patch
from playwright.sync_api import sync_playwright

import test_ocr_metadata as fixtures
from sku_library import browse,labels
from sku_library_management import manage
from sku_service import SkuService
from metadata_writer import sha


class MediaPoolTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.OcrMetadataTests();self.f.setUp();f=self.f
        f.parse();f.choose('A-100');f.sku.parse_capture(f.sid,f.ids[3]);f.choose('B-200',f.ids[3]);self.drain()
    def tearDown(self):self.f.tearDown()
    def drain(self):
        for _ in range(200):
            with self.f.archive.connect() as c:
                if not c.execute("SELECT 1 FROM metadata_jobs WHERE status='queued'").fetchone():return
            self.f.archive.process_one()
        self.fail('队列未完成')
    def act(self,action,**body):return manage(self.f.sku,self.f.archive,dict(action=action,**body))
    def hashes(self):return [sha(self.f.photos/f'{i}.JPG') for i in range(4)]
    def test_bins_moves_nested_delete_and_restart_preserve_files(self):
        before=self.hashes();parent=self.act('create_bin',name='春季')['bin_id'];child=self.act('create_bin',name='模特图',parent_id=parent)['bin_id']
        self.act('move',sku='A-100',bin_id=child)
        self.assertEqual([r['sku'] for r in browse(self.f.sku,bin_id=parent)['groups']],['A-100'])
        self.assertEqual([r['sku'] for r in browse(self.f.sku,bin_id='ungrouped')['groups']],['B-200'])
        reopened=SkuService(self.f.store,self.f.providers,archive=self.f.archive,autostart=False,require_sku_choice=True)
        try:self.assertEqual(browse(reopened,bin_id=child)['total'],2)
        finally:reopened.close()
        self.act('rename_bin',bin_id=child,name='新版模特图');self.act('delete_bin',bin_id=child)
        self.assertEqual(browse(self.f.sku,bin_id=parent)['total'],2)
        self.act('delete_bin',bin_id=parent);self.assertEqual(browse(self.f.sku,bin_id='ungrouped')['total'],4)
        self.assertEqual(before,self.hashes())
    def test_removal_survives_readings_and_restores_original_bin(self):
        before=self.hashes();folder=self.act('create_bin',name='拍摄组')['bin_id'];self.act('move',sku='A-100',bin_id=folder);self.act('remove',sku='A-100')
        self.assertEqual(browse(self.f.sku)['total'],2);self.assertEqual(browse(self.f.sku)['interval_count'],1);self.assertEqual(browse(self.f.sku,bin_id='trash')['total'],2)
        self.f.parse();self.assertEqual(browse(self.f.sku,code='A-100')['total'],0)
        self.act('restore',sku='A-100');self.assertEqual(browse(self.f.sku,bin_id=folder)['total'],2)
        self.assertEqual(before,self.hashes())
    def test_rename_writes_every_member_keeps_other_sku_and_bin(self):
        f=self.f;folder=self.act('create_bin',name='已拍')['bin_id'];self.act('move',sku='A-100',bin_id=folder);before=self.hashes()
        result=self.act('rename',sku='A-100',new_sku='A-900')
        self.assertEqual(result['capture_count'],2);self.assertEqual(before,self.hashes());self.drain()
        for i in range(2):self.assertEqual(f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'],'A-900')
        self.assertEqual(before[2:],self.hashes()[2:])
        self.assertEqual([r['sku'] for r in browse(f.sku,bin_id=folder)['groups']],['A-900'])
        self.assertEqual(labels(f.sku,f.sid)['labels'],dict(zip(f.ids,['A-900','A-900','B-200','B-200'])))
        repeated=self.act('rename',sku='A-100',new_sku='A-900');self.assertEqual(result['operation_ids'],repeated['operation_ids'])
    def test_all_same_sku_intervals_rename_without_merging_boundaries(self):
        f=self.f;f.choose('A-100',f.ids[3]);self.drain()
        with f.archive.connect() as c:segments={r[0] for r in c.execute("SELECT id FROM segments WHERE status='confirmed'")}
        result=self.act('rename',sku='A-100',new_sku='WHOLE-300');self.assertEqual(result['capture_count'],4);self.drain()
        for i in range(4):self.assertEqual(f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'],'WHOLE-300')
        with f.archive.connect() as c:self.assertEqual(segments,{r[0] for r in c.execute("SELECT id FROM segments WHERE status='confirmed'")})
        f.choose('FIRST-ONLY');self.drain()
        self.assertEqual([f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'] for i in range(4)],['FIRST-ONLY','FIRST-ONLY','WHOLE-300','WHOLE-300'])
    def test_conflicts_and_invalid_targets_do_not_change_files(self):
        before=self.hashes()
        with self.assertRaisesRegex(ValueError,'已存在'):self.act('rename',sku='A-100',new_sku='B-200')
        with self.assertRaisesRegex(ValueError,'有效'):self.act('rename',sku='A-100',new_sku='')
        with self.assertRaisesRegex(ValueError,'不存在'):self.act('move',sku='A-100',bin_id='missing')
        folder=self.act('create_bin',name='重复')['bin_id']
        with self.assertRaisesRegex(ValueError,'同名组'):self.act('create_bin',name='重复')
        self.assertEqual(before,self.hashes());self.assertEqual(browse(self.f.sku)['organization']['bins'][0]['id'],folder)
    def test_failed_multi_interval_rename_can_resume(self):
        f=self.f;f.choose('A-100',f.ids[3]);self.drain();original=f.archive.apply;calls=[]
        def fail_second(pid):
            calls.append(pid)
            if len(calls)==2:raise ValueError('隔离测试中断')
            return original(pid)
        with patch.object(f.archive,'apply',side_effect=fail_second):
            with self.assertRaisesRegex(ValueError,'测试中断'):self.act('rename',sku='A-100',new_sku='RESUME-300')
        result=self.act('rename',sku='A-100',new_sku='RESUME-300');self.assertEqual(result['capture_count'],4);self.drain()
        for i in range(4):self.assertEqual(f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'],'RESUME-300')
    def test_bulk_rename_is_not_limited_to_first_browser_page(self):
        f=self.f;template=browse(f.sku,code='A-100')['rows'][0]
        rows=[dict(template,id=f'bulk:{i}',capture_id=f'c{i}',name=f'{i}.JPG',relative=f'{i}.JPG',path=str(f.photos/f'{i}.JPG'),sku='BULK-100',normalized='BULK100') for i in range(260)]
        requested=[]
        def preview(*args,**kwargs):requested.extend(kwargs['asset_ids']);return {'preview_id':'mock-bulk'}
        with patch('sku_library.interval_rows',return_value=(rows,[])),patch('sku_library.onsite_assigned_rows',return_value=[]),patch('sku_library.archived_rows',return_value=[]),patch.object(f.archive,'ensure',side_effect=lambda sid,cid:cid),patch.object(f.archive,'asset',return_value={'scope':'模特图','project':f.pid,'segment':'','source':'manual'}),patch.object(f.archive,'preview',side_effect=preview),patch.object(f.archive,'apply',return_value={'operation':{'id':'mock-operation'}}):
            result=self.act('rename',sku='BULK-100',new_sku='BULK-200')
        self.assertEqual(result['capture_count'],260);self.assertEqual(len(set(requested)),260)
    def test_browser_drag_rename_remove_restore_and_theme_layout(self):
        import server
        f=self.f;http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        values=dict(STORE=f.store,DATA=f.store.data,MODELS=f.providers,SKU=f.sku,ARCHIVE=f.archive,CATALOG=None,ANALYSIS=None,WATCH=None,MOUSE=None,CANON=None,RAWLINK=None,FILESEARCH=None,UPDATES=None,COMPONENTS=None,DRIVER=None)
        with ExitStack() as stack:
            for key,value in values.items():stack.enter_context(patch.object(server,key,value))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1680,'height':1050},reduced_motion='reduce');errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}',wait_until='networkidle');page.locator('#sku-database-open').click()
                        page.wait_for_selector('#sku-database-list [data-db-sku="A100"]')
                        page.locator('#sku-bin-create').click();page.locator('#sku-pool-name').fill('秋季拍摄');page.locator('#sku-pool-submit').click()
                        page.wait_for_function('()=>document.getElementById("sku-pool-status").textContent.includes("已创建组")')
                        folder=browse(f.sku)['organization']['bins'][0]['id'];before=self.hashes()
                        page.locator('[data-sku-bin=""]').click();page.wait_for_selector('#sku-database-list [data-db-sku="A100"]')
                        page.locator('#sku-database-list [data-db-sku="A100"]').drag_to(page.locator('[data-sku-bin="'+folder+'"]'))
                        page.wait_for_function('()=>document.getElementById("sku-pool-status").textContent.includes("已移入")')
                        self.assertEqual(before,self.hashes());page.locator('[data-sku-bin="'+folder+'"]').click()
                        page.wait_for_function('()=>document.querySelectorAll("#sku-database-list [data-db-sku]").length===1')
                        page.locator('#sku-database-list [data-db-sku="A100"]').dblclick();page.wait_for_selector('#sku-pool-editor[open]')
                        page.locator('#sku-pool-name').fill('A-900');page.locator('#sku-pool-name').press('Enter')
                        page.wait_for_function('()=>!document.getElementById("sku-pool-editor").open')
                        self.drain();page.wait_for_selector('#sku-database-list [data-db-sku="A900"]')
                        page.wait_for_function('()=>document.getElementById("sku-pool-status").textContent.includes("已写入 2 个文件")')
                        for i in range(2):self.assertEqual(f.archive.writer.read(f.photos/f'{i}.JPG')['XMP-fk:SKU'],'A-900')
                        self.assertEqual(before[2:],self.hashes()[2:]);after=self.hashes()
                        out=fixtures.ROOT/'build/sku-media-pool-acceptance';out.mkdir(parents=True,exist_ok=True)
                        for theme in ['dark','light','pure-white','hardcore']:
                            page.evaluate('(theme)=>FramekeepTheme.set(theme)',theme)
                            for width in [1680,1024,600,375]:
                                page.set_viewport_size({'width':width,'height':1050});self.assertFalse(page.evaluate('()=>document.documentElement.scrollWidth>innerWidth'),(theme,width))
                                self.assertTrue(page.locator('#sku-bin-tree').is_visible());self.assertTrue(page.locator('#sku-database-list').is_visible())
                                if width==1680:page.screenshot(path=str(out/f'pool-{theme}.png'))
                        page.set_viewport_size({'width':1680,'height':1050})
                        page.locator('#sku-database-grid .catalog-card').first.dblclick();page.wait_for_selector('#asset-image-viewer[open]');page.keyboard.press('Escape')
                        page.locator('#sku-database-list [data-db-sku="A900"]').click(button='right');page.locator('#sku-pool-menu [data-pool-action="remove"]').click()
                        page.wait_for_function('()=>document.getElementById("sku-pool-status").textContent.includes("已移入“已移除”")')
                        self.assertEqual(after,self.hashes());page.locator('[data-sku-bin="trash"]').click();page.wait_for_selector('#sku-database-list [data-db-sku="A900"]')
                        page.locator('#sku-database-list [data-db-sku="A900"]').click(button='right');page.locator('#sku-pool-menu [data-pool-action="restore"]').click()
                        page.wait_for_function('()=>document.getElementById("sku-pool-status").textContent.includes("已恢复")')
                        self.assertEqual(after,self.hashes());page.reload(wait_until='networkidle');page.locator('#sku-database-open').click()
                        page.locator('[data-sku-bin="'+folder+'"]').click();page.wait_for_selector('#sku-database-list [data-db-sku="A900"]')
                        self.assertFalse(errors,errors)
                    finally:browser.close()
            finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
