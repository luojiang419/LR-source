import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from sku_vision import recognize
from sku_match import match,normalize


class OcrCompatibilityTests(unittest.TestCase):
    def provider(self,**kwargs):
        return dict(id='ocr',model='glm-ocr',protocol='chat',url='http://127.0.0.1:1234',key='test-only',**kwargs)
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
    def test_auth_and_unrelated_bad_request_are_not_retried(self):
        for code,detail in [(401,b'Unauthorized'),(400,b'image invalid')]:
            with self.subTest(code=code),patch('urllib.request.urlopen',side_effect=HTTPError('http://localhost',code,'error',{},io.BytesIO(detail))) as request:
                with self.assertRaisesRegex(ValueError,'HTTP '+str(code)):recognize(self.provider(),b'image')
                self.assertEqual(request.call_count,1)


if __name__=='__main__':unittest.main()
