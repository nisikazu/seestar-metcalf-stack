@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"

if exist "%ROOT%cacert.pem" (
  set "SSL_CERT_FILE=%ROOT%cacert.pem"
  set "REQUESTS_CA_BUNDLE=%ROOT%cacert.pem"
)

if exist "%ROOT%seestar-metcalf-stack.exe" goto run_exe

if exist "%ROOT%.venv\Scripts\python.exe" goto run_venv

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Run setup-python-deps.cmd first.
  set "RC=1"
  goto finish
)

python "%ROOT%scripts\moving_target_pipeline.py" %*
set "RC=%ERRORLEVEL%"
goto finish

:run_exe
"%ROOT%seestar-metcalf-stack.exe" %*
set "RC=%ERRORLEVEL%"
goto finish

:run_venv
"%ROOT%.venv\Scripts\python.exe" "%ROOT%scripts\moving_target_pipeline.py" %*
set "RC=%ERRORLEVEL%"

:finish
if "%RC%"=="0" exit /b 0
echo.
echo Processing did not complete successfully. Review the message above and the log file.
echo Press any key to close this window.
pause >nul
exit /b %RC%
