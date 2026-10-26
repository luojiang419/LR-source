import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from asset_catalog import AssetCatalog
from everything_search import folder_query
from reference_library import ReferenceLibrary


class SharedArchiveIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shared-archive-index-')
        self.base = Path(self.temp.name)
        self.media = self.base / 'media'
        self.media.mkdir()
        Image.new('RGB', (24, 16), 'teal').save(self.media / 'look.TIF')
        (self.media / 'master.MOV').write_bytes(b'video-container')
        (self.media / 'layout.PSD').write_bytes(b'design-source')
        self.catalog = AssetCatalog(self.base / 'data')
        self.library = ReferenceLibrary(self.base / 'data', catalog=self.catalog)

    def tearDown(self):
        self.library.close()
        self.catalog.close()
        self.temp.cleanup()

    def ready(self, root_id):
        until = time.monotonic() + 10
        while self.library._root(root_id)['status'] in ('queued', 'scanning'):
            if time.monotonic() > until:
                self.fail('共享素材索引超时')
            time.sleep(.02)

    def test_split_and_archive_share_one_database_and_media_index(self):
        root_id = self.library.add_root(str(self.media), '共享媒体')['root_id']
        self.ready(root_id)
        self.assertEqual(self.catalog.db, self.library.catalog(root_id).db)
        self.assertFalse((self.base / 'data' / 'reference-library' / 'catalogs').exists())
        rows = {row['name']: row for row in self.catalog.items(limit=10)['rows']}
        self.assertEqual(rows['look.TIF']['media_type'], 'image')
        self.assertEqual(rows['master.MOV']['media_type'], 'video')
        self.assertEqual(rows['layout.PSD']['media_type'], 'design')
        self.assertEqual(self.catalog.items(text='master')['rows'][0]['name'], 'master.MOV')
        self.assertEqual(self.catalog.items(media='video')['total'], 1)
        self.assertEqual(self.library.items(root_id, text='layout')['rows'][0]['media_format'], 'PSD')
        # 此夹具只有 MOV 文件头；新视频封面逻辑必须报告损坏，而非返回成功占位图。
        with self.assertRaisesRegex(RuntimeError, '无法解码'):
            self.library.preview(root_id, 'master.MOV', 180)

    def test_video_codec_is_cached_by_background_metadata(self):
        root_id = self.library.add_root(str(self.media))['root_id']
        self.ready(root_id)
        item = self.catalog.items(media='video')['rows'][0]
        payload = {'format': {'duration': '12.5', 'format_name': 'mov'}, 'streams': [{'codec_name': 'prores', 'codec_long_name': 'Apple ProRes', 'width': 3840, 'height': 2160}]}
        completed = type('Completed', (), {'stdout': json.dumps(payload)})()
        with patch('asset_catalog.shutil.which', return_value='ffprobe'), patch('asset_catalog.subprocess.run', return_value=completed) as probe, patch('media_engine.ExifToolSession.execute',return_value='[{"File:FileType":"MOV"}]'):
            self.catalog.metadata.request(item['id'])
            while self.catalog.metadata.process_one():pass
            details = self.catalog.details(item['id'])
            self.assertEqual(details['media']['codec'], 'Apple ProRes')
            self.assertEqual(details['media']['duration_ms'], 12500)
            self.assertEqual(details['media']['width'], 3840)
            self.catalog.details(item['id'])
        self.assertEqual(probe.call_count, 1)

    def test_everything_query_covers_the_same_professional_media_extensions(self):
        query = folder_query(self.media)
        for extension in ('dng', 'tif', 'mov', 'mxf', 'r3d', 'braw', 'psd', 'psb', 'ai', 'indd'):
            self.assertIn(extension, query)


if __name__ == '__main__':
    unittest.main()
