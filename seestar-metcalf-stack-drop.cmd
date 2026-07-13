@echo off
setlocal
set "ROOT=%~dp0"

if "%~1"=="" (
  echo Drag a Seestar subframe folder onto this file, or run:
  echo   %~nx0 "C:\path\to\target_sub"
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\run_metcalf_stack_drop.ps1" "%~1"
exit /b %ERRORLEVEL%
