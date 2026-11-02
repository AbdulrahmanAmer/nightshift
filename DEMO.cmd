@echo off
rem Nightshift demo: the dashboard with sample data, no engine or Python needed. Needs Node.js 18 or newer.
setlocal
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js is not installed. Get the LTS version from https://nodejs.org/ and run DEMO.cmd again.
  pause
  exit /b 1
)
for /f "delims=" %%v in ('node --version') do echo Using Node.js %%v
node "dev\demo\serve.js" %*
if errorlevel 1 pause
