"""异常退出遗留 WAL 时，启动与短连接关闭不能同步合并历史日志。"""
from pathlib import Path
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from asset_catalog import AssetCatalog
from startup_tasks import StartupTasks


class CatalogWalStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        catalog = AssetCatalog(self.data,keep_wal_open=True)
        self.db = catalog.db
        catalog.close()
        # 模拟实际进程异常退出：提交数据只留在 WAL，主库尚未 checkpoint。
        subprocess.run([sys.executable, '-c', '''
import os,sqlite3,sys
c=sqlite3.connect(sys.argv[1])
c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('CREATE TABLE startup_probe(id INTEGER PRIMARY KEY,payload BLOB)')
c.executemany('INSERT INTO startup_probe(payload) VALUES (?)',[(bytes(65536),)]*64)
c.commit()
os._exit(0)
''', str(self.db)], check=True, timeout=10)
        self.wal = Path(str(self.db)+'-wal')
        self.assertGreater(self.wal.stat().st_size, 4_000_000)

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_restart_reads_committed_wal_without_checkpoint_on_any_startup_close(self):
        before = self.digest(self.db), self.digest(self.wal)
        # 包含真实 FastPathIndex 和 CatalogMetadata 初始化；它们各自关闭连接。
        catalog = AssetCatalog(self.data,keep_wal_open=True)
        try:
            with catalog.connect() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM startup_probe').fetchone()[0], 64)
                self.assertEqual(c.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 0)
            self.assertEqual((self.digest(self.db), self.digest(self.wal)), before)
        finally:
            catalog.close()
        self.assertEqual((self.digest(self.db), self.digest(self.wal)), before)

    def test_commit_rollback_and_explicit_checkpoint_remain_available(self):
        catalog = AssetCatalog(self.data,keep_wal_open=True)
        try:
            before = self.digest(self.db)
            with catalog.connect() as c:
                c.execute("INSERT INTO startup_probe(id,payload) VALUES (100,X'01')")
            # 超过 1000 页的历史日志也不能由当前提交线程同步合并。
            self.assertEqual(self.digest(self.db), before)
            with self.assertRaisesRegex(ValueError, 'rollback'):
                with catalog.connect() as c:
                    c.execute("INSERT INTO startup_probe(id,payload) VALUES (101,X'02')")
                    raise ValueError('rollback')
            with catalog.connect() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM startup_probe').fetchone()[0], 65)
                self.assertIsNone(c.execute('SELECT id FROM startup_probe WHERE id=101').fetchone())
                self.assertEqual(tuple(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()), (0,0,0))
                self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(self.wal.stat().st_size, 0)
        finally:
            catalog.close()

    def test_background_reclaims_large_wal_and_preserves_committed_rows(self):
        catalog = AssetCatalog(self.data,keep_wal_open=True)
        try:
            with catalog.connect() as c:
                c.executemany('INSERT INTO startup_probe(payload) VALUES (?)', [(bytes(65536),)]*128)
            self.assertGreater(self.wal.stat().st_size, 8*1024*1024)
            catalog.wal_maintenance.interval = .02
            catalog.wal_maintenance.start()
            catalog.wal_maintenance.start()
            until = time.monotonic()+5
            while self.wal.stat().st_size > 8*1024*1024 and time.monotonic() < until:
                time.sleep(.02)
            if os.name == 'nt':
                self.assertTrue(catalog.wal_maintenance.low_priority)
            self.assertLessEqual(self.wal.stat().st_size, 8*1024*1024)
            with catalog.connect() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM startup_probe').fetchone()[0], 192)
                self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        finally:
            catalog.close()
        self.assertFalse(catalog.wal_maintenance.thread.is_alive())

    def test_busy_reader_is_not_waited_on_and_next_pass_can_reclaim(self):
        catalog = AssetCatalog(self.data,keep_wal_open=True)
        try:
            with catalog.connect() as reader:
                reader.execute('BEGIN')
                reader.execute('SELECT count(*) FROM startup_probe').fetchone()
                with catalog.connect() as writer:
                    writer.executemany('INSERT INTO startup_probe(payload) VALUES (?)', [(bytes(65536),)]*128)
                started = time.monotonic()
                with patch('wal_maintenance.WAL_PRESSURE_BYTES',1):
                    result = catalog.wal_maintenance.checkpoint()
                self.assertLess(time.monotonic()-started, .5)
                self.assertLess(result[2], result[1])
                self.assertEqual(reader.execute('SELECT count(*) FROM startup_probe').fetchone()[0], 64)
            catalog.wal_maintenance.checkpoint()
            self.assertLessEqual(self.wal.stat().st_size, 8*1024*1024)
        finally:
            catalog.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows maintenance thread priority')
    def test_maintenance_keeps_normal_io_priority_under_write_pressure(self):
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentThread.restype = ctypes.c_void_p
        kernel.GetThreadPriority.argtypes = [ctypes.c_void_p]
        catalog = AssetCatalog(self.data, keep_wal_open=True)
        observed = []
        checkpoint = catalog.wal_maintenance.checkpoint
        def probe():
            observed.append(kernel.GetThreadPriority(kernel.GetCurrentThread()))
            return checkpoint()
        try:
            with catalog.connect() as c:
                c.executemany('INSERT INTO startup_probe(payload) VALUES (?)', [(bytes(65536),)]*128)
            with patch.object(catalog.wal_maintenance, 'checkpoint', side_effect=probe):
                catalog.wal_maintenance.interval = .02
                catalog.wal_maintenance.start()
                until = time.monotonic()+5
                while self.wal.stat().st_size > 8*1024*1024 and time.monotonic() < until:
                    time.sleep(.02)
                self.assertLessEqual(self.wal.stat().st_size, 8*1024*1024)
            self.assertTrue(observed)
            self.assertEqual(set(observed), {-1})  # BELOW_NORMAL，无后台 I/O 降级标志。
        finally:
            catalog.close()

    def test_slow_background_maintenance_does_not_hold_foreground_connection(self):
        catalog = AssetCatalog(self.data,keep_wal_open=True)
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(5)
        try:
            with patch.object(catalog.wal_maintenance, 'checkpoint', side_effect=slow):
                catalog.wal_maintenance.interval = .01
                catalog.wal_maintenance.start()
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                with catalog.connect() as c:
                    c.execute("INSERT INTO startup_probe(id,payload) VALUES (100,X'01')")
                self.assertLess(time.monotonic()-started, .5)
                self.assertFalse(release.is_set())
                release.set()
        finally:
            release.set()
            catalog.close()

    def test_pressure_reclaims_even_when_passive_snapshot_is_not_caught_up(self):
        catalog = AssetCatalog(self.data, keep_wal_open=True)
        try:
            with catalog.connect() as c:
                c.executemany('INSERT INTO startup_probe(payload) VALUES (?)',[(bytes(65536),)]*128)
            # 使用真实数据库：在 PASSIVE 返回后并发提交，复现回收判定
            # 刚落后于写入的竞态。维护须继续尝试 TRUNCATE，不能永久跳过。
            original=sqlite3.connect
            class RacingConnection(sqlite3.Connection):
                def execute(connection, sql, *args, **kwargs):
                    cursor=super().execute(sql,*args,**kwargs)
                    if sql=='PRAGMA wal_checkpoint(PASSIVE)':
                        with catalog.connect() as writer:
                            writer.executemany('INSERT INTO startup_probe(payload) VALUES (?)',[(bytes(65536),)]*256)
                        class Snapshot:
                            def fetchone(self):return (0,100,99)
                        return Snapshot()
                    return cursor
            def connect(*args,**kwargs):
                if kwargs.get('uri'):kwargs['factory']=RacingConnection
                return original(*args,**kwargs)
            with patch('wal_maintenance.WAL_PRESSURE_BYTES',1),patch('wal_maintenance.sqlite3.connect',side_effect=connect):
                self.assertEqual(catalog.wal_maintenance.checkpoint(),(0,0,0))
            with catalog.connect() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM startup_probe').fetchone()[0],448)
        finally:
            catalog.close()

    def test_preview_recovery_releases_snapshot_before_enqueue_or_pause(self):
        catalog = AssetCatalog(self.data, keep_wal_open=True)
        seen=[]
        try:
            with catalog.connect() as c:
                c.execute("INSERT INTO roots(id,path,created) VALUES('root','photos',0)")
                c.executemany('''INSERT INTO items(id,root,path,relative,folder,name,extension,
                    size,modified,fingerprint,last_seen,pair_key) VALUES(?,'root',?,?,'/',?,'.jpg',1,0,'fp',0,?)''',
                    [(str(i),f'photos/{i}.jpg',f'{i}.jpg',f'{i}.jpg',f'pair-{i//2:04}') for i in range(600)])
            def checkpoint():
                with catalog.connect() as c:
                    c.execute("INSERT INTO startup_probe(payload) VALUES(X'01')")
                result=catalog.wal_maintenance.checkpoint()
                self.assertEqual(result[0],0)
                self.assertEqual(result[1],result[2],'恢复入队/等待暂停时仍有历史读取快照')
                with catalog.connect() as c:
                    self.assertEqual(tuple(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()),(0,0,0))
            def enqueue(keys):
                self.assertLessEqual(len(keys),128)
                seen.extend(keys)
                checkpoint()
            def stop():
                if seen:checkpoint()  # 暂停等待发生在 stop 回调中，同样不能持读锁。
                return False
            with patch.object(catalog,'queue_previews',side_effect=enqueue):
                catalog.queue_all_previews(stop)
            self.assertEqual(len(seen),300)
            self.assertEqual(set(seen),{f'pair-{i:04}' for i in range(300)})
        finally:
            catalog.close()

    def test_heavy_startup_tasks_wait_for_recovery_without_blocking_reads(self):
        catalog = AssetCatalog(self.data,keep_wal_open=True)
        ran = threading.Event()
        tasks = StartupTasks([('recover',catalog.wal_maintenance.wait_for_startup_recovery),('heavy',ran.set)],fallback_seconds=0)
        try:
            with catalog.connect() as c:
                c.executemany('INSERT INTO startup_probe(payload) VALUES (?)',[(bytes(65536),)]*128)
            tasks.start()
            self.assertFalse(ran.wait(.05))
            with catalog.connect() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM startup_probe').fetchone()[0],192)
            catalog.wal_maintenance.checkpoint()
            self.assertTrue(ran.wait(1))
        finally:
            catalog.wal_maintenance.close();tasks.close();catalog.close()

    def test_pinned_wal_cannot_block_background_restart_indefinitely(self):
        catalog = AssetCatalog(self.data,keep_wal_open=True)
        ran = threading.Event()
        tasks = StartupTasks([('recover',lambda:catalog.wal_maintenance.wait_for_startup_recovery(timeout=.15)),
                              ('decode',ran.set)],fallback_seconds=0)
        try:
            with catalog.connect() as reader:
                reader.execute('BEGIN')
                reader.execute('SELECT count(*) FROM startup_probe').fetchone()
                with catalog.connect() as writer:
                    writer.executemany('INSERT INTO startup_probe(payload) VALUES (?)',[(bytes(65536),)]*128)
                self.assertLess(catalog.wal_maintenance.checkpoint()[2],self.wal.stat().st_size//4096)
                started=time.monotonic();tasks.start()
                self.assertTrue(ran.wait(1))
                self.assertGreaterEqual(time.monotonic()-started,.14)
                self.assertGreater(self.wal.stat().st_size,8*1024*1024)
                self.assertEqual(reader.execute('SELECT count(*) FROM startup_probe').fetchone()[0],64)
            catalog.wal_maintenance.checkpoint()
            self.assertLessEqual(self.wal.stat().st_size,8*1024*1024)
        finally:
            catalog.wal_maintenance.close();tasks.close();catalog.close()


if __name__ == '__main__':
    unittest.main()
