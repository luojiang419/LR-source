@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -X utf8 tools\multinode_sync_demo\verify.py
if errorlevel 1 (
  echo 验证未通过，请查看上方错误及 build\multinode-sync-demo 下的本轮记录。
  pause
  exit /b 1
)
powershell -NoProfile -Command "$demoLatest = Get-Content -LiteralPath 'build\multinode-sync-demo\latest.json' -Raw | ConvertFrom-Json; Start-Process -FilePath $demoLatest.report"
