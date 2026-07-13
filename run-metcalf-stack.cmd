@echo off
setlocal
set "ROOT=%~dp0"

if exist "%ROOT%metcalf-stack.exe" (
  "%ROOT%metcalf-stack.exe" %*
  exit /b %ERRORLEVEL%
)

set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10+ and run setup-python-deps.cmd.
  exit /b 1
)

"%PY%" "%ROOT%scripts\moving_target_pipeline.py" %*
