"""EOS Utility 自动登记与照片通知；不加载 Canon 代码，不访问相机。"""
from __future__ import annotations
import ctypes
from datetime import date
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import time
from xml.dom import minidom
import xml.etree.ElementTree as ET

import media_engine as media
from file_safety import atomic_write,photo_directory

LINK_FLAGS={'IsLinkedAppOther':'True','IsLinkedAppDPP':'False','IsLinkedAppNA':'False',
            'IsLinkedAppVR':'False','IsLinkedAppIBX':'False'}
LINK_VALUES={**LINK_FLAGS,'NotifyJPG':'True','NotifyCR2':'True','NotifyCR3':'True','NotifyTIF':'True','NotifyCRW':'True'}
SAVE_VALUES={'SubFolderDownload':'True','FolderCustomizeIndex':'0','FolderCustomizeSelectedIndex':'0',
             'FolderCustomizeSelectedIndex2':'0','FolderNameSeparator':'1','FolderDateOrder':'0','FolderDateFormat':'0',
             'FileCustomizeIndex':'0','FileCustomizeSelectedIndex':'0','FileCustomizeSelectedIndex2':'0'}
SAVE_REQUIRED={'SaveFolder','SubFolderDownload','FolderCustomizeIndex','FolderNameSeparator','FolderDateOrder','FolderDateFormat','FileCustomizeIndex'}
_IDENTITIES={}


def write_json(path,value):atomic_write(path,json.dumps(value,ensure_ascii=False,indent=2).encode('utf8'))


def eos_running():
    if os.name!='nt':return False
    from ctypes import wintypes as W
    class Entry(ctypes.Structure):
        _fields_=[('size',W.DWORD),('usage',W.DWORD),('pid',W.DWORD),('heap',ctypes.c_size_t),
                  ('module',W.DWORD),('threads',W.DWORD),('parent',W.DWORD),('priority',W.LONG),
                  ('flags',W.DWORD),('name',W.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype=W.HANDLE
    kernel.Process32FirstW.argtypes=[W.HANDLE,ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes=[W.HANDLE,ctypes.POINTER(Entry)];kernel.CloseHandle.argtypes=[W.HANDLE]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle==ctypes.c_void_p(-1).value:raise OSError('无法检查 EOS Utility 运行状态')
    try:
        item=Entry();item.size=ctypes.sizeof(item);more=kernel.Process32FirstW(handle,ctypes.byref(item))
        while more:
            if item.name.casefold()=='eos utility 3.exe':return True
            more=kernel.Process32NextW(handle,ctypes.byref(item))
        return False
    finally:kernel.CloseHandle(handle)


def discover_installation():
    if os.name!='nt':return None
    roots=[]
    for variable in ('ProgramFiles(x86)','ProgramFiles'):
        if os.environ.get(variable):roots.append(Path(os.environ[variable])/'Canon/EOS Utility')
    import winreg
    for view in (winreg.KEY_WOW64_32KEY,winreg.KEY_WOW64_64KEY):
        for key in (r'SOFTWARE\Canon_Inc_IC\EOS Utility',r'SOFTWARE\Canon\EOS Utility'):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,key,0,winreg.KEY_READ|view) as opened:
                    for index in range(winreg.QueryInfoKey(opened)[1]):
                        name,value,_=winreg.EnumValue(opened,index)
                        if isinstance(value,str) and any(part in name.lower() for part in ('path','dir')):
                            roots.append(Path(value.strip('"')))
            except OSError:pass
    candidates=[]
    for root in roots:
        for exe in (root/'EU3/EOS Utility 3.exe',root/'EOS Utility 3.exe'):
            if exe.is_file() and exe not in candidates:candidates.append(exe)
    found=[]
    for exe in candidates:
        stamp=(str(exe),exe.stat().st_mtime_ns)
        if stamp not in _IDENTITIES:
            helper=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'canon_probe.ps1'
            powershell=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
            result=subprocess.run([str(powershell),'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                                   '-File',str(helper),'-Executable',str(exe)],capture_output=True,timeout=10,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode:raise RuntimeError('无法读取 EOS Utility 配置位置')
            identity=json.loads(result.stdout.decode('utf-8-sig'))
            _IDENTITIES[stamp]={**identity,'config':str(Path(identity['config']).resolve()),'defaults':str(exe)+'.config'}
        found.append(_IDENTITIES[stamp])
    return max(found,key=lambda item:tuple(int(n) for n in item['version'].split('.'))) if found else None


def read_settings(path):
    if not Path(path).is_file():return {}
    content=Path(path).read_bytes()
    if len(content)>2_000_000:raise ValueError('EOS Utility 配置文件过大')
    root=ET.fromstring(content)
    return {node.get('name'):node.findtext('value') or '' for node in root.findall('./userSettings/EOSUtility.AppSettings/setting')}


def _group(document):
    root=document.documentElement
    def child(parent,name):
        return next((n for n in parent.childNodes if n.nodeType==n.ELEMENT_NODE and n.tagName==name),None)
    user=child(root,'userSettings')
    if user is None:user=document.createElement('userSettings');root.appendChild(user)
    group=child(user,'EOSUtility.AppSettings')
    if group is None:group=document.createElement('EOSUtility.AppSettings');user.appendChild(group)
    return group


def _nodes(group):
    return {node.getAttribute('name'):node for node in group.childNodes
            if node.nodeType==node.ELEMENT_NODE and node.tagName=='setting'}


def configuration_document(content,defaults):
    if content:return minidom.parseString(content)
    document=minidom.parseString(b'<?xml version="1.0" encoding="utf-8"?><configuration/>')
    installed=minidom.parse(str(defaults))
    groups=[n for n in installed.getElementsByTagName('sectionGroup') if n.getAttribute('name')=='userSettings']
    if not groups:raise ValueError('当前 EOS Utility 版本没有可登记的用户设置')
    # user.config 继承 EXE.config 的声明，不能在用户层重复声明整个配置节。
    return document


class CanonLink:
    def __init__(self,store,watch,application,*,allow_registration=True,discover=discover_installation,
                 running=eos_running,autostart=True):
        self.store=store;self.watch=watch;self.application=Path(application).resolve()
        self.allow_registration=allow_registration;self.discover=discover;self.running=running
        self.lock=threading.RLock();self.closed=threading.Event();self.wake=threading.Event()
        self.folder=store.data/'canon-link';self.preferences=self.folder/'preferences.json'
        # 历史保存目录只供手动回填；只有 EOS 已选中本软件才自动只读跟随。
        self.enabled=False;self.activated=False
        self.destination='';self.follow_eos=False;self.read_only_follow=False;self.auto_follow=True;self.awaiting_profile=False
        preference_error=''
        if self.preferences.is_file():
            try:
                saved=json.loads(self.preferences.read_text(encoding='utf8'))
                self.destination=saved.get('destination','') if isinstance(saved.get('destination',''),str) else ''
                self.auto_follow=saved.get('auto_follow',True) is True
            except (OSError,ValueError,TypeError,AttributeError):self.enabled=False;preference_error='自动接入设置读取失败，请重新启用'
        self.info={'enabled':self.enabled,'installed':False,'registered':False,'pending':False,'running':False,
                   'message':'等待 EOS Utility 关联本软件，关联后自动监控其保存目录；也可手动设置联机','error':preference_error,'save_folder':'','last_file':'','event':0}
        self.actual_folder=None;self.base_folder=None;self.manual_pause=False
        self.thread=threading.Thread(target=self._run,name='canon-link',daemon=True)
        if autostart:self.thread.start()

    def state(self):
        with self.lock:return dict(self.info,destination=self.destination,follow_eos=self.follow_eos,auto_follow=self.auto_follow,date_folder=date.today().isoformat(),keep_filename=True)

    def _persist(self):write_json(self.preferences,{'enabled':self.enabled,'destination':self.destination,'auto_follow':self.auto_follow})

    def _linked_to_us(self,settings):
        linked=settings.get('LinkedApp','').strip().strip('"')
        return bool(linked) and os.path.normpath(linked).casefold()==os.path.normpath(str(self.application)).casefold() and settings.get('IsLinkedAppOther','').casefold()=='true'

    def configure_destination(self,folder):
        if not self.allow_registration:raise ValueError('请在桌面版中设置联机保存目录')
        if not isinstance(folder,str) or not folder.strip():raise ValueError('请选择联机保存根目录')
        value=folder.strip().strip('"');root=Path(value)
        if not root.is_absolute():raise ValueError('请填写完整目录，例如 D:\\拍摄、Z:\\拍摄或 \\\\192.168.1.10\\共享\\拍摄')
        try:
            root=root.resolve(strict=True)
            if not root.is_dir():raise ValueError('请选择文件夹，不能选择文件')
            photo_directory(root,self.store.data)
            with tempfile.TemporaryFile(prefix='.framekeep-write-',dir=root) as probe:probe.write(b'framekeep');probe.flush()
        except OSError as exc:raise ValueError('保存目录不可用或无法写入，请检查共享连接、网络驱动器和写入权限') from exc
        installation=self.discover()
        if not installation:raise ValueError('未检测到 EOS Utility，请安装后进入联机拍摄')
        if not SAVE_REQUIRED<=read_settings(installation['defaults']).keys():raise ValueError('当前 EOS Utility 版本不支持所需的保存命名设置')
        with self.lock:
            self.destination=str(root);self.enabled=True;self.activated=True;self.manual_pause=False;self.actual_folder=None
            self.follow_eos=False;self.read_only_follow=False;self.auto_follow=True;self._persist()
            self._update(enabled=True,destination_pending=True,destination_ready=False,error='')
            self.tick()
        self.wake.set();return self.state()

    def _update(self,**values):
        with self.lock:self.info.update(values)

    def configure(self,enabled,follow_eos=False):
        if not isinstance(enabled,bool):raise ValueError('自动接入开关必须为布尔值')
        if not isinstance(follow_eos,bool):raise ValueError('跟随 EOS 开关必须为布尔值')
        with self.lock:
            self.enabled=enabled;self.activated=True;self.manual_pause=False
            self.auto_follow=enabled;self.read_only_follow=False
            if enabled:
                self.follow_eos=follow_eos;self.actual_folder=None;self.base_folder=None
            self._persist();self.info.update(enabled=enabled,error='')
            if not enabled and self.watch.state().get('source')=='canon':self.watch.stop()
        self.wake.set();return self.state()

    def pause(self):
        # 用户主动停止监控后，本次运行不再被自动目录检测重开；重新启用开关可恢复。
        with self.lock:self.manual_pause=True
        self._update(message='Canon 自动接入已暂停，可重新启用自动接入恢复')

    def _backup_path(self,path):return self.folder/('registration-'+hashlib.sha256(str(path).casefold().encode()).hexdigest()[:20]+'.json')

    def _register(self,installation,restore=False):
        path=Path(installation['config']);content=path.read_bytes() if path.is_file() else b''
        if len(content)>2_000_000:raise ValueError('EOS Utility 配置文件过大')
        document=configuration_document(content,installation['defaults']);group=_group(document);nodes=_nodes(group)
        supported=read_settings(installation['defaults'])
        values={key:value for key,value in {'LinkedApp':str(self.application),**LINK_VALUES}.items() if key in supported}
        if not {'LinkedApp','IsLinkedAppOther','IsLinkedAppDPP','IsLinkedAppNA'}<=values.keys():
            raise ValueError('当前 EOS Utility 版本的链接配置不受支持')
        backup_path=self._backup_path(path);backup=json.loads(backup_path.read_text(encoding='utf8')) if backup_path.is_file() else None
        if restore and not backup:return False
        if restore:values=backup['owned']
        elif self.destination and not self.follow_eos:
            if not SAVE_REQUIRED<=supported.keys():raise ValueError('当前 EOS Utility 版本不支持所需的保存命名设置')
            values.update({key:value for key,value in {'SaveFolder':self.destination,**SAVE_VALUES}.items() if key in supported})
        if restore and (read_node(nodes.get('LinkedApp')) or '').casefold()!=backup['owned'].get('LinkedApp','').casefold():
            backup_path.unlink(missing_ok=True);return True  # 用户已换用其他链接软件，整组设置均保留。
        if not backup:
            backup={'config':str(path),'original':{key:nodes[key].toxml() if key in nodes else None for key in values},'owned':values}
            write_json(backup_path,backup)
            if content:atomic_write(backup_path.with_suffix('.original.config'),content)
        elif not restore:
            # 联机保存配置可能晚于自动链接登记；恢复基线只记录首次接管时的字段。
            for key in values:
                if key not in backup['original']:backup['original'][key]=nodes[key].toxml() if key in nodes else None
            write_json(backup_path,backup)
        changed=False
        for key,value in values.items():
            node=nodes.get(key);current=read_node(node)
            if restore:
                if current!=backup['owned'].get(key):continue  # 保留用户随后作出的选择。
                if node is not None:group.removeChild(node)
                original=backup['original'].get(key)
                if original:group.appendChild(document.importNode(minidom.parseString(original).documentElement,True))
                changed=True;continue
            if current==value:continue
            if node is None:node=document.createElement('setting');node.setAttribute('name',key);node.setAttribute('serializeAs','String');group.appendChild(node)
            for old in list(node.childNodes):node.removeChild(old)
            item=document.createElement('value');item.appendChild(document.createTextNode(value));node.appendChild(item);changed=True
        if changed:
            if self.running():return None
            if (path.read_bytes() if path.is_file() else b'')!=content:raise OSError('EOS 配置正在变化，稍后重试')
            atomic_write(path,document.toprettyxml(indent='  ',encoding='utf-8'))
        if restore:backup_path.unlink(missing_ok=True)
        else:
            backup['owned']=values;write_json(backup_path,backup)
        return True

    def receive(self,paths):
        if not isinstance(paths,list) or not 1<=len(paths)<=64:raise ValueError('请选择有效的联机照片')
        files=[]
        for value in paths:
            if not isinstance(value,str) or not Path(value).is_absolute():raise ValueError('联机照片必须是完整路径')
            path=Path(value).resolve(strict=True)
            if path.suffix.lower() not in media.IMAGE_EXTS|media.RAW_EXTS or not path.is_file():raise ValueError('不支持的联机照片类型')
            files.append(path)
        with self.lock:
            # EOS 刚在内存中完成登记、首选项尚未落盘时，可信桌面照片通知也可启动跟随。
            if not self.activated and self.allow_registration and self.auto_follow and not self.manual_pause:
                watch=self.watch.state()
                if watch['enabled'] and watch.get('source')!='canon':return {'accepted':False,'reason':'正在使用手动监控'}
                self.enabled=True;self.activated=True;self.follow_eos=True;self.read_only_follow=True
                self.awaiting_profile=True
                self.info.update(enabled=True)
            if not self.enabled or self.manual_pause:return {'accepted':False,'reason':'自动接入已关闭或暂停'}
            last=files[-1];self.actual_folder=last.parent
            folder=self.base_folder if self.follow_eos and self.base_folder and last.is_relative_to(self.base_folder) else last.parent
            state=self._attach(folder)
            self.info.update(last_file=str(last),event=self.info['event']+1,error='',message='Canon 联机照片已自动接入')
            return {'accepted':True,'session_id':state['session_id'],'folder':state['folder']}

    def _attach(self,folder,recursive=True):
        state=self.watch.state()
        if not state['enabled'] or Path(state['folder']).resolve()!=Path(folder).resolve() or state.get('recursive')!=recursive:
            state=self.watch.start(str(folder),recursive,source='canon')
        elif state.get('source')!='canon':state=self.watch.set_source('canon')
        self.store.activate(state['session_id']);return state

    def tick(self):
        with self.lock:self._tick()

    def _tick(self):
        installation=None
        if not self.activated:
            if not self.allow_registration or not self.auto_follow or self.manual_pause:return
            installation=self.discover()
            if not installation:
                self._update(installed=False);return
            settings={**read_settings(installation['defaults']),**read_settings(installation['config'])}
            self._update(installed=True,running=self.running())
            if not self._linked_to_us(settings):return
            watch=self.watch.state()
            if watch['enabled'] and watch.get('source')!='canon':
                self._update(message='EOS 已关联本软件，当前保留手动现场监控；停止后可自动跟随');return
            self.enabled=True;self.activated=True;self.follow_eos=True;self.read_only_follow=True
            self._update(enabled=True,destination_pending=False,destination_ready=False)
        if not self.allow_registration:
            self._update(message='Canon 自动登记在桌面版运行时启用');return
        installation=installation or self.discover()
        if not installation:
            self._update(installed=False,registered=False,pending=False,message='未检测到 EOS Utility，安装后会自动接入');return
        running=self.running();self._update(installed=True,running=running,executable=installation['executable'],version=installation['version'])
        defaults=read_settings(installation['defaults']);settings={**defaults,**read_settings(installation['config'])}
        if self.enabled:
            if self.follow_eos:
                self._follow(installation,settings,running);return
            if self.destination:photo_directory(self.destination,self.store.data)
            if not self.application.is_file():raise ValueError('未找到可登记的末帧桌面主程序')
            registered=settings.get('LinkedApp','').casefold()==str(self.application).casefold() and all(
                settings.get(key)==value for key,value in LINK_FLAGS.items() if key in read_settings(installation['defaults']))
            desired={key:value for key,value in {'SaveFolder':self.destination,**SAVE_VALUES}.items() if key in read_settings(installation['defaults'])} if self.destination else {}
            save_ready=bool(desired) and all(settings.get(key)==value for key,value in desired.items())
            if (not registered or self.destination and not save_ready) and not running:
                with self.lock:
                    if not self.enabled:return
                    result=self._register(installation);registered=result is True
                    settings=read_settings(installation['config']);save_ready=bool(desired) and all(settings.get(key)==value for key,value in desired.items())
            self._update(registered=registered,pending=not registered,
                         message='Canon 链接已自动登记，拍摄后自动进入照片目录' if registered else 'EOS 正在运行，已自动读取保存目录；关闭 EOS 后完成链接登记',error='')
            if self.destination:
                self._update(destination_ready=save_ready,destination_pending=not save_ready,
                             message='联机保存目录已设置，保留原文件名，按 YYYY-MM-DD 日期分文件夹' if save_ready else '保存设置等待生效，请关闭 EOS Utility 后重新打开；当前拍摄仍使用原目录')
                if not save_ready:return
                if not self.manual_pause:
                    folder=Path(self.destination)
                    if self.base_folder!=folder:self.base_folder=folder;self.actual_folder=None
                    if self.actual_folder is None:self._attach(folder,True)
            base=settings.get('SaveFolder') or read_settings(installation['defaults']).get('SaveFolder')
            if base and Path(base).is_absolute():
                folder=Path(base).resolve();self._update(save_folder=str(folder))
                if running and folder.is_dir() and not self.manual_pause:
                    with self.lock:
                        if not self.enabled or self.manual_pause:return
                        if self.base_folder!=folder:
                            if self.base_folder is not None:self.actual_folder=None
                            self.base_folder=folder
                        if self.actual_folder is None:self._attach(folder,settings.get('SubFolderDownload','True')=='True')
            if self.manual_pause:self._update(message='Canon 自动接入已暂停，点击恢复自动接入继续')
        else:
            if self.follow_eos:
                self._update(registered=self._linked_to_us(settings),pending=False,message='EOS 自动跟随已关闭，可点击跟随 EOS 保存目录恢复',error='');return
            pending=self._backup_path(Path(installation['config'])).is_file()
            if pending and not running:self._register(installation,restore=True);pending=False
            self._update(registered=False,pending=pending,message='自动接入已关闭；EOS 关闭后恢复原链接设置' if pending else 'Canon 自动接入已关闭',error='')

    def _follow(self,installation,settings,running):
        registered=self._linked_to_us(settings)
        if registered:self.awaiting_profile=False
        if not registered and not self.read_only_follow and not running:
            registered=self._register(installation) is True
            settings={**read_settings(installation['defaults']),**read_settings(installation['config'])}
        # EOS 回调可能早于首选项落盘；EOS 运行中继续采用可信回调目录。
        if not registered and self.read_only_follow and not (running and self.awaiting_profile and self.actual_folder):
            if self.watch.state().get('source')=='canon':self.watch.stop()
            self.enabled=False;self.activated=False;self.base_folder=None;self.actual_folder=None
            self._update(enabled=False,registered=False,pending=False,save_folder='',message='等待 EOS Utility 重新关联本软件',error='');return
        message='正在跟随 EOS 保存目录，自动监控拍摄子文件夹'
        self._update(registered=registered,pending=not registered,error='',destination_pending=False,destination_ready=False)
        base=settings.get('SaveFolder','').strip().strip('"')
        if base and Path(base).is_absolute():
            folder=Path(base).resolve();photo_directory(folder,self.store.data)
            self._update(save_folder=str(folder))
            if self.base_folder!=folder:
                # 回调给出了 EOS 内存中的新目录时，不用旧磁盘设置覆盖。
                if self.base_folder is not None:self.actual_folder=None
                self.base_folder=folder
            if not self.manual_pause and (self.actual_folder is None or self.actual_folder.is_relative_to(folder)):
                self._attach(folder,True)
        elif self.actual_folder is None:
            self._update(save_folder='');message='EOS 已关联，等待其保存目录或首张联机照片'
        if self.manual_pause:message='Canon 自动接入已暂停，点击跟随 EOS 保存目录恢复'
        self._update(message=message)

    def _run(self):
        while not self.closed.is_set():
            self.wake.clear()
            try:self.tick()
            except Exception as exc:self._update(error=str(exc)[:220],message='Canon 自动接入暂不可用，正在重试')
            self.wake.wait(3 if self.info['installed'] else 15)

    def close(self):
        self.closed.set();self.wake.set()
        if self.thread.is_alive():self.thread.join(timeout=3)


def read_node(node):
    if node is None:return None
    values=node.getElementsByTagName('value')
    return ''.join(n.data for n in values[0].childNodes if n.nodeType in (n.TEXT_NODE,n.CDATA_SECTION_NODE)) if values else ''
