@echo off
rem Nightshift GPU kit (card gpu-kit): run it on the GPU PC after installing the release candidate with
rem INSTALL.cmd. It says what each step does before it starts; run it again and it goes on where it stopped.
setlocal
title Nightshift GPU kit
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev\kit\gpukit.ps1" %*
set "NS_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %NS_EXIT%
