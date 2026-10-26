"""末帧签名更新检查与独立更新器交接；不在引擎进程内替换应用。"""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
import sys
import time

if not getattr(sys, 'frozen', False):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'更新系统'))

from updater import TRUST, fetch_envelope, read_json, write_json, version, alive


class UpdateClient:
    def __init__(self, root, data, port):
        self.root, self.data, self.port = Path(root), Path(data), port
        self.lock = threading.RLock()
        self.session = None
        self.host = None
        self.value = {'status': 'idle', 'message': '尚未检查更新', 'available': False}
        # 旧更新器升级后也能补齐新卸载入口；便携目录不会自动注册。
        if (self.root/'update/install-layout.json').is_file():
            threading.Thread(target=self._refresh_installation,daemon=True,name='installation-integration').start()

    def _refresh_installation(self):
        import installation
        try:
            for _ in range(120):
                if not (self.root/'.update-state/pending.json').exists():
                    installation.refresh_registered_install(self.root,self.data);return
                time.sleep(.5)
        except Exception as error:
            write_json(self.data/'installation-integration-error.json',{'message':str(error)})

    def state(self):
        with self.lock:
            result = dict(self.value)
            current = read_json(self.root/'update-config.json') if (self.root/'update-config.json').exists() else TRUST
            result.update(currentVersion=current['version'], currentReleaseId=current['releaseId'])
            if self.session:
                ready_file=self.session/'ready.json'
                result['ready'] = ready_file.exists() and not (self.session/'cancel.json').exists() and alive(read_json(ready_file)['pid'])
                if (self.session/'status.json').exists():
                    result['transaction'] = read_json(self.session/'status.json')
                    if result['transaction']['status'] in ('error','rolled-back','complete'):result['ready']=False
            return result

    def check(self):
        with self.lock:
            if self.value['status'] == 'checking': return self.state()
            self.value.update(status='checking', message='正在检查更新…', available=False)
        def work():
            try:
                _, manifest = fetch_envelope()
                current = read_json(self.root/'update-config.json') if (self.root/'update-config.json').exists() else TRUST
                available = (version(manifest['version']),manifest['releaseId']) > (version(current['version']),current['releaseId'])
                with self.lock:
                    self.value.update(status='checked', available=available, version=manifest['version'],
                        releaseId=manifest['releaseId'], notes=manifest['notes'], size=manifest['package']['size'],
                        message='发现新版本' if available else '当前已是最新版本')
            except Exception as error:
                with self.lock: self.value.update(status='error', available=False, message=f'检查更新失败：{error}')
        threading.Thread(target=work,daemon=True).start()
        return self.state()

    def open(self):
        with self.lock:
            if self.host and self.host.poll() is None: return {'ok':True}
            if not self.value.get('available'): raise ValueError('请先检查是否有可用更新')
            if not os.environ.get('FRAMEKEEP_DESKTOP_PID'): raise ValueError('请从桌面版使用独立更新器')
            native=self.root/'update/末帧更新器.exe'; worker=self.root/'update/update-worker.exe'
            if not native.is_file() or not worker.is_file(): raise ValueError('独立更新组件缺失，请运行轻量安装器修复')
            self.session=self.data/'maintenance/updates'/uuid.uuid4().hex
            self.session.mkdir(parents=True)
            for source in (native,worker): shutil.copy2(source,self.session/source.name)
            write_json(self.session/'session.json',{'mode':'update','session':str(self.session),'root':str(self.root),
                'data':str(self.data),'port':self.port,'parentPid':int(os.environ['FRAMEKEEP_DESKTOP_PID']),
                'enginePid':os.getpid(),'notes':self.value.get('notes',''),'version':self.value.get('version','')})
            self.host=subprocess.Popen([str(self.session/native.name),'--session',str(self.session)],
                cwd=self.session,creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP)
            return {'ok':True}

    def cancel(self):
        if self.session: write_json(self.session/'cancel.json',{'cancelled':True})
        return {'ok':True}
