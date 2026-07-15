@echo off
setlocal
set "ROOT=%~dp0"

if not "%SIRIL_CLI%"=="" (
  if exist "%SIRIL_CLI%" (
    "%SIRIL_CLI%" %*
    exit /b %ERRORLEVEL%
  )
)

set "SIRIL=%ROOT%tools\siril-1.4.1\siril\bin\siril-cli.exe"
if exist "%SIRIL%" (
  "%SIRIL%" %*
  exit /b %ERRORLEVEL%
)

where siril-cli.exe >nul 2>nul
if not errorlevel 1 (
  siril-cli.exe %*
  exit /b %ERRORLEVEL%
)

set "SIRIL=%ProgramFiles%\Siril\bin\siril-cli.exe"
if exist "%SIRIL%" (
  "%SIRIL%" %*
  exit /b %ERRORLEVEL%
)

set "SIRIL=%ProgramFiles%\Siril\siril-cli.exe"
if exist "%SIRIL%" (
  "%SIRIL%" %*
  exit /b %ERRORLEVEL%
)

set "SIRIL=%LocalAppData%\Programs\Siril\bin\siril-cli.exe"
if exist "%SIRIL%" (
  "%SIRIL%" %*
  exit /b %ERRORLEVEL%
)

echo Siril CLI was not found.
echo Install Siril, add siril-cli.exe to PATH, or set SIRIL_CLI to the full path.
echo Example:
echo   set SIRIL_CLI=C:\Program Files\Siril\bin\siril-cli.exe
exit /b 1
