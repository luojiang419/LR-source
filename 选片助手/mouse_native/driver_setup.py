"""官方签名输入驱动准备；可经授权定向刷新已适配鼠标，不重启整机。"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import threading

from component_setup import digest, write_json
from .mouse_transport import SUPPORTED_DRIVER_SHA256


def installed_driver():
    return Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/drivers/mouse.sys'


def elevated_install(executable, log):
    # 使用官方安装器支持的 /install；程序只打开已适配的鼠标控制节点。
    # UAC 由系统显示，不保存管理员凭据。
    script = "$ErrorActionPreference='Stop';try{$p=Start-Process -FilePath $env:FRAMEKEEP_DRIVER_INSTALLER -ArgumentList '/install' -Verb RunAs -WindowStyle Hidden -Wait -PassThru;$p.ExitCode | Out-File -LiteralPath $env:FRAMEKEEP_DRIVER_LOG -Encoding utf8;exit $p.ExitCode}catch{exit 1223}"
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
        env={**os.environ,'FRAMEKEEP_DRIVER_INSTALLER':str(executable),'FRAMEKEEP_DRIVER_LOG':str(log)},
        capture_output=True,timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return result.returncode


class DriverSetup:
    def __init__(self, components, data, installer=elevated_install, driver_path=None, activator=None):
        self.components=components;self.data=Path(data);self.installer=installer
        self.path=Path(driver_path) if driver_path else installed_driver()
        self.lock=threading.RLock();self.thread=None
        self.activator=activator
        self.result={'status':'idle','message':'启用遥控时自动检查鼠标驱动','restart_required':False}

    def state(self):
        with self.lock:
            present=self.path.is_file()
            verified=present and digest(self.path)==SUPPORTED_DRIVER_SHA256
            return {**self.result,'installed':verified,'supported':not present or verified,
                    'message':self.result['message'] if self.result['status']!='idle' else ('鼠标驱动文件已校验，刷新设备后确认可用' if verified else '首次使用将自动安装鼠标输入驱动，Windows 可能要求授权或重启')}

    def ensure(self, activate=False):
        with self.lock:
            state=self.state()
            if state['installed'] and not (activate and self.activator):
                return state
            if not state['supported']:
                self.result={'status':'error','message':'检测到其他版本鼠标过滤驱动，请先检查兼容性；未覆盖现有驱动','restart_required':False}
                raise ValueError(self.result['message'])
            self.result={'status':'working','message':'正在准备官方鼠标驱动','restart_required':False}
        try:
            if state['installed']:
                return self._activate()
            self.components.ensure(['driver'])
            executable=self.components.root/'driver/install-interception.exe'
            with self.lock:self.result['message']='请在 Windows 授权窗口允许安装鼠标驱动，正在等待安装结果'
            code=self.installer(executable,self.data/'mouse-driver-install.log')
            if code==1223:raise ValueError('鼠标驱动安装未获授权；点击自动安装后允许 Windows 授权即可继续')
            if code not in (0,3010):raise ValueError('鼠标驱动安装未完成（代码 '+str(code)+'），请查看安装日志并重试')
            if not self.path.is_file() or digest(self.path)!=SUPPORTED_DRIVER_SHA256:raise ValueError('驱动尚未成功部署，请重试安装；未启用遥控')
            with self.lock:self.result={'status':'ready','message':'鼠标驱动已安装；请重新插入接收器，若仍未识别请重启电脑','restart_required':True}
            if self.activator:
                return self._activate()
            write_json(self.data/'mouse-driver-status.json',self.result)
            return self.state()
        except Exception as exc:
            with self.lock:self.result={'status':'error','message':str(exc),'restart_required':False}
            raise

    def _activate(self):
        with self.lock:self.result={'status':'working','message':'正在识别鼠标；如出现 Windows 授权，请允许定向刷新接收器','restart_required':False}
        result=self.activator()
        with self.lock:self.result={'status':'ready',**result}
        write_json(self.data/'mouse-driver-status.json',self.result)
        return self.state()

    def start(self, activate=False):
        with self.lock:
            if self.thread and self.thread.is_alive():return self.state()
            if self.state()['installed'] and not (activate and self.activator):return self.state()
            self.result={'status':'working','message':'正在检查鼠标驱动，请稍候','restart_required':False}
            def run():
                try:self.ensure(activate=activate)
                except Exception as exc:
                    with self.lock:self.result={'status':'error','message':str(exc),'restart_required':False}
            self.thread=threading.Thread(target=run,name='mouse-driver-install',daemon=True);self.thread.start()
            return self.state()
