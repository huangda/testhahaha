@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Generating the analysis report ...
echo.
"%~dp0python\python.exe" -m tem brief
echo.
start "" "%~dp0data\exports\report.html"
timeout /t 3 >nul
