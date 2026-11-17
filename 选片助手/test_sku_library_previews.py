"""SKU小图真实解码复用、版本失效和同时请求合并。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile,threading,unittest
from unittest.mock import patch

from PIL import Image
from archive_thumbnails import ArchiveThumbnails
import sku_library_previews as previews


class LibraryPreviewTests(unittest.TestCase):
    def test_real_image_reused_then_file_version_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);image=root/'photo.jpg';Image.new('RGB',(600,400),'red').save(image)
            record={'id':'one','jpg':image.name,'fingerprint':'version-1','orientation':1}
            with patch.object(previews,'_THUMBNAILS',ArchiveThumbnails(root)),patch.object(previews,'preview_bytes',wraps=previews.preview_bytes) as read:
                first=previews.library_preview_bytes(record,root,None,420)
                self.assertEqual(first,previews.library_preview_bytes(record,root,None,420));self.assertEqual(read.call_count,1)
                Image.new('RGB',(600,400),'blue').save(image);record['fingerprint']='version-2'
                self.assertNotEqual(first,previews.library_preview_bytes(record,root,None,420));self.assertEqual(read.call_count,2)
            self.assertEqual([p.name for p in root.iterdir()],['photo.jpg'])

    def test_concurrent_requests_decode_once_and_failure_can_retry(self):
        gate=threading.Event();entered=threading.Event();record={'id':'one','jpg':'one.jpg','fingerprint':'sha'}
        def slow(*args):entered.set();self.assertTrue(gate.wait(3));return b'jpeg'
        with patch.object(previews,'_THUMBNAILS',ArchiveThumbnails(Path('.'))),patch.object(previews,'preview_bytes',side_effect=slow) as read:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures=[pool.submit(previews.library_preview_bytes,record,'root',None,420) for _ in range(3)]
                self.assertTrue(entered.wait(3));gate.set()
                self.assertEqual([f.result(3) for f in futures],[b'jpeg']*3);self.assertEqual(read.call_count,1)
        with patch.object(previews,'_THUMBNAILS',ArchiveThumbnails(Path('.'))),patch.object(previews,'preview_bytes',side_effect=[OSError('offline'),b'jpeg']) as read:
            with self.assertRaises(OSError):previews.library_preview_bytes(record,'root',None,420)
            self.assertEqual(previews.library_preview_bytes(record,'root',None,420),b'jpeg');self.assertEqual(read.call_count,2)
