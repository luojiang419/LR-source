$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$env:FRAMEKEEP_DATA = Join-Path $taskRoot '.framekeep'
$env:FRAMEKEEP_LOCAL_MODELS = Join-Path $taskRoot '.runtime\local-inference\local-models.json'
$taskExe = Join-Path $taskRoot 'dist\末帧选片-内置模型\末帧选片.exe'
if (-not (Test-Path -LiteralPath $taskExe)) { throw '尚未部署内置模型版。请查看技术文档/内置模型部署与实测.md。' }
if (-not (Test-Path -LiteralPath $env:FRAMEKEEP_LOCAL_MODELS)) { throw '内置模型尚未部署，请先运行 setup_local_inference.py。' }
Start-Process -FilePath $taskExe -WorkingDirectory $taskRoot
