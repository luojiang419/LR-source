param([switch]$Wait, [switch]$Startup, [switch]$Silent, [string]$DataDirectory)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskPython = (Get-Command python.exe -ErrorAction Stop).Source
$taskExe = Join-Path $taskRoot '桌面程序\target\debug\framekeep-desktop.exe'
# 每次由 Cargo 增量检查，确保主项目正式构建不会被当成独立开发版运行。
$taskPreviousConfig = $env:TAURI_CONFIG
try {
    $env:TAURI_CONFIG = '{"productName":"末帧资产检索开发版","identifier":"com.framekeep.culling.multimodal-dev"}'
    cargo build --locked --manifest-path (Join-Path $taskRoot '桌面程序\Cargo.toml')
    if ($LASTEXITCODE -ne 0) { throw '桌面开发版构建失败' }
} finally {
    if ($null -eq $taskPreviousConfig) { Remove-Item Env:TAURI_CONFIG -ErrorAction SilentlyContinue } else { $env:TAURI_CONFIG = $taskPreviousConfig }
}
$env:FRAMEKEEP_DATA = if ($DataDirectory) { [System.IO.Path]::GetFullPath($DataDirectory) } else { Join-Path $taskRoot '.framekeep-search-dev' }
$env:FRAMEKEEP_APP_ROOT = $taskRoot
$env:FRAMEKEEP_PORT = '8779'
$env:FRAMEKEEP_ENGINE = $taskPython
$env:FRAMEKEEP_ENGINE_SCRIPT = Join-Path $taskRoot '选片助手\server.py'
$env:FRAMEKEEP_CANON_REGISTRATION = '0'
$env:FRAMEKEEP_DEV_WEB_NO_CODE = '1'
# 只引用已有运行时，不复制模型、不改原版数据、不安装驱动或注册系统关联。
$taskRuntimeRoot = Join-Path $taskRoot '.runtime'
if (-not (Test-Path -LiteralPath $taskRuntimeRoot)) { $taskRuntimeRoot = 'G:\app\LR\.runtime' }
$taskModelConfig = Join-Path $taskRuntimeRoot 'local-inference\local-models.json'
if (Test-Path -LiteralPath $taskModelConfig) { $env:FRAMEKEEP_LOCAL_MODELS = $taskModelConfig }
$taskExif = Get-ChildItem -LiteralPath (Join-Path $taskRuntimeRoot 'exiftool') -Filter exiftool.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if ($taskExif) { $env:FRAMEKEEP_EXIFTOOL = $taskExif.FullName }
$env:PATH = (Join-Path $taskRuntimeRoot 'ffmpeg') + ';' + $env:PATH
if ((Test-Path -LiteralPath $taskModelConfig) -and -not (Test-Path -LiteralPath (Join-Path $env:FRAMEKEEP_DATA 'model-providers.json'))) {
    & $taskPython -X utf8 -c "import os,sys; from pathlib import Path; sys.path.insert(0,str(Path(os.environ['FRAMEKEEP_APP_ROOT'])/'选片助手')); from model_providers import Providers; Providers(os.environ['FRAMEKEEP_DATA']).set_builtin(True)"
    if ($LASTEXITCODE -ne 0) { throw '初始化开发版模型配置失败' }
}
$taskLaunchOptions = @{ FilePath = $taskExe; WorkingDirectory = $taskRoot; WindowStyle = 'Hidden'; PassThru = $true }
$taskLaunchArgs = @()
if ($Startup) { $taskLaunchArgs += '--startup' }
if ($Silent) { $taskLaunchArgs += '--silent' }
if ($taskLaunchArgs.Count) { $taskLaunchOptions.ArgumentList = $taskLaunchArgs }
$taskApp = Start-Process @taskLaunchOptions
if ($Wait) { $taskApp.WaitForExit() }
