import base64
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from asset_archive import AssetArchive
from everything_search import EverythingSearch,literal_query
from sku_match import normalize

ROOT=Path(__file__).resolve().parents[1]


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.archive=AssetArchive(SimpleNamespace(data=self.base,root=ROOT),autostart=False)
        with self.archive.connect() as c:
            c.executemany('INSERT INTO assets(id,created,sku,normalized,project,scope) VALUES (?,?,?,?,?,?)',[(f'a{i:03d}',float(i//2),'SKU-中文123',normalize('SKU-中文123'),'p','细节图') for i in range(145)])
            c.execute("INSERT INTO assets(id,created,sku,normalized,project,scope) VALUES ('other',200,'SKU中文1234','SKU1234','other','模特图')")
            c.execute("INSERT INTO files(id,asset,component) VALUES ('f','a144','jpg')")
            c.execute("INSERT INTO locations VALUES ('D:/照片/IMG001.JPG','f',1,'online')")
            c.execute("INSERT INTO catalog_links VALUES ('s','c','a144')")
    def tearDown(self):self.archive.close();self.temp.cleanup()
    def test_exact_prefix_and_exact_scope(self):
        exact=self.archive.search(sku='sku-中文123',mode='exact')
        self.assertEqual(exact['rows'][0]['id'],'a144');self.assertIsNone(exact['total']);self.assertTrue(exact['has_more'])
        self.assertEqual(self.archive.search(sku='SKU中文123',project='other')['rows'][0]['id'],'other')
        self.assertEqual(self.archive.search(sku='SKU中文123',scope='细节')['rows'],[])
        self.assertEqual(self.archive.search(sku='SKU中文123',project='other',mode='exact')['rows'],[])
    def test_cursor_duplicate_timestamps_no_gaps_or_repeats(self):
        ids=[];cursor=''
        while True:
            page=self.archive.search(project='p',cursor=cursor)
            ids.extend(r['id'] for r in page['rows'])
            if not page['has_more']:break
            cursor=page['next_cursor']
        self.assertEqual(len(ids),145);self.assertEqual(len(set(ids)),145)
        self.assertEqual(ids,[f'a{i:03d}' for i in range(144,-1,-1)])
        with self.assertRaises(ValueError):self.archive.search(project='other',cursor=cursor)
        with self.assertRaises(ValueError):self.archive.search(cursor='not-json')
    def test_bound_page_batch_joins_and_no_filesystem_probe(self):
        with patch.object(Path,'is_file',side_effect=AssertionError('搜索不准探测原片')):
            rows=self.archive.search(project='p',limit=200)['rows']
        self.assertEqual(len(rows),100);self.assertEqual(rows[0]['locations'][0]['path'],'D:/照片/IMG001.JPG')
        self.assertEqual(rows[0]['preview'],'/api/preview/s/c?size=480')
    def test_query_plans_and_reopen_migration(self):
        with self.archive.connect() as c:
            plan=' '.join(r[3] for r in c.execute('EXPLAIN QUERY PLAN SELECT * FROM assets WHERE project=? AND scope=? ORDER BY normalized DESC,created DESC,id DESC LIMIT 61',('p','细节图')))
        self.assertIn('assets_project_scope_search',plan);self.assertNotIn('TEMP B-TREE',plan)
        self.archive.close();self.archive=AssetArchive(SimpleNamespace(data=self.base,root=ROOT),autostart=False)
        self.assertEqual(len(self.archive.search(project='p')['rows']),60)
    def test_everything_fallback_timeout_and_case_insensitive_known_path(self):
        service=EverythingSearch(self.archive)
        with patch.object(service,'_request',return_value={'available':False,'message':'离线'}):
            r=service.search('IMG001')
        self.assertEqual(r['source'],'archive');self.assertEqual(r['rows'][0]['asset']['id'],'a144');self.assertFalse(r['rows'][0]['identity_verified'])
        with patch.object(service,'_request',return_value={'available':True,'paths':['d:/照片/img001.jpg'],'has_more':False}):r=service.search('IMG001')
        self.assertEqual(r['rows'][0]['asset']['id'],'a144')
        # 模拟一个真实存在的 SDK 路径，确保测试覆盖 subprocess timeout 分支。
        service.dll=self.base/'sdk.dll';service.dll.touch()
        with patch('everything_search.subprocess.run',side_effect=subprocess.TimeoutExpired('query',1.5)):
            r=service.search('IMG001')
        self.assertEqual(r['source'],'archive');self.assertIn('超时',r['message']);self.assertFalse(service.gate.locked())
    def test_literal_query_and_pagination_limits(self):
        self.assertIn('"D:\\照片\\IMG001"',literal_query('D:\\照片\\IMG001'))
        for text in ['','" | file:', 'foo*', 'foo\nbar','x'*181]:
            with self.assertRaises(ValueError):literal_query(text)
        with self.assertRaises(ValueError):EverythingSearch(self.archive).search('IMG',offset=10001)

    def test_disabled_everything_never_starts_a_worker_and_reports_native_fallback(self):
        with patch.dict('everything_search.os.environ',{'FRAMEKEEP_DISABLE_EVERYTHING':'1'}),patch('everything_search.subprocess.run',side_effect=AssertionError('不应启动 Everything worker')):
            service=EverythingSearch(self.archive);status=service.status();result=service.search('IMG001')
        self.assertFalse(status['available']);self.assertIn('本机快速索引',status['message'])
        self.assertEqual(result['source'],'archive');self.assertEqual(result['rows'][0]['asset']['id'],'a144')


if __name__=='__main__':unittest.main()
