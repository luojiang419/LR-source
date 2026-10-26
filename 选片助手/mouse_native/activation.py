"""首次安装后的定向加载：只处理唯一、已适配的鼠标实例。"""
import base64
import json
import subprocess
import time

from .profiles import receiver_profile
from .mouse_transport import SUPPORTED_DRIVER_SHA256, Win32MouseTransport


def powershell(script, timeout=25):
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                           '-EncodedCommand', base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
                          capture_output=True, timeout=timeout,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def present_mice():
    result = powershell("$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                        "@(Get-PnpDevice -Class Mouse -PresentOnly | Select-Object -ExpandProperty InstanceId) | ConvertTo-Json -Compress")
    if result.returncode:
        raise ValueError('无法查询当前鼠标设备，请检查 Windows 设备管理状态')
    raw = result.stdout.decode('utf-8-sig').strip()
    values = json.loads(raw) if raw else []
    return [values] if isinstance(values, str) else values or []


def activation_script(instance):
    # 不将硬件标识拼成命令；身份通过 Base64 JSON 传递，提升权限后再次核对。
    payload = base64.b64encode(json.dumps(instance).encode('utf-8')).decode('ascii')
    return r"""
$ErrorActionPreference='Stop'
try {
    $target = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__TARGET__')) | ConvertFrom-Json
    $driver = Join-Path $env:SystemRoot 'System32\drivers\mouse.sys'
    if ((Get-FileHash -LiteralPath $driver -Algorithm SHA256).Hash -ne '__HASH__') { exit 10 }
    $class = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e96f-e325-11ce-bfc1-08002be10318}'
    if (@($class.UpperFilters) -notcontains 'mouse') { exit 11 }
    # 仅允许 HID 鼠标，保留 MI/COL；任何拔插或身份变化均停止。
    $mice = @(Get-PnpDevice -Class Mouse -PresentOnly)
    $supported = @($mice | Where-Object {
        $parts = ($_.InstanceId.ToUpper().Split('\')[1] -replace '&REV_[0-9A-F]{4}(?=&|$)','').Split('&')
        $_.InstanceId -like 'HID\*' -and (
            ($parts -contains 'VID_367B' -and $parts -contains 'PID_1919') -or
            ($parts -contains 'VID_046D' -and $parts -contains 'PID_C534' -and $parts -contains 'MI_01' -and $parts -contains 'COL01'))
    })
    if ($supported.Count -ne 1 -or $supported[0].InstanceId -ne $target) { exit 13 }
    $servicePath = 'HKLM:\SYSTEM\CurrentControlSet\Services\mouse'
    if (Test-Path -LiteralPath $servicePath) {
        $service = Get-ItemProperty -LiteralPath $servicePath
        $image = $service.ImagePath -replace '/', '\'
        $allowed = @('System32\drivers\mouse.sys', '\SystemRoot\System32\drivers\mouse.sys',
                     ('\??\' + $driver), $driver)
        if ($service.Type -ne 1 -or $allowed -notcontains $image -or $service.Start -eq 4) { exit 12 }
    } else {
        & sc.exe create mouse type= kernel start= demand binPath= System32\drivers\mouse.sys | Out-Null
        if ($LASTEXITCODE -ne 0) { exit 12 }
    }
    $running = Get-Service -Name mouse
    if ($running.Status -ne 'Running') { Start-Service -Name mouse }
    # Windows 10 2004 起支持定向 restart-device；旧系统不退化为重启整个控制器。
    if ([Environment]::OSVersion.Version.Build -lt 19041) { exit 15 }
    & "$env:SystemRoot\System32\pnputil.exe" /restart-device $target | Out-Null
    if ($LASTEXITCODE -eq 3010) { exit 3010 }
    if ($LASTEXITCODE -ne 0) { exit 16 }
    exit 0
} catch { exit 17 }
""".replace('__TARGET__', payload).replace('__HASH__', SUPPORTED_DRIVER_SHA256)


def elevate(instance):
    encoded = base64.b64encode(activation_script(instance).encode('utf-16-le')).decode('ascii')
    # 不经过 cmd，不传 /reboot，不关闭签名或系统防护。
    script = ("$ErrorActionPreference='Stop';try{$p=Start-Process -FilePath "
              "(Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe') "
              "-ArgumentList @('-NoProfile','-NonInteractive','-EncodedCommand','" + encoded + "') "
              "-Verb RunAs -WindowStyle Hidden -Wait -PassThru;exit $p.ExitCode}"
              "catch{if($_.Exception.GetBaseException().NativeErrorCode -eq 1223){exit 1223};exit 17}")
    return powershell(script, timeout=180).returncode


def activate(*, discover=present_mice, runner=elevate, transport_factory=Win32MouseTransport,
             pause=time.sleep):
    """调用方必须持有接收器进程锁，且没有活动遥控和打开的输入句柄。"""
    def probe():
        transport = transport_factory()
        devices = transport.enumerate()
        errors = getattr(transport, 'enumeration_errors', [])
        if not devices and any(item['code'] in (5, 32) for item in errors):
            raise ValueError('鼠标控制节点访问被拒绝或被占用，请退出其他接管工具后刷新')
        return devices

    if probe():
        return {'usable': True, 'restart_required': False, 'message': '鼠标已识别，无需重新加载驱动'}
    targets = [identity for identity in discover() if receiver_profile(identity)]
    if len(targets) != 1:
        return {'usable': False, 'restart_required': False, 'message':
                '未检测到已适配接收器，请插入后刷新' if not targets else '检测到多个已适配接收器，请仅保留要启用的一只后刷新'}
    code = runner(targets[0])
    if code == 1223:
        raise ValueError('鼠标免重启识别未获 Windows 授权；允许授权后可再次刷新')
    # 成功请求和“需重启”都重新观察，不能以命令退出码冒充硬件枚举成功。
    if code in (0, 3010):
        for attempt in range(10):
            if probe():
                return {'usable': True, 'restart_required': False, 'message': '鼠标已识别，可直接启用遥控，无需重启电脑'}
            if attempt < 9:
                pause(.3)
    messages = {
        10: '驱动文件校验不符，未重新加载，请检查驱动兼容性',
        11: '鼠标过滤器注册未完成，请修复驱动安装',
        12: '现有鼠标服务配置不兼容或已禁用，未覆盖，请检查驱动安装',
        13: '接收器身份或数量已变化，未重载设备，请重新插入后刷新',
        15: '此 Windows 10 版本不支持定向刷新，请重新插入接收器；仍未识别时需重启电脑',
        16: 'Windows 未能重新枚举目标鼠标，请重新插入接收器；仍未识别时需重启电脑',
        17: 'Windows 未能加载鼠标驱动，请检查系统驱动拦截记录；重新插入后仍未识别时需重启电脑',
    }
    return {'usable': False, 'restart_required': code in (0, 3010, 15, 16, 17),
            'message': messages.get(code, '驱动已部署但尚未识别鼠标，请重新插入接收器；仍未识别时需重启电脑')}


def activate_standalone():
    from .lease import ReceiverLease, check_heartrelay
    check_heartrelay()
    lease = ReceiverLease()
    try:
        return activate()
    finally:
        lease.close()
