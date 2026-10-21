@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -r "选片助手\requirements.txt"
if errorlevel 1 exit /b 1
python -X utf8 "选片助手\install_runtime.py"
pause
