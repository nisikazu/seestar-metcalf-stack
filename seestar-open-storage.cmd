@echo off
setlocal EnableExtensions

if "%~1"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\seestar-open-storage.ps1"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\seestar-open-storage.ps1" -SeestarHost "%~1"
)

set "RC=%ERRORLEVEL%"
if "%RC%"=="0" exit /b 0

echo.
echo Press any key to close this window.
pause >nul
exit /b %RC%
