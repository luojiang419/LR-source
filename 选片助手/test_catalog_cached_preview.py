"""列表批量缓存提示：命中不查素材库，淘汰/版本/尺寸变化正确回退。"""
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from unittest.mock import patch
import tempfile
import threading
import unittest
import urllib.request

from PIL import Image
from asset_catalog import AssetCatalog
import media_engine as media


class CatalogCachedPreviewTests(unittest.TestCase):
    def test_cached_http_hint_bypasses_catalog_and_source_and_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir()
            exif=Image.Exif();exif[274]=6
            Image.new('RGB',(1200,800),'teal').save(photos/'paired.jpg',exif=exif)
            (photos/'paired.CR3').write_bytes(b'paired-raw')
            catalog=AssetCatalog(base/'data')
            try:
                catalog.add_root(str(photos));row=catalog.items()['rows'][0]
                small=catalog.preview(row['id'],420,read=True)
                large=catalog.preview(row['id'],2048,read=True)
                row=catalog.items()['rows'][0];hint=parse_qs(urlsplit(row['preview']).query)['cached'][0]
                (photos/'paired.jpg').rename(photos/'offline.jpg')
                with patch.object(catalog,'connect',side_effect=AssertionError('命中不应查素材大库')),patch.object(media,'_jpeg_source',side_effect=AssertionError('命中不应读取源图')):
                    self.assertEqual(catalog.preview(row['id'],420,read=True,cached=hint),small)
                    import server
                    with patch.object(server,'CATALOG',catalog):
                        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
                        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
                        try:
                            with urllib.request.urlopen(f'http://127.0.0.1:{http.server_port}'+row['preview']+'&size=420',timeout=3) as response:
                                self.assertEqual(response.read(),small)
                                self.assertIn('immutable',response.headers['Cache-Control'])
                        finally:http.shutdown();http.server_close();thread.join(3)
                # 清晰图不能返回420缓存；无效/不存在的提示仍走真实路径。
                self.assertEqual(catalog.preview(row['id'],2048,read=True,cached=hint),large)
                for invalid in ('../outside.jpg','0'*64+'.jpg'):
                    self.assertEqual(catalog.preview(row['id'],420,read=True,cached=invalid),small)
                with catalog.connect() as c:c.execute("UPDATE items SET fingerprint='changed' WHERE extension='.jpg'")
                updated=catalog.items()['rows'][0]
                self.assertNotIn('cached=',updated['preview'])
                self.assertNotEqual(updated['preview'].split('&cached=')[0],row['preview'].split('&cached=')[0])
            finally:catalog.close()


if __name__=='__main__':unittest.main()
