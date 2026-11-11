<#
.SYNOPSIS
  The Nightshift GPU kit (card gpu-kit). GPU-KIT.cmd runs this under Windows PowerShell 5.1.

.DESCRIPTION
  Finds the install (-Home <folder>, else the user's NIGHTSHIFT_HOME, else the install this file sits in,
  else it asks once at the keyboard, for at most 15 minutes), says which one on screen, checks
  config\runtime.json and app\VERSION, and runs the install's own copy of the kit (app\dev\kit\gpukit.py)
  with the install's own Python (venvs\studio\Scripts\python.exe). NIGHTSHIFT_HOME is set for that one
  process only; nothing on the PC is changed here. The kit's steps, its state and its zip are the Python
  file's.

  Nothing here waits without an end: the one question (the folder) is asked only at a keyboard and ends by
  time; when the input is not a keyboard (a pipe, a file, NUL, nothing) it is not asked at all.

.PARAMETER Home
  The install folder (NIGHTSHIFT_HOME).

.PARAMETER DryRun
  A laptop test install only (fake_gpu); the kit refuses it on a real install.

.PARAMETER NightNow
  With -DryRun: each "night" runs at once through the install's own stub.

.PARAMETER Clock
  With -DryRun: the kit's clock starts at this time (ISO, for example 2026-11-12T22:00).

.PARAMETER Unattended
  For an agent or a script, not a person: GPU-KIT.cmd -Unattended. The kit reads no input and waits for no
  key, does not pause before it starts, and asks none of the questions only a person can answer (they stay
  open and are listed under "For the owner"; a later run without the switch asks them). Everything else is
  as in an attended run. Its last line on stdout is one JSON object (step, state, next_run_after,
  open_questions, zip, ...), and its exit code is one of: 0 done (only the owner's questions may be open),
  1 a step failed, 2 refused (nothing was changed), 3 waiting (run it again after next_run_after),
  4 another run of the kit holds the lock, 5 a step could not start now, 130 interrupted.
#>
[CmdletBinding()]
param(
    [Alias('Home')][string]$NsHome = '',
    [switch]$DryRun,
    [switch]$NightNow,
    [string]$Clock = '',
    [switch]$Unattended
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-NsInstall {
    param([string]$Folder)
    if (-not $Folder) { return $false }
    return (Test-Path -LiteralPath (Join-Path $Folder 'config\runtime.json')) -and
        (Test-Path -LiteralPath (Join-Path $Folder 'app\VERSION'))
}

function Read-KitLine {
    # One line typed at the keyboard, or $null: nobody typed one within $TimeoutSec, or the input is not a
    # keyboard (a pipe, a file, NUL, nothing). Such an input is never read and the question is not shown:
    # an agent's shell cannot be asked, and a read on a pipe nobody writes to would never end.
    param([string]$Question, [string]$Prompt, [int]$TimeoutSec = 900)
    $redirected = $true
    try { $redirected = [Console]::IsInputRedirected } catch { $redirected = $true }
    if ($redirected) { return $null }
    Write-Host $Question
    Write-Host ($Prompt + ': ') -NoNewline
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $typed = ''
    while ((Get-Date) -lt $deadline) {
        $ready = $false
        try { $ready = [Console]::KeyAvailable } catch { Write-Host ''; return $null }
        if (-not $ready) { Start-Sleep -Milliseconds 100; continue }
        $key = [Console]::ReadKey($true)
        if ($key.Key -eq [ConsoleKey]::Enter) { Write-Host ''; return $typed }
        if ($key.Key -eq [ConsoleKey]::Backspace) {
            if ($typed.Length -gt 0) {
                $typed = $typed.Substring(0, $typed.Length - 1)
                Write-Host ([string][char]8 + ' ' + [string][char]8) -NoNewline
            }
            continue
        }
        if ($key.KeyChar -ne [char]0) {
            $typed += $key.KeyChar
            Write-Host $key.KeyChar -NoNewline
        }
    }
    Write-Host ''
    return $null
}

function Exit-KitRefused {
    # The kit did not start and changed nothing: the reason, the way out and exit code 2. Unattended: the
    # last line is the same JSON object the kit's own refusals end with (state "refused").
    param([string]$Why, [string]$WayOut)
    Write-Host ('REFUSED ' + $Why)
    Write-Host $WayOut
    if ($Unattended) {
        $reason = ConvertTo-Json ($Why + ' ' + $WayOut) -Compress
        Write-Host ('{"kit": "nightshift-gpu-kit", "step": "K0", "step_title": "start", "state": "refused", "exit_code": 2, "next_run_after": null, "open_questions": [], "zip": null, "failed_steps": [], "reason": ' + $reason + '}')
    }
    exit 2
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
        $typed = $null
        if (-not $Unattended) {
            $typed = Read-KitLine -Question 'Which folder is Nightshift installed in? (the folder that holds app, config and venvs)' -Prompt 'Folder' -TimeoutSec 900
        }
        if ($null -eq $typed) {
            Exit-KitRefused 'no Nightshift install was found (no NIGHTSHIFT_HOME, and this kit is not inside one), and no folder was typed.' 'Run it again with the folder: GPU-KIT.cmd -Home <the folder that holds app, config and venvs>'
        }
        $found = ([string]$typed).Trim().Trim('"')
        $why = 'typed by you'
    }
}
if (-not (Test-NsInstall $found)) {
    Exit-KitRefused "$found is not a Nightshift install: it has no config\runtime.json or no app\VERSION." 'Install the release candidate with INSTALL.cmd first, then run GPU-KIT.cmd again.'
}
$found = (Resolve-Path -LiteralPath $found).Path
$python = Join-Path $found 'venvs\studio\Scripts\python.exe'
$kit = Join-Path $found 'app\dev\kit\gpukit.py'
$version = (Get-Content -LiteralPath (Join-Path $found 'app\VERSION') -Raw).Trim()
Write-Host "NIGHTSHIFT_HOME for this kit: $found ($why), version $version"
if (-not (Test-Path -LiteralPath $python)) {
    Exit-KitRefused "the install's Python is missing ($python)." 'Run INSTALL.cmd again (it repairs it), then run GPU-KIT.cmd again.'
}
if (-not (Test-Path -LiteralPath $kit)) {
    Exit-KitRefused "this install ($version) has no GPU kit ($kit)." 'Install the release candidate that has GPU-KIT.cmd with its INSTALL.cmd, then run GPU-KIT.cmd again.'
}
$kitArgs = @($kit, '--home', $found)
if ($DryRun) { $kitArgs += '--dry-run' }
if ($NightNow) { $kitArgs += '--night-now' }
if ($Clock) { $kitArgs += @('--clock', $Clock) }
if ($Unattended) { $kitArgs += '--unattended' }
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
