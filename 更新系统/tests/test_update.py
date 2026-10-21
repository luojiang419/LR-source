"""用隔离目录与临时签名验证事务边界，不碰生产库。"""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import updater as u
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'安装目录';self.root.mkdir()
        self.session=self.base/'会话';self.session.mkdir()
        self.key=Ed25519PrivateKey.generate()
        trust=dict(u.TRUST,publicKey=self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex())
        self.trust=patch.object(u,'TRUST',trust);self.trust.start();self.addCleanup(self.trust.stop)
        self.settings={'currentVersion':'1.0.0','currentReleaseId':'20260101000000'}

    def fixture(self, extra=None, release='20261021010101'):
        files={'末帧选片.exe':b'desktop','engine/framekeep-engine.exe':b'engine',
            'update/末帧更新器.exe':b'native','update/update-worker.exe':b'worker',
            'update-config.json':json.dumps({'version':'1.0.0','releaseId':release}).encode()}
        files.update(extra or {})
        records=[]
        with zipfile.ZipFile(self.session/'package.zip','w') as archive:
            for name,data in files.items():
                archive.writestr(name,data);records.append({'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        manifest={'protocol':1,'product':'framekeep','platform':'win32-x64','version':'1.0.0',
            'releaseId':release,'minVersion':'1.0.0','package':{'size':(self.session/'package.zip').stat().st_size,
            'sha256':u.sha256(self.session/'package.zip'),'url':'https://modelscope.cn/api/v1/models/'+u.TRUST['repository']+'/repo?x=1'},'files':records}
        payload=json.dumps(manifest)
        u.write_json(self.session/'envelope.json',{'payload':payload,'signature':self.key.sign(payload.encode()).hex()})
        return manifest

    def test_valid_signed_install_preserves_data(self):
        self.fixture();manifest=u.validate(self.session,self.settings)
        data=self.root/'.framekeep/framekeep.sqlite3';data.parent.mkdir();data.write_bytes(b'ratings')
        previous=self.root/'engine/framekeep-engine.exe';previous.parent.mkdir();previous.write_bytes(b'incomplete old install')
        unrelated=self.root/'engine/user-file.txt';unrelated.write_bytes(b'keep')
        u.check_root(self.root,True)
        u.install(self.root,self.session,manifest)
        self.assertEqual(data.read_bytes(),b'ratings')
        self.assertEqual(unrelated.read_bytes(),b'keep')
        self.assertEqual(previous.read_bytes(),b'engine')
        self.assertEqual((self.session/'backup/engine/framekeep-engine.exe').read_bytes(),b'incomplete old install')
        self.assertEqual((self.root/'末帧选片.exe').read_bytes(),b'desktop')
        self.assertEqual(u.read_json(self.session/'journal.json')['status'],'awaiting-health')

    def test_write_failure_rolls_back_old_and_new_files(self):
        self.fixture();manifest=u.validate(self.session,self.settings)
        (self.root/'末帧选片.exe').write_bytes(b'old')
        with self.assertRaises(OSError):u.install(self.root,self.session,manifest,fail_after=3)
        self.assertEqual((self.root/'末帧选片.exe').read_bytes(),b'old')
        self.assertFalse((self.root/'engine/framekeep-engine.exe').exists())
        self.assertEqual(u.read_json(self.session/'journal.json')['status'],'rolled-back')

    def test_nonempty_first_install_failure_restores_overwritten_files(self):
        self.fixture();manifest=u.validate(self.session,self.settings)
        previous=self.root/'engine/framekeep-engine.exe';previous.parent.mkdir();previous.write_bytes(b'old engine')
        unrelated=self.root/'photo.jpg';unrelated.write_bytes(b'photo')
        u.check_root(self.root,True)
        with self.assertRaisesRegex(OSError,'测试注入写入失败'):u.install(self.root,self.session,manifest,fail_after=3)
        self.assertEqual(previous.read_bytes(),b'old engine')
        self.assertEqual(unrelated.read_bytes(),b'photo')
        self.assertFalse((self.root/'末帧选片.exe').exists())
        self.assertFalse((self.root/'update/末帧更新器.exe').exists())
        self.assertEqual(u.read_json(self.session/'journal.json')['status'],'rolled-back')

    def test_invalid_signature_rejected(self):
        self.fixture();e=u.read_json(self.session/'envelope.json');e['payload']+=' '
        u.write_json(self.session/'envelope.json',e)
        with self.assertRaises(Exception):u.validate(self.session,self.settings)

    def test_wrong_product_rejected(self):
        m=self.fixture();m['product']='voxcpm-studio';p=json.dumps(m)
        with self.assertRaises(ValueError):u.verify_envelope({'payload':p,'signature':self.key.sign(p.encode()).hex()})

    def test_tampered_package_rejected(self):
        self.fixture();(self.session/'package.zip').write_bytes(b'broken')
        with self.assertRaises(ValueError):u.validate(self.session,self.settings)

    def test_old_and_same_release_rejected(self):
        for release in ('20250101000000','20260101000000'):
            self.fixture(release=release)
            with self.assertRaises(ValueError):u.validate(self.session,self.settings)

    def test_same_release_repair_allowed(self):
        self.fixture(release=self.settings['currentReleaseId'])
        self.assertTrue(u.validate(self.session,dict(self.settings,repair=True)))

    def test_package_cannot_write_user_data_or_traversal(self):
        for path in ('../末帧选片.exe','/末帧选片.exe','engine/../x','engine/COn.txt',
                     '.framekeep/framekeep.sqlite3','api key.md','engine/.env','engine/a:b','engine/a.','engine/a\\b'):
            with self.subTest(path=path),self.assertRaises(ValueError):u.safe_path(path)

    def test_signed_data_path_rejected(self):
        self.fixture({'.framekeep/framekeep.sqlite3':b'bad'})
        with self.assertRaises(ValueError):u.validate(self.session,self.settings)

    def test_duplicate_case_rejected(self):
        self.fixture({'ENGINE/framekeep-engine.exe':b'evil'})
        with self.assertRaises(ValueError):u.validate(self.session,self.settings)

    def test_missing_required_component_rejected(self):
        self.fixture();e=u.read_json(self.session/'envelope.json');m=json.loads(e['payload'])
        m['files']=[r for r in m['files'] if r['path']!='update/update-worker.exe']
        p=json.dumps(m);u.write_json(self.session/'envelope.json',{'payload':p,'signature':self.key.sign(p.encode()).hex()})
        with self.assertRaises(ValueError):u.validate(self.session,self.settings)

    def test_link_destination_rejected(self):
        outside=self.base/'outside';outside.mkdir()
        import subprocess
        result=subprocess.run(['cmd','/c','mklink','/J',str(self.root/'engine'),str(outside)],capture_output=True)
        self.assertEqual(result.returncode,0)
        try:
            with self.assertRaises(ValueError):u.destination(self.root,'engine/x')
        finally:(self.root/'engine').rmdir()

    def test_download_resume_and_cache(self):
        data=b'test download'*10000;observed=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                value=self.headers.get('Range');observed.append(value)
                offset=int(value.split('=')[1].split('-')[0]) if value else 0
                self.send_response(206);self.send_header('Content-Range',f'bytes {offset}-{len(data)-1}/{len(data)}')
                self.send_header('Content-Length',str(len(data)-offset));self.end_headers();self.wfile.write(data[offset:])
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            meta={'url':f'http://127.0.0.1:{server.server_port}/x','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
            (self.session/'package.part').write_bytes(data[:100])
            u.download(meta,self.session/'package.zip');u.download(meta,self.session/'package.zip')
            self.assertEqual(observed,['bytes=100-']);self.assertEqual((self.session/'package.zip').read_bytes(),data)
        finally:server.shutdown();server.server_close()

    def test_recovery_restores_interrupted_transaction(self):
        self.fixture();m=u.validate(self.session,self.settings)
        (self.root/'末帧选片.exe').write_bytes(b'old')
        u.install(self.root,self.session,m)
        u.write_json(self.session/'session.json',{'root':str(self.root)})
        u.write_json(self.root/'.update-state/pending.json',{'session':str(self.session),'pid':0})
        u.recover_pending(self.root)
        self.assertEqual((self.root/'末帧选片.exe').read_bytes(),b'old')
        self.assertFalse((self.root/'.update-state/pending.json').exists())

    def test_recovery_restores_previous_user_data_marker(self):
        self.fixture();manifest=u.validate(self.session,self.settings)
        (self.root/'末帧选片.exe').write_bytes(b'old')
        marker=self.root/u.installation.MARKER;marker.write_bytes(b'old-data-location')
        u.write_json(self.session/'installation-marker.json',{'root':str(self.root),'existed':True})
        (self.session/'installation-marker.before').write_bytes(marker.read_bytes())
        u.install(self.root,self.session,manifest);marker.write_bytes(b'new-data-location')
        u.write_json(self.session/'session.json',{'root':str(self.root)})
        u.write_json(self.root/'.update-state/pending.json',{'session':str(self.session),'pid':0})
        u.recover_pending(self.root)
        self.assertEqual(marker.read_bytes(),b'old-data-location');self.assertEqual((self.root/'末帧选片.exe').read_bytes(),b'old')

    def test_registered_healthy_install_is_not_rolled_back_after_interruption(self):
        self.fixture();manifest=u.validate(self.session,self.settings)
        (self.root/'末帧选片.exe').write_bytes(b'old');u.install(self.root,self.session,manifest)
        state=u.installation.prepare_marker(self.root,self.base/'data',manifest,False)
        u.installation.complete_registration(self.root,state)
        u.write_json(self.session/'healthy.json',{'releaseId':manifest['releaseId'],'version':manifest['version'],'pid':123})
        u.write_json(self.session/'session.json',{'root':str(self.root)})
        u.write_json(self.root/'.update-state/pending.json',{'session':str(self.session),'pid':0})
        u.recover_pending(self.root)
        self.assertEqual((self.root/'末帧选片.exe').read_bytes(),b'desktop');self.assertEqual(u.read_json(self.session/'journal.json')['status'],'complete')


if __name__=='__main__':unittest.main()
