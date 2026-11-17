"""用小文件、隔离 HTTP 和模拟授权覆盖真实部署边界。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import tarfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
import zipfile

from component_setup import ComponentManager, DEFAULT_MODEL_IDS, inside, write_json
from local_inference import LocalInference
from mouse_native.driver_setup import DriverSetup
from windows_dependencies import ensure_webview


def record(path, group, raw, **extra):
    return {'path':path,'group':group,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),**extra}


class ComponentSetupTests(unittest.TestCase):
    def test_onnx_tar_is_verified_without_llama_and_cannot_write_archive_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);archive=base/'source.tar';raw=b'verified detector'
            with tarfile.open(archive,'w') as package:
                entry=tarfile.TarInfo('../../escape/inference.onnx');entry.size=len(raw)
                package.addfile(entry,io.BytesIO(raw))
            asset=record('models/paddle/det/inference.onnx','pp-ocrv6-small-local',raw,archive='small.tar',role='detector')
            manifest={'assets':[asset],'archives':[], 'model_archives':[{'name':'small.tar','group':'pp-ocrv6-small-local','url':archive.as_uri(),'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}]}
            manager=ComponentManager(base/'data',manifest=manifest)
            def download(url,target,expected,size=None):
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read_bytes())
            with patch.object(manager,'_download',side_effect=download):manager.ensure(['pp-ocrv6-small-local'])
            self.assertEqual((manager.root/asset['path']).read_bytes(),raw)
            self.assertFalse((manager.root/'runtime').exists());self.assertFalse((base/'escape').exists())
            config=json.loads((manager.root/'local-models.json').read_text('utf8'))
            self.assertEqual(config['models']['pp-ocrv6-small-local']['backend'],'onnx')
            self.assertFalse((manager.root/'downloads/small.tar').exists())
            (manager.root/asset['path']).unlink();asset['sha256']='0'*64
            with patch.object(manager,'_download',side_effect=download),self.assertRaisesRegex(ValueError,'校验失败'):manager.ensure(['pp-ocrv6-small-local'])

    def test_default_install_only_prepares_medium_without_glm_llama_or_qwen(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);seed=base/'seed';assets=[]
            for role in ('detector','recognizer','dictionary'):
                name='models/medium/'+role;raw=role.encode();p=seed/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
                assets.append(record(name,'pp-ocrv6-medium-local',raw,role=role,bundled=True))
            for group in ('qwen2.5-vl-7b-q4','glm-ocr-local','runtime'):
                assets.append(record('unused/'+group,group,b'future',url='https://example.invalid/future'))
            manager=ComponentManager(base/'data',[seed],{'assets':assets,'archives':[]})
            with patch('urllib.request.urlopen',side_effect=AssertionError('默认安装不应请求 GLM/llama/Qwen')):
                manager.ensure([*DEFAULT_MODEL_IDS,'driver'])
            self.assertEqual(DEFAULT_MODEL_IDS,('pp-ocrv6-medium-local',))
            self.assertFalse((manager.root/'unused').exists())
            self.assertEqual(set(json.loads((manager.root/'local-models.json').read_text('utf8'))['models']),{'pp-ocrv6-medium-local'})

    def test_progress_replace_retries_transient_windows_reader_lock(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'progress.json';target.write_text('{}');replace=os.replace;calls=[]
            def busy(source,dest):
                calls.append(1)
                if len(calls)<3:raise PermissionError('reader has not closed yet')
                return replace(source,dest)
            with patch('component_setup.os.replace',side_effect=busy):write_json(target,{'status':'ready'})
            self.assertEqual(json.loads(target.read_text()),{'status':'ready'});self.assertEqual(len(calls),3)
            self.assertEqual(list(Path(folder).glob('*.tmp')),[])

    def test_complete_runtime_partial_is_promoted_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            manager=ComponentManager(folder,manifest={'assets':[],'archives':[]});target=manager.root/'runtime.zip'
            raw=b'complete archive';target.with_name('runtime.zip.part').write_bytes(raw)
            with patch('component_setup.urllib.request.urlopen',side_effect=AssertionError('不应重新下载')):
                manager._download('https://example.invalid/runtime',target,hashlib.sha256(raw).hexdigest())
            self.assertEqual(target.read_bytes(),raw)

    def fixture(self, base):
        files={'runtime/llama-server.exe':b'server','models/ocr/model.gguf':b'model','models/ocr/mmproj.gguf':b'projection'}
        seed=base/'seed'
        for name, raw in files.items():
            target=seed/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        manifest={'assets':[record(name,'runtime' if name.startswith('runtime') else 'qwen2.5-vl-7b-q4',raw,bundled=True) for name,raw in files.items()],'archives':[]}
        return seed,manifest

    def test_offline_prepare_restart_and_corrupt_file_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);seed,manifest=self.fixture(base)
            manager=ComponentManager(base/'data',[seed],manifest)
            with patch('urllib.request.urlopen',side_effect=AssertionError('离线不应联网')):
                manager.ensure(['qwen2.5-vl-7b-q4'])
                config=json.loads((manager.root/'local-models.json').read_text(encoding='utf8'))
                self.assertEqual(config['executable'],str(manager.root/'runtime/llama-server.exe'))
                reopened=ComponentManager(base/'data',[seed],manifest)
                self.assertIn('qwen2.5-vl-7b-q4',reopened.snapshot()['ready'])
                target=manager.root/'models/ocr/model.gguf';target.write_bytes(b'xxxxx')
                reopened.ensure(['qwen2.5-vl-7b-q4']);self.assertEqual(target.read_bytes(),b'model')

    def test_resume_range_and_server_ignoring_range_are_handled(self):
        raw=b'hello world'*10000;requests=[];ignore=[False]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                offset=int(self.headers.get('Range','bytes=0-').split('=')[1].split('-')[0]);requests.append(offset)
                if ignore[0]:offset=0;self.send_response(200)
                else:self.send_response(206);self.send_header('Content-Range',f'bytes {offset}-{len(raw)-1}/{len(raw)}')
                self.send_header('Content-Length',str(len(raw)-offset));self.end_headers();self.wfile.write(raw[offset:])
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                r=record('models/ocr/model.gguf','qwen2.5-vl-7b-q4',raw,url=f'http://127.0.0.1:{http.server_port}/file')
                manifest={'assets':[r],'archives':[]};manager=ComponentManager(folder,manifest=manifest)
                target=manager.root/r['path'];target.parent.mkdir(parents=True);target.with_name(target.name+'.part').write_bytes(raw[:100])
                manager.ensure(['qwen2.5-vl-7b-q4']);self.assertEqual(requests,[100]);self.assertEqual(target.read_bytes(),raw)
                target.unlink();target.with_name(target.name+'.part').write_bytes(raw[:200]);ignore[0]=True
                manager.ensure(['qwen2.5-vl-7b-q4']);self.assertEqual(target.read_bytes(),raw)
        finally:http.shutdown();http.server_close()

    def test_concurrent_requests_share_preparation_and_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);seed,manifest=self.fixture(base);manager=ComponentManager(base/'data',[seed],manifest)
            original=manager._seed;calls=[]
            def delayed(r,t):calls.append(r['path']);time.sleep(.02);return original(r,t)
            manager._seed=delayed
            with ThreadPoolExecutor(2) as pool:list(pool.map(lambda _:manager.ensure(['qwen2.5-vl-7b-q4']),range(2)))
            self.assertEqual(len(calls),3)
            manager.start(['qwen2.5-vl-7b-q4'])
            thread=manager.thread
            if thread:thread.join(3)
            self.assertEqual(manager.snapshot()['status'],'ready')

    def test_missing_files_low_space_and_bad_paths_fail_clearly(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);seed,manifest=self.fixture(base);manager=ComponentManager(base/'data',[seed],manifest)
            with patch('component_setup.shutil.disk_usage',return_value=Mock(free=0)),self.assertRaisesRegex(OSError,'空间不足'):manager.ensure(['qwen2.5-vl-7b-q4'])
            for path in ('../x','/x','C:/x','models\\x','a/../x'):
                with self.assertRaises(ValueError):inside(base,path)
            (seed/'runtime/llama-server.exe').write_bytes(b'broken')
            with self.assertRaisesRegex(ValueError,'安装组件缺失'):manager.ensure(['qwen2.5-vl-7b-q4'])

    def test_runtime_resolve_prepares_before_start_without_external_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);seed,manifest=self.fixture(base);manager=ComponentManager(base/'data',[seed],manifest)
            local=LocalInference(base/'missing.json',base/'logs',manager)
            # 真实完成自动准备后才进入进程启动；这里停止在虚拟可执行文件边界。
            with patch('local_inference.subprocess.Popen',side_effect=OSError('test process boundary')),self.assertRaisesRegex(OSError,'test process boundary'):
                local._start('qwen2.5-vl-7b-q4')
            self.assertEqual(local.config,manager.root/'local-models.json')
            self.assertIn('qwen2.5-vl-7b-q4',manager.snapshot()['ready']);local.close()


class DriverSetupTests(unittest.TestCase):
    def test_authorized_install_checks_deployed_driver_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);manager=Mock();manager.root=base;path=base/'mouse.sys';raw=b'verified'
            def install(exe,log):path.write_bytes(raw);return 0
            with patch('mouse_native.driver_setup.SUPPORTED_DRIVER_SHA256',hashlib.sha256(raw).hexdigest()):
                driver=DriverSetup(manager,base,install,path);result=driver.ensure()
                self.assertTrue(result['installed']);self.assertTrue(result['restart_required']);manager.ensure.assert_called_once_with(['driver'])
                driver.ensure();manager.ensure.assert_called_once()

    def test_declined_and_conflicting_drivers_do_not_claim_success(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);manager=Mock();manager.root=base;path=base/'mouse.sys';installer=Mock(return_value=1223)
            driver=DriverSetup(manager,base,installer,path)
            with self.assertRaisesRegex(ValueError,'未获授权'):driver.ensure()
            self.assertEqual(driver.state()['status'],'error');self.assertFalse(driver.state()['installed'])
            path.write_bytes(b'other-driver');installer.reset_mock()
            with self.assertRaisesRegex(ValueError,'其他版本'):driver.ensure()
            installer.assert_not_called();self.assertFalse(driver.state()['supported'])

    def test_webview_existing_skips_and_missing_installs_offline(self):
        manager=Mock();manager.root=Path('C:/fixture');runner=Mock(return_value=Mock(returncode=0))
        self.assertTrue(ensure_webview(manager,lambda:'155',runner)['existing']);runner.assert_not_called()
        detect=Mock(side_effect=['','155'])
        self.assertTrue(ensure_webview(manager,detect,runner)['installed']);manager.ensure.assert_called_once_with(['webview'])


if __name__=='__main__':unittest.main()
