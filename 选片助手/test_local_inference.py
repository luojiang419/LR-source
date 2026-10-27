from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from local_inference import LocalInference
from model_providers import Providers


class LocalIntegrationTests(unittest.TestCase):
    def test_cpu_image_budget_and_legacy_timeout_allow_real_photo_recognition(self):
        from unittest.mock import patch,MagicMock
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ('server.exe','model.gguf','mmproj.gguf'):(root/name).touch()
            config=root/'config.json';config.write_text(json.dumps({'executable':str(root/'server.exe'),'models':{'glm-ocr-local':{'model':str(root/'model.gguf'),'mmproj':str(root/'mmproj.gguf')}}}),'utf8')
            runtime=LocalInference(config,root/'logs');process=MagicMock();process.poll.return_value=None
            with patch('local_inference.subprocess.check_output',side_effect=FileNotFoundError),patch('local_inference.subprocess.Popen',return_value=process) as start,patch('local_inference.own_process',return_value=lambda:None),patch('local_inference.urllib.request.urlopen') as health:
                health.return_value.__enter__.return_value.status=200
                with runtime.resolve({'model':'glm-ocr-local','timeout':90}) as provider:
                    self.assertEqual(provider['timeout'],300);self.assertEqual(provider['gpu_layers'],0)
                command=start.call_args.args[0]
                self.assertEqual(command[command.index('--image-max-tokens')+1],'1024');self.assertIn('--no-mmproj-offload',command)
                runtime.close();process.terminate.assert_called_once()

    def test_builtin_switch_locks_cards_routes_locally_and_restores_external_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Providers(folder)
            state=p.save({'name':'外部视觉','category':'multimodal','protocol':'chat','url':'https://example.com/v1','model':'remote','select':True})
            remote=state['selected']['multimodal'];p.bind('asset_analysis',remote);p.bind('sku_label',remote)
            p.save({'name':'外部 OCR','category':'ocr','protocol':'chat','url':'https://example.com/v1','model':'remote-ocr','select':True})
            original=p.public();enabled=p.set_builtin(True)
            self.assertTrue(enabled['builtin_enabled']);self.assertEqual(p.selected('ocr')['model'],'glm-ocr-local')
            self.assertEqual(p.purpose('asset_analysis')['model'],'qwen2.5-vl-7b-q4');self.assertEqual(p.purpose('sku_label')['model'],'qwen2.5-vl-7b-q4');self.assertEqual(p.selected('multimodal')['model'],'qwen2.5-vl-7b-q4')
            self.assertEqual(set(enabled['selected']),{'ocr','multimodal'});self.assertEqual(enabled['bindings'],{})
            for operation in [lambda:p.select(remote),lambda:p.delete(remote),lambda:p.bind('asset_analysis',remote),lambda:p.save({'name':'attempt'}),lambda:p.models({'id':remote}),lambda:p.check_inference(p.by_id(remote))]:
                with self.assertRaisesRegex(ValueError,'内置模型'):operation()
            self.assertTrue(Providers(folder).public()['builtin_enabled'])
            restored=Providers(folder).set_builtin(False)
            self.assertEqual(restored['selected'],original['selected']);self.assertEqual(restored['bindings'],original['bindings'])
            self.assertEqual(p.data['selected'],original['selected'])
            self.assertEqual(len(restored['providers']),4)
            with self.assertRaises(ValueError):p.set_builtin('false')

    def test_cards_keep_external_selection_and_local_has_no_secret_or_endpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Providers(folder)
            p.save({'name':'外部','category':'multimodal','protocol':'chat','url':'https://example.com/v1','model':'remote','select':True})
            selected=p.public()['selected'].copy()
            p.save({'name':'内置','category':'ocr','protocol':'local','model':'glm-ocr-local','key':'must-not-persist'})
            self.assertEqual(p.public()['selected'],selected)
            local=p.public()['providers'][-1]
            self.assertFalse(local['key_set']);self.assertNotIn('must-not-persist',p.path.read_text(encoding='utf8'))
            self.assertEqual(set(p.models({'protocol':'local'})['models']),{'glm-ocr-local','qwen2.5-vl-7b-q4'})
            with self.assertRaises(ValueError):p.save({'name':'错误','category':'ocr','protocol':'local','model':'qwen2.5-vl-7b-q4'})

    def test_existing_qwen_card_is_reenabled_without_rewriting_credentials(self):
        import server
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            p=Providers(folder)
            old={'name':'原内置 Qwen','category':'multimodal','protocol':'local','model':'qwen2.5-vl-7b-q4'}
            saved=p.save(old);pid=saved['providers'][0]['id'];p.select(pid);p.bind('asset_analysis',pid)
            p=Providers(folder);original=p.path.read_bytes()
            self.assertFalse(p.public()['providers'][0]['reserved'])
            self.assertEqual(p.selected('multimodal')['id'],pid)
            self.assertEqual(p.purpose('asset_analysis')['id'],pid)
            self.assertEqual(p.path.read_bytes(),original)
            with patch.object(server,'MODELS',p):
                self.assertEqual(server.preparation_groups('/api/providers/test',{'id':pid}),['qwen2.5-vl-7b-q4'])
                self.assertEqual(server.preparation_groups('/api/analysis/batches',{}),['qwen2.5-vl-7b-q4'])
            p.set_builtin(True)
            self.assertEqual(p.selected('ocr')['model'],'glm-ocr-local')
            self.assertEqual(p.selected('multimodal')['id'],pid)
            self.assertEqual(len(p.public()['providers']),2)

    def test_model_switch_cannot_unload_during_another_request(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime=LocalInference(Path(folder)/'config.json',folder);events=[];entered=threading.Event();release=threading.Event()
            def start(model):events.append('load:'+model);runtime.endpoint={'protocol':'chat'}
            runtime._start=start
            def first():
                with runtime.resolve({'model':'qwen'}):entered.set();release.wait(3);events.append('finished:qwen')
            def second():
                entered.wait(3)
                with runtime.resolve({'model':'ocr'}):events.append('finished:ocr')
            with ThreadPoolExecutor(2) as pool:
                a=pool.submit(first);b=pool.submit(second);entered.wait(3);time.sleep(.05)
                self.assertEqual(events,['load:qwen']);release.set();a.result();b.result()
            self.assertEqual(events,['load:qwen','finished:qwen','load:ocr','finished:ocr'])
            runtime.close()

    def test_missing_runtime_does_not_connect_to_an_external_service(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime=LocalInference(Path(folder)/'missing.json',folder)
            with self.assertRaisesRegex(ValueError,'尚未部署'):
                with runtime.resolve({'model':'glm-ocr-local'}):pass
            self.assertIsNone(runtime.process);runtime.close()


if __name__=='__main__':unittest.main()
