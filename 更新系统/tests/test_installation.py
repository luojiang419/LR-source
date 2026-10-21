"""安装盘选择、资料兼容、真实 HKCU/快捷方式及受管卸载验证。"""
from pathlib import Path
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import socket

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import installation as i


class InstallPolicyTests(unittest.TestCase):
    def test_d_then_e_then_user_directory_with_space_and_permission_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            fallback=Path(folder)/'Programs/Framekeep'
            drives=[{'root':'E:\\','free':5*i.MIN_FREE},{'root':'D:\\','free':2*i.MIN_FREE}]
            self.assertEqual(i.choose_directory(drives,fallback=fallback,can_write=lambda p:True)['root'],r'D:\Program Files\Framekeep')
            drives[1]['free']=i.MIN_FREE-1
            self.assertEqual(i.choose_directory(drives,fallback=fallback,can_write=lambda p:True)['root'],r'E:\Program Files\Framekeep')
            drives[1]['free']=2*i.MIN_FREE
            self.assertEqual(i.choose_directory(drives,fallback=fallback,can_write=lambda p:str(p).startswith('E:'))['root'],r'E:\Program Files\Framekeep')
            for available in ([],[{'root':'C:\\','free':20*i.MIN_FREE}],[{'root':'D:\\','free':1}],[{'root':'D:\\','free':9*i.MIN_FREE,'type':'removable'}]):
                self.assertEqual(i.choose_directory(available,fallback=fallback,can_write=lambda p:True)['root'],str(fallback))

    def test_existing_install_reused_and_user_fallback_needs_space(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'old';root.mkdir();(root/'末帧选片.exe').write_bytes(b'old')
            self.assertEqual(i.choose_directory([],registered=str(root),can_write=lambda p:True)['root'],str(root))
            with patch.object(i.shutil,'disk_usage',return_value=shutil._ntuple_diskusage(100,99,1)),self.assertRaises(OSError):i.choose_directory([],fallback=root,can_write=lambda p:True)
            with self.assertRaises(OSError):i.choose_directory([],fallback=root,can_write=lambda p:False)

    def test_explicit_marker_and_legacy_data_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'app';root.mkdir();old=root/'.framekeep';old.mkdir();(old/'framekeep.sqlite3').write_bytes(b'ratings')
            self.assertEqual(i.data_directory(root),old)
            data=Path(folder)/'user-data'
            i.write_json(root/i.MARKER,{'product':'framekeep','root':str(root),'dataPath':str(data)})
            self.assertEqual(i.data_directory(root),data)
            self.assertEqual(i.data_directory(root,old),old)
            with patch.dict(os.environ,{'LOCALAPPDATA':folder}):
                fresh=Path(folder)/'new';self.assertEqual(i.data_directory(fresh),Path(folder)/'Framekeep/data')

    def test_installing_to_new_partition_keeps_registered_legacy_data(self):
        with tempfile.TemporaryDirectory() as folder:
            old=Path(folder)/'old';legacy=old/'.framekeep';legacy.mkdir(parents=True);(legacy/'framekeep.sqlite3').write_bytes(b'old ratings')
            class Previous:
                def preferences(self):return {'InstallLocation':str(old)}
            self.assertEqual(i.data_directory(Path(folder)/'new',integration=Previous()),legacy)

    def test_project_launcher_data_is_not_replaced_by_empty_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            project=Path(folder);(project/'启动末帧.ps1').write_text('fixture');data=project/'.framekeep';data.mkdir();(data/'framekeep.sqlite3').write_bytes(b'existing ratings')
            root=project/'dist/末帧选片';root.mkdir(parents=True)
            (root/'.framekeep').mkdir();(root/'.framekeep/legacy.log').write_text('older portable residue')
            self.assertEqual(i.data_directory(root),data)

    def test_running_portable_workbench_is_detected_before_install(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError,'便携版'):i.require_port_free(port)
        i.require_port_free(port)


class ManagedUninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'程序';self.root.mkdir();self.data=self.base/'资料'
        self.files={'末帧选片.exe':b'desktop','engine/framekeep-engine.exe':b'engine',i.UNINSTALLER:b'uninstaller','update/update-worker.exe':b'worker'}
        for name,content in self.files.items():
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content)
        self.manifest={'version':'1.0.0','releaseId':'20261021000001','files':[{'path':name,'size':len(content),'sha256':i.digest(self.root/name)} for name,content in self.files.items()]}
        self.state=i.prepare_marker(self.root,self.data,self.manifest,False);i.complete_registration(self.root,self.state)

    def test_uninstall_preserves_data_models_unknown_and_changed_files(self):
        self.data.mkdir();(self.data/'ratings.sqlite3').write_bytes(b'ratings')
        unknown=self.root/'照片.JPG';unknown.write_bytes(b'photo')
        model=self.root/'runtime/models/local.gguf';model.parent.mkdir(parents=True);model.write_bytes(b'model')
        changed=self.root/'engine/framekeep-engine.exe';changed.write_bytes(b'user-edited')
        result=i.uninstall(self.root,is_running=lambda root:False)
        self.assertEqual(result['preservedModifiedFiles'],['engine/framekeep-engine.exe'])
        self.assertEqual(changed.read_bytes(),b'user-edited');self.assertEqual(unknown.read_bytes(),b'photo');self.assertEqual(model.read_bytes(),b'model')
        self.assertEqual((self.data/'ratings.sqlite3').read_bytes(),b'ratings')
        self.assertFalse((self.root/'末帧选片.exe').exists());self.assertFalse((self.root/i.MARKER).exists())

    def test_running_application_blocks_every_delete(self):
        with self.assertRaisesRegex(RuntimeError,'正常关闭'):i.uninstall(self.root,is_running=lambda root:True)
        self.assertTrue(all((self.root/name).is_file() for name in self.files))

    def test_foreign_root_and_traversal_are_rejected_before_deleting(self):
        original=i.read_json(self.root/i.MARKER)
        for bad in ({**original,'root':str(self.base)},{**original,'product':'other'},{**original,'files':[original['files'][0],{'path':'../outside','sha256':'0'*64}]}):
            i.write_json(self.root/i.MARKER,bad)
            with self.assertRaises(ValueError):i.uninstall(self.root,is_running=lambda root:False)
            self.assertTrue((self.root/'末帧选片.exe').exists())

    def test_upgrade_remembers_old_managed_files_for_later_uninstall(self):
        extra=self.root/'engine/old-library.dll';extra.write_bytes(b'old library')
        first={**self.manifest,'files':self.manifest['files']+[{'path':'engine/old-library.dll','size':11,'sha256':i.digest(extra)}]}
        i.complete_registration(self.root,i.prepare_marker(self.root,self.data,first,False))
        i.complete_registration(self.root,i.prepare_marker(self.root,self.data,self.manifest,False))
        i.uninstall(self.root,is_running=lambda root:False);self.assertFalse(extra.exists())

    def test_junction_never_deletes_files_outside_installation(self):
        outside=self.base/'outside';outside.mkdir();(outside/'framekeep-engine.exe').write_bytes(b'engine')
        (self.root/'engine/framekeep-engine.exe').unlink();(self.root/'engine').rmdir()
        import subprocess
        subprocess.run(['cmd','/c','mklink','/J',str(self.root/'engine'),str(outside)],check=True,capture_output=True)
        try:
            with self.assertRaises(ValueError):i.uninstall(self.root,is_running=lambda root:False)
            self.assertEqual((outside/'framekeep-engine.exe').read_bytes(),b'engine');self.assertTrue((self.root/'末帧选片.exe').exists())
        finally:(self.root/'engine').rmdir()


@unittest.skipUnless(os.name=='nt','需要 Windows 用户级注册表与 Shell')
class WindowsRegistrationTests(unittest.TestCase):
    def setUp(self):
        ManagedUninstallTests.setUp(self);identity=uuid.uuid4().hex
        self.backend=i.WindowsIntegration(product_key='Software\\FramekeepInstallerTests\\'+identity,uninstall_key='Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Framekeep-Test-'+identity,desktop=self.base/'桌面',programs=self.base/'开始菜单',display_name='末帧选片安装测试 '+identity)
        self.addCleanup(self.backend.replace,self.backend.product_key,None);self.addCleanup(self.backend.replace,self.backend.uninstall_key,None)

    def test_real_registry_shortcuts_repair_and_uninstall(self):
        i.complete_registration(self.root,i.prepare_marker(self.root,self.data,self.manifest,True),self.backend)
        values=self.backend.values(self.backend.uninstall_key)
        self.assertIn('安装测试',values['DisplayName'][0]);self.assertEqual(values['DisplayVersion'][0],'1.0.0')
        self.assertEqual(values['UninstallString'][0],'"'+str(self.root/i.UNINSTALLER)+'"')
        paths=self.backend.shortcuts(self.root)
        self.assertTrue(all(p.is_file() for p,_ in paths))
        targets=json.loads(self.backend.shell("$shell=New-Object -ComObject WScript.Shell;@(foreach($item in (ConvertFrom-Json $env:FRAMEKEEP_LINKS)){($shell.CreateShortcut($item)).TargetPath})|ConvertTo-Json -Compress",{'FRAMEKEEP_LINKS':json.dumps([str(p) for p,_ in paths],ensure_ascii=False)}))
        self.assertEqual([os.path.normcase(t) for t in targets],[os.path.normcase(t) for _,t in paths])
        # 重新登记不会复制应用或覆盖用户资料；开始菜单入口可修复。
        paths[1][0].unlink();i.complete_registration(self.root,i.prepare_marker(self.root,self.data,self.manifest,True),self.backend)
        self.assertTrue(paths[1][0].exists())
        i.uninstall(self.root,self.backend,is_running=lambda root:False)
        self.assertIsNone(self.backend.values(self.backend.uninstall_key));self.assertTrue(all(not p.exists() for p,_ in paths))
        self.assertEqual(self.backend.preferences()['DataLocation'],str(self.data))

    def test_failed_registration_restores_old_values_and_shortcuts(self):
        import winreg
        self.backend.replace(self.backend.product_key,{'Keep':('old',winreg.REG_SZ)})
        before=self.backend.snapshot(self.root);register=self.backend.register
        def fail(*args):register(*args);raise OSError('injected failure')
        with patch.object(self.backend,'register',side_effect=fail),self.assertRaises(OSError):
            i.complete_registration(self.root,i.prepare_marker(self.root,self.data,self.manifest,True),self.backend)
        self.assertEqual(self.backend.values(self.backend.product_key),before['product'])
        self.assertIsNone(self.backend.values(self.backend.uninstall_key));self.assertTrue(all(not p.exists() for p,_ in self.backend.shortcuts(self.root)))

    def test_uninstall_old_copy_keeps_other_registered_install(self):
        other=self.base/'other';other.mkdir();(other/'末帧选片.exe').write_bytes(b'other')
        (other/'update').mkdir();(other/i.UNINSTALLER).write_bytes(b'uninstaller')
        self.backend.register(other,self.data,self.manifest)
        state=i.read_json(self.root/i.MARKER);state['integrated']=True;i.write_json(self.root/i.MARKER,state)
        i.uninstall(self.root,self.backend,is_running=lambda root:False)
        self.assertEqual(self.backend.preferences()['InstallLocation'],str(other))
        self.assertTrue(all(p.exists() for p,_ in self.backend.shortcuts(other)))
        # 测试快捷方式位于隔离目录；清理本测试的注册及入口。
        self.backend.unregister(other)

    def test_persisted_registration_snapshot_restores_after_interruption(self):
        i.complete_registration(self.root,i.prepare_marker(self.root,self.data,self.manifest,True),self.backend)
        before=self.backend.snapshot(self.root);session=self.base/'session';session.mkdir()
        changed={**self.manifest,'version':'1.1.0','releaseId':'20261021000002'}
        i.complete_registration(self.root,i.prepare_marker(self.root,self.base/'new-data',changed,True),self.backend,session)
        self.assertEqual(self.backend.values(self.backend.uninstall_key)['DisplayVersion'][0],'1.1.0')
        i.rollback_registration(self.root,session,self.backend)
        self.assertEqual(self.backend.values(self.backend.uninstall_key),before['uninstall'])
        self.assertEqual(self.backend.values(self.backend.product_key),before['product'])
        self.assertEqual([(p,p.read_bytes()) for p,_ in before['links']],before['links'])


if __name__=='__main__':unittest.main()
