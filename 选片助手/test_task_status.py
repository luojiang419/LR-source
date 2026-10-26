"""任务灯摘要覆盖持久队列、暂停、数据库占用及归档锁隔离。"""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from asset_analysis import AssetAnalysis
from asset_archive import AssetArchive
from asset_catalog import AssetCatalog
from catalog_store import Store
from model_providers import Providers
from preview_cache import PreviewCache
from raw_reconcile import RawReconcile
from sku_service import SkuService
from vision_scheduler import VisionScheduler
import task_status

ROOT=Path(__file__).resolve().parents[1]


class TaskStatusFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.store=Store(self.base/'data',ROOT)
        self.archive=AssetArchive(self.store,autostart=False)
        self.providers=Providers(self.store.data);self.scheduler=VisionScheduler()
        self.sku=SkuService(self.store,self.providers,archive=self.archive,scheduler=self.scheduler,autostart=False)
        self.analysis=AssetAnalysis(self.archive,self.providers,self.scheduler,autostart=False)
        self.catalog=AssetCatalog(self.store.data);self.rawlink=RawReconcile(self.archive,autostart=False)
        self.previews=PreviewCache(self.store.data/'previews')
        self.components=SimpleNamespace(state={'status':'idle'})
        self.updates=SimpleNamespace(value={'status':'idle'})

    def tearDown(self):
        self.analysis.close();self.rawlink.close();self.sku.close();self.archive.close();self.temp.cleanup()

    def snapshot(self):
        return task_status.snapshot(self.store,self.sku,self.archive,self.catalog,self.analysis,self.rawlink,self.components,self.updates)['modules']


class TaskStatusTests(TaskStatusFixture):
    def test_idle_and_completed_tasks_do_not_look_busy(self):
        with self.sku.connect() as c:c.execute("INSERT INTO jobs(id,status) VALUES ('old','failed')")
        with self.archive.connect() as c:c.execute("INSERT INTO metadata_jobs(id,status) VALUES ('old','completed')")
        self.assertTrue(all(value['state']=='idle' for value in self.snapshot().values()))

    def test_real_queues_and_component_work_are_mapped_to_their_modules(self):
        self.store.jobs['index']={'status':'running'}
        with self.sku.connect() as c:
            c.execute("INSERT INTO jobs(id,status,trigger) VALUES ('ocr','queued','manual')")
            c.execute("INSERT INTO archive_events(id,delivered) VALUES ('event',0)")
        with self.archive.connect() as c:c.execute("INSERT INTO metadata_jobs(id,status) VALUES ('write','running')")
        with self.analysis.connect() as c:c.execute("INSERT INTO jobs(id,status,trigger) VALUES ('parse','queued','manual')")
        self.components.state['status']='working'
        values=self.snapshot()
        self.assertTrue(all(value['state']=='busy' for value in values.values()),values)
        self.assertIn('SKU 入库',values['database']['message'])
        self.assertEqual(values['archive']['active'],1)

    def test_paused_and_waiting_queues_do_not_breathe_but_running_jobs_do(self):
        self.sku.tasks_paused=True;self.analysis.paused=True
        with self.sku.connect() as c:c.execute("INSERT INTO jobs(id,status,trigger) VALUES ('ocr','queued','manual')")
        with self.analysis.connect() as c:c.execute("INSERT INTO jobs(id,status,trigger) VALUES ('parse','queued','manual')")
        self.assertEqual(self.snapshot()['progress']['state'],'paused')
        self.assertEqual(self.snapshot()['analysis']['state'],'paused')
        with self.sku.connect() as c:c.execute("UPDATE jobs SET status='waiting_model'")
        self.assertEqual(self.snapshot()['progress']['state'],'waiting')
        with self.sku.connect() as c:c.execute("UPDATE jobs SET status='running'")
        self.assertEqual(self.snapshot()['progress']['state'],'busy')

    def test_disabled_automatic_queues_wait_instead_of_pretending_to_work(self):
        self.sku.task_mode='manual';self.sku.full_auto['enabled']=False
        with self.sku.connect() as c:c.execute("INSERT INTO jobs(id,status,trigger) VALUES ('ocr','queued','full_auto')")
        self.analysis.auto['enabled']=False
        with self.analysis.connect() as c:c.execute("INSERT INTO jobs(id,status,trigger) VALUES ('parse','queued','auto')")
        self.assertEqual(self.snapshot()['progress']['state'],'waiting')
        self.assertEqual(self.snapshot()['analysis']['state'],'waiting')

    def test_database_contention_is_unknown_and_does_not_block_other_modules(self):
        with closing(sqlite3.connect(self.sku.db)) as lock:
            lock.execute('BEGIN EXCLUSIVE')
            started=time.monotonic();values=self.snapshot();elapsed=time.monotonic()-started
            self.assertLess(elapsed,1)
            self.assertEqual(values['progress']['state'],'unknown')
            self.assertEqual(values['selection']['state'],'idle')
            lock.rollback()
        self.assertEqual(self.snapshot()['progress']['state'],'idle')

    def test_task_status_never_waits_for_archive_business_lock(self):
        held=threading.Event();release=threading.Event()
        def work():
            with self.archive.lock:held.set();release.wait(3)
        thread=threading.Thread(target=work);thread.start()
        try:
            self.assertTrue(held.wait(1));started=time.monotonic()
            self.assertEqual(self.snapshot()['archive']['state'],'idle')
            self.assertLess(time.monotonic()-started,1)
        finally:release.set();thread.join()


if __name__=='__main__':unittest.main()
