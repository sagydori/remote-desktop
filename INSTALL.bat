@echo off
title Remote Desktop - Installer
cd /d "%~dp0"
echo ================================================
echo    Remote Desktop  -  Installer
echo ================================================
echo.
echo This will set up everything needed on this PC.
echo.

REM ---- 1. Find a real Python (ignore the Windows Store stub) ----
call :findpy
if not defined PY (
  echo Python is not installed. Downloading Python 3.12 ...
  set "PYINST=%TEMP%\python-rd-setup.exe"
  curl -L -o "%TEMP%\python-rd-setup.exe" https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
  if errorlevel 1 (
    echo.
    echo Download failed. Please check your internet connection and try again.
    pause & exit /b 1
  )
  echo Installing Python ^(about a minute^)...
  start /wait "" "%TEMP%\python-rd-setup.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1
  call :findpy
)
if not defined PY (
  echo.
  echo Could not set up Python automatically.
  echo Please install it from https://python.org/downloads ^(tick "Add Python to PATH"^),
  echo then run this installer again.
  pause & exit /b 1
)
echo Using Python: %PY%
echo.

REM ---- 2. Install the required libraries ----
echo Installing required libraries (this can take a couple of minutes)...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo.
  echo Installing libraries failed. Please try running the installer again.
  pause & exit /b 1
)
echo.

REM ---- 3. Launch the graphical Setup Wizard ----
echo Opening the Setup Wizard...
set "PYW=%PY:python.exe=pythonw.exe%"
if exist "%PYW%" ( start "" "%PYW%" "%~dp0setup_wizard.py" ) else ( start "" "%PY%" "%~dp0setup_wizard.py" )
exit /b 0

:findpy
set "PY="
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
goto :eof
