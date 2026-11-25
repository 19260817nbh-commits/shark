@echo off
rem One-click Windows desktop GUI page (QtWebEngine, no browser).
cd /d "%~dp0"
echo Serval GUI - Windows remote UDP receiver
py -3 windows_gui.py %*
if errorlevel 1 pause
