"""缓存跨盘/跨机、校验备份、在线迁移和文件归属边界。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image
from asset_catalog import AssetCatalog
from cache_storage import CacheStorage
from catalog_store import Store
import media_engine as media
import preview_cache as previews

ROOT=Path(__file__).resolve().parents[1]

class CacheStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.photos=self.base/'原盘'/'照片';self.photos.mkdir(parents=True)
        self.data=self.base/'data';self.catalog=AssetCatalog(self.data)
        self.store=Store(self.data,ROOT);self.storage=CacheStorage(self.data,self.store,self.catalog)
        self.backups=self.base/'backups';self.backups.mkdir()

    def tearDown(self):self.storage.close();self.temp.cleanup()

    def image(self,name='001.JPG',color='teal'):
        path=self.photos/name;exif=Image.Exif();exif[270]='Camera-A'
        Image.new('RGB',(1200,800),color).save(path,exif=exif);return path

    def archive(self):
        self.image();root=self.catalog.add_root(str(self.photos))['root_id'];row=self.catalog.items()['rows'][0]
        for size in (420,1800):self.catalog.preview(row['id'],size)
        return root,row

    def test_migration_delta_seeks_publish_index_instead_of_sorting_names(self):
        self.archive();cache=self.storage.budget.caches[1]
        with cache.connect() as c:
            mark=c.execute('SELECT revision FROM cache_totals WHERE id=1').fetchone()[0]
            names=[r[0] for r in c.execute('SELECT name FROM previews')]
            c.execute('UPDATE previews SET bytes=bytes WHERE name=?',(names[0],))
            plan=' '.join(r[3] for r in c.execute('EXPLAIN QUERY PLAN SELECT * FROM previews INDEXED BY preview_published WHERE published_seq>? ORDER BY published_seq LIMIT 256',(mark,)))
        self.assertIn('SEARCH previews USING INDEX preview_published',plan);self.assertNotIn('TEMP B-TREE',plan)
        self.assertEqual([r['name'] for r in self.storage._registered(cache,mark)],[names[0]])

    def test_export_import_cross_drive_renamed_photos_reuse_without_decode(self):
        _,row=self.archive();backup=Path(self.storage.export(str(self.backups))['file'])
        output=ROOT/'build/cache-storage-tests';output.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output) as folder:
            new=Path(folder);assets=new/'不同盘符和目录';assets.mkdir()
            shutil.copyfile(self.photos/'001.JPG',assets/'完全不同文件名.JPG')
            catalog=AssetCatalog(new/'new-machine');storage=CacheStorage(new/'new-machine',catalog=catalog)
            result=storage.import_(str(backup));self.assertEqual(result['imported'],2)
            self.assertEqual(storage.state()['portable_images'],2)
            (self.photos/'001.JPG').unlink();catalog.add_root(str(assets));item=catalog.items()['rows'][0]
            before=storage.state()['images']
            with patch.object(media,'_companion_preview',side_effect=AssertionError('已匹配内容不能重新解码')):
                for size in (420,1800):self.assertTrue(catalog.preview(item['id'],size,read=True).startswith(b'\xff\xd8'))
            self.assertEqual(storage.state()['images'],before)
            self.assertEqual(storage.import_(str(backup))['imported'],2)
            self.assertEqual(storage.state()['images'],2)
            if self.base.drive!=new.drive:self.assertNotEqual(self.photos.drive,assets.drive)

    def test_same_name_size_and_time_with_different_content_is_not_reused(self):
        self.archive();backup=self.storage.export(str(self.backups))['file']
        new=self.base/'new';assets=new/'photos';assets.mkdir(parents=True)
        original=self.photos/'001.JPG';path=assets/'001.JPG';payload=original.read_bytes().replace(b'Camera-A',b'Camera-B')
        self.assertEqual(len(payload),original.stat().st_size);self.assertNotEqual(payload,original.read_bytes());path.write_bytes(payload)
        import os
        os.utime(path,ns=(original.stat().st_atime_ns,original.stat().st_mtime_ns))
        catalog=AssetCatalog(new/'data');storage=CacheStorage(new/'data',catalog=catalog);storage.import_(backup);catalog.add_root(str(assets))
        with patch.object(media,'_read_preview',wraps=media._read_preview) as decoder:
            catalog.preview(catalog.items()['rows'][0]['id']);self.assertEqual(decoder.call_count,1)

    def test_migrate_preserves_sources_unknown_files_and_restarts_at_new_location(self):
        _,row=self.archive();self.storage.configure(32)
        old=self.storage.root;unknown=old/'catalog_previews/user.jpg';unknown.write_bytes(b'keep')
        destination=self.base/'高速SSD'/'末帧缓存';result=self.storage.migrate(str(destination))
        self.assertTrue(result['source_retained']);self.assertEqual(self.storage.state()['path'],str(destination))
        self.assertEqual(unknown.read_bytes(),b'keep');self.assertFalse((destination/'catalog_previews/user.jpg').exists())
        self.assertTrue(list((old/'catalog_previews').glob('*.jpg')))
        (self.photos/'001.JPG').unlink()
        restarted=CacheStorage(self.data,catalog=self.catalog)
        self.assertEqual(restarted.state()['limit_gb'],32);self.assertEqual(restarted.root,destination)
        with patch.object(media,'_companion_preview',side_effect=AssertionError('迁移后不解码')):
            self.assertTrue(self.catalog.preview(row['id'],1800,read=True).startswith(b'\xff\xd8'))

    def test_online_migration_waits_for_inflight_preview_and_copies_final_commit(self):
        path=self.image();record=media.capture_record(self.photos,path,None,{})
        ready=threading.Event();release=threading.Event();original=media._jpeg_source
        def block(*args):
            ready.set();self.assertTrue(release.wait(5));return original(*args)
        with patch.object(media,'_jpeg_source',side_effect=block),ThreadPoolExecutor(max_workers=2) as pool:
            render=pool.submit(media.preview,record,self.photos,self.data/'previews',None)
            self.assertTrue(ready.wait(3));move=pool.submit(self.storage.migrate,str(self.base/'ssd'))
            until=time.monotonic()+3
            while not self.storage.budget.switching and time.monotonic()<until:time.sleep(.01)
            self.assertFalse(move.done());release.set();render.result(timeout=5);move.result(timeout=5)
        with patch.object(media,'_companion_preview',side_effect=AssertionError('快照不能漏掉在途生成')):
            self.assertTrue(media.preview_bytes(record,self.photos,self.data/'previews',None).startswith(b'\xff\xd8'))

    def test_online_migration_during_single_read_batch_does_not_deadlock(self):
        path=self.image();record=media.capture_record(self.photos,path,None,{})
        ready=threading.Event();release=threading.Event();original=media._jpeg_source
        def block(*args):
            ready.set();self.assertTrue(release.wait(5));return original(*args)
        with patch.object(media,'_jpeg_source',side_effect=block),ThreadPoolExecutor(max_workers=2) as pool:
            render=pool.submit(media.warm_previews,record,self.photos,self.data/'previews',None)
            self.assertTrue(ready.wait(3));move=pool.submit(self.storage.migrate,str(self.base/'batch-ssd'))
            until=time.monotonic()+3
            while not self.storage.budget.switching and time.monotonic()<until:time.sleep(.01)
            self.assertTrue(self.storage.budget.switching);release.set()
            render.result(timeout=5);move.result(timeout=5)
        cache=previews.cache_for(self.data/'previews')
        for edge in (420,2048):self.assertIsNotNone(cache.get(previews.cache_name(record,edge)))

    def test_copy_failure_keeps_original_location_and_configuration(self):
        _,row=self.archive();original=self.catalog.preview(row['id']).read_bytes()
        with patch('cache_storage.shutil.copyfile',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.storage.migrate(str(self.base/'ssd'))
        self.assertEqual(self.storage.root,self.data);self.assertFalse(self.storage.config.exists())
        self.assertEqual(self.catalog.preview(row['id']).read_bytes(),original)

    def test_rejects_nonempty_or_overlapping_migration_target(self):
        self.archive();target=self.base/'occupied';target.mkdir();note=target/'source.JPG';note.write_bytes(b'original')
        for path in (target,self.data,self.data/'inside',self.photos,self.photos/'cache',self.base):
            with self.assertRaises(ValueError):self.storage.migrate(str(path))
        self.assertEqual(note.read_bytes(),b'original')

    def test_offline_cache_device_uses_explicit_temporary_cache_without_losing_pointer(self):
        self.archive();destination=self.base/'ssd';self.storage.migrate(str(destination))
        destination.rename(self.base/'offline');before=self.storage.config.read_bytes()
        restarted=CacheStorage(self.data,catalog=self.catalog)
        self.assertTrue(restarted.state()['warning']);self.assertEqual(restarted.root,self.data/'image-cache-offline')
        self.assertEqual(self.storage.config.read_bytes(),before)

    def test_legacy_cache_prepare_hashes_without_decode_and_rejects_stale_photo(self):
        _,row=self.archive();cache=previews.cache_for(self.data/'catalog_previews')
        with cache.connect() as c:
            c.execute('DELETE FROM content_records');c.execute('DELETE FROM content_previews');c.execute('DELETE FROM content_files')
        with patch.object(media,'_companion_preview',side_effect=AssertionError('只补指纹不能解码')):
            self.assertEqual(self.storage.prepare()['prepared'],2)
        with cache.connect() as c:c.execute('DELETE FROM content_records');c.execute('DELETE FROM content_previews')
        path=self.photos/'001.JPG';path.write_bytes(path.read_bytes().replace(b'Camera-A',b'Camera-B'))
        result=self.storage.prepare();self.assertEqual(result['unavailable'],2);self.assertEqual(self.storage.state()['portable_images'],0)

    def test_import_rejects_unregistered_paths_and_bad_checksum_before_writing(self):
        self.archive();backup=Path(self.storage.export(str(self.backups))['file'])
        for bad_hash in (False,True):
            target=self.backups/('bad'+str(bad_hash)+'.zip')
            with zipfile.ZipFile(backup) as src,zipfile.ZipFile(target,'w') as dst:
                for name in src.namelist():dst.writestr(name,b'x'*64 if bad_hash and name=='manifest.sha256' else src.read(name))
                if not bad_hash:dst.writestr('../source.JPG',b'never extract')
            empty=CacheStorage(self.base/('empty'+str(bad_hash)))
            with self.assertRaises(ValueError):empty.import_(str(target))
            self.assertEqual(empty.state()['images'],0)

    def test_import_preserves_unknown_same_name_file(self):
        self.archive();backup=self.storage.export(str(self.backups))['file'];empty=CacheStorage(self.base/'new')
        with zipfile.ZipFile(backup) as z:
            manifest=json.loads(z.read('manifest.json'));row=json.loads(z.read(manifest['chunks'][0]['name']))['previews'][0]
        cache=previews.cache_for(empty.data/row['cache']);path=cache.folder/row['name'];path.write_bytes(b'user original')
        result=empty.import_(backup);self.assertEqual(result['conflicts'],1);self.assertEqual(path.read_bytes(),b'user original')

    def test_custom_cache_is_excluded_from_photo_import(self):
        self.archive();destination=self.base/'ssd';self.storage.migrate(str(destination))
        with self.assertRaises(ValueError):self.catalog.add_root(str(destination))
        root,shots=media.discover(self.base,exclude=(self.data,self.backups))
        self.assertEqual(len(shots),1)

    def test_import_after_archive_was_added_deduplicates_by_content(self):
        self.archive();backup=self.storage.export(str(self.backups))['file']
        assets=self.base/'new-photos';assets.mkdir();shutil.copyfile(self.photos/'001.JPG',assets/'renamed.JPG')
        catalog=AssetCatalog(self.base/'new-data');storage=CacheStorage(catalog.data,catalog=catalog)
        catalog.add_root(str(assets));row=catalog.items()['rows'][0]
        for size in (420,1800):catalog.preview(row['id'],size)
        before={p.name for p in catalog.data.rglob('*.jpg')};result=storage.import_(backup)
        self.assertEqual(result['reused'],2);self.assertEqual(before,{p.name for p in catalog.data.rglob('*.jpg')})
        self.assertEqual(storage.state()['images'],2)

    def test_clean_previous_removes_only_verified_old_cache_and_keeps_live_cache(self):
        _,row=self.archive();old=self.storage.root
        unknown=old/'catalog_previews/user-original.jpg';unknown.write_bytes(b'keep source')
        self.storage.migrate(str(self.base/'ssd'));result=self.storage.clean_previous()
        self.assertEqual(result['removed'],2);self.assertGreater(result['freed_bytes'],0)
        self.assertEqual(unknown.read_bytes(),b'keep source');self.assertTrue((old/'catalog_previews/index.sqlite3').is_file())
        self.assertTrue(self.catalog.preview(row['id'],read=True).startswith(b'\xff\xd8'))
        self.assertEqual(self.storage.clean_previous()['removed'],0)

    def test_clean_previous_preserves_external_replacement(self):
        _,row=self.archive();old_path=self.catalog.preview(row['id']);self.storage.migrate(str(self.base/'ssd'))
        old_path.write_bytes(b'replaced by another app');result=self.storage.clean_previous()
        self.assertEqual(old_path.read_bytes(),b'replaced by another app');self.assertEqual(result['retained'],1)

    def test_chunked_backup_uses_bounded_index_blocks_and_imports_all(self):
        for i in range(3):self.image(f'{i}.JPG',('red','green','blue')[i])
        self.catalog.add_root(str(self.photos))
        for row in self.catalog.items()['rows']:
            for size in (420,1800):self.catalog.preview(row['id'],size)
        with patch('cache_storage.CHUNK_IMAGES',2):backup=self.storage.export(str(self.backups))['file']
        with zipfile.ZipFile(backup) as z:
            manifest=json.loads(z.read('manifest.json'));self.assertEqual(manifest['version'],3)
            self.assertEqual(len(manifest['chunks']),3)
            for chunk in manifest['chunks']:self.assertLessEqual(len(json.loads(z.read(chunk['name']))['previews']),2)
        empty=CacheStorage(self.base/'new');self.assertEqual(empty.import_(backup)['imported'],6)

    def test_legacy_manifest_backup_remains_compatible(self):
        self.archive();backup=self.storage.export(str(self.backups))['file'];old=self.backups/'legacy.zip'
        with zipfile.ZipFile(backup) as src,zipfile.ZipFile(old,'w') as dst:
            manifest=json.loads(src.read('manifest.json'));rows=[];aliases=[]
            for chunk in manifest.pop('chunks'):
                part=json.loads(src.read(chunk['name']));rows+=part['previews'];aliases+=part['aliases']
            for name in src.namelist():
                if name.startswith(('previews/','catalog_previews/')):dst.writestr(name,src.read(name))
            manifest.update(version=1,previews=rows,aliases=aliases);payload=json.dumps(manifest).encode()
            dst.writestr('manifest.json',payload);dst.writestr('manifest.sha256',hashlib.sha256(payload).hexdigest())
        empty=CacheStorage(self.base/'new');self.assertEqual(empty.import_(str(old))['imported'],2)

    def test_genuine_jpeg_recipe4_and_v2_backup_import_and_reuse_still_work(self):
        path=self.image();record=media.capture_record(self.photos,path,None,{})
        cache=self.storage.cache;name=previews.cache_name(record,480,version=4);target=cache.folder/name
        with Image.open(path) as image:
            image.thumbnail((480,480));image.save(target,format='JPEG',quality=85)
        with cache.connect() as db:
            db.execute('INSERT INTO previews(name,bytes,accessed,fingerprint,edge,decoded,orientation,digest) VALUES (?,?,?,?,?,?,?,?)',
                       (name,target.stat().st_size,1,record['fingerprint'],480,0,record.get('orientation',1),cache._digest(target)))
        content=cache.content_key(record,self.photos);cache.bind_content(content,record,480,False,name,4)
        exported=self.storage.export(str(self.backups))['file'];legacy=self.backups/'real-v2-jpeg.zip'
        with zipfile.ZipFile(exported) as src,zipfile.ZipFile(legacy,'w') as dst:
            for entry in src.namelist():
                if entry in ('manifest.json','manifest.sha256'):continue
                dst.writestr(entry,src.read(entry))
            manifest=json.loads(src.read('manifest.json'));manifest['version']=2;payload=json.dumps(manifest).encode()
            dst.writestr('manifest.json',payload);dst.writestr('manifest.sha256',hashlib.sha256(payload).hexdigest())
        empty=CacheStorage(self.base/'legacy-import')
        self.assertEqual(empty.import_(str(legacy))['imported'],1)
        with patch.object(media,'_companion_preview',side_effect=AssertionError('历史内容命中不能重新解码')):
            self.assertTrue(media.preview_bytes(record,self.photos,empty.data/'previews',None).startswith(b'\xff\xd8'))

    def test_foreground_and_automatic_background_do_not_hash_originals(self):
        path=self.image();record=media.capture_record(self.photos,path,None,{})
        cache=previews.cache_for(self.data/'previews')
        with patch('preview_cache.hashlib.file_digest',wraps=hashlib.file_digest) as digest:
            media.preview(record,self.photos,cache.folder,None);self.assertEqual(digest.call_count,0)
            worker=previews.PreviewWarmer(cache,None,autostart=False);worker.enqueue(self.photos,[record])
            worker.process_one();self.assertEqual(digest.call_count,0)
        self.assertEqual(self.storage.state()['portable_images'],0)

    def test_concurrent_sizes_share_one_full_content_hash(self):
        path=self.image();record=media.capture_record(self.photos,path,None,{})
        with patch('preview_cache.hashlib.file_digest',wraps=hashlib.file_digest) as digest,ThreadPoolExecutor(max_workers=8) as pool:
            keys=list(pool.map(lambda _:self.storage.cache.content_key(dict(record),self.photos),range(8)))
            self.assertEqual(len(set(keys)),1);self.assertEqual(digest.call_count,1)

    def test_same_probe_candidate_still_requires_full_sha256_match(self):
        cache=self.storage.cache
        def probe(record,root):record['_portable_probe']='a'*64;return 'a'*64
        first=self.image();record=media.capture_record(self.photos,first,None,{})
        with patch.object(previews.PreviewCache,'probe_key',side_effect=probe):
            worker=previews.PreviewWarmer(cache,None,autostart=False);worker.enqueue(self.photos,[record]);worker.process_one()
            second=self.image('different.JPG','red');different=media.capture_record(self.photos,second,None,{})
            with patch.object(media,'_read_preview',wraps=media._read_preview) as decoder:
                media.preview(different,self.photos,cache.folder,None);self.assertEqual(decoder.call_count,1)

    def test_large_enqueue_commits_each_bounded_batch(self):
        worker=previews.PreviewWarmer(self.storage.cache,None,autostart=False);observed=[]
        def records():
            for i in range(300):
                if i in (128,256):observed.append(self.storage.cache.state()['pending'])
                yield {'id':str(i),'fingerprint':str(i)}
        worker.enqueue(self.photos,records());self.assertEqual(observed,[128,256])
        self.assertEqual(self.storage.state()['pending'],300)

    def test_restart_reconciles_bounded_records_and_totals_stay_exact(self):
        cache=self.storage.cache
        with cache.connect() as c:
            c.executemany('INSERT INTO previews(name,bytes,accessed,fingerprint,edge,decoded,orientation,digest) VALUES (?,?,?,?,?,?,?,?)',
                ((previews.cache_name({'fingerprint':str(i)},480),100,i,str(i),480,0,1,'a'*64) for i in range(1000)))
        self.assertEqual(cache.state()['images'],1000)
        restarted=previews.PreviewCache(cache.folder)
        self.assertEqual(restarted.state()['images'],744)
        with restarted.connect() as c:self.assertEqual(restarted.state()['bytes'],c.execute('SELECT SUM(bytes) FROM previews').fetchone()[0])

    def test_counter_transitions_and_alias_upsert_do_not_drift(self):
        _,row=self.archive();self.storage.prepare();cache=previews.cache_for(self.catalog.previews)
        worker=previews.PreviewWarmer(self.storage.cache,None,autostart=False)
        worker.enqueue(self.photos,[{'id':'one','fingerprint':'pending'}])
        with self.storage.cache.connect() as c:c.execute('UPDATE pending SET attempts=3')
        self.assertEqual(self.storage.state()['pending'],0);self.assertEqual(self.storage.state()['failed'],1)
        with cache.connect() as c:alias=dict(c.execute('SELECT * FROM content_previews LIMIT 1').fetchone())
        for _ in range(3):cache.bind_content(alias['content'],{'orientation':alias['orientation']},alias['edge'],alias['decoded'],alias['name'])
        self.assertEqual(self.storage.state()['portable_images'],2)
        with cache.lock,cache.connect() as c:cache._remove(c,alias['name'])
        self.assertEqual(self.storage.state()['portable_images'],1);self.assertEqual(self.storage.state()['images'],1)

    def test_shrinking_large_cache_runs_bounded_background_batches(self):
        out=self.image().read_bytes();cache=self.storage.cache
        for i in range(33):
            record={'fingerprint':str(i)};target=cache.folder/previews.cache_name(record,480)
            temp=cache.folder/(str(i)+'.tmp');temp.write_bytes(out);cache.publish(temp,target,record,480,False)
        # 元数据容量负载，不分配真实的 2 GB 图片；每次删除仍校验实际 JPEG 内容。
        with cache.connect() as c:c.execute('UPDATE previews SET bytes=64*1024*1024')
        self.storage.configure(1);self.storage.thread.join(timeout=8)
        self.assertEqual(self.storage.job['status'],'completed',self.storage.job)
        self.assertEqual(self.storage.state()['limit_gb'],1);self.assertEqual(self.storage.state()['images'],16)

if __name__=='__main__':unittest.main()
