@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title wxMoments
echo.
echo   Loading...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0src\launch.ps1"
set "WXMOMENTS_EXIT=%errorlevel%"
echo.
if not "%WXMOMENTS_EXIT%"=="0" (
  echo   Startup did not finish. Follow the message above, then run run.bat again.
) else (
  echo   If the browser did not open, visit: http://127.0.0.1:8756/
)
echo.
echo   This window stays open. You can close it at any time.
pause
exit /b %WXMOMENTS_EXIT%
