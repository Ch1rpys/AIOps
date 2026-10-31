@echo off
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_all.ps1" %*
if errorlevel 1 (
  echo FAIL - See the message above.
  pause
  exit /b 1
)
