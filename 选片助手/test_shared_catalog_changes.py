"""正式visited模式的真实文件通知、关闭期间对账及快捷搜索同步。"""
from pathlib import Path
from contextlib import ExitStack
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from archive_visits import ArchiveVisits
from asset_catalog import AssetCatalog
from asset_intelligence import AssetIntelligence
from model_providers import Providers
from reference_library import ReferenceLibrary


@unittest.skipUnless(os.name=='nt','真实ReadDirectoryChangesW验收需要Windows')
class SharedCatalogChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='lr-shared-changes-');self.base=Path(self.temp.name)
        self.photos=self.base/'photos';self.photos.mkdir();(self.photos/'old-folder').mkdir()
        self.image(self.photos/'KEEP7877.JPG');self.image(self.photos/'old-folder'/'NEST7877.JPG')
        self.catalog=AssetCatalog(self.base/'data');self.root=self.catalog.add_root(str(self.photos))['root_id']
        self.catalog._scan(self.root)
        self.catalog.visits=ArchiveVisits(self.catalog)
        self.providers=Providers(self.catalog.data)
        self.service=AssetIntelligence(self.catalog,self.providers,autostart=False);self.service.quick_index.build()
        while self.service.quick_index.sync():pass
        self.stack=ExitStack();self.stack.enter_context(patch('reference_library.WATCH_RETRY_SECONDS',.1));self.stack.enter_context(patch('reference_library.RECONCILE_RETRY_SECONDS',.1))
        self.library=ReferenceLibrary(self.catalog.data,catalog=self.catalog,autostart=False)

    def tearDown(self):
        self.library.close();self.service.close();self.catalog.close();self.stack.close();self.temp.cleanup()

    @staticmethod
    def image(path,color='teal'):
        Image.new('RGB',(40,50),color).save(path)

    def until(self,callback,timeout=12):
        deadline=time.monotonic()+timeout
        while True:
            result=callback()
            if result:return result
            with self.library.lock:state=next((entry for entry in self.library.config['roots'] if entry['id']==self.root),{})
            self.assertLess(time.monotonic(),deadline,'等待文件监控/快捷索引同步超时：'+str(state.get('monitor_error','')))
            time.sleep(.03)

    def search(self,text,**kwargs):return self.service.search(text=text,quick=True,**kwargs)['rows']

    def start(self):
        self.library.start()
        self.until(lambda:self.library._root(self.root).get('monitor_checked_at') and self.root not in self.library.index_active)
        self.until(lambda:self.root in self.library.watchers and self.library.watchers[self.root].active)

    def test_real_add_delete_file_and_directory_rename_update_quick_search(self):
        self.start();original=self.search('KEEP7877')[0]['id']
        added=self.photos/'ADDED7877.JPG';self.image(added)
        self.until(lambda:len(self.search('ADDED7877'))==1)
        renamed=self.photos/'RENAMED7877.JPG';added.rename(renamed)
        self.until(lambda:len(self.search('RENAMED7877'))==1 and not self.search('ADDED7877'))
        old=self.photos/'old-folder';new=self.photos/'new-folder'
        self.assertTrue(old.resolve().is_relative_to(self.base.resolve()));self.assertEqual(new.resolve().parent,self.base.resolve()/'photos')
        old.rename(new)
        self.until(lambda:len(self.search('new-folder'))==1 and not self.search('old-folder'))
        shutil.rmtree(new);renamed.unlink()
        self.until(lambda:not self.search('NEST7877') and not self.search('RENAMED7877'))
        (self.photos/'KEEP7877.JPG').unlink()
        self.until(lambda:not self.search('KEEP7877'))
        self.assertEqual(self.catalog.item(original)['online'],0,'删除只改变可用性，业务记录仍保留')

    def test_restart_reconciles_changes_while_application_was_closed(self):
        self.start();self.library.close()
        (self.photos/'KEEP7877.JPG').unlink()
        original=self.photos/'old-folder';moved=self.photos/'moved-folder'
        self.assertTrue(original.resolve().is_relative_to(self.base.resolve()));self.assertEqual(moved.resolve().parent,self.photos.resolve())
        original.rename(moved);self.image(self.photos/'OFFLINE7877.JPG')
        self.library=ReferenceLibrary(self.catalog.data,catalog=self.catalog,autostart=False);self.start()
        self.until(lambda:not self.search('KEEP7877') and not self.search('old-folder') and len(self.search('moved-folder'))==1 and len(self.search('OFFLINE7877'))==1)

    def test_case_only_rename_keeps_current_path_and_manual_fields(self):
        self.start();row=self.search('KEEP7877')[0];self.catalog.assign(row['id'],'MANUAL-CASE')
        (self.photos/'KEEP7877.JPG').rename(self.photos/'keep7877.jpg')
        self.until(lambda:self.catalog.item(row['id'])['name']=='keep7877.jpg')
        self.assertEqual(self.catalog.item(row['id'])['sku'],'MANUAL-CASE')
        (self.photos/'old-folder').rename(self.photos/'OLD-FOLDER')
        self.until(lambda:any('/OLD-FOLDER/' in item['path'].replace('\\','/') for item in self.search('NEST7877')))

    def test_removed_root_stops_watcher_and_keeps_source_files(self):
        self.start();self.library.remove_root(self.root)
        self.until(lambda:self.root not in self.library.watchers and not self.search('KEEP7877'))
        self.assertTrue((self.photos/'KEEP7877.JPG').is_file())

    def test_offline_root_preserves_records_and_reconnect_reconciles(self):
        self.start();self.library.close();offline=self.base/'offline-photos'
        self.assertEqual(offline.resolve().parent,self.base.resolve());self.assertTrue(self.photos.resolve().is_relative_to(self.base.resolve()))
        self.photos.rename(offline)
        self.library=ReferenceLibrary(self.catalog.data,catalog=self.catalog,autostart=False);self.library.start()
        self.until(lambda:self.library._root(self.root).get('monitor_error'))
        self.assertEqual(len(self.search('KEEP7877')),1)
        (offline/'KEEP7877.JPG').unlink();offline.rename(self.photos)
        self.until(lambda:not self.search('KEEP7877') and len(self.search('NEST7877'))==1)
        self.until(lambda:not self.library._root(self.root).get('monitor_error'))

    def test_changed_file_preserves_manual_sku_and_unchanged_reconcile_does_not_rewrite(self):
        self.start();row=self.search('KEEP7877')[0]
        self.catalog.assign(row['id'],'MANUAL-7877')
        old=self.catalog.item(row['id'])['fingerprint'];self.image(self.photos/'KEEP7877.JPG','red')
        self.until(lambda:self.catalog.item(row['id'])['fingerprint']!=old)
        self.until(lambda:len(self.search('MANUAL-7877'))==1)
        self.assertEqual(self.catalog.item(row['id'])['sku'],'MANUAL-7877')
        self.until(lambda:self.root not in self.library.delta_active and self.root not in self.library.index_active)
        with self.catalog.connect() as c:before=[tuple(row) for row in c.execute('SELECT id,last_seen,fingerprint,sku FROM items ORDER BY id')]
        checked=self.library._root(self.root)['monitor_checked_at'];self.library.enqueue(self.root)
        self.until(lambda:self.library._root(self.root)['monitor_checked_at']>checked and self.root not in self.library.index_active)
        with self.catalog.connect() as c:after=[tuple(row) for row in c.execute('SELECT id,last_seen,fingerprint,sku FROM items ORDER BY id')]
        self.assertEqual(before,after)

    def test_overflow_repairs_missed_notifications(self):
        overflow=threading.Event()
        class Signal:
            active=True
            def __init__(self,*args,**kwargs):pass
            def poll(self):
                state=overflow.is_set();overflow.clear();return {'paths':[],'overflow':state}
            def close(self):self.active=False
        with patch('reference_library.DirectoryChanges',Signal):
            self.start();(self.photos/'KEEP7877.JPG').unlink();self.image(self.photos/'MISSED7877.JPG');overflow.set()
            self.until(lambda:not self.search('KEEP7877') and len(self.search('MISSED7877'))==1)

    def test_permission_failure_does_not_treat_folder_as_deleted(self):
        from catalog_file_changes import reconcile
        original=os.scandir
        def denied(path):
            if Path(path)==self.photos/'old-folder':raise PermissionError('temporarily denied')
            return original(path)
        with patch('catalog_file_changes.os.scandir',side_effect=denied):
            with self.assertRaises(PermissionError):reconcile(self.library,self.root)
        self.assertEqual(len(self.search('NEST7877')),1)
        resume=self.library._root(self.root)['monitor_resume']
        self.assertIn('old-folder',resume['folders'])
        self.start();self.until(lambda:not self.library._root(self.root).get('monitor_resume'))

    def test_unreadable_directory_does_not_block_other_directory_reconciliation(self):
        from catalog_file_changes import reconcile
        other=self.photos/'zzz-other';other.mkdir();photo=other/'REMOVED7877.JPG';self.image(photo)
        self.catalog._scan(self.root,visit_folder='zzz-other');photo.unlink()
        original=os.scandir
        def denied(path):
            if Path(path)==self.photos/'old-folder':raise PermissionError('temporarily denied')
            return original(path)
        with patch('catalog_file_changes.os.scandir',side_effect=denied):
            with self.assertRaises(PermissionError):reconcile(self.library,self.root)
        self.until(lambda:not self.search('REMOVED7877'))
        self.assertEqual(len(self.search('NEST7877')),1)
        self.assertIn('old-folder',self.library._root(self.root)['monitor_resume']['folders'])

    def test_nested_roots_do_not_steal_each_others_items(self):
        self.start();child=self.library.add_root(self.photos/'old-folder')['root_id']
        self.until(lambda:child not in self.library.index_active)
        self.image(self.photos/'old-folder'/'CHILD7877.JPG')
        self.until(lambda:len(self.search('CHILD7877',root=child))==1)
        self.assertFalse(self.search('CHILD7877',root=self.root))
        self.library.enqueue(self.root);self.until(lambda:self.root not in self.library.index_active)
        self.assertEqual(len(self.search('CHILD7877',root=child)),1)
