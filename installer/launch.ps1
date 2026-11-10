# STUB_SCHEMA: 4
param(
    [string]$Launcher = '',
    [switch]$Nightly,
    [string]$Studio = '',
    [string]$DesktopDir = '',
    [switch]$Cleanup
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# What the Desktop shortcuts (-Launcher <script>) and the nightly task (-Nightly -Studio <studio.exe>) start.
# Setup and the first start of a new version copy this file to <home>\nightshift-launch.ps1, BESIDE the
# program folder (<home>\app), so it is still there when the program folder is not: an update or a rollback
# that lost power between two folder renames leaves no <home>\app, and then no launcher, no ns command and
# no Dashboard can start (they all live in, or import from, that folder). This file needs nothing but
# Windows PowerShell 5.1. With the program there it runs the launcher, or the nightly run, and passes the
# exit code on. Without it:
#   a shortcut  - the window stays open and says in plain words what to do;
#   the task    - nobody is at the screen (and a task that runs signed out has no screen: session 0), so
#                 the missed night is counted in <home>\run\launch-missed.json FIRST, then written to the
#                 Desktop as 'Nightshift did not run.txt', shown as a toast when there is a session to show
#                 it in, and logged as one NS-W209 line in <home>\logs\launch-<yyyymmdd>.jsonl.
# The first start that finds the program again leaves one morning note for the Dashboard, removes the
# Desktop note and the count: a shortcut and the nightly task do it here before they run anything, and the
# engine does it at every start by running this file with -Cleanup (setup's Open dashboard, ns up and the
# restart after an update start the engine without this file). Whoever comes first holds the count's file
# open while it works; the others find it taken or gone, so one night is cleared once.
# A dev install and a test run never touch the real Desktop: no note is written or removed there unless
# -DesktopDir names the folder. The line above the param block is this file's format number: a copy in the
# home is only replaced by one with the same or a higher number.
$nsHome = $PSScriptRoot
if ($Launcher) { $app = Split-Path -Parent (Split-Path -Parent $Launcher) } else { $app = Join-Path $nsHome 'app' }
$swap = $app + '.swap'
$inv = [Globalization.CultureInfo]::InvariantCulture
$today = [DateTime]::Now.ToString('yyyy-MM-dd', $inv)
$utf8 = New-Object System.Text.UTF8Encoding($false)
$missedFile = Join-Path (Join-Path $nsHome 'run') 'launch-missed.json'
$noteName = 'Nightshift did not run.txt'
$noteStart = 'Nightshift could not start its nightly run on '
# A test run never shows a toast and never names the real Desktop: only -DesktopDir counts there.
$underTest = [bool]($env:NS_UNDER_TEST -or $env:PYTEST_CURRENT_TEST)
# A dev install (config\runtime.json says dev_mode) never names the real Desktop either.
$devInstall = $false
try {
    $rt = [IO.File]::ReadAllText((Join-Path (Join-Path $nsHome 'config') 'runtime.json')) | ConvertFrom-Json
    $devInstall = [bool]($rt.PSObject.Properties['dev_mode'] -and $rt.dev_mode -eq $true)
} catch { $devInstall = $false }

function Get-MoveBack {
    # With a rollback's record and no app.swap, somebody moved the folder: setup would start a fresh one.
    $record = $app + '.rollback-pending.json'
    if ((Test-Path -LiteralPath $record -PathType Leaf) -and -not (Test-Path -LiteralPath $swap -PathType Container)) {
        return ('If you moved the folder app or app.swap somewhere else, move it back to ' + $swap +
            ' before you run INSTALL.cmd: otherwise setup starts a fresh program folder.')
    }
    return ''
}

function Write-NsFileSwap {
    # A file another program reads (the count, the Dashboard's notes, the note on the Desktop) is either the
    # old one or the new one, never half of either: the text goes to a temp file in the same folder, is
    # flushed, and is then swapped over -Path (File.Replace, or Move for a new file). While another process
    # has -Path open the swap is refused for a moment: retried, the error thrown after the last try, the old
    # file untouched. The same function as in pick-folder.ps1 and stage1.ps1 (this script loads no other
    # file: it must work when the program folder is gone). UTF-8; -Bom for a file the owner opens in Notepad.
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

function Read-Missed {
    # -Stream: the count's file as its holder has it open (Restore-AfterMiss); else read by its path.
    param($Stream = $null)
    try {
        if ($null -ne $Stream) { $text = (New-Object System.IO.StreamReader($Stream)).ReadToEnd() }
        else { $text = [IO.File]::ReadAllText($missedFile) }
        $r = $text | ConvertFrom-Json
        $m = @{ first = [string]$r.first; last = [string]$r.last; nights = [int]$r.nights; what = [string]$r.what }
        $day = '^\d{4}-\d\d-\d\d$'
        if ($m.first -match $day -and $m.last -match $day -and $m.nights -ge 1 -and @('folder', 'environment') -contains $m.what) { return $m }
    } catch { }
    return $null
}

function Get-NoteFolder {
    if ($DesktopDir) { return $DesktopDir }
    if ($underTest -or $devInstall) { return $null }
    return [Environment]::GetFolderPath('Desktop')
}

function Test-OurNote {
    param([string]$File)
    $first = [string]([IO.File]::ReadAllLines($File) | Select-Object -First 1)
    return $first.StartsWith($noteStart)
}

function Get-Nights {
    param([int]$Count)
    if ($Count -eq 1) { return '1 night' }
    return ([string]$Count + ' nights')
}

function Get-NoteLines {
    param($Missed)
    $head = $noteStart + $Missed.last + ' (' + (Get-Nights $Missed.nights) + ' so far, the first on ' + $Missed.first + '). (NS-W209)'
    $lines = @($head, '')
    if ($Missed.what -eq 'environment') {
        $thing = 'program environment'
        $lines += 'Its program environment is missing or incomplete (not found: ' + $Studio + ').'
    } else {
        $thing = 'program folder'
        $lines += 'Its program folder ' + $app + ' is missing or incomplete.'
        $lines += 'This happens when an update or a rollback was interrupted, for example by a power cut.'
        if (Test-Path -LiteralPath $swap -PathType Container) {
            $lines += 'The program is waiting in ' + $swap + ' and nothing in it was deleted.'
        }
    }
    $lines += @('', 'What to do:',
        '  1. Close every Nightshift window and any Explorer window that shows the Nightshift folder.',
        '  2. Run INSTALL.cmd from a Nightshift zip of version 0.1.15 or later and choose the same install',
        ('     folder (' + $nsHome + '). Setup puts the ' + $thing + ' back.'),
        '  3. Nothing else: the next night runs by itself, and this file is removed the next time Nightshift starts.')
    $back = Get-MoveBack
    if ($back) { $lines += @('', $back) }
    $lines += @('', "To stop the nightly attempts until then, disable 'Nightshift Daily' in Task Scheduler.")
    return $lines
}

function Show-Toast {
    param([string]$Title, [string]$Body)
    if ($underTest) { return 'test-run' }
    try {
        if ((Get-Process -Id $PID).SessionId -eq 0) { return 'session-0' }
        [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
        [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
        $tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $text = $tpl.GetElementsByTagName('text')
        [void]$text.Item(0).AppendChild($tpl.CreateTextNode($Title))
        [void]$text.Item(1).AppendChild($tpl.CreateTextNode($Body))
        $toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Nightshift').Show($toast)
        return 'shown'
    } catch { return ('failed: ' + $_.Exception.Message) }
}

function Open-NsNotesLock {
    # The one lock of the Dashboard's notes: byte 0 of the file morning_note.lock in <home>\run, locked by
    # Windows. The program takes the same byte (ns_supervisor/notify.py, through msvcrt.locking), so its
    # writers and this script never change the notes at the same time. The lock belongs to the open file:
    # when its holder ends or is killed, Windows drops it, so the file that stays on disk never keeps anybody
    # out. Returns the open file (Dispose it to release), or nothing when the lock was not free in -WaitMs.
    param([Parameter(Mandatory = $true)][string]$RunDir, [int]$WaitMs = 5000)
    $until = [DateTime]::UtcNow.AddMilliseconds($WaitMs)
    while ($true) {
        $file = $null
        try {
            [void][IO.Directory]::CreateDirectory($RunDir)
            $file = New-Object System.IO.FileStream((Join-Path $RunDir 'morning_note.lock'), [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
            $file.Lock(0, 1)
            return $file
        } catch {
            if ($null -ne $file) { $file.Dispose() }
            if ([DateTime]::UtcNow -ge $until) { return $null }
            Start-Sleep -Milliseconds 100
        }
    }
}

function Add-NsMorningNote {
    # The only place this script touches the Dashboard's notes (the program's side is ns_supervisor/notify.py,
    # which owns the file; tests/test_morning_notes_owner.py fails on any other writer). One read-modify-write
    # under the notes lock: read, skip when a waiting note already says it (its message starts with -OnceStart
    # and contains -OnceHas), add, swap the file. No waiting note is dropped to make room. The file keeps the
    # format 0.1.14 reads: UTF-8 without a byte order mark, { notes: [ { at, title, message } ] }.
    # A damaged file is never emptied: it is first copied to a file of the same name plus .bad-<its time>,
    # then replaced by what could be read of it plus a note that says so (notify.damaged_message's words).
    # Returns true when the note was added and false when it was said already; throws when it could not be
    # written (the lock not free in 5 seconds, the notes full, the file not readable or writable): then
    # nothing was changed and the caller keeps what it wanted to say for the next start.
    param([Parameter(Mandatory = $true)][string]$Message, [string]$OnceStart = '', [string]$OnceHas = '')
    $run = Join-Path $nsHome 'run'
    $notesFile = Join-Path $run 'morning_note.json'
    $lock = Open-NsNotesLock -RunDir $run
    if ($null -eq $lock) { throw 'the notes are being written by another program (waited 5 seconds); nothing was changed' }
    try {
        $notes = @()
        $damaged = $false
        if (Test-Path -LiteralPath $notesFile -PathType Leaf) {
            $text = [IO.File]::ReadAllText($notesFile)
            $read = $null
            try { $read = $text | ConvertFrom-Json } catch { $damaged = $true }
            if (-not $damaged) {
                if (($read -is [System.Management.Automation.PSCustomObject]) -and $read.PSObject.Properties['notes'] -and ($read.notes -is [array])) {
                    foreach ($n in $read.notes) {
                        if ($n -is [System.Management.Automation.PSCustomObject]) { $notes += $n } else { $damaged = $true }
                    }
                } else { $damaged = $true }
            }
        }
        $now = [DateTime]::Now.ToString('s')
        if ($damaged) {
            $kept = $notesFile + '.bad-' + [IO.File]::GetLastWriteTime($notesFile).ToString('yyyyMMdd-HHmmss', $inv)
            [IO.File]::Copy($notesFile, $kept, $true)
            $notes += [ordered]@{ at = $now; title = 'Nightshift'; message = 'Nightshift could not read its waiting notes: the file that holds them was damaged. The notes that could still be read are listed here. The damaged file was kept as ' + $kept + '; open it in Notepad to see what else it held.' }
        }
        $said = @($notes | Where-Object {
                $OnceStart -and ($_ -is [System.Management.Automation.PSCustomObject]) -and $_.PSObject.Properties['message'] -and ([string]$_.message).StartsWith($OnceStart) -and ([string]$_.message).Contains($OnceHas)
            })
        $add = ($said.Count -eq 0)
        # the program refuses new notes at 200 waiting and says so in the notes; this script stops one short
        if ($add -and @($notes).Count -ge 199) { throw 'the notes are full (199 or more wait unread); nothing was changed' }
        if ($add) { $notes += [ordered]@{ at = $now; title = 'Nightshift'; message = $Message } }
        if ($add -or $damaged) {
            Write-NsFileSwap -Path $notesFile -Text ((ConvertTo-Json -InputObject ([ordered]@{ notes = @($notes) }) -Depth 5) + "`n")
        }
        return $add
    }
    finally { $lock.Dispose() }
}

function Restore-AfterMiss {
    # The program is back. One morning note, then the Desktop note, then the count: a failure keeps the
    # count for the next start, and never stops what this start was asked to run. The count's file is held
    # open, sharing nothing but its deletion, from before the note until the count is gone: a second
    # starter (a shortcut, the nightly task, the engine's -Cleanup) cannot open it and leaves the work to
    # the first. The note goes through Add-NsMorningNote, which waits up to 5 seconds for the program's
    # writers; when it cannot write, the count stays and the next start leaves the note.
    # Says what it did: nothing, busy, waiting, cleared, or kept: <why>.
    param([bool]$EnvironmentKnown)
    $held = $null
    try {
        if (-not (Test-Path -LiteralPath $missedFile -PathType Leaf)) { return 'nothing' }
        try { $held = New-Object System.IO.FileStream($missedFile, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Delete) }
        catch { return 'busy' }
        $m = Read-Missed -Stream $held
        if ($null -ne $m) {
            $thing = 'program folder'
            if ($m.what -eq 'environment') {
                $thing = 'program environment'
                $exe = Join-Path $nsHome 'venvs\studio\Scripts\studio.exe'
                if (-not $EnvironmentKnown -and -not (Test-Path -LiteralPath $exe -PathType Leaf)) { return 'waiting' }
            }
            # said once per first missed date: a start killed after the note and before the count went
            $start = 'Nightshift did not run on '
            $mark = ', from ' + $m.first + ' to '
            $message = $start + (Get-Nights $m.nights) + $mark + $m.last + ': its ' + $thing +
                ' was missing until it was put back on ' + $today + '.'
            [void](Add-NsMorningNote -Message $message -OnceStart $start -OnceHas $mark)
        }
        $desk = Get-NoteFolder
        if ($desk) {
            $file = Join-Path $desk $noteName
            if ((Test-Path -LiteralPath $file -PathType Leaf) -and (Test-OurNote $file)) { Remove-Item -LiteralPath $file -Force }
        }
        [System.IO.File]::Delete($missedFile)
        return 'cleared'
    } catch { return ('kept: ' + $_.Exception.Message) }
    finally { if ($null -ne $held) { $held.Dispose() } }
}

function Write-Miss {
    # The nightly run cannot start. The count first (a kill after it still counts the night), then the
    # Desktop note, the toast, and one log line that says how each of them went.
    param([string]$What)
    $m = Read-Missed
    if ($null -eq $m) { $m = @{ first = $today; last = $today; nights = 1; what = $What } }
    elseif ($m.last -ne $today) { $m.nights = $m.nights + 1; $m.last = $today }
    $m.what = $What
    try {
        [void][IO.Directory]::CreateDirectory((Split-Path -Parent $missedFile))
        $json = '{"first":"' + $m.first + '","last":"' + $m.last + '","nights":' + $m.nights + ',"what":"' + $m.what + '"}'
        Write-NsFileSwap -Path $missedFile -Text $json
    } catch { }
    $note = 'test-run'
    try {
        $desk = Get-NoteFolder
        if ($desk) {
            $file = Join-Path $desk $noteName
            if (-not (Test-Path -LiteralPath $desk -PathType Container)) { $note = 'failed: the Desktop folder ' + $desk + ' is not there' }
            elseif ((Test-Path -LiteralPath $file) -and -not (Test-OurNote $file)) { $note = 'failed: ' + $file + ' is there and is not a note from Nightshift; it was left alone' }
            else {
                Write-NsFileSwap -Path $file -Text (((Get-NoteLines $m) -join "`r`n") + "`r`n") -Bom
                $note = 'written'
            }
        } elseif ($devInstall) { $note = 'dev-install' }
        elseif (-not $underTest) { $note = 'failed: Windows named no Desktop folder for this session' }
    } catch { $note = 'failed: ' + $_.Exception.Message }
    $thing = 'program folder'
    if ($What -eq 'environment') { $thing = 'program environment' }
    $where = "Details: '" + $noteName + "' on your Desktop."
    if ($note -ne 'written') { $where = 'Details: the newest launch-*.jsonl in ' + (Join-Path $nsHome 'logs') + '.' }
    $body = 'Its ' + $thing + ' is missing. Run INSTALL.cmd from the Nightshift zip (0.1.15 or later) into ' + $nsHome + '. ' + $where
    $toast = Show-Toast 'Nightshift did not run last night' $body
    try {
        $now = [DateTime]::UtcNow
        $data = [ordered]@{ code = 'NS-W209'; home = $nsHome; first = $m.first; last = $m.last; nights = $m.nights; what = $m.what; toast = $toast; desktop_note = $note }
        $rec = [ordered]@{ ts = $now.ToString('o', $inv); level = 'WARN'; component = 'launch'; event = 'nightly_not_started'
            msg = 'The nightly run could not start: the ' + $thing + ' is missing.'; data = $data }
        $logs = Join-Path $nsHome 'logs'
        [void][IO.Directory]::CreateDirectory($logs)
        # one line, one write, at the end of the day's file (UTF-8 without a BOM, like every Nightshift log)
        $bytes = $utf8.GetBytes((ConvertTo-Json -InputObject $rec -Compress -Depth 4) + "`n")
        $file = Join-Path $logs ('launch-' + $now.ToString('yyyyMMdd', $inv) + '.jsonl')
        $fs = New-Object System.IO.FileStream($file, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
        try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) }
        finally { $fs.Dispose() }
    } catch { }
}

if ($Nightly) {
    $what = ''
    if (-not (Test-Path -LiteralPath (Join-Path (Join-Path $app 'ns_studio') 'cli.py') -PathType Leaf)) { $what = 'folder' }
    elseif (-not $Studio -or -not ((Test-Path -LiteralPath $Studio -PathType Leaf) -or (Get-Command -Name $Studio -CommandType Application -ErrorAction SilentlyContinue))) { $what = 'environment' }
    if ($what) {
        Write-Miss $what
        exit 1
    }
    [void](Restore-AfterMiss $true)
    $global:LASTEXITCODE = 0
    & $Studio run --daily
    exit $LASTEXITCODE
}

if ($Cleanup) {
    # the engine's start: the program runs, so a counted night is over (ns_supervisor/launch_reach.py)
    Write-Output (Restore-AfterMiss $false)
    exit 0
}

if (-not $Launcher) {
    Write-Host 'nightshift-launch.ps1: pass -Launcher <script>, -Nightly -Studio <studio.exe>, or -Cleanup.'
    exit 2
}
if (Test-Path -LiteralPath $Launcher -PathType Leaf) {
    [void](Restore-AfterMiss $false)
    $global:LASTEXITCODE = 0
    & $Launcher
    exit $LASTEXITCODE
}

Write-Host ''
Write-Host 'Nightshift cannot start: its program folder is missing or incomplete.'
Write-Host ('  not found: ' + $Launcher)
Write-Host ''
Write-Host 'This happens when an update or a rollback was interrupted, for example by a power cut.'
if (Test-Path -LiteralPath $swap -PathType Container) {
    Write-Host ('The program is waiting in ' + $swap + ' and nothing in it was deleted.')
}
Write-Host ''
Write-Host 'What to do:'
Write-Host '  1. Close every Nightshift window and any Explorer window that shows the Nightshift folder.'
Write-Host '  2. Run INSTALL.cmd from a Nightshift zip of version 0.1.15 or later (extract the zip again if'
Write-Host '     that folder is gone) and choose the same install folder. Setup puts the program folder back.'
Write-Host '  3. Start Nightshift from this shortcut again.'
$back = Get-MoveBack
if ($back) {
    Write-Host ''
    Write-Host $back
}
Write-Host ''
Read-Host 'Press Enter to close' | Out-Null
exit 1
