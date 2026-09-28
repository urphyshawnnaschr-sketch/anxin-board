@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-local-secure.ps1" %*
set "ANXIN_EXIT=%ERRORLEVEL%"
if not "%ANXIN_EXIT%"=="0" (
  echo.
  echo AnxinBoard failed to start. See the message above.
  pause
)
exit /b %ANXIN_EXIT%
