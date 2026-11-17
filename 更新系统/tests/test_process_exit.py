"""真实 Windows 文件锁/进程树与更新退出兜底；所有进程仅来自临时测试目录。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import process_control as pc
import updater as u


class ExitBoundaryTests(unittest.TestCase):
    def test_timeout_forces_once_then_checks_directory_and_port(self):
        with tempfile.TemporaryDirectory() as folder:
            session=Path(folder);processes=MagicMock();processes.__enter__.return_value=processes
            process=Mock(pid=123,path='D:/app/SHIYING 数字资产.exe')
            processes.alive.side_effect=[[process],[process],[process],[]]
            with patch.object(u,'ApplicationProcesses',return_value=processes),patch.object(u,'GRACEFUL_EXIT_TIMEOUT',0),patch.object(u.installation,'running_application',return_value=False),patch.object(u.installation,'require_port_free') as port:
                u.wait_for_application_exit(session,session,{'port':456}, {})
            processes.terminate.assert_called_once();port.assert_called_once_with(456)
            self.assertEqual(u.read_json(session/'forced-exit.json')['processes'][0]['pid'],123)

    def test_explicit_cancel_never_terminates_or_replaces(self):
        with tempfile.TemporaryDirectory() as folder:
            session=Path(folder);u.write_json(session/'cancel.json',{})
            with patch.object(u,'ApplicationProcesses') as processes,self.assertRaisesRegex(RuntimeError,'取消'):
                u.wait_for_application_exit(session,session,{}, {})
            processes.assert_not_called()

    def test_cancel_during_grace_and_force_failure_keep_files(self):
        for mode in ('cancel','denied','timeout'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                session=Path(folder);processes=MagicMock();processes.__enter__.return_value=processes
                processes.alive.return_value=[Mock(pid=123,path='app.exe')]
                def sleep(_): u.write_json(session/'cancel.json',{})
                processes.terminate.side_effect=PermissionError('denied') if mode=='denied' else None
                with patch.object(u,'ApplicationProcesses',return_value=processes),patch.object(u,'GRACEFUL_EXIT_TIMEOUT',1 if mode=='cancel' else 0),patch.object(u,'FORCED_EXIT_TIMEOUT',0),patch.object(u.time,'sleep',side_effect=sleep),patch.object(u.installation,'running_application') as running,patch.object(u.installation,'require_port_free') as port:
                    with self.assertRaises((RuntimeError,PermissionError,TimeoutError)):
                        u.wait_for_application_exit(session,session,{}, {})
                running.assert_not_called();port.assert_not_called()
                if mode=='cancel':processes.terminate.assert_not_called()

    def test_process_graph_protects_updater_and_other_installations(self):
        root=Path('D:/app');current=os.getpid();processes={}
        def open_process(pid):
            path=root/'SHIYING 数字资产.exe' if pid==101 else root/'engine/framekeep-engine.exe' if pid==102 else Path('D:/other/SHIYING 数字资产.exe') if pid==202 else Path('C:/helper.exe')
            process=Mock(pid=pid,path=os.path.normcase(os.path.abspath(path)),created=pid);process.alive.return_value=True
            processes[pid]=process;return process
        graph={90:101,101:0,102:101,103:102,current:103,105:current,106:102,202:0,203:202}
        with patch.object(pc,'process_parents',return_value=graph),patch.object(pc,'Process',side_effect=open_process):
            with pc.ApplicationProcesses(root) as tracked:
                self.assertEqual(set(tracked.processes),{101,102,106})
                tracked.terminate()
            for pid in (101,102,106):processes[pid].terminate.assert_called_once()
            # 90 的创建时间早于当前父 PID，不能把 PID 复用后的陈旧父子关系当成目标子进程。
            for pid in (90,103,105,202,203):processes[pid].terminate.assert_not_called()

    def test_refresh_captures_late_orphan_after_parent_exit(self):
        root=Path('D:/app');parent=Mock(pid=101,path=os.path.normcase(os.path.abspath(root/'SHIYING 数字资产.exe')),created=100)
        child=Mock(pid=102,path='C:/helper.exe',created=200)
        with patch.object(pc,'process_parents',side_effect=[{101:0},{102:101}]),patch.object(pc,'Process',side_effect=[parent,child]):
            with pc.ApplicationProcesses(root) as tracked:
                parent.alive.return_value=False;tracked.refresh()
                self.assertEqual(set(tracked.processes),{101,102})


@unittest.skipUnless(os.name=='nt','Windows 进程/文件锁实测')
class WindowsExitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=tempfile.TemporaryDirectory();cls.base=Path(cls.folder.name)
        source=cls.base/'host.cs';cls.host=cls.base/'host.exe'
        source.write_text('''using System;using System.IO;using System.Diagnostics;using System.Threading;
class Host {static void Main(string[] args) {
 using(var file=new FileStream(args[0],FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None)) {
 if(args.Length>2)Process.Start(new ProcessStartInfo(args[2],args[3]){UseShellExecute=false,CreateNoWindow=true});
 File.WriteAllText(args[1],Process.GetCurrentProcess().Id.ToString());Thread.Sleep(Timeout.Infinite);
 } }}''',encoding='utf8')
        subprocess.run(['C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe','/nologo','/out:'+str(cls.host),str(source)],check=True,capture_output=True)

    @classmethod
    def tearDownClass(cls):cls.folder.cleanup()

    def wait_file(self,path):
        deadline=time.monotonic()+10
        while not path.exists():
            if time.monotonic()>deadline:self.fail('隔离测试进程未就绪：'+str(path))
            time.sleep(.03)

    def launch(self,exe,lock,ready,*extra):
        exe.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(self.host,exe)
        child=subprocess.Popen([str(exe),str(lock),str(ready),*extra],creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(self.cleanup_child,child);return child

    @staticmethod
    def cleanup_child(child):
        if child.poll() is None:child.kill()
        child.wait(timeout=5)

    def test_hung_app_child_and_orphan_engine_release_locks_but_other_installation_lives(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'目标软件';root.mkdir();other=base/'另一个软件';other.mkdir()
            session=base/'session';session.mkdir();helper=base/'helper.exe';shutil.copy2(self.host,helper)
            child_lock=base/'child.lock';child_ready=base/'child.ready'
            app=self.launch(root/'SHIYING 数字资产.exe',root/'library.dll',base/'app.ready',str(helper),f'"{child_lock}" "{child_ready}"')
            engine=self.launch(root/'engine/framekeep-engine.exe',root/'engine.dll',base/'engine.ready')
            unrelated=self.launch(other/'SHIYING 数字资产.exe',other/'keep.dll',base/'other.ready')
            try:
                for name in ('app','engine','other','child'):self.wait_file(base/(name+'.ready'))
                descendant=int(child_ready.read_text());self.addCleanup(self.cleanup_pid,descendant)
                replacement=base/'new.dll';replacement.write_bytes(b'new version')
                with self.assertRaises(PermissionError):os.replace(replacement,root/'library.dll')
                started=time.monotonic()
                with patch.object(u,'GRACEFUL_EXIT_TIMEOUT',.4),patch.object(u.installation,'require_port_free'):
                    u.wait_for_application_exit(root,session,{'parentPid':app.pid,'enginePid':engine.pid}, {'releaseId':'20261114000000'})
                self.assertLess(time.monotonic()-started,10)
                self.assertFalse(u.alive(app.pid));self.assertFalse(u.alive(engine.pid));self.assertFalse(u.alive(descendant))
                self.assertIsNone(unrelated.poll());os.replace(replacement,root/'library.dll')
                self.assertEqual((root/'library.dll').read_bytes(),b'new version')
            finally:
                for process in (app,engine,unrelated):self.cleanup_child(process)
                if child_ready.exists():self.cleanup_pid(int(child_ready.read_text()))

    def test_detached_bundled_adb_releases_image_but_external_adb_lives(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'目标安装';root.mkdir();session=base/'session';session.mkdir()
            bundled=root/'engine/_internal/runtime/adb/adb.exe'
            adb=self.launch(bundled,base/'bundled.lock',base/'bundled.ready')
            sdk=self.launch(base/'Android/SDK/platform-tools/adb.exe',base/'sdk.lock',base/'sdk.ready')
            unrelated=self.launch(root/'unmanaged/adb.exe',base/'unmanaged.lock',base/'unmanaged.ready')
            try:
                for name in ('bundled','sdk','unmanaged'):self.wait_file(base/(name+'.ready'))
                replacement=base/'adb-new.exe';replacement.write_bytes(b'new adb version')
                with self.assertRaises(PermissionError):os.replace(replacement,bundled)
                # 没有桌面/引擎进程；ADB父进程为测试宿主，模拟已脱离原进程树。
                with patch.object(u,'GRACEFUL_EXIT_TIMEOUT',.1),patch.object(u.installation,'require_port_free'):
                    u.wait_for_application_exit(root,session,{}, {'releaseId':'20261116000000'})
                self.assertFalse(u.alive(adb.pid))
                self.assertIsNone(sdk.poll());self.assertIsNone(unrelated.poll())
                os.replace(replacement,bundled)
                self.assertEqual(bundled.read_bytes(),b'new adb version')
                self.assertEqual(u.read_json(session/'forced-exit.json')['processes'][0]['path'],os.path.normcase(os.path.abspath(bundled)))
            finally:
                for process in (adb,sdk,unrelated):self.cleanup_child(process)

    @staticmethod
    def cleanup_pid(pid):
        try:
            process=pc.Process(pid)
            try:process.terminate()
            finally:process.close()
        except OSError:pass

    def test_worker_survives_termination_of_its_own_app_ancestor(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'app';root.mkdir();script=base/'worker.py';result=base/'result.json';ready=base/'app.ready'
            script.write_text('''import json,sys,time\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nimport process_control as pc\nroot=Path(sys.argv[2]);ready=Path(sys.argv[3])\nwhile not ready.exists():time.sleep(.02)\nwith pc.ApplicationProcesses(root) as app:\n app.terminate()\n time.sleep(.2)\n Path(sys.argv[4]).write_text(json.dumps({'alive':not app.alive()}))\n''',encoding='utf8')
            args=subprocess.list2cmdline([str(script),str(Path(pc.__file__).parent),str(root),str(ready),str(result)])
            app=self.launch(root/'SHIYING 数字资产.exe',base/'locked',ready,sys.executable,args)
            try:
                self.wait_file(result);self.assertTrue(json.loads(result.read_text())['alive'])
                app.wait(timeout=5)
            finally:self.cleanup_child(app)


if __name__=='__main__':unittest.main()
