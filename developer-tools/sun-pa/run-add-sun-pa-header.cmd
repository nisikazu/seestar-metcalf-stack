@echo off
setlocal
set "ROOT=%~dp0..\.."
set "PYTHON=%ROOT%\tools\python\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
"%PYTHON%" "%~dp0add_sun_pa_header.py" %*
exit /b %ERRORLEVEL%
