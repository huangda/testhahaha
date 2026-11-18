@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting dashboard ... (close this window to stop)
echo.
"%~dp0python\python.exe" -m tem serve
echo.
pause
