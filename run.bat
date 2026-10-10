@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title WeChat Data Exporter
chcp 65001 >NUL
echo Open in browser: http://127.0.0.1:8756/
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0src\launch.ps1"
exit /b %errorlevel%
