$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskExe = Join-Path $taskRoot 'dist\SHIYING 数字资产\SHIYING 数字资产.exe'
if (Test-Path -LiteralPath $taskExe) {
    $env:FRAMEKEEP_DATA = Join-Path $taskRoot '.framekeep'
    if (-not $env:FRAMEKEEP_CONFIG -and -not (Test-Path -LiteralPath (Join-Path $taskRoot '.framekeep\config-location')) -and (Test-Path -LiteralPath (Join-Path $taskRoot 'api key.md'))) {
        $env:FRAMEKEEP_CONFIG = Join-Path $taskRoot 'api key.md'
    }
    Start-Process -FilePath $taskExe -WorkingDirectory $taskRoot
    return
}
$installation = Get-ItemProperty -LiteralPath 'HKCU:\Software\Framekeep' -ErrorAction SilentlyContinue
if ($installation -and $installation.InstallLocation) {
    $installedExe = Join-Path $installation.InstallLocation 'SHIYING 数字资产.exe'
    if (Test-Path -LiteralPath $installedExe) {
        Start-Process -FilePath $installedExe -WorkingDirectory $installation.InstallLocation
        return
    }
}
Add-Type -AssemblyName PresentationFramework
[System.Windows.MessageBox]::Show('未找到桌面程序。请先安装SHIYING 数字资产，或运行 python -X utf8 选片助手/build_release.py 构建本地程序。', 'SHIYING 数字资产') | Out-Null
