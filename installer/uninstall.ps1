<#
.SYNOPSIS
    Nightshift uninstaller (component C1, spec 5).

.DESCRIPTION
    Removes the scheduled task, desktop shortcuts, venvs\, bin\ and the NIGHTSHIFT_HOME user environment
    variable. models\ and the user data (outbox, rejected, data, imports, cache) are kept unless you answer
    yes (default No) or pass -DeleteModels / -DeleteData. Safe to run more than once.

    -HomeDir     folder to clean (default: NIGHTSHIFT_HOME, then %LOCALAPPDATA%\Nightshift).
    -DryRun      print "[dry-run] <action>" lines, change nothing, exit 0.
    -SkipSystem  leave the scheduled task, shortcuts and environment variable alone (used by CI).
#>
[CmdletBinding()]
param(
    [string]$HomeDir = '',
    [switch]$DryRun,
    [switch]$DeleteModels,
    [switch]$DeleteData,
    [switch]$SkipSystem
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:IsDry = [bool]$DryRun
$TaskName = 'Nightshift Daily'

function Show-Action {
    param([Parameter(Mandatory = $true)][string]$Text)
    Write-Host "[dry-run] $Text"
}

function Test-Interactive {
    if ($env:NS_NONINTERACTIVE -eq '1') { return $false }
    return [Environment]::UserInteractive
}

function Read-YesNo {
    param([string]$Question)
    if ($script:IsDry -or -not (Test-Interactive)) { return $false }
    $reply = Read-Host "$Question [y/N]"
    return ($reply -match '^[Yy]')
}

function Remove-Tree {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ($script:IsDry) {
        Show-Action "delete folder $Path"
        return
    }
    Write-Host "  removing $Path"
    try {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
    }
    catch {
        # Long paths or read-only files: let cmd.exe have a go.
        & cmd.exe /c rmdir /s /q "$Path" | Out-Null
    }
    if (Test-Path -LiteralPath $Path) {
        Write-Host "  could not remove $Path completely (a file may be in use); close Nightshift windows and run again." -ForegroundColor Yellow
    }
}

# ---- where is the install -------------------------------------------------------------------------------
$nsHome = $HomeDir
if (-not $nsHome) { $nsHome = [Environment]::GetEnvironmentVariable('NIGHTSHIFT_HOME', 'User') }
if (-not $nsHome) { $nsHome = $env:NIGHTSHIFT_HOME }
if (-not $nsHome) { $nsHome = Join-Path $env:LOCALAPPDATA 'Nightshift' }

Write-Host ''
Write-Host 'Nightshift uninstall' -ForegroundColor Cyan
if ($script:IsDry) { Write-Host 'Dry run: nothing will be changed on this PC.' }
Write-Host "Install folder: $nsHome"

# ---- questions (default No) -----------------------------------------------------------------------------
$wipeModels = [bool]$DeleteModels
if (-not $PSBoundParameters.ContainsKey('DeleteModels')) {
    $wipeModels = Read-YesNo 'Also delete the downloaded AI models (about 15 GB)?'
}
$wipeData = [bool]$DeleteData
if (-not $PSBoundParameters.ContainsKey('DeleteData')) {
    $wipeData = Read-YesNo 'Also delete your videos and history (outbox, data, imports, cache)?'
}

# ---- stop anything still running from the install folder ------------------------------------------------
if (Test-Path -LiteralPath $nsHome) {
    if ($script:IsDry) {
        Show-Action "stop Nightshift processes running from $nsHome"
    }
    else {
        foreach ($p in @(Get-Process -ErrorAction SilentlyContinue)) {
            try {
                $exe = [string]$p.Path
                if ($exe -and $exe.StartsWith($nsHome, [StringComparison]::OrdinalIgnoreCase)) {
                    Write-Host "  stopping $($p.ProcessName)"
                    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
                }
            }
            catch {
                Write-Verbose "cannot inspect process $($p.Id)"
            }
        }
    }
}

# ---- system integration ---------------------------------------------------------------------------------
if (-not $SkipSystem) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        if ($script:IsDry) { Show-Action "remove scheduled task '$TaskName'" }
        else {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "  removed scheduled task '$TaskName'"
        }
    }
    foreach ($desktop in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory'))) {
        if (-not $desktop -or -not (Test-Path -LiteralPath $desktop)) { continue }
        foreach ($lnk in @(Get-ChildItem -LiteralPath $desktop -Filter 'Nightshift*.lnk' -File -ErrorAction SilentlyContinue)) {
            if ($script:IsDry) { Show-Action "delete shortcut $($lnk.FullName)" }
            else {
                Remove-Item -LiteralPath $lnk.FullName -Force
                Write-Host "  removed shortcut $($lnk.Name)"
            }
        }
    }
}

# ---- folders --------------------------------------------------------------------------------------------
Remove-Tree -Path (Join-Path $nsHome 'venvs')
Remove-Tree -Path (Join-Path $nsHome 'bin')
if ($wipeModels) { Remove-Tree -Path (Join-Path $nsHome 'models') }
if ($wipeData) {
    foreach ($d in @('outbox', 'rejected', 'data', 'imports', 'cache')) { Remove-Tree -Path (Join-Path $nsHome $d) }
}

# ---- environment variable (last, so a half-finished run can be repeated) --------------------------------
if (-not $SkipSystem) {
    if ($script:IsDry) { Show-Action 'remove user environment variable NIGHTSHIFT_HOME' }
    else {
        [Environment]::SetEnvironmentVariable('NIGHTSHIFT_HOME', $null, 'User')
        Write-Host '  removed user environment variable NIGHTSHIFT_HOME'
    }
}

# ---- what is left ---------------------------------------------------------------------------------------
Write-Host ''
$kept = @()
foreach ($d in @('models', 'outbox', 'rejected', 'data', 'imports', 'cache', 'config', 'logs')) {
    $full = Join-Path $nsHome $d
    if (Test-Path -LiteralPath $full) { $kept += $d }
}
if ($script:IsDry) {
    Write-Host 'Dry run finished. Nothing was changed.'
}
elseif ($kept.Count -eq 0) {
    Write-Host 'Nightshift is removed. Nothing was kept.'
}
else {
    Write-Host "Kept in ${nsHome}: $($kept -join ', ')"
    Write-Host 'Delete that folder by hand when you no longer need it.'
}
exit 0
