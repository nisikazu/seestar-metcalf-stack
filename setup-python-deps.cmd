@echo off
setlocal
set "ROOT=%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
  exit /b 1
)

python -m pip install --upgrade pip
python -m pip install -r "%ROOT%requirements.txt"
