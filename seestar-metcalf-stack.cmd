@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"

if "%~1"=="" goto run_without_source

set "FIRST=%~1"
if "%FIRST:~0,1%"=="-" goto run_without_source

rem Pass the source through the environment so a trailing backslash cannot
rem escape PowerShell's closing quote. Re-quote the remaining option values.
set "SEESTAR_METCALF_SOURCE=%~1"
shift
set "FORWARD_ARGS="

:collect_arguments
if "%~1"=="" goto run_with_source
set "FORWARD_ARGS=%FORWARD_ARGS% "%~1""
shift
goto collect_arguments

:run_with_source
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\run_metcalf_stack.ps1" %FORWARD_ARGS%
exit /b %ERRORLEVEL%

:run_without_source
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\run_metcalf_stack.ps1" %*
exit /b %ERRORLEVEL%
