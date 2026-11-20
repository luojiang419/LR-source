"""持久元数据兼容、空闲无写锁、提交唤醒和可重建缓存的性能边界。"""
import json
import gzip
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import importlib.util
import unittest
from unittest.mock import patch, MagicMock

from asset_catalog import AssetCatalog
from asset_archive import AssetArchive
from catalog_store import Store
from catalog_metadata import CatalogMetadata
from catalog_metadata_codec import pack, unpack
from workbench_assets import WorkbenchAssets,accepts_gzip
from workbench_views import WorkbenchViews
import worker_activity as activity


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)

    def test_compression_preserves_every_tag_and_json_capture_fields(self):
        value={'capture':{'camera':'测试相机','shutter':1/125,'iso':400,'aperture':2.8},
               'description':{'title':'蓝色丝绸','keywords':['100%_ "样片"']},'media':{},'stamp':['v1','xmp'],
               'tags':{'MakerNotes:Unknown':'abcdef'*4000,'XMP-fk:SKU':'SKU-244','XMP-dc:Subject':['蓝色丝绸']}}
        raw=pack(value)
        self.assertLess(len(raw.encode()),len(json.dumps(value,ensure_ascii=False).encode())/4)
        self.assertEqual(unpack(raw),value)
        self.assertEqual(json.loads(raw)['capture'],value['capture'])
        text=CatalogMetadata._search_text(value)
        for word in ('蓝色丝绸','SKU-244','ISO 400','f/2.8','1/125','100%_ "样片"'):
            self.assertIn(word,text)
        self.assertNotIn('abcdef',text)
        self.assertEqual(unpack(json.dumps(value)),value)

    def test_incremental_migration_resumes_and_keeps_fts_details_generation(self):
        catalog=AssetCatalog(self.base/'data');self.addCleanup(catalog.close)
        value={'capture':{'camera':'TEST CAMERA'},'description':{'title':'蓝色丝绸'},'tags':{'MakerNotes:Raw':'abc'*2000},'media':{}}
        with catalog.connect() as c:
            c.execute('DELETE FROM catalog_metadata_maintenance')
            for index in range(5):
                c.execute("INSERT INTO catalog_metadata(item,fingerprint,status,payload,search_text,generation) VALUES (?,?,'ready',?,?,7)",
                          (str(index),'v1',json.dumps(value,ensure_ascii=False),'old text'))
        self.assertTrue(catalog.metadata.compact_batch(2))
        with catalog.connect() as c:
            self.assertEqual(c.execute("SELECT cursor FROM catalog_metadata_maintenance WHERE key='compact-v2'").fetchone()[0],2)
        # 构造新消费者模拟进程恢复，不能重做已迁移行。
        metadata=CatalogMetadata(catalog)
        metadata.maintenance_blocked=lambda:True
        self.assertFalse(metadata.compact_batch(2))
        metadata.maintenance_blocked=None
        while metadata.compact_batch(2):pass
        for index in range(5):self.assertEqual(metadata.cached(str(index))['data'],value)
        with catalog.connect() as c:
            self.assertEqual(c.execute('SELECT min(generation),max(generation) FROM catalog_metadata').fetchone()[:],(7,7))
            if metadata.fts:self.assertEqual(c.execute("SELECT count(*) FROM catalog_metadata_fts WHERE catalog_metadata_fts MATCH '"+'"蓝色丝绸"'+"'").fetchone()[0],5)
            self.assertEqual(c.execute("SELECT rewritten,done FROM catalog_metadata_maintenance WHERE key='compact-v2'").fetchone()[:],(5,1))

    def test_empty_archive_does_not_wait_for_another_writer(self):
        store=Store(self.base/'data',Path(__file__).resolve().parents[1])
        archive=AssetArchive(store,autostart=False);self.addCleanup(archive.close)
        with archive.connect() as writer:
            writer.execute('BEGIN IMMEDIATE')
            started=time.monotonic()
            self.assertFalse(archive.process_one())
            self.assertLess(time.monotonic()-started,.3)

    def test_reference_constructor_defers_pair_counts_and_background_finishes(self):
        from reference_library import ReferenceLibrary
        from PIL import Image
        photos=self.base/'photos';photos.mkdir();Image.new('RGB',(20,10)).save(photos/'one.jpg')
        catalog=AssetCatalog(self.base/'data');self.addCleanup(catalog.close);catalog.add_root(str(photos))
        queries=[];original=catalog.connect
        @contextmanager
        def traced():
            with original() as c:
                c.set_trace_callback(queries.append);yield c
        with patch.object(catalog,'connect',traced):library=ReferenceLibrary(self.base/'data',catalog=catalog,autostart=False)
        self.addCleanup(library.close)
        self.assertFalse(any('FROM capture_items' in sql for sql in queries),queries)
        self.assertTrue(library.config['roots'][0]['count_pending'])
        library.start();deadline=time.monotonic()+3
        while library.config['roots'][0]['count_pending'] and time.monotonic()<deadline:time.sleep(.02)
        self.assertFalse(library.config['roots'][0]['count_pending'])
        self.assertEqual(library.config['roots'][0]['count'],1)

    def test_compaction_exports_verified_copy_and_keeps_source(self):
        script=Path(__file__).resolve().parents[1]/'tools/compact_catalog.py'
        spec=importlib.util.spec_from_file_location('compact_244',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        source=self.base/'catalog.sqlite3';output=self.base/'compact.sqlite3'
        with closing(sqlite3.connect(source)) as c:
            c.execute('CREATE TABLE items(id INTEGER PRIMARY KEY,payload TEXT)')
            c.executemany('INSERT INTO items(payload) VALUES(?)',[('x'*10000,)]*50);c.commit()
            c.execute('DELETE FROM items WHERE id>1');c.commit()
        before=source.read_bytes();result=module.compact(source,output)
        self.assertEqual(source.read_bytes(),before);self.assertTrue(result['verified'])
        self.assertLess(output.stat().st_size,source.stat().st_size)
        exported=output.read_bytes()
        with self.assertRaises(ValueError):module.compact(source,output)
        self.assertEqual(output.read_bytes(),exported)

    def test_commit_wakes_idle_consumer_without_lost_notification(self):
        closed=threading.Event();done=threading.Event();observed=activity.revision()
        thread=threading.Thread(target=lambda:(activity.wait(closed,observed),done.set()))
        thread.start();self.addCleanup(lambda:(closed.set(),activity.notify(),thread.join(2)))
        with closing(sqlite3.connect(self.base/'queue.db')) as c:
            c.execute('CREATE TABLE jobs(id INTEGER)')
            with activity.transaction(c):c.execute('INSERT INTO jobs VALUES(1)')
        self.assertTrue(done.wait(.5))
        observed=activity.revision();activity.notify()
        started=time.monotonic();activity.wait(closed,observed)
        self.assertLess(time.monotonic()-started,.1)

    def test_camera_auto_request_wakes_worker_without_database_mutation(self):
        from asset_analysis import AssetAnalysis
        from vision_scheduler import VisionScheduler
        store=Store(self.base/'data',Path(__file__).resolve().parents[1])
        archive=AssetArchive(store,autostart=False);self.addCleanup(archive.close)
        analysis=AssetAnalysis(archive,MagicMock(),VisionScheduler(),autostart=False)
        self.addCleanup(analysis.close)
        analysis.auto={'enabled':True,'session_id':'camera'}
        idle=threading.Event();processed=threading.Event()
        def tick():
            if 'camera' in analysis.auto_pending:processed.set()
            else:idle.set()
        with patch.object(analysis,'auto_tick',side_effect=tick):
            analysis.auto_thread.start();self.assertTrue(idle.wait(1))
            analysis.request_auto('camera')
            self.assertTrue(processed.wait(.5),'相机内存入队必须立即唤醒，不能等10秒兜底')
            analysis.close()

    def test_snapshot_burst_coalesces_and_flush_keeps_latest_epoch(self):
        db=self.base/'db';db.touch();views=WorkbenchViews(self.base,db,save_delay=.2)
        self.addCleanup(views.flush)
        import workbench_views
        original=workbench_views.atomic_write
        with patch('workbench_views.atomic_write',wraps=original) as write:
            for index in range(100):views.remember('/api/sku',{'rows':[index]},'one',0)
            self.assertEqual(write.call_count,0)
            time.sleep(.3)
            self.assertEqual(write.call_count,1)
            self.assertFalse(write.call_args.kwargs['durable'])
            views.invalidate('/api/sku/choose')
            views.remember('/api/sku',{'rows':['stale']},'one',0)
            views.remember('/api/sku',{'rows':['latest']},'one',views.epoch)
            views.flush()
        self.assertEqual(WorkbenchViews(self.base,db).bootstrap('one')['values']['/api/sku']['rows'],['latest'])

    def test_bundle_digest_is_cached_and_source_change_invalidates(self):
        (self.base/'boot.js').write_text("const modules=['one.js'];",encoding='utf8')
        (self.base/'one.js').write_text('first',encoding='utf8');assets=WorkbenchAssets(self.base)
        import workbench_assets
        with patch('workbench_assets.hashlib.sha256',wraps=workbench_assets.hashlib.sha256) as digest:
            first=assets.read();self.assertEqual(assets.read(),first);self.assertEqual(digest.call_count,1)
            (self.base/'one.js').write_text('second',encoding='utf8')
            self.assertNotEqual(assets.read(),first);self.assertEqual(digest.call_count,2)
        compressed,etag=assets.read(compressed=True)
        self.assertEqual(gzip.decompress(compressed),assets.read()[0])
        self.assertTrue(etag.endswith('-gzip'))
        for header,expected in [('gzip, deflate',True),('gzip;q=0',False),('gzip;q=0.5',True),('identity',False)]:
            self.assertEqual(accepts_gzip(header),expected)


if __name__=='__main__':unittest.main()
