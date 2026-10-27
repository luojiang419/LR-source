"""真实 GPU 像素、八种方向、JPEG 协议、并发及故障兼容边界。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
import gpu_media as gpu
import media_engine as media
from preview_codec import jpeg_payload


def fixture():
    y,x=np.mgrid[:400,:600]
    return Image.fromarray(np.stack(((x//3)%256,(y//2)%256,(x+y)//4),axis=2).astype('uint8'))


def psnr(a,b):
    error=np.mean((np.asarray(a,dtype=float)-np.asarray(b,dtype=float))**2)
    return float(10*np.log10(255**2/max(error,1e-10)))


class GpuMediaTests(unittest.TestCase):
    def require_gpu(self):
        if gpu.backend() is None:self.skipTest('需已部署 NVIDIA 图像运行库与兼容 GPU')

    def test_all_exif_directions_and_explicit_upright_match_cpu(self):
        self.require_gpu();original=fixture();before=gpu.state()['jpeg_gpu']
        for orientation in range(1,9):
            with self.subTest(orientation=orientation):
                stream=io.BytesIO();exif=Image.Exif();exif[274]=orientation;original.save(stream,'JPEG',quality=96,exif=exif)
                payload=stream.getvalue();digest=hashlib.sha256(payload).hexdigest()
                actual=gpu.decode_jpeg(payload,200,orientation)
                with Image.open(io.BytesIO(payload)) as source:
                    expected=source.convert('RGB');expected.thumbnail((200,200),Image.Resampling.LANCZOS);expected=media._orient_preview(expected,orientation)
                self.assertEqual(actual.size,expected.size);self.assertGreater(psnr(actual,expected),36)
                self.assertEqual(hashlib.sha256(payload).hexdigest(),digest)
        self.assertEqual(gpu.state()['jpeg_gpu']-before,8)

    def test_fast_preview_respects_exif_without_waiting_for_gpu(self):
        original=fixture()
        for orientation in range(1,9):
            stream=io.BytesIO();exif=Image.Exif();exif[274]=orientation;original.save(stream,'JPEG',quality=96,exif=exif)
            with patch.object(gpu,'decode_jpeg',side_effect=AssertionError('快速预览不能等待GPU')):
                actual=media._read_preview(io.BytesIO(stream.getvalue()),200,6)
            with Image.open(io.BytesIO(stream.getvalue())) as image:expected=media._orient_preview(image.convert('RGB'),orientation)
            expected.thumbnail((200,200),Image.Resampling.LANCZOS)
            self.assertEqual(actual.size,expected.size);self.assertGreater(psnr(actual,expected),36)

    def test_cpu_decoded_pixels_resize_on_gpu_and_parallel_contexts_are_safe(self):
        self.require_gpu();original=fixture();expected=original.copy();expected.thumbnail((160,160),Image.Resampling.LANCZOS)
        before=gpu.state()['resize_gpu']
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(lambda _:gpu.resize_preview(original,160),range(12)))
        self.assertEqual(gpu.state()['resize_gpu']-before,12)
        for actual in results:self.assertEqual(actual.size,expected.size);self.assertGreater(psnr(actual,expected),42)
        self.assertEqual(original.size,(600,400))

    def test_gpu_jpeg_encode_preserves_protocol_dimensions_and_strips_private_metadata(self):
        self.require_gpu();image=fixture();stream=io.BytesIO();exif=Image.Exif();exif[315]='PRIVATE_AUTHOR';image.save(stream,'WEBP',quality=90,exif=exif)
        before=gpu.state()['jpeg_encode_gpu'];payload=jpeg_payload(stream.getvalue(),quality=95)
        with Image.open(io.BytesIO(payload)) as actual:
            self.assertEqual(actual.format,'JPEG');self.assertEqual(actual.size,image.size);self.assertFalse(actual.getexif());self.assertGreater(psnr(image,actual),34)
        self.assertEqual(gpu.state()['jpeg_encode_gpu']-before,1)
        self.assertNotIn(b'PRIVATE_AUTHOR',payload)

    def test_driver_or_memory_failure_and_explicit_cpu_mode_remain_readable(self):
        image=fixture();stream=io.BytesIO();image.save(stream,'JPEG',quality=95);payload=stream.getvalue()
        with patch.object(gpu,'backend',return_value=None):expected=media._read_preview(io.BytesIO(payload),160,'6');expected.thumbnail((160,160),Image.Resampling.LANCZOS)
        failed=unittest.mock.Mock();failed.jpeg_image.side_effect=gpu.GPUError('显存不足');failed.image.side_effect=gpu.GPUError('设备已离线')
        with patch.object(gpu,'backend',return_value=failed):
            self.assertIsNone(gpu.decode_jpeg(payload,160,'6'))
            actual=media._read_preview(io.BytesIO(payload),160,'6');actual=gpu.resize_preview(actual,160)
        self.assertEqual(actual.size,expected.size);self.assertGreater(psnr(actual,expected),80)
        with patch.dict(os.environ,{'FRAMEKEEP_GPU':'off'}):
            self.assertIsNone(gpu.backend());self.assertIsNone(gpu.decode_jpeg(payload,160));self.assertFalse(gpu.state()['ready'])

    def test_grayscale_progressive_odd_dimensions_and_truncated_jpeg(self):
        self.require_gpu()
        for mode,progressive in [('RGB',False),('RGB',True),('L',False)]:
            stream=io.BytesIO();fixture().resize((315,167)).convert(mode).save(stream,'JPEG',quality=95,progressive=progressive)
            payload=stream.getvalue();actual=media._read_preview(io.BytesIO(payload),160);actual=gpu.resize_preview(actual,160)
            self.assertEqual(actual.size,(160,85));self.assertEqual(actual.mode,'RGB')
        self.assertIsNone(gpu.decode_jpeg(b'\xff\xd8broken',160))

    def test_modified_runtime_manifest_is_not_trusted(self):
        from setup_gpu import verify_runtime
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'source.json').write_text('{"packages":[],"files":{}}',encoding='utf8')
            self.assertFalse(verify_runtime(folder))

    def test_missing_driver_initializes_once_then_uses_cpu(self):
        with patch.dict(os.environ,{'FRAMEKEEP_GPU':'auto'}),patch.object(gpu,'_ATTEMPTED',False),patch.object(gpu,'_BACKEND',None),\
             patch.dict(gpu._STATUS,{'ready':False,'backend':'cpu'}),patch.object(gpu,'CudaImages',side_effect=OSError('GPU 驱动不可用')) as create:
            self.assertIsNone(gpu.backend());self.assertIsNone(gpu.backend());self.assertEqual(create.call_count,1)
            actual=gpu.resize_preview(fixture(),160);self.assertEqual(actual.size,(160,107));self.assertFalse(gpu.state()['ready'])


if __name__=='__main__':unittest.main()
