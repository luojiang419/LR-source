import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
import random
from unittest.mock import patch,Mock

from asset_catalog import AssetCatalog
from fast_path_index import FastPathIndex


def add_item(connection, item, relative, *, pair=None, source='unmatched', online=1):
    connection.execute('''INSERT INTO items(id,root,path,relative,folder,name,extension,size,modified,
        fingerprint,pair_key,last_seen,source,online) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (item, 'root', 'Z:/'+relative, relative, relative.rpartition('/')[0] or '/',
         relative.rpartition('/')[2], Path(relative).suffix.lower(), 10, 1, item,
         pair or item, 1, source, online))


class FastPathIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.catalog = AssetCatalog(Path(self.temporary.name),fast_index=Mock())
        self.indices = []
        with self.catalog.connect() as connection:
            connection.execute("INSERT INTO roots(id,path,created) VALUES('root','Z:/',1)")

    def tearDown(self):
        for index in self.indices:
            index.close()
        self.catalog.close()
        self.temporary.cleanup()

    def index(self, background=False):
        index = FastPathIndex(self.catalog.db, background=background)
        self.indices.append(index)
        return index

    def find(self, index, text):
        with self.catalog.connect() as connection:
            clause, args = index.condition(connection, text)
            return [row[0] for row in connection.execute('SELECT id FROM capture_items WHERE '+clause+' ORDER BY id', args)]

    def test_migrate_existing_data_and_persist_ready_without_rebuild(self):
        with self.catalog.connect() as connection:
            add_item(connection, 'one', '2026/LC7877499-P6/look.jpg')
        index = self.index()
        self.assertTrue(index.ready, index.error)
        self.assertEqual(self.find(index, 'lc7877499'), ['one'])
        with patch.object(FastPathIndex, '_build', side_effect=AssertionError('不应重复迁移')):
            reopened = self.index()
        self.assertTrue(reopened.ready)
        self.assertEqual(self.find(reopened, 'LC7877499'), ['one'])

    def test_insert_delete_rename_and_metadata_only_changes(self):
        index = self.index()
        with self.catalog.connect() as connection:
            add_item(connection, 'one', 'old-needle.jpg')
        self.assertEqual(self.find(index, 'needle'), ['one'])
        with self.catalog.connect() as connection:
            before = connection.execute('SELECT total_changes()').fetchone()[0]
            connection.execute("UPDATE items SET online=1,source='manual',relative=relative WHERE id='one'")
            self.assertEqual(connection.execute('SELECT total_changes()').fetchone()[0]-before, 1)
            connection.execute("UPDATE items SET relative='new-label.jpg' WHERE id='one'")
        self.assertEqual(self.find(index, 'needle'), [])
        self.assertEqual(self.find(index, 'label'), ['one'])
        with self.catalog.connect() as connection:
            connection.execute("UPDATE items SET online=0 WHERE id='one'")
        self.assertEqual(self.find(index, 'label'), [])
        with self.catalog.connect() as connection:
            connection.execute("DELETE FROM items WHERE id='one'")
            connection.execute("INSERT INTO reference_path_fts(reference_path_fts,rank) VALUES('integrity-check',1)")
        self.assertEqual(self.find(index, 'label'), [])

    def test_literal_unicode_quotes_percent_short_terms_and_nul(self):
        relatives = ['春夏新品/款式甲乙.jpg', 'one/100%_"quote".jpg', 'Éclair/AÄB中.jpg', 'a/xy.jpg', 'abc\0def.jpg']
        with self.catalog.connect() as connection:
            for number, relative in enumerate(relatives):
                add_item(connection, str(number), relative)
        index = self.index()
        for term in ['春夏新', '甲乙', '100%', '_"quote"', 'one/', 'Écl', 'écl', 'AÄB', 'aäb', 'xy', 'x', 'abc\0def', '不存在', '%_', '"', '']:
            with self.subTest(term=term), self.catalog.connect() as connection:
                expected = [row[0] for row in connection.execute('SELECT id FROM capture_items WHERE instr(lower(relative),lower(?))>0 ORDER BY id', (term,))]
                self.assertEqual(self.find(index, term), expected)

    def test_unicode_tokenizer_candidates_never_drop_literal_substrings(self):
        generator = random.Random(17)
        alphabet = 'aAbB中春😀ßẞİıéÉá_ %"\u0301'
        relatives = [''.join(generator.choices(alphabet, k=18))+'.jpg' for _ in range(30)]
        with self.catalog.connect() as connection:
            for number, relative in enumerate(relatives):
                add_item(connection, str(number), relative)
        index = self.index()
        for relative in relatives:
            for offset in range(15):
                term = relative[offset:offset+3]
                with self.subTest(term=term), self.catalog.connect() as connection:
                    expected = [row[0] for row in connection.execute('SELECT id FROM capture_items WHERE instr(lower(relative),lower(?))>0 ORDER BY id', (term,))]
                    self.assertEqual(self.find(index, term), expected)

    def test_raw_match_returns_jpg_and_obeys_manual_pair_preference(self):
        index = self.index()
        with self.catalog.connect() as connection:
            add_item(connection, 'raw', 'SKU-123/look.CR3', pair='capture')
            add_item(connection, 'jpg', 'SKU-123/look.jpg', pair='capture')
        self.assertEqual(self.find(index, '.CR3'), ['jpg'])
        with self.catalog.connect() as connection:
            connection.execute("UPDATE items SET source='manual' WHERE id='raw'")
        self.assertEqual(self.find(index, '.jpg'), ['raw'])

    def test_background_migration_fallback_concurrent_write_and_clean_stop(self):
        with self.catalog.connect() as connection:
            add_item(connection, 'before', 'SKU-before.jpg')
        entered, release = threading.Event(), threading.Event()
        original = FastPathIndex._build
        def paused(index):
            entered.set()
            release.wait(5)
            original(index)
        with patch.object(FastPathIndex, '_build', paused):
            index = self.index(background=True)
            self.assertTrue(entered.wait(2))
        self.assertFalse(index.ready)
        self.assertEqual(self.find(index, 'before'), ['before'])
        with self.catalog.connect() as connection:
            add_item(connection, 'during', 'SKU-during.jpg')
            connection.execute("UPDATE items SET relative='SKU-renamed.jpg' WHERE id='before'")
        release.set()
        index._worker.join(5)
        self.assertFalse(index._worker.is_alive())
        self.assertTrue(index.ready, index.error)
        self.assertEqual(self.find(index, 'renamed'), ['before'])
        self.assertEqual(self.find(index, 'during'), ['during'])
        index.close()
        self.assertFalse(index._worker.is_alive())

    def test_no_fts_support_keeps_all_results_available(self):
        with self.catalog.connect() as connection:
            add_item(connection, 'one', 'SKU-123.jpg')
        with patch.object(FastPathIndex, '_prepare', side_effect=sqlite3.OperationalError('no such tokenizer: trigram')):
            index = self.index()
        self.assertFalse(index.ready)
        self.assertEqual(self.find(index, 'SKU-123'), ['one'])

    def test_close_while_writer_holds_lock_and_restart_resumes(self):
        with self.catalog.connect() as connection:
            add_item(connection, 'one', 'SKU-123.jpg')
        writer = sqlite3.connect(self.catalog.db)
        try:
            writer.execute('BEGIN IMMEDIATE')
            index = self.index(background=True)
            self.assertFalse(index.ready)
            self.assertEqual(self.find(index, 'SKU-123'), ['one'])
            index.close()
            self.assertFalse(index._worker.is_alive())
        finally:
            writer.rollback()
            writer.close()
        restarted = self.index()
        self.assertTrue(restarted.ready, restarted.error)
        self.assertEqual(self.find(restarted, 'SKU-123'), ['one'])

    def test_read_snapshot_before_completed_migration_falls_back(self):
        with self.catalog.connect() as connection:
            add_item(connection, 'one', 'SKU-123.jpg')
        entered, release = threading.Event(), threading.Event()
        original = FastPathIndex._build
        def paused(index):
            entered.set()
            release.wait(5)
            original(index)
        with patch.object(FastPathIndex, '_build', paused):
            index = self.index(background=True)
            self.assertTrue(entered.wait(2))
        with self.catalog.connect() as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT ready FROM reference_path_index_state').fetchall()
            release.set()
            index._worker.join(5)
            self.assertTrue(index.ready)
            clause, args = index.condition(reader, 'SKU-123')
            self.assertNotIn('MATCH', clause)
            self.assertEqual([row[0] for row in reader.execute('SELECT id FROM capture_items WHERE '+clause, args)], ['one'])

    def test_cancel_rebuild_preserves_items_and_restart_rebuilds(self):
        with self.catalog.connect() as connection:
            for number in range(1000):
                add_item(connection, str(number), f'SKU-{number}/look.jpg')
        rebuilding = threading.Event()
        original = FastPathIndex._connect
        def traced(index, timeout):
            connection = original(index, timeout)
            if threading.current_thread() is not threading.main_thread():
                def trace(sql):
                    if "VALUES('rebuild')" in sql:
                        rebuilding.set()
                        index._closed.wait(3)
                connection.set_trace_callback(trace)
            return connection
        with patch.object(FastPathIndex, '_connect', traced):
            index = self.index(background=True)
            self.assertTrue(rebuilding.wait(3))
            index.close()
        with self.catalog.connect() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM items').fetchone()[0], 1000)
            self.assertEqual(connection.execute('SELECT ready FROM reference_path_index_state').fetchone()[0], 0)
        restarted = self.index()
        self.assertTrue(restarted.ready, restarted.error)
        self.assertEqual(self.find(restarted, 'SKU-777/'), ['777'])

    def test_query_plan_uses_fts_and_pair_index(self):
        with self.catalog.connect() as connection:
            connection.execute('CREATE INDEX reference_name_page ON items(root,online,name COLLATE NOCASE,id)')
            for number in range(100):
                add_item(connection, str(number), f'SKU-{number}/look.jpg')
        index = self.index()
        with self.catalog.connect() as connection:
            clause, args = index.condition(connection, 'SKU-77')
            plan = '\n'.join(row[3] for row in connection.execute('EXPLAIN QUERY PLAN SELECT id FROM capture_items WHERE +root=? AND '+clause+' ORDER BY name COLLATE NOCASE,id LIMIT 61', ['root', *args]))
        self.assertIn('VIRTUAL TABLE INDEX', plan)
        self.assertIn('catalog_pair (pair_key=', plan)
        self.assertNotIn('SCAN a', plan)
        self.assertNotIn('SEARCH a USING INDEX reference_name_page', plan)


if __name__ == '__main__':
    unittest.main()
