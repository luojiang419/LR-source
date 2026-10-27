"""嵌入 JPEG 快速默认策略、方向、单次读取及旧缓存清理的安全边界。"""
from contextlib import contextmanager,closing
import hashlib
import io
import os
import sqlite3
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import threading

from PIL import Image,ImageOps
import media_engine as media
from asset_catalog import AssetCatalog
from preview_cache import PreviewCache,PreviewWarmer,cache_for,cache_name
from preview_codec import embedded_jpeg


def jpeg(orientation=None,size=(300,200)):
    image=Image.new('RGB',size,'red');image.paste('blue',(0,0,size[0]//2,size[1]//2))
    exif=Image.Exif();exif[315]='PRIVATE_AUTHOR'
    if orientation is not None:exif[274]=orientation
    data=io.BytesIO();image.save(data,'JPEG',quality=90,exif=exif);return data.getvalue()


class EmbeddedPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.cache=PreviewCache(self.base/'cache')

    def test_camera_jpeg_scan_data_is_preserved_for_all_eight_orientations(self):
        for direction in range(1,9):
            original=jpeg(direction)
            with patch.object(Image.Image,'save',side_effect=AssertionError('不得重新编码像素')):
                result=embedded_jpeg(original,2048,6)
            self.assertEqual(result[result.index(b'\xff\xda'):],original[original.index(b'\xff\xda'):])
            self.assertNotIn(b'PRIVATE_AUTHOR',result)
            with Image.open(io.BytesIO(original)) as before,Image.open(io.BytesIO(result)) as after:
                self.assertEqual(ImageOps.exif_transpose(before).tobytes(),ImageOps.exif_transpose(after).tobytes())

    def test_raw_orientation_is_added_without_reencoding_and_embedded_one_wins(self):
        for embedded,expected in [(None,6),(1,1)]:
            result=embedded_jpeg(jpeg(embedded),2048,6)
            with Image.open(io.BytesIO(result)) as image:
                self.assertEqual(image.getexif().get(274,1),expected)
                self.assertEqual(ImageOps.exif_transpose(image).size,(200,300) if expected==6 else (300,200))

    def test_background_reads_jpeg_once_and_finishes_both_sizes_without_full_hash(self):
        photo=self.base/'photo.jpg';photo.write_bytes(jpeg(size=(3000,2000)))
        digest=hashlib.sha256(photo.read_bytes()).hexdigest();record=media.capture_record(self.base,photo,None,{})
        original=Path.read_bytes;reads=[]
        def read(path):
            if path==photo:reads.append(path)
            return original(path)
        with patch.object(Path,'read_bytes',read),patch.object(self.cache,'content_key',side_effect=AssertionError('预览不得完整读取源文件指纹')):
            media.warm_previews(record,self.base,self.cache.folder,None,cache_index=self.cache)
        self.assertEqual(len(reads),1)
        for edge in (420,2048):
            path=self.cache.get(cache_name(record,edge));self.assertIsNotNone(path)
            with Image.open(path) as image:self.assertEqual((image.format,max(image.size)),('JPEG',edge))
        self.assertEqual(hashlib.sha256(photo.read_bytes()).hexdigest(),digest)
        # 输入捕获记录还要写业务 JSON，内存 JPEG 数据不得泄漏进调用方记录。
        __import__('json').dumps(record)

    def test_raw_embedded_fetch_is_shared_and_no_raw_pixels_are_unpacked(self):
        raw=self.base/'camera.CR2';raw.write_bytes(b'raw container remains untouched')
        catalog=AssetCatalog(self.base/'data');self.addCleanup(catalog.close)
        record={'fingerprint':'raw','id':'raw','raw':raw.name,'_catalog':True,'_source_root':str(self.base)}
        payload=jpeg(size=(1600,1000))
        with patch.object(media,'raw_embedded_metadata',return_value=(6,payload)) as fetch,patch.object(media,'_raw_preview',side_effect=AssertionError('不得 RAW 解包')):
            catalog.render_preview_batch(record,self.base,None)
        self.assertEqual(fetch.call_count,1)
        cache=cache_for(catalog.previews)
        for edge in (420,2048):
            path=cache.get(cache_name(dict(record,orientation=6),edge));self.assertIsNotNone(path)
            with Image.open(path) as image:
                self.assertLessEqual(max(image.size),2048)
                self.assertEqual(ImageOps.exif_transpose(image).height>ImageOps.exif_transpose(image).width,True)
        self.assertEqual(raw.read_bytes(),b'raw container remains untouched')

    def test_request_above_limit_reuses_2048_cache_offline_without_another_source_read(self):
        photo=self.base/'large.jpg';photo.write_bytes(jpeg(size=(3000,2000)))
        record=media.capture_record(self.base,photo,None,{})
        path=media.preview(record,self.base,self.cache.folder,None,4096,cache_index=self.cache)
        with Image.open(path) as image:self.assertEqual(max(image.size),2048)
        photo.unlink()
        with patch.object(media,'_jpeg_source',side_effect=AssertionError('缓存命中不能重读 NAS')):
            self.assertEqual(media.preview(record,self.base,self.cache.folder,None,4096,cache_index=self.cache),path)

    @unittest.skipUnless(os.name=='nt','按句柄安全删除仅支持 Windows')
    def test_cleanup_preserves_originals_unknown_replaced_and_current_cache(self):
        original=self.base/'original.CR3';original.write_bytes(b'original')
        def register(label,version,payload):
            record={'fingerprint':label,'orientation':1};name=cache_name(record,2048,version=version)
            path=self.cache.folder/name;path.write_bytes(payload)
            with self.cache.connect() as c:
                c.execute('INSERT INTO previews(name,bytes,accessed,fingerprint,edge,decoded,orientation,digest) VALUES (?,?,?,?,?,?,?,?)',
                    (name,len(payload),1,label,2048,0,1,hashlib.sha256(payload).hexdigest()))
            return path
        old=register('old',5,jpeg());current=register('current',6,jpeg())
        replaced=register('changed',4,jpeg());replaced.write_bytes(b'user replacement')
        linked=register('linked',5,jpeg());os.link(linked,self.base/'user-hardlink.jpg')
        unknown=self.cache.folder/('a'*64+'.webp');unknown.write_bytes(b'unregistered')
        invalid=register('invalid',5,jpeg())
        with self.cache.connect() as c:c.execute("UPDATE previews SET fingerprint='not the owner' WHERE name=?",(invalid.name,))
        report=self.cache.retire_legacy()
        self.assertEqual(report['removed_files'],1);self.assertFalse(old.exists())
        self.assertTrue(current.exists());self.assertTrue(invalid.exists());self.assertEqual(unknown.read_bytes(),b'unregistered')
        self.assertEqual(replaced.read_bytes(),b'user replacement');self.assertEqual(original.read_bytes(),b'original')
        self.assertTrue(linked.exists());self.assertEqual((self.base/'user-hardlink.jpg').read_bytes(),linked.read_bytes())

    def test_old_large_queue_is_migrated_and_one_claim_makes_a_photo_ready(self):
        photos=self.base/'photos';photos.mkdir()
        for i in range(3):(photos/f'{i}.jpg').write_bytes(jpeg())
        catalog=AssetCatalog(self.base/'data');self.addCleanup(catalog.close)
        root=catalog.add_root(str(photos))['root_id']
        cache=cache_for(self.base/'data/previews')
        worker=PreviewWarmer(cache,None,catalog=catalog,autostart=False);self.addCleanup(worker.close)
        catalog.on_previews=worker.enqueue;catalog.queue_all_previews()
        with cache.connect() as c:c.execute('UPDATE pending SET edge=4096')
        worker=PreviewWarmer(cache,None,catalog=catalog,autostart=False);self.addCleanup(worker.close)
        # 启动保留旧任务及重试版本，领取后仍只生成 2048 JPEG。
        with cache.connect() as c:self.assertEqual({r[0] for r in c.execute('SELECT edge FROM pending')},{4096})
        self.assertTrue(worker.process_one());state=worker.archive_state([root])
        self.assertEqual((state['ready'],state['thumbnails_ready'],state['pending']),(1,1,2))
        with cache_for(catalog.previews).connect() as c:self.assertEqual({r[0] for r in c.execute('SELECT edge FROM previews')},{420,2048})

    def test_restoring_missing_cache_queue_does_not_block_foreground_cache_state(self):
        cache=cache_for(self.base/'data/previews');catalog_index=cache_for(self.base/'data/catalog_previews')
        worker=PreviewWarmer(cache,None,autostart=False);self.addCleanup(worker.close)
        entered=threading.Event();release=threading.Event()
        def slow_check(*args,**kwargs):
            entered.set();self.assertTrue(release.wait(5));return False
        record={'id':'cold','fingerprint':'cold','_catalog':True,'_catalog_root':'root'}
        with patch.object(catalog_index,'contains',side_effect=slow_check),ThreadPoolExecutor(max_workers=2) as pool:
            enqueue=pool.submit(worker.enqueue,self.base,[record]);self.assertTrue(entered.wait(2))
            try:
                state=pool.submit(cache.state).result(timeout=2)
                self.assertEqual(state['pending'],0)
            finally:release.set()
            enqueue.result(timeout=3)
        self.assertEqual(cache.state()['pending'],1)

    def test_archive_progress_snapshot_does_not_hold_cache_publication_lock(self):
        cache=cache_for(self.base/'data/previews');worker=PreviewWarmer(cache,None,autostart=False)
        self.addCleanup(worker.close);entered=threading.Event();release=threading.Event()
        original=cache.connect
        class Connection:
            def __init__(self,connection):self.connection=connection
            def execute(self,sql,*args):
                if sql.startswith('SELECT root_id,state,count'):
                    entered.set()
                    if not release.wait(5):raise TimeoutError('统计未释放')
                return self.connection.execute(sql,*args)
        @contextmanager
        def connect():
            with original() as connection:yield Connection(connection)
        with patch.object(cache,'connect',connect),ThreadPoolExecutor(max_workers=2) as pool:
            counting=pool.submit(worker.archive_state,['root']);self.assertTrue(entered.wait(2))
            try:self.assertEqual(pool.submit(cache.state).result(timeout=2)['images'],0)
            finally:release.set()
            self.assertEqual(counting.result(timeout=2)['total'],0)

    def test_legacy_progress_keys_migrate_once_and_statistics_use_covering_index(self):
        folder=self.base/'legacy';folder.mkdir()
        with closing(sqlite3.connect(folder/'index.sqlite3')) as c:
            c.execute('CREATE TABLE archive_jobs(identity TEXT PRIMARY KEY,root_id TEXT,record TEXT,state TEXT,small_ready INTEGER)')
            c.execute('INSERT INTO archive_jobs VALUES (?,?,?,?,?)',('one','root',json.dumps({'_catalog_pair':'pair'}),'queued',1))
            c.commit()
        cache=PreviewCache(folder)
        with cache.connect() as c:
            self.assertEqual(c.execute('SELECT pair_key FROM archive_jobs').fetchone()[0],'pair')
            self.assertEqual(c.execute("SELECT value FROM configuration WHERE key='archive_progress_v1'").fetchone()[0],'1')
            plan=c.execute('EXPLAIN QUERY PLAN SELECT root_id,state,count(*),sum(small_ready) FROM archive_jobs INDEXED BY archive_job_progress WHERE root_id=? GROUP BY root_id,state',('root',)).fetchall()
            self.assertTrue(any('COVERING INDEX archive_job_progress' in r[3] for r in plan),plan)

    def test_reconciliation_clears_retired_thumbnail_flag_without_retrying_failed_files(self):
        cache=cache_for(self.base/'data/previews');worker=PreviewWarmer(cache,None,autostart=False)
        self.addCleanup(worker.close)
        record={'id':'cold','fingerprint':'cold','_catalog':True,'_catalog_root':'root','_catalog_pair':'pair'}
        worker.enqueue(self.base,[record])
        with cache.connect() as c:
            c.execute("UPDATE archive_jobs SET small_ready=1,state='failed'")
            c.execute('UPDATE pending SET attempts=3')
        worker.enqueue(self.base,[record])
        with cache.connect() as c:
            row=c.execute('SELECT state,small_ready FROM archive_jobs').fetchone()
            self.assertEqual(tuple(row),('failed',0))
            self.assertEqual(c.execute('SELECT attempts FROM pending').fetchone()[0],3)


if __name__=='__main__':unittest.main()
