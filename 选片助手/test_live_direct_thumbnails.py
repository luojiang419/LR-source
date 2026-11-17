"""现场直读通道：不依赖缓存，方向、格式回退及大图保持可用。"""
from contextlib import ExitStack,contextmanager
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import json,tempfile,threading,unittest,urllib.request
from unittest.mock import patch
from PIL import Image
from catalog_store import Store,stable_json
from live_thumbnails import thumbnail_bytes
from test_jpeg_receiving_preview import camera_jpeg

ROOT=Path(__file__).resolve().parents[1]


class LiveThumbnailTests(unittest.TestCase):
    def test_embedded_pixels_orientation_and_no_persistent_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'photo.jpg';path.write_bytes(camera_jpeg(6))
            with patch('media_engine.cache_for',side_effect=AssertionError('不得访问缓存')):
                payload=thumbnail_bytes({'jpg':path.name,'orientation':1},folder,lambda:None)
            with Image.open(BytesIO(payload)) as image:
                self.assertEqual(image.size,(100,160));self.assertGreater(image.getpixel((40,40))[0],200)
            self.assertEqual(list(Path(folder).iterdir()),[path])

    def test_missing_or_broken_embedded_image_uses_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'photo.jpg';Image.new('RGB',(1000,600),'teal').save(path)
            for embedded in (None,(b'\xff\xd8broken\xff\xd9',1)):
                with self.subTest(embedded=embedded),patch('live_thumbnails.jpeg_thumbnail',return_value=embedded):
                    payload=thumbnail_bytes({'jpg':path.name,'orientation':6},folder,lambda:None)
                with Image.open(BytesIO(payload)) as image:self.assertEqual(image.size,(288,480))

    def test_raw_only_and_missing_companion_keep_raw_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            for jpg in (None,'missing.jpg'):
                record={'jpg':jpg,'raw':'photo.CR3','orientation':6}
                with self.subTest(jpg=jpg),patch('media_engine._raw_preview',return_value=Image.new('RGB',(700,1000),'teal')) as raw:
                    payload=thumbnail_bytes(record,folder,lambda:None)
                    raw.assert_called_once()
                with Image.open(BytesIO(payload)) as image:self.assertEqual(max(image.size),480)

    def test_png_preserves_record_orientation(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'photo.png';Image.new('RGB',(800,400),'teal').save(path)
            payload=thumbnail_bytes({'jpg':path.name,'orientation':6},folder,lambda:None)
            with Image.open(BytesIO(payload)) as image:self.assertEqual(image.size,(240,480))

    def test_path_outside_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):thumbnail_bytes({'jpg':'../outside.jpg'},folder,lambda:None)


@contextmanager
def runtime():
    import server
    with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
        root=Path(folder);photos=root/'photos';photos.mkdir();(photos/'photo.jpg').write_bytes(camera_jpeg(6))
        store=Store(root/'data',ROOT);sid=store.ensure_live_session(photos);store.activate(sid)
        record={'id':'photo','jpg':'photo.jpg','raw':None,'fingerprint':'fingerprint','orientation':6,'time':'2026-11-11T10:00:00','cameraKey':'camera','existingRating':0,'fileCount':1}
        with store.connect() as c:c.execute('INSERT INTO captures VALUES(?,?,?,?)',(sid,'photo',stable_json(record),0))
        for name,value in dict(STORE=store,DATA=store.data,WATCH=None,PREVIEWS=None,DIRECT=None).items():stack.enter_context(patch.object(server,name,value))
        stack.enter_context(patch.object(server,'workbench_views',return_value=None))
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True;threading.Thread(target=http.serve_forever,daemon=True).start()
        try:yield server,store,sid,f'http://127.0.0.1:{http.server_port}'
        finally:http.shutdown();http.server_close()


class LiveThumbnailHttpTests(unittest.TestCase):
    def test_history_and_instant_small_images_bypass_cache_and_old_urls(self):
        with runtime() as (server,store,sid,url):
            photo=server.ui_photos(sid)[0];self.assertIn('thumbnail_source=file',photo['preview'])
            self.assertNotIn('thumbnail_source',photo['previewLarge']);self.assertNotIn('thumbnail_source',photo['previewFull'])
            with patch('media_engine.preview_bytes',side_effect=AssertionError('不得等待旧缓存')):
                for address in (photo['preview'],f'/api/preview/{sid}/photo?size=420&instant=1&v=fingerprint'):
                    with self.subTest(address=address),urllib.request.urlopen(url+address,timeout=3) as response:
                        self.assertEqual(response.headers['Cache-Control'],'no-store')
                        with Image.open(BytesIO(response.read())) as image:self.assertEqual(image.size,(100,160))

    def test_large_decode_sku_library_and_full_original_use_files_without_cache(self):
        with runtime() as (server,store,sid,url):
            with patch('media_engine.preview_bytes',side_effect=AssertionError('不得构建预览缓存')),patch('media_engine.cache_for',side_effect=AssertionError('不得访问预览库')):
                for query,edge in (('size=2048',2048),('size=480&decode=1',480),('size=420',160)):
                    prefix='/api/sku/library/preview/' if edge==160 else '/api/preview/'
                    with self.subTest(query=query),urllib.request.urlopen(f'{url}{prefix}{sid}/photo?{query}&v=fingerprint',timeout=3) as response:
                        self.assertEqual(response.headers['Cache-Control'],'no-store')
                        with Image.open(BytesIO(response.read())) as image:self.assertEqual(max(image.size),edge)
            with patch('media_engine.preview_bytes',side_effect=AssertionError('原图不得重新编码')),urllib.request.urlopen(url+server.ui_photos(sid)[0]['previewFull'],timeout=3) as response:
                self.assertEqual(response.read(),(Path(store.session(sid)['root'])/'photo.jpg').read_bytes())


if __name__=='__main__':unittest.main()
