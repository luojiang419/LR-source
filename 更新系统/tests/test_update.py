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
import os
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

    def fixture(self, extra=None, release='20261021010101', legacy=False):
        files={'SHIYING 数字资产.exe':b'desktop','engine/framekeep-engine.exe':b'engine',
            'update/SHIYING 更新器.exe':b'native','update/update-worker.exe':b'worker',
            'update-config.json':json.dumps({'version':'1.0.0','releaseId':release}).encode()}
        files.update(extra or {})
        if legacy:
            files['末帧选片.exe']=files.pop('SHIYING 数字资产.exe')
            files['update/末帧更新器.exe']=files.pop('update/SHIYING 更新器.exe')
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

    def test_legacy_signed_package_and_install_directory_remain_supported(self):
        self.fixture(legacy=True)
        manifest=u.validate(self.session,self.settings)
        old=self.root/'末帧选片.exe';old.write_bytes(b'previous')
        u.check_root(self.root,False)
        u.install(self.root,self.session,manifest)
        self.assertEqual(old.read_bytes(),b'desktop')
        with patch.object(u.subprocess,'Popen') as launch:
            u.write_json(self.session/'session.json',{})
            u.restart(self.root,self.session,health=False)
        self.assertEqual(launch.call_args.args[0],[str(old)])

    def test_unchanged_adb_is_not_replaced_or_added_to_rollback(self):
        relative='engine/_internal/runtime/adb/adb.exe'
        payload=b'identical adb executable'
        self.fixture({relative:payload});manifest=u.validate(self.session,self.settings)
        target=self.root/relative;target.parent.mkdir(parents=True);target.write_bytes(payload)
        replace=u.replace_program_file
        def guarded_replace(source,destination):
            if destination==target:raise PermissionError('正在运行的相同ADB不允许替换')
            return replace(source,destination)
        with patch.object(u,'replace_program_file',side_effect=guarded_replace):
            u.install(self.root,self.session,manifest)
        self.assertEqual(target.read_bytes(),payload)
        self.assertFalse((self.session/'backup'/relative).exists())
        self.assertNotIn(relative,[change['path'] for change in u.read_json(self.session/'journal.json')['changes']])
        u.rollback(self.root,self.session)
        self.assertEqual(target.read_bytes(),payload)

    def test_same_size_changed_adb_is_replaced_and_can_roll_back(self):
        relative='engine/_internal/runtime/adb/adb.exe'
        self.fixture({relative:b'new adb'});manifest=u.validate(self.session,self.settings)
        target=self.root/relative;target.parent.mkdir(parents=True);target.write_bytes(b'old adb')
        u.install(self.root,self.session,manifest)
        self.assertEqual(target.read_bytes(),b'new adb')
        u.rollback(self.root,self.session)
        self.assertEqual(target.read_bytes(),b'old adb')

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
        self.assertEqual((self.root/'SHIYING 数字资产.exe').read_bytes(),b'desktop')
        self.assertEqual(u.read_json(self.session/'journal.json')['status'],'awaiting-health')

    def test_install_controller_upgrades_running_instance_preserves_data_and_completes(self):
        from unittest.mock import Mock
        manifest=self.fixture();data=self.root/'data';data.mkdir();(data/'keep.jpg').write_bytes(b'original photo')
        setup={'mode':'install','root':str(self.root),'data':str(data),'integration':False,
               'localEnvelope':str(self.session/'envelope.json'),'localPackage':str(self.session/'package.zip'),'preferLatest':True}
        u.write_json(self.session/'session.json',setup)
        active={'parentPid':321,'enginePid':654,'port':18761};child=Mock(pid=999);child.poll.return_value=None
        def stopped(root,session,settings,verified):
            self.assertEqual(settings['parentPid'],321);self.assertEqual(settings['enginePid'],654)
            self.assertEqual(settings['data'],str(data))
        def restart(root,session):
            u.write_json(session/'healthy.json',{'pid':999,'version':manifest['version'],'releaseId':manifest['releaseId']})
            return child
        with patch.object(u.installation,'active_installation',return_value=active),patch.object(u.installation,'integration_backend',return_value=None),patch.object(u,'fetch_envelope',return_value=(u.read_json(self.session/'envelope.json'),manifest)),patch.object(u,'wait_for_application_exit',side_effect=stopped),patch.object(u,'restart',side_effect=restart),patch.object(u.installation,'require_port_free') as port:
            u.run(self.session)
        port.assert_not_called();self.assertEqual((data/'keep.jpg').read_bytes(),b'original photo')
        self.assertEqual(u.read_json(self.session/'status.json')['status'],'complete')
        self.assertEqual(u.read_json(self.session/'journal.json')['status'],'complete')
        self.assertFalse((self.root/'.update-state/pending.json').exists())

    def test_handoff_waits_for_both_processes_and_checks_directory_and_port(self):
        from unittest.mock import MagicMock
        events=[];settings={'parentPid':123,'enginePid':456,'data':str(self.base/'data'),'port':43210}
        processes=MagicMock();processes.__enter__.return_value=processes
        processes.alive.side_effect=[True,False,False]
        with patch.object(u,'ApplicationProcesses',return_value=processes),patch.object(u.time,'sleep'),patch.object(u.installation,'running_application',side_effect=lambda root:events.append('directory') or False),patch.object(u.installation,'require_port_free',side_effect=lambda port:events.append(port)):
            u.wait_for_application_exit(self.root,self.session,settings,{'releaseId':'20261029000000'})
        self.assertEqual(events,['directory',43210]);processes.terminate.assert_not_called()
        self.assertEqual(u.read_json(self.base/'data/maintenance/updates/handoff.json')['enginePid'],456)
        self.assertTrue((self.session/'ready.json').is_file())

    def test_remaining_instance_or_cancel_prevents_install_even_without_parent(self):
        with patch.object(u.installation,'running_application',return_value=True),patch.object(u.installation,'require_port_free') as port:
            with self.assertRaisesRegex(RuntimeError,'仍有程序运行'):u.wait_for_application_exit(self.root,self.session,{}, {})
            port.assert_not_called()
        u.write_json(self.session/'cancel.json',{'cancelled':True})
        with patch.object(u.installation,'running_application') as running:
            with self.assertRaisesRegex(RuntimeError,'取消'):u.wait_for_application_exit(self.root,self.session,{}, {})
            running.assert_not_called()

    def test_write_failure_rolls_back_old_and_new_files(self):
        self.fixture();manifest=u.validate(self.session,self.settings)
        (self.root/'SHIYING 数字资产.exe').write_bytes(b'old')
        with self.assertRaises(OSError):u.install(self.root,self.session,manifest,fail_after=3)
        self.assertEqual((self.root/'SHIYING 数字资产.exe').read_bytes(),b'old')
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
        self.assertFalse((self.root/'SHIYING 数字资产.exe').exists())
        self.assertFalse((self.root/'update/SHIYING 更新器.exe').exists())
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
        for path in ('../SHIYING 数字资产.exe','/SHIYING 数字资产.exe','engine/../x','engine/COn.txt',
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
        (self.root/'SHIYING 数字资产.exe').write_bytes(b'old')
        u.install(self.root,self.session,m)
        u.write_json(self.session/'session.json',{'root':str(self.root)})
        u.write_json(self.root/'.update-state/pending.json',{'session':str(self.session),'pid':0})
        u.recover_pending(self.root)
        self.assertEqual((self.root/'SHIYING 数字资产.exe').read_bytes(),b'old')
        self.assertFalse((self.root/'.update-state/pending.json').exists())

    def test_recovery_recreates_removed_parent_directories(self):
        self.fixture();manifest=u.validate(self.session,self.settings)
        previous=self.root/'engine/framekeep-engine.exe';previous.parent.mkdir();previous.write_bytes(b'old engine')
        u.install(self.root,self.session,manifest)
        previous.unlink();previous.parent.rmdir()
        u.write_json(self.session/'session.json',{'root':str(self.root)})
        u.write_json(self.root/'.update-state/pending.json',{'session':str(self.session),'pid':0})
        u.recover_pending(self.root)
        self.assertEqual(previous.read_bytes(),b'old engine')
        self.assertEqual(u.read_json(self.session/'journal.json')['status'],'rolled-back')

    def test_locked_unchanged_file_does_not_break_rollback(self):
        name='runtime/exiftool/package/bin/exiftool.exe'
        self.fixture({name:b'new helper'});manifest=u.validate(self.session,self.settings)
        target=self.root/name;target.parent.mkdir(parents=True);target.write_bytes(b'old helper')
        original_replace=u.os.replace;attempts=[]
        def locked(source,destination):
            if Path(destination)==target:
                attempts.append(Path(source).name)
                if str(source).endswith('.framekeep-rollback'):raise AssertionError('未改变的文件不应再次替换')
                raise PermissionError('测试注入文件占用')
            return original_replace(source,destination)
        with patch.object(u.os,'replace',side_effect=locked),patch.object(u.time,'sleep'):
            with self.assertRaisesRegex(PermissionError,'程序文件被占用'):u.install(self.root,self.session,manifest)
        self.assertEqual(len(attempts),30)
        self.assertEqual(target.read_bytes(),b'old helper')
        self.assertEqual(u.read_json(self.session/'journal.json')['status'],'rolled-back')
        self.assertFalse((self.root/'SHIYING 数字资产.exe').exists())

    def test_transient_program_file_lock_is_retried(self):
        source=self.base/'replacement';source.write_bytes(b'new')
        target=self.root/'SHIYING 数字资产.exe';target.write_bytes(b'old')
        original=u.os.replace;calls=[]
        def transient(a,b):
            calls.append(1)
            if len(calls)<3:raise PermissionError('短暂占用')
            original(a,b)
        with patch.object(u.os,'replace',side_effect=transient),patch.object(u.time,'sleep'):
            u.replace_program_file(source,target)
        self.assertEqual(target.read_bytes(),b'new');self.assertEqual(len(calls),3)

    @unittest.skipUnless(os.name=='nt','需要 Windows 文件共享锁')
    def test_real_windows_locked_helper_rolls_back_other_files(self):
        import ctypes
        from ctypes import wintypes
        name='runtime/exiftool/package/bin/exiftool.exe'
        self.fixture({name:b'new helper'});manifest=u.validate(self.session,self.settings)
        target=self.root/name;target.parent.mkdir(parents=True);target.write_bytes(b'old helper')
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
        handle=kernel.CreateFileW(str(target),0x80000000,1,None,3,0x80,None)
        self.assertNotEqual(handle,wintypes.HANDLE(-1).value)
        try:
            with patch.object(u.time,'sleep'),self.assertRaisesRegex(PermissionError,'程序文件被占用'):
                u.install(self.root,self.session,manifest)
            self.assertEqual(target.read_bytes(),b'old helper')
            self.assertEqual(u.read_json(self.session/'journal.json')['status'],'rolled-back')
            self.assertFalse((self.root/'SHIYING 数字资产.exe').exists())
        finally:kernel.CloseHandle(handle)

    def uninstall_receipt(self,root=None,status='complete',older=False):
        home=self.base/'uninstall';folder=home/'receipt';folder.mkdir(parents=True,exist_ok=True)
        u.write_json(folder/'session.json',{'mode':'uninstall','root':str(root or self.root)})
        u.write_json(folder/'status.json',{'status':status})
        if older:os.utime(folder/'status.json',(1,1))
        return home,folder/'status.json'

    def test_legacy_successful_uninstall_retires_pending_without_resurrecting_old_app(self):
        u.write_json(self.session/'session.json',{'root':str(self.root)})
        u.write_json(self.session/'journal.json',{'status':'installing','changes':[{'path':'engine/framekeep-engine.exe','existed':True}]})
        pending=self.root/'.update-state/pending.json';u.write_json(pending,{'session':str(self.session),'pid':0})
        backup=self.session/'backup/engine/framekeep-engine.exe';backup.parent.mkdir(parents=True);backup.write_bytes(b'old')
        home,receipt=self.uninstall_receipt()
        with patch.object(u.installation,'user_data',return_value=home.parent/'data'),patch.object(u,'rollback',side_effect=AssertionError('成功卸载后不能恢复旧程序')):
            u.recover_pending(self.root)
        self.assertFalse(pending.exists());self.assertTrue(list(pending.parent.glob('pending.uninstalled-*.json')))
        self.assertFalse((self.root/'SHIYING 数字资产.exe').exists());self.assertEqual(backup.read_bytes(),b'old')

    def test_unrelated_old_failed_receipts_cannot_disable_recovery(self):
        pending=self.root/'.update-state/pending.json';u.write_json(pending,{'session':str(self.session),'pid':0})
        for kwargs in ({'root':self.base/'other'},{'status':'error'},{'older':True}):
            home,_=self.uninstall_receipt(**kwargs)
            self.assertIsNone(u.completed_uninstall_receipt(self.root,pending,home))
        home,_=self.uninstall_receipt()
        (self.root/'SHIYING 数字资产.exe').write_bytes(b'installed')
        self.assertIsNone(u.completed_uninstall_receipt(self.root,pending,home))

    def test_successful_uninstall_archives_pending_and_keeps_backups(self):
        pending=self.root/'.update-state/pending.json';u.write_json(pending,{'session':str(self.session),'pid':0})
        uninstall=self.base/'uninstall-session';uninstall.mkdir()
        with patch.object(u.installation,'uninstall',return_value={'status':'complete','dataPath':str(self.base/'data')}):
            u.run_uninstall(uninstall,{'root':str(self.root)})
        self.assertFalse(pending.exists());self.assertTrue(list(pending.parent.glob('pending.uninstalled-*.json')))
        self.assertEqual(u.read_json(uninstall/'status.json')['status'],'complete')

    def test_failed_uninstall_does_not_archive_pending(self):
        pending=self.root/'.update-state/pending.json';u.write_json(pending,{'session':str(self.session),'pid':0})
        with patch.object(u.installation,'uninstall',side_effect=RuntimeError('测试卸载失败')):
            with self.assertRaisesRegex(RuntimeError,'测试卸载失败'):u.run_uninstall(self.session,{'root':str(self.root)})
        self.assertTrue(pending.is_file())

    def test_recovery_restores_previous_user_data_marker(self):
        self.fixture();manifest=u.validate(self.session,self.settings)
        (self.root/'SHIYING 数字资产.exe').write_bytes(b'old')
        marker=self.root/u.installation.MARKER;marker.write_bytes(b'old-data-location')
        u.write_json(self.session/'installation-marker.json',{'root':str(self.root),'existed':True})
        (self.session/'installation-marker.before').write_bytes(marker.read_bytes())
        u.install(self.root,self.session,manifest);marker.write_bytes(b'new-data-location')
        u.write_json(self.session/'session.json',{'root':str(self.root)})
        u.write_json(self.root/'.update-state/pending.json',{'session':str(self.session),'pid':0})
        u.recover_pending(self.root)
        self.assertEqual(marker.read_bytes(),b'old-data-location');self.assertEqual((self.root/'SHIYING 数字资产.exe').read_bytes(),b'old')

    def test_registered_healthy_install_is_not_rolled_back_after_interruption(self):
        self.fixture();manifest=u.validate(self.session,self.settings)
        (self.root/'SHIYING 数字资产.exe').write_bytes(b'old');u.install(self.root,self.session,manifest)
        state=u.installation.prepare_marker(self.root,self.base/'data',manifest,False)
        u.installation.complete_registration(self.root,state)
        u.write_json(self.session/'healthy.json',{'releaseId':manifest['releaseId'],'version':manifest['version'],'pid':123})
        u.write_json(self.session/'session.json',{'root':str(self.root)})
        u.write_json(self.root/'.update-state/pending.json',{'session':str(self.session),'pid':0})
        u.recover_pending(self.root)
        self.assertEqual((self.root/'SHIYING 数字资产.exe').read_bytes(),b'desktop');self.assertEqual(u.read_json(self.session/'journal.json')['status'],'complete')


if __name__=='__main__':unittest.main()
