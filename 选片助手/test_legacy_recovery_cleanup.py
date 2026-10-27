"""旧版 bin 目录的清理授权、边界、链接及一次性升级标记验证。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
from contextlib import contextmanager,closing
from types import SimpleNamespace
import sqlite3

import legacy_recovery_cleanup as cleanup


@unittest.skipUnless(os.name=='nt','Windows 目录句柄保护')
class LegacyRecoveryCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.photos=self.base/'photos';self.photos.mkdir();self.data=self.base/'data';self.data.mkdir()

    def tearDown(self):self.temp.cleanup()

    def legacy(self,parent=None,number=1):
        path=(parent or self.photos)/('.framekeep-recovery-'+f'{number:032x}');path.mkdir()
        (path/'original.bin').write_bytes(b'old media backup')
        return path

    def junction(self,path,target):
        result=subprocess.run(['cmd','/c','mklink','/J',str(path),str(target)],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_missing_photo_and_unverifiable_or_missing_manifest_are_removed(self):
        a=self.legacy();b=self.legacy(number=2)
        (a/'recovery.json').write_text('{broken manifest',encoding='utf8')
        c=self.legacy(number=3);(c/'recovery.json').write_text(json.dumps({'target':'Z:/missing.JPG','expected_sha256':'unknown'}))
        nested=b/'unexpected';nested.mkdir();(nested/'old.bin').write_bytes(b'anything inside the old directory')
        report=cleanup.clean_root(self.photos)
        self.assertEqual(report['removed'],3);self.assertFalse(report['errors'])
        self.assertFalse(a.exists());self.assertFalse(b.exists());self.assertFalse(c.exists())

    def test_source_files_and_other_names_are_untouched(self):
        photo=self.photos/'XSY_9120.JPG';photo.write_bytes(b'actual photo')
        ordinary=self.photos/'original.bin';ordinary.write_bytes(b'user named bin')
        unknown=self.photos/'.framekeep-recovery-not-a-generated-id';unknown.mkdir();(unknown/'original.bin').write_bytes(b'keep')
        current=self.photos/'.framekeep-write-active';current.mkdir();(current/'original.bin').write_bytes(b'keep current write')
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (photo,ordinary,unknown/'original.bin',current/'original.bin')}
        self.legacy();report=cleanup.clean_root(self.photos)
        self.assertEqual(report['removed'],1)
        for p,value in before.items():self.assertEqual((p.read_bytes(),p.stat().st_mtime_ns),value)

    def test_only_registered_roots_nested_folders_are_processed(self):
        sub=self.photos/'2026-10-26';sub.mkdir();a=self.legacy(sub)
        outside=self.base/'outside';outside.mkdir();b=self.legacy(outside)
        report=cleanup.clean_root(self.photos)
        self.assertEqual(report['removed'],1);self.assertFalse(a.exists());self.assertTrue(b.exists())
        with self.assertRaises(ValueError):cleanup.remove_legacy_folder(b,self.photos,threading.Event())

    def test_reparse_points_are_unlinked_or_skipped_without_touching_destination(self):
        outside=self.base/'outside';outside.mkdir();photo=outside/'original.bin';photo.write_bytes(b'do not delete linked destination')
        a=self.legacy();self.junction(a/'nested-link',outside)
        root_link=self.photos/('.framekeep-recovery-'+'a'*32);self.junction(root_link,outside)
        self.junction(self.photos/'ordinary-link',outside)
        report=cleanup.clean_root(self.photos)
        self.assertEqual(report['removed'],2);self.assertFalse(a.exists());self.assertFalse(root_link.exists())
        self.assertEqual(photo.read_bytes(),b'do not delete linked destination')
        (self.photos/'ordinary-link').rmdir()

    def test_parent_cannot_be_renamed_during_recursive_removal(self):
        self.legacy();remove=cleanup._remove_tree
        def guarded(path,stop):
            with self.assertRaises(OSError):self.photos.rename(self.base/'renamed')
            return remove(path,stop)
        with patch.object(cleanup,'_remove_tree',side_effect=guarded):report=cleanup.clean_root(self.photos)
        self.assertEqual(report['removed'],1);self.assertFalse(report['errors'])

    def test_locked_file_is_retained_and_retry_succeeds_after_release(self):
        a=self.legacy();path=a/'original.bin'
        # 复用现有 Windows 文件保护句柄，模拟旧程序仍占用副本。
        import ctypes as C
        from ctypes import wintypes as W
        kernel=C.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE];kernel.CreateFileW.restype=W.HANDLE
        kernel.CloseHandle.argtypes=[W.HANDLE]
        handle=kernel.CreateFileW(str(path),0x80000000,1,None,3,0,None)
        self.assertNotEqual(handle,C.c_void_p(-1).value)
        try:
            report=cleanup.clean_root(self.photos);self.assertEqual(report['removed'],0);self.assertTrue(report['errors']);self.assertTrue(path.exists())
        finally:kernel.CloseHandle(handle)
        self.assertEqual(cleanup.clean_root(self.photos)['removed'],1)

    def test_data_cache_exclusion_stop_and_regular_file_are_preserved(self):
        data=self.photos/'data';data.mkdir();a=self.legacy(data)
        name=self.photos/('.framekeep-recovery-'+'b'*32);name.write_bytes(b'ordinary file')
        b=self.legacy();stop=threading.Event();stop.set()
        self.assertFalse(cleanup.clean_root(self.photos,stop)['complete']);self.assertTrue(b.exists())
        report=cleanup.clean_root(self.photos,exclude=[data]);self.assertEqual(report['removed'],1);self.assertTrue(a.exists());self.assertTrue(name.exists())

    def test_upgrade_runs_once_and_persisted_marker_prevents_restart_scan(self):
        self.legacy();roots=[self.photos]
        worker=cleanup.LegacyRecoveryCleanup(self.data,lambda:roots)
        self.assertEqual(worker.run_once()[0]['removed'],1)
        first=self.legacy();self.assertEqual(worker.run_once(),[]);self.assertTrue(first.exists())
        def no_scan():raise AssertionError('完成后不应再次读取根目录')
        restarted=cleanup.LegacyRecoveryCleanup(self.data,no_scan)
        self.assertEqual(restarted.run_once(),[]);self.assertTrue(first.exists())
        report=json.loads((self.data/'legacy-recovery-cleanup.json').read_text(encoding='utf8'))
        self.assertEqual(report['migration'],cleanup.MIGRATION);self.assertTrue(report['complete'])

    def test_interrupted_upgrade_can_finish_once_on_next_start(self):
        a=self.legacy();worker=cleanup.LegacyRecoveryCleanup(self.data,lambda:[self.photos]);worker.closed.set();worker.run_once()
        self.assertTrue(a.exists());self.assertFalse(json.loads(worker.marker.read_text(encoding='utf8'))['complete'])
        restarted=cleanup.LegacyRecoveryCleanup(self.data,lambda:[self.photos]);self.assertEqual(restarted.run_once()[0]['removed'],1)
        self.assertFalse(a.exists());self.assertTrue(json.loads(restarted.marker.read_text(encoding='utf8'))['complete'])

    def test_background_thread_runs_without_explicit_cleanup_call(self):
        a=self.legacy();worker=cleanup.LegacyRecoveryCleanup(self.data,lambda:[self.photos]);worker.start()
        try:
            deadline=time.monotonic()+5
            while a.exists() and time.monotonic()<deadline:threading.Event().wait(.02)
            self.assertFalse(a.exists())
        finally:worker.close()

    def test_readonly_old_backup_can_be_deleted(self):
        a=self.legacy();(a/'original.bin').chmod(0o444)
        report=cleanup.clean_root(self.photos)
        self.assertEqual(report['removed'],1);self.assertFalse(report['errors']);self.assertFalse(a.exists())

    def test_readonly_hardlink_cleanup_keeps_original_bytes_and_attributes(self):
        photo=self.photos/'original.JPG';photo.write_bytes(b'original source')
        a=self.legacy();(a/'original.bin').unlink();os.link(photo,a/'original.bin');photo.chmod(0o444)
        before=photo.stat().st_file_attributes
        try:
            report=cleanup.clean_root(self.photos);self.assertEqual(report['removed'],1,report)
            self.assertEqual(photo.read_bytes(),b'original source');self.assertEqual(photo.stat().st_file_attributes,before)
        finally:photo.chmod(0o666)

    def test_directory_changed_to_junction_before_open_does_not_escape(self):
        a=self.legacy();outside=self.base/'outside';outside.mkdir();keep=outside/'photo.JPG';keep.write_bytes(b'keep')
        remove=cleanup._remove_tree
        def changed(path,stop):
            path.rename(self.photos/'retained');self.junction(path,outside)
            return remove(path,stop)
        with patch.object(cleanup,'_remove_tree',side_effect=changed):report=cleanup.clean_root(self.photos)
        self.assertEqual(report['removed'],0);self.assertTrue(report['errors']);self.assertEqual(keep.read_bytes(),b'keep')
        a.rmdir()

    def test_configured_monitor_catalog_and_reference_roots_exclude_removed_entries(self):
        db=self.base/'roots.db'
        with closing(sqlite3.connect(db)) as c,c:
            c.execute('CREATE TABLE roots(path TEXT,removed INTEGER)')
            c.executemany('INSERT INTO roots VALUES (?,?)',[('archive',0),('removed',1)])
        @contextmanager
        def connect():
            c=sqlite3.connect(db)
            try:yield c
            finally:c.close()
        store=SimpleNamespace(sessions=lambda:[{'root':'session'}])
        catalog=SimpleNamespace(connect=connect)
        watch=SimpleNamespace(state=lambda:{'folder':'monitor'})
        references=SimpleNamespace(lock=threading.RLock(),config={'roots':[{'path':'reference'},{'path':'removed-reference','removed':True}]})
        self.assertEqual(cleanup.configured_roots(store,catalog,watch,references),['session','archive','monitor','reference'])
