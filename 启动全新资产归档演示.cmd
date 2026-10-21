@echo off
cd /d "%~dp0"
python -X utf8 "演示方案\资产归档重设计\启动演示.py"
if errorlevel 1 pause
