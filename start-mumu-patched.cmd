@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=H:\MuMuPlayer\nx_main\MuMuNxMain.exe"

if not exist "%TARGET%" (
  echo Target was not found: %TARGET%
  echo Usage: %~nx0 [full-path-to-MuMuNxMain.exe]
  exit /b 1
)

python "%SCRIPT_DIR%auto-patch-mumu.py" --target "%TARGET%"
if errorlevel 1 (
  echo.
  echo Patch failed. Close MuMuNxMain.exe and try again.
  exit /b 1
)

start "" "%TARGET%"
