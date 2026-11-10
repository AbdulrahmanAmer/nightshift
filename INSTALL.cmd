@echo off
setlocal
title Nightshift setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\stage1.ps1" -AppPath "%~dp0." %*
set "NS_EXIT=%ERRORLEVEL%"
echo.
if not "%NS_EXIT%"=="0" echo Setup stopped (exit code %NS_EXIT%). Read the message above, then run INSTALL.cmd again.
rem The window stays open at "pause" for a person who double-clicked INSTALL.cmd, so that the message above can be
rem read. A run that said it has nobody at the keyboard (-NoUi, NS_NONINTERACTIVE=1, or -Answers without -Ui) must
rem not wait for a key either: a script with a console attached would wait for ever (D-40).
set "NS_WAIT=1"
set "NS_ANSWERS="
set "NS_WINDOW="
if "%NS_NONINTERACTIVE%"=="1" set "NS_WAIT="
:scan
if "%~1"=="" goto scanned
if /i "%~1"=="-NoUi" set "NS_WAIT="
if /i "%~1"=="-Answers" set "NS_ANSWERS=1"
if /i "%~1"=="-Ui" set "NS_WINDOW=1"
shift
goto scan
:scanned
if defined NS_ANSWERS if not defined NS_WINDOW set "NS_WAIT="
if defined NS_WAIT pause
exit /b %NS_EXIT%
