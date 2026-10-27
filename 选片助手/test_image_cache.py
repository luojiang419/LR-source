"""共享图片额度、并行预热、归档重启复用与小图优先验证。"""
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from asset_catalog import AssetCatalog
import media_engine as media
import preview_cache as previews


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.photos=self.base/'照片';self.photos.mkdir();self.data=self.base/'data'
        self.cache=previews.cache_for(self.data/'previews')

    def tearDown(self):
        self.temp.cleanup()

    def records(self,count=6):
        result=[]
        for i in range(count):
            path=self.photos/f'{i:03d}.JPG';Image.new('RGB',(900,600),(i*12,90,130)).save(path)
            result.append(media.capture_record(self.photos,path,None,{}))
        return result

    def drain(self,worker):
        for _ in range(100):
            if not worker.process_one():return
        self.fail('队列未结束')

    def test_limit_range_strict_validation_and_persistence(self):
        self.assertEqual(self.cache.state()['limit_gb'],10)
        for value in (0,257,1.5,True,None,'16'):
            with self.assertRaises(ValueError):self.cache.budget.configure(value)
        for value in (1,256):
            state=self.cache.budget.configure(value)
            self.assertEqual(state['limit_bytes'],value*1024**3)
            self.assertEqual(previews.CacheBudget(self.data).state()['limit_gb'],value)
        self.assertEqual(previews.cache_for(self.data/'catalog_previews').limit,256*1024**3)

    def test_shared_lru_counts_both_directories_and_keeps_unknown_files(self):
        out=io.BytesIO();Image.new('RGB',(50,30),'teal').save(out,format='JPEG');payload=out.getvalue()
        archive=previews.cache_for(self.data/'catalog_previews');budget=self.cache.budget
        budget.limit=len(payload)*2
        for cache in budget.caches:cache.limit=budget.limit
        records=[{'fingerprint':str(i)} for i in range(3)]
        def put(cache,record):
            path=cache.folder/previews.cache_name(record,480);temp=path.with_suffix('.tmp');temp.write_bytes(payload)
            cache.publish(temp,path,record,480,False);return path
        first=put(self.cache,records[0]);second=put(archive,records[1])
        self.cache.get(first.name)
        unknown=archive.folder/'user.jpg';unknown.write_bytes(b'keep')
        put(archive,records[2])
        self.assertTrue(first.exists());self.assertFalse(second.exists());self.assertEqual(unknown.read_bytes(),b'keep')
        self.assertEqual(self.cache.state()['images'],2);self.assertLessEqual(self.cache.state()['bytes'],budget.limit)

    def test_all_small_previews_are_processed_before_large_and_rescan_deduplicates(self):
        records=self.records();worker=previews.PreviewWarmer(self.cache,None,autostart=False)
        worker.enqueue(self.photos,records);edges=[];original=media._preview
        def render(*args,**kwargs):
            edges.append(args[4]);return original(*args,**kwargs)
        with patch.object(media,'_preview',side_effect=render):self.drain(worker)
        self.assertEqual(edges,[480,media.MAX_PREVIEW_EDGE]*6)
        worker.enqueue(self.photos,records);self.assertEqual(self.cache.state()['pending'],0)
        (self.cache.folder/previews.cache_name(records[0],480)).unlink()
        worker.enqueue(self.photos,records);self.assertEqual(self.cache.state()['pending'],1)
        self.drain(worker);self.assertEqual(self.cache.state()['images'],12)

    def test_parallel_workers_claim_distinct_records_and_generate_once(self):
        records=self.records(8);worker=previews.PreviewWarmer(self.cache,None,autostart=False)
        worker.enqueue(self.photos,records);gate=threading.Event();ready=threading.Event();lock=threading.Lock()
        active=0;peak=0;calls=[];original=media.warm_previews
        def render(*args,**kwargs):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active);calls.append((args[0]['id'],tuple(args[4])))
                if active>=3:ready.set()
            try:
                if not gate.wait(8):raise RuntimeError('工作线程未并行')
                return original(*args,**kwargs)
            finally:
                with lock:active-=1
        with patch.object(media,'warm_previews',side_effect=render):
            for thread in worker.threads:thread.start()
            try:
                self.assertTrue(ready.wait(5),'未达到三路后台生成');gate.set()
                deadline=time.monotonic()+12
                while self.cache.state()['pending'] and time.monotonic()<deadline:time.sleep(.02)
                self.assertEqual(self.cache.state()['pending'],0)
            finally:gate.set();worker.close()
        self.assertGreaterEqual(peak,3);self.assertLessEqual(peak,len(records));self.assertEqual(len(calls),8);self.assertEqual(len(set(calls)),8)
        self.assertEqual(self.cache.state()['failed'],0)

    def test_archive_enqueues_pairs_uses_legacy_cache_and_reopens_offline(self):
        self.records(3);(self.photos/'000.CR3').write_bytes(b'RAW paired with JPG')
        catalog=AssetCatalog(self.data);worker=previews.PreviewWarmer(self.cache,None,autostart=False,catalog=catalog)
        catalog.on_previews=worker.enqueue;root=catalog.add_root(str(self.photos))['root_id']
        self.assertEqual(self.cache.state()['pending'],3);self.drain(worker)
        self.assertEqual(self.cache.state()['images'],6);catalog.scan(root)
        self.assertEqual(self.cache.state()['pending'],0)
        item=catalog.items()['rows'][0];payload=catalog.preview(item['id'],read=True)
        reopened=AssetCatalog(self.data)
        for path in self.photos.iterdir():path.unlink()
        with patch.object(media,'_companion_preview',side_effect=AssertionError('缓存命中不解码')),patch.object(media,'find_exiftool',side_effect=AssertionError('缓存命中不找ExifTool')):
            self.assertEqual(reopened.preview(item['id'],read=True),payload)
        self.assertEqual(previews.CacheBudget(self.data).state()['images'],6)

    def test_archive_raw_orientation_and_both_previews_survive_offline_restart(self):
        raw=self.photos/'人像.CR3';raw.write_bytes(b'raw')
        catalog=AssetCatalog(self.data);catalog.add_root(str(self.photos));item=catalog.items()['rows'][0]
        out=io.BytesIO();Image.new('RGB',(600,400),'teal').save(out,format='JPEG')
        with patch.object(media,'find_exiftool',return_value=Path('exiftool')),patch.object(media,'read_metadata',return_value=[{'Orientation':6}]),patch.object(media.ExifToolSession,'execute',return_value=out.getvalue()):
            payloads=[catalog.preview(item['id'],size,read=True) for size in (420,1800)]
        raw.unlink();reopened=AssetCatalog(self.data)
        with patch.object(media,'find_exiftool',side_effect=AssertionError('离线命中不找ExifTool')):
            self.assertEqual([reopened.preview(item['id'],size,read=True) for size in (420,1800)],payloads)

    def test_legacy_archive_previews_are_reused_when_warming_is_enabled(self):
        self.records(1);catalog=AssetCatalog(self.data);root=catalog.add_root(str(self.photos))['root_id']
        item=catalog.items()['rows'][0]
        for size in (420,1800):catalog.preview(item['id'],size)
        worker=previews.PreviewWarmer(self.cache,None,autostart=False,catalog=catalog)
        catalog.on_previews=worker.enqueue
        with patch.object(media,'_companion_preview',side_effect=AssertionError('不应重建旧缓存')):
            catalog.scan(root);self.drain(worker)
        self.assertEqual(self.cache.state()['images'],3)

    def test_restart_resumes_large_stage_without_rebuilding_small(self):
        record=self.records(1)[0];worker=previews.PreviewWarmer(self.cache,None,autostart=False)
        worker.enqueue(self.photos,[record]);self.assertTrue(worker.process_one())
        self.assertEqual(self.cache.state()['images'],2)
        small=self.cache.folder/previews.cache_name(record,480);stamp=small.stat().st_mtime_ns
        reopened=previews.PreviewCache(self.cache.folder)
        resumed=previews.PreviewWarmer(reopened,None,autostart=False)
        self.assertFalse(resumed.process_one())
        self.assertEqual(small.stat().st_mtime_ns,stamp);self.assertEqual(reopened.state()['images'],2)

    def test_missing_paired_jpeg_still_falls_back_to_raw_thumbnail(self):
        self.records(1);(self.photos/'000.CR3').write_bytes(b'raw')
        catalog=AssetCatalog(self.data);catalog.add_root(str(self.photos));item=catalog.items()['rows'][0]
        (self.photos/'000.JPG').unlink()
        out=io.BytesIO();Image.new('RGB',(160,120),'teal').save(out,format='JPEG')
        with patch.object(media,'find_exiftool',return_value=Path('exiftool')),patch.object(media,'read_metadata',return_value=[{'Orientation':6}]),patch.object(media.ExifToolSession,'execute',return_value=out.getvalue()):
            payload=catalog.preview(item['id'],read=True)
        with Image.open(io.BytesIO(payload)) as image:self.assertEqual(image.size,(120,160))


if __name__=='__main__':unittest.main()
