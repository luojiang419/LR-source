"""固定文件直读：显示请求不读取/发布缓存，旧生成入口不可重新启用。"""
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
import json,tempfile,unittest,urllib.request,urllib.error
from unittest.mock import patch
from PIL import Image
from direct_previews import preview_bytes,state
from asset_catalog import AssetCatalog
from test_jpeg_receiving_preview import camera_jpeg
from test_live_direct_thumbnails import runtime


class DirectOnlyTests(unittest.TestCase):
    def test_pipeline_without_preview_service_reports_folder_wait_without_crashing(self):
        from archive_analysis_pipeline import ArchiveAnalysisPipeline
        from types import SimpleNamespace
        service=ArchiveAnalysisPipeline();service.catalog=SimpleNamespace(preview_warmer=None,visits=SimpleNamespace(state=lambda:dict(root='',running=False,thumbnail_cache_enabled=False)))
        service.config=dict(enabled=False,paused=True);service.sku=None;service.providers=SimpleNamespace(purpose=lambda _:None,selected=lambda _:None);service.scheduler=SimpleNamespace(state=lambda:{})
        result=service.pipeline_state();self.assertEqual(result['decode'],{});self.assertEqual(result['phase'],'idle');self.assertFalse(result['can_start'])

    def test_pipeline_does_not_wait_for_cache_when_direct_mode_is_active(self):
        from archive_analysis_pipeline import ArchiveAnalysisPipeline
        from types import SimpleNamespace
        service=ArchiveAnalysisPipeline();service.catalog=SimpleNamespace(preview_warmer=None,visits=SimpleNamespace(state=lambda:dict(root='photos',running=True,thumbnail_cache_enabled=False)))
        self.assertEqual(service._preview_blocker(),'')

    def test_clear_jpeg_uses_full_source_and_preserves_orientation_without_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'photo.jpg';path.write_bytes(camera_jpeg(6))
            with patch('media_engine.cache_for',side_effect=AssertionError('不得读取缓存')):
                payload=preview_bytes({'jpg':path.name},folder,lambda:None,2048)
            with Image.open(BytesIO(payload)) as image:
                self.assertEqual(image.size,(1365,2048));self.assertGreater(image.getpixel((500,500))[1],60)
            self.assertEqual(list(Path(folder).iterdir()),[path])

    def test_existing_business_preview_signature_also_reads_file_without_cache(self):
        import media_engine
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'photo.jpg';path.write_bytes(camera_jpeg())
            with patch('media_engine.cache_for',side_effect=AssertionError('业务预览不得构建缓存')):
                payload=media_engine.preview_bytes({'jpg':path.name},folder,Path(folder)/'unused-cache',lambda:None,1600)
            with Image.open(BytesIO(payload)) as image:self.assertEqual(max(image.size),1600)
            self.assertFalse((Path(folder)/'unused-cache').exists())

    def test_raw_detail_reads_embedded_pixels_and_orientation_without_catalog_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'photo.CR3';path.write_bytes(b'raw-fixture')
            buf=BytesIO();Image.new('RGB',(1800,1200),'teal').save(buf,format='JPEG')
            with patch('media_engine.cache_for',side_effect=AssertionError('不得读取缓存')),patch('media_engine.raw_embedded_metadata',return_value=(6,buf.getvalue())),patch('rawpy.imread',side_effect=AssertionError('浏览不应展开RAW')):
                payload=preview_bytes({'raw':path.name,'jpg':None},folder,lambda:'exiftool',1800)
            with Image.open(BytesIO(payload)) as image:self.assertEqual(image.size,(1200,1800))

    def test_video_frame_and_design_are_read_on_demand(self):
        with patch('video_preview.extract_frame',return_value=Image.new('RGB',(1600,900),'teal')) as frame:
            preview_bytes({'_video':'clip.mp4','_video_frame':10},'.',None,1200);self.assertEqual(frame.call_args.args,('clip.mp4',1200,10))
        with patch('design_preview.preview',return_value=Image.new('RGB',(1500,1000),'teal')) as design:
            payload=preview_bytes({'_design':'photo.psd'},'.',None,1200);design.assert_called_once_with('photo.psd',1200)
            with Image.open(BytesIO(payload)) as image:self.assertEqual(image.size,(1200,800))

    def test_catalog_all_display_sizes_ignore_cached_hint_and_create_no_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);photos=root/'photos';photos.mkdir();(photos/'photo.jpg').write_bytes(camera_jpeg())
            catalog=AssetCatalog(root/'data');rid=catalog.add_root(str(photos))['root_id'];catalog.scan(rid)
            try:
                row=catalog.items(root=rid,sort='original')['rows'][0]
                with patch('media_engine.cache_for',side_effect=AssertionError('不得访问预览库')),patch.object(catalog,'render_preview',side_effect=AssertionError('不得构建缓存')):
                    for size in (420,2048):
                        payload=catalog.preview(row['id'],size,read=True,cached='a'*64+'.jpg')
                        with Image.open(BytesIO(payload)) as image:self.assertEqual(max(image.size),160 if size==420 else 2048)
                self.assertFalse(catalog.previews.exists());self.assertFalse((root/'data/previews').exists())
            finally:catalog.close()

    def test_http_old_generation_controls_return_gone_without_cache_access(self):
        with runtime() as (server,store,sid,url),patch('media_engine.cache_for',side_effect=AssertionError('不得访问预览库')):
            with urllib.request.urlopen(url+'/api/preview-cache',timeout=3) as response:self.assertEqual(json.load(response),state())
            for endpoint in ('/api/preview-cache','/api/preview-cache/manage','/api/catalog/decode','/api/catalog/thumbnails/repair'):
                req=urllib.request.Request(url+endpoint,data=json.dumps({'action':'start','limit_gb':10}).encode(),headers={'Content-Type':'application/json','X-Framekeep-Token':server.TOKEN})
                with self.subTest(endpoint=endpoint),self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(req,timeout=3)
                self.assertEqual(error.exception.code,410)
            self.assertFalse((store.data/'previews').exists())

    def test_http_reloads_changed_source_even_with_unchanged_old_preview_url(self):
        with runtime() as (server,store,sid,url):
            address=url+server.ui_photos(sid)[0]['preview']
            with urllib.request.urlopen(address) as response:first=response.read()
            Image.new('RGB',(700,400),'blue').save(Path(store.session(sid)['root'])/'photo.jpg')
            with urllib.request.urlopen(address) as response:
                self.assertEqual(response.headers['Cache-Control'],'no-store');second=response.read()
            self.assertNotEqual(first,second)
            with Image.open(BytesIO(second)) as image:self.assertGreater(image.getpixel((50,50))[2],200)


if __name__=='__main__':unittest.main()
