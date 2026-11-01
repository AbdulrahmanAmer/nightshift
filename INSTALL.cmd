@echo off
setlocal
title Nightshift setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\stage1.ps1" -AppPath "%~dp0." %*
set "NS_EXIT=%ERRORLEVEL%"
echo.
if not "%NS_EXIT%"=="0" echo Setup stopped (exit code %NS_EXIT%). Read the message above, then run INSTALL.cmd again.
pause
exit /b %NS_EXIT%
