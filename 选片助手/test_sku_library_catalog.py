"""归档单张入库证据必须带出同目录全部已确认照片。"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image
from sku_library import browse
from sku_library_catalog import catalog_rows
from sku_library_index import SkuLibraryIndex,SCHEMA
from sku_library_management import manage
import test_sku_library as fixtures


class CatalogLibraryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SkuLibraryTests();self.f.setUp();f=self.f;f.sku.archive=f.archive
        for i in range(125):Image.new('RGB',(80,60),(i,50,100)).save(f.photos/'A'/f'added{i:03}.JPG')
        (f.photos/'A'/'added000.CR3').write_bytes(b'paired-raw')
        f.catalog.scan(f.root)
        with f.catalog.connect() as c:
            c.execute("UPDATE items SET sku='A-100',normalized='A100',source='shooting' WHERE folder='A'")
            c.execute("UPDATE items SET sku='A-100',normalized='A100',source='shooting' WHERE folder='B'")
        with f.store.connect() as c:c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('archive_session:'+f.sid,str(f.photos)))
        f.archive.apply(f.archive.preview(f.sid,[f.ids[0]],'A-100','细节图')['preview_id'])
        self.index=SkuLibraryIndex(f.sku)

    def tearDown(self):
        self.index.close();self.f.catalog.close();self.f.tearDown()

    def test_complete_folder_pairs_natural_pages_and_unrelated_folder_excluded(self):
        self.assertTrue(self.index.refresh())
        with patch.object(self.index,'start'):
            first=self.index.browse(code='A-100',limit=120);last=self.index.browse(code='A-100',offset=120,limit=120)
        self.assertEqual(first['total'],127);self.assertEqual(len(first['rows']),120);self.assertEqual(len(last['rows']),7)
        self.assertTrue(first['has_more']);self.assertFalse(last['has_more'])
        rows=first['rows']+last['rows'];self.assertEqual(len({r['path'] for r in rows}),127)
        self.assertEqual({r['folder'] for r in rows},{'A'})
        pair=next(r for r in rows if r['name']=='added000.JPG');self.assertTrue(pair['has_jpg']);self.assertTrue(pair['has_raw'])
        self.assertEqual(sum(r['source']=='archive' for r in rows),1)

    def test_only_confirmed_sources_expand_and_manual_pair_correction_wins(self):
        f=self.f
        with f.catalog.connect() as c:
            c.execute("UPDATE items SET source='path_rule' WHERE name='added001.JPG'")
            c.execute("UPDATE items SET source='folder_default' WHERE name='added002.JPG'")
            c.execute("UPDATE items SET sku='OTHER-900',normalized='OTHER900',source='manual' WHERE name='added000.CR3'")
        self.assertEqual(browse(f.sku,code='A-100')['total'],124)

    def test_catalog_version_refreshes_removed_rows_and_restart_uses_complete_snapshot(self):
        self.index.refresh();before=self.index.signature();f=self.f
        with f.catalog.connect() as c:c.execute("UPDATE items SET online=0 WHERE name='added010.JPG'")
        self.assertNotEqual(self.index.signature(),before)
        with patch.object(self.index,'start'):self.assertEqual(self.index.browse(code='A-100',fresh=True)['total'],126)
        reopened=SkuLibraryIndex(f.sku)
        try:
            with patch.object(reopened,'start'),patch('sku_library.project',side_effect=AssertionError('重启不得重扫')):
                self.assertEqual(reopened.browse(code='A-100')['total'],126)
        finally:reopened.close()
        saved=json.loads(self.index.path.read_bytes());self.assertEqual(saved['schema'],SCHEMA)

    def test_unknown_catalog_file_does_not_register_a_new_sku(self):
        f=self.f
        with f.catalog.connect() as c:c.execute("UPDATE items SET sku='GUESS-900',normalized='GUESS900',source='path_rule' WHERE folder='A'")
        self.assertEqual(browse(f.sku,code='A-100')['total'],1)
        self.assertEqual(browse(f.sku,code='GUESS-900')['total'],0)

    def test_correct_catalog_only_photo_uses_existing_write_queue_and_keeps_other_photos(self):
        f=self.f;rows=browse(f.sku,code='A-100')['rows'];row=next(r for r in rows if r['name']=='added003.JPG')
        result=manage(f.sku,f.archive,{'action':'correct_selection','request_id':'catalog-correction','new_sku':'B-200','items':[{'id':row['id'],'sku':row['sku']}]})
        self.assertEqual(result['capture_count'],1);self.assertTrue(result['operation_ids'])
        self.assertEqual(browse(f.sku,code='A-100')['total'],126);self.assertEqual(browse(f.sku,code='B-200')['total'],1)

    def test_removed_catalog_root_does_not_return_supplemental_rows(self):
        self.f.catalog.remove(self.f.root)
        self.assertEqual(browse(self.f.sku,code='A-100')['total'],1)

    def test_named_sku_folder_displays_all_nine_without_writing_missing_sku(self):
        import media_engine as media
        f=self.f;folder=f.photos/'A-100';folder.mkdir()
        for i in range(9):Image.new('RGB',(80,60),(i*20+10,30,40)).save(folder/f'photo{i}.JPG')
        f.catalog.scan(f.root)
        with f.catalog.connect() as c:
            c.execute("UPDATE items SET sku='',normalized='',source='unmatched' WHERE folder='A-100'")
            c.execute("UPDATE items SET sku='A-100',normalized='A100',source='embedded' WHERE folder='A-100' AND name IN ('photo0.JPG','photo1.JPG','photo2.JPG','photo3.JPG')")
            c.execute("UPDATE items SET source='manual' WHERE folder='A-100' AND name='photo4.JPG'")
        record=media.capture_record(f.photos,folder/'photo0.JPG',None,{})
        f.store.append_live(f.sid,[record]);f.archive.apply(f.archive.preview(f.sid,[record['id']],'A-100','细节图')['preview_id'])
        result=browse(f.sku,code='A-100',folder='A-100')
        self.assertEqual(result['total'],9);self.assertEqual({r['name'] for r in result['rows']},{f'photo{i}.JPG' for i in range(9)})
        self.assertEqual(sum(r.get('provenance_label')=='SKU目录' for r in result['rows']),5)
        with f.catalog.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM items WHERE folder='A-100' AND sku=''").fetchone()[0],5)
        # 明确修正为其它SKU后仍按人工归属展示。
        with f.catalog.connect() as c:c.execute("UPDATE items SET sku='B-200',normalized='B200',source='manual' WHERE folder='A-100' AND name='photo8.JPG'")
        self.assertEqual(browse(f.sku,code='A-100',folder='A-100')['total'],8)


if __name__=='__main__':unittest.main()
