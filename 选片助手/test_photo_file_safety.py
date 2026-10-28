"""隔离复现覆盖/清理风险；所有破坏性注入只操作临时测试副本。"""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from canon_link import atomic_write
from preview_cache import PreviewCache,cache_name
from file_safety import delete_verified,publish_new,write_verified,overwrite_stream
from metadata_writer import MetadataWriter,sha


class PhotoFileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)

    def tearDown(self):self.tmp.cleanup()

    def cache_fixture(self):
        cache=PreviewCache(self.root/'cache');record={'fingerprint':'known-source'}
        target=cache.folder/cache_name(record,480);temp=cache.folder/'new.tmp'
        Image.new('RGB',(40,30),'red').save(temp,format='JPEG')
        return cache,record,temp,target

    def test_legacy_cache_index_must_not_adopt_replaced_original(self):
        cache,record,temp,target=self.cache_fixture();cache.publish(temp,target,record,480,False)
        with cache.connect() as c:c.execute('UPDATE previews SET digest=NULL')
        Image.new('RGB',(100,80),'green').save(target);original=target.read_bytes()
        PreviewCache(cache.folder,limit=0)
        self.assertTrue(target.exists(),'旧索引不能接管并删除后来放入的原图')
        self.assertEqual(target.read_bytes(),original)

    def test_cache_publish_must_not_replace_file_arriving_after_check(self):
        cache,record,temp,target=self.cache_fixture();original=b'customer original arriving concurrently'
        with patch.object(cache,'_trim',side_effect=lambda *a:target.write_bytes(original)):
            with self.assertRaises(OSError):cache.publish(temp,target,record,480,False)
        self.assertEqual(target.read_bytes(),original)

    def test_cache_delete_must_not_unlink_external_replacement_after_check(self):
        cache,record,temp,target=self.cache_fixture();cache.publish(temp,target,record,480,False)
        original=b'new customer photo';owned=cache._owned
        def replaced(*args):
            result=owned(*args);target.write_bytes(original);return result
        with patch.object(cache,'_owned',side_effect=replaced),cache.connect() as c:cache._remove(c,target.name)
        self.assertTrue(target.exists(),'归属检查和删除之间替换的文件必须保留')
        self.assertEqual(target.read_bytes(),original)

    def test_canon_config_write_preserves_unowned_same_name_temporary(self):
        path=self.root/'user.config';temp=self.root/'user.config.framekeep-tmp'
        temp.write_bytes(b'other software data');atomic_write(path,b'new config')
        self.assertEqual(path.read_bytes(),b'new config')
        self.assertTrue(temp.exists(),'固定临时路径不能覆盖后再删除')
        self.assertEqual(temp.read_bytes(),b'other software data')

    def test_old_release_cache_can_regenerate_without_deleting_legacy_files(self):
        cache,record,temp,target=self.cache_fixture()
        legacy=cache.folder/cache_name(record,480,version=3);legacy.write_bytes(temp.read_bytes())
        with cache.connect() as c:
            c.execute('INSERT INTO previews(name,bytes,accessed,fingerprint,edge,decoded,orientation) VALUES (?,?,?,?,?,?,?)',
                      (legacy.name,legacy.stat().st_size,1,record['fingerprint'],480,0,1))
        restarted=PreviewCache(cache.folder);restarted.publish(temp,target,record,480,False)
        self.assertTrue(legacy.exists());self.assertNotEqual(target,legacy)
        self.assertIsNotNone(restarted.get(target.name))

    @unittest.skipUnless(os.name=='nt','Windows 文件句柄清理')
    def test_delete_holds_same_handle_against_write_and_replacement(self):
        path=self.root/'cache.jpg';path.write_bytes(b'cache');other=self.root/'other';other.write_bytes(b'new')
        digest=hashlib.sha256(b'cache').hexdigest();original=hashlib.file_digest
        def concurrent(stream,*args):
            with self.assertRaises(OSError):path.write_bytes(b'damaged')
            with self.assertRaises(OSError):os.replace(other,path)
            return original(stream,*args)
        with patch('file_safety.hashlib.file_digest',side_effect=concurrent):
            self.assertTrue(delete_verified(path,digest))
        self.assertFalse(path.exists());self.assertEqual(other.read_bytes(),b'new')

    def test_existing_sidecar_cannot_be_replaced_when_expected_missing(self):
        source=self.root/'prepared.xmp';source.write_bytes(b'new rating')
        target=self.root/'customer.xmp';target.write_bytes(b'customer edits')
        with self.assertRaises(FileExistsError):write_verified(source.read_bytes(),target,None)
        self.assertEqual(target.read_bytes(),b'customer edits');self.assertTrue(source.exists())

    @unittest.skipUnless(os.name=='nt','Windows 原子替换')
    def test_direct_write_keeps_file_identity_and_refuses_stale_hash(self):
        target=self.root/'IMG_0001.JPG';target.write_bytes(b'original image')
        source=self.root/'prepared';source.write_bytes(b'edited image')
        before=sha(target);identity=target.stat().st_ino;write_verified(source.read_bytes(),target,before)
        self.assertEqual(target.stat().st_ino,identity)
        self.assertEqual(target.read_bytes(),b'edited image')
        self.assertFalse(list(self.root.glob('.framekeep-recovery-*')))
        source.write_bytes(b'stale edits')
        with self.assertRaisesRegex(ValueError,'外部修改'):write_verified(source.read_bytes(),target,before)
        self.assertEqual(target.read_bytes(),b'edited image')

    @unittest.skipUnless(os.name=='nt','Windows 原子替换不创建恢复副本')
    def test_metadata_commit_never_creates_original_bin(self):
        target=self.root/'IMG_0001.JPG';target.write_bytes(b'original image')
        source=self.root/'prepared';source.write_bytes(b'edited image');before=sha(target)
        write_verified(source.read_bytes(),target,before)
        self.assertEqual(target.read_bytes(),b'edited image')
        self.assertFalse(list(self.root.rglob('original.bin')))

    @unittest.skipUnless(os.name=='nt','Windows 元数据真实文件验收')
    def test_rating_keeps_small_field_records_and_releases_successful_full_copies(self):
        data=self.root/'data';data.mkdir();writer=MetadataWriter(Path(__file__).resolve().parents[1],data)
        photo=self.root/'IMG_0001.JPG';Image.new('RGB',(30,20),'green').save(photo)
        xmp=photo.with_suffix('.xmp');extra=Path(str(photo)+'.xmp')
        for target in (xmp,extra):writer.run('-XMP-xmp:Rating=2',target)
        originals={p:sha(p) for p in (photo,xmp,extra)}
        writer.write_rating(photo,4)
        for p in originals:self.assertEqual(writer.read(p)['XMP-xmp:Rating'],4)
        self.assertFalse(list(self.root.glob('.framekeep-recovery-*')))
        import json
        records=[json.loads(p.read_text('utf8')) for p in (data/'metadata-history').glob('*.json')]
        self.assertEqual(len(records),3)
        self.assertTrue(all(r['status']=='applied' for r in records))
        self.assertEqual({r['journal']['before_hash'] for r in records},set(originals.values()))
        self.assertTrue(all(r['journal']['before']=={'Rating':2} if r['journal']['mode']=='sidecar' else r['journal']['before']=={'Rating':None} for r in records))

    @unittest.skipUnless(os.name=='nt','Windows 无源文件备份提交')
    def test_metadata_commit_uses_no_recovery_directory(self):
        target=self.root/'photo';target.write_bytes(b'before');source=self.root/'temp';source.write_bytes(b'after')
        write_verified(source.read_bytes(),target,sha(target))
        self.assertEqual(target.read_bytes(),b'after')
        self.assertFalse(list(self.root.glob('.framekeep-recovery-*')))

    @unittest.skipUnless(os.name=='nt','Windows 元数据写入')
    def test_rating_creates_no_staging_in_photo_directory_during_write_or_failed_commit(self):
        data=self.root/'data';data.mkdir();photos=self.root/'photos';photos.mkdir()
        writer=MetadataWriter(Path(__file__).resolve().parents[1],data)
        photo=photos/'IMG_0001.JPG';Image.new('RGB',(30,20),'green').save(photo)
        run=writer.binary
        def check(*args,**kwargs):
            self.assertFalse([p for p in photos.iterdir() if p.is_dir()])
            self.assertFalse(list(photos.glob('.framekeep-*')))
            self.assertFalse(list(photos.rglob('original.bin')))
            self.assertFalse(list(data.rglob('*.JPG')))
            self.assertFalse((data/'metadata-staging').exists())
            return run(*args,**kwargs)
        with patch.object(writer,'binary',side_effect=check):writer.write_rating(photo,4)
        before=sha(photo)
        with patch.object(writer,'binary',side_effect=check),patch('metadata_writer.write_verified',side_effect=OSError('injected commit failure')):
            with self.assertRaisesRegex(OSError,'injected'):writer.write_rating(photo,1)
        self.assertEqual(sha(photo),before)
        self.assertFalse(list(photos.glob('.framekeep-*')))

    def test_legacy_prepared_cleanup_preserves_originals(self):
        photo=self.root/'photo.JPG';photo.write_bytes(b'original')
        legacy=self.root/'.framekeep-write-legacy';legacy.mkdir();old=legacy/'photo.JPG';old.write_bytes(b'prepared')
        MetadataWriter.discard_prepared(old);MetadataWriter.discard_prepared(old)
        self.assertFalse(legacy.exists());self.assertEqual(photo.read_bytes(),b'original')
        old=self.root/'.framekeep-write-old.JPG';old.write_bytes(b'old temporary')
        MetadataWriter.discard_prepared(old);self.assertFalse(old.exists())

    def test_failed_direct_write_restores_original_from_memory(self):
        target=self.root/'photo.JPG';target.write_bytes(b'original photo bytes')
        original=target.read_bytes();calls=[]
        def interrupted(stream,payload):
            calls.append(payload)
            if len(calls)==1:
                stream.seek(0);stream.write(b'partial overwrite');raise OSError('injected interruption')
            return overwrite_stream(stream,payload)
        with patch('file_safety.overwrite_stream',side_effect=interrupted):
            with self.assertRaisesRegex(OSError,'injected'):write_verified(b'new metadata bytes',target,sha(target))
        self.assertEqual(target.read_bytes(),original);self.assertEqual(len(calls),2)
        self.assertEqual(list(self.root.iterdir()),[target])

    def test_failed_new_sidecar_is_removed_without_leaving_partial_file(self):
        target=self.root/'photo.xmp';calls=[]
        def interrupted(stream,payload):
            calls.append(payload)
            if len(calls)==1:
                stream.write(b'partial');raise OSError('injected sidecar failure')
            return overwrite_stream(stream,payload)
        with patch('file_safety.overwrite_stream',side_effect=interrupted):
            with self.assertRaisesRegex(OSError,'injected'):write_verified(b'new sidecar',target,None)
        self.assertFalse(target.exists());self.assertEqual(list(self.root.iterdir()),[])

    def test_direct_write_blocks_concurrent_changes_on_same_handle(self):
        target=self.root/'photo.JPG';target.write_bytes(b'original')
        def concurrent(stream,payload):
            with self.assertRaises(OSError):target.write_bytes(b'external writer')
            other=self.root/'other.JPG';other.write_bytes(b'external replacement')
            with self.assertRaises(OSError):os.replace(other,target)
            return overwrite_stream(stream,payload)
        with patch('file_safety.overwrite_stream',side_effect=concurrent):write_verified(b'new metadata',target,sha(target))
        self.assertEqual(target.read_bytes(),b'new metadata')

    def test_locked_photo_fails_without_any_full_image_files(self):
        data=self.root/'data';data.mkdir();photos=self.root/'photos';photos.mkdir()
        writer=MetadataWriter(Path(__file__).resolve().parents[1],data)
        photo=photos/'IMG_0001.JPG';Image.new('RGB',(30,20),'green').save(photo);before=sha(photo)
        with writer.exclusive(photo):
            with self.assertRaises(OSError):writer.write_rating(photo,4)
        self.assertEqual(sha(photo),before)
        self.assertEqual(list(photos.glob('*.JPG')),[photo])
        self.assertFalse(list(data.rglob('*.JPG')))
        self.assertFalse((data/'metadata-staging').exists())

    def test_open_preview_reader_does_not_require_replacing_original(self):
        data=self.root/'data';data.mkdir();writer=MetadataWriter(Path(__file__).resolve().parents[1],data)
        photo=self.root/'IMG_0001.JPG';Image.new('RGB',(30,20),'green').save(photo)
        identity=photo.stat().st_ino
        with photo.open('rb'):writer.write_rating(photo,4)
        self.assertEqual(photo.stat().st_ino,identity)
        self.assertEqual(writer.read(photo)['XMP-xmp:Rating'],4)
        self.assertFalse(list(data.rglob('*.JPG')))

    def test_readonly_photo_is_preserved_without_any_full_image_files(self):
        target=self.root/'photo.JPG';target.write_bytes(b'original')
        os.chmod(target,0o444)
        try:
            with self.assertRaises(OSError):write_verified(b'changed',target,sha(target))
            self.assertEqual(target.read_bytes(),b'original')
        finally:os.chmod(target,0o666)

    @unittest.skipUnless(os.name=='nt','Windows 元数据写入')
    def test_sku_and_sidecar_write_stay_outside_photo_directory(self):
        data=self.root/'data';data.mkdir();photos=self.root/'photos';photos.mkdir()
        writer=MetadataWriter(Path(__file__).resolve().parents[1],data)
        photo=photos/'IMG_0001.JPG';Image.new('RGB',(30,20),'green').save(photo)
        with Image.open(photo) as im:pixels=im.tobytes()
        run=writer.binary
        def check(*args,**kwargs):
            self.assertFalse(list(photos.glob('.framekeep-*')))
            self.assertFalse(list(data.rglob('*.JPG')))
            self.assertFalse((data/'metadata-staging').exists())
            return run(*args,**kwargs)
        with patch.object(writer,'binary',side_effect=check):
            temp,target,journal=writer.write(photo,{'SKU':'SKU123','Title':'中文商品名称'},sha(photo))
            self.assertFalse(list(photos.glob('.framekeep-*')))
            self.assertIsInstance(temp,bytes)
            self.assertNotIn('prepared',journal)
            writer.commit(temp,target,journal)
            writer.commit_sidecars(journal)
        self.assertEqual(writer.read(photo)['XMP-fk:SKU'],'SKU123')
        self.assertEqual(writer.read(Path(str(photo)+'.xmp'))['XMP-dc:Title'],'中文商品名称')
        self.assertFalse((data/'metadata-staging').exists())
        self.assertFalse(list(photos.glob('.framekeep-*')))
        with Image.open(photo) as im:self.assertEqual(im.tobytes(),pixels)

    @unittest.skipUnless(os.name=='nt','Windows 元数据写入')
    def test_sku_text_write_failure_cleans_staging_without_source_directories(self):
        data=self.root/'data';data.mkdir();photos=self.root/'photos';photos.mkdir()
        writer=MetadataWriter(Path(__file__).resolve().parents[1],data)
        photo=photos/'IMG_0001.JPG';Image.new('RGB',(30,20),'green').save(photo);before=sha(photo)
        def fail(*args):
            self.assertFalse([p for p in photos.iterdir() if p.is_dir()])
            self.assertFalse(list(photos.glob('.framekeep-*')))
            self.assertTrue(list(data.glob('.framekeep-text-*')))
            raise OSError('injected text write failure')
        run=writer.binary
        def checked(*args,**kwargs):
            if '-o' in args:return fail(*args)
            self.assertFalse(list(data.rglob('*.JPG')))
            self.assertFalse((data/'metadata-staging').exists())
            return run(*args,**kwargs)
        with patch.object(writer,'binary',side_effect=checked):
            with self.assertRaisesRegex(OSError,'injected'):
                writer.write(photo,{'SKU':'SKU123','Title':'中文商品名称'},before)
        self.assertEqual(sha(photo),before)
        self.assertEqual(list(photos.iterdir()),[photo])
        self.assertFalse(list(data.glob('.framekeep-text-*')))


if __name__=='__main__':unittest.main()
