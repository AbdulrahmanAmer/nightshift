@echo off
setlocal
title Nightshift uninstall
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\uninstall.ps1" -Ui %*
set "NS_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %NS_EXIT%
