"""CPU OCR 的会话生命周期、完整款号与结果置信语义。"""
from contextlib import ExitStack
from contextlib import nullcontext
import json
import io
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from local_inference import LocalInference
from model_providers import Providers
from paddle_ocr import PaddleOcr,candidate_text,ordered_box
from sku_match import extract_ocr_codes,match,normalize
from sku_vision import recognize


class CpuOcrTests(unittest.TestCase):
    def test_page_direction_uses_bidirectional_evidence_in_all_quadrants(self):
        import numpy as np
        import cv2
        engine=PaddleOcr.__new__(PaddleOcr);engine.np=np;engine.cv2=cv2
        engine._crop=Mock(return_value=np.zeros((20,120,3),dtype='uint8'))
        horizontal=np.array([[10,10],[130,10],[130,30],[10,30]],dtype='float32')
        vertical=np.array([[10,10],[30,10],[30,130],[10,130]],dtype='float32')
        upright={'text':'LC123456-P1','confidence':.99};garbled={'text':'7','confidence':.995}
        for box,readings,expected in [(horizontal,[upright,garbled],0),(horizontal,[garbled,upright],2),(vertical,[upright,garbled],1),(vertical,[garbled,upright],3)]:
            engine._read_crops=Mock(return_value=readings)
            quarter,info=engine._page_direction(np.zeros((150,150,3),dtype='uint8'),[box])
            self.assertEqual(quarter,expected);self.assertFalse(info['uncertain'])
        engine._read_crops=Mock(return_value=[upright,upright])
        quarter,info=engine._page_direction(np.zeros((150,150,3),dtype='uint8'),[horizontal])
        self.assertEqual(quarter,0);self.assertTrue(info['uncertain'])

    def test_rotation_points_map_back_to_source_image(self):
        import numpy as np
        points=np.array([[0,0],[199,0],[199,99],[0,99]],dtype='float32');x=points[:,0];y=points[:,1]
        canonical=[points,np.column_stack((y,199-x)),np.column_stack((199-x,99-y)),np.column_stack((99-y,x))]
        for quarter,transformed in enumerate(canonical):
            np.testing.assert_array_equal(PaddleOcr.source_points(transformed,quarter,200,100),points)

    def test_pipeline_redetects_after_turning_image_and_keeps_input_untouched(self):
        import numpy as np
        import cv2
        from PIL import Image
        import hashlib
        engine=PaddleOcr.__new__(PaddleOcr);engine.np=np;engine.cv2=cv2;engine.load_ms=1
        box=np.array([[1,1],[8,1],[8,4],[1,4]],dtype='float32')
        engine._detect=Mock(return_value=([box],[1,3,32,32]))
        engine._page_direction=Mock(return_value=(2,{'votes':[0,0,1,0],'confidence':1,'uncertain':False}))
        engine._crop=Mock(return_value=np.zeros((3,7,3),dtype='uint8'))
        engine._read_crops=Mock(return_value=[{'text':'AB123456-P1','confidence':.99}])
        pixels=np.arange(10*6*3,dtype='uint8').reshape((6,10,3));payload=io.BytesIO();Image.fromarray(pixels).save(payload,'PNG');raw=payload.getvalue();before=hashlib.sha256(raw).hexdigest()
        result=engine.recognize(raw)
        self.assertEqual(engine._detect.call_count,2)
        np.testing.assert_array_equal(engine._detect.call_args_list[1].args[0],np.rot90(pixels[:,:,::-1],2))
        self.assertEqual(result['text'],'AB123456-P1');self.assertEqual(result['orientation']['correction_ccw_degrees'],180)
        self.assertEqual(result['lines'][0]['reading_points'],box.tolist());self.assertNotEqual(result['lines'][0]['points'],box.tolist())
        self.assertEqual(hashlib.sha256(raw).hexdigest(),before)

    def test_old_glm_cards_migrate_in_place_without_changing_external_selection(self):
        for enabled in (False,True):
            with self.subTest(enabled=enabled),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'model-providers.json'
                old={'providers':[
                    {'id':'legacy','name':'内置 GLM-OCR','category':'ocr','protocol':'local','model':'glm-ocr-local','url':'http://127.0.0.1'},
                    {'id':'remote','name':'外部','category':'ocr','protocol':'chat','model':'glm-ocr','url':'https://example.invalid/v1','secret':'opaque-unchanged'}],
                    'selected':{'ocr':'legacy'},'bindings':{'sku_label':'remote'},'builtin_enabled':enabled}
                path.write_text(json.dumps(old),'utf8');providers=Providers(folder)
                self.assertEqual(providers.data['selected'],old['selected']);self.assertEqual(providers.data['bindings'],old['bindings'])
                self.assertEqual(providers.data['providers'][0]['id'],'legacy')
                self.assertEqual(providers.data['providers'][0]['model'],'pp-ocrv6-medium-local')
                self.assertEqual(providers.data['providers'][1],old['providers'][1])
                first=path.read_bytes();Providers(folder);self.assertEqual(path.read_bytes(),first)
                self.assertNotIn('glm-ocr-local',[p['model'] for p in providers.data['providers']])

    def test_line_wrapped_suffix_retains_characters_and_raw_text(self):
        for text,code in [('款号：LC7832773\n-P805\n尺码：S','LC7832773-P805'),
                          ('1C7832773\n—P805','1C7832773—P805')]:
            self.assertIn(code,extract_ocr_codes(candidate_text(text)))
        self.assertEqual(candidate_text('LC123456\nP805'),'LC123456\nP805')
        self.assertEqual(candidate_text('LC123456\n-\nP805'),'LC123456\n-\nP805')

    def test_rotated_boxes_have_four_distinct_points(self):
        import numpy as np
        for points in [[[0,2],[2,0],[4,2],[2,4]],[[0,0],[4,0],[4,2],[0,2]]]:
            box=ordered_box(points)
            self.assertEqual(len(np.unique(box,axis=0)),4)
            a,b=box[1]-box[0],box[3]-box[0]
            self.assertGreater(abs(float(a[0]*b[1]-a[1]*b[0])),0)

    def test_model_selection_keeps_cpu_models_optional_and_rejects_visual_category(self):
        with tempfile.TemporaryDirectory() as folder:
            providers=Providers(folder)
            for model in ('pp-ocrv6-small-local','pp-ocrv6-medium-local'):
                providers.save({'name':model,'category':'ocr','protocol':'local','model':model,'select':True})
                self.assertEqual(providers.selected('ocr')['model'],model)
                with self.assertRaises(ValueError):providers.save({'name':'wrong','category':'multimodal','protocol':'local','model':model})
            providers.set_builtin(True)
            self.assertEqual(providers.selected('ocr')['model'],'pp-ocrv6-medium-local')
            providers.set_builtin(False)
            self.assertEqual(providers.selected('ocr')['model'],'pp-ocrv6-medium-local')

    def test_cpu_path_works_without_llama_or_network_reuses_and_releases(self):
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            root=Path(folder);item={}
            for name in ('detector','recognizer','dictionary'):
                path=root/name;path.touch();item[name]=str(path)
            config=root/'models.json';config.write_text(json.dumps({'models':{'pp-ocrv6-medium-local':item}}),'utf8')
            runtime=LocalInference(config,root/'logs');stack.callback(runtime.close)
            stack.enter_context(patch('local_inference._runtime',runtime))
            engine=Mock();engine.recognize.return_value={'text':'款号：LC7832773\n-P805','lines':[{'text':'LC7832773','confidence':.999}], 'timing':{}}
            factory=stack.enter_context(patch('paddle_ocr.PaddleOcr',return_value=engine))
            stack.enter_context(patch('urllib.request.urlopen',side_effect=AssertionError('纯 CPU 不应联网')))
            stack.enter_context(patch('subprocess.Popen',side_effect=AssertionError('不应启动 llama')))
            provider={'id':'medium','model':'pp-ocrv6-medium-local','protocol':'local','url':'http://127.0.0.1'}
            for _ in range(2):result=recognize(provider,b'image')
            legacy=recognize({**provider,'model':'glm-ocr-local'},b'image')
            self.assertEqual(legacy['model'],'pp-ocrv6-medium-local');self.assertEqual(legacy['engine'],'local|pp-ocrv6-medium-local')
            self.assertEqual(result['codes'],['LC7832773-P805']);self.assertIn('\n-P805',result['text'])
            self.assertEqual(result['confidence'],0);self.assertTrue(result['unscored'])
            row={'id':'one','sku':'LC7832773-P805','normalized':normalize('LC7832773-P805'),'sheet':'test','excel_row':2}
            self.assertEqual(match([result],[row])['status'],'review')
            from asset_analysis import AssetAnalysis
            providers=Mock();providers.by_id.return_value=provider;providers.inference_slot.return_value=nullcontext()
            check=AssetAnalysis.test_provider(SimpleNamespace(providers=providers,closed=threading.Event()),'medium')
            self.assertTrue(check['ok']);self.assertIn('CPU',check['message'])
            factory.assert_called_once();self.assertIsNone(runtime.process)
            runtime.preload(None);until=time.monotonic()+2
            while runtime.warm_thread is not None and time.monotonic()<until:time.sleep(.01)
            self.assertIsNone(runtime.ocr);self.assertEqual(runtime.snapshot()['status'],'idle')

    def test_loading_failure_does_not_report_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'model';path.touch();config=root/'config.json'
            config.write_text(json.dumps({'models':{'pp-ocrv6-small-local':{k:str(path) for k in ('detector','recognizer','dictionary')}}}),'utf8')
            runtime=LocalInference(config,root/'logs')
            with patch('paddle_ocr.PaddleOcr',side_effect=ValueError('bad weights')),self.assertRaisesRegex(ValueError,'bad weights'):
                runtime._start('pp-ocrv6-small-local')
            self.assertEqual(runtime.snapshot()['status'],'failed');self.assertIsNone(runtime.ocr);runtime.close()


if __name__=='__main__':unittest.main()
