import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from catalog_store import Store
import media_engine as media
from file_reveal import reveal_capture

ROOT=Path(__file__).resolve().parents[1]


class OrientationTests(unittest.TestCase):
    def test_all_eight_exif_transforms_preserve_correct_corners(self):
        colors=[(255,0,0),(0,255,0),(0,0,255),(255,255,0)]
        image=Image.new('RGB',(60,40))
        for box,color in zip([(0,0,30,20),(30,0,60,20),(0,20,30,40),(30,20,60,40)],colors):image.paste(color,box)
        expected={1:[0,1,2,3],2:[1,0,3,2],3:[3,2,1,0],4:[2,3,0,1],5:[0,2,1,3],6:[2,0,3,1],7:[3,1,2,0],8:[1,3,0,2]}
        for orientation,order in expected.items():
            with self.subTest(orientation=orientation):
                out=io.BytesIO();exif=Image.Exif();exif[274]=orientation;image.save(out,format='PNG',exif=exif);out.seek(0)
                preview=media._read_preview(out,480,6)
                self.assertEqual(preview.size,(40,60) if orientation>=5 else (60,40))
                w,h=preview.size;self.assertEqual([preview.getpixel(p) for p in [(2,2),(w-3,2),(2,h-3),(w-3,h-3)]],[colors[i] for i in order])
    def test_jpeg_catalog_fallback_explicit_upright_and_cache_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);file=base/'入库.JPG';Image.new('RGB',(120,80),'red').save(file)
            record={'jpg':file.name,'raw':None,'fingerprint':'same-file','orientation':6}
            rotated=media.preview(record,base,base/'cache',Path('unused'))
            with Image.open(rotated) as image:self.assertEqual(image.size,(80,120));self.assertNotIn(274,image.getexif())
            record['orientation']=1;upright=media.preview(record,base,base/'cache',Path('unused'));self.assertNotEqual(rotated,upright)
            with Image.open(upright) as image:self.assertEqual(image.size,(120,80))
            exif=Image.Exif();exif[274]=1;Image.new('RGB',(120,80),'red').save(file,exif=exif)
            record.update(fingerprint='explicit-upright',orientation=6)
            with Image.open(media.preview(record,base,base/'cache',Path('unused'))) as image:self.assertEqual(image.size,(120,80))


class RevealTests(unittest.TestCase):
    def test_only_known_components_and_root_confined_files(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);photos=base/'photos';photos.mkdir();image=photos/'照片,含空格.JPG';Image.new('RGB',(20,30)).save(image)
            store=Store(base/'data',ROOT);sid=store.ensure_live_session(photos);record=media.capture_record(photos,image,None,{})
            store.append_live(sid,[record]);seen=[]
            with patch('file_reveal.reveal_path',side_effect=lambda p:seen.append(p)):
                self.assertEqual(reveal_capture(store,sid,record['id']),{'ok':True,'component':'jpg'});self.assertEqual(seen,[image.resolve()])
                for sid2,cid,component in [('bad',record['id'],None),(sid,'bad',None),(sid,record['id'],'../../evil')]:
                    with self.assertRaises(ValueError):reveal_capture(store,sid2,cid,component)
                with store.connect() as c:
                    bad=dict(record,jpg='../outside.JPG');c.execute('UPDATE captures SET data=? WHERE session=? AND id=?',(json.dumps(bad),sid,record['id']))
                with self.assertRaises(ValueError):reveal_capture(store,sid,record['id'])
            self.assertEqual(len(seen),1)


if __name__=='__main__':unittest.main()
