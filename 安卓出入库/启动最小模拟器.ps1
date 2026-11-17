param([switch]$Visible)
$ErrorActionPreference = 'Stop'
$inventorySdk = @($env:ANDROID_SDK_ROOT, $env:ANDROID_HOME, 'D:\Android\Sdk') | Where-Object { $_ -and (Test-Path -LiteralPath (Join-Path $_ 'emulator\emulator.exe')) } | Select-Object -First 1
if (-not $inventorySdk) { throw '未找到 Android Emulator。请先安装 emulator 和 system-images;android-34;default;x86_64。' }
if (Get-NetTCPConnection -LocalPort 5556 -State Listen -ErrorAction SilentlyContinue) { Write-Host '模拟器端口 5556 已在使用，请复用当前模拟器。'; exit 0 }
$inventoryArgs = @('-avd','FramekeepMinApi34','-port','5556','-memory','1536','-cores','2','-gpu','swiftshader_indirect','-camera-back','emulated','-camera-front','none','-no-snapshot','-no-boot-anim','-partition-size','512')
if (-not $Visible) { $inventoryArgs += '-no-window' }
$inventoryLaunch = @{ FilePath=(Join-Path $inventorySdk 'emulator\emulator.exe'); ArgumentList=$inventoryArgs; PassThru=$true; WindowStyle='Hidden' }
if ($Visible) { $inventoryLaunch.WindowStyle = 'Normal' }
$inventoryProcess = Start-Process @inventoryLaunch
Write-Host "已启动 FramekeepMinApi34，PID $($inventoryProcess.Id)，序列号 emulator-5556。"
