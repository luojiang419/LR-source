$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$demoPython = (Get-Command python.exe -ErrorAction Stop).Source
& $demoPython -X utf8 (Join-Path $PSScriptRoot '选片助手\real_data_demo.py')
if ($LASTEXITCODE -ne 0) { throw 'The real-data demo did not start. See build/真实数据可操作演示/demo-server.log.' }
