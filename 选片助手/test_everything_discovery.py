"""Everything 自动首批发现：覆盖不完整、路径边界和既有扫描回退。"""
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from asset_catalog import AssetCatalog
from catalog_store import Store
from everything_search import EverythingSearch,folder_query,root_aliases,canonical_result

ROOT=Path(__file__).resolve().parents[1]


class DiscoveryTests(unittest.TestCase):
    def test_internal_write_and_recycle_directories_are_not_scanned(self):
        for name in ('.framekeep-write-disappearing','#recycle','$RECYCLE.BIN'):
            folder=self.photos/name;folder.mkdir();(folder/'internal.JPG').write_bytes(b'internal')
        catalog=AssetCatalog(self.base/'data')
        self.assertEqual(catalog.add_root(str(self.photos))['indexed'],2)

    def test_unc_and_mapped_roots_share_scope_and_canonical_results(self):
        with patch('everything_search.network_mappings',return_value=[('Y:\\',r'\\server\photos')]):
            self.assertEqual(root_aliases(r'\\server\photos\2026'),[r'\\server\photos\2026',r'Y:\2026'])
            self.assertEqual(root_aliases(r'Y:\2026'),[r'Y:\2026',r'\\server\photos\2026'])
        self.assertEqual(canonical_result(r'Y:\2026\中文.JPG',r'\\server\photos',[r'\\server\photos','Y:\\']),r'\\server\photos\2026\中文.JPG')
        with self.assertRaises(ValueError):canonical_result(r'Y:\photos2\a.JPG',r'\\server\photos',[r'Y:\photos'])
        self.assertIn('parent:',folder_query(self.photos,recursive=False))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.photos=self.base/'照片';self.photos.mkdir()
        self.first=self.photos/'A.JPG';self.second=self.photos/'B.JPG'
        for path in (self.first,self.second):Image.new('RGB',(30,20),'teal').save(path)
        self.service=EverythingSearch(SimpleNamespace(store=SimpleNamespace(root=ROOT)))

    def tearDown(self):self.temp.cleanup()

    def test_literal_root_boundary_and_candidates_verified_before_use(self):
        self.assertIn(str(self.photos).replace('/','\\')+'\\"',folder_query(self.photos))
        with self.assertRaises(ValueError):folder_query('bad" | file:')
        outside=self.base/'照片2';outside.mkdir();other=outside/'A.JPG';other.write_bytes(b'x')
        hidden=self.photos/'.framekeep-hidden';hidden.mkdir();h=hidden/'A.JPG';h.write_bytes(b'x')
        sub=self.photos/'sub';sub.mkdir();nested=sub/'C.JPG';nested.write_bytes(b'x')
        paths=[str(p) for p in (self.first,self.first,other,h,nested,self.photos/'missing.JPG',self.photos)]
        with patch.object(self.service,'_request',return_value={'available':True,'paths':paths}) as request:
            result=list(self.service.candidates(self.photos,recursive=False))
        self.assertEqual(result,[self.first]);self.assertEqual(request.call_args.args[0]['limit'],2048)
        with patch.object(self.service,'_request',return_value={'available':True,'paths':paths}):
            self.assertEqual(list(self.service.candidates(self.photos,exclude=[sub])),[self.first])

    def test_index_head_is_visible_before_walk_and_omissions_are_filled(self):
        catalog=AssetCatalog(self.base/'data');catalog.discovery=self.service
        events=[];catalog.on_previews=lambda root,records:events.append(records)
        original=catalog._media_paths
        def walk(*args):
            self.assertEqual(catalog.items()['total'],1)
            self.assertTrue(events)
            yield from original(*args)
        with patch.object(self.service,'_request',return_value={'available':True,'paths':[str(self.first)]}),patch.object(catalog,'_media_paths',side_effect=walk):
            result=catalog.add_root(str(self.photos))
        self.assertEqual(result['indexed'],2);self.assertEqual(result['indexed_candidates'],1)
        self.assertEqual(catalog.items()['total'],2)
        # 空索引不把仍在原目录的照片误判离线。
        with patch.object(self.service,'_request',return_value={'available':True,'paths':[]}):catalog.scan(result['root_id'])
        self.assertEqual(catalog.items()['total'],2)
        with patch.object(self.service,'_request',return_value={'available':False,'message':'超时'}):catalog.scan(result['root_id'])
        self.assertEqual(catalog.items()['total'],2)

    def test_incomplete_walk_keeps_previous_online_records(self):
        catalog=AssetCatalog(self.base/'data');root=catalog.add_root(str(self.photos))['root_id']
        def broken(*args):
            yield self.first
            raise PermissionError('子目录不可读')
        with patch.object(catalog,'_media_paths',side_effect=broken),self.assertRaisesRegex(ValueError,'不可读'):catalog.scan(root)
        self.assertEqual(catalog.items()['total'],2);self.assertEqual(catalog._root(root)['status'],'error')

    def test_monitor_early_preview_does_not_read_metadata_twice(self):
        store=Store(self.base/'data',ROOT);store.discovery=self.service;events=[]
        store.on_previews=lambda root,records:events.extend(r['name'] for r in records)
        metadata=[]
        def read(paths,*args,**kwargs):metadata.extend(paths);return [{} for _ in paths]
        with patch.object(self.service,'_request',return_value={'available':True,'paths':[str(self.first)]}),patch('media_engine.find_exiftool',return_value=Path('unused-exiftool.exe')),patch('media_engine.read_metadata',side_effect=read):
            job=store.scan(str(self.photos))
            until=time.monotonic()+5
            while store.scan_worker_running and time.monotonic()<until:time.sleep(.01)
        self.assertEqual(store.jobs[job]['status'],'completed',store.jobs[job]);self.assertEqual(store.jobs[job]['indexed_candidates'],1)
        self.assertEqual(sorted(metadata),[self.first,self.second]);self.assertEqual(len(store.records()),2)
        self.assertEqual(events[0],'A')

    def test_stale_index_preview_failure_does_not_cancel_directory_scan(self):
        store=Store(self.base/'data',ROOT);store.discovery=self.service;store.on_previews=lambda *args:None
        calls=0
        def read(paths,*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:raise OSError('索引候选读取期间离线')
            return [{} for _ in paths]
        with patch.object(self.service,'_request',return_value={'available':True,'paths':[str(self.first)]}),patch('media_engine.find_exiftool',return_value=Path('unused-exiftool.exe')),patch('media_engine.read_metadata',side_effect=read):
            job=store.scan(str(self.photos));until=time.monotonic()+5
            while store.scan_worker_running and time.monotonic()<until:time.sleep(.01)
        self.assertEqual(store.jobs[job]['status'],'completed',store.jobs[job]);self.assertEqual(len(store.records()),2)


if __name__=='__main__':unittest.main()
