"""指定发行桌面程序后，隔离验证首次图片参数与 Windows 单实例转交。"""
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request

from PIL import Image

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name=='nt' and os.environ.get('FRAMEKEEP_TEST_DESKTOP'),'需显式指定隔离测试的桌面程序')
class CanonDesktopTests(unittest.TestCase):
    def test_first_launch_and_single_instance_deliver_photos_once(self):
        executable=Path(os.environ['FRAMEKEEP_TEST_DESKTOP']).resolve()
        out=ROOT/'build/canon-link-acceptance';out.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='native-',dir=out) as temporary:
            base=Path(temporary);photos=base/'新电脑 传入';photos.mkdir();data=base/'data'
            one=photos/'001.JPG';Image.new('RGB',(900,600),'blue').save(one)
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            env={key:value for key,value in os.environ.items() if not key.startswith('FRAMEKEEP_')}
            env.update(FRAMEKEEP_PORT=str(port),FRAMEKEEP_DATA=str(data),FRAMEKEEP_CANON_REGISTRATION='0',FRAMEKEEP_CONFIG=str(base/'no-config.md'))
            def api(path,body=None):
                url=f'http://127.0.0.1:{port}'
                headers={'Content-Type':'application/json','Origin':url}
                if body is not None:headers['X-Framekeep-Token']=api('/api/state')['request_token']
                request=urllib.request.Request(url+path,None if body is None else json.dumps(body).encode(),headers)
                with urllib.request.urlopen(request,timeout=3) as response:return json.load(response)
            def until(check,seconds=35):
                deadline=time.monotonic()+seconds
                while time.monotonic()<deadline:
                    try:
                        result=check()
                        if result:return result
                    except OSError:pass
                    if process.poll() is not None:self.fail('桌面进程提前退出，可能已有相同单实例程序运行')
                    time.sleep(.1)
                self.fail('桌面联机接入超时')
            process=subprocess.Popen([str(executable),str(one)],cwd=executable.parent,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            engine_handle=None
            try:
                health=until(lambda:api('/api/health'))
                self.assertEqual(health['application'],'framekeep')
                from ctypes import wintypes
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.CloseHandle.argtypes=[wintypes.HANDLE]
                kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
                engine_handle=kernel.OpenProcess(0x100001,False,health['process_id'])
                until(lambda:(data/'desktop-metrics.json').is_file())
                until(lambda:not list((data/'canon-link/inbox').glob('*.json')))
                self.assertFalse(api('/api/canon')['enabled']);self.assertEqual(api('/api/canon')['event'],0)
                self.assertFalse(api('/api/watch')['enabled']);self.assertEqual(len(api('/api/state')['catalog']),0)
                api('/api/canon',{'enabled':True})
                subprocess.run([str(executable),str(one)],cwd=executable.parent,env=env,check=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                until(lambda:api('/api/canon')['event']==1)
                until(lambda:len(api('/api/state')['catalog'])==1)
                first=api('/api/watch');self.assertEqual(first['source'],'canon');self.assertEqual(Path(first['folder']),photos)
                two=photos/'002.JPG';Image.new('RGB',(900,600),'green').save(two)
                # 首次窗口的异步显示需先完成，避免将启动聚焦误判为照片通知抢焦点。
                def initial_window_focused():
                    foreground_pid=wintypes.DWORD()
                    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.windll.user32.GetForegroundWindow(),ctypes.byref(foreground_pid))
                    return foreground_pid.value==process.pid
                until(initial_window_focused,seconds=8)
                foreground=ctypes.windll.user32.GetForegroundWindow()
                secondary=subprocess.run([str(executable),str(two)],cwd=executable.parent,env=env,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(secondary.returncode,0);until(lambda:api('/api/canon')['event']==2)
                until(lambda:len(api('/api/state')['catalog'])==2)
                self.assertEqual(api('/api/watch')['generation'],first['generation'])
                self.assertEqual(ctypes.windll.user32.GetForegroundWindow(),foreground,'联机通知改变了前台窗口')
                third=base/'另一天/003.JPG';third.parent.mkdir();Image.new('RGB',(900,600),'red').save(third)
                subprocess.run([str(executable),str(third)],cwd=executable.parent,env=env,check=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                until(lambda:api('/api/canon')['event']==3);until(lambda:Path(api('/api/watch')['folder'])==third.parent)
                until(lambda:api('/api/preview-cache')['images']>=6)
                self.assertFalse(list((data/'canon-link/inbox').glob('*.json')))
                self.assertEqual(json.loads((data/'desktop-metrics.json').read_text())['desktop_pid'],process.pid)
                (out/'native-result.json').write_text(json.dumps({'passed':True,'first_launch':True,'single_instance_once':True,'focus_preserved':True,'folder_switch':True,'cache':api('/api/preview-cache')},ensure_ascii=False,indent=2),encoding='utf8')
            finally:
                if process.poll() is None:process.terminate();process.wait(timeout=10)
                if engine_handle:
                    try:
                        # 接口停止监听不代表 SQLite 已释放，必须等本次隔离引擎进程退出。
                        # 若后台线程尚未结束，只终止已持有句柄的测试子进程，不匹配进程名称。
                        if kernel.WaitForSingleObject(engine_handle,8000)==258:
                            kernel.TerminateProcess(engine_handle,0)
                            self.assertEqual(kernel.WaitForSingleObject(engine_handle,5000),0,'隔离引擎未退出')
                    finally:kernel.CloseHandle(engine_handle)


if __name__=='__main__':unittest.main()
