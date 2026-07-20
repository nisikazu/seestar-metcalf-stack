@echo off
setlocal
set "ROOT=%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
  exit /b 1
)

if not exist "%ROOT%.venv\Scripts\python.exe" (
  python -m venv "%ROOT%.venv"
  if errorlevel 1 exit /b %ERRORLEVEL%
)

"%ROOT%.venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b %ERRORLEVEL%
"%ROOT%.venv\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 exit /b %ERRORLEVEL%

echo Setup complete.
echo Run: seestar-metcalf-stack.cmd "C:\path\to\Target_sub"
