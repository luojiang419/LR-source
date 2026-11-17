"""文件名升序、稳定翻页及业务更新不改排序。"""
import base64
import json
import unittest
import test_catalog_folder_latency as fixtures


class FilenameOrderTests(unittest.TestCase):
    setUp=fixtures.CatalogFolderLatencyTests.setUp
    tearDown=fixtures.CatalogFolderLatencyTests.tearDown
    insert=fixtures.CatalogFolderLatencyTests.insert

    def seed(self):
        names=['XSY_1383.JPG','XSY_1381.JPG','XSY_1384.JPG','XSY_1382.JPG']
        self.insert([(name,'target','.jpg',index,'',1,'unmatched','image') for index,name in enumerate(names)])

    def test_1382_precedes_1383_across_page_boundary(self):
        self.seed();first=self.catalog.items(root='root',folder='target',sort='original',limit=2)
        self.assertEqual([r['name'] for r in first['rows']],['XSY_1381.JPG','XSY_1382.JPG'])
        second=self.catalog.items(root='root',folder='target',sort='original',cursor=first['next_cursor'],limit=2)
        self.assertEqual([r['name'] for r in second['rows']],['XSY_1383.JPG','XSY_1384.JPG'])
        offset=self.catalog.items(root='root',folder='target',sort='original',offset=2,limit=2)
        self.assertEqual([r['id'] for r in offset['rows']],[r['id'] for r in second['rows']])
        self.assertFalse(second['has_more'])

    def test_metadata_updates_do_not_move_original_order(self):
        self.seed();query=dict(root='root',folder='target',sort='original')
        before=[r['id'] for r in self.catalog.items(**query)['rows']]
        with self.catalog.connect() as c:c.execute("UPDATE items SET modified=9999999999,sku='LC123456',source='manual' WHERE id='XSY_1383.JPG'")
        self.assertEqual([r['id'] for r in self.catalog.items(**query)['rows']],before)
        self.assertEqual(self.catalog.items(root='root',folder='target',sort='modified')['rows'][0]['name'],'XSY_1383.JPG')

    def test_equal_names_use_id_tiebreaker_and_old_descending_cursor_is_rejected(self):
        self.seed()
        with self.catalog.connect() as c:c.execute("UPDATE items SET name='XSY_1382.JPG' WHERE id='XSY_1383.JPG'")
        first=self.catalog.items(root='root',folder='target',sort='original',limit=2)
        second=self.catalog.items(root='root',folder='target',sort='original',cursor=first['next_cursor'],limit=2)
        self.assertEqual(len({r['id'] for r in first['rows']+second['rows']}),4)
        old=base64.urlsafe_b64encode(json.dumps(['original','XSY_1382.JPG','XSY_1382.JPG']).encode()).decode()
        with self.assertRaises(ValueError):self.catalog.items(root='root',folder='target',sort='original',cursor=old)


if __name__=='__main__':unittest.main()
