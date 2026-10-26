"""索引开关及迁移的实际 SQLite、并发、失败和重启验收。"""
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from everything_search import EverythingSearch
from reference_library import ReferenceLibrary
from reference_api import local_post


class ReferenceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='reference-storage-');self.base=Path(self.temp.name)
        self.photos=self.base/'photos';self.photos.mkdir()
        Image.new('RGB',(12,12),'teal').save(self.photos/'SSD-100.JPG')
        self.data=self.base/'data'
        self.service=EverythingSearch(SimpleNamespace(store=SimpleNamespace(root=self.base,data=self.data)))
        self.service.set_enabled(False)
        self.library=ReferenceLibrary(self.data,self.service)
        self.root=self.library.add_root(self.photos)['root_id'];self.wait_ready()

    def tearDown(self):
        self.library.close();self.temp.cleanup()

    def wait_ready(self):
        deadline=time.monotonic()+8
        while self.library._root(self.root)['status'] in ('queued','scanning'):
            self.assertLess(time.monotonic(),deadline);time.sleep(.02)
        self.assertEqual(self.library._root(self.root)['status'],'ready',self.library._root(self.root))

    def migrate(self,name='ssd'):
        state=local_post(self.library,'index-storage',{'path':str(self.base/name)})
        self.assertEqual(state['job']['status'],'running')
        self.library.storage.worker.join(10);self.assertFalse(self.library.storage.worker.is_alive())
        return self.library.storage.state()

    def test_disabled_everything_never_starts_sdk_and_restart_retains_setting(self):
        self.service.dll=self.photos/'sdk.dll';self.service.dll.write_bytes(b'not a DLL')
        with patch('everything_search.subprocess.run',side_effect=AssertionError('SDK 不应执行')):
            self.assertFalse(self.service.status()['enabled'])
            self.assertFalse(self.service._request({'probe':True})['available'])
            self.assertEqual(list(self.service.candidates(self.photos)),[])
        second=EverythingSearch(self.service.archive)
        self.assertFalse(second.enabled)
        with self.assertRaises(ValueError):local_post(self.library,'everything',{'enabled':'false'})
        local_post(self.library,'everything',{'enabled':True})
        self.assertTrue(EverythingSearch(self.service.archive).enabled)

    def test_disabling_everything_builds_uncovered_native_snapshot(self):
        with self.library.lock:
            row=self.library.config['roots'][0]
            row.update(native_complete=False,index_source='everything')
        local_post(self.library,'everything',{'enabled':False});self.wait_ready()
        self.assertTrue(self.library._root(self.root)['native_complete'])
        self.assertEqual(self.library.items(self.root,text='SSD-100')['total'],1)

    def test_migration_preserves_records_fts_originals_and_restart(self):
        catalog=self.library.catalog(self.root);old=catalog.db
        with catalog.connect() as c:c.execute("UPDATE items SET sku='manual-preserved',source='manual'")
        original=(self.photos/'SSD-100.JPG').read_bytes()
        state=self.migrate()
        self.assertEqual(state['job']['status'],'completed',state)
        self.assertEqual(state['job']['percent'],100)
        self.assertEqual(Path(state['previous']),old.parent.parent)
        self.assertTrue(old.is_file())
        new=self.library.catalog(self.root)
        self.assertTrue(new.db.is_relative_to(self.base/'ssd'))
        with new.connect() as c:
            self.assertEqual(c.execute('SELECT sku FROM items').fetchone()[0],'manual-preserved')
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')
        self.assertEqual(self.library.items(self.root,text='SSD-100')['total'],1)
        self.assertEqual((self.photos/'SSD-100.JPG').read_bytes(),original)
        path=self.library.storage.path;self.library.close()
        self.library=ReferenceLibrary(self.data,self.service);self.wait_ready()
        self.assertEqual(self.library.storage.path,path)
        self.assertEqual(self.library.items(self.root,text='SSD-100')['total'],1)
        self.assertTrue(self.library.is_internal(path/'file.JPG'))
        # 可再次迁移，旧盘目录继续保留。
        second=self.migrate('ssd-again');self.assertEqual(second['job']['status'],'completed',second)
        self.assertEqual(second['previous'],str(path));self.assertTrue(path.is_dir())

    def test_config_commit_failure_keeps_original_route_and_search(self):
        old=self.library.storage.path
        with patch.object(self.library,'save',side_effect=OSError('模拟配置写入失败')):
            state=self.migrate()
        self.assertEqual(state['job']['status'],'error',state)
        self.assertEqual(self.library.storage.path,old)
        self.assertNotIn('index_storage',self.library.config)
        self.assertEqual(self.library.items(self.root,text='SSD-100')['total'],1)

    def test_rejects_nonempty_relative_photo_and_internal_destinations(self):
        occupied=self.base/'occupied';occupied.mkdir();(occupied/'mine.txt').write_text('keep')
        for target in (occupied,self.photos/'index',self.data/'index',self.library.storage.path,'relative-path'):
            with self.subTest(target=target),self.assertRaises(ValueError):self.library.storage.start(str(target))
        self.assertEqual((occupied/'mine.txt').read_text(),'keep')

    def test_parallel_queries_and_notification_after_cutover(self):
        stop=threading.Event();errors=[];totals=[]
        def query():
            while not stop.is_set():
                try:totals.append(self.library.items(self.root,text='SSD-100')['total'])
                except Exception as error:errors.append(str(error))
        reader=threading.Thread(target=query);reader.start()
        try:state=self.migrate();self.assertEqual(state['job']['status'],'completed',state)
        finally:stop.set();reader.join(5)
        self.assertFalse(errors,errors);self.assertTrue(totals);self.assertEqual(set(totals),{1})
        deadline=time.monotonic()+3
        while self.root not in self.library.watchers:
            self.assertLess(time.monotonic(),deadline);time.sleep(.02)
        Image.new('RGB',(12,12),'blue').save(self.photos/'SSD-200.JPG')
        deadline=time.monotonic()+8
        while self.library.items(self.root,text='SSD-200')['total']!=1:
            self.assertLess(time.monotonic(),deadline);time.sleep(.05)

    def test_unavailable_external_path_never_silently_creates_empty_database(self):
        self.assertEqual(self.migrate()['job']['status'],'completed')
        path=self.library.storage.path;self.library.close();away=path.with_name('unplugged');path.rename(away)
        self.library=ReferenceLibrary(self.data,self.service)
        self.assertIn('不可用',self.library.storage.state()['warning'])
        with self.assertRaisesRegex(ValueError,'不可用'):self.library.catalog(self.root)
        self.assertFalse(path.exists());self.assertEqual(self.library.storage.path,path)

    def test_migration_waits_for_open_operation_and_rejects_duplicate_job(self):
        entered=threading.Event();release=threading.Event()
        def active():
            with self.library.storage.gate.read():entered.set();release.wait(5)
        reader=threading.Thread(target=active);reader.start();self.assertTrue(entered.wait(2))
        try:
            self.library.storage.start(str(self.base/'ssd'))
            with self.assertRaisesRegex(ValueError,'正在迁移'):self.library.storage.start(str(self.base/'other'))
            self.assertEqual(self.library.storage.job['status'],'running')
        finally:release.set();reader.join(3)
        self.library.storage.worker.join(8)
        self.assertEqual(self.library.storage.job['status'],'completed',self.library.storage.job)

    def test_paused_scan_preserves_snapshot_and_resumes_after_migration(self):
        import reference_library
        entered=threading.Event();original=reference_library.iter_tree_records
        def slow(root,**options):
            if not self.library.storage.pausing.is_set() and not entered.is_set():
                entered.set()
                deadline=time.monotonic()+5
                while not self.library.storage.pausing.is_set():
                    if time.monotonic()>deadline:raise RuntimeError('暂停未发生')
                    time.sleep(.01)
                return
            yield from original(root,**options)
        with patch('reference_library.iter_tree_records',slow):
            self.library.rescan(self.root);self.assertTrue(entered.wait(3))
            state=self.migrate();self.assertEqual(state['job']['status'],'completed',state)
            self.wait_ready()
        self.assertTrue(self.library._root(self.root)['native_complete'])
        self.assertEqual(self.library.items(self.root,text='SSD-100')['total'],1)

    def test_backup_failure_does_not_publish_new_configuration(self):
        old=self.library.storage.path
        with patch('reference_storage.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            state=self.migrate()
        self.assertEqual(state['job']['status'],'error')
        self.assertIn('空间不足',state['job']['message'])
        self.assertEqual(self.library.storage.path,old)
        self.assertNotIn('index_storage',self.library.config)
        self.assertEqual(self.library.items(self.root,text='SSD-100')['total'],1)


if __name__=='__main__':unittest.main()
