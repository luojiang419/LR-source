$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$env:FRAMEKEEP_DATA = Join-Path $taskRoot '.framekeep'
if (-not $env:FRAMEKEEP_CONFIG -and -not (Test-Path -LiteralPath (Join-Path $taskRoot '.framekeep\config-location')) -and (Test-Path -LiteralPath (Join-Path $taskRoot 'api key.md'))) {
    $env:FRAMEKEEP_CONFIG = Join-Path $taskRoot 'api key.md'
}
$taskExe = Join-Path $taskRoot 'dist\末帧选片\末帧选片.exe'
if (Test-Path -LiteralPath $taskExe) {
    Start-Process -FilePath $taskExe -WorkingDirectory $taskRoot
} else {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show('未找到桌面程序。请先运行 python -X utf8 选片助手/build_release.py 构建本地程序。', '末帧选片') | Out-Null
}
