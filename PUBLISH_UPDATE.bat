@echo off
title Publish update to GitHub
cd /d "%~dp0"
echo ============================================================
echo   Publishing the latest version to GitHub.
echo   Both PCs will auto-update next time they open the app.
echo ============================================================
echo.
git push -u origin main
echo.
echo If you see "main -> main" or "Everything up-to-date", it worked.
pause
