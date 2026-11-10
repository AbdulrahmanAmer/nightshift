# STUB_SCHEMA: 5
param(
    [string]$Launcher = '',
    [switch]$Nightly,
    [string]$Studio = '',
    [string]$DesktopDir = '',
    [switch]$Cleanup,
    [int]$UpdateWaitSeconds = 1800,
    [int]$ClearWaitSeconds = 15
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
#
# Format 5 (card D-39b): Nightshift never starts on a Python environment an update, a rollback or a resync
# left unfinished, and never while one runs. Before a shortcut's launcher or the nightly run, holding
# <home>\run\update.lock (the byte lock every updater and setup take, ns_common/singleton.py):
#   S0  an update, rollback, repair or setup runs: a shortcut says so and starts nothing; the task waits up to
#       -UpdateWaitSeconds (30 minutes), then runs or counts a missed night. An update whose process died (a
#       power cut) is told once: its state becomes 'failed' with the sentence it left, and a morning note.
#   S1-S4  <home>\run\venv-repair.json (written by the update before it changed anything) is replayed: its
#       uv command lines, with its environment, then it is deleted and a morning note says so. A record of
#       another program is stale and deleted; one that cannot be read (read again for 2 s first: it may be
#       held or swapped at that instant), or a uv that fails, keeps the record and starts nothing (NS-W210,
#       with the way out). A delete that Windows refuses is retried for -ClearWaitSeconds (15), then the
#       record is marked FINISHED by the sibling venv-repair.done, which names its id (a program that holds
#       the record open blocks its delete, not a new file beside it); every reader takes a finished record
#       as nothing pending (installer/stage2/web/venvrepair.py, the same rule). Only when neither works is
#       it said, and nothing starts.
#   S5  without a record, an environment that holds another Nightshift than the program (an older updater
#       was interrupted) is said once, with INSTALL.cmd's Repair; the start goes on.
$nsHome = $PSScriptRoot
if ($Launcher) { $app = Split-Path -Parent (Split-Path -Parent $Launcher) } else { $app = Join-Path $nsHome 'app' }
$swap = $app + '.swap'
$inv = [Globalization.CultureInfo]::InvariantCulture
$today = [DateTime]::Now.ToString('yyyy-MM-dd', $inv)
$utf8 = New-Object System.Text.UTF8Encoding($false)
$runDir = Join-Path $nsHome 'run'
$missedFile = Join-Path $runDir 'launch-missed.json'
$lockFile = Join-Path $runDir 'update.lock'
$stateFile = Join-Path $runDir 'update_state.json'
$repairFile = Join-Path $runDir 'venv-repair.json'
$doneFile = Join-Path $runDir 'venv-repair.done'
$mixedTold = Join-Path $runDir 'venv-mixed-told.txt'
# D-48: the words of an update state that sends the owner to finish the environments (venvrepair.py
# FINISH_MARKS): once the replay has finished them, that state is closed (Close-FinishedUpdate).
$finishMarks = @('finishes the Python environments', 'To fill every environment', 'To finish it:', 'could not be refreshed')
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
        if ($m.first -match $day -and $m.last -match $day -and $m.nights -ge 1 -and @('folder', 'environment', 'update', 'repair') -contains $m.what) { return $m }
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
    param($Missed, [string]$Reason = '')
    $code = 'NS-W209'
    if (@('update', 'repair') -contains $Missed.what) { $code = 'NS-W210' }
    $head = $noteStart + $Missed.last + ' (' + (Get-Nights $Missed.nights) + ' so far, the first on ' + $Missed.first + '). (' + $code + ')'
    $lines = @($head, '')
    if ($Missed.what -eq 'update') {
        $lines += ('An update, rollback or repair of Nightshift was still running after ' + [string][Math]::Round($UpdateWaitSeconds / 60) + ' minutes, so the nightly run did not start.')
        $lines += @('', 'What to do: nothing. While Automatic runs are on, the next night runs by itself; this file is removed the next time Nightshift starts.')
        $lines += @('', "To stop the nightly attempts, disable 'Nightshift Daily' in Task Scheduler.")
        return $lines
    }
    if ($Missed.what -eq 'repair') {
        $lines += @($Reason, '', 'This file is removed the next time Nightshift starts.')
        $lines += @('', "To stop the nightly attempts until then, disable 'Nightshift Daily' in Task Scheduler.")
        return $lines
    }
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
    # and contains -OnceHas), add, keep the bound, swap the file. The file keeps the format 0.1.14 reads:
    # UTF-8 without a byte order mark, { notes: [ { at, title, message } ] }, plus the program's two marks
    # (routine = true on a routine note, removed = <n> on the note that counts the removed ones).
    # The bound is the program's rule (notify._add): while more than 200 notes would wait, the oldest note
    # marked routine goes; when none is, the oldest note goes (this script's notes are never routine). Each
    # removed note is logged with its text (morning_note_dropped) and one note, first in the file, says how
    # many went in all (notify.removed_message's words), replaced and never repeated. The newest is kept.
    # The program's other counted case, a routine note kept out because every waiting note is not routine
    # (it raises the same count note), cannot arise here: this script never leaves a routine note, so its
    # note is always added.
    # A damaged file is never emptied: it is first copied to a file of the same name plus .bad-<its time>,
    # then replaced by what could be read of it plus a note that says so (notify.damaged_message's words).
    # Returns true when the note was added and false when it was said already; throws when it could not be
    # written (the lock not free in 5 seconds, the file not readable or writable): then nothing was changed
    # and the caller keeps what it wanted to say for the next start (its own spool: the missed-night count).
    # 5 seconds and not the program's longer wait: a person may be waiting for a window to open, and giving up
    # loses nothing.
    param([Parameter(Mandatory = $true)][string]$Message, [string]$OnceStart = '', [string]$OnceHas = '')
    $notesFull = 200    # notify.NOTES_FULL
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
        if ($add) {
            $count = $null
            $kept = New-Object System.Collections.ArrayList
            foreach ($n in $notes) {
                if (($n -is [System.Management.Automation.PSCustomObject]) -and $n.PSObject.Properties['removed'] -and ($n.removed -is [ValueType]) -and -not ($n.removed -is [bool])) { $count = $n }
                else { [void]$kept.Add($n) }
            }
            [void]$kept.Add([ordered]@{ at = $now; title = 'Nightshift'; message = $Message })
            $gone = New-Object System.Collections.ArrayList
            while ($kept.Count -gt $notesFull) {
                $drop = 0
                for ($i = 0; $i -lt ($kept.Count - 1); $i++) {
                    $n = $kept[$i]
                    if (($n -is [System.Management.Automation.PSCustomObject]) -and $n.PSObject.Properties['routine'] -and ($n.routine -eq $true)) { $drop = $i; break }
                }
                [void]$gone.Add($kept[$drop])
                $kept.RemoveAt($drop)
            }
            if ($gone.Count -gt 0) {
                $total = $gone.Count
                if ($null -ne $count) { $total += [int]$count.removed }
                $many = [string]$total + ' unread notes were'
                if ($total -eq 1) { $many = '1 unread note was' }
                $logs = Join-Path $nsHome 'logs'
                $words = $many + ' removed to keep at most ' + $notesFull + ' notes here: routine news such as "ready for review" goes first, and a failure or a note that asks something of you only makes room for a newer one. Each removed note is in the log files in ' + $logs + ' (morning_note_dropped). Press Mark all read once you have read the notes, so that nothing more is removed.'
                try {
                    $stamp = [DateTime]::UtcNow
                    $list = @(foreach ($g in $gone) { [ordered]@{ at = [string]$g.at; title = [string]$g.title; message = [string]$g.message } })
                    $rec = [ordered]@{ ts = $stamp.ToString('o', $inv); level = 'WARN'; component = 'launch'; event = 'morning_note_dropped'
                        msg = [string]$gone.Count + ' unread note(s) removed, bound ' + $notesFull; data = [ordered]@{ notes = $list; removed_in_all = $total } }
                    [void][IO.Directory]::CreateDirectory($logs)
                    $bytes = $utf8.GetBytes((ConvertTo-Json -InputObject $rec -Compress -Depth 6) + "`n")
                    $fs = New-Object System.IO.FileStream((Join-Path $logs ('launch-' + $stamp.ToString('yyyyMMdd', $inv) + '.jsonl')), [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
                    try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) }
                    finally { $fs.Dispose() }
                } catch { }
                $count = [ordered]@{ at = $now; title = 'Nightshift'; message = $words; removed = $total }
            }
            $notes = @()
            if ($null -ne $count) { $notes += $count }
            $notes += @($kept)
        }
        if ($add -or $damaged) {
            Write-NsFileSwap -Path $notesFile -Text ((ConvertTo-Json -InputObject ([ordered]@{ notes = @($notes) }) -Depth 5) + "`n")
        }
        return $add
    }
    finally { $lock.Dispose() }
}

function Write-LaunchLog {
    # One line, one write, at the end of the day's logs\launch-<yyyymmdd>.jsonl (UTF-8 without a BOM, like
    # every Nightshift log). Never throws.
    param([string]$Level, [string]$EventName, [string]$Message, $Data)
    try {
        $now = [DateTime]::UtcNow
        $rec = [ordered]@{ ts = $now.ToString('o', $inv); level = $Level; component = 'launch'; event = $EventName; msg = $Message; data = $Data }
        $logs = Join-Path $nsHome 'logs'
        [void][IO.Directory]::CreateDirectory($logs)
        $bytes = $utf8.GetBytes((ConvertTo-Json -InputObject $rec -Compress -Depth 4) + "`n")
        $file = Join-Path $logs ('launch-' + $now.ToString('yyyyMMdd', $inv) + '.jsonl')
        $fs = New-Object System.IO.FileStream($file, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
        try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) }
        finally { $fs.Dispose() }
    } catch { Write-Verbose 'launch log line not written' }
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
            $why = ': its ' + $thing + ' was missing until it was put back on ' + $today + '.'
            if ($m.what -eq 'update') { $why = ': an update, rollback or repair of Nightshift was running at the time.' }
            if ($m.what -eq 'repair') { $why = ': an interrupted update had left its Python environments unfinished until ' + $today + '.' }
            $message = $start + (Get-Nights $m.nights) + $mark + $m.last + $why
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
    param([string]$What, [string]$Reason = '')
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
                Write-NsFileSwap -Path $file -Text (((Get-NoteLines $m $Reason) -join "`r`n") + "`r`n") -Bom
                $note = 'written'
            }
        } elseif ($devInstall) { $note = 'dev-install' }
        elseif (-not $underTest) { $note = 'failed: Windows named no Desktop folder for this session' }
    } catch { $note = 'failed: ' + $_.Exception.Message }
    $where = "Details: '" + $noteName + "' on your Desktop."
    if ($note -ne 'written') { $where = 'Details: the newest launch-*.jsonl in ' + (Join-Path $nsHome 'logs') + '.' }
    $code = 'NS-W209'
    if ($What -eq 'update') {
        $code = 'NS-W210'
        $why = 'an update, rollback or repair of Nightshift was running.'
        $body = 'An update, rollback or repair of Nightshift was running. While Automatic runs are on, the next night runs by itself. ' + $where
    } elseif ($What -eq 'repair') {
        $code = 'NS-W210'
        $why = 'an interrupted update could not be finished.'
        $body = $Reason + ' ' + $where
    } else {
        $thing = 'program folder'
        if ($What -eq 'environment') { $thing = 'program environment' }
        $why = 'the ' + $thing + ' is missing.'
        $body = 'Its ' + $thing + ' is missing. Run INSTALL.cmd from the Nightshift zip (0.1.15 or later) into ' + $nsHome + '. ' + $where
    }
    $toast = Show-Toast 'Nightshift did not run last night' $body
    $data = [ordered]@{ code = $code; home = $nsHome; first = $m.first; last = $m.last; nights = $m.nights; what = $m.what; toast = $toast; desktop_note = $note }
    if ($Reason) { $data['reason'] = $Reason }
    Write-LaunchLog 'WARN' 'nightly_not_started' ('The nightly run could not start: ' + $why) $data
}

function Get-AppVersion {
    try { return [IO.File]::ReadAllText((Join-Path $app 'VERSION')).Trim() } catch { return '' }
}

function Get-VersionKey {
    # a project version as uv writes it (installer/stage2/web/deplock.py version_key): 0.1.15-rc3 is 0.1.15rc3
    param([string]$Version)
    return ($Version.Trim().ToLowerInvariant() -replace '[-_.]?(a|b|rc)[-_.]?(\d+)$', '$1$2')
}

function Lock-Update {
    # S0: the byte lock of run\update.lock (byte 0, this pid after it), as ns_common/singleton.py FileLock takes
    # it. The open file that holds it; $null when another process holds it; 'none' when there is nothing to
    # protect (no run folder: no update ever ran here) or the file cannot be opened at all.
    if (-not (Test-Path -LiteralPath $runDir -PathType Container)) { return 'none' }
    try { $fs = New-Object System.IO.FileStream($lockFile, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete)) }
    catch {
        Write-LaunchLog 'WARN' 'update_lock_unread' ('run\update.lock could not be opened: ' + $_.Exception.Message) $null
        return 'none'
    }
    try { $fs.Lock(0, 1) }
    catch { $fs.Dispose(); return $null }
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes([string]$PID + "`n")
        $fs.SetLength(1); $fs.Position = 1; $fs.Write($bytes, 0, $bytes.Length); $fs.Flush()
    } catch { Write-Verbose 'pid not written into the lock file' }
    return $fs
}

function Unlock-Update {
    param($Held)
    if ($Held -is [System.IO.FileStream]) {
        try { $Held.Unlock(0, 1) } catch { Write-Verbose 'already unlocked' }
        $Held.Dispose()
    }
}

function Read-UpdateState {
    try { return ([IO.File]::ReadAllText($stateFile) | ConvertFrom-Json) } catch { return $null }
}

function Test-WorkerAlive {
    # An update process of an older release takes no lock: its 'running' state with a live Python process (or,
    # without a pid, written in the last 2 minutes: ns_supervisor/update_job.py effective_status) is one.
    param($State)
    if ($null -eq $State -or -not $State.PSObject.Properties['state'] -or [string]$State.state -ne 'running') { return $false }
    $workerPid = 0
    if ($State.PSObject.Properties['pid']) { try { $workerPid = [int]$State.pid } catch { $workerPid = 0 } }
    if ($workerPid -gt 0) {
        $proc = Get-Process -Id $workerPid -ErrorAction SilentlyContinue
        return ($null -ne $proc -and $proc.ProcessName -match 'python')
    }
    try { $at = [DateTime]::Parse([string]$State.updated_at, $inv) } catch { return $false }
    return (([DateTime]::Now - $at).TotalSeconds -lt 120)
}

function Send-DeadUpdate {
    # S0b: the update's process died without a result (a power cut, a kill). Nobody saw its window: the state
    # becomes 'failed' with the sentence it left for that step (this script holds the lock, so it may write
    # the state), and one morning note says it.
    param($State)
    if ($null -eq $State -or -not $State.PSObject.Properties['state'] -or [string]$State.state -ne 'running') { return }
    $left = 'The update process stopped before it finished.'
    if ($State.PSObject.Properties['if_stopped_here'] -and [string]$State.if_stopped_here) { $left = [string]$State.if_stopped_here }
    try {
        foreach ($pair in @(@('state', 'failed'), @('exit_code', 1), @('message', $left), @('updated_at', [DateTime]::Now.ToString('s')))) {
            $State | Add-Member -NotePropertyName $pair[0] -NotePropertyValue $pair[1] -Force
        }
        Write-NsFileSwap -Path $stateFile -Text (ConvertTo-Json -InputObject $State -Compress -Depth 5)
        [void](Add-NsMorningNote -Message ('An update or rollback of Nightshift was interrupted. ' + $left) -OnceStart ('An update or rollback of Nightshift was interrupted. ' + $left))
        Write-LaunchLog 'WARN' 'update_interrupted' $left $null
    } catch { Write-LaunchLog 'WARN' 'update_interrupted_not_told' $_.Exception.Message $null }
}

function Close-FinishedUpdate {
    # D-48 (this script holds run\update.lock, so no update process writes the state now): the replay finished
    # what an interrupted update or rollback left, so its state says that, in the words of
    # installer/stage2/web/venvrepair.py close_update_state. Only a state that sends the owner to finish the
    # environments is changed (needs_finish: a 'running' or 'failed' state with record, venvs_left or
    # record_left, or one of $finishMarks in its words).
    $state = Read-UpdateState
    if ($null -eq $state -or -not $state.PSObject.Properties['state']) { return }
    if (@('running', 'failed') -notcontains [string]$state.state) { return }
    $flagged = $false
    foreach ($k in @('record', 'venvs_left', 'record_left')) { if ($state.PSObject.Properties[$k] -and $state.$k) { $flagged = $true } }
    $text = ''
    foreach ($k in @('message', 'if_stopped_here')) { if ($state.PSObject.Properties[$k]) { $text += ' ' + [string]$state.$k } }
    foreach ($m in $finishMarks) { if ($text.Contains($m)) { $flagged = $true } }
    if (-not $flagged) { return }
    $version = Get-AppVersion
    if (-not $version) { $version = 'unknown version' }
    $at = [DateTime]::Now
    $by = "Nightshift's start (a Desktop shortcut or the nightly run)"
    $said = 'An update or rollback was interrupted and has since been finished by ' + $by + ' (' + $at.ToString('yyyy-MM-dd HH:mm', $inv) + '): Nightshift ' + $version + ', its program and Python environments, is in place.'
    $old = ''
    if ($state.PSObject.Properties['old']) { $old = [string]$state.old }
    $log = ''
    if ($state.PSObject.Properties['log']) { $log = [string]$state.log }
    try {
        Write-NsFileSwap -Path $stateFile -Text (ConvertTo-Json -Compress -InputObject ([ordered]@{ state = 'done'; old = $old; new = $version; exit_code = 0; message = $said; updated_at = $at.ToString('s', $inv); finished_by = $by; log = $log }))
        Write-LaunchLog 'INFO' 'update_state_closed' $said $null
    } catch { Write-LaunchLog 'WARN' 'update_state_not_closed' $_.Exception.Message $null }
}

function Get-RepairWords {
    param([string]$What, [string]$Said)
    $version = Get-AppVersion
    if (-not $version) { $version = 'unknown version' }
    $lead = 'Nightshift could not finish an interrupted update (NS-W210): ' + $What + '.'
    if ($Said) { $lead += ' uv said: ' + $Said }
    return ($lead + ' Close every Nightshift window and open Nightshift again. If it fails again, run INSTALL.cmd from the Nightshift ' + $version + ' zip and choose this folder (Repair).')
}

function Test-RecordFor {
    # S2: the record was written for the program now in $app (its .ns-app.done marker, else its VERSION).
    param($Record)
    try {
        $there = [IO.Path]::GetFullPath([string]$Record.app).TrimEnd('\')
        if ($there -ine [IO.Path]::GetFullPath($app).TrimEnd('\')) { return $false }
    } catch { return $false }
    $marker = Join-Path $app '.ns-app.done'
    if ([string]$Record.match -eq 'marker' -and (Test-Path -LiteralPath $marker -PathType Leaf)) {
        return ([IO.File]::ReadAllText($marker).Trim() -ceq [string]$Record.for_app)
    }
    return ([bool][string]$Record.version -and (Get-VersionKey (Get-AppVersion)) -eq (Get-VersionKey ([string]$Record.version)))
}

function Read-RepairRecord {
    # S1: the record; $null when there is none. It is read with every share mode (this read never blocks a
    # delete), and read again for up to 2 s while it cannot be read at this instant (held without read
    # sharing, swapped right then); after that it throws: an unreadable record counts as unfinished.
    $deadline = [DateTime]::UtcNow.AddSeconds(2)
    while ($true) {
        if (-not (Test-Path -LiteralPath $repairFile -PathType Leaf)) { return $null }
        try {
            $fs = New-Object System.IO.FileStream($repairFile, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            try { $text = (New-Object System.IO.StreamReader($fs, $utf8)).ReadToEnd() } finally { $fs.Dispose() }
            $record = $text | ConvertFrom-Json
            if ($null -eq $record -or $null -eq $record.PSObject.Properties['commands']) { throw 'it is not a repair record' }
            if (@($record.commands).Count -eq 0) { throw 'it holds no command' }
            return $record
        } catch [System.IO.FileNotFoundException], [System.IO.DirectoryNotFoundException] {
            return $null
        } catch {
            if ([DateTime]::UtcNow -ge $deadline) { throw }
            Start-Sleep -Milliseconds 100
        }
    }
}

function Test-RecordFinished {
    # The sibling venv-repair.done names this record's id: its replay or sync finished and only its delete
    # was refused. A record without an id is never finished this way.
    param($Record)
    $rid = ''
    if ($Record.PSObject.Properties['id']) { $rid = [string]$Record.id }
    if (-not $rid) { return $false }
    try { $done = [IO.File]::ReadAllText($doneFile) | ConvertFrom-Json } catch { return $false }
    return ($null -ne $done -and $null -ne $done.PSObject.Properties['id'] -and [string]$done.id -ceq $rid)
}

function Test-RecordPending {
    # A record that waits: there, and not finished (or there and unreadable).
    try { $r = Read-RepairRecord } catch { return $true }
    return ($null -ne $r -and -not (Test-RecordFinished $r))
}

function Remove-RepairRecord {
    # S4 (this script holds run\update.lock): the record goes. The delete is retried for -ClearWaitSeconds
    # while Windows refuses it; still refused, venv-repair.done gets its id (finished). '' when one of them
    # worked; else why neither did (then nothing may start: every Python start would refuse the record).
    param($Record)
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(0, $ClearWaitSeconds))
    $pause = 10
    $why = ''
    while ($true) {
        try {
            if (Test-Path -LiteralPath $repairFile -PathType Leaf) { [IO.File]::Delete($repairFile) }
            try { if (Test-Path -LiteralPath $doneFile -PathType Leaf) { [IO.File]::Delete($doneFile) } } catch { Write-Verbose 'the finished-marker of an older record is left' }
            return ''
        } catch {
            $why = $_.Exception.Message
            if ([DateTime]::UtcNow -ge $deadline) { break }
            Start-Sleep -Milliseconds $pause
            $pause = [Math]::Min($pause * 2, 250)
        }
    }
    $rid = ''
    if ($null -ne $Record -and $Record.PSObject.Properties['id']) { $rid = [string]$Record.id }
    if (-not $rid) { return ('it could not be deleted (' + $why + ') and has no id to mark it finished') }
    try {
        Write-NsFileSwap -Path $doneFile -Text (ConvertTo-Json -Compress -InputObject ([ordered]@{ id = $rid; finished = [DateTime]::Now.ToString('s', $inv); delete_refused = $why }))
    } catch { return ('it could not be deleted (' + $why + ') nor marked finished (' + $_.Exception.Message + ')') }
    Write-LaunchLog 'WARN' 'venv_record_marked_finished' ('its delete was refused: ' + $why) $null
    return ''
}

function Invoke-Repair {
    # S1 to S4. '' when nothing is left to do (no record, a finished or stale one, or the replay worked);
    # else the words.
    try { $record = Read-RepairRecord }
    catch { return (Get-RepairWords ('the record ' + $repairFile + ' cannot be read (' + $_.Exception.Message + ')') '') }
    if ($null -eq $record) { return '' }
    $commands = @($record.commands)
    if (Test-RecordFinished $record) {
        # finished; only its delete was refused earlier: the two files go now if they can
        try { [IO.File]::Delete($repairFile); [IO.File]::Delete($doneFile) } catch { Write-Verbose 'the finished record is still held' }
        Close-FinishedUpdate
        return ''
    }
    if (-not (Test-RecordFor $record)) {
        # (the Python gates ignore a stale record too: one that cannot be deleted now stops nothing)
        try {
            Remove-Item -LiteralPath $repairFile -Force
            Write-LaunchLog 'INFO' 'venv_record_stale' ('deleted: written for ' + [string]$record.version + ' at ' + [string]$record.app) $null
        } catch { Write-LaunchLog 'WARN' 'venv_record_stale' ('not deleted: ' + $_.Exception.Message) $null }
        return ''
    }
    $names = @()
    foreach ($c in $commands) { if ($names -notcontains [string]$c.venv) { $names += [string]$c.venv } }
    $quoted = ($names | ForEach-Object { "'" + $_ + "'" }) -join ', '
    Write-Host ('Finishing an interrupted update: refreshing the Python environments ' + $quoted + '. This can take a few minutes.')
    $saved = @{}
    if ($record.PSObject.Properties['env'] -and $null -ne $record.env) {
        foreach ($p in $record.env.PSObject.Properties) {
            $saved[$p.Name] = [Environment]::GetEnvironmentVariable($p.Name, 'Process')
            [Environment]::SetEnvironmentVariable($p.Name, [string]$p.Value, 'Process')
        }
    }
    try {
        foreach ($c in $commands) {
            $argv = @($c.argv | ForEach-Object { [string]$_ })
            $what = "its Python environment '" + [string]$c.venv + "' does not match the program yet"
            if (-not (Test-Path -LiteralPath $argv[0] -PathType Leaf)) { return (Get-RepairWords $what ('uv was not found at ' + $argv[0])) }
            $rest = @($argv | Select-Object -Skip 1)
            $ErrorActionPreference = 'Continue'
            $global:LASTEXITCODE = 0
            $out = @(& $argv[0] @rest 2>&1 | ForEach-Object { [string]$_ })
            $exitCode = $LASTEXITCODE
            $ErrorActionPreference = 'Stop'
            if ($exitCode -ne 0) {
                $last = [string](@($out | Where-Object { $_.Trim() }) | Select-Object -Last 1)
                Write-LaunchLog 'WARN' 'venv_repair_failed' $what ([ordered]@{ code = 'NS-W210'; venv = [string]$c.venv; exit_code = $exitCode; uv = $last })
                return (Get-RepairWords $what $last.Trim())
            }
        }
    } finally {
        $ErrorActionPreference = 'Stop'
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    }
    $left = Remove-RepairRecord $record
    if ($left) {
        $what = 'its Python environments ' + $quoted + ' were refreshed, but the record ' + $repairFile + ' could not be removed: ' + $left + ', and every start of Nightshift refuses while it is there'
        Write-LaunchLog 'WARN' 'venv_record_left' $what ([ordered]@{ code = 'NS-W210' })
        return (Get-RepairWords $what '')
    }
    $version = Get-AppVersion
    if ($names.Count -eq 1) { $now = "the Python environment " + $quoted + ' now matches' } else { $now = 'the Python environments ' + $quoted + ' now match' }
    $said = 'An interrupted update was finished on ' + $today + ': ' + $now + ' Nightshift ' + $version + '.'
    try { [void](Add-NsMorningNote -Message $said -OnceStart $said) } catch { Write-LaunchLog 'WARN' 'morning_note_not_written' $_.Exception.Message $null }
    Write-LaunchLog 'INFO' 'venv_repaired' $said ([ordered]@{ venvs = $names })
    Close-FinishedUpdate
    return ''
}

function Get-Mixed {
    # S5: without a record, a venv whose nightshift-*.dist-info is not the program's VERSION, or two of them
    # (installer/stage2/web/venvrepair.py mixed, the same words).
    $want = Get-VersionKey (Get-AppVersion)
    $parts = @()
    foreach ($v in @('studio', 'supervisor', 'tts')) {
        $folder = Join-Path (Join-Path $nsHome 'venvs') $v
        if (-not (Test-Path -LiteralPath (Join-Path $folder 'Lib\site-packages') -PathType Container)) { continue }
        $have = @()
        foreach ($d in @(Get-ChildItem -LiteralPath (Join-Path $folder 'Lib\site-packages') -Directory -Filter 'nightshift-*.dist-info' -ErrorAction SilentlyContinue | Sort-Object Name)) {
            $ver = $d.Name.Substring(11, $d.Name.Length - 21)
            try {
                foreach ($line in [IO.File]::ReadAllLines((Join-Path $d.FullName 'METADATA'))) {
                    if ($line -match '^(?i)version:\s*(.+)$') { $ver = $Matches[1].Trim(); break }
                }
            } catch { Write-Verbose 'METADATA not read' }
            $have += $ver
        }
        if ($have.Count -gt 1 -or ($have.Count -eq 1 -and (Get-VersionKey $have[0]) -ne $want)) {
            $parts += ("The Python environment '" + $v + "' holds Nightshift " + ($have -join ' and '))
        }
    }
    if ($parts.Count -eq 0) { return '' }
    $here = Get-AppVersion
    if (-not $here) { $here = 'unknown version' }
    return (($parts -join '; ') + ', but the program is ' + $here + ' (an update by an older version was interrupted). Run INSTALL.cmd from the Nightshift ' + $here + ' zip and choose this folder (Repair).')
}

function Test-BeforeStart {
    # S0 to S5 before a shortcut's launcher ($Night false) or the nightly run. $null: go on. Else
    # @{ what = 'update' | 'repair'; text = <the words> }: the caller says it and starts nothing.
    param([bool]$Night)
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(0, $UpdateWaitSeconds))
    while ($true) {
        $held = Lock-Update
        $state = Read-UpdateState
        if ($null -ne $held -and -not (Test-WorkerAlive $state)) { break }
        Unlock-Update $held
        if (-not $Night -or [DateTime]::UtcNow -ge $deadline) {
            $progress = $stateFile
            if ($null -ne $state -and $state.PSObject.Properties['log'] -and [string]$state.log) { $progress = [string]$state.log }
            return @{ what = 'update'; text = ('An update, rollback or repair of Nightshift is running (progress: ' + $progress + '). Nothing was started. Open Nightshift again when it has finished: an update opens Nightshift by itself when it is done.') }
        }
        Start-Sleep -Seconds ([Math]::Max(1, [Math]::Min(5, [int]($deadline - [DateTime]::UtcNow).TotalSeconds)))
    }
    try {
        Send-DeadUpdate $state
        $words = Invoke-Repair
        if ($words) { return @{ what = 'repair'; text = $words } }
        if (Test-Path -LiteralPath $repairFile -PathType Leaf) { return $null }
        $mixed = Get-Mixed
        if ($mixed) {
            Write-Host $mixed
            $told = ''
            try { $told = [IO.File]::ReadAllText($mixedTold) } catch { $told = '' }
            if ($told -ne $mixed) {
                [void](Add-NsMorningNote -Message $mixed -OnceStart $mixed)
                Write-LaunchLog 'WARN' 'venv_mixed' $mixed ([ordered]@{ code = 'NS-W210' })
                Write-NsFileSwap -Path $mixedTold -Text $mixed
            }
        } elseif (Test-Path -LiteralPath $mixedTold -PathType Leaf) { Remove-Item -LiteralPath $mixedTold -Force }
    } catch {
        # nothing starts on a record that is still there, whatever went wrong while it was replayed
        Write-LaunchLog 'WARN' 'start_check_failed' $_.Exception.Message $null
        if (Test-RecordPending) {
            return @{ what = 'repair'; text = (Get-RepairWords ('the repair stopped: ' + $_.Exception.Message) '') }
        }
    } finally { Unlock-Update $held }
    return $null
}

if ($Nightly) {
    if (-not (Test-Path -LiteralPath (Join-Path (Join-Path $app 'ns_studio') 'cli.py') -PathType Leaf)) {
        Write-Miss 'folder'
        exit 1
    }
    # (before the look at studio.exe: an interrupted sync may have removed it, and the repair puts it back)
    $stop = Test-BeforeStart $true
    if ($null -ne $stop) {
        Write-Miss $stop.what $stop.text
        exit 1
    }
    if (-not $Studio -or -not ((Test-Path -LiteralPath $Studio -PathType Leaf) -or (Get-Command -Name $Studio -CommandType Application -ErrorAction SilentlyContinue))) {
        Write-Miss 'environment'
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
    # A PC can hold several Nightshift folders and has one NIGHTSHIFT_HOME: a shortcut runs the install this
    # file is in, whatever the variable names (the launchers read the variable before their own place).
    if (Test-Path -LiteralPath (Join-Path (Join-Path $nsHome 'config') 'runtime.json') -PathType Leaf) { $env:NIGHTSHIFT_HOME = $nsHome }
    $stop = Test-BeforeStart $false
    # After a repair that FAILED (not while an update runs: then they would read files it is replacing), the
    # Diagnostics and Outbox shortcuts still work: the diagnostics zip is what the way out may ask for, and
    # the outbox is only a folder. Neither starts the engine.
    $readOnly = @('diagnostics.ps1', 'open-outbox.ps1') -contains ([IO.Path]::GetFileName($Launcher).ToLowerInvariant())
    if ($null -ne $stop -and $stop.what -eq 'repair' -and $readOnly) {
        Write-Host $stop.text
        Write-Host ''
        $stop = $null
    }
    if ($null -ne $stop) {
        Write-Host ''
        Write-Host $stop.text
        if ($stop.what -eq 'repair') {
            Write-Host ''
            Write-Host 'The Diagnostics shortcut still opens: it makes a diagnostics zip on the Desktop (a smaller one without Python when the full one cannot be made) and says which one it made. It does not start Nightshift. The Outbox shortcut opens too.'
        }
        Write-Host ''
        Read-Host 'Press Enter to close' | Out-Null
        exit 1
    }
    [void](Restore-AfterMiss $false)
    $global:LASTEXITCODE = 0
    if ($DesktopDir -and [IO.Path]::GetFileName($Launcher) -ieq 'diagnostics.ps1') { & $Launcher -DesktopDir $DesktopDir }
    else { & $Launcher }
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
