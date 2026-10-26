@echo off
chcp 65001 >nul
powershell -NoProfile -WindowStyle Hidden -File "%~dp0演示方案\硬核工作台\launch.ps1"
