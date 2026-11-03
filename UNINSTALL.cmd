@echo off
setlocal
title Nightshift uninstall
rem This console may have been opened in <home>\app (Explorer starts it in the folder of the file). Windows will
rem not delete a folder that is some process's working directory, so leave it before anything else runs:
rem the uninstall removes <home>\app, and the console waits at "pause" long after the window has closed.
cd /d "%TEMP%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\uninstall.ps1" -Ui %*
set "NS_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %NS_EXIT%
