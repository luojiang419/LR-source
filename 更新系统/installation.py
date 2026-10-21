"""当前用户安装位置、Windows 应用集成及仅删除受管文件的卸载。"""
from contextlib import contextmanager
from datetime import datetime
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import uuid
import base64
import socket

PRODUCT='framekeep'
MARKER='.framekeep-install.json'
LAYOUT='update/install-layout.json'
UNINSTALLER='update/卸载末帧选片.exe'
MIN_FREE=1024**3


def read_json(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8');os.replace(temp,path)
def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def same_path(a,b):return os.path.normcase(os.path.abspath(str(a)))==os.path.normcase(os.path.abspath(str(b)))


def safe_relative(relative):
    if not isinstance(relative,str) or any(c in relative for c in ('\\',':','\x00')):raise ValueError('更新路径非法')
    parts=PurePosixPath(relative).parts
    if not parts or relative!='/'.join(parts) or any(p in ('.','..') or p.endswith(('.',' ')) or re.match(r'^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\.|$)',p,re.I) for p in parts):raise ValueError('更新路径非法：'+relative)
    allowed=relative in ('末帧选片.exe','update-config.json','完整使用说明.md','使用说明.txt') or relative.startswith(('engine/','runtime/exiftool/','runtime/everything/','FramekeepBridge.lrplugin/','update/'))
    if not allowed or any(p.lower() in ('.git','.framekeep','__pycache__','node_modules','.env') for p in parts):raise ValueError('禁止修改此路径：'+relative)
    return relative


def direct_file(root,relative):
    root=Path(root).absolute();target=root/safe_relative(relative)
    if not target.resolve().is_relative_to(root.resolve()):raise ValueError('程序文件超出安装目录')
    cursor=target
    while cursor!=root:
        if cursor.is_symlink() or cursor.is_junction():raise ValueError('程序文件不能经过链接或目录联接')
        cursor=cursor.parent
    return target


def user_programs():return Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local'))/'Programs/Framekeep'
def user_data():return Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local'))/'Framekeep/data'


def writable_directory(path):
    """在最近的已有目录测试写权限，不为每个候选磁盘遗留目录。"""
    path=Path(path)
    while not path.exists() and path.parent!=path:path=path.parent
    if not path.is_dir() or path.is_symlink() or path.is_junction():return False
    try:
        with tempfile.TemporaryFile(prefix='.framekeep-probe-',dir=path):pass
        return True
    except OSError:return False


def choose_directory(drives=None,registered='',fallback=None,required=MIN_FREE,can_write=writable_directory):
    fallback=Path(fallback or user_programs())
    if registered and Path(registered,'末帧选片.exe').is_file() and can_write(registered):
        return {'root':str(Path(registered)),'reason':'沿用已安装位置，保留原有资料','requiredBytes':required}
    if drives is None:
        drives=[]
        if os.name=='nt':
            for letter in 'DEFGHIJKLMNOPQRSTUVWXYZ':
                root=letter+':\\'
                try:
                    if ctypes.windll.kernel32.GetDriveTypeW(root)==3:drives.append({'root':root,'free':shutil.disk_usage(root).free})
                except OSError:pass
    for drive in sorted(drives,key=lambda d:str(d['root']).upper()):
        if str(drive['root'])[:1].upper() not in 'DEFGHIJKLMNOPQRSTUVWXYZ' or drive.get('type','fixed')!='fixed':continue
        target=Path(drive['root'])/'Program Files/Framekeep'
        if drive['free']>=required and can_write(target):return {'root':str(target),'reason':'优先选择空间充足且可写的非 C 分区','requiredBytes':required}
    if not can_write(fallback):raise OSError('用户安装目录不可写，请选择其他可写目录')
    parent=fallback
    while not parent.exists():parent=parent.parent
    if shutil.disk_usage(parent).free<required:raise OSError('可用磁盘空间不足，请释放空间或选择其他分区')
    return {'root':str(fallback),'reason':'没有合适的非 C 分区，使用无需提权的用户目录','requiredBytes':required}


def data_directory(root,explicit=None,integration=None):
    root=Path(root)
    if explicit:return Path(explicit).absolute()
    if (root/MARKER).is_file():
        state=read_json(root/MARKER)
        if state.get('product')==PRODUCT and same_path(state.get('root',''),root) and state.get('dataPath'):return Path(state['dataPath']).absolute()
    project=root.parent.parent
    if (project/'启动末帧.ps1').is_file() and (project/'.framekeep/framekeep.sqlite3').is_file():return project/'.framekeep'
    legacy=root/'.framekeep'
    if legacy.is_dir() and any(legacy.iterdir()):return legacy
    if integration:
        previous=integration.preferences()
        if previous.get('DataLocation'):return Path(previous['DataLocation']).absolute()
        previous_root=previous.get('InstallLocation')
        if previous_root and not same_path(previous_root,root):
            old=Path(previous_root);legacy=old/'.framekeep'
            if (old/MARKER).is_file() or (legacy.is_dir() and any(legacy.iterdir())):return data_directory(old)
    return user_data()


class WindowsIntegration:
    """可注入测试注册项和快捷方式目录；生产只操作当前用户的 Framekeep。"""
    def __init__(self,product_key=r'Software\Framekeep',uninstall_key=r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Framekeep',desktop=None,programs=None,display_name='末帧选片'):
        self.product_key=product_key;self.uninstall_key=uninstall_key;self.desktop=desktop;self.programs=programs
        self.display_name=display_name

    @staticmethod
    def shell(script,values):
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',"[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"+script],env={**os.environ,**values},capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:raise OSError('Windows 应用集成失败：'+result.stderr.decode('utf8',errors='replace')[-300:])
        return result.stdout.decode('utf-8-sig').strip()

    def folders(self):
        if self.desktop is None or self.programs is None:
            folders=json.loads(self.shell("@{desktop=[Environment]::GetFolderPath('Desktop');programs=[Environment]::GetFolderPath('Programs')}|ConvertTo-Json -Compress",{}))
            self.desktop=Path(folders['desktop']);self.programs=Path(folders['programs'])
        return Path(self.desktop),Path(self.programs)/'末帧选片'

    def values(self,name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,name,0,winreg.KEY_READ|winreg.KEY_WOW64_64KEY) as key:
                result={};index=0
                while True:
                    try:n,v,t=winreg.EnumValue(key,index);result[n]=(v,t);index+=1
                    except OSError:break
                return result
        except FileNotFoundError:return None

    def replace(self,name,values):
        import winreg
        previous=self.values(name)
        if previous is None and values is None:return
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,name,0,winreg.KEY_READ|winreg.KEY_WRITE|winreg.KEY_WOW64_64KEY) as key:
            for n in (previous or {}):
                if n not in (values or {}):winreg.DeleteValue(key,n)
            for n,(v,t) in (values or {}).items():winreg.SetValueEx(key,n,0,t,v)
            children=winreg.QueryInfoKey(key)[0]
        if values is None and not children:winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER,name,winreg.KEY_WOW64_64KEY)

    def preferences(self):return {n:v[0] for n,v in (self.values(self.product_key) or {}).items()}

    def shortcuts(self,root):
        desktop,programs=self.folders();exe=str(Path(root)/'末帧选片.exe');uninstall=str(Path(root)/UNINSTALLER)
        return [(desktop/'末帧选片.lnk',exe),(programs/'末帧选片.lnk',exe),(programs/'卸载末帧选片.lnk',uninstall)]

    def snapshot(self,root):
        return {'product':self.values(self.product_key),'uninstall':self.values(self.uninstall_key),'links':[(p,p.read_bytes() if p.exists() else None) for p,_ in self.shortcuts(root)]}

    def restore(self,snapshot):
        self.replace(self.product_key,snapshot['product']);self.replace(self.uninstall_key,snapshot['uninstall'])
        for path,content in snapshot['links']:
            if content is None:path.unlink(missing_ok=True)
            else:path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)

    def register(self,root,data,manifest):
        import winreg
        root=Path(root).absolute();links=[{'path':str(p),'target':target} for p,target in self.shortcuts(root)]
        for item in links:Path(item['path']).parent.mkdir(parents=True,exist_ok=True)
        self.shell("$shell=New-Object -ComObject WScript.Shell;foreach($item in (ConvertFrom-Json $env:FRAMEKEEP_LINKS)){$link=$shell.CreateShortcut($item.path);$link.TargetPath=$item.target;$link.WorkingDirectory=$env:FRAMEKEEP_ROOT;$link.IconLocation=$env:FRAMEKEEP_EXE+',0';$link.Description='末帧选片';$link.Save()}",{'FRAMEKEEP_LINKS':json.dumps(links,ensure_ascii=False),'FRAMEKEEP_ROOT':str(root),'FRAMEKEEP_EXE':str(root/'末帧选片.exe')})
        previous=self.values(self.uninstall_key) or {}
        date=previous.get('InstallDate',(datetime.now().strftime('%Y%m%d'),0))[0] if same_path(previous.get('InstallLocation',('',0))[0],root) else datetime.now().strftime('%Y%m%d')
        strings={'DisplayName':self.display_name,'DisplayVersion':manifest['version'],'Publisher':'末帧选片','InstallLocation':str(root),'DisplayIcon':'"'+str(root/'末帧选片.exe')+'",0','UninstallString':'"'+str(root/UNINSTALLER)+'"','InstallDate':date,'URLInfoAbout':'https://modelscope.cn/models/jiangjiang419/framekeep-updates'}
        values={k:(v,winreg.REG_SZ) for k,v in strings.items()}
        values.update({k:(v,winreg.REG_DWORD) for k,v in {'NoModify':1,'NoRepair':1,'EstimatedSize':max(1,sum(r['size'] for r in manifest['files'])//1024)}.items()})
        self.replace(self.uninstall_key,values)
        prefs=self.values(self.product_key) or {};prefs.update({'InstallLocation':(str(root),winreg.REG_SZ),'DataLocation':(str(data),winreg.REG_SZ)})
        self.replace(self.product_key,prefs)

    def unregister(self,root):
        root=Path(root).absolute();prefs=self.values(self.product_key) or {};registered=prefs.get('InstallLocation',('',0))[0]
        values=self.values(self.uninstall_key)
        if values and same_path(values.get('InstallLocation',('',0))[0],root):self.replace(self.uninstall_key,None)
        links=[{'path':str(p),'target':target} for p,target in self.shortcuts(root)]
        self.shell("$shell=New-Object -ComObject WScript.Shell;foreach($item in (ConvertFrom-Json $env:FRAMEKEEP_LINKS)){if(Test-Path -LiteralPath $item.path){$link=$shell.CreateShortcut($item.path);if($link.TargetPath -ieq $item.target){Remove-Item -LiteralPath $item.path -Force}}}",{'FRAMEKEEP_LINKS':json.dumps(links,ensure_ascii=False)})
        if same_path(registered,root):
            prefs.pop('InstallLocation',None);self.replace(self.product_key,prefs or None)
        _,programs=self.folders()
        try:programs.rmdir()
        except OSError:pass


def integration_backend():return WindowsIntegration() if os.name=='nt' else None


def install_plan():
    backend=integration_backend();registered=backend.preferences().get('InstallLocation','') if backend else ''
    return choose_directory(registered=registered)


def prepare_marker(root,data,manifest,integrated):
    root=Path(root).absolute();records=list(manifest['files']);seen=set()
    for record in records:
        relative=safe_relative(record['path'])
        if relative.lower() in seen or not re.fullmatch('[0-9a-f]{64}',record['sha256']):raise ValueError('安装文件清单无效')
        seen.add(relative.lower());direct_file(root,relative)
    if (root/MARKER).is_file():
        previous=read_json(root/MARKER)
        if previous.get('product')==PRODUCT and same_path(previous.get('root',''),root):
            for old in previous.get('files',[]):
                relative=safe_relative(old['path']);direct_file(root,relative)
                if relative.lower() not in seen and re.fullmatch('[0-9a-f]{64}',old['sha256']):records.append(old);seen.add(relative.lower())
    state={'product':PRODUCT,'schema':1,'root':str(root),'dataPath':str(Path(data).absolute()),'version':manifest['version'],'releaseId':manifest['releaseId'],'integrated':bool(integrated),'status':'installing','files':records}
    write_json(root/MARKER,state);return state


def save_registration_snapshot(root,session,backend,snapshot):
    def values(data):
        return None if data is None else {n:{'value':base64.b64encode(v).decode() if isinstance(v,bytes) else v,'binary':isinstance(v,bytes),'type':t} for n,(v,t) in data.items()}
    write_json(Path(session)/'integration-before.json',{'root':str(Path(root).absolute()),'product':values(snapshot['product']),'uninstall':values(snapshot['uninstall']),'links':[{'path':str(p),'data':base64.b64encode(content).decode() if content is not None else None} for p,content in snapshot['links']]})


def rollback_registration(root,session,backend=None):
    file=Path(session)/'integration-before.json'
    if not file.is_file():return
    saved=read_json(file);backend=backend if backend is not None else integration_backend()
    if not backend or not same_path(saved['root'],root):raise ValueError('应用注册备份与安装目录不匹配')
    expected=[p for p,_ in backend.shortcuts(root)]
    if len(expected)!=len(saved['links']) or any(not same_path(p,item['path']) for p,item in zip(expected,saved['links'])):raise ValueError('快捷方式备份位置无效')
    def values(data):return None if data is None else {n:(base64.b64decode(item['value']) if item['binary'] else item['value'],item['type']) for n,item in data.items()}
    previous=values(saved['product']) or {};current=backend.preferences().get('InstallLocation','');prior=previous.get('InstallLocation',('',0))[0]
    if current and not same_path(current,root) and (not prior or not same_path(current,prior)):return
    backend.restore({'product':values(saved['product']),'uninstall':values(saved['uninstall']),'links':[(p,base64.b64decode(item['data']) if item['data'] is not None else None) for p,item in zip(expected,saved['links'])]})


def complete_registration(root,state,backend=None,session=None):
    backend=backend if backend is not None else integration_backend()
    before=None
    if state['integrated']:
        if backend is None:raise OSError('Windows 应用注册不可用')
        if not (Path(root)/UNINSTALLER).is_file():raise OSError('缺少独立卸载程序')
        before=backend.snapshot(root)
        if session is not None:save_registration_snapshot(root,session,backend,before)
    try:
        if state['integrated']:backend.register(root,state['dataPath'],state)
        state={**state,'status':'installed'};write_json(Path(root)/MARKER,state)
    except BaseException:
        if before is not None:backend.restore(before)
        raise


def registered_here(root,backend=None):
    backend=backend if backend is not None else integration_backend()
    if backend is None:return False
    previous=backend.preferences().get('InstallLocation','')
    return bool(previous and same_path(previous,root))


def refresh_registered_install(root,data):
    root=Path(root)
    if not (root/LAYOUT).is_file() or not registered_here(root):return
    if (root/'.update-state/pending.json').exists():return
    layout=read_json(root/LAYOUT)
    if layout.get('product')!=PRODUCT:raise ValueError('安装身份无效')
    records=layout['files']+[{'path':LAYOUT,'size':(root/LAYOUT).stat().st_size,'sha256':digest(root/LAYOUT)}]
    marker=root/MARKER;before=marker.read_bytes() if marker.exists() else None
    try:
        state=prepare_marker(root,data,{**layout,'files':records},True)
        complete_registration(root,state)
    except BaseException:
        if before is None:marker.unlink(missing_ok=True)
        else:marker.write_bytes(before)
        raise


def running_application(root):
    if os.name!='nt':return False
    result=WindowsIntegration.shell("$found=Get-CimInstance Win32_Process|Where-Object {$_.ExecutablePath -and ($_.ExecutablePath -ieq $env:FRAMEKEEP_EXE -or $_.ExecutablePath -ieq $env:FRAMEKEEP_ENGINE)};if($found){'running'}",{'FRAMEKEEP_EXE':str(Path(root)/'末帧选片.exe'),'FRAMEKEEP_ENGINE':str(Path(root)/'engine/framekeep-engine.exe')})
    return result=='running'


def require_port_free(port):
    with socket.socket() as connection:
        connection.settimeout(.3)
        if connection.connect_ex(('127.0.0.1',int(port)))==0:raise RuntimeError('工作台连接仍被占用，请先保存并关闭已运行的末帧选片（含便携版）后重试；如仍失败，请检查本机 '+str(port)+' 端口。')


def uninstall(root,backend=None,is_running=running_application):
    root=Path(root).absolute();marker=root/MARKER
    if root.is_symlink() or root.is_junction() or not marker.is_file():raise ValueError('此目录没有有效的末帧安装记录')
    state=read_json(marker)
    if state.get('product')!=PRODUCT or not same_path(state.get('root',''),root) or state.get('status')!='installed':raise ValueError('安装记录与目录不匹配，未删除文件')
    if is_running(root):raise RuntimeError('请先保存任务并正常关闭末帧选片，再重试卸载')
    records=state.get('files',[])
    if not records or len(records)>20000:raise ValueError('卸载文件清单无效')
    files=[];preserved=[];seen=set()
    for record in records:
        relative=safe_relative(record['path']);path=direct_file(root,relative)
        if relative.lower() in seen or not re.fullmatch('[0-9a-f]{64}',record['sha256']):raise ValueError('卸载文件清单无效')
        seen.add(relative.lower())
        if path.is_file():
            if digest(path)==record['sha256']:files.append(path)
            else:preserved.append(relative)
    backend=backend if backend is not None else integration_backend()
    snapshot=backend.snapshot(root) if state.get('integrated') and backend else None
    try:
        if state.get('integrated'):
            if backend is None:raise OSError('Windows 应用注册不可用')
            backend.unregister(root)
        # 保留卸载入口到最后；不递归删除目录或触碰用户资料。
        files.sort(key=lambda p:p.relative_to(root).as_posix() in (UNINSTALLER,'update/update-worker.exe'))
        for path in files:path.unlink()
        marker.unlink()
    except BaseException:
        if snapshot:backend.restore(snapshot)
        raise
    directories={parent for file in files for parent in file.parents if parent==root or parent.is_relative_to(root)}
    for folder in sorted(directories,key=lambda p:len(p.parts),reverse=True):
        try:folder.rmdir()
        except OSError:pass
    return {'status':'complete','dataPath':state['dataPath'],'preservedModifiedFiles':preserved}
