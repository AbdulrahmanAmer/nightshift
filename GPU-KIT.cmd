@echo off
rem Nightshift GPU kit (card gpu-kit): run it on the GPU PC after installing the release candidate with
rem INSTALL.cmd. It says what each step does before it starts; run it again and it goes on where it stopped.
rem An agent or a script runs it as: GPU-KIT.cmd -Unattended (no input is read, no key waited for, no question
rem asked; the last line is one JSON object; exit code 0 done, 1 a step failed, 2 refused, 3 waiting,
rem 4 another kit runs, 5 a step could not start now, 130 interrupted).
setlocal
title Nightshift GPU kit
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev\kit\gpukit.ps1" %*
set "NS_EXIT=%ERRORLEVEL%"
rem A person's window stays open until a key is pressed (for at most 99999 seconds). Nothing waits when the
rem kit runs unattended, or when its input is not a keyboard (a pipe, a file, NUL): nobody could press a key.
rem (Windows' own timeout.exe, by its full path: another shell's PATH may hold a different program of that name.)
set "NS_UNATTENDED="
for %%A in (%*) do if /i "%%~A"=="-Unattended" set "NS_UNATTENDED=1"
if defined NS_UNATTENDED exit /b %NS_EXIT%
"%SystemRoot%\System32\timeout.exe" /t 0 >nul 2>nul
if errorlevel 1 exit /b %NS_EXIT%
echo.
echo Press any key to close this window . . .
"%SystemRoot%\System32\timeout.exe" /t 99999 >nul 2>nul
exit /b %NS_EXIT%
