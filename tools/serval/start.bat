@echo off
cd /d "%~dp0"
python monitor.py --open %*
if errorlevel 1 pause
