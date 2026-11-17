"""目录小结果集不能因同一根目录的大库而退化为全库读取。"""
import tempfile
import unittest
from pathlib import Path

from asset_catalog import AssetCatalog


class CatalogFolderLatencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = AssetCatalog(Path(self.temp.name))
        with self.catalog.connect() as c:
            c.execute("INSERT INTO roots(id,path,created) VALUES ('root','photos',0)")

    def tearDown(self):
        self.catalog.close()
        self.temp.cleanup()

    def insert(self, rows):
        with self.catalog.connect() as c:
            c.executemany('''INSERT INTO items
                (id,root,path,relative,folder,name,extension,size,modified,fingerprint,last_seen,pair_key,online,source,media_type)
                VALUES (?,'root',?,?,?,?,?,1,?,'fingerprint',0,?,?,?,?)''',
                [(id, folder+'/'+id, folder+'/'+id, folder, id, extension, modified, pair or id, online, source, media)
                 for id, folder, extension, modified, pair, online, source, media in rows])

    def test_small_folder_query_work_is_bounded_in_large_root(self):
        self.insert([(str(i), 'unrelated/'+str(i), '.jpg', i, '', 1, 'unmatched', 'image') for i in range(12000)] +
                    [('target-'+str(i), 'target', '.jpg', i, '', 1, 'unmatched', 'image') for i in range(8)])
        steps = []
        def cancel():
            steps.append(1)
            return len(steps) > 5
        result = self.catalog.items(root='root', folder='target', cancel=cancel)
        self.assertEqual(result['total'], 8)
        self.assertEqual(len(result['rows']), 8)
        self.assertLess(len(steps), 5, '小目录应范围读取，不能遍历同根目录全部素材')

    def test_first_page_hydration_is_bounded_in_large_archive(self):
        self.insert([(str(i), 'folder', '.jpg', i, '', 1, 'unmatched', 'image') for i in range(12000)])
        steps = []
        def cancel():
            steps.append(1)
            return len(steps) > 8
        result = self.catalog.items(quick=True, limit=120, cancel=cancel)
        self.assertEqual(len(result['rows']),120)
        self.assertEqual([r['id'] for r in result['rows']], [str(i) for i in range(11999,11879,-1)])
        self.assertTrue(result['has_more'])
        self.assertLess(len(steps),8,'补齐当前页不能再次全库读取')

    def test_folder_candidates_preserve_filters_pairs_and_cursor(self):
        selected = '款_A%'
        self.insert([
            ('jpg', selected, '.jpg', 20, 'pair', 1, 'unmatched', 'image'),
            ('raw', selected, '.cr3', 20, 'pair', 1, 'manual', 'image'),
            ('child', selected+'/child', '.jpg', 15, '', 1, 'unmatched', 'image'),
            ('mixed-child', '款_a%/Child', '.jpg', 10, '', 1, 'unmatched', 'image'),
            ('wrong-case-direct', '款_a%', '.jpg', 9, '', 1, 'unmatched', 'image'),
            ('prefix', selected+'suffix', '.jpg', 9, '', 1, 'unmatched', 'image'),
            ('wildcard', '款xAB/child', '.jpg', 9, '', 1, 'unmatched', 'image'),
            ('offline', selected, '.jpg', 9, '', 0, 'unmatched', 'image'),
            ('video', selected, '.mp4', 5, '', 1, 'unmatched', 'video'),
        ])
        first = self.catalog.items(root='root', folder=selected, limit=2)
        self.assertEqual(first['total'], 4)
        self.assertEqual([r['id'] for r in first['rows']], ['raw', 'child'])
        self.assertTrue(first['rows'][0]['has_jpg'])
        self.assertTrue(first['rows'][0]['has_raw'])
        self.assertTrue(first['has_more'])
        second = self.catalog.items(root='root', folder=selected, limit=2, cursor=first['next_cursor'])
        self.assertEqual([r['id'] for r in second['rows']], ['mixed-child', 'video'])
        self.assertFalse(second['has_more'])
        self.assertEqual(self.catalog.items(root='root', folder=selected, media='image')['total'], 3)
        self.assertEqual(self.catalog.items(root='root', folder='missing')['total'], 0)
        self.assertEqual([r['id'] for r in self.catalog.items(root='root', folder=selected, offset=2)['rows']], ['mixed-child', 'video'])
        # 原视图的目录语义是验收基准，覆盖带目录统计的搜索路径。
        baseline = self.catalog.items(root='root', folder=selected, include_tree=True)
        self.assertEqual(first['total'], baseline['total'])
        self.assertEqual([r['id'] for r in baseline['rows']], ['raw', 'child', 'mixed-child', 'video'])


if __name__ == '__main__':
    unittest.main()
