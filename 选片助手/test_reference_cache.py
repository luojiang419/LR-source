"""临时参考缓存的复用、选择性清理和并发清理验证。"""
from pathlib import Path
import tempfile
import threading
import unittest

from reference_cache import ReferenceCache


class ReferenceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)/'temporary'
        self.cache = ReferenceCache(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def test_reuses_cached_images_and_json_after_restart(self):
        calls = []
        def load():
            calls.append(1)
            return b'\xff\xd8preview'
        for cache in (self.cache, ReferenceCache(self.folder)):
            self.assertEqual(cache.get_or_load('peer', 'root', 'one.JPG', 'preview', {'size':420}, load), b'\xff\xd8preview')
        self.assertEqual(calls, [1])
        value = {'rows':[{'relative':'one.JPG'}]}
        self.cache.get_or_load('peer', 'root', '', 'items', {'q':'one'}, lambda: value)
        self.assertEqual(self.cache.get_or_load('peer', 'root', '', 'items', {'q':'one'}, lambda: self.fail('重复联网')), value)

    def test_selected_cleanup_preserves_other_images_and_unknown_files(self):
        for relative in ('one.JPG', 'two.JPG'):
            self.cache.get_or_load('peer', 'root', relative, 'preview', {}, lambda: b'\xff\xd8image')
        self.cache.get_or_load('peer', 'root', '', 'items', {}, lambda: {'rows':[]})
        unrelated = self.folder/'notes.txt';unrelated.write_text('用户文件', 'utf8')
        result = self.cache.clear(reference={'device':'peer', 'root':'root', 'relative':'one.JPG'})
        self.assertEqual(result['removed_files'], 2)
        with self.cache.connect() as connection:
            self.assertEqual([row['relative'] for row in connection.execute('SELECT relative FROM entries')], ['two.JPG'])
        self.assertEqual(unrelated.read_text('utf8'), '用户文件')
        self.cache.clear()
        self.assertTrue(unrelated.exists())

    def test_same_image_requests_share_one_fetch(self):
        entered, release = threading.Event(), threading.Event()
        calls, results = [], []
        def load():
            calls.append(1);entered.set();release.wait(5)
            return b'\xff\xd8image'
        def request():
            results.append(self.cache.get_or_load('peer', 'root', 'one.JPG', 'preview', {}, load))
        threads = [threading.Thread(target=request) for _ in range(2)]
        for thread in threads:thread.start()
        try:
            self.assertTrue(entered.wait(3))
        finally:
            release.set()
            for thread in threads:thread.join(5)
        self.assertEqual(calls, [1]);self.assertEqual(results, [b'\xff\xd8image', b'\xff\xd8image'])

    def test_clearing_during_fetch_prevents_late_cache_repopulation(self):
        entered, release = threading.Event(), threading.Event()
        def load():
            entered.set();release.wait(5)
            return b'\xff\xd8image'
        thread = threading.Thread(target=lambda: self.cache.get_or_load('peer', 'root', 'one.JPG', 'preview', {}, load))
        thread.start()
        try:
            self.assertTrue(entered.wait(3));self.cache.clear()
        finally:
            release.set();thread.join(5)
        self.assertFalse(list(self.folder.glob('*.jpg')))
        with self.assertRaises(ValueError):
            self.cache.get_or_load('peer', 'root', 'one.JPG', 'preview', {}, lambda: b'old', generation=0)
        self.assertEqual(ReferenceCache(self.folder).generation, 1)

    def test_modified_cache_file_is_preserved_and_not_served(self):
        self.cache.get_or_load('peer', 'root', 'one.JPG', 'preview', {}, lambda: b'\xff\xd8image')
        path = next(self.folder.glob('*.jpg'));path.write_bytes(b'user replacement')
        self.assertEqual(self.cache.clear()['removed_files'], 0)
        self.assertEqual(path.read_bytes(), b'user replacement')
        self.assertEqual(self.cache.get_or_load('peer', 'root', 'one.JPG', 'preview', {}, lambda: b'\xff\xd8fresh'), b'\xff\xd8fresh')


if __name__=='__main__':
    unittest.main()
