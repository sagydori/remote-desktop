@echo off
REM Opens the Remote Desktop app (no console). The Setup Wizard also makes a Desktop shortcut.
cd /d "%~dp0"
set "PYW="
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\pythonw.exe" set "PYW=%%D\pythonw.exe"
if not defined PYW for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%D\pythonw.exe" set "PYW=%%D\pythonw.exe"
if not defined PYW where pythonw.exe >nul 2>nul && set "PYW=pythonw.exe"
if not defined PYW (
  echo Python not found. Run INSTALL.bat first.
  pause & exit /b 1
)
start "" "%PYW%" remote_desktop.py
