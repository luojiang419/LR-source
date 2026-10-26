"""没有 Everything 时的完整参考索引、热查询及增量生命周期。"""
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from reference_library import ReferenceLibrary
import reference_library


class NativeReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='native-reference-');self.base=Path(self.temp.name)
        self.photos=self.base/'photos';self.photos.mkdir();Image.new('RGB',(20,20),'teal').save(self.photos/'BASE-100.JPG')
        self.library=ReferenceLibrary(self.base/'data');self.root=self.library.add_root(self.photos)['root_id'];self.ready(self.root)

    def tearDown(self):self.library.close();self.temp.cleanup()

    def until(self,fn,timeout=8):
        deadline=time.monotonic()+timeout
        while True:
            value=fn()
            if value:return value
            self.assertLess(time.monotonic(),deadline,'等待索引变化超时');time.sleep(.03)

    def ready(self,root):
        self.until(lambda:self.library._root(root)['status'] not in ('queued','scanning'))
        self.assertEqual(self.library._root(root)['status'],'ready',self.library._root(root))

    def count(self,text):return self.library.items(self.root,text=text)['total']

    def test_hot_search_and_global_search_do_not_access_source_filesystem(self):
        self.assertTrue(self.library._root(self.root)['native_complete'])
        with patch.object(self.library,'resolve',side_effect=AssertionError('热搜索不解析原目录')),patch('reference_library.os.scandir',side_effect=AssertionError('热搜索不遍历原目录')),patch.object(Path,'stat',side_effect=AssertionError('热搜索不读取原文件属性')):
            result=self.library.items(self.root,text='BASE-100')
            global_result=self.library.search_all('BASE-100')
        self.assertEqual(result['total'],1);self.assertEqual(global_result['total'],1)
        self.assertIn('索引',result['engine']);self.assertNotIn('Everything',result['engine'])

    def test_everything_disappearing_automatically_builds_independent_index(self):
        from types import SimpleNamespace
        folder=self.base/'sdk-covered';folder.mkdir();photo=folder/'NO-SDK-500.JPG';Image.new('RGB',(20,20),'blue').save(photo)
        available=[True]
        self.library.search=SimpleNamespace(_request=lambda query:{'available':available[0],'paths':[str(photo)] if available[0] else [],'total':1})
        root=self.library.add_root(folder)['root_id'];self.ready(root)
        self.assertFalse(self.library._root(root).get('native_complete',False))
        available[0]=False;self.library.items(root,text='NO-SDK-500')
        self.until(lambda:self.library._root(root).get('native_complete'))
        self.assertEqual(self.library.items(root,text='NO-SDK-500')['total'],1)

    def test_first_committed_batch_can_be_searched_before_scan_finishes(self):
        folder=self.base/'stream';folder.mkdir();entered=threading.Event();release=threading.Event()
        original=reference_library.iter_tree_records
        def streaming(root,**options):
            if Path(root)!=folder:yield from original(root,**options);return
            for number in range(400):
                if number==128:entered.set();release.wait(8)
                yield {'path':str(folder/f'PARTIAL-{number:03d}.JPG'),'size':10,'modified_ns':1700000000000000000}
        try:
            with patch('reference_library.iter_tree_records',streaming):
                root=self.library.add_root(folder)['root_id'];self.assertTrue(entered.wait(5))
                result=self.library.items(root,text='PARTIAL')
                self.assertEqual(result['total'],128);self.assertEqual(result['status'],'scanning')
                release.set();self.ready(root)
                self.assertEqual(self.library.items(root,text='PARTIAL')['total'],400)
        finally:release.set()

    def test_initial_scan_reports_directory_estimate_without_counting_twice(self):
        folder=self.base/'progress';folder.mkdir()
        for name in ('A','B','C'):
            child=folder/name;child.mkdir()
            for index in range(130):(child/f'PROGRESS-{index:03d}.JPG').write_bytes(b'index metadata only')
        entered=threading.Event();release=threading.Event();original=reference_library.iter_tree_records
        def streaming(root,**options):
            if Path(root)!=folder:yield from original(root,**options);return
            report=options['on_progress']
            def progress(done,total):
                report(done,total)
                if done==2 and total==4:entered.set();release.wait(8)
            options['on_progress']=progress
            yield from original(root,**options)
        try:
            with patch('reference_library.iter_tree_records',streaming):
                root=self.library.add_root(folder)['root_id'];self.assertTrue(entered.wait(5))
                progress=self.library._root(root)['progress']
                self.assertEqual(progress['percent'],50);self.assertTrue(progress['estimated'])
                self.assertEqual(progress['total'],0);self.assertEqual(progress['basis'],'directories')
                # 并行扫描可交错返回多个子目录；只要求已提交批次可立即搜索。
                self.assertGreaterEqual(progress['done'],128);self.assertLessEqual(progress['done'],390)
                self.assertEqual(self.library.items(root,text='PROGRESS')['total'],progress['done']//128*128)
                release.set();self.ready(root)
                self.assertEqual(self.library._root(root)['progress']['percent'],100)
                self.assertEqual(self.library._root(root)['progress']['done'],390)
        finally:release.set()

    @unittest.skipUnless(reference_library.INDEX_WORKERS>=2,'本机硬件预算不足 2 路')
    def test_slow_root_does_not_block_another_roots_index(self):
        slow=self.base/'slow';slow.mkdir();fast=self.base/'fast';fast.mkdir()
        Image.new('RGB',(10,10),'red').save(fast/'FAST-200.JPG')
        entered=threading.Event();release=threading.Event();original=reference_library.iter_tree_records
        def streaming(root,**options):
            if Path(root)==slow:entered.set();release.wait(8)
            yield from original(root,**options)
        try:
            with patch('reference_library.iter_tree_records',streaming):
                first=self.library.add_root(slow)['root_id'];self.assertTrue(entered.wait(5))
                second=self.library.add_root(fast)['root_id'];self.ready(second)
                self.assertEqual(self.library._root(first)['status'],'scanning')
                self.assertEqual(self.library.items(second,text='FAST-200')['total'],1)
                release.set();self.ready(first)
        finally:release.set()

    @unittest.skipUnless(reference_library.INDEX_WORKERS>=4,'本机硬件预算不足 4 路')
    def test_three_blocked_roots_do_not_block_a_fourth_root(self):
        release=threading.Event();entered=[threading.Event() for _ in range(3)]
        slow=[self.base/f'slow-{index}' for index in range(3)]
        fast=self.base/'fourth';fast.mkdir();Image.new('RGB',(10,10),'blue').save(fast/'FAST-400.JPG')
        for folder in slow:folder.mkdir()
        original=reference_library.iter_tree_records
        def streaming(root,**options):
            if Path(root) in slow:
                entered[slow.index(Path(root))].set();release.wait(8)
            yield from original(root,**options)
        try:
            with patch('reference_library.iter_tree_records',streaming):
                for index,folder in enumerate(slow):
                    self.library.add_root(folder);self.assertTrue(entered[index].wait(5))
                root=self.library.add_root(fast)['root_id'];self.ready(root)
                self.assertEqual(self.library.items(root,text='FAST-400')['total'],1)
                release.set();self.until(lambda:not self.library.index_active)
        finally:release.set()

    def test_disappearing_subdirectory_retries_without_discarding_snapshot(self):
        folder=self.base/'moving-tree';folder.mkdir();Image.new('RGB',(10,10),'red').save(folder/'RETRY-600.JPG')
        original=reference_library.iter_tree_records;attempts=[]
        def transient(root,**options):
            if Path(root)==folder:
                attempts.append(1)
                if len(attempts)==1:raise FileNotFoundError('扫描中子目录被移走')
            yield from original(root,**options)
        with patch('reference_library.iter_tree_records',transient):
            root=self.library.add_root(folder)['root_id'];self.ready(root)
        self.assertEqual(len(attempts),2)
        self.assertEqual(self.library.items(root,text='RETRY-600')['total'],1)

    def test_real_notifications_add_rename_delete_and_directory_subtrees(self):
        self.until(lambda:self.root in self.library.watchers)
        added=self.photos/'FRESH-200.JPG';Image.new('RGB',(20,20),'blue').save(added)
        self.until(lambda:self.count('FRESH-200')==1)
        renamed=self.photos/'RENAMED-300.JPG';added.rename(renamed)
        self.until(lambda:self.count('RENAMED-300')==1 and self.count('FRESH-200')==0)
        lower=self.photos/'renamed-300.jpg';renamed.rename(lower)
        self.until(lambda:self.library.items(self.root,text='renamed-300')['rows'][0]['name']=='renamed-300.jpg')
        renamed=lower
        old=self.photos/'old-directory';nested=old/'nested';nested.mkdir(parents=True)
        Image.new('RGB',(20,20),'green').save(nested/'SUBTREE-400.JPG')
        self.until(lambda:self.count('old-directory')==1)
        new=self.photos/'new-directory';old.rename(new)
        self.until(lambda:self.count('new-directory')==1 and self.count('old-directory')==0)
        shutil.rmtree(new);renamed.unlink()
        self.until(lambda:self.count('SUBTREE-400')==0 and self.count('RENAMED-300')==0)
        self.assertEqual(self.count('BASE-100'),1)

    def test_restart_offline_root_keeps_searchable_snapshot_and_recovers(self):
        self.library.close();moved=self.base/'offline-photos';self.photos.rename(moved)
        self.library=ReferenceLibrary(self.base/'data')
        self.until(lambda:self.library._root(self.root)['status']=='error')
        with patch.object(self.library,'resolve',side_effect=AssertionError('离线查询不可访问 NAS')):
            result=self.library.items(self.root,text='BASE-100')
        self.assertEqual(result['total'],1);self.assertTrue(result['incomplete'])
        moved.rename(self.photos);self.library.rescan(self.root);self.ready(self.root)
        self.assertFalse(self.library.items(self.root,text='BASE-100')['incomplete'])

    def test_overflow_triggers_reconciliation_and_preserves_unchanged_ids(self):
        self.library.close();overflow=threading.Event()
        class Signal:
            active=True
            def __init__(self,*args,**kwargs):pass
            def poll(self):
                state=overflow.is_set();overflow.clear();return {'paths':[],'overflow':state}
            def close(self):self.active=False
        with patch('reference_library.DirectoryChanges',Signal):
            self.library=ReferenceLibrary(self.base/'data');self.ready(self.root)
            catalog=self.library.catalog(self.root)
            with catalog.connect() as c:epoch=c.execute('SELECT scan_epoch FROM roots').fetchone()[0]
            before=self.library.items(self.root,text='BASE-100')['rows'][0]['id'];overflow.set()
            def rescanned():
                with catalog.connect() as c:return c.execute('SELECT scan_epoch FROM roots').fetchone()[0]>epoch
            self.until(rescanned);self.ready(self.root)
            self.assertEqual(self.library.items(self.root,text='BASE-100')['rows'][0]['id'],before)
