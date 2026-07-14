@echo off
setlocal
set "ROOT=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\run_metcalf_stack.ps1" %*
exit /b %ERRORLEVEL%
