"""内置 OCR 预加载：开关不阻塞、真实可测进度、代次取消及复用。"""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock,patch

from local_inference import LocalInference
from ocr_progress import OcrProgress


class OcrPreloadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        for name in ('server.exe','model.gguf','mmproj.gguf'):(self.root/name).touch()
        self.config=self.root/'local-models.json';self.config.write_text(json.dumps({'executable':str(self.root/'server.exe'),'models':{'glm-ocr-local':{'model':str(self.root/'model.gguf'),'mmproj':str(self.root/'mmproj.gguf')}}}),'utf8')
        self.runtime=LocalInference(self.config,self.root/'logs');self.stack=ExitStack()
        self.process=MagicMock();self.process.poll.return_value=None
        self.start=self.stack.enter_context(patch('local_inference.subprocess.Popen',return_value=self.process))
        self.stack.enter_context(patch('local_inference.own_process',return_value=lambda:None))
        self.stack.enter_context(patch('local_inference.subprocess.check_output',return_value='10000'))
        self.health=self.stack.enter_context(patch('local_inference.urllib.request.urlopen'))
        self.health.return_value.__enter__.return_value.status=200

    def tearDown(self):
        self.runtime.close();self.stack.close();self.temp.cleanup()

    def wait(self,predicate,seconds=3):
        until=time.monotonic()+seconds
        while not predicate():
            if time.monotonic()>until:self.fail('状态未在限定时间内完成')
            time.sleep(.01)

    def test_toggle_returns_during_component_prepare_and_reuses_loaded_model(self):
        entered=threading.Event();release=threading.Event()
        components=MagicMock();components.root=self.root
        components.snapshot.return_value={'status':'working','message':'正在准备 model.gguf','done':42,'total':100}
        components.ensure.side_effect=lambda _: (entered.set(),release.wait(3))
        self.runtime.components=components
        begin=time.monotonic();self.runtime.preload({'model':'glm-ocr-local'})
        self.assertLess(time.monotonic()-begin,.25);self.assertTrue(entered.wait(1))
        before=time.monotonic();state=self.runtime.snapshot()
        self.assertLess(time.monotonic()-before,.1);self.assertEqual(state['percent'],42)
        self.assertEqual(state['progress_label'],'当前文件准备进度');self.assertFalse(self.start.called)
        self.runtime.preload({'model':'glm-ocr-local'});release.set()
        self.wait(lambda:self.runtime.snapshot()['status']=='ready')
        with self.runtime.resolve({'model':'glm-ocr-local'}) as endpoint:
            self.assertEqual(endpoint['protocol'],'chat')
        self.assertEqual(self.start.call_count,1);self.assertEqual(components.ensure.call_count,1)
        self.assertNotIn('key',self.runtime.snapshot());self.assertNotIn('url',self.runtime.snapshot())

    def test_disable_during_loading_cancels_process_and_latest_enable_can_restart(self):
        entered=threading.Event();ready=threading.Event()
        def health(*_,**__):
            entered.set()
            if not ready.is_set():raise OSError('loading')
            response=MagicMock();response.__enter__.return_value.status=200;return response
        self.health.side_effect=health
        self.runtime.preload({'model':'glm-ocr-local'});self.assertTrue(entered.wait(1))
        self.assertEqual(self.runtime.snapshot()['status'],'loading');self.assertIsNone(self.runtime.snapshot()['percent'])
        self.runtime.preload(None);self.wait(lambda:self.runtime.warm_thread is None)
        self.process.terminate.assert_called_once();self.assertEqual(self.runtime.snapshot()['status'],'idle')
        ready.set();self.runtime.preload({'model':'glm-ocr-local'})
        self.wait(lambda:self.runtime.snapshot()['status']=='ready');self.assertEqual(self.start.call_count,2)

    def test_progress_snapshot_is_scoped_to_job_generation_and_reads_model_bytes(self):
        progress=OcrProgress();self.runtime._publish('preparing','glm-ocr-local','准备组件')
        components=MagicMock();components.snapshot.return_value={'done':45,'total':90,'message':'正在复制模型'}
        self.runtime.components=components
        with patch('local_inference._runtime',self.runtime):
            with progress.track('jobs','one','new') as update:
                update('loading_model',{'protocol':'local','model':'glm-ocr-local','key':'private'})
                self.assertIsNone(progress.snapshot('jobs','one','old'))
                state=progress.snapshot('jobs','one','new');self.assertEqual(state['percent'],50)
                self.assertNotIn('private',json.dumps(state));self.assertGreaterEqual(state['elapsed_seconds'],0)
                update('recognizing');self.assertIsNone(progress.snapshot('jobs','one','new')['percent'])
            self.assertIsNone(progress.snapshot('jobs','one','new'))


if __name__=='__main__':unittest.main()
