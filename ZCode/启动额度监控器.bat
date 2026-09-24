@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if errorlevel 1 exit /b 1
start "" pythonw.exe "%~dp0dashboard.py"
exit /b 0
