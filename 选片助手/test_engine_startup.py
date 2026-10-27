"""历史归档后台持锁时，引擎仍须完成启动并响应健康检查。"""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import MagicMock,patch

from asset_archive import AssetArchive
from catalog_store import Store
import server

ROOT=Path(__file__).resolve().parents[1]


class EngineStartupTests(unittest.TestCase):
    def test_ready_handshake_does_not_query_busy_business_database(self):
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        worker=threading.Thread(target=http.serve_forever,daemon=True);worker.start()
        store=MagicMock()
        store.connect.side_effect=AssertionError('启动握手不能读取业务数据库')
        store.folder_state.side_effect=AssertionError('启动握手不能等待扫描锁')
        try:
            with patch.object(server,'STORE',store):
                with urllib.request.urlopen(f'http://127.0.0.1:{http.server_port}/api/ready',timeout=1) as response:
                    ready=json.load(response)
            self.assertTrue(ready['ready']);self.assertEqual(ready['application'],'framekeep')
            self.assertEqual(ready['process_id'],__import__('os').getpid())
            store.connect.assert_not_called();store.folder_state.assert_not_called()
        finally:http.shutdown();http.server_close();worker.join(2)

    def test_startup_progress_retains_phase_and_failure_for_this_process(self):
        from startup_status import StartupProgress
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)
            with self.assertRaisesRegex(RuntimeError,'blocked'):
                with StartupProgress(base) as progress:
                    progress.checkpoint('preview queue ready')
                    state=json.loads((base/'engine-startup.json').read_text(encoding='utf8'))
                    self.assertEqual(state['stage'],'preview queue ready')
                    self.assertEqual(state['status'],'starting')
                    raise RuntimeError('blocked')
            failed=json.loads((base/'engine-startup.json').read_text(encoding='utf8'))
            self.assertEqual(failed['status'],'failed');self.assertEqual(failed['error_type'],'RuntimeError')
            with StartupProgress(base) as progress:progress.checkpoint('http ready')
            self.assertEqual(json.loads((base/'engine-startup.json').read_text(encoding='utf8'))['status'],'ready')
            self.assertFalse(progress.worker.is_alive())

    def test_adopted_reference_roots_do_not_recount_archive_on_restart(self):
        import sqlite3
        from asset_catalog import AssetCatalog
        from reference_library import ReferenceLibrary
        with tempfile.TemporaryDirectory() as temporary:
            catalog=AssetCatalog(Path(temporary)/'data')
            try:
                with catalog.connect() as c:
                    c.execute("INSERT INTO roots(id,path,created) VALUES ('root','photos',0)")
                library=ReferenceLibrary.__new__(ReferenceLibrary)
                library.shared_catalog=catalog;library.config={'roots':[]};library.lock=threading.RLock();library.save=MagicMock()
                library._adopt_shared_catalog_roots()
                self.assertEqual(library.config['roots'][0]['id'],'root')
                library.config['roots'][0]['count']=123
                statements=[];connect=sqlite3.connect
                def traced(*args,**kwargs):
                    c=connect(*args,**kwargs);c.set_trace_callback(statements.append);return c
                with patch('asset_catalog.sqlite3.connect',side_effect=traced):library._adopt_shared_catalog_roots()
                self.assertEqual(library.config['roots'][0]['count'],123)
                self.assertFalse(any('FROM capture_items' in sql for sql in statements))
            finally:catalog.close()

    def test_health_ready_while_archive_worker_holds_lock(self):
        with tempfile.TemporaryDirectory() as temporary,ExitStack() as patches:
            base=Path(temporary);photos=base/'photos';photos.mkdir()
            store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos)
            with store.connect() as c:
                c.execute('INSERT INTO captures VALUES (?,?,?,?)',(sid,'one',json.dumps({'id':'one','jpg':'one.jpg'}),0))
                c.execute('UPDATE sessions SET state=? WHERE id=?',(json.dumps({'overrides':{'one':4}}),sid))
            archive=AssetArchive(store,autostart=False)
            held=threading.Event();release=threading.Event();serving=threading.Event()
            def slow_archive():
                with archive.lock:
                    held.set();release.wait(10)
            class SlowThread(threading.Thread):
                def start(self):
                    super().start()
                    if not held.wait(3):raise TimeoutError('归档线程未拿到锁')
            archive.thread=SlowThread(target=slow_archive,daemon=True)
            class TestHTTP(server.ThreadingHTTPServer):
                def serve_forever(self,*args,**kwargs):
                    serving.set();super().serve_forever(*args,**kwargs)
            http=TestHTTP(('127.0.0.1',0),server.Handler);http.daemon_threads=True
            for name in ('STORE','MOUSE','WATCH','MODELS','SKU','ARCHIVE','ANALYSIS','INTELLIGENCE','LANWORKSPACE','RAWLINK','FILESEARCH','UPDATES','CATALOG','COMPONENTS','DRIVER','PREVIEWS','CANON','BOOT_TASKS'):
                patches.enter_context(patch.object(server,name,None))
            patches.enter_context(patch.object(server,'DATA',store.data))
            patches.enter_context(patch.object(server,'Store',return_value=store))
            patches.enter_context(patch.object(server,'AssetArchive',return_value=archive))
            patches.enter_context(patch.object(server,'ThreadingHTTPServer',return_value=http))
            patches.enter_context(patch.object(server,'watch_desktop_parent'))
            patches.enter_context(patch.object(server,'local_inference'))
            patches.enter_context(patch.object(server.sys,'argv',['server.py','--port','0','--no-browser']))
            for name in ('cache_for','PreviewWarmer','ReferenceLibrary','CanonLink','MouseRemote','FolderWatch','Providers','ComponentManager','DriverSetup','VisionScheduler','AssetCatalog','EverythingSearch','SkuService','AssetAnalysis','AssetIntelligence','LanWorkspace','RawReconcile','UpdateClient','ArchivePipeline'):
                patches.enter_context(patch.object(server,name,MagicMock()))
            errors=[]
            def run():
                try:server.main()
                except BaseException as error:errors.append(error)
            main=threading.Thread(target=run,daemon=True)
            try:
                main.start()
                self.assertTrue(serving.wait(3),'引擎就绪仍在等待归档锁')
                self.assertFalse(held.is_set(),'首次界面加载前不能启动重归档任务')
                server.BOOT_TASKS.release()
                self.assertTrue(held.wait(3),'界面就绪后归档工作线程应恢复')
                with urllib.request.urlopen(f'http://127.0.0.1:{http.server_port}/api/health',timeout=2) as response:
                    health=json.load(response)
                self.assertEqual(health['application'],'framekeep')
                self.assertFalse(health['busy'])
                self.assertTrue(archive.thread.is_alive())
                self.assertFalse(release.is_set())
                self.assertEqual(archive.ratings.get([photos/'one.jpg']),4)
                self.assertIn(sid,archive.ratings.seed_versions)
            finally:
                release.set()
                if serving.wait(3):http.shutdown()
                main.join(4);archive.thread.join(2);http.server_close()
            self.assertFalse(main.is_alive())
            self.assertEqual(errors,[])


if __name__=='__main__':unittest.main()
