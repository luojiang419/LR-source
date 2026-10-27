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
import sqlite3
from contextlib import closing

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
    if (root/'data').is_dir() and any((root/'data').iterdir()):return root/'data'
    legacy=root/'.framekeep'
    if legacy.is_dir() and any(legacy.iterdir()):return legacy
    if integration:
        previous=integration.preferences()
        if previous.get('DataLocation'):return Path(previous['DataLocation']).absolute()
        previous_root=previous.get('InstallLocation')
        if previous_root and not same_path(previous_root,root):
            old=Path(previous_root);legacy=old/'.framekeep'
            if (old/MARKER).is_file() or (legacy.is_dir() and any(legacy.iterdir())):return data_directory(old)
    return root/'data'


def prepare_data_directory(root,source):
    """停机后迁移到安装目录；SQLite backup 包含 WAL，源资料始终保留。"""
    root=Path(root).absolute();source=Path(source).absolute();target=root/'data'
    for path in (root,source,target):
        if any(p.is_symlink() or p.is_junction() for p in (path,*path.parents)):
            raise ValueError('资料目录不能包含链接，未迁移')
    if same_path(source,target):
        target.mkdir(parents=True,exist_ok=True);return target
    if not source.exists():
        raise ValueError('原资料目录不存在，未创建空库替代：'+str(source))
    if source.is_relative_to(target) or target.is_relative_to(source):raise ValueError('新旧资料目录不能重叠')
    def source_stamp():
        return {p.relative_to(source).as_posix():[p.stat().st_size,p.stat().st_mtime_ns]
                for p in source.rglob('*') if p.is_file() and not p.name.endswith('-shm')
                and (not p.name.endswith('-wal') or p.stat().st_size>0)}
    stamp=source_stamp()
    if target.exists() and any(target.iterdir()):
        receipt=target/'.framekeep-data.json'
        if receipt.is_file():
            saved=read_json(receipt)
            if saved.get('schema')=='framekeep.data' and same_path(saved.get('source',''),source) and saved.get('sourceStamp')==stamp:
                # 上次已复制成功但程序安装回滚：沿用新目录，不能重新覆盖其启动后数据。
                return target
        raise ValueError('安装目录 data 已有资料，未覆盖；请先核对两个资料目录')
    total=sum(p.stat().st_size for p in source.rglob('*') if p.is_file())
    if shutil.disk_usage(root).free<total+64*1024**2:raise OSError('安装磁盘空间不足以迁移资料，原目录保留')
    stage=root/('.data-migration-'+uuid.uuid4().hex);stage.mkdir()
    try:
        compact={}
        database=source/'asset_archive.sqlite3'
        if database.is_file():
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as archive:
                if archive.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_jobs'").fetchone():
                    for identity,path,status,journal in archive.execute('SELECT id,path,status,journal FROM metadata_jobs'):
                        value=json.loads(journal);backup=source/'metadata-backups'/(identity+'.bin')
                        if not re.fullmatch('[0-9a-f]{32}',identity) or not backup.is_file():continue
                        if not all(k in value for k in ('before','after','before_hash','after_hash')):continue
                        if digest(backup)!=value['before_hash']:continue  # 不丢弃无法核实的恢复副本。
                        value.pop('backup',None);value.pop('prepared',None)
                        value['history']=str(target/'metadata-history'/(identity+'.json'))
                        compact[identity]={'schema':'framekeep.metadata-history','version':1,'id':identity,
                                           'source':path,'status':status,'journal':value}
        for path in source.rglob('*'):
            if path.is_symlink() or path.is_junction():raise ValueError('原资料包含链接，未迁移：'+str(path))
            dest=stage/path.relative_to(source)
            if path.is_dir():dest.mkdir(exist_ok=True);continue
            if path.parent==source/'metadata-backups' and path.suffix=='.bin' and path.stem in compact:continue
            if path.name.endswith(('-wal','-shm')) and path.with_name(path.name[:-4]).suffix in ('.sqlite3','.db'):continue
            if path.suffix in ('.sqlite3','.db'):
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as old,closing(sqlite3.connect(dest)) as new:
                    old.backup(new)
                    if new.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('迁移数据库校验失败：'+path.name)
            else:
                shutil.copy2(path,dest)
                if digest(path)!=digest(dest):raise ValueError('迁移文件校验失败：'+path.name)
        for identity,record in compact.items():write_json(stage/'metadata-history'/(identity+'.json'),record)
        if compact:
            with closing(sqlite3.connect(stage/'asset_archive.sqlite3')) as archive,archive:
                for identity,record in compact.items():archive.execute('UPDATE metadata_jobs SET journal=? WHERE id=?',(json.dumps(record['journal'],ensure_ascii=False),identity))
        # 仅重定位程序配置内指向原 data 的路径；外置缓存/索引、原照片路径保持原值。
        def relocate(value):
            if isinstance(value,dict):return {k:relocate(v) for k,v in value.items()}
            if isinstance(value,list):return [relocate(v) for v in value]
            if isinstance(value,str):
                p=Path(value)
                if p.is_absolute() and (same_path(p,source) or p.is_relative_to(source)):
                    return str(target/p.relative_to(source))
            return value
        for path in stage.rglob('*.json'):
            try:value=read_json(path)
            except (ValueError,UnicodeError):continue
            updated=relocate(value)
            if value!=updated:write_json(path,updated)
        if source_stamp()!=stamp:raise ValueError('迁移期间原资料发生变化，未切换资料目录')
        write_json(stage/'.framekeep-data.json',{'schema':'framekeep.data','version':1,'source':str(source),'path':str(target),'sourceStamp':stamp})
        if target.exists():target.rmdir()  # 仅接受空目录，不合并或覆写其他资料。
        stage.rename(target)
        return target
    except BaseException:
        # 此目录只包含本次未发布的复制品；源目录及已有目标不动。
        shutil.rmtree(stage)
        raise


class WindowsIntegration:
    """可注入测试注册项和快捷方式目录；生产只操作当前用户的 Framekeep。"""
    def __init__(self,product_key=r'Software\Framekeep',uninstall_key=r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Framekeep',desktop=None,programs=None,display_name='末帧选片',startup_key=r'Software\Microsoft\Windows\CurrentVersion\Run',startup_name='Framekeep'):
        self.product_key=product_key;self.uninstall_key=uninstall_key;self.desktop=desktop;self.programs=programs
        self.display_name=display_name
        self.startup_key=startup_key;self.startup_name=startup_name

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
        # 只移除指向本次卸载目录的启动项，保留其他安装副本及其他程序。
        startup=(self.values(self.startup_key) or {}).get(self.startup_name,('',0))[0]
        match=re.match(r'^"([^"]+)"(?:\s|$)',startup) if isinstance(startup,str) else None
        if match and same_path(match.group(1),root/'末帧选片.exe'):
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,self.startup_key,0,winreg.KEY_SET_VALUE) as key:
                try:winreg.DeleteValue(key,self.startup_name)
                except FileNotFoundError:pass
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
    # 自动刷新只维护安装身份，不能把 FRAMEKEEP_DATA 的隔离运行目录登记为用户资料。
    # 资料迁移由安装/更新事务显式处理；正常启动也须沿用已有 marker 或注册项。
    data=data_directory(root,integration=WindowsIntegration())
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
