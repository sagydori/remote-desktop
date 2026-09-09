@echo off
REM ============================================================
REM  Build "doris pccontrol.exe"  —  a standalone Windows app.
REM  Run this once; the finished app appears in the  dist  folder.
REM ============================================================
cd /d "%~dp0"

echo Installing the build tool (first time only)...
python -m pip install --quiet pyinstaller

echo.
echo Building doris pccontrol.exe  (this takes a minute or two)...
python -m PyInstaller --noconfirm --onefile --windowed ^
  --name "doris pccontrol" ^
  --icon "icon.ico" ^
  --collect-all customtkinter ^
  --collect-all bettercam ^
  remote_desktop.py

echo.
echo Copying your settings next to the app...
copy /Y "config.py"  "dist\config.py"   >nul 2>&1
copy /Y "icon.ico"   "dist\icon.ico"    >nul 2>&1
copy /Y "VERSION"    "dist\VERSION"      >nul 2>&1

echo.
echo ============================================================
echo  Done!  Your app is here:
echo     dist\doris pccontrol.exe
echo.
echo  Right-click it -^> Send to -^> Desktop (create shortcut)
echo  to put an icon on your desktop.
echo ============================================================
pause
