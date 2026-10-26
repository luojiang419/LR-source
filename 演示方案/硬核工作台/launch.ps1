$ErrorActionPreference = 'Stop'
$previewUrl = 'http://127.0.0.1:8795/'
try {
    $previewResponse = Invoke-WebRequest -Uri $previewUrl -UseBasicParsing -TimeoutSec 2
    if ($previewResponse.Content -notmatch '硬核工作台/bootstrap.js') { throw '8795 端口已被其他服务占用。' }
    Start-Process $previewUrl
} catch {
    if ($previewResponse) { throw }
    $previewPython = (Get-Command python -ErrorAction Stop).Source
    $previewScript = Join-Path $PSScriptRoot 'preview_server.py'
    Start-Process -FilePath $previewPython -ArgumentList @('-X', 'utf8', ('"{0}"' -f $previewScript), '--browser') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
}
