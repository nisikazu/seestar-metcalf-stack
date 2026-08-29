@echo off
setlocal EnableExtensions
set "SEESTAR_STACK_TARGET_MODE=fixed"
call "%~dp0seestar-metcalf-stack.cmd" %*
exit /b %ERRORLEVEL%
