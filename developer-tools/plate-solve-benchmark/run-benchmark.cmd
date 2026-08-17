@echo off
setlocal EnableExtensions
set "TOOL_ROOT=%~dp0"
for %%I in ("%TOOL_ROOT%..\..") do set "ROOT=%%~fI\"

if not exist "%ROOT%.venv\Scripts\python.exe" goto system_python
"%ROOT%.venv\Scripts\python.exe" "%TOOL_ROOT%plate_solve_benchmark.py" %*
exit /b %ERRORLEVEL%

:system_python
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Run setup-python-deps.cmd first.
  exit /b 1
)

python "%TOOL_ROOT%plate_solve_benchmark.py" %*
exit /b %ERRORLEVEL%
