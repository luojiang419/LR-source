"""缺少 WebView2 时安装固定版本官方离线运行时，已有环境不改动。"""
import os
import subprocess


def webview_version():
    if os.name!='nt':return ''
    import winreg
    identity=r'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    for hive in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
        for prefix in ('Software\\','Software\\WOW6432Node\\'):
            try:
                with winreg.OpenKey(hive,prefix+identity) as key:version=winreg.QueryValueEx(key,'pv')[0]
                if version and version!='0.0.0.0':return version
            except OSError:pass
    return ''


def ensure_webview(components, detect=webview_version, runner=subprocess.run):
    if detect():return {'installed':True,'existing':True}
    components.ensure(['webview'])
    components.progress('正在安装 Microsoft WebView2 界面运行时')
    result=runner([str(components.root/'webview/MicrosoftEdgeWebView2RuntimeInstallerX64.exe'),'/silent','/install'],
        capture_output=True,timeout=300,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode not in (0,3010) or not detect():raise RuntimeError('WebView2 运行环境未安装完成，请允许 Windows 安装授权后重试')
    return {'installed':True,'existing':False,'restart_required':result.returncode==3010}
