"""独立快捷索引：主库仅提供倒排、根状态，排序和内容不回查主业务大表。"""
from pathlib import Path
from contextlib import closing
from contextlib import contextmanager
import tempfile
import unittest
from unittest.mock import patch
import sqlite3
import hashlib
import subprocess
import sys
import threading

from asset_catalog import AssetCatalog
from quick_file_index import QuickFileIndex
from catalog_search_requests import SearchCancelled
import test_catalog_search_candidates as fixtures


class QuickFileIndexTests(unittest.TestCase):
    def test_first_search_starts_deferred_index_and_returns_results(self):
        from asset_intelligence import AssetIntelligence
        deferred=QuickFileIndex(self.catalog,folder=self.base/'deferred')
        service=AssetIntelligence.__new__(AssetIntelligence);service.catalog=self.catalog;service.quick_index=deferred
        try:
            self.assertFalse(deferred.ready);self.assertIsNone(deferred.thread)
            service.search(text='7877',quick=True)
            deferred.thread.join(.1)
            for _ in range(100):
                if deferred.ready:break
                deferred.stopped.wait(.02)
            self.assertTrue(deferred.ready,deferred.error)
            self.assertEqual(len(service.search(text='7877',quick=True)['rows']),60)
        finally:deferred.close()

    def test_concurrent_start_keeps_one_worker_and_closed_index_stays_closed(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def work():calls.append(1);entered.set();release.wait(3)
        with patch.object(self.index,'_run',side_effect=work):
            workers=[threading.Thread(target=self.index.start) for _ in range(12)]
            try:
                for worker in workers:worker.start()
                for worker in workers:worker.join(2)
                self.assertTrue(entered.wait(1));self.assertEqual(calls,[1])
            finally:release.set();self.index.close()
        self.assertFalse(self.index.start());self.assertEqual(calls,[1])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.catalog=AssetCatalog(self.base/'data')
        if self.catalog.fast_index._worker:self.catalog.fast_index._worker.join(5)
        with self.catalog.connect() as c:
            c.execute("INSERT INTO roots(id,path,created) VALUES('one','Z:/unavailable',0)")
            for number in range(100):
                name=f'LC7877/XSY_{number:04}.jpg' if number<70 else f'other/{number}.jpg'
                fixtures.CatalogSearchCandidateTests.add(c,str(number),'one',name,str(number),'')
            c.execute("UPDATE catalog_metadata SET search_text='7877 metadata-only' WHERE item='99'")
            c.execute("UPDATE items SET sku='LC7877',normalized='LC7877' WHERE id='98'")
        self.index=QuickFileIndex(self.catalog,folder=self.base/'ssd');self.index.build()
        while self.index.sync():pass

    def tearDown(self):
        self.index.close();self.catalog.close();self.temp.cleanup()

    def test_numeric_query_uses_file_evidence_and_stable_all_pages(self):
        result=self.index.query('7877',limit=17);actual=[]
        while True:
            actual += [r['id'] for r in result['rows']]
            if not result['has_more']:break
            result=self.index.query('7877',limit=17,cursor=result['next_cursor'])
        self.assertEqual(set(actual),{str(i) for i in range(70)}|{'98'})
        self.assertEqual(len(actual),71);self.assertNotIn('99',actual)

    def test_query_does_not_read_main_item_rows_or_metadata_payloads(self):
        original=sqlite3.connect;statements=[]
        def connect(*args,**kwargs):
            c=original(*args,**kwargs)
            if args and str(args[0]).startswith(self.index.path.as_uri()):c.set_trace_callback(statements.append)
            return c
        with patch('quick_file_index.sqlite3.connect',side_effect=connect):
            self.assertEqual(len(self.index.query('7877')['rows']),60)
        sql='\n'.join(statements).lower()
        self.assertNotIn('from items',sql);self.assertNotIn('catalog_metadata',sql)
        self.assertNotIn('attach database',sql)
        self.assertIn('quick_items',sql)

    def test_service_quick_route_bypasses_general_metadata_search(self):
        from asset_intelligence import AssetIntelligence
        service=AssetIntelligence.__new__(AssetIntelligence);service.catalog=self.catalog;service.quick_index=self.index
        with patch('asset_intelligence.index.query',side_effect=AssertionError('快捷查询不能回退全库元数据')):
            result=service.search(text='7877',quick=True)
            regular=service.search(text='7877')
        self.assertEqual(result['engine'],'quick-file-index');self.assertEqual(len(result['rows']),60)
        self.assertEqual(regular['engine'],'quick-file-index')

    def test_changes_delete_restart_and_noop_rescan(self):
        with self.catalog.connect() as c:
            c.execute('UPDATE items SET relative=relative')
            self.assertFalse(c.execute('SELECT 1 FROM quick_file_dirty LIMIT 1').fetchone())
            c.execute("UPDATE items SET relative='unique7877.jpg',sku='UNIQUE77',normalized='UNIQUE77' WHERE id='99'")
            c.execute("DELETE FROM items WHERE id='0'")
        while self.index.sync():pass
        self.assertEqual([r['id'] for r in self.index.query('unique7877')['rows']],['99'])
        self.assertNotIn('0',{r['id'] for r in self.index.query('7877')['rows']})
        reopened=QuickFileIndex(self.catalog,folder=self.base/'ssd')
        try:
            self.assertTrue(reopened.ready)
            self.assertEqual(reopened.query('UNIQUE77')['rows'][0]['id'],'99')
        finally:reopened.close()

    def test_mature_restart_is_read_only_while_index_writer_is_busy(self):
        # 实际持有写锁，防止相同 application_id 赋值或目录删写再次挡住启动。
        original=sqlite3.connect;statements=[]
        with closing(original(self.index.path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            def connect(*args,**kwargs):
                if args and args[0]==self.index.path:kwargs['timeout']=0
                c=original(*args,**kwargs)
                if args and args[0]==self.index.path:c.set_trace_callback(statements.append)
                return c
            try:
                with patch('quick_file_index.sqlite3.connect',side_effect=connect):
                    reopened=QuickFileIndex(self.catalog,folder=self.base/'ssd')
                try:
                    self.assertTrue(reopened.ready)
                    actual=reopened.query('7877');expected=self.index.query('7877')
                    actual.pop('elapsed_ms');expected.pop('elapsed_ms')
                    self.assertEqual(actual,expected)
                finally:reopened.close()
            finally:writer.rollback()
        changes=[sql for sql in statements if sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE'))
                 or sql.lower().startswith('pragma application_id=')]
        self.assertEqual(changes,[])

    def test_restart_refreshes_changed_scopes_and_rejects_old_cursor(self):
        page=self.index.query('7877',limit=1)
        with self.catalog.connect() as c:
            c.execute("INSERT INTO exclusions(root,folder) VALUES('one','LC7877')")
        reopened=QuickFileIndex(self.catalog,folder=self.base/'ssd')
        try:
            self.assertTrue(reopened.ready)
            self.assertEqual([r['id'] for r in reopened.query('7877')['rows']],['98'])
            with self.assertRaises(ValueError):reopened.query('7877',cursor=page['next_cursor'])
            self.assertFalse(reopened.sync_roots())
        finally:reopened.close()

    def test_startup_does_not_checkpoint_historical_index_wal_on_close(self):
        self.index.close()
        subprocess.run([sys.executable,'-c',"import sqlite3,os,sys;c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.execute('PRAGMA wal_autocheckpoint=0');c.execute('CREATE TABLE startup_probe(payload)');c.executemany('INSERT INTO startup_probe VALUES(?)',[(bytes(65536),)]*64);c.commit();os._exit(0)",str(self.index.path)],check=True,timeout=15)
        wal=Path(str(self.index.path)+'-wal')
        self.assertGreater(wal.stat().st_size,4_000_000)
        def digest():return tuple(hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.index.path,wal))
        before=digest();reopened=QuickFileIndex(self.catalog,folder=self.base/'ssd')
        try:self.assertTrue(reopened.ready);self.assertEqual(digest(),before)
        finally:reopened.close()

    def test_root_removal_syncs_scope_even_before_item_copy(self):
        self.index.query('7877');self.assertTrue(self.index.query('7877')['cache_hit'])
        with self.catalog.connect() as c:c.execute("UPDATE roots SET removed=1 WHERE id='one'")
        self.index.sync_roots()
        self.assertEqual(self.index.query('7877')['rows'],[])

    def test_catalog_remove_hides_folder_before_dirty_item_copy(self):
        from types import SimpleNamespace
        self.catalog.intelligence=SimpleNamespace(quick_index=self.index)
        self.index.query('7877')
        with patch.object(self.catalog,'folder_path',return_value=self.base):self.catalog.remove('one','LC7877')
        self.assertEqual([r['id'] for r in self.index.query('7877')['rows']],['98'])

    def test_folder_scope_keeps_literal_percent_and_underscore(self):
        from types import SimpleNamespace
        self.catalog.intelligence=SimpleNamespace(quick_index=self.index)
        with self.catalog.connect() as c:
            fixtures.CatalogSearchCandidateTests.add(c,'special','one','bucket%_q/bound.jpg','special','')
            fixtures.CatalogSearchCandidateTests.add(c,'similar','one','bucketAAq/bound.jpg','similar','')
        while self.index.sync():pass
        self.assertEqual(len(self.index.query('bound')['rows']),2)
        with patch.object(self.catalog,'folder_path',return_value=self.base):self.catalog.remove('one','bucket%_q')
        self.assertEqual([r['id'] for r in self.index.query('bound')['rows']],['similar'])

    def test_result_cache_returns_copies_and_content_changes_invalidate_it(self):
        first=self.index.query('7877');original=first['rows'][0]['name'];first['rows'][0]['name']='外部改动'
        cached=self.index.query('7877');self.assertTrue(cached['cache_hit']);self.assertEqual(cached['rows'][0]['name'],original)
        with self.catalog.connect() as c:c.execute("UPDATE items SET relative='changed-only.jpg' WHERE id='0'")
        self.index.sync();self.assertEqual([r['id'] for r in self.index.query('changed-only')['rows']],['0'])

    def test_hot_result_cache_does_not_open_sqlite_for_repeated_input(self):
        self.index.query('7877')
        with patch.object(self.index,'reader',side_effect=AssertionError('热结果不应打开 SQLite')):
            cached=self.index.query('7877')
        self.assertTrue(cached['cache_hit'])
        self.assertEqual(len(cached['rows']),60)

    def test_sqlite_budget_interrupt_does_not_start_endless_retry(self):
        with patch('literal_search.page_keys',side_effect=sqlite3.OperationalError('interrupted')):
            with self.assertRaisesRegex(ValueError,'时间预算'):self.index.query('uncached-7877')

    def test_file_queries_use_quick_route_without_main_database_preview_lookup(self):
        from asset_intelligence import AssetIntelligence
        service=AssetIntelligence.__new__(AssetIntelligence);service.catalog=self.catalog;service.quick_index=self.index
        with patch.object(self.catalog,'cached_preview_hints',side_effect=AssertionError('按键不能等待主库或预览库')):
            for text in ('LC','XSY','98.jpg','  LC7877  ','LC12','7'):
                self.assertEqual(service.search(text=text)['engine'],'quick-file-index')
        self.assertEqual(self.index.query('  LC7877  ')['rows'],self.index.query('LC7877')['rows'])

    def test_memory_index_queries_and_incremental_changes_survive_disk_reader_failure(self):
        self.index.build_memory();self.assertIsNotNone(self.index.memory)
        self.index.results.clear()
        with patch('quick_file_index.sqlite3.connect',side_effect=AssertionError('RAM 查询不能打开磁盘数据库')):
            self.assertEqual(len(self.index.query('7877')['rows']),60)
        with self.catalog.connect() as c:
            c.execute("UPDATE items SET relative='new-memory-key.jpg',sku='MEMORY99',normalized='MEMORY99' WHERE id='99'")
            c.execute("DELETE FROM items WHERE id='0'")
        while self.index.sync():pass
        self.assertEqual([r['id'] for r in self.index.query('new-memory-key')['rows']],['99'])
        self.assertNotIn('0',{r['id'] for r in self.index.query('7877',limit=100)['rows']})
        with self.catalog.connect() as c:c.execute("UPDATE roots SET removed=1 WHERE id='one'")
        self.index.sync_roots();self.assertEqual(self.index.query('MEMORY99')['rows'],[])

    def test_memory_write_failure_discards_stale_mirror(self):
        self.index.build_memory()
        with self.index.memory_write() as c:c.execute('INSERT INTO missing_table VALUES(1)')
        self.assertIsNone(self.index.memory)
        self.assertTrue(self.index.memory_error)
        self.assertEqual(len(self.index.query('7877')['rows']),60)

    def test_quick_search_defers_new_preview_work_without_waiting_for_cache_lock(self):
        from asset_intelligence import AssetIntelligence
        class Warmer:
            def __init__(self):self.entered=0
            @contextmanager
            def foreground(self):
                self.entered+=1;yield
        warmer=Warmer();self.catalog.preview_warmer=warmer
        service=AssetIntelligence.__new__(AssetIntelligence);service.catalog=self.catalog;service.quick_index=self.index
        result=service.search(text='7877',quick=True)
        self.assertEqual(result['engine'],'quick-file-index')
        self.assertEqual(warmer.entered,1)

    def test_sku_priority_pairing_filters_and_cancellation(self):
        with self.catalog.connect() as c:
            fixtures.CatalogSearchCandidateTests.add(c,'jpg','one','pair.jpg','paired','LC7877')
            fixtures.CatalogSearchCandidateTests.add(c,'raw','one','pair.CR3','paired','')
            c.execute("UPDATE items SET sku='LC78770',normalized='LC78770' WHERE id='97'")
        while self.index.sync():pass
        rows=self.index.query('LC7877')['rows']
        self.assertEqual([r['id'] for r in rows[:3]],['98','jpg','97'])
        self.assertEqual([r['priority'] for r in rows[:3]],[0,0,1]);self.assertNotIn('raw',{r['id'] for r in rows})
        self.assertEqual(self.index.query('7877',media='video')['rows'],[])
        with self.assertRaises(SearchCancelled):self.index.query('7877',cancel=lambda:True)

    def test_generation_invalidates_old_cursor_and_unowned_cache_is_preserved(self):
        page=self.index.query('7877',limit=1)
        with self.catalog.connect() as c:c.execute("UPDATE items SET modified=20 WHERE id='1'")
        self.index.sync()
        with self.assertRaises(ValueError):self.index.query('7877',cursor=page['next_cursor'])
        foreign=self.base/'foreign';foreign.mkdir();path=foreign/'quick_search.sqlite3'
        with closing(sqlite3.connect(path)) as c,c:c.execute('CREATE TABLE user_data(value)')
        before=path.read_bytes()
        with self.assertRaises(ValueError):QuickFileIndex(self.catalog,folder=foreign)
        self.assertEqual(path.read_bytes(),before)

    def test_idle_readers_do_not_pin_wal_and_cancel_does_not_poison_pool(self):
        self.index.query('7877')
        with self.index.reader() as c:self.assertFalse(c.in_transaction)
        with self.catalog.connect() as c:c.execute("UPDATE items SET modified=21 WHERE id='2'")
        self.index.sync()
        with closing(sqlite3.connect(self.index.path)) as c:
            self.assertEqual(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
        checks=iter((False,True))
        with self.assertRaises(SearchCancelled):self.index.query('7877',cancel=lambda:next(checks,True))
        self.assertEqual(len(self.index.query('7877')['rows']),60)


if __name__=='__main__':unittest.main()
