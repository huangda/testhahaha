@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Collecting odds + news, analysing, forecasting ...
echo.
"%~dp0python\python.exe" -m tem run --limit 150 --days 7 --window 7
echo.
pause
