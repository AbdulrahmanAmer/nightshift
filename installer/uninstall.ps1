<#
.SYNOPSIS
    Nightshift uninstaller (component C1, spec 5).

.DESCRIPTION
    Removes the scheduled task, desktop shortcuts, venvs\, bin\, the program folder (app, app.new, app.prev, app.failed, app.swap, app.swap-left-*),
    the user PATH entries of the FFmpeg fallback and of this install's ns and studio commands (<home>\cmd, card
    FN-3; another install's entry is never touched, and the rest of the PATH stays byte for byte), the cmd folder
    and the NIGHTSHIFT_HOME user environment
    variable. models\ and the user data (outbox, rejected, data, imports, cache) are kept unless you answer
    yes (default No) or pass -DeleteModels / -DeleteData. Safe to run more than once.

    A PC has one 'Nightshift Daily' task, one Desktop, one notification registration, one set of stored
    stock-footage keys and one NIGHTSHIFT_HOME, and can hold several Nightshift folders. This script removes
    each of them only when it belongs to the folder it is cleaning, or to a folder that is gone; what
    another Nightshift folder uses (one that exists, or whose drive is not there now) is left, and a line
    starting with "Left" says so. A shortcut or a task it cannot read exactly is left too.

    -HomeDir     folder to clean. Default: the install this script is in (<home>\app\installer). Started
                 from anywhere else (a repository, an extracted zip) it takes the install NIGHTSHIFT_HOME names
                 (then %LOCALAPPDATA%\Nightshift) only when that install runs THIS program folder; otherwise it
                 changes nothing and asks for -HomeDir: the variable may name another Nightshift folder.
    -DesktopDir  the Desktop folder to look in, instead of this user's and the public one.
    -StateFile   with -DryRun only: a JSON file {task: {arguments} or null, userHome, userPath, toastKey, installs,
                 credentials} that stands in for what Windows has, so a dry run reads nothing of this PC (the
                 tests use it; credentials: the names of Nightshift's entries in Credential Manager).
    -DryRun      print "[dry-run] <action>" lines, change nothing, exit 0.
    -SkipSystem  leave the scheduled task, shortcuts and environment variable alone (used by CI).
    -KeepBin     leave bin for the caller to remove after it has exited (the uninstall window itself has
                 loaded binwebview2*.dll, so Windows cannot delete them until the window is gone).
    -Ui          open the setup window for the questions (UNINSTALL.cmd passes it). Falls back to the console
                 questions when no uv Python is found.
#>
[CmdletBinding()]
param(
    [string]$HomeDir = '',
    [switch]$DryRun,
    [switch]$DeleteModels,
    [switch]$DeleteData,
    [switch]$SkipSystem,
    [switch]$KeepBin,
    [switch]$Ui,
    [string]$DesktopDir = '',
    [string]$StateFile = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:IsDry = [bool]$DryRun

# A test run must never change this PC: the real nightly task, the Desktop shortcuts, the notification
# registration, the user PATH and the stored keys are all removed below unless -SkipSystem (or -DryRun) is given.
# The test session sets NS_UNDER_TEST (tests/conftest.py); pytest itself sets PYTEST_CURRENT_TEST. No switch
# turns this off.
if (($env:NS_UNDER_TEST -or $env:PYTEST_CURRENT_TEST) -and -not $SkipSystem -and -not $DryRun) {
    Write-Host 'uninstall.ps1: refused. A test run may only call it with -SkipSystem or -DryRun (it would change this PC).'
    exit 3
}
if ($StateFile -and -not $DryRun) {
    Write-Host 'uninstall.ps1: refused. -StateFile describes a PC for a dry run; it is read with -DryRun only.'
    exit 2
}
$TaskName = 'Nightshift Daily'
$machine = $null
if ($StateFile) { $machine = (Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8) | ConvertFrom-Json }

function Get-MachineValue {
    # One value of the -StateFile description ($null when it does not name it).
    param([string]$Name)
    if ($null -eq $machine) { return $null }
    $found = $machine.PSObject.Properties[$Name]
    if ($null -eq $found) { return $null }
    return $found.Value
}

function Get-UserHomeVariable {
    if ($null -ne $machine) { return [string](Get-MachineValue 'userHome') }
    return [string][Environment]::GetEnvironmentVariable('NIGHTSHIFT_HOME', 'User')
}

function Get-TaskArguments {
    # The arguments of the nightly task's action; $null when Windows has no such task.
    if ($null -ne $machine) {
        $described = Get-MachineValue 'task'
        if ($null -eq $described) { return $null }
        $named = $described.PSObject.Properties['arguments']
        if ($null -eq $named) { return '' }
        return [string]$named.Value
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) { return $null }
    $action = @($task.Actions) | Select-Object -First 1
    if ($null -eq $action) { return '' }
    return [string]$action.Arguments
}

function Test-UnderFolder {
    # True when the file $Path is inside $Folder, by whole path segments: D:\Nightshift2\x.exe is NOT inside
    # D:\Nightshift.
    param([string]$Path, [string]$Folder)
    if (-not $Path -or -not $Folder) { return $false }
    return $Path.StartsWith(($Folder.TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase)
}

function Test-RunsThisProgram {
    # True when the install in $Folder runs the program folder this script is in: its own <folder>\app, or the
    # folder its setup recorded (config\wizard_flags.json, shortcuts_app: a dev install runs a repository, an
    # older install the extracted zip).
    param([string]$Folder)
    if (-not $Folder) { return $false }
    $program = (Split-Path -Parent $PSScriptRoot).TrimEnd('\')
    if ((Join-Path $Folder 'app').TrimEnd('\') -ieq $program) { return $true }
    try {
        $flagFile = Join-Path (Join-Path $Folder 'config') 'wizard_flags.json'
        $flags = (Get-Content -LiteralPath $flagFile -Raw -Encoding UTF8 -ErrorAction Stop) | ConvertFrom-Json
        $made = $flags.PSObject.Properties['shortcuts_app']
        return (($null -ne $made) -and (([string]$made.Value).TrimEnd('\') -ieq $program))
    }
    catch {
        Write-Verbose "no setup record in ${Folder}: $($_.Exception.Message)"
        return $false
    }
}

function Write-NsFileSwap {
    # A file another program reads (installs.txt: another install's setup reads it and writes it back with
    # its own folder added, shortcut_owner.record_home) is either the old one or the new one, never half of
    # either: the text goes to a temp file in the same folder, is flushed, and is then swapped over -Path
    # (File.Replace, or Move for a new file). While another process has -Path open the swap is refused for a
    # moment: retried, the error thrown after the last try, the old file untouched. The same function as in
    # launch.ps1, pick-folder.ps1 and stage1.ps1 (this script loads no other file: it must work when the
    # program folder is gone). UTF-8; -Bom for a file the owner opens in Notepad.
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text,
        [int]$Tries = 20,
        [switch]$Bom
    )
    $full = [System.IO.Path]::GetFullPath($Path)
    $tmp = Join-Path ([System.IO.Path]::GetDirectoryName($full)) ('~ns' + $PID + '-0-' + (Get-Random -Maximum 2000000000) + '.tmp')
    try {
        $encoding = New-Object System.Text.UTF8Encoding([bool]$Bom)
        $bytes = $encoding.GetPreamble() + $encoding.GetBytes($Text)
        $fs = New-Object System.IO.FileStream($tmp, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) }
        finally { $fs.Dispose() }
        for ($attempt = 1; ; $attempt++) {
            try {
                if ([System.IO.File]::Exists($full)) { [System.IO.File]::Replace($tmp, $full, [NullString]::Value) }
                else { [System.IO.File]::Move($tmp, $full) }
                break
            }
            catch [System.IO.IOException], [System.UnauthorizedAccessException] {
                # a sharing or lock violation, or access denied while a reader has the target open
                if ($attempt -ge $Tries) { throw }
                Start-Sleep -Milliseconds ([Math]::Min(10 * $attempt, 100))
            }
        }
    }
    finally {
        try { if ([System.IO.File]::Exists($tmp)) { [System.IO.File]::Delete($tmp) } }
        catch { Write-Verbose ('temp file not removed: ' + $tmp) }
    }
}

function Get-KnownInstalls {
    # The Nightshift folders setup recorded on this PC (%LOCALAPPDATA%\Nightshift\installs.txt, one per line):
    # how an uninstall learns of another install that holds none of the shared things.
    if ($null -ne $machine) { return @(Get-MachineValue 'installs' | Where-Object { $_ } | ForEach-Object { [string]$_ }) }
    $listFile = Join-Path (Join-Path $env:LOCALAPPDATA 'Nightshift') 'installs.txt'
    if (-not (Test-Path -LiteralPath $listFile -PathType Leaf)) { return @() }
    return @(Get-Content -LiteralPath $listFile -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
}

function Read-LinkArguments {
    # The arguments of a shell link, read from its bytes (MS-SHLLINK; the strings are UTF-16, so any folder
    # name is read exactly): the same reading as installer/stage2/shortcut_owner.py. $null when the file is
    # not a shell link that can be read.
    param([string]$Path)
    try {
        $d = [IO.File]::ReadAllBytes($Path)
        if ($d.Length -lt 76 -or [BitConverter]::ToUInt32($d, 0) -ne 76) { return $null }
        if (([BitConverter]::ToString($d, 4, 16) -replace '-', '') -ne '0114020000000000C000000000000046') { return $null }
        $flags = [int64][BitConverter]::ToUInt32($d, 20)
        $at = 76
        if ($flags -band 1) { $at += 2 + [int][BitConverter]::ToUInt16($d, $at) }
        if ($flags -band 2) { $at += [int][BitConverter]::ToUInt32($d, $at) }
        $unicode = [bool]($flags -band 128)
        foreach ($bit in @(4, 8, 16, 32)) {
            if (-not ($flags -band $bit)) { continue }
            $size = [int][BitConverter]::ToUInt16($d, $at)
            if ($unicode) { $size = $size * 2 }
            if ($at + 2 + $size -gt $d.Length) { return $null }
            if ($bit -eq 32) {
                if ($unicode) { return [Text.Encoding]::Unicode.GetString($d, $at + 2, $size) }
                return [Text.Encoding]::Default.GetString($d, $at + 2, $size)
            }
            $at += 2 + $size
        }
        return ''
    }
    catch {
        Write-Verbose "cannot read $Path as a shortcut: $($_.Exception.Message)"
        return $null
    }
}

function Get-LinkHome {
    # The Nightshift folder a shortcut belongs to: the folder of the launch script it starts through
    # (<home>\nightshift-launch.ps1), else the folder above the program folder 'app' of its launcher; a direct
    # shortcut to a launcher of the program folder this script is in belongs to the folder being cleaned.
    # '' when the arguments are not a Nightshift shortcut's.
    param([string]$Arguments)
    try {
        $m = [regex]::Match($Arguments, '-File\s+"([^"]+)"(?:\s+-Launcher\s+"([^"]+)")?\s*$', 'IgnoreCase')
        if (-not $m.Success) { return '' }
        $launcher = $m.Groups[1].Value
        $stub = ''
        if ($m.Groups[2].Value) { $launcher = $m.Groups[2].Value; $stub = $m.Groups[1].Value }
        if (-not $launcher.ToLowerInvariant().EndsWith('.ps1')) { return '' }
        $launchers = [IO.Path]::GetDirectoryName($launcher)
        if ([IO.Path]::GetFileName($launchers) -ine 'launchers') { return '' }
        if ($stub) {
            if ([IO.Path]::GetFileName($stub) -ine 'nightshift-launch.ps1') { return '' }
            return [string][IO.Path]::GetDirectoryName($stub)
        }
        $program = [IO.Path]::GetDirectoryName($launchers)
        if ([IO.Path]::GetFileName($program) -ieq 'app') { return [string][IO.Path]::GetDirectoryName($program) }
        if ($program.TrimEnd('\') -ieq (Split-Path -Parent $PSScriptRoot).TrimEnd('\')) { return $nsHome }
        return ''
    }
    catch {
        Write-Verbose "cannot tell which Nightshift a shortcut opens: $($_.Exception.Message)"
        return ''
    }
}

function Get-FolderState {
    # Whose a folder named by the task, a shortcut or NIGHTSHIFT_HOME is, seen from the folder being cleaned:
    # mine; other (another folder that exists); unreachable (its drive or share is not there now: it may come
    # back); gone (the drive is there and the folder is not: a Nightshift deleted by hand).
    param([string]$Folder)
    $there = $Folder.Trim().TrimEnd('\')
    $here = $nsHome.Trim().TrimEnd('\')
    $root = ''
    try {
        $there = [IO.Path]::GetFullPath($there).TrimEnd('\')
        $here = [IO.Path]::GetFullPath($here).TrimEnd('\')
        $root = [string][IO.Path]::GetPathRoot($there)
    }
    catch { Write-Verbose "cannot normalize $Folder" }
    if ($there -ieq $here) { return 'mine' }
    if (Test-Path -LiteralPath $Folder -PathType Container) { return 'other' }
    if ($root -and (Test-Path -LiteralPath $root)) { return 'gone' }
    return 'unreachable'
}

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

function Remove-UserPathEntry {
    # Takes $Directory out of the user's PATH (setup puts <home>\cmd there, the FFmpeg fallback <home>\bin\ffmpeg).
    # Only the entries that name that folder go (case and one trailing backslash aside); every other byte of the
    # value stays: empty entries, a trailing ';', duplicates, %VAR% entries. The value is read and written
    # unexpanded and keeps its kind (REG_EXPAND_SZ or REG_SZ). A described PC (-StateFile) gives it as 'userPath'.
    # $Key: the user's Environment key by default (opened for writing only when not a dry run), or a stand-in
    # with GetValue, GetValueKind, SetValue and Close (the tests: they never open the registry).
    param([string]$Directory, $Key = $null)
    $wanted = $Directory.Trim().TrimEnd('\')
    $value = $null
    if ($null -ne $Key) { $value = $Key.GetValue('Path', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) }
    elseif ($null -ne $machine) { $value = Get-MachineValue 'userPath' }
    else {
        $Key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', (-not $script:IsDry))
        if ($null -eq $Key) { return $false }
        $value = $Key.GetValue('Path', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    }
    try {
        if ($null -eq $value) { return $false }
        $parts = ([string]$value).Split(';')
        $kept = @($parts | Where-Object { $_.Trim().TrimEnd('\') -ine $wanted })
        if ($kept.Count -eq $parts.Count) { return $false }
        if ($script:IsDry) {
            Show-Action "remove $Directory from the user PATH"
            return $true
        }
        $kind = $Key.GetValueKind('Path')
        $Key.SetValue('Path', ($kept -join ';'), $kind)
        Write-Host "  removed $Directory from the user PATH"
        return $true
    }
    finally { if ($null -ne $Key) { $Key.Close() } }
}

# ---- where is the install -------------------------------------------------------------------------------
$nsHome = $HomeDir
if (-not $nsHome) {
    # UNINSTALL.cmd passes no folder. Run from an install's own program folder (<home>\app) it removes THAT
    # install, whatever NIGHTSHIFT_HOME names: on a PC with two Nightshift folders the variable names one.
    # A NIGHTSHIFT_HOME typed in this window does not change that either ('ns' and 'studio' follow one):
    # removing an install is never done on the word of a variable; -HomeDir is the explicit way.
    $programDir = Split-Path -Parent $PSScriptRoot
    if ((Split-Path -Leaf $programDir) -ieq 'app') {
        $above = Split-Path -Parent $programDir
        if ($above -and (Test-Path -LiteralPath (Join-Path $above 'config') -PathType Container)) { $nsHome = $above }
    }
}
if (-not $nsHome) {
    # Not inside an install (a repository, an extracted zip): the variable is only a hint. It is followed when
    # the install it names runs THIS program folder; an install of another program folder is never offered.
    $named = @(Get-UserHomeVariable)
    if ($null -eq $machine) { $named += @([string]$env:NIGHTSHIFT_HOME, (Join-Path $env:LOCALAPPDATA 'Nightshift')) }
    $named = @($named | Where-Object { $_ })
    foreach ($candidate in $named) {
        if (Test-RunsThisProgram $candidate) { $nsHome = $candidate; break }
    }
    if (-not $nsHome) {
        Write-Host ''
        Write-Host 'Nightshift uninstall: nothing was changed.' -ForegroundColor Yellow
        Write-Host "This copy of the program ($(Split-Path -Parent $PSScriptRoot)) is not inside an install."
        if ($named.Count -gt 0) {
            Write-Host "NIGHTSHIFT_HOME names $($named[0]), and that Nightshift does not run this copy, so it is not removed from here."
            Write-Host "To remove that one, run UNINSTALL.cmd from its own program folder: $(Join-Path $named[0] 'app')"
        }
        Write-Host 'To remove another install, name its folder:'
        Write-Host "  `"$(Join-Path (Split-Path -Parent $PSScriptRoot) 'UNINSTALL.cmd')`" -HomeDir <its folder>"
        exit 2
    }
}

Write-Host ''
Write-Host 'Nightshift uninstall' -ForegroundColor Cyan
if ($script:IsDry) { Write-Host 'Dry run: nothing will be changed on this PC.' }
Write-Host "Install folder: $nsHome"

if ($Ui -and -not $script:IsDry) {
    $uvCandidates = @()
    $cmd = Get-Command 'uv.exe' -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { $uvCandidates += $cmd.Source }
    $uvCandidates += (Join-Path $env:LOCALAPPDATA 'Nightshift\bootstrap\uv\uv.exe')
    $uvCandidates += (Join-Path $nsHome 'bin\uv\uv.exe')
    $appRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    foreach ($uvExe in $uvCandidates) {
        if (-not (Test-Path -LiteralPath $uvExe)) { continue }
        $py = $null
        try { $py = ((& $uvExe python find 3.12 | Out-String).Trim().Split("`n")[0]).Trim() } catch { $py = $null }
        if ($py -and (Test-Path -LiteralPath $py)) {
            $env:PYTHONUTF8 = '1'
            $env:PYTHONPATH = $appRoot
            Push-Location $appRoot
            try {
                & $py -X utf8 -m installer.stage2.web --uninstall --app-path $appRoot --home $nsHome
                exit $LASTEXITCODE
            }
            finally { Pop-Location }
        }
    }
    Write-Host 'The setup window is not available (no Python found); asking here instead.'
}

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
                if (Test-UnderFolder -Path $exe -Folder $nsHome) {
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
# The other Nightshift folders found to use what this PC has one of (the task, a Desktop shortcut,
# NIGHTSHIFT_HOME): while there is one, the notification registration and the stored keys stay too.
$others = New-Object System.Collections.Generic.List[string]
$varState = 'none'
$userHome = ''
$stillHere = New-Object System.Collections.Generic.List[string]     # other installs setup recorded on this PC
if (-not $SkipSystem) {
    foreach ($known in @(Get-KnownInstalls)) {
        if ((Get-FolderState $known) -ne 'other') { continue }
        if (-not (Test-Path -LiteralPath (Join-Path (Join-Path $known 'config') 'runtime.json') -PathType Leaf)) { continue }
        if (-not $stillHere.Contains($known)) { $stillHere.Add($known) }
        if (-not $others.Contains($known)) { $others.Add($known) }
    }
    $taskArgs = Get-TaskArguments
    if ($null -ne $taskArgs) {
        $named = [regex]::Match($taskArgs, 'NIGHTSHIFT_HOME=([^"]+)"')
        $taskHome = ''
        if ($named.Success) { $taskHome = $named.Groups[1].Value.Trim() }
        $taskState = 'unknown'
        if ($taskHome) { $taskState = Get-FolderState $taskHome }
        if ($taskState -eq 'mine' -or $taskState -eq 'gone') {
            if ($script:IsDry) { Show-Action "remove scheduled task '$TaskName'" }
            else {
                Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
                Write-Host "  removed scheduled task '$TaskName'"
            }
        }
        elseif ($taskState -eq 'unknown') {
            Write-Host "Left '$TaskName': it does not say which Nightshift it runs; delete it in Task Scheduler if it is not needed."
        }
        else {
            if (-not $others.Contains($taskHome)) { $others.Add($taskHome) }
            $away = ''
            if ($taskState -eq 'unreachable') { $away = ' (its drive is not there now)' }
            Write-Host "Left '$TaskName': it runs the Nightshift at $taskHome$away."
        }
    }
    # config\schedule.json is Nightshift's record of that task ("Automatic runs are on"). With "keep my data"
    # the config folder stays, so the record goes here, with the task: left behind, the next setup and every
    # page would read Automatic runs as on with no task in Windows. The run time stays in config\answers.json
    # and config\dashboard.json, and the next setup asks its question again.
    if ($nsHome) {
        $scheduleRecord = Join-Path (Join-Path $nsHome 'config') 'schedule.json'
        if (Test-Path -LiteralPath $scheduleRecord) {
            if ($script:IsDry) { Show-Action "delete $scheduleRecord" }
            else { Remove-Item -LiteralPath $scheduleRecord -Force -ErrorAction SilentlyContinue; Write-Host "  removed $scheduleRecord" }
        }
    }
    # The FFmpeg fallback put its folder on the user PATH; the environment variable change below broadcasts it.
    [void](Remove-UserPathEntry -Directory (Join-Path (Join-Path $nsHome 'bin') 'ffmpeg'))
    # Setup put this install's command folder (ns.cmd, studio.cmd) on the user PATH (card FN-3). Only that entry
    # goes: another install's command folder is that install's.
    $commandsGone = [bool](Remove-UserPathEntry -Directory (Join-Path $nsHome 'cmd'))
    $desktops = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory'))
    if ($DesktopDir) { $desktops = @($DesktopDir) }
    $leftFor = New-Object System.Collections.Specialized.OrderedDictionary
    foreach ($desktop in $desktops) {
        if (-not $desktop -or -not (Test-Path -LiteralPath $desktop)) { continue }
        foreach ($lnk in @(Get-ChildItem -LiteralPath $desktop -Filter 'Nightshift*.lnk' -File -ErrorAction SilentlyContinue)) {
            # whose shortcut: only one that opens this folder, or a folder that is gone, is removed
            $linkArgs = Read-LinkArguments $lnk.FullName
            $linkHome = ''
            $linkState = 'unread'
            if ($null -ne $linkArgs) {
                $linkHome = Get-LinkHome $linkArgs
                $linkState = 'foreign'
                if ($linkHome) { $linkState = Get-FolderState $linkHome }
            }
            if ($linkState -eq 'mine' -or $linkState -eq 'gone') {
                if ($script:IsDry) { Show-Action "delete shortcut $($lnk.FullName)" }
                else {
                    Remove-Item -LiteralPath $lnk.FullName -Force
                    Write-Host "  removed shortcut $($lnk.Name)"
                }
            }
            elseif ($linkState -eq 'other' -or $linkState -eq 'unreachable') {
                if (-not $others.Contains($linkHome)) { $others.Add($linkHome) }
                if ($leftFor.Contains($linkHome)) { $leftFor[$linkHome] = [int]$leftFor[$linkHome] + 1 } else { $leftFor.Add($linkHome, 1) }
            }
            elseif ($linkState -eq 'unread') {
                Write-Host "Left the Desktop shortcut $($lnk.FullName): it could not be read, so it is not known which Nightshift it opens."
            }
            else {
                Write-Host "Left the Desktop shortcut $($lnk.FullName): it is not one this Nightshift made."
            }
        }
        # the note the launch script leaves when a nightly run could not start (installer/launch.ps1); a
        # file of that name that does not start with the note's own first words is not Nightshift's
        $missed = Join-Path $desktop 'Nightshift did not run.txt'
        if (Test-Path -LiteralPath $missed -PathType Leaf) {
            $firstLine = [string](Get-Content -LiteralPath $missed -TotalCount 1 -ErrorAction SilentlyContinue)
            if ($firstLine.StartsWith('Nightshift could not start its nightly run on ')) {
                if ($script:IsDry) { Show-Action "delete $missed" }
                else {
                    Remove-Item -LiteralPath $missed -Force -ErrorAction SilentlyContinue
                    Write-Host "  removed $missed"
                }
            }
        }
    }
    foreach ($folder in @($leftFor.Keys)) {
        if ([int]$leftFor[$folder] -eq 1) { Write-Host "Left 1 Desktop shortcut: it opens the Nightshift at $folder." }
        else { Write-Host "Left $($leftFor[$folder]) Desktop shortcuts: they open the Nightshift at $folder." }
    }
    # NIGHTSHIFT_HOME is decided here and removed last (below): it counts as a user of the shared things too.
    $userHome = (Get-UserHomeVariable).Trim()
    if ($userHome) { $varState = Get-FolderState $userHome }
    if (($varState -eq 'other' -or $varState -eq 'unreachable') -and -not $others.Contains($userHome)) { $others.Add($userHome) }
    # The toast identity the installer registered (HKCU, no admin needed): one for the PC, so it stays while
    # another Nightshift folder was found above.
    $aumidKey = 'HKCU:\Software\Classes\AppUserModelId\Nightshift'
    $toastKey = $false
    if ($null -ne $machine) { $toastKey = [bool](Get-MachineValue 'toastKey') } else { $toastKey = Test-Path -LiteralPath $aumidKey }
    if ($toastKey) {
        if ($others.Count -gt 0) { Write-Host "Left the notification registration: the Nightshift at $($others[0]) uses it." }
        elseif ($script:IsDry) { Show-Action "remove toast registration $aumidKey" }
        else {
            Remove-Item -LiteralPath $aumidKey -Recurse -Force
            Write-Host '  removed the notification registration'
        }
    }
    # (the stored stock-footage keys follow below, by the same rule)
}

# ---- the stock-footage keys -----------------------------------------------------------------------------
# A key is kept in two places: Windows Credential Manager (one per Windows account, so every Nightshift folder
# of this account shares it) and config\secrets.dpapi (this folder's own). A key left behind made the next
# setup on this PC fail (it validated the stale key and stopped). What happened to them is said by the one
# function that knows both places (installer.stage2.secrets, forget_stock_keys), run with this install's own
# Python before its environment goes below; its lines are printed as they are. Without that Python, or when it
# cannot reach Credential Manager, this script looks with cmdkey itself and its lines say so.
$secretsFile = Join-Path (Join-Path $nsHome 'config') 'secrets.dpapi'
$KeysByHand = 'open Control Panel > Credential Manager > Windows Credentials and remove every entry whose name ends in Nightshift'

function Get-NightshiftCredential {
    # The Credential Manager entries keyring makes for Nightshift ('Nightshift' and '<key name>@Nightshift'):
    # @{ Ok; Entries (Target to delete, Name shown); Problem }. A described PC (-StateFile) lists them as
    # 'credentials'; a test run without one reads nothing of this PC. cmdkey's labels are in the language of
    # Windows, so a line is matched by the entry's name at its end, never by its label.
    if ($null -ne $machine) {
        $listed = @(Get-MachineValue 'credentials' | Where-Object { $_ } | ForEach-Object { [pscustomobject]@{ Target = [string]$_; Name = [string]$_ } })
        return @{ Ok = $true; Entries = $listed; Problem = '' }
    }
    if ($env:NS_UNDER_TEST -or $env:PYTEST_CURRENT_TEST) { return @{ Ok = $false; Entries = @(); Problem = 'a test run reads nothing of this PC' } }
    $saved = $ErrorActionPreference
    $lines = @()
    $code = 0
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(& cmdkey.exe /list 2>$null)
        $code = $LASTEXITCODE
    }
    catch { return @{ Ok = $false; Entries = @(); Problem = "cmdkey could not run: $($_.Exception.Message)" } }
    finally { $ErrorActionPreference = $saved }
    if ($code -ne 0) { return @{ Ok = $false; Entries = @(); Problem = "cmdkey /list stopped with exit code $code" } }
    $found = @()
    foreach ($line in $lines) {
        $token = @(([string]$line).Trim() -split '\s+')[-1]
        $name = $token
        $at = $token.ToLowerInvariant().LastIndexOf('target=')
        if ($at -ge 0) { $name = $token.Substring($at + 7) }
        if ($name -match '^(?:[A-Za-z0-9_]+@)?Nightshift$') { $found += [pscustomobject]@{ Target = $token; Name = $name } }
    }
    return @{ Ok = $true; Entries = $found; Problem = '' }
}

function Remove-NightshiftCredential {
    # Credential Manager without Python: cmdkey removes each entry, and a second look says what is gone.
    param([string]$Why)
    $look = Get-NightshiftCredential
    if (-not $look.Ok) {
        Write-Host "  could not check the stock-footage keys in Credential Manager ($Why; $($look.Problem)): $KeysByHand"
        return
    }
    $entries = @($look.Entries)
    if ($entries.Count -eq 0) {
        if ($script:IsDry) { Show-Action "no stock-footage keys found in Credential Manager ($Why)" }
        else { Write-Host "  no stock-footage keys found in Credential Manager ($Why)" }
        return
    }
    $names = ($entries | ForEach-Object { $_.Name }) -join ', '
    if ($script:IsDry) { Show-Action "remove from Credential Manager with cmdkey: $names ($Why)"; return }
    $saved = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        foreach ($entry in $entries) {
            & cmdkey.exe "/delete:$($entry.Target)" 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0 -and $entry.Target -ne $entry.Name) { & cmdkey.exe "/delete:$($entry.Name)" 2>$null | Out-Null }
        }
    }
    catch { Write-Verbose "cmdkey: $($_.Exception.Message)" }
    finally { $ErrorActionPreference = $saved }
    $after = Get-NightshiftCredential
    if (-not $after.Ok) {
        Write-Host "  asked cmdkey to remove $names from Credential Manager and could not look again ($Why; $($after.Problem)): if they are still listed, $KeysByHand"
        return
    }
    $still = @($after.Entries | ForEach-Object { $_.Name })
    $gone = @($entries | Where-Object { $still -notcontains $_.Name } | ForEach-Object { $_.Name })
    $left = @($entries | Where-Object { $still -contains $_.Name } | ForEach-Object { $_.Name })
    if ($gone.Count -gt 0) { Write-Host "  removed from Credential Manager with cmdkey: $($gone -join ', ') ($Why)" }
    if ($left.Count -gt 0) { Write-Host "  could not remove from Credential Manager with cmdkey: $($left -join ', ') ($Why): $KeysByHand" }
}

function Remove-SecretsFile {
    # This folder's copy of the keys, when no Python said what became of it.
    if (-not (Test-Path -LiteralPath $secretsFile -PathType Leaf)) { return }
    if ($script:IsDry) { Show-Action "delete $secretsFile"; return }
    try {
        Remove-Item -LiteralPath $secretsFile -Force -ErrorAction Stop
        Write-Host "  removed $secretsFile (where nightly runs keep a copy of the stock-footage keys)"
    }
    catch { Write-Host "  could not remove $secretsFile ($($_.Exception.Message)): delete it by hand, it may still hold stock-footage keys" }
}

function Invoke-KeysProgram {
    # installer.stage2.secrets with this install's Python: @{ Lines; Code } (Code -1: it could not start).
    param([string]$Python, [string[]]$Arguments)
    $saved = $ErrorActionPreference
    $encoding = $null
    try {
        $ErrorActionPreference = 'Continue'
        try { $encoding = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false }
        catch { $encoding = $null }
        $out = @(& $Python -I -X utf8 -m installer.stage2.secrets forget @Arguments 2>$null)
        return @{ Lines = $out; Code = $LASTEXITCODE }
    }
    catch { return @{ Lines = @(); Code = -1 } }
    finally {
        if ($null -ne $encoding) { try { [Console]::OutputEncoding = $encoding } catch { Write-Verbose 'console encoding kept' } }
        $ErrorActionPreference = $saved
    }
}

if ($SkipSystem) { Remove-SecretsFile }     # Credential Manager is part of this PC: left alone
else {
    $keysPython = Join-Path $nsHome 'venvs\supervisor\Scripts\python.exe'
    $usedBy = ''
    if ($others.Count -gt 0) { $usedBy = $others[0] }
    $why = 'this install has no Python environment to look with'
    $saidFile = $false
    $vaultAsked = $false
    if (Test-Path -LiteralPath $keysPython -PathType Leaf) {
        $keysArgs = @('--home', $nsHome)
        if ($script:IsDry) { $keysArgs += '--dry-run' }
        if ($usedBy) { $keysArgs += @('--used-by', $usedBy) }
        $said = Invoke-KeysProgram -Python $keysPython -Arguments $keysArgs
        foreach ($line in @($said.Lines)) { Write-Host ([string]$line) }
        if ($said.Code -eq 0) { $vaultAsked = $true; $saidFile = $true }
        elseif ($said.Code -eq 4) { $why = "this install's Python has no keyring package"; $saidFile = $true }
        elseif ($said.Code -eq -1) { $why = "this install's Python could not start" }
        else { $why = "this install's Python stopped with exit code $($said.Code)" }
    }
    if (-not $vaultAsked) {
        if ($usedBy) { Write-Host "Left Credential Manager as it is (not looked into: $why): any stock-footage keys in it are used by the Nightshift at $usedBy." }
        else { Remove-NightshiftCredential -Why $why }
    }
    if (-not $saidFile) { Remove-SecretsFile }
}

# The install state describes the program being removed; the next setup must run its steps again (the
# answers and channels stay in config). The window trusted this file and showed Done over an empty folder.
# wizard_flags.json is the same record in smaller print (venvs_installed, smoke: passed, the shortcuts, the
# nightly task): kept, it made the next setup skip its venvs and its smoke test over empty folders (D-15).
foreach ($leaf in @('install_state.json', 'install_state.json.bak', 'wizard_flags.json')) {
    $stateFile = Join-Path (Join-Path $nsHome 'config') $leaf
    if (Test-Path -LiteralPath $stateFile) {
        if ($script:IsDry) { Show-Action "delete $stateFile" }
        else { Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue; Write-Host "  removed $stateFile" }
    }
}

# ---- folders --------------------------------------------------------------------------------------------
Remove-Tree -Path (Join-Path $nsHome 'venvs')
# This install's own set of shortcuts (setup makes it; the Desktop's may be another install's).
Remove-Tree -Path (Join-Path $nsHome 'Shortcuts')
# The update worker's private copy of the supervisor venv (150-300 MB after any update) lives in run, under updater.
Remove-Tree -Path (Join-Path (Join-Path $nsHome 'run') 'updater')
if ($KeepBin) { Write-Host '  bin and run are removed when the uninstall window closes' }
else {
    Remove-Tree -Path (Join-Path $nsHome 'bin')
    Remove-Tree -Path (Join-Path $nsHome 'run')
    # The program folder setup copied (and the swap folders of an update). With -KeepBin the window runs from
    # it, so the deferred cleanup of the window removes these.
    # app.failed and app.swap are what an interrupted `ns update` leaves beside the program folder;
    # app.prev-config.zip is the copy of the settings files an update keeps for `ns update --rollback`
    # cmd: the ns and studio command files (card FN-3), which start this program
    foreach ($leaf in @('app', 'app.new', 'app.prev', 'app.prev-config.zip', 'app.prev-config.zip.unrestored', 'app.prev-config.zip.lock', 'app.rollback-pending.json', 'app.failed', 'app.swap', 'nightshift-launch.ps1', 'cmd')) { Remove-Tree -Path (Join-Path $nsHome $leaf) }
    # app.swap-left-<stamp>: a third program folder a start set aside after an interrupted rollback
    # (ns_common/rollback_intent.py settle_swap); nightshift-launch.ps1 above is what the shortcuts start
    foreach ($left in @(Get-ChildItem -LiteralPath $nsHome -Directory -Filter 'app.swap-left-*' -ErrorAction SilentlyContinue)) { Remove-Tree -Path $left.FullName }
    # app.rollback-settings-<stamp>.zip: the settings a stale rollback record held (rollback_intent.recover).
    # run\launch-missed.json and run\launch-reach.json (the launch script's count, the reach's marker) go with run,
    # and so do run\venv-repair.json and run\update.lock (an unfinished sync's record, the update lock: D-39b).
    foreach ($kept in @(Get-ChildItem -LiteralPath $nsHome -File -Filter 'app.rollback-settings-*.zip' -ErrorAction SilentlyContinue)) { Remove-Tree -Path $kept.FullName }
}
if ($wipeModels) { Remove-Tree -Path (Join-Path $nsHome 'models') }
if (-not $SkipSystem) {
    # With -KeepBin the window that runs this may be using the browser profile and webview2: those go with bin.
    # venvs (the setup environment), run, state and tmp under %LOCALAPPDATA%\Nightshift were left behind by the
    # first dev-mode uninstall; with -KeepBin the window still runs from that venv, so only bootstrap goes then.
    $leaves = @('bootstrap', 'browser', 'webview2', 'venvs', 'run', 'state', 'tmp')
    if ($KeepBin) { $leaves = @('bootstrap') }
    $setupRoot = Join-Path $env:LOCALAPPDATA 'Nightshift'
    if ($others.Count -gt 0) {
        # setup's own files (its Python environment, the window's browser profile) serve every install here
        if (Test-Path -LiteralPath $setupRoot) { Write-Host "Left setup's files in ${setupRoot}: the Nightshift at $($others[0]) uses them for its own setup and repair." }
    }
    else {
        foreach ($leaf in $leaves) { Remove-Tree -Path (Join-Path $setupRoot $leaf) }
    }
}
foreach ($d in @('state', 'tmp')) { Remove-Tree -Path (Join-Path $nsHome $d) }    # internal, never user data
if ($wipeData) {
    foreach ($d in @('outbox', 'rejected', 'data', 'imports', 'cache', 'stories')) { Remove-Tree -Path (Join-Path $nsHome $d) }
}

# ---- environment variable (last, so a half-finished run can be repeated) --------------------------------
if (-not $SkipSystem) {
    if ($varState -eq 'mine' -or $varState -eq 'gone') {
        if ($script:IsDry) { Show-Action 'remove user environment variable NIGHTSHIFT_HOME' }
        else {
            [Environment]::SetEnvironmentVariable('NIGHTSHIFT_HOME', $null, 'User')
            Write-Host '  removed user environment variable NIGHTSHIFT_HOME'
        }
    }
    elseif ($varState -ne 'none') {
        Write-Host "Left NIGHTSHIFT_HOME: it names the Nightshift at $userHome."
    }
    # Another install that held none of the shared things: say that it is there and what it needs now.
    foreach ($known in $stillHere) {
        if ($known.TrimEnd('\') -ieq $userHome.TrimEnd('\')) { continue }
        Write-Host "Another Nightshift is still installed at ${known}. NIGHTSHIFT_HOME does not name it; its shortcuts in $(Join-Path $known 'Shortcuts') work, and so do its commands by their full path: $(Join-Path (Join-Path $known 'cmd') 'ns.cmd') and studio.cmd. To make the Desktop shortcuts, NIGHTSHIFT_HOME, the notification icon and the plain 'ns' and 'studio' commands its own, run INSTALL.cmd in $(Join-Path $known 'app') (or from its zip), choose that folder and Repair."
    }
    # The plain commands were this install's: say how another install here gets them (its Repair puts its own
    # command folder on the PATH; nothing of another install is put there from here).
    if ($commandsGone -and $others.Count -gt 0) {
        $next = $others[0]
        Write-Host "The plain 'ns' and 'studio' commands opened this install and are no longer on your PATH. The Nightshift at $next has its own: $(Join-Path (Join-Path $next 'cmd') 'ns.cmd') and studio.cmd. To make the plain commands open it, run INSTALL.cmd in $(Join-Path $next 'app') (or from its zip), choose that folder and Repair."
    }
    if ($stillHere.Count -eq 0 -and $others.Count -eq 0 -and ($varState -eq 'mine' -or $varState -eq 'gone')) {
        Write-Host 'If another Nightshift folder is on this PC (one installed by an older version is not listed), run its INSTALL.cmd and choose Repair once: that registers its notifications, its stock-footage keys and NIGHTSHIFT_HOME again.'
    }
    # this folder leaves the PC's list of installs; another install's setup may read the list at this moment
    # and write it back with its own folder added, so the list is swapped in whole (a half-written list read
    # there would lose the other folders for good)
    $listFile = Join-Path (Join-Path $env:LOCALAPPDATA 'Nightshift') 'installs.txt'
    if (-not $script:IsDry -and $null -eq $machine -and (Test-Path -LiteralPath $listFile -PathType Leaf)) {
        $rest = @(Get-Content -LiteralPath $listFile -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -and ((Get-FolderState $_) -ne 'mine') })
        $listText = ''
        if ($rest.Count -gt 0) { $listText = ($rest -join "`n") + "`n" }
        try { Write-NsFileSwap -Path $listFile -Text $listText }
        catch { Write-Host "Could not take this folder out of the list of installs in ${listFile}: $($_.Exception.Message)" }
    }
}

# ---- what is left ---------------------------------------------------------------------------------------
Write-Host ''
$kept = @()
foreach ($d in @('models', 'outbox', 'rejected', 'data', 'imports', 'cache', 'stories', 'config', 'logs')) {
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
