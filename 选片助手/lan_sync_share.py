"""当前监控目录的临时只读 SMB 共享与有界读取；沿用 Windows 登录凭据。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time


class TemporaryCaptureShare:
    def __init__(self,device,enabled=True):
        self.device=device;self.enabled=enabled and os.name=='nt';self.lock=threading.Lock();self.root='';self.name='';self.ready=False;self.error='';self.worker=None;self.closed=False;self.owned=set()
        self.retry_at=0

    def describe(self,root):
        root=str(root or '')
        with self.lock:
            if not self.enabled or self.closed or not root:return None
            if root!=self.root or not self.ready and time.monotonic()>=self.retry_at:
                if self.worker and self.worker.is_alive():return None
                self.root=root;self.ready=False;self.name='Framekeep_'+self.device[:8]+'_'+hashlib.sha256(root.casefold().encode()).hexdigest()[:8]
                self.worker=threading.Thread(target=self._prepare,args=(root,self.name),daemon=True,name='lan-smb-share');self.worker.start()
            return {'name':self.name} if self.ready else None

    def _powershell(self,script,details):
        env=os.environ.copy();env['FRAMEKEEP_CAPTURE_SHARE']=json.dumps(details,ensure_ascii=True)
        encoded=base64.b64encode(script.encode('utf-16le')).decode()
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],env=env,capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise OSError('Windows 临时只读共享未就绪（可能需要共享管理权限或 Windows 登录凭据）')

    def _prepare(self,root,name):
        try:
            self._powershell('''$ErrorActionPreference='Stop';$d=$env:FRAMEKEEP_CAPTURE_SHARE|ConvertFrom-Json
$s=Get-SmbShare -Name $d.name -ErrorAction SilentlyContinue
if($s){if($s.Path -ne $d.root -or $s.Description -ne $d.description){throw 'share owner mismatch'}}else{
New-SmbShare -Name $d.name -Path $d.root -ReadAccess ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -Description $d.description -Temporary -CachingMode None|Out-Null
}''',{'root':root,'name':name,'description':'Framekeep current capture '+self.device})
            with self.lock:self.ready=True;self.error='';self.owned.add((root,name))
        except (OSError,subprocess.SubprocessError) as exc:
            with self.lock:self.error=str(exc);self.retry_at=time.monotonic()+5

    def close(self):
        with self.lock:self.closed=True
        if self.worker and self.worker.is_alive():self.worker.join(timeout=9)
        for root,name in self.owned:
            try:self._powershell("$ErrorActionPreference='Stop';$d=$env:FRAMEKEEP_CAPTURE_SHARE|ConvertFrom-Json;$s=Get-SmbShare -Name $d.name -ErrorAction SilentlyContinue;if($s -and $s.Path -eq $d.root -and $s.Description -eq $d.description){Remove-SmbShare -Name $d.name -Force -Confirm:$false}",{'root':root,'name':name,'description':'Framekeep current capture '+self.device})
            except (OSError,subprocess.SubprocessError):pass  # 仅应用创建的临时共享；Windows 重启仍会回收。


class SharedFileReader:
    def __init__(self):self.cooldown={};self.slots=threading.BoundedSemaphore(3)

    def copy(self,host,share,relative,target,expected,cancel,on_bytes,timeout=2):
        if not re.fullmatch(r'[\w .-]{1,80}',share or ''):return False
        if not re.fullmatch(r'[0-9.]+',host or ''):return False
        parts=relative.replace(chr(92),'/').split('/')
        if not parts or any(p in ('','.','..') or ':' in p for p in parts):return False
        key=(host,share)
        if self.cooldown.get(key,0)>time.monotonic() or not self.slots.acquire(blocking=False):return False
        stopped=threading.Event();result=queue.Queue(maxsize=1);temporary=target.with_name(target.name+'.smb')
        def run():
            try:
                source=Path(chr(92)*2+host+chr(92)+share).joinpath(*parts);h=hashlib.sha256();count=0
                with source.open('rb') as src,temporary.open('xb') as dest:
                    for chunk in iter(lambda:src.read(1024*1024),b''):
                        if stopped.is_set() or cancel.is_set():raise OSError('共享读取已停止')
                        count+=len(chunk)
                        if count>expected['size']:raise OSError('共享文件已变化')
                        dest.write(chunk);h.update(chunk);on_bytes(len(chunk))
                    dest.flush();os.fsync(dest.fileno())
                if count!=expected['size'] or h.hexdigest()!=expected['sha256']:raise OSError('共享文件尚未写完或校验失败')
                if stopped.is_set() or cancel.is_set():raise OSError('共享读取已停止')
                result.put(True)
            except OSError:result.put(False)
            finally:
                if stopped.is_set():temporary.unlink(missing_ok=True)
                self.slots.release()
        threading.Thread(target=run,daemon=True,name='lan-smb-read').start()
        try:ok=result.get(timeout=timeout)
        except queue.Empty:
            stopped.set();self.cooldown[key]=time.monotonic()+30;return False
        if not ok:
            temporary.unlink(missing_ok=True);self.cooldown[key]=time.monotonic()+30;return False
        temporary.replace(target);return True
