@echo off
setlocal
set "ROOT=%~dp0"
set "PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%ROOT%scripts\moving_target_pipeline.py" %*
