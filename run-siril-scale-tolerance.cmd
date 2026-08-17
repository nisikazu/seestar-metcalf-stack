@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"

if exist "%ROOT%.venv\Scripts\python.exe" (
  "%ROOT%.venv\Scripts\python.exe" "%ROOT%scripts\siril_scale_tolerance.py" %*
  exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Run setup-python-deps.cmd first.
  exit /b 1
)

python "%ROOT%scripts\siril_scale_tolerance.py" %*
exit /b %ERRORLEVEL%
