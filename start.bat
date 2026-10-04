@echo off
chcp 65001 >nul
cd /d "%~dp0"
python agy_switch.py %*
if errorlevel 1 pause
