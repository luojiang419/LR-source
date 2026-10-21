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
    def test_builtin_switch_locks_cards_routes_locally_and_restores_external_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Providers(folder)
            state=p.save({'name':'外部视觉','category':'multimodal','protocol':'chat','url':'https://example.com/v1','model':'remote','select':True})
            remote=state['selected']['multimodal'];p.bind('asset_analysis',remote);p.bind('sku_label',remote)
            p.save({'name':'外部 OCR','category':'ocr','protocol':'chat','url':'https://example.com/v1','model':'remote-ocr','select':True})
            original=p.public();enabled=p.set_builtin(True)
            self.assertTrue(enabled['builtin_enabled']);self.assertEqual(p.selected('ocr')['model'],'glm-ocr-local')
            self.assertIsNone(p.purpose('asset_analysis'));self.assertIsNone(p.purpose('sku_label'));self.assertIsNone(p.selected('multimodal'))
            self.assertEqual(set(enabled['selected']),{'ocr'});self.assertEqual(enabled['bindings'],{})
            for operation in [lambda:p.select(remote),lambda:p.delete(remote),lambda:p.bind('asset_analysis',remote),lambda:p.save({'name':'attempt'}),lambda:p.models({'id':remote}),lambda:p.check_inference(p.by_id(remote))]:
                with self.assertRaisesRegex(ValueError,'内置模型'):operation()
            self.assertTrue(Providers(folder).public()['builtin_enabled'])
            restored=Providers(folder).set_builtin(False)
            self.assertEqual(restored['selected'],original['selected']);self.assertEqual(restored['bindings'],original['bindings'])
            self.assertEqual(p.data['selected'],original['selected'])
            self.assertEqual(len(restored['providers']),3)
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
            self.assertEqual(p.models({'protocol':'local'})['models'],['glm-ocr-local'])
            with self.assertRaises(ValueError):p.save({'name':'错误','category':'ocr','protocol':'local','model':'qwen2.5-vl-7b-q4'})

    def test_legacy_qwen_is_preserved_but_never_selected_prepared_or_run(self):
        from unittest.mock import patch
        import server
        with tempfile.TemporaryDirectory() as folder:
            p=Providers(folder)
            old={'name':'原内置 Qwen','category':'multimodal','protocol':'local','model':'qwen2.5-vl-7b-q4'}
            saved=p.save(old);pid=saved['providers'][0]['id'];p.data['selected']['multimodal']=pid;p.data['bindings']={'sku_label':pid,'asset_analysis':pid};p._persist()
            p=Providers(folder);original=p.path.read_bytes();self.assertTrue(p.public()['providers'][0]['reserved'])
            self.assertEqual(p.public()['selected'],{});self.assertEqual(p.public()['bindings'],{})
            self.assertIsNone(p.selected('multimodal'));self.assertIsNone(p.purpose('asset_analysis'));self.assertEqual(p.path.read_bytes(),original)
            for action in (lambda:p.select(pid),lambda:p.bind('asset_analysis',pid),lambda:p.check_inference(p.by_id(pid)),lambda:p.save({**old,'select':True})):
                with self.assertRaisesRegex(ValueError,'预留'):action()
            with patch.object(server,'MODELS',p):
                with self.assertRaisesRegex(ValueError,'预留'):server.preparation_groups('/api/providers/test',{'id':pid})
                self.assertEqual(server.preparation_groups('/api/analysis/batches',{}),[])
            p.set_builtin(True);self.assertEqual(p.selected('ocr')['model'],'glm-ocr-local');self.assertIsNone(p.selected('multimodal'))
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
