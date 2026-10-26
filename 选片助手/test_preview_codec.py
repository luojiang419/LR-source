"""压缩体积、分辨率、离线旧缓存和 WebP/JPEG 兼容边界。"""
from contextlib import closing
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image,ImageDraw

import media_engine as media
from preview_cache import PreviewCache,cache_name
from preview_codec import compatible_image,image_mime,save_preview,validate_stream


class PreviewCodecTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.cache=PreviewCache(self.root/'cache')

    def test_generated_jpeg_preserves_resolution_source_and_omits_source_metadata(self):
        photo=self.root/'photo.JPG';exif=Image.Exif();exif[315]='PRIVATE_AUTHOR'
        Image.new('RGB',(2400,1600),'teal').save(photo,exif=exif)
        before=hashlib.sha256(photo.read_bytes()).hexdigest()
        record=media.capture_record(self.root,photo,None,{})
        for size in (480,1800):
            path=media.preview(record,self.root,self.cache.folder,None,size,cache_index=self.cache)
            self.assertEqual(path.suffix,'.jpg');self.assertEqual(image_mime(path.read_bytes()),'image/jpeg')
            with Image.open(path) as image:
                self.assertEqual(image.format,'JPEG');self.assertEqual(image.size,(size,size*2//3))
                self.assertFalse(image.getexif());self.assertNotIn('icc_profile',image.info)
            self.assertNotIn(b'PRIVATE_AUTHOR',path.read_bytes())
        self.assertEqual(hashlib.sha256(photo.read_bytes()).hexdigest(),before)
        with patch.object(media,'_companion_preview',side_effect=AssertionError('热缓存不应重读原图')):
            payload=media.preview_bytes(record,self.root,self.cache.folder,None,1800)
            self.assertEqual(image_mime(payload),'image/jpeg')

    def test_fast_jpeg_preserves_label_and_texture_detail(self):
        y,x=np.mgrid[:800,:1200]
        data=np.stack((100+60*np.sin(x/23),120+60*np.cos(y/19),150+40*np.sin((x+y)/37)),axis=2).clip(0,255).astype('uint8')
        image=Image.fromarray(data);ImageDraw.Draw(image).text((80,80),'SKU AB-123456 / DETAILS',fill='black',font_size=55)
        jpeg=io.BytesIO();image.save(jpeg,format='JPEG',quality=85,optimize=True)
        path=self.root/'sample.jpg';save_preview(image,path)
        self.assertEqual(image_mime(path.read_bytes()),'image/jpeg')
        original=np.asarray(image,dtype=float)
        with Image.open(path) as decoded:actual=np.asarray(decoded.convert('RGB'),dtype=float)
        error=np.mean((original-actual)**2)
        self.assertGreater(10*np.log10(255**2/error),35)

    def test_webp_length_mismatch_and_truncated_jpeg_are_rejected(self):
        path=self.root/'image.webp';Image.new('RGB',(100,80),'red').save(path,'WEBP')
        payload=path.read_bytes()
        self.assertEqual(validate_stream(io.BytesIO(payload),len(payload)),'image/webp')
        for value in (payload[:-2],b'RIFF'+b'\x00'*4+b'WEBPxxxx',b'\xff\xd8broken'):
            with self.assertRaises(ValueError):validate_stream(io.BytesIO(value),len(value))

    def test_legacy_offline_jpeg_is_reused_without_raw_or_image_decode(self):
        record={'fingerprint':'legacy','jpg':'offline.JPG','orientation':1}
        name=cache_name(record,480,version=4);path=self.cache.folder/name
        Image.new('RGB',(480,320),'blue').save(path)
        with self.cache.connect() as db:
            db.execute('INSERT INTO previews(name,bytes,accessed,fingerprint,edge,decoded,orientation,digest) VALUES (?,?,?,?,?,?,?,?)',
                       (name,path.stat().st_size,1,record['fingerprint'],480,0,1,self.cache._digest(path)))
        with patch.object(media,'_companion_preview',side_effect=AssertionError('离线旧缓存不得重新解码')):
            result=media.preview(record,self.root,self.cache.folder,None,cache_index=self.cache)
        self.assertEqual(result,path);self.assertTrue(path.exists())
        self.assertNotEqual(cache_name(record,480),name)

    def test_legacy_client_transcode_does_not_write_second_cache_file(self):
        path=self.root/'image.webp';Image.new('RGB',(100,80),'red').save(path,'WEBP')
        original=path.read_bytes();before=set(self.root.rglob('*'))
        self.assertEqual(compatible_image(original,'image/webp,image/jpeg'),original)
        legacy=compatible_image(original)
        self.assertEqual(image_mime(legacy),'image/jpeg')
        self.assertEqual(image_mime(compatible_image(original,'image/webp;q=0,image/jpeg')),'image/jpeg')
        with Image.open(io.BytesIO(legacy)) as image:self.assertEqual(image.size,(100,80))
        self.assertEqual(set(self.root.rglob('*')),before)

    def test_asset_analysis_webp_request_keeps_compatible_jpeg_payload(self):
        import base64
        from asset_analysis import vision
        path=self.root/'image.webp';Image.new('RGB',(640,480),'white').save(path,'WEBP')
        provider={'protocol':'chat','url':'http://localhost','model':'test'}
        with patch('asset_analysis.request_json',return_value={'choices':[{'message':{'content':'{}'}}]}) as request:
            self.assertEqual(vision(provider,path.read_bytes(),'test',{'type':'object'}),'{}')
        url=request.call_args.args[1]['messages'][1]['content'][1]['image_url']['url']
        with Image.open(io.BytesIO(base64.b64decode(url.split(',')[1]))) as decoded:self.assertEqual(decoded.format,'JPEG')

    def test_reference_cache_uses_webp_filename_and_reuses_it_after_restart(self):
        from reference_cache import ReferenceCache
        stream=io.BytesIO();Image.new('RGB',(100,80),'red').save(stream,'WEBP',quality=82)
        payload=stream.getvalue();folder=self.root/'references';cache=ReferenceCache(folder)
        self.assertEqual(cache.get_or_load('local','root','photo.JPG','preview',{},lambda:payload),payload)
        self.assertEqual(len(list(folder.glob('*.webp'))),1);self.assertFalse(list(folder.glob('*.jpg')))
        cache=ReferenceCache(folder)
        self.assertEqual(cache.get_or_load('local','root','photo.JPG','preview',{},lambda:self.fail('不能重复提取')),payload)
        self.assertEqual(cache.clear()['removed_files'],1);self.assertFalse(list(folder.glob('*.webp')))


if __name__=='__main__':unittest.main()
