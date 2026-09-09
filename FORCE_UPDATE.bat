@echo off
title doris pccontrol - Force Update
cd /d "%~dp0"
echo ============================================================
echo   doris pccontrol  -  Force update to the newest version
echo   (Use this once on the OTHER PC if it won't auto-update.)
echo ============================================================
echo.
echo Downloading the latest code from GitHub...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $repo='sagydori/remote-desktop'; $dir=('%~dp0').TrimEnd('\'); $tmp=Join-Path $env:TEMP ('pcc_'+[guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $tmp | Out-Null; $sha=(Invoke-RestMethod -Headers @{'User-Agent'='pcc'} ('https://api.github.com/repos/'+$repo+'/commits/main')).sha; Write-Host ('Newest version: '+$sha.Substring(0,7)); $zip=Join-Path $tmp 'src.zip'; Invoke-WebRequest ('https://codeload.github.com/'+$repo+'/zip/'+$sha) -OutFile $zip; Write-Host 'Installing...'; Expand-Archive $zip -DestinationPath $tmp -Force; $src=(Get-ChildItem $tmp -Directory)[0].FullName; robocopy $src $dir /E /XF config.py settings.json .applied_commit /XD .git *> $null; Set-Content -Path (Join-Path $dir '.applied_commit') -Value $sha -Encoding ascii; Remove-Item $tmp -Recurse -Force; Write-Host ''; Write-Host 'Update complete!'"

if errorlevel 1 (
  echo.
  echo Something went wrong. Check the internet connection and try again.
) else (
  echo.
  echo You now have the newest version. Open the app normally.
)
echo.
pause
