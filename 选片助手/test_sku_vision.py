import io
import json
import unittest
from PIL import Image
from unittest.mock import patch
from urllib.error import HTTPError

from sku_vision import recognize
from sku_match import match,normalize


class OcrCompatibilityTests(unittest.TestCase):
    def provider(self,**kwargs):
        return dict(id='ocr',model='test-vision',protocol='chat',url='http://127.0.0.1:1234',key='test-only',**kwargs)
    def response(self,content):
        return io.BytesIO(json.dumps({'choices':[{'message':{'content':content}}]}).encode())
    def test_lmstudio_format_rejection_retries_plain_and_keeps_unscored_reading(self):
        calls=[]
        def request(req,timeout):
            calls.append(json.loads(req.data))
            if len(calls)==1:raise HTTPError(req.full_url,400,'Bad Request',{},io.BytesIO(b'{"error":"response_format.type must be json_schema or text"}'))
            return self.response('```json\n{"text":"LCJ81166\\n1281布\\n1371纬\\n10.8米"}\n```')
        with patch('urllib.request.urlopen',side_effect=request):result=recognize(self.provider(),b'image')
        self.assertEqual(len(calls),2);self.assertIn('response_format',calls[0]);self.assertNotIn('response_format',calls[1])
        self.assertEqual(result['codes'],['LCJ81166']);self.assertEqual(result['confidence'],0);self.assertTrue(result['unscored'])
        row={'id':'row','sku':'LCJ81166','normalized':normalize('LCJ81166'),'sheet':'test','excel_row':2}
        self.assertEqual(match([result],[row])['status'],'review')
    def test_json_disabled_plain_text_is_supported_without_inventing_confidence(self):
        with patch('urllib.request.urlopen',return_value=self.response('LCJ81166')) as request:
            result=recognize(self.provider(json_mode=False),b'image')
        self.assertNotIn('response_format',json.loads(request.call_args.args[0].data));self.assertEqual(result['codes'],['LCJ81166']);self.assertEqual(result['confidence'],0)

    def test_webp_preview_is_sent_as_real_jpeg_for_existing_ocr_protocol(self):
        import base64
        image=io.BytesIO();Image.new('RGB',(640,480),'white').save(image,'WEBP',quality=82)
        with patch('urllib.request.urlopen',return_value=self.response('AB-123456')) as request:
            recognize(self.provider(json_mode=False),image.getvalue())
        body=json.loads(request.call_args.args[0].data);url=body['messages'][0]['content'][1]['image_url']['url']
        self.assertTrue(url.startswith('data:image/jpeg;base64,'))
        with Image.open(io.BytesIO(base64.b64decode(url.split(',')[1]))) as decoded:
            self.assertEqual(decoded.format,'JPEG');self.assertEqual(decoded.size,(640,480))
    def test_auth_and_unrelated_bad_request_are_not_retried(self):
        for code,detail in [(401,b'Unauthorized'),(400,b'image invalid')]:
            with self.subTest(code=code),patch('urllib.request.urlopen',side_effect=HTTPError('http://localhost',code,'error',{},io.BytesIO(detail))) as request:
                with self.assertRaisesRegex(ValueError,'HTTP '+str(code)):recognize(self.provider(),b'image')
                self.assertEqual(request.call_count,1)

    def test_builtin_glm_uses_native_ocr_task_and_resizes_without_guessing_confidence(self):
        import base64
        image=io.BytesIO();Image.new('RGB',(2400,1200),'white').save(image,'JPEG')
        provider=self.provider(local_runtime=True,json_mode=True,image_size=1200,max_tokens=1200)
        provider['model']='glm-ocr-local'
        with patch('urllib.request.urlopen',return_value=self.response('SIZE:S\nLC7832730-P604')) as request:
            result=recognize(provider,image.getvalue())
        body=json.loads(request.call_args.args[0].data);parts=body['messages'][0]['content']
        self.assertEqual(parts[0]['text'],'Text Recognition:');self.assertNotIn('response_format',body)
        with Image.open(io.BytesIO(base64.b64decode(parts[1]['image_url']['url'].split(',')[1]))) as resized:self.assertEqual(resized.size,(1200,600))
        self.assertIn('LC7832730-P604',result['codes']);self.assertTrue(result['unscored']);self.assertEqual(result['confidence'],0)
        self.assertTrue(provider['json_mode']);self.assertEqual(provider['max_tokens'],1200)

    def test_lmstudio_glm_uses_native_prompt_without_local_runtime_flag(self):
        image=io.BytesIO();Image.new('RGB',(100,100),'white').save(image,'JPEG')
        provider=self.provider(json_mode=True);provider['model']='glm-ocr'
        with patch('urllib.request.urlopen',return_value=self.response('LC7877496-P905')) as request:
            result=recognize(provider,image.getvalue())
        body=json.loads(request.call_args.args[0].data)
        self.assertEqual(body['messages'][0]['content'][0]['text'],'Text Recognition:')
        self.assertNotIn('response_format',body)
        self.assertEqual(result['codes'],['LC7877496-P905'])

    def test_builtin_glm_rejects_prompt_echo_and_truncated_results(self):
        image=io.BytesIO();Image.new('RGB',(480,480),'white').save(image,'JPEG')
        provider=self.provider(local_runtime=True);provider['model']='glm-ocr-local'
        for content in ('完整可见文本','逐字读取的完整款号'):
            with patch('urllib.request.urlopen',return_value=self.response(content)):
                with self.assertRaisesRegex(ValueError,'提示模板'):recognize(provider,image.getvalue())
        response=io.BytesIO(json.dumps({'choices':[{'message':{'content':'LC7832730-P604'},'finish_reason':'length'}]}).encode())
        with patch('urllib.request.urlopen',return_value=response):
            with self.assertRaisesRegex(ValueError,'长度上限'):recognize(provider,image.getvalue())

    def test_timeout_is_reported_as_timeout_instead_of_configuration_error(self):
        with patch('urllib.request.urlopen',side_effect=TimeoutError):
            with self.assertRaisesRegex(ValueError,'识别超时'):recognize(self.provider(),b'image')

    def test_product_code_is_not_mixed_with_production_batch_and_date(self):
        for text,expected in [('SIZE:S\nLC7832730-P604\n261009 G82\n70%COTTON',['LC7832730-P604']),('款 号:L07878089\n面 料:8348\n锁边线:S764',['L07878089']),('LC7832730-P604\nLC7832730-P804\n261009 G82',['LC7832730-P604','LC7832730-P804'])]:
            with self.subTest(text=text),patch('urllib.request.urlopen',return_value=self.response(text)):
                result=recognize(self.provider(json_mode=False),b'image')
            self.assertEqual(result['codes'],expected);self.assertEqual(result['text'],text);self.assertEqual(result['confidence'],0)


if __name__=='__main__':unittest.main()
