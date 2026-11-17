"""SKU 启动快照、来源失效和只读缩略图的集成回归。"""
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

import media_engine as media
import sku_library
from sku_library_index import SkuLibraryIndex,for_sku
import test_sku_range as fixtures
import test_sku_write_progress as browser_fixtures
from preview_cache import cache_for


class SkuIndexTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SkuRangeTests();self.fixture.setUp();self.f=self.fixture.f;self.index=for_sku(self.f.sku)
    def tearDown(self):self.fixture.tearDown()

    def test_persisted_projection_reopens_in_new_process_without_project_or_photo_probe(self):
        self.assertTrue(self.index.refresh());self.index.close()
        code='''from pathlib import Path
from types import SimpleNamespace
import threading,json,sys
from unittest.mock import patch
from sku_library_index import SkuLibraryIndex,Reader
base=Path(sys.argv[1]);sku=SimpleNamespace(db=base/'sku.sqlite3',store=SimpleNamespace(db=base/'framekeep.sqlite3',data=base),archive=SimpleNamespace(db=base/'asset_archive.sqlite3'),_library_schema_ready=True)
index=SkuLibraryIndex(sku)
try:
    with patch('sku_library.project',side_effect=AssertionError('不得重新构建')),patch('pathlib.Path.is_file',side_effect=AssertionError('不得探测原片')):
        result=index.browse(code='A-100');assert result['total']==4 and not result['loading'];print(json.dumps({'rows':len(result['rows']),'groups':len(result['groups'])}))
finally:index.close()
'''
        result=subprocess.run([sys.executable,'-X','utf8','-c',code,str(self.f.store.data)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout)['rows'],4)

    def test_correction_and_scope_metadata_are_current_after_fresh_read(self):
        self.index.refresh();self.fixture.act(self.fixture.body((2,3)))
        data=self.index.browse(code='B-200',fresh=True)
        self.assertEqual(data['total'],2);self.assertFalse(data['refreshing'])
        self.assertEqual({r['sku']:r['count'] for r in data['groups']},{'A-100':2,'B-200':2})
        from sku_library_management import manage
        folder=manage(self.f.sku,self.f.archive,{'action':'create_bin','name':'新组'})['bin_id']
        manage(self.f.sku,self.f.archive,{'action':'move','sku':'B-200','bin_id':folder})
        self.assertEqual(self.index.browse(bin_id=folder)['total'],2)
        manage(self.f.sku,self.f.archive,{'action':'remove','sku':'B-200'})
        self.assertEqual(self.index.browse()['total'],2)
        self.assertEqual(self.index.browse(bin_id='trash')['total'],2)

    def test_concurrent_source_change_cannot_publish_old_projection(self):
        self.index.refresh();before=self.index.path.read_bytes();project=sku_library.project
        def changed(reader):
            value=project(reader)
            with self.f.archive.connect() as c:c.execute("UPDATE assets SET sku='AFTER-900',normalized='AFTER900' WHERE id=(SELECT asset FROM catalog_links LIMIT 1)")
            return value
        # 强制一次语义变化，使本轮确实进入重建。
        with self.f.store.connect() as c:c.execute('UPDATE captures SET position=position+1 WHERE session=?',(self.f.sid,))
        with patch('sku_library.project',side_effect=changed):self.assertFalse(self.index.refresh())
        self.assertEqual(before,self.index.path.read_bytes())
        self.assertTrue(self.index.refresh());self.assertEqual(self.index.browse(code='AFTER-900')['total'],1)

    def test_cached_read_ignores_writer_lock_and_ui_state_does_not_invalidate(self):
        self.index.refresh();signature=self.index.signature()
        session=self.f.store.session(self.f.sid);state=session['state'];state['selectedId']='ui-only'
        self.f.store.save(self.f.sid,session['revision'],state)
        with self.f.store.connect() as c:c.execute("INSERT INTO settings VALUES ('unrelated_ui_setting','1')")
        self.assertEqual(signature,self.index.signature())
        with self.f.sku.connect() as c:
            c.execute('BEGIN IMMEDIATE');c.execute("UPDATE settings SET value=value WHERE key='active'")
            started=time.perf_counter()
            with patch('sku_library.project',side_effect=AssertionError('不得构建')),patch('pathlib.Path.is_file',side_effect=AssertionError('不得探测')):data=self.index.browse(code='A-100')
            self.assertEqual(data['total'],4);self.assertLess(time.perf_counter()-started,2)

    def test_ocr_auxiliary_search_and_natural_pagination_keep_original_semantics(self):
        self.index.refresh()
        for query in ['COTTON','红色','261009','" OR anything*']:
            expected=sku_library.browse(self.f.sku,search=query)
            actual=self.index.browse(search=query)
            self.assertEqual([r['id'] for r in actual['rows']],[r['id'] for r in expected['rows']],query)
        expected=sku_library.browse(self.f.sku,order='name_desc',offset=1,limit=2)
        self.assertEqual([r['id'] for r in self.index.browse(order='name_desc',offset=1,limit=2)['rows']],[r['id'] for r in expected['rows']])

    def test_optional_thumbnail_index_failure_does_not_hide_known_skus(self):
        import sqlite3
        with patch.object(self.index,'_preview_hints',side_effect=sqlite3.OperationalError('缓存索引繁忙')):self.assertTrue(self.index.refresh())
        self.assertEqual(self.index.browse(code='A-100')['total'],4)

    def test_legacy_snapshot_is_visible_then_upgraded_and_persisted(self):
        from sku_library_index import SCHEMA
        self.index.refresh();saved=json.loads(self.index.path.read_bytes());self.index.close()
        for schema in ('framekeep.sku-browser-v1','framekeep.sku-browser-v2','framekeep.sku-browser-v3','framekeep.sku-browser-v4'):
            with self.subTest(schema=schema):
                saved['schema']=schema;self.index.path.write_text(json.dumps(saved),encoding='utf8')
                index=SkuLibraryIndex(self.f.sku)
                try:
                    with patch.object(index,'start'):
                        initial=index.browse(code='A-100')
                    self.assertFalse(initial['loading']);self.assertTrue(initial['refreshing']);self.assertEqual(initial['total'],4)
                    self.assertTrue(index.refresh())
                    self.assertEqual(json.loads(index.path.read_bytes())['schema'],SCHEMA)
                    reopened=SkuLibraryIndex(self.f.sku)
                    try:
                        with patch.object(reopened,'start'),patch('sku_library.project',side_effect=AssertionError('重启不得重建')):
                            self.assertEqual(reopened.browse(code='A-100')['total'],4)
                    finally:reopened.close()
                finally:index.close()

    def test_repeated_query_reuses_groups_but_management_and_search_stay_current(self):
        from sku_library_management import manage
        self.index.refresh()
        with patch('sku_library.browse',wraps=sku_library.browse) as browse:
            self.assertEqual(self.index.browse(code='A-100')['total'],4)
            self.assertEqual(self.index.browse(code='A-100')['total'],4)
            self.assertEqual(browse.call_count,1)
            manage(self.f.sku,self.f.archive,{'action':'remove','sku':'A-100'})
            calls=browse.call_count
            self.assertEqual(self.index.browse(code='A-100')['total'],0)
            self.assertEqual(browse.call_count,calls+1)
            self.assertEqual(self.index.browse(bin_id='trash')['total'],4)

    def test_snapshot_write_failure_keeps_list_and_unknown_file_is_preserved(self):
        self.index.path.parent.mkdir(parents=True,exist_ok=True)
        self.index.path.write_bytes(b'unknown user file')
        self.assertTrue(self.index.refresh());self.assertEqual(self.index.path.read_bytes(),b'unknown user file')
        self.index.path.unlink()
        self.index.value=None
        with patch('sku_library_index.atomic_write',side_effect=OSError('disk full')):self.assertTrue(self.index.refresh())
        with patch.object(self.index,'start'):self.assertEqual(self.index.browse(code='A-100')['total'],4)
        self.assertIn('disk full',self.index.error)

    def test_shared_rating_changes_refresh_projected_badges(self):
        ratings=self.f.archive.ratings;self.index.refresh()
        ratings.set([str(self.f.photos/'0.JPG')],5)
        rows=self.index.browse(code='A-100',fresh=True)['rows']
        self.assertEqual(next(r for r in rows if r['capture_id']==self.f.ids[0])['rating'],5)

    def test_cached_thumbnail_is_served_offline_without_store_or_cache_write_lock(self):
        import server
        f=self.f;record=f.store.records(f.sid,ids=[f.ids[0]])[0]
        expected=media.preview_bytes(record,f.photos,f.store.data/'previews',lambda:media.find_exiftool(f.store.root),420)
        large=media.preview_bytes(record,f.photos,f.store.data/'previews',lambda:media.find_exiftool(f.store.root),2048)
        self.index.refresh();row=self.index.browse(code='A-100')['rows'][0]
        self.assertIn('/api/sku/library/preview/',row['preview'])
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        with ExitStack() as stack:
            stack.enter_context(patch.object(server,'STORE',f.store));stack.enter_context(patch.object(server,'DATA',f.store.data))
            stack.enter_context(patch.object(f.store,'session',side_effect=AssertionError('命中不得回查拍摄表')))
            threading.Thread(target=http.serve_forever,daemon=True).start()
            entered=threading.Event();release=threading.Event();cache=cache_for(f.store.data/'previews')
            def hold():
                with cache.lock:entered.set();release.wait(timeout=8)
            worker=threading.Thread(target=hold);worker.start();entered.wait(timeout=2)
            try:
                for size,payload in [(420,expected),(2048,large)]:
                    with urllib.request.urlopen(f'http://127.0.0.1:{http.server_port}'+row['preview']+'&size='+str(size),timeout=3) as response:self.assertEqual(response.read(),payload)
            finally:release.set();worker.join();http.shutdown();http.server_close()


class SkuStartupBrowserTests(unittest.TestCase):
    def test_empty_database_finishes_loading_without_a_permanent_wait_message(self):
        fixture=browser_fixtures.SkuWriteProgressTests();fixture.setUp()
        try:
            with fixture.f.sku.connect() as c:
                for table in ('ocr_choices','jobs','parse_jobs'):c.execute('DELETE FROM '+table)
            with fixture.f.archive.connect() as c:
                c.execute('DELETE FROM assignments');c.execute("UPDATE assets SET sku='',normalized='' ")
            with fixture.browser() as page:
                page.locator('#sku-database-open').click()
                page.wait_for_function('()=>document.getElementById("sku-database-list").textContent.includes("此处暂无 SKU")')
                self.assertNotIn('正在读取',page.locator('#sku-library-count').inner_text())
        finally:fixture.tearDown()

    def test_first_group_automatic_restore_and_inactive_progress_polling(self):
        fixture=browser_fixtures.SkuWriteProgressTests();fixture.setUp()
        try:
            f=fixture.f;fixture.fixture.act(fixture.fixture.body((2,3)));fixture.fixture.drain()
            with fixture.browser() as page:
                calls=[];page.on('request',lambda r:calls.append(r.url) if r.url.endswith('/api/sku') else None)
                page.locator('#sku-database-open').click()
                page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===2')
                self.assertIn('A-100',page.locator('#sku-database-title').inner_text())
                page.locator('#sku-database-list [data-db-sku="B200"]').click()
                page.wait_for_function('()=>document.getElementById("sku-database-title").textContent.includes("B-200")')
                before=len(calls);page.wait_for_timeout(1300);self.assertEqual(before,len(calls))
                page.reload(wait_until='domcontentloaded');page.wait_for_function('()=>!window.FramekeepBoot?.loading')
                page.wait_for_function('()=>document.querySelectorAll("#sku-database-grid .catalog-card").length===2&&document.getElementById("sku-database-title").textContent.includes("B-200")')
                self.assertEqual(page.locator('#sku-database-open').get_attribute('aria-selected'),'true')
                out=Path(__file__).resolve().parents[2];page.screenshot(path=str(out/'sku-startup-restored.png'))
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
