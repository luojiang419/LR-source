"""近似画面核验契约；不加载 CLIP 或修改数据库。"""
import io
import unittest

import numpy as np
from PIL import Image,ImageDraw

from material_search_detail import FrameMatcher


class FrameMatcherTests(unittest.TestCase):
    def setUp(self):
        self.matcher=FrameMatcher(cache_size=2)
        rng=np.random.default_rng(47)
        self.image=Image.fromarray(rng.integers(0,256,(240,180,3),dtype=np.uint8))

    def score(self,first,second):
        return self.matcher.compare(self.matcher.prepare(first),self.matcher.prepare(second))['score']

    def test_identical_and_resized_picture_match(self):
        self.assertGreater(self.score(self.image,self.image),.99)
        self.assertGreater(self.score(self.image,self.image.resize((360,480))),.85)

    def test_jpeg_reencoding_preserves_structure_match(self):
        # 使用有纹理、边缘和平坦区的普通合成场景测试压缩容忍。
        scene=Image.new('RGB',(240,320),'#c9ccd8');draw=ImageDraw.Draw(scene)
        for box,color in [((35,25,200,110),'#b2ac9a'),((45,135,100,300),'#927f62'),((130,135,195,300),'#aa9170')]:draw.rectangle(box,fill=color)
        buffer=io.BytesIO();scene.save(buffer,'JPEG',quality=70)
        with Image.open(io.BytesIO(buffer.getvalue())) as encoded:self.assertGreater(self.score(scene,encoded),.85)

    def test_substantial_local_content_change_is_rejected(self):
        changed=self.image.copy();ImageDraw.Draw(changed).rectangle((0,0,179,130),fill='black')
        self.assertLess(self.score(self.image,changed),.85)

    def test_equal_gray_luminance_but_different_color_is_not_a_match(self):
        red=Image.new('RGB',(100,150),(255,0,0));green=Image.new('RGB',(100,150),(0,129,0))
        self.assertLess(self.score(red,green),.85)

    def test_smooth_picture_without_keypoints_still_compares(self):
        plain=Image.new('RGB',(120,180),'white')
        self.assertGreater(self.score(plain,plain),.99)

    def test_incompatible_frame_shape_is_not_stretched_to_match(self):
        result=self.matcher.compare(self.matcher.prepare(self.image),self.matcher.prepare(self.image.resize((600,80))))
        self.assertEqual(result['score'],0);self.assertFalse(result['comparable']);self.assertIn('画幅',result['reason'])

    def test_cache_is_versioned_and_bounded(self):
        first=self.matcher.prepare(Image.new('RGB',(100,150),'red'),key=('same-path',1))
        second=self.matcher.prepare(Image.new('RGB',(100,150),'blue'),key=('same-path',2))
        self.assertLess(self.matcher.compare(first,second)['score'],.85)
        self.matcher.prepare(self.image,key=('another',1))
        self.assertEqual(len(self.matcher.cache),2);self.assertIsNone(self.matcher.cached(('same-path',1)))


if __name__=='__main__':unittest.main()
