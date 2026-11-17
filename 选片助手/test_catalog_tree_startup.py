"""归档目录统计与代表图语义、跨启动快照、失效和取消。"""
from contextlib import closing
from pathlib import Path
import json,sqlite3,subprocess,sys,tempfile,unittest
from unittest.mock import patch

from asset_catalog import AssetCatalog
from catalog_search_requests import SearchCancelled
import test_catalog_search_candidates as fixtures


class CatalogTreeStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name)
        self.catalog=AssetCatalog(self.data)
        with self.catalog.connect() as c:
            c.executemany('INSERT INTO roots(id,path,created) VALUES(?,?,0)',[('one','Z:/one'),('two','Z:/two')])
            for row in [('jpg','one','甲/plain.jpg','pair','A'),('raw','one','甲/plain.CR3','pair','B'),
                        ('manual-raw','one','乙/manual.CR3','manual','C','manual'),
                        ('manual-jpg','one','乙/manual.jpg','manual','D'),
                        ('video','two','clip.mp4','video','','review','video'),
                        ('design','two','layout.psd','design','','unmatched','design'),
                        ('offline','one','offline.jpg','offline','','unmatched','image',0)]:
                fixtures.CatalogSearchCandidateTests.add(c,*row)

    def tearDown(self):self.catalog.close();self.temp.cleanup()

    def expected(self,media=''):
        with self.catalog.connect() as c:
            matches={}
            for row in c.execute('SELECT root,folder,count(*) AS count FROM capture_items'+(' WHERE media_type=?' if media else '')+' GROUP BY root,folder ORDER BY folder COLLATE NOCASE',[media] if media else []):
                matches.setdefault(row['root'],[]).append({'folder':row['folder'],'count':row['count']})
            return self.catalog._search_tree(c,matches,bool(media))

    def test_tree_and_state_match_view_with_manual_raw_offline_and_all_media(self):
        for media in ('','image','video','design'):
            self.assertEqual(self.catalog.tree(media=media),self.expected(media))
        with self.catalog.connect() as c:
            expected=dict(c.execute("SELECT count(*) AS total,sum(sku!='') AS tagged,sum(source='review') AS review,sum(media_type='image') AS images,sum(media_type='video') AS videos,sum(media_type='design') AS designs FROM capture_items").fetchone())
        self.assertEqual(self.catalog.state()['counts'],expected)
        with self.assertRaises(ValueError):self.catalog.tree(media='unknown')

    def test_restart_reuses_tree_and_scan_bookkeeping_does_not_rebuild(self):
        expected=self.catalog.tree()
        counts=self.catalog.state()['counts']
        self.catalog.close();self.catalog=AssetCatalog(self.data)
        with self.catalog.connect() as c:
            c.execute('UPDATE items SET last_seen=123,scan_epoch=scan_epoch+1')
            c.execute("UPDATE roots SET status='scanning'")
        for root in expected['roots']:root['status']='scanning'
        with closing(sqlite3.connect(self.catalog.db)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            try:
                with patch('asset_catalog._ranked_catalog',side_effect=AssertionError('成熟目录不能重扫')):
                    self.assertEqual(self.catalog.tree(),expected)
                    self.assertEqual(self.catalog.state()['counts'],counts)
            finally:writer.rollback()
        code="from pathlib import Path;from unittest.mock import patch;from asset_catalog import AssetCatalog;import json,sys;c=AssetCatalog(Path(sys.argv[1]));\nwith patch('asset_catalog._ranked_catalog',side_effect=AssertionError('跨进程不能重扫')):print(json.dumps(c.tree()))\nc.close()"
        result=subprocess.run([sys.executable,'-c',code,str(self.data)],cwd=Path(__file__).parent,capture_output=True,text=True,check=True,timeout=15)
        self.assertEqual(json.loads(result.stdout),expected)

    def test_membership_pairing_and_scope_changes_invalidate_cached_tree(self):
        mutations=["UPDATE items SET source='manual' WHERE id='raw'",
                   "UPDATE items SET online=0 WHERE id='manual-raw'",
                   "UPDATE items SET folder='移动后' WHERE id='jpg'",
                   "UPDATE items SET pair_key='independent' WHERE id='raw'",
                   "UPDATE items SET media_type='design' WHERE id='jpg'",
                   "UPDATE roots SET removed=1 WHERE id='two'"]
        for sql in mutations:
            self.catalog.tree()
            self.catalog.state()
            with self.catalog.connect() as c:c.execute(sql)
            self.assertEqual(self.catalog.tree(),self.expected(),sql)
            with self.catalog.connect() as c:expected=c.execute('SELECT count(*) FROM capture_items').fetchone()[0]
            self.assertEqual(self.catalog.state()['counts']['total'],expected)
        with self.catalog.connect() as c:fixtures.CatalogSearchCandidateTests.add(c,'new','one','新目录/new.jpg','new','')
        self.assertEqual(self.catalog.tree(),self.expected())

    def test_old_snapshot_is_not_published_after_concurrent_change(self):
        original=self.catalog.summaries.write
        def changed(*args,**kwargs):
            with self.catalog.connect() as c:fixtures.CatalogSearchCandidateTests.add(c,'new','one','新增/new.jpg','new','')
            original(*args,**kwargs)
        with patch.object(self.catalog.summaries,'write',side_effect=changed):self.catalog.tree()
        self.assertEqual(self.catalog.tree(),self.expected())

    def test_metadata_details_do_not_prevent_publishing_unchanged_aggregates(self):
        original=self.catalog.summaries.write
        def metadata_changed(*args,**kwargs):
            with self.catalog.connect() as c:c.execute("UPDATE items SET width=coalesce(width,0)+1 WHERE id='jpg'")
            original(*args,**kwargs)
        with patch.object(self.catalog.summaries,'write',side_effect=metadata_changed):
            tree=self.catalog.tree();counts=self.catalog.state()['counts']
        with patch('asset_catalog._ranked_catalog',side_effect=AssertionError('详情变化不应阻止聚合快照发布')):
            self.assertEqual(self.catalog.tree(),tree)
            self.assertEqual(self.catalog.state()['counts'],counts)

    def test_cancelled_tree_does_not_publish_snapshot(self):
        with self.catalog.connect() as c:
            for number in range(1000):fixtures.CatalogSearchCandidateTests.add(c,str(number),'one',f'bulk/{number}.jpg',str(number),'')
        checks=[0]
        def cancel():
            checks[0]+=1;return checks[0]>1
        with self.assertRaises(SearchCancelled):self.catalog.tree(cancel=cancel)
        self.assertFalse((self.data/'catalog-summaries/tree-all.json').exists())

    def test_first_summary_publishes_while_main_database_writer_is_held(self):
        with closing(sqlite3.connect(self.catalog.db)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            try:
                tree=self.catalog.tree();counts=self.catalog.state()['counts']
                self.assertTrue((self.data/'catalog-summaries/tree-all.json').is_file())
                self.assertTrue((self.data/'catalog-summaries/counts.json').is_file())
            finally:writer.rollback()
        with patch('asset_catalog._ranked_catalog',side_effect=AssertionError('业务写锁不能导致快照丢失')):
            self.assertEqual(self.catalog.tree(),tree);self.assertEqual(self.catalog.state()['counts'],counts)

    def test_summary_preserves_unknown_file_and_rejects_other_database_payload(self):
        cache=self.catalog.summaries;cache.folder.mkdir()
        path=cache.folder/'tree-all.json';path.write_bytes(b'unknown user file')
        self.assertEqual(self.catalog.tree(),self.expected())
        self.assertEqual(path.read_bytes(),b'unknown user file')
        path.write_text(json.dumps({'schema':cache.SCHEMA,'kind':'tree:','database':'other',
                                    'revision':0,'payload':{'wrong-root':[]}}),encoding='utf8')
        self.assertEqual(self.catalog.tree(),self.expected())
        self.assertEqual(json.loads(path.read_text(encoding='utf8'))['database'],cache.database)
