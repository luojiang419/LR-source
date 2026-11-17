"""独立更新运行文件复制、损坏阻断及原生封装约束。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'选片助手'))
from update_client import copy_worker

spec=importlib.util.spec_from_file_location('worker_build_tools',ROOT/'更新系统/scripts/build_tools.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


class WorkerRuntimeTests(unittest.TestCase):
    def fixture(self,root):
        distribution=root/'distribution';(distribution/'_internal').mkdir(parents=True)
        (distribution/'update-worker.exe').write_bytes(b'MZfake executable')
        (distribution/'_internal/python312.dll').write_bytes(b'fake runtime')
        output=root/'output';builder.pack_worker(distribution,output)
        session=root/'session';session.mkdir()
        return output,session

    def test_bundle_stores_files_without_deflate_and_copies_verified_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            source,session=self.fixture(Path(folder))
            with zipfile.ZipFile(source/'worker-runtime.zip') as package:
                self.assertTrue(all(e.compress_type==zipfile.ZIP_STORED for e in package.infolist()))
            copy_worker(source/'update-worker.exe',session)
            manifest=json.loads((source/'worker-manifest.json').read_text('utf8'))
            for record in manifest['files']:
                self.assertEqual(hashlib.sha256((session/record['path']).read_bytes()).hexdigest(),record['sha256'])

    def test_corrupt_source_stops_handoff_without_changing_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source,session=self.fixture(Path(folder));dll=source/'_internal/python312.dll';dll.write_bytes(b'broken')
            with self.assertRaisesRegex(ValueError,'已损坏'):copy_worker(source/'update-worker.exe',session)
            self.assertEqual(dll.read_bytes(),b'broken');self.assertFalse((session/'_internal/python312.dll').exists())

    def test_path_traversal_and_missing_manifest_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            source,session=self.fixture(Path(folder));manifest=source/'worker-manifest.json'
            data=json.loads(manifest.read_text('utf8'));data['files'][0]['path']='_internal/../../escape.dll';manifest.write_text(json.dumps(data),'utf8')
            with self.assertRaises(ValueError):copy_worker(source/'update-worker.exe',session)
            self.assertFalse((Path(folder)/'escape.dll').exists())
            manifest.unlink()
            with self.assertRaisesRegex(ValueError,'清单缺失'):copy_worker(source/'update-worker.exe',session)

    def test_legacy_single_worker_remains_compatible(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);worker=root/'update-worker.exe';worker.write_bytes(b'legacy');session=root/'session';session.mkdir()
            copy_worker(worker,session);self.assertEqual((session/worker.name).read_bytes(),b'legacy')

    def test_known_baseline_runtime_is_copied_and_unknown_bytes_still_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            source,session=self.fixture(Path(folder));manifest=source/'worker-manifest.json'
            data=json.loads(manifest.read_text('utf8'));old=b'trusted prior runtime'
            row=next(r for r in data['files'] if r['path']=='_internal/python312.dll')
            row['compatible']=[{'size':len(old),'sha256':hashlib.sha256(old).hexdigest()}]
            manifest.write_text(json.dumps(data),'utf8');dll=source/'_internal/python312.dll';dll.write_bytes(old)
            copy_worker(source/'update-worker.exe',session)
            self.assertEqual((session/'_internal/python312.dll').read_bytes(),old)
            dll.write_bytes(b'unknown runtime')
            with self.assertRaisesRegex(ValueError,'已损坏'):copy_worker(source/'update-worker.exe',session)


if __name__=='__main__':unittest.main()
