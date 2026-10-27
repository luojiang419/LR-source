"""大型参考图接收、RAW 内嵌图、方向与临时文件清理。"""
import base64
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import reference_upload
from visual_features import decode_upload


def jpeg(size=(600,400)):
    output=io.BytesIO();Image.new('RGB',size,'red').save(output,'JPEG');return output.getvalue()


class ChunkGuard:
    def __init__(self,stream):self.stream=stream;self.calls=0;self.max_read=0
    def read(self,size=-1):
        if not 0<size<=reference_upload.CHUNK:raise AssertionError('不得整文件读入内存')
        self.calls+=1;self.max_read=max(size,self.max_read);return self.stream.read(size)


class ReferenceUploadTests(unittest.TestCase):
    def test_128mb_image_streams_without_size_cap_and_tempfile_is_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'large.jpg';source.write_bytes(jpeg())
            # 合法 JPEG 尾部填充，验证体积门槛，与图片像素尺寸分开。
            with source.open('r+b') as stream:stream.truncate(128*1024*1024)
            with source.open('rb') as stream:before=hashlib.file_digest(stream,'sha256').hexdigest()
            prepared=[];actual=reference_upload.prepare
            def decode(path,root):prepared.append(Path(path));return actual(path,root)
            with source.open('rb') as stream,patch.object(reference_upload,'prepare',side_effect=decode):
                guard=ChunkGuard(stream);result=reference_upload.receive(guard,source.stat().st_size,'large.jpg',Path(folder))
            self.assertEqual(result['source_bytes'],128*1024*1024);self.assertEqual(result['width'],600)
            self.assertGreaterEqual(guard.calls,128);self.assertEqual(guard.max_read,1024*1024)
            self.assertLess(len(result['image']),100000);self.assertFalse(prepared[0].exists());self.assertFalse(prepared[0].parent.exists())
            with source.open('rb') as stream:self.assertEqual(hashlib.file_digest(stream,'sha256').hexdigest(),before)

    def test_raw_is_decoded_on_host_using_embedded_preview_and_orientation(self):
        with patch('media_engine.find_exiftool',return_value='exiftool'),patch('media_engine.raw_embedded_metadata',return_value=(6,jpeg((120,80)))) as embedded,patch('media_engine._raw_preview',side_effect=AssertionError('内嵌图可用时不得解包 RAW')):
            result=reference_upload.receive(io.BytesIO(b'camera raw payload'),18,'相机.CR3',Path.cwd())
        self.assertEqual((result['width'],result['height']),(80,120))
        self.assertEqual(embedded.call_args.args[0].suffix,'.cr3');self.assertFalse(embedded.call_args.args[0].exists())

    def test_incomplete_upload_and_invalid_file_are_reported_and_cleanup_runs(self):
        created=[];actual=tempfile.TemporaryDirectory
        def temporary(*args,**kwargs):
            result=actual(*args,**kwargs);created.append(Path(result.name));return result
        with patch.object(reference_upload.tempfile,'TemporaryDirectory',side_effect=temporary):
            with self.assertRaisesRegex(ValueError,'未完成'):reference_upload.receive(io.BytesIO(b'one'),100,'file.nef',Path.cwd())
            with self.assertRaisesRegex(ValueError,'无法读取'):reference_upload.receive(io.BytesIO(b'bad'),3,'bad.jpg',Path.cwd())
        self.assertTrue(all(not path.exists() for path in created))
        with self.assertRaisesRegex(ValueError,'RAW'):reference_upload.receive(io.BytesIO(b'one'),3,'file.exe',Path.cwd())

    def test_legacy_base64_larger_than_previous_8mb_limit_still_decodes(self):
        data=jpeg()+bytes(10*1024*1024);payload=decode_upload(base64.b64encode(data).decode())
        with Image.open(io.BytesIO(payload)) as image:self.assertEqual(image.size,(600,400))

    def test_image_above_old_40megapixel_gate_is_reduced_on_host(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'high-resolution.jpg';Image.new('RGB',(9000,5000),'blue').save(path,'JPEG')
            with path.open('rb') as stream:result=reference_upload.receive(stream,path.stat().st_size,path.name,Path(folder))
            self.assertLessEqual(max(result['width'],result['height']),1600)


if __name__=='__main__':unittest.main()
