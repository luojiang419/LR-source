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
            for name in ('STORE','MOUSE','WATCH','MODELS','SKU','ARCHIVE','ANALYSIS','RAWLINK','FILESEARCH','UPDATES','CATALOG','COMPONENTS','DRIVER','PREVIEWS','CANON'):
                patches.enter_context(patch.object(server,name,None))
            patches.enter_context(patch.object(server,'DATA',store.data))
            patches.enter_context(patch.object(server,'Store',return_value=store))
            patches.enter_context(patch.object(server,'AssetArchive',return_value=archive))
            patches.enter_context(patch.object(server,'ThreadingHTTPServer',return_value=http))
            patches.enter_context(patch.object(server,'watch_desktop_parent'))
            patches.enter_context(patch.object(server,'local_inference'))
            patches.enter_context(patch.object(server.sys,'argv',['server.py','--port','0','--no-browser']))
            for name in ('cache_for','PreviewWarmer','CanonLink','MouseRemote','FolderWatch','Providers','ComponentManager','DriverSetup','VisionScheduler','AssetCatalog','EverythingSearch','SkuService','AssetAnalysis','RawReconcile','UpdateClient','ArchivePipeline'):
                patches.enter_context(patch.object(server,name,MagicMock()))
            errors=[]
            def run():
                try:server.main()
                except BaseException as error:errors.append(error)
            main=threading.Thread(target=run,daemon=True)
            try:
                main.start()
                self.assertTrue(held.wait(3),'归档工作线程未开始')
                self.assertTrue(serving.wait(3),'引擎就绪仍在等待归档锁')
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
