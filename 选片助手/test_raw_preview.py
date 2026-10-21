"""RAW 浏览预览的资源选择、回退与解码边界。"""
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

import numpy as np
import rawpy
from PIL import Image
import media_engine as media


def jpeg(size=(160,120),orientation=None):
    out=io.BytesIO();image=Image.new('RGB',size,(80,120,160))
    exif=Image.Exif()
    if orientation is not None:exif[274]=orientation
    image.save(out,format='JPEG',exif=exif)
    return out.getvalue()


class RawPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'photo.ARW').write_bytes(b'raw')
        self.record={'raw':'photo.ARW','jpg':None,'fingerprint':'raw-preview','orientation':1}
        self.cache=self.root/'cache';self.tags=[]

    def tearDown(self):self.temp.cleanup()

    def extract(self,payloads):
        def run(args,**kwargs):
            tag=next(arg for arg in args if arg in ('-ThumbnailImage','-PreviewImage','-JpgFromRaw'));self.tags.append(tag)
            value=payloads.get(tag,b'')
            if isinstance(value,Exception):raise value
            return SimpleNamespace(stdout=value,returncode=0)
        return patch.object(media.subprocess,'run',side_effect=run)

    def preview(self,**kwargs):
        return media.preview(self.record,self.root,self.cache,Path('exiftool'),**kwargs)

    def test_small_embedded_thumbnail_wins_over_companion_and_large_preview(self):
        self.record['jpg']='missing.JPG'
        with self.extract({'-ThumbnailImage':jpeg()}),patch('rawpy.RawPy',side_effect=AssertionError('不应打开 LibRaw')):
            path=self.preview()
        self.assertEqual(self.tags,['-ThumbnailImage'])
        with Image.open(path) as image:self.assertEqual(image.size,(160,120))

    def test_missing_thumbnail_uses_companion_before_large_embedded_image(self):
        self.record['jpg']='photo.JPG';(self.root/'photo.JPG').write_bytes(jpeg((1600,1200)))
        with self.extract({}):path=self.preview()
        self.assertEqual(self.tags,['-ThumbnailImage'])
        with Image.open(path) as image:self.assertEqual(image.size,(480,360))

    def test_unicode_raw_path_is_passed_as_utf8_argument_stream(self):
        path=self.root/'中文照片.ARW';path.write_bytes(b'raw');self.record['raw']=path.name
        with self.extract({'-ThumbnailImage':jpeg()}) as run:self.preview()
        args=run.call_args.args[0]
        self.assertEqual(args[-2:],['-@','-']);self.assertNotIn(str(path),args)
        self.assertEqual(run.call_args.kwargs['input'],(str(path)+'\n').encode('utf8'))

    def test_broken_thumbnail_and_preview_fall_back_to_jpg_from_raw(self):
        with self.extract({'-ThumbnailImage':b'\xff\xd8broken','-PreviewImage':b'', '-JpgFromRaw':jpeg((2400,1600))}):
            path=self.preview()
        self.assertEqual(self.tags,['-ThumbnailImage','-PreviewImage','-JpgFromRaw'])
        with Image.open(path) as image:self.assertEqual(image.size,(480,320))

    def test_timeout_does_not_prevent_preview_fallback(self):
        with self.extract({'-ThumbnailImage':media.subprocess.TimeoutExpired('exiftool',15),'-PreviewImage':jpeg()}):
            path=self.preview()
        self.assertTrue(path.is_file());self.assertEqual(self.tags,['-ThumbnailImage','-PreviewImage'])

    def test_libraw_fallback_only_extracts_thumbnail(self):
        source=MagicMock();source.__enter__.return_value=source
        source.extract_thumb.return_value=SimpleNamespace(format=rawpy.ThumbFormat.JPEG,data=jpeg())
        with self.extract({}),patch('rawpy.RawPy',return_value=source),patch('rawpy.imread',side_effect=AssertionError('不应解码 RAW')):
            path=self.preview()
        source.open_file.assert_called_once_with(str(self.root/'photo.ARW'))
        source.extract_thumb.assert_called_once();source.unpack.assert_not_called();source.postprocess.assert_not_called()
        self.assertTrue(path.is_file())

    def test_bitmap_thumbnail_is_oriented_without_raw_postprocess(self):
        self.record['orientation']=6
        source=MagicMock();source.__enter__.return_value=source
        source.extract_thumb.return_value=SimpleNamespace(format=rawpy.ThumbFormat.BITMAP,data=np.zeros((120,160,3),dtype=np.uint8))
        with self.extract({}),patch('rawpy.RawPy',return_value=source):path=self.preview()
        with Image.open(path) as image:self.assertEqual(image.size,(120,160))
        source.postprocess.assert_not_called()

    def test_large_preview_keeps_detail_and_falls_back_to_small_if_needed(self):
        with self.extract({'-ThumbnailImage':jpeg(),'-PreviewImage':jpeg((2400,1600))}):
            path=self.preview(size=1800)
        self.assertEqual(self.tags,['-PreviewImage'])
        with Image.open(path) as image:self.assertEqual(image.size,(1800,1200))
        self.tags=[]
        with self.extract({'-ThumbnailImage':jpeg()}):path=self.preview(size=1801)
        self.assertEqual(self.tags,['-PreviewImage','-JpgFromRaw','-ThumbnailImage'])
        with Image.open(path) as image:self.assertEqual(image.size,(160,120))

    def test_orientation_from_raw_or_embedded_exif_is_applied_once(self):
        self.record['orientation']=6
        for embedded in (None,6):
            with self.subTest(embedded=embedded),self.extract({'-ThumbnailImage':jpeg(orientation=embedded)}):
                path=self.preview(size=480 if embedded is None else 479)
                with Image.open(path) as image:self.assertEqual(image.size,(120,160))

    def test_cached_thumbnail_does_not_wait_for_generation_slot(self):
        with self.extract({'-ThumbnailImage':jpeg()}):path=self.preview()
        slots=MagicMock();slots.__enter__.side_effect=AssertionError('缓存不应等待生成槽位')
        with patch.object(media,'_PREVIEW_SLOTS',slots),patch.object(media.subprocess,'run',side_effect=AssertionError('缓存不应提取')):
            self.assertEqual(self.preview(),path)

    def test_explicit_decode_still_uses_camera_white_balance_and_half_size(self):
        source=MagicMock();source.__enter__.return_value=source
        source.postprocess.return_value=np.zeros((600,800,3),dtype=np.uint8)
        with patch('rawpy.imread',return_value=source),patch.object(media.subprocess,'run',side_effect=AssertionError('解码不应读取内嵌图')):
            path=self.preview(decode=True)
        source.postprocess.assert_called_once_with(use_camera_wb=True,half_size=True)
        with Image.open(path) as image:self.assertEqual(image.size,(480,360))

    def test_no_embedded_image_never_automatically_postprocesses_raw(self):
        source=MagicMock();source.__enter__.return_value=source
        source.extract_thumb.side_effect=rawpy.LibRawNoThumbnailError()
        with self.extract({}),patch('rawpy.RawPy',return_value=source),self.assertRaises(rawpy.LibRawNoThumbnailError):
            self.preview()
        source.postprocess.assert_not_called();self.assertFalse(list(self.cache.glob('*.jpg')))


if __name__=='__main__':unittest.main()
