@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -X utf8 "选片助手\split_demo.py" --source "%~dp0." --browser
if errorlevel 1 pause
