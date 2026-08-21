@echo off
setlocal
set "ROOT=%~dp0..\..\"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
"%PYTHON%" "%~dp0background_normalization_benchmark.py" %*
exit /b %ERRORLEVEL%
