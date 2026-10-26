"""真实 FFmpeg 编解码、精确帧号和封面缓存的隔离验收。"""
from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from asset_catalog import AssetCatalog
from video_preview import VideoPreview, find_tool

ROOT = Path(__file__).resolve().parents[1]


def make_clip(path, codec='ffv1', frames=6):
    width, height = 256, 144
    colors = [(0,0,0), (0,0,0), (255,0,0), (0,255,0), (0,0,255), (255,255,0)][:frames]
    pixels = b''.join(bytes(color) * width * height for color in colors)
    options = {'ffv1': [], 'libx264':['-pix_fmt','yuv420p'],
               'libx265':['-pix_fmt','yuv420p','-x265-params','pools=1:frame-threads=1:log-level=error'],
               'prores_ks':['-profile:v','3','-pix_fmt','yuv422p10le'],
               'dnxhd':['-profile:v','dnxhr_hq','-pix_fmt','yuv422p']}[codec]
    command = [find_tool('ffmpeg'), '-v','error','-nostdin','-threads','2', '-f','rawvideo',
               '-pixel_format','rgb24','-video_size',f'{width}x{height}','-framerate','25','-i','pipe:0',
               '-c:v',codec, '-threads','2', *options, str(path)]
    result = subprocess.run(command, input=pixels, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode: raise AssertionError(result.stderr.decode('utf8', errors='replace'))


def pixel(payload):
    with Image.open(BytesIO(payload)) as image:
        return image.convert('RGB').getpixel((image.width//2, image.height//2))


class VideoPreviewTests(unittest.TestCase):
    def setUp(self):
        if not find_tool('ffmpeg') or not find_tool('ffprobe'): self.skipTest('需先部署 FFmpeg 和 ffprobe')
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name)
        self.photos = self.base / 'clips'; self.photos.mkdir()
        self.catalog = AssetCatalog(self.base / 'data')

    def tearDown(self):
        if hasattr(self, 'temp'): self.temp.cleanup()

    def add(self, name='black-first.mkv', codec='ffv1', frames=6):
        source = self.photos / name; make_clip(source, codec, frames)
        self.catalog.add_root(str(self.photos))
        row = next(row for row in self.catalog.items()['rows'] if row['name'] == name)
        return source, row

    def test_third_frame_setting_restart_cache_identity_and_offline_reuse(self):
        source, row = self.add()
        original = source.read_bytes()
        self.assertIn('frame=3', row['preview'])
        self.assertGreater(pixel(self.catalog.preview(row['id'], read=True))[0], 240)
        self.catalog.video.configure(4)
        self.assertEqual(VideoPreview(self.catalog.data).frame, 4)
        self.assertIn('frame=4', self.catalog.items()['rows'][0]['preview'])
        green = pixel(self.catalog.preview(row['id'], read=True)); self.assertGreater(green[1], 240); self.assertLess(green[0], 15)
        # 旧 URL 保持原帧语义，新设置不能污染 immutable 旧 URL。
        self.assertGreater(pixel(self.catalog.preview(row['id'], read=True, frame=3))[0], 240)
        self.assertEqual(source.read_bytes(), original)
        source.unlink()
        with patch('video_preview.extract_frame', side_effect=AssertionError('缓存命中不得重新读取原视频')):
            self.assertGreater(pixel(self.catalog.preview(row['id'], read=True))[1], 240)

    def test_frame_bounds_short_video_and_corrupt_input(self):
        for value in [True, False, 0, -1, 1001, 3.0, '2.5', None, 'bad']:
            with self.subTest(value=value), self.assertRaises(ValueError): self.catalog.video.configure(value)
        self.assertEqual(self.catalog.video.frame, 3)
        _, row = self.add(frames=4)
        self.catalog.video.configure(100)
        self.assertGreater(pixel(self.catalog.preview(row['id'], read=True))[1], 240)
        broken = self.photos / 'broken.MOV'; broken.write_bytes(b'not a movie')
        self.catalog.scan(self.catalog.state()['roots'][0]['id'])
        bad = next(r for r in self.catalog.items()['rows'] if r['name'] == broken.name)
        with self.assertRaisesRegex(RuntimeError, '无法解码'): self.catalog.preview(bad['id'], read=True)

    def test_real_h264_hevc_prores_and_dnxhr(self):
        for codec, suffix in [('libx264','.mp4'), ('libx265','.mp4'), ('prores_ks','.mov'), ('dnxhd','.mxf')]:
            with self.subTest(codec=codec):
                _, row = self.add(codec + suffix, codec)
                value = pixel(self.catalog.preview(row['id'], read=True))
                self.assertGreater(value[0], 220); self.assertLess(value[1], 25); self.assertLess(value[2], 25)
                self.catalog.metadata.request(row['id'])
                while self.catalog.metadata.process_one():pass
                details = self.catalog.details(row['id'], ROOT)
                self.assertTrue(details['media']['codec']); self.assertEqual(details['capture']['width'],256)

    def test_shared_reference_cache_changes_with_frame_setting(self):
        from reference_library import ReferenceLibrary
        _,row=self.add()
        library=ReferenceLibrary(self.catalog.data,catalog=self.catalog)
        try:
            root=self.catalog.state()['roots'][0]['id']
            query={'root':root,'file':row['relative'],'size':'420','v':'unchanged-source'}
            self.assertGreater(pixel(library.client_resource('local','preview',query))[0],240)
            before=library.items(root)['rows'][0]['fingerprint']
            self.catalog.video.configure(4)
            self.assertNotEqual(library.items(root)['rows'][0]['fingerprint'],before)
            self.assertGreater(pixel(library.client_resource('local','preview',query))[1],240)
        finally:library.close()


if __name__ == '__main__': unittest.main()
