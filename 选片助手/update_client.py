"""SHIYING签名更新检查与独立更新器交接；不在引擎进程内替换应用。"""
from pathlib import Path
import hashlib
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

from updater import TRUST, fetch_envelope, read_json, write_json, version, alive, download
from component_setup import inside


def copy_worker(worker,session):
    worker=Path(worker);session=Path(session);manifest=worker.parent/'worker-manifest.json'
    if not manifest.is_file():
        if (worker.parent/'_internal').exists():raise ValueError('独立更新运行文件清单缺失，请运行新版安装器修复')
        shutil.copy2(worker,session/worker.name);return
    data=read_json(manifest)
    if data.get('schema')!=1 or not data.get('files'):raise ValueError('独立更新运行文件清单无效')
    for item in data['files']:
        relative=item['path']
        if relative!='update-worker.exe' and not relative.startswith('_internal/'):raise ValueError('独立更新运行文件路径无效')
        source=inside(worker.parent,relative);target=inside(session,relative)
        with source.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        accepted=[item,*item.get('compatible',[])]
        if not any(source.stat().st_size==entry['size'] and actual==entry['sha256'] for entry in accepted):raise ValueError('独立更新运行文件已损坏：'+relative)
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        with target.open('rb') as stream:copied=hashlib.file_digest(stream,'sha256').hexdigest()
        if copied!=actual:raise ValueError('独立更新运行文件复制校验失败：'+relative)
    shutil.copy2(manifest,session/manifest.name)


class UpdateClient:
    def __init__(self, root, data, port):
        self.root, self.data, self.port = Path(root), Path(data), port
        self.lock = threading.RLock()
        self.session = None
        self.host = None
        self.download_session = None
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
            # 经校验的独立 worker 主动登记交接；不依赖此前前端是否成功打开更新窗口。
            handoff=self.data/'maintenance/updates/handoff.json'
            if handoff.is_file():
                try:
                    value=read_json(handoff)
                    if (value.get('enginePid')==os.getpid() and
                            value.get('parentPid')==int(os.environ.get('FRAMEKEEP_DESKTOP_PID','0')) and
                            Path(value['root']).resolve()==self.root.resolve()):
                        candidate=Path(value['session']);settings=read_json(candidate/'session.json')
                        if all(settings.get(key)==value.get(key) for key in ('root','parentPid','enginePid')):
                            self.session=candidate
                except (OSError,ValueError,KeyError,TypeError):pass
            result = dict(self.value)
            result['updaterOpen'] = bool(self.host and self.host.poll() is None)
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
            if self.value['status'] in ('checking','downloading','downloaded'): return self.state()
            self.value.update(status='checking', message='正在检查更新…', available=False, downloaded=False)
        def work():
            try:
                envelope, manifest = fetch_envelope()
                current = read_json(self.root/'update-config.json') if (self.root/'update-config.json').exists() else TRUST
                available = (version(manifest['version']),manifest['releaseId']) > (version(current['version']),current['releaseId'])
                with self.lock:
                    self.value.update(status='checked', available=available, version=manifest['version'],
                        releaseId=manifest['releaseId'], notes=manifest['notes'], size=manifest['package']['size'],
                        message='发现新版本' if available else '当前已是最新版本')
                    if available:self.value.update(status='downloading',message='正在后台下载更新')
                if available:
                    session=self.data/'maintenance/updates/downloads'/(manifest['version']+'-'+manifest['releaseId'])
                    session.mkdir(parents=True,exist_ok=True)
                    write_json(session/'envelope.json',envelope)
                    download(manifest['package'],session/'package.zip',quiet=True)
                    with self.lock:
                        self.download_session=session
                        self.value.update(status='downloaded',downloaded=True,message='新版已下载完成，可立即更新')
            except Exception as error:
                with self.lock:
                    downloading=self.value['status']=='downloading'
                    self.value.update(status='error',available=False,downloaded=False,
                        message=f'{"下载更新" if downloading else "检查更新"}失败：{error}')
        threading.Thread(target=work,daemon=True).start()
        return self.state()

    def open(self):
        with self.lock:
            if self.host and self.host.poll() is None: return {'ok':True}
            if not self.value.get('downloaded') or not self.download_session: raise ValueError('更新包尚未下载完成')
            if not os.environ.get('FRAMEKEEP_DESKTOP_PID'): raise ValueError('请从桌面版使用独立更新器')
            native=self.root/'update/SHIYING 更新器.exe'; worker=self.root/'update/update-worker.exe'
            if not native.is_file():native=self.root/'update/末帧更新器.exe'
            if not native.is_file() or not worker.is_file(): raise ValueError('独立更新组件缺失，请运行轻量安装器修复')
            self.session=self.data/'maintenance/updates'/uuid.uuid4().hex
            self.session.mkdir(parents=True)
            shutil.copy2(native,self.session/native.name);copy_worker(worker,self.session)
            write_json(self.session/'session.json',{'mode':'update','session':str(self.session),'root':str(self.root),
                'data':str(self.data),'port':self.port,'parentPid':int(os.environ['FRAMEKEEP_DESKTOP_PID']),
                'enginePid':os.getpid(),'notes':self.value.get('notes',''),'version':self.value.get('version',''),
                'localEnvelope':str(self.download_session/'envelope.json'),
                'localPackage':str(self.download_session/'package.zip')})
            self.host=subprocess.Popen([str(self.session/native.name),'--session',str(self.session)],
                cwd=self.session,creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP)
            return {'ok':True}

    def cancel(self):
        if self.session: write_json(self.session/'cancel.json',{'cancelled':True})
        return {'ok':True}
