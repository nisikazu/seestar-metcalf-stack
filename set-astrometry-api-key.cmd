@echo off
setlocal
set "ROOT=%~dp0"

if "%~1"=="" (
  echo Usage: set-astrometry-api-key.cmd YOUR_ASTROMETRY_NET_API_KEY
  echo This writes the key to "%ROOT%.astrometry_api_key".
  exit /b 1
)

> "%ROOT%.astrometry_api_key" echo %~1
echo Wrote "%ROOT%.astrometry_api_key"
