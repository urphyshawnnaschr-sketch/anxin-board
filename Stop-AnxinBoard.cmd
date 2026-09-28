@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-local.ps1" %*
set "ANXIN_EXIT=%ERRORLEVEL%"
if not "%ANXIN_EXIT%"=="0" (
  echo.
  echo AnxinBoard did not stop cleanly. See the message above.
  pause
)
exit /b %ANXIN_EXIT%
