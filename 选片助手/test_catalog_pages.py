"""归档浏览页跨进程复用、数据变更失效及后台扫描/写锁边界。"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
from asset_catalog import AssetCatalog


class CatalogPagesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.photos=self.base/'photos';self.photos.mkdir()
        Image.new('RGB',(120,80),'teal').save(self.photos/'one.jpg')
        self.catalog=AssetCatalog(self.base/'data');self.root=self.catalog.add_root(str(self.photos))['root_id']
        self.row=self.catalog.items()['rows'][0];self.catalog.preview(self.row['id'],420,read=True)

    def tearDown(self):self.catalog.close();self.temp.cleanup()

    def test_restarts_return_same_complete_page_without_querying_items_or_cache_index(self):
        original=self.catalog.items(quick=True)
        self.catalog.close();self.catalog=AssetCatalog(self.base/'data')
        with patch.object(self.catalog,'_search_connection',side_effect=AssertionError('快照不应重新查素材')), \
             patch.object(self.catalog,'cached_preview_hints',side_effect=AssertionError('快照不应重新定位缓存')):
            self.assertEqual(self.catalog.items(quick=True),original)
        source='''import json,sys
from unittest.mock import patch
from pathlib import Path
from asset_catalog import AssetCatalog
c=AssetCatalog(Path(sys.argv[1]))
try:
 with patch.object(c,'_search_connection',side_effect=AssertionError('fresh process should reuse disk page')):
  print(json.dumps(c.items(quick=True),ensure_ascii=False))
finally:c.close()
'''
        result=subprocess.run([sys.executable,'-X','utf8','-c',source,str(self.catalog.data)],cwd=Path(__file__).parent,capture_output=True,text=True,encoding='utf8',timeout=15)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout),original)

    def test_file_versions_manual_changes_and_removed_roots_invalidate(self):
        for sql,value in [("UPDATE items SET name=? WHERE id=?",'changed.jpg'),
                          ("UPDATE items SET fingerprint=? WHERE id=?",'new-file-version'),
                          ("UPDATE items SET sku=? WHERE id=?",'new-sku')]:
            self.catalog.items(quick=True)
            with self.catalog.connect() as c:c.execute(sql,(value,self.row['id']))
            with patch.object(self.catalog,'_search_connection',wraps=self.catalog._search_connection) as query:
                result=self.catalog.items(quick=True);self.assertTrue(query.called)
            self.assertEqual(result['rows'][0]['name'],'changed.jpg')
        self.catalog.remove(self.root)
        self.assertEqual(self.catalog.items(quick=True)['rows'],[])

    def test_missing_thumbnail_does_not_prevent_restart_page_reuse(self):
        Image.new('RGB',(100,70),'blue').save(self.photos/'uncached.jpg')
        self.catalog.scan(self.root)
        original=self.catalog.items(quick=True)
        missing=next(row for row in original['rows'] if row['name']=='uncached.jpg')
        self.assertNotIn('cached=',missing['preview'])
        self.catalog.close();self.catalog=AssetCatalog(self.base/'data')
        with patch.object(self.catalog,'_search_connection',side_effect=AssertionError('缩略图缺失不能阻止重启复用列表')):
            self.assertEqual(self.catalog.items(quick=True),original)
        self.assertTrue(self.catalog.preview(missing['id'],420,read=True).startswith(b'\xff\xd8'))

    def test_scan_bookkeeping_preserves_page_and_busy_write_does_not_wait(self):
        original=self.catalog.items(quick=True)
        with self.catalog.connect() as c:
            c.execute('UPDATE items SET last_seen=123,scan_epoch=scan_epoch+1,scan_revision=scan_revision+1')
            c.execute("UPDATE roots SET scanned=123,status='scanning',scan_epoch=scan_epoch+1")
        with closing(sqlite3.connect(self.catalog.db)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            with patch.object(self.catalog,'_search_connection',side_effect=AssertionError('扫描到访不使快照失效')):
                self.assertEqual(self.catalog.items(quick=True),original)
            revision,_=self.catalog.pages.read('none');started=time.perf_counter()
            self.catalog.pages.write('busy',revision,original)
            self.assertLess(time.perf_counter()-started,.2)

    def test_no_stale_publication_pagination_key_and_bounded_disk_cache(self):
        original=self.catalog.items(quick=True);revision,_=self.catalog.pages.read('none')
        with self.catalog.connect() as c:c.execute("UPDATE items SET name='newest.jpg'")
        self.catalog.pages.write('stale',revision,original)
        self.assertIsNone(self.catalog.pages.read('stale')[1])
        revision,_=self.catalog.pages.read('none')
        for number in range(15):self.catalog.pages.write(str(number),revision,original)
        with self.catalog.connect() as c:self.assertLessEqual(c.execute('SELECT count(*) FROM catalog_pages').fetchone()[0],8)
        self.assertEqual(self.catalog.items(quick=True,offset=1)['rows'],[])
        self.assertEqual(self.catalog.items(quick=True,folder='not-here')['rows'],[])

    def test_new_deleted_files_exclusions_and_cancel_do_not_reuse_old_pages(self):
        from catalog_search_requests import SearchCancelled
        original=self.catalog.items(quick=True)
        path=self.photos/'new.jpg';Image.new('RGB',(80,60),'blue').save(path);self.catalog.scan(self.root)
        self.assertEqual(len(self.catalog.items(quick=True)['rows']),2)
        path.unlink();self.catalog.scan(self.root)
        self.assertEqual(len(self.catalog.items(quick=True)['rows']),1)
        self.catalog.items(quick=True)
        with self.catalog.connect() as c:
            before=c.execute('SELECT value FROM catalog_page_revision').fetchone()[0]
            c.execute('INSERT INTO exclusions VALUES (?,?)',(self.root,'a-folder'))
            self.assertGreater(c.execute('SELECT value FROM catalog_page_revision').fetchone()[0],before)
        self.catalog.items(quick=True)
        with self.assertRaises(SearchCancelled):self.catalog.items(quick=True,cancel=lambda:True)

    def test_metadata_updates_outside_page_do_not_invalidate_other_pages(self):
        path=self.photos/'other.jpg';Image.new('RGB',(70,70),'red').save(path);self.catalog.scan(self.root)
        # 生成两页且均已有缓存；第一页不包含other，第二页包含other。
        rows=self.catalog.items()['rows']
        for row in rows:self.catalog.preview(row['id'],420,read=True)
        first=self.catalog.items(quick=True,limit=1);second=self.catalog.items(quick=True,limit=1,offset=1)
        target=second['rows'][0]['id']
        with self.catalog.connect() as c:c.execute("UPDATE items SET codec='new metadata',width=77 WHERE id=?",(target,))
        with patch.object(self.catalog,'_search_connection',side_effect=AssertionError('其他页元数据不阻挡首屏')):
            self.assertEqual(self.catalog.items(quick=True,limit=1),first)
        self.assertEqual(self.catalog.items(quick=True,limit=1,offset=1)['rows'][0]['width'],77)

    def test_older_unrelated_updates_preserve_first_page_but_newer_items_and_pair_changes_invalidate(self):
        older=self.photos/'older.jpg';Image.new('RGB',(70,70),'blue').save(older);self.catalog.scan(self.root)
        rows=self.catalog.items()['rows'];old=next(row for row in rows if row['name']=='older.jpg')
        with self.catalog.connect() as c:
            c.execute('UPDATE items SET modified=1 WHERE id=?',(old['id'],))
        for row in rows:self.catalog.preview(row['id'],420,read=True)
        first=self.catalog.items(quick=True,limit=1)
        with self.catalog.connect() as c:c.execute("UPDATE items SET sku='older-updated',source='embedded' WHERE id=?",(old['id'],))
        with patch.object(self.catalog,'_search_connection',side_effect=AssertionError('旧素材变化不影响首屏')):
            self.assertEqual(self.catalog.items(quick=True,limit=1),first)
        with self.catalog.connect() as c:c.execute('UPDATE items SET modified=? WHERE id=?',(first['rows'][0]['modified']+10,old['id']))
        self.assertEqual(self.catalog.items(quick=True,limit=1)['rows'][0]['id'],old['id'])
        # 即使RAW的时间更旧，同一拍摄资产的预览版本变化也必须使JPG页失效。
        raw=self.photos/'older.CR3';raw.write_bytes(b'raw fixture');self.catalog.scan(self.root)
        with self.catalog.connect() as c:
            raw_id=c.execute("SELECT id FROM items WHERE extension='.cr3'").fetchone()[0]
            c.execute('UPDATE items SET modified=0 WHERE id=?',(raw_id,))
        self.catalog.items(quick=True,limit=1)
        with self.catalog.connect() as c:c.execute("UPDATE items SET fingerprint='updated-raw' WHERE id=?",(raw_id,))
        with patch.object(self.catalog,'_search_connection',wraps=self.catalog._search_connection) as query:
            self.catalog.items(quick=True,limit=1);self.assertTrue(query.called)


if __name__=='__main__':unittest.main()
