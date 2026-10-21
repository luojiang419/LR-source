"""冻结组件子进程交接和失败反馈；不安装系统驱动。"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import updater as u


class ComponentWorkerTests(unittest.TestCase):
    def test_first_install_accepts_nonempty_directory_without_desktop_exe(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'app';(root/'engine/empty').mkdir(parents=True);(root/'.update-state').mkdir()
            (root/'.framekeep-update.lock').write_text('0')
            u.check_root(root,True)
            (root/'engine/user-file.txt').write_text('preserve')
            u.check_root(root,True)
            self.assertEqual((root/'engine/user-file.txt').read_text(),'preserve')
            with self.assertRaisesRegex(ValueError,'原软件目录无效'):u.check_root(root,False)

    def test_install_root_cannot_be_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'app';root.write_bytes(b'preserve')
            with self.assertRaisesRegex(ValueError,'已被文件占用'):u.check_root(root,True)
            self.assertEqual(root.read_bytes(),b'preserve')

    def test_success_passes_offline_seed_and_explicit_driver_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'app';session=base/'session';session.mkdir();(root/'update').mkdir(parents=True)
            (root/'update/component-manifest.json').write_text('{}')
            def start(command,**kwargs):
                self.assertEqual(command[1],'--prepare-components');self.assertEqual(command[3],str(base/'seed'))
                self.assertEqual(kwargs['env']['FRAMEKEEP_PREPARE_DRIVER'],'0')
                u.write_json(Path(command[4]),{'status':'ready','driver':{'installed':True}})
                return Mock(poll=Mock(return_value=0),returncode=0)
            with patch.object(u.subprocess,'Popen',side_effect=start):
                result=u.prepare_components(root,base/'data',session,{'componentSeed':str(base/'seed'),'installDriver':False})
                self.assertEqual(result['status'],'ready')

    def test_error_and_missing_capability_never_report_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'app';session=base/'session';session.mkdir();(root/'update').mkdir(parents=True)
            with self.assertRaisesRegex(ValueError,'不支持'):u.prepare_components(root,base/'data',session,{})
            (root/'update/component-manifest.json').write_text('{}')
            def start(command,**kwargs):
                u.write_json(Path(command[4]),{'status':'error','message':'模型下载尚未完成，保留断点'})
                return Mock(poll=Mock(return_value=1),returncode=1)
            with patch.object(u.subprocess,'Popen',side_effect=start),self.assertRaisesRegex(RuntimeError,'保留断点'):
                u.prepare_components(root,base/'data',session,{})


if __name__=='__main__':unittest.main()
