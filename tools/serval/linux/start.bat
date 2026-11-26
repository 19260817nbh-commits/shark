@echo off
rem Serval browser-mode launcher (Linux/Jetson desktop, opens the web page).
cd /d "%~dp0.."
python monitor.py --open %*
if errorlevel 1 pause
