@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=H:\MuMuPlayer\nx_main\MuMuNxMain.exe"
set "MAIN_TARGET=H:\MuMuPlayer\nx_main\MuMuNxMain.exe"
set "SERVICE_TARGET=H:\MuMuPlayer\nx_main\MuMuNxService.exe"
set "PYTHON_EXE="

if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PYTHON_EXE if exist "%ProgramFiles%\Python312\python.exe" set "PYTHON_EXE=%ProgramFiles%\Python312\python.exe"
if not defined PYTHON_EXE if exist "%ProgramFiles%\Python311\python.exe" set "PYTHON_EXE=%ProgramFiles%\Python311\python.exe"

if not defined PYTHON_EXE (
  echo Python 3.11+ was not found in standard install locations.
  echo Install CPython for the current user, then re-run this launcher.
  exit /b 1
)

if /I not "%TARGET%"=="%MAIN_TARGET%" (
  echo Refusing non-allowlisted launcher target: %TARGET%
  echo Usage: %~nx0 [H:\MuMuPlayer\nx_main\MuMuNxMain.exe]
  exit /b 1
)

if not exist "%MAIN_TARGET%" (
  echo Main target was not found: %MAIN_TARGET%
  exit /b 1
)

if not exist "%SERVICE_TARGET%" (
  echo Service target was not found: %SERVICE_TARGET%
  exit /b 1
)

"%PYTHON_EXE%" "%SCRIPT_DIR%auto-patch-mumu.py" apply --targets main,service
if errorlevel 1 (
  echo.
  echo Transactional Main+Service patch failed. Close local MuMuNxMain.exe and MuMuNxService.exe and try again.
  exit /b 1
)

"%PYTHON_EXE%" "%SCRIPT_DIR%auto-patch-mumu.py" verify --targets main,service
if errorlevel 1 exit /b 1

start "" "%MAIN_TARGET%"
