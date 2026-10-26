"""缓存持久化、入库预生成、失效和容量淘汰的真实文件验证。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from catalog_store import Store
import media_engine as media
import preview_cache as preview_cache_module
from preview_cache import MAX_CACHE_BYTES,PreviewCache,PreviewWarmer,cache_name

ROOT=Path(__file__).resolve().parents[1]


class PreviewCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.photos=self.root/'照片';self.photos.mkdir();self.folder=self.root/'cache'
        self.cache=PreviewCache(self.folder)
        self.patch=patch.object(media,'cache_for',return_value=self.cache);self.patch.start()

    def tearDown(self):self.patch.stop();self.temp.cleanup()

    def record(self,name='001.JPG',color='red'):
        path=self.photos/name;Image.new('RGB',(1200,800),color).save(path)
        media._STATS.pop(str(path),None)
        return media.capture_record(self.photos,path,None,{})

    def warm(self,record):
        worker=PreviewWarmer(self.cache,Path('unused'),autostart=False)
        worker.enqueue(self.photos,[record]);self.assertTrue(worker.process_one());self.assertFalse(worker.process_one());return worker

    def test_restart_reuses_both_sizes_without_opening_source_or_exiftool(self):
        record=self.record();self.warm(record)
        self.assertEqual(self.cache.state()['images'],2)
        self.assertEqual(self.cache.state()['limit_bytes'],MAX_CACHE_BYTES)
        restarted=PreviewCache(self.folder)
        (self.photos/'001.JPG').unlink()  # 缓存命中在源目录离线时也可浏览。
        with patch.object(media,'cache_for',return_value=restarted),patch.object(media,'_companion_preview',side_effect=AssertionError('重复解码')):
            for edge in (480,2048):
                payload=media.preview_bytes(record,self.photos,self.folder,lambda:self.fail('缓存不应查找ExifTool'),edge)
                with Image.open(io.BytesIO(payload)) as image:self.assertLessEqual(max(image.size),edge)

    def test_queue_is_durable_and_resumes_after_restart(self):
        record=self.record();PreviewWarmer(self.cache,'unused',autostart=False).enqueue(self.photos,[record])
        restarted=PreviewCache(self.folder);worker=PreviewWarmer(restarted,'unused',autostart=False)
        self.assertEqual(restarted.state()['pending'],1);self.assertTrue(worker.process_one());self.assertFalse(worker.process_one())
        self.assertEqual(restarted.state()['pending'],0);self.assertEqual(restarted.state()['images'],2)

    def test_cache_reads_batch_access_writes_and_trim_uses_latest_access(self):
        record=self.record();self.warm(record);name=cache_name(record,480)
        with self.cache.connect() as c:c.execute('UPDATE previews SET accessed=1')
        self.cache._last_touch_flush=time.monotonic()
        for _ in range(5):self.assertIsNotNone(self.cache.get(name,read=True))
        with self.cache.connect() as c:self.assertEqual(c.execute('SELECT accessed FROM previews WHERE name=?',(name,)).fetchone()[0],1)
        with self.cache.lock,self.cache.connect() as c:
            self.cache._trim(c)
            self.assertGreater(c.execute('SELECT accessed FROM previews WHERE name=?',(name,)).fetchone()[0],1)
        self.assertFalse(self.cache._touches)

    def test_parallel_payload_reads_release_index_lock_but_hold_eviction_lease(self):
        first=self.record('001.JPG','red');second=self.record('002.JPG','blue')
        self.warm(first);self.warm(second)
        names=[cache_name(record,480) for record in (first,second)]
        entered=threading.Event();release=threading.Event();count=0;guard=threading.Lock()
        original=preview_cache_module.validate_stream
        def validate(stream,size):
            nonlocal count
            with guard:
                count+=1
                if count==2:entered.set()
            release.wait(3);return original(stream,size)
        with patch.object(preview_cache_module,'validate_stream',side_effect=validate),ThreadPoolExecutor(max_workers=2) as pool:
            first_read=pool.submit(self.cache.get,names[0],read=True)
            second_read=pool.submit(self.cache.get,names[1],read=True)
            self.assertTrue(entered.wait(2),'第二个缓存读取仍被全局锁串行阻塞')
            with self.cache.lock,self.cache.connect() as c:
                self.assertFalse(self.cache._remove(c,names[0]))
            release.set();self.assertIsNotNone(first_read.result(timeout=3));self.assertIsNotNone(second_read.result(timeout=3))

    def test_lru_evicts_only_published_cache_and_preserves_unknown_files(self):
        payload=io.BytesIO();Image.new('RGB',(60,40),'blue').save(payload,format='JPEG');data=payload.getvalue()
        records=[{'fingerprint':str(i)} for i in range(3)];keys=[cache_name(r,480) for r in records]
        for r,name in zip(records[:2],keys[:2]):
            temp=(self.folder/name).with_suffix('.tmp');temp.write_bytes(data)
            self.cache.publish(temp,self.folder/name,r,480,False)
        unrelated=self.folder/'user-note.jpg';unrelated.write_bytes(b'keep')
        hashed=self.folder/('f'*64+'.jpg');hashed.write_bytes(data)
        abandoned=hashed.with_suffix('.tmp');abandoned.write_bytes(b'keep temporary')
        __import__('os').utime(abandoned,(0,0))
        bounded=PreviewCache(self.folder,limit=len(data)*2)
        with bounded.connect() as c:c.execute('UPDATE previews SET accessed=1')
        bounded.get(keys[0])
        temp=(self.folder/keys[2]).with_suffix('.tmp');temp.write_bytes(data)
        bounded.publish(temp,self.folder/keys[2],records[2],480,False)
        self.assertIsNotNone(bounded.get(keys[0]));self.assertIsNone(bounded.get(keys[1]))
        self.assertTrue(unrelated.exists());self.assertEqual(hashed.read_bytes(),data);self.assertEqual(abandoned.read_bytes(),b'keep temporary');self.assertLessEqual(bounded.state()['bytes'],bounded.limit)
        with bounded.connect() as c:
            self.assertEqual(c.execute('SELECT bytes FROM usage').fetchone()[0],bounded.state()['bytes'])

    def test_startup_enforces_limit_and_removes_missing_entries(self):
        self.warm(self.record());state=self.cache.state()
        smallest=min(self.folder.glob('*.jpg'),key=lambda p:p.stat().st_size)
        bounded=PreviewCache(self.folder,limit=smallest.stat().st_size)
        self.assertLessEqual(bounded.state()['bytes'],bounded.limit)
        for path in self.folder.glob('*.webp'):path.unlink()
        self.assertEqual(PreviewCache(self.folder).state()['bytes'],0)
        self.assertGreater(state['bytes'],0)

    def test_corrupt_and_deleted_cache_regenerates(self):
        record=self.record();path=media.preview(record,self.photos,self.folder,'unused')
        path.write_bytes(b'broken')
        # 无法区分外部替换与损坏；不得擅自删除或覆盖，显式移走后才再生成。
        with self.assertRaisesRegex(OSError,'已保留原文件'):media.preview_bytes(record,self.photos,self.folder,'unused')
        self.assertEqual(path.read_bytes(),b'broken');path.unlink()
        payload=media.preview_bytes(record,self.photos,self.folder,'unused')
        with Image.open(io.BytesIO(payload)) as image:self.assertEqual(image.size,(480,320))
        path.unlink();self.assertTrue(media.preview(record,self.photos,self.folder,'unused').is_file())

    def test_eviction_preserves_external_replacement_and_same_name_source(self):
        record=self.record();path=media.preview(record,self.photos,self.folder,'unused')
        Image.new('RGB',(100,80),'yellow').save(path);payload=path.read_bytes()
        PreviewCache(self.folder,limit=0)
        self.assertEqual(path.read_bytes(),payload)
        with self.assertRaisesRegex(OSError,'已保留原文件'):media.preview(record,self.photos,self.folder,'unused')
        self.assertEqual(path.read_bytes(),payload)

    def test_old_index_migration_preserves_files_without_claiming_ownership(self):
        record=self.record();path=media.preview(record,self.photos,self.folder,'unused');payload=path.read_bytes()
        unknown=self.folder/('a'*64+'.jpg');unknown.write_bytes(payload)
        with self.cache.connect() as c:
            c.execute('UPDATE previews SET digest=NULL')
            c.execute('INSERT INTO previews(name,bytes,accessed) VALUES (?,?,?)',(unknown.name,len(payload),1))
            c.execute('ALTER TABLE previews DROP COLUMN digest')
        migrated=PreviewCache(self.folder)
        self.assertIsNone(migrated.get(path.name,read=True))
        self.assertEqual(path.read_bytes(),payload)
        self.assertEqual(migrated.state()['images'],0);self.assertEqual(unknown.read_bytes(),payload)

    def test_existing_hash_temp_file_is_never_overwritten_or_cleaned(self):
        record=self.record();temp=self.folder/Path(cache_name(record,480)).with_suffix('.tmp')
        temp.write_bytes(b'other software temporary original')
        media.preview(record,self.photos,self.folder,'unused')
        self.assertEqual(temp.read_bytes(),b'other software temporary original')

    def test_file_version_and_orientation_use_separate_cache_keys(self):
        record=self.record();old=media.preview(record,self.photos,self.folder,'unused')
        changed=self.record(color='green');changed['orientation']=6;self.warm(changed)
        new=media.preview(changed,self.photos,self.folder,'unused')
        self.assertNotEqual(old,new)
        with Image.open(new) as image:self.assertEqual(image.size,(320,480))

    def test_parallel_foreground_and_warmer_generate_each_size_once(self):
        record=self.record();original=self.cache.publish;calls=[];lock=threading.Lock()
        def load(*args):
            with lock:calls.append(args[3])
            time.sleep(.02);return original(*args)
        worker=PreviewWarmer(self.cache,'unused',autostart=False);worker.enqueue(self.photos,[record])
        with patch.object(self.cache,'publish',side_effect=load),ThreadPoolExecutor(max_workers=8) as pool:
            work=[pool.submit(worker.process_one)]
            work += [pool.submit(media.preview_bytes,record,self.photos,self.folder,'unused',edge) for edge in [480,2048]*4]
            for result in work:result.result(timeout=15)
        self.assertEqual(sorted(calls),[480,2048]);self.assertEqual(self.cache.state()['images'],2)

    def test_failure_is_bounded_and_does_not_cache_placeholder(self):
        record=self.record();(self.photos/'001.JPG').unlink()
        worker=PreviewWarmer(self.cache,'unused',autostart=False);worker.enqueue(self.photos,[record])
        for _ in range(3):self.assertTrue(worker.process_one())
        self.assertFalse(worker.process_one());self.assertEqual(self.cache.state()['failed'],1)
        self.assertEqual(self.cache.state()['images'],0);self.assertFalse(list(self.folder.glob('*.tmp')))

    def test_live_append_and_pair_updates_schedule_without_changing_ratings(self):
        store=Store(self.root/'data',ROOT);worker=PreviewWarmer(self.cache,'unused',autostart=False)
        store.on_previews=worker.enqueue;sid=store.ensure_live_session(self.photos)
        record=self.record();result=store.append_live(sid,[record]);self.assertEqual(len(result['added']),1)
        self.assertEqual(self.cache.state()['pending'],1);worker.process_one();worker.process_one()
        old=store.records(sid)[0];new=dict(old,fingerprint='new-version')
        store.append_live(sid,[new]);worker.process_one();worker.process_one()
        self.assertEqual(self.cache.state()['images'],4)  # 极速默认按文件版本缓存，不为去重额外完整读取原图。
        self.assertEqual(store.records(sid)[0]['liveRating'],old['liveRating'])

    def test_import_scan_schedules_all_records(self):
        self.record();store=Store(self.root/'data',ROOT);seen=[];store.on_previews=lambda root,records:seen.extend(records)
        job=store.scan(str(self.photos));deadline=time.monotonic()+15
        while store.jobs[job]['status'] in ('queued','running') and time.monotonic()<deadline:time.sleep(.05)
        self.assertEqual(store.jobs[job]['status'],'completed',store.jobs[job]);self.assertEqual({r['id'] for r in seen},{store.records(store.jobs[job]['session_id'])[0]['id']})

    def test_newer_record_is_not_removed_by_old_job_completion(self):
        old=self.record();new=dict(old,fingerprint='replacement');worker=PreviewWarmer(self.cache,'unused',autostart=False)
        worker.enqueue(self.photos,[old]);original=media._preview
        def replaced(*args,**kwargs):
            worker.enqueue(self.photos,[new]);return original(*args,**kwargs)
        with patch.object(media,'_preview',side_effect=replaced):worker.process_one()
        self.assertEqual(self.cache.state()['pending'],1);worker.process_one();worker.process_one();self.assertEqual(self.cache.state()['pending'],0)


class PreviewLockTests(unittest.TestCase):
    def test_different_previews_do_not_share_lock_stripes(self):
        # 两个 key 的前缀相同，旧的 64 条带锁会让第二个预览阻塞。
        entered=threading.Event()
        def second():
            with media._preview_lock('00000001'+'b'*56):entered.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with media._preview_lock('00000001'+'a'*56):
                future=pool.submit(second)
                self.assertTrue(entered.wait(3))
            future.result()

    def test_unused_per_preview_locks_are_released(self):
        for number in range(1000):
            key=f'unused-test-{number}'
            with media._preview_lock(key):self.assertIn(key,media._PREVIEW_LOCKS)
            self.assertNotIn(key,media._PREVIEW_LOCKS)


if __name__=='__main__':unittest.main()
