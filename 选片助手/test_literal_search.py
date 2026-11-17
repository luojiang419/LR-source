"""真实搜索路径的短词、宽词、实时更新和分页回归；不读取原照片。"""
from pathlib import Path
import sqlite3
import tempfile
import unittest
import time

from asset_catalog import AssetCatalog
from retrieval_store import install, query
import test_catalog_search_candidates as fixtures


class LiteralSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = AssetCatalog(Path(self.temp.name))
        if self.catalog.fast_index._worker:self.catalog.fast_index._worker.join(5)
        self.catalog.intelligence = object()
        with self.catalog.connect() as c:
            install(c)
            c.execute("INSERT INTO roots(id,path,created) VALUES('one','Z:/unavailable',0)")
            for number, name in enumerate(('蓝色/款100.jpg','春夏/款101.jpg','plain102.jpg','plain103.CR3','plain103.jpg','last104.jpg')):
                pair='pair' if '103' in name else str(number)
                fixtures.CatalogSearchCandidateTests.add(c,str(number),'one',name,pair,'')
            c.execute("UPDATE catalog_metadata SET search_text='蓝' WHERE item='2'")
            c.execute("UPDATE catalog_metadata SET search_text='春夏' WHERE item='3'")
        self.catalog.literal_index.build()
        while self.catalog.literal_index.process():pass

    def tearDown(self):
        self.catalog.close(); self.temp.cleanup()

    def expected(self, text):
        with self.catalog.connect() as c:
            condition, args = self.catalog.search_condition(c,text)
            return [r['id'] for r in c.execute('SELECT id FROM capture_items WHERE '+condition+' ORDER BY modified DESC,id',args)]

    def test_short_words_and_small_documents_keep_literal_pair_semantics(self):
        for text in ('蓝','春夏','款','10','%','_','不在','3','xy','a\0b'):
            with self.subTest(text=text):
                expected=self.expected(text)
                for quick in (False,True):
                    result=self.catalog.items(text=text,quick=quick)
                    self.assertEqual([r['id'] for r in result['rows']],expected)
                self.assertEqual(self.catalog.items(text=text,include_tree=True)['total'],len(expected))

    def test_pending_changes_are_immediately_searchable_and_old_short_text_does_not_leak(self):
        with self.catalog.connect() as c:
            c.execute("UPDATE items SET relative='稀有鸢/照片.jpg' WHERE id='0'")
            c.execute("UPDATE catalog_metadata SET search_text='新颖鸢' WHERE item='2'")
            c.execute("UPDATE catalog_metadata SET search_text='更换' WHERE item='3'")
        for text in ('稀有','鸢','新颖','蓝','春夏','更换'):
            self.assertEqual([r['id'] for r in self.catalog.items(text=text,quick=True)['rows']],self.expected(text))

        while self.catalog.literal_index.process():pass
        for text in ('鸢','蓝','春夏','更换'):
            self.assertEqual([r['id'] for r in self.catalog.items(text=text,quick=True)['rows']],self.expected(text))

    def test_long_ocr_text_indexes_without_quadratic_normalization(self):
        text='SKU蓝色立体印花'*10000+'结尾稀有鸢尾'
        with self.catalog.connect() as c:c.execute('UPDATE catalog_metadata SET search_text=? WHERE item=?',(text,'2'))
        start=time.perf_counter()
        while self.catalog.literal_index.process():pass
        self.assertLess(time.perf_counter()-start,2,'长文本索引不得反复归一化整段文本并占用写事务')
        self.assertIn('2',[r['id'] for r in self.catalog.items(text='鸢尾',quick=True)['rows']])

    def test_quick_pages_match_full_search_and_defer_only_expensive_counts(self):
        expected=self.expected('plain')
        page=self.catalog.items(text='plain',limit=1,quick=True,include_tree=True)
        self.assertIsNone(page['total']);self.assertTrue(page['counts_pending']);self.assertTrue(page['tree_deferred'])
        actual=[r['id'] for r in page['rows']]
        while page['has_more']:
            page=self.catalog.items(text='plain',limit=1,quick=True,cursor=page['next_cursor'])
            actual += [r['id'] for r in page['rows']]
        self.assertEqual(actual,expected)
        self.assertEqual(self.catalog.items(text='plain',include_tree=True)['total'],len(expected))
        for root,folder,media in [('one','蓝色','image'),('one','春夏','image'),('one','','video')]:
            regular=self.catalog.items(text='款',root=root,folder=folder,media=media)
            quick=self.catalog.items(text='款',root=root,folder=folder,media=media,quick=True)
            self.assertEqual(quick['rows'],regular['rows'])

    def test_ranked_pages_keep_exact_prefix_and_keyword_order(self):
        with self.catalog.connect() as c:
            c.execute("UPDATE items SET sku='LC12',normalized='LC12' WHERE id='0'")
            c.execute("UPDATE items SET sku='LC123',normalized='LC123' WHERE id='1'")
            c.execute("UPDATE items SET relative='LC12.jpg' WHERE id='2'")
        page=query(self.catalog,text='LC12',limit=1);actual=[]
        while True:
            actual += [(r['id'],r['priority']) for r in page['rows']]
            if not page['has_more']:break
            page=query(self.catalog,text='LC12',limit=1,cursor=page['next_cursor'])
        self.assertEqual(actual,[('0',0),('1',1),('2',2)])

    def test_index_restart_and_removed_or_stale_members(self):
        with self.catalog.connect() as c:
            c.execute("UPDATE catalog_metadata SET fingerprint='stale' WHERE item='2'")
            c.execute("UPDATE items SET online=0 WHERE id='0'")
        self.assertEqual(self.catalog.items(text='蓝',quick=True)['rows'],[])
        self.catalog.literal_index.build()
        self.assertEqual(self.catalog.items(text='春夏',quick=True)['total'],2)

    def test_cancel_interrupts_both_probe_and_fallback(self):
        from catalog_search_requests import SearchCancelled
        with self.assertRaises(SearchCancelled):
            self.catalog.items(text='春夏',quick=True,cancel=lambda:True)
        with self.assertRaises(SearchCancelled):query(self.catalog,text='plain',cancel=lambda:True)

    def test_archive_without_visual_service_does_not_query_visual_tables(self):
        self.catalog.intelligence=None
        with self.catalog.connect() as c:
            c.execute('DROP TABLE intelligence_fts')
        self.assertEqual([r['id'] for r in self.catalog.items(text='plain',quick=True)['rows']],['2','4'])

    def test_unchanged_rescans_do_not_enqueue_the_entire_library(self):
        with self.catalog.connect() as c:
            c.execute('UPDATE items SET relative=relative')
            c.execute('UPDATE catalog_metadata SET search_text=search_text')
            self.assertFalse(c.execute('SELECT 1 FROM literal_search_dirty LIMIT 1').fetchone())
            c.execute("UPDATE items SET relative='新版/照片.jpg' WHERE id='0'")
            self.assertEqual(c.execute("SELECT item FROM literal_search_dirty WHERE source='path'").fetchone()[0],'0')

    def test_empty_query_also_returns_cached_thumbnail_rows_before_global_counts(self):
        expected=[r['id'] for r in self.catalog.items()['rows']]
        page=self.catalog.items(quick=True,limit=1)
        self.assertIsNone(page['total']);actual=[]
        while True:
            actual.extend(r['id'] for r in page['rows'])
            if not page['has_more']:break
            page=self.catalog.items(quick=True,limit=1,cursor=page['next_cursor'])
        self.assertEqual(actual,expected)
        self.assertEqual(self.catalog.items(quick=True,folder='蓝色')['total'],1)

    def test_old_noop_trigger_upgrades_without_losing_pending_changes(self):
        from literal_search import LiteralSearchIndex
        with self.catalog.connect() as c:
            sql=c.execute("SELECT sql FROM sqlite_master WHERE name='literal_search_path_update'").fetchone()[0]
            c.execute('DROP TRIGGER literal_search_path_update')
            c.execute(sql.replace(' WHEN old.relative IS NOT new.relative',''))
            c.execute("INSERT INTO literal_search_dirty VALUES('path','0')")
        upgraded=LiteralSearchIndex(self.catalog);upgraded.close()
        with self.catalog.connect() as c:
            c.execute('UPDATE items SET relative=relative')
            self.assertEqual([r[0] for r in c.execute("SELECT item FROM literal_search_dirty WHERE source='path'")],['0'])


if __name__=='__main__':unittest.main()
