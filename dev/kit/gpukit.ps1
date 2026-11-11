<#
.SYNOPSIS
  The Nightshift GPU kit (card gpu-kit). GPU-KIT.cmd runs this under Windows PowerShell 5.1.

.DESCRIPTION
  Finds the install (-Home <folder>, else the user's NIGHTSHIFT_HOME, else the install this file sits in,
  else it asks once), says which one on screen, checks config\runtime.json and app\VERSION, and runs the
  install's own copy of the kit (app\dev\kit\gpukit.py) with the install's own Python
  (venvs\studio\Scripts\python.exe). NIGHTSHIFT_HOME is set for that one process only; nothing on the PC is
  changed here. The kit's steps, its state and its zip are the Python file's.

.PARAMETER Home
  The install folder (NIGHTSHIFT_HOME).

.PARAMETER DryRun
  A laptop test install only (fake_gpu); the kit refuses it on a real install.

.PARAMETER NightNow
  With -DryRun: each "night" runs at once through the install's own stub.

.PARAMETER Clock
  With -DryRun: the kit's clock starts at this time (ISO, for example 2026-11-12T22:00).
#>
[CmdletBinding()]
param(
    [Alias('Home')][string]$NsHome = '',
    [switch]$DryRun,
    [switch]$NightNow,
    [string]$Clock = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-NsInstall {
    param([string]$Folder)
    if (-not $Folder) { return $false }
    return (Test-Path -LiteralPath (Join-Path $Folder 'config\runtime.json')) -and
        (Test-Path -LiteralPath (Join-Path $Folder 'app\VERSION'))
}

$found = ''
$why = ''
if ($NsHome) {
    $found = $NsHome
    $why = 'given with -Home'
}
else {
    $userHome = [Environment]::GetEnvironmentVariable('NIGHTSHIFT_HOME', 'User')
    $inside = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
    if (Test-NsInstall $userHome) { $found = $userHome; $why = "your NIGHTSHIFT_HOME" }
    elseif (Test-NsInstall $env:NIGHTSHIFT_HOME) { $found = $env:NIGHTSHIFT_HOME; $why = 'NIGHTSHIFT_HOME of this window' }
    elseif (Test-NsInstall $inside) { $found = $inside; $why = 'the install this kit is part of' }
    else {
        Write-Host 'Which folder is Nightshift installed in? (the folder that holds app, config and venvs)'
        $found = (Read-Host 'Folder').Trim().Trim('"')
        $why = 'typed by you'
    }
}
if (-not (Test-NsInstall $found)) {
    Write-Host "REFUSED $found is not a Nightshift install: it has no config\runtime.json or no app\VERSION."
    Write-Host 'Install the release candidate with INSTALL.cmd first, then run GPU-KIT.cmd again.'
    exit 2
}
$found = (Resolve-Path -LiteralPath $found).Path
$python = Join-Path $found 'venvs\studio\Scripts\python.exe'
$kit = Join-Path $found 'app\dev\kit\gpukit.py'
$version = (Get-Content -LiteralPath (Join-Path $found 'app\VERSION') -Raw).Trim()
Write-Host "NIGHTSHIFT_HOME for this kit: $found ($why), version $version"
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "REFUSED the install's Python is missing ($python). Run INSTALL.cmd again (it repairs it), then run GPU-KIT.cmd again."
    exit 2
}
if (-not (Test-Path -LiteralPath $kit)) {
    Write-Host "REFUSED this install ($version) has no GPU kit ($kit). Install the release candidate that has GPU-KIT.cmd with its INSTALL.cmd, then run GPU-KIT.cmd again."
    exit 2
}
$kitArgs = @($kit, '--home', $found)
if ($DryRun) { $kitArgs += '--dry-run' }
if ($NightNow) { $kitArgs += '--night-now' }
if ($Clock) { $kitArgs += @('--clock', $Clock) }
$env:NIGHTSHIFT_HOME = $found
Push-Location -LiteralPath $found
try {
    & $python @kitArgs
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $code
