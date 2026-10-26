@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -X utf8 "选片助手\install_bridge.py"
pause
