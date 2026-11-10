Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Shared helpers for the Nightshift launchers (dot-sourced; not a shortcut target).

# --- state files that other processes read -------------------------------------------------------------------
# Every launcher that writes a file somebody else reads (the window host's run\apphost-<role>.json and
# config\window.json, config\opencode.json, the cache stamp) writes it with Write-NsFileAtomic, and every
# launcher that reads such a file reads it with Read-NsTextRetry. The Python side does the same through
# ns_common.atomic. A file written in place is empty for a moment (the write truncates it first), so a reader
# in that moment meets a torn file and takes it for "no host window", "stale", or "no saved window".

function Write-NsFileAtomic {
    # The file at -Path is either what it was or exactly the bytes asked for, never half written: the bytes
    # go to a temp file in the SAME folder (a swap across drives is not atomic), are flushed to disk, and are
    # then moved over the target with MoveFileEx(REPLACE_EXISTING), the call Python's os.replace makes.
    # Windows refuses that move for a few milliseconds while another process has the target open without
    # sharing delete (any Python reader, an editor, a sync client): that is retried (-Tries, a growing pause).
    # The temp file is removed whatever happens; on an error the old file is untouched and the error is thrown.
    # -Text is encoded with -Encoding (default UTF-8 WITHOUT a BOM; pass
    # (New-Object System.Text.UTF8Encoding $true) for a BOM); line endings are written as given.
    [CmdletBinding(DefaultParameterSetName = 'Text')]
    param(
        [Parameter(Mandatory = $true, Position = 0)][string]$Path,
        [Parameter(Mandatory = $true, Position = 1, ParameterSetName = 'Text')][AllowEmptyString()][string]$Text,
        [Parameter(ParameterSetName = 'Text')][System.Text.Encoding]$Encoding = (New-Object System.Text.UTF8Encoding $false),
        [Parameter(Mandatory = $true, ParameterSetName = 'Bytes')][AllowEmptyCollection()][byte[]]$Bytes,
        [int]$Tries = 20
    )
    if ($PSCmdlet.ParameterSetName -eq 'Text') {
        $pre = $Encoding.GetPreamble()
        $body = $Encoding.GetBytes($Text)
        $payload = New-Object byte[] ($pre.Length + $body.Length)
        [Array]::Copy($pre, 0, $payload, 0, $pre.Length)
        [Array]::Copy($body, 0, $payload, $pre.Length, $body.Length)
    }
    else {
        $payload = $Bytes
    }
    # The swap call is compiled once per process (about 400 ms). Where the compile is not possible (a C#
    # compiler blocked by security software or policy, a TEMP that cannot be written) the helper falls back
    # to [IO.File]::Replace / Move, which swap the same way; without the fallback a launcher would write no
    # state file at all and the Dashboard launcher would wait out its host-ready timeout for nothing.
    $native = $null -ne ('NsCommon.AtomicFile' -as [type])
    if (-not $native) {
        try {
            Add-Type -Namespace NsCommon -Name AtomicFile -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
private static extern bool MoveFileExW(string src, string dst, int flags);
public static int Swap(string src, string dst) {
    if (MoveFileExW(src, dst, 9)) { return 0; }   // MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
    return System.Runtime.InteropServices.Marshal.GetLastWin32Error();
}
'@
            $native = $true
        }
        catch { Write-Verbose ('swap helper not compiled, using File.Replace: ' + $_.Exception.Message) }
    }
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $dir = [System.IO.Path]::GetDirectoryName($full)
    if (-not [System.IO.Directory]::Exists($dir)) { [void][System.IO.Directory]::CreateDirectory($dir) }
    # ~ns<pid>-0-<n>.tmp: the name the Python side sweeps (ns_common.atomic.sweep_stale_tmp) if a crash leaves one
    $tmp = Join-Path $dir ('~ns' + $PID + '-0-' + (Get-Random -Maximum 2000000000) + '.tmp')
    try {
        $fs = New-Object System.IO.FileStream($tmp, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try {
            $fs.Write($payload, 0, $payload.Length)
            $fs.Flush($true)
        }
        finally {
            $fs.Dispose()
        }
        for ($attempt = 1; ; $attempt++) {
            $err = 0
            $detail = ''
            if ($native) { $err = [NsCommon.AtomicFile]::Swap($tmp, $full) }
            else {
                try {
                    if ([System.IO.File]::Exists($full)) { [System.IO.File]::Replace($tmp, $full, [NullString]::Value) }
                    else { [System.IO.File]::Move($tmp, $full) }
                }
                catch [System.IO.IOException], [System.UnauthorizedAccessException] {
                    $err = 32
                    $detail = $_.Exception.Message
                }
            }
            if ($err -eq 0) { break }
            # 5 access denied, 32 sharing violation, 33 lock violation: a reader has the target open right now
            if (($err -notin @(5, 32, 33)) -or $attempt -ge $Tries) {
                if (-not $detail) { $detail = (New-Object System.ComponentModel.Win32Exception($err)).Message }
                throw (New-Object System.IO.IOException(('Could not replace ' + $full + ': ' + $detail)))
            }
            Start-Sleep -Milliseconds ([Math]::Min(10 * $attempt, 100))
        }
    }
    finally {
        try { if ([System.IO.File]::Exists($tmp)) { [System.IO.File]::Delete($tmp) } }
        catch { Write-Verbose ('temp file not removed: ' + $tmp) }
    }
}

function Read-NsTextRetry {
    # The text of a file another process may be swapping, or $null when there is no such file. The file is
    # opened with read, write AND delete sharing, so this open never fails because another reader or a
    # writer has the file open, and an open that is refused anyway is retried (-Tries, a growing pause)
    # before the error is thrown: a refused open is never reported as "no file". The sharing does NOT let a
    # writer's MoveFileEx swap through while this stream is open (measured: Windows answers "access denied"
    # even with Delete sharing); what gets the swap through is that this stream is closed straight after
    # ReadToEnd and that Write-NsFileAtomic retries a refused swap (-Tries). Decodes UTF-8 with or without a
    # BOM (Get-Content reads a BOM-less file as ANSI).
    param([Parameter(Mandatory = $true)][string]$Path, [int]$Tries = 10)
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    for ($attempt = 1; ; $attempt++) {
        try {
            $share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
            $fs = New-Object System.IO.FileStream($full, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $share)
            try {
                $reader = New-Object System.IO.StreamReader($fs, (New-Object System.Text.UTF8Encoding $false), $true)
                return $reader.ReadToEnd()
            }
            finally {
                $fs.Dispose()
            }
        }
        catch [System.IO.FileNotFoundException], [System.IO.DirectoryNotFoundException] {
            return $null
        }
        catch {
            if ($attempt -ge $Tries) { throw }
            Start-Sleep -Milliseconds ([Math]::Min(20 * $attempt, 100))
        }
    }
}

function Test-NsHomeFolder {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    return (Test-Path -LiteralPath (Join-Path $Candidate 'config\runtime.json'))
}

function Get-NsHome {
    # The launchers live in <home>\app\launchers, so the folder two levels up is the install home. That beats
    # NIGHTSHIFT_HOME when the variable is unset or stale: a process started before setup finished (the setup
    # server behind the Done screen, an old console, an Explorer that did not pick up the new user variable)
    # still has the old environment, and the first dev-mode run looked for the engine's venv under
    # %LOCALAPPDATA%\Nightshift and stopped with "Supervisor venv not found".
    # The resolved home is exported to this process so every child (engine, dashboard, studio job) that reads
    # NIGHTSHIFT_HOME (ns_common.paths.Paths.from_env) uses the same folder; without that the engine ran with
    # %LOCALAPPDATA%\Nightshift as its home (no channels, wrong disk, version 0.1.0 in Settings).
    # A PC can hold several Nightshift folders and has one NIGHTSHIFT_HOME, which may name another of them: a
    # launcher that is inside an install (<home>\app\launchers) runs THAT install, whatever the variable names,
    # and says so once when the variable names another install. Only a launcher that is in no install (a dev install's, in a
    # repository) follows the variable, which its launch script sets to the install it was started for.
    # This is stricter than 'ns' and 'studio' on purpose (ns_common.paths.Paths.from_env follows a variable
    # that was set FOR its process, such as $env:NIGHTSHIFT_HOME typed in a terminal): a launcher starts the
    # programs of an install and belongs to that install's version, and nothing in the product hands a
    # launcher inside one install the home of another. To start another install, use its own launchers.
    $fromEnv = $env:NIGHTSHIFT_HOME
    $fromHere = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    if ((Split-Path -Leaf (Split-Path -Parent $PSScriptRoot)) -ieq 'app' -and (Test-NsHomeFolder $fromHere)) {
        if ((Test-NsHomeFolder $fromEnv) -and ($fromEnv.TrimEnd('\') -ine $fromHere.TrimEnd('\'))) {
            Write-Host "Nightshift: using the install in $fromHere (this launcher is in it); NIGHTSHIFT_HOME names $fromEnv, which is another folder."
        }
        $env:NIGHTSHIFT_HOME = $fromHere
        return $fromHere
    }
    if (Test-NsHomeFolder $fromEnv) { return $fromEnv }
    if (Test-NsHomeFolder $fromHere) { $env:NIGHTSHIFT_HOME = $fromHere; return $fromHere }
    if ($fromEnv) { return $fromEnv }
    return (Join-Path $env:LOCALAPPDATA 'Nightshift')
}

function Set-NsHomeLocation {
    # Step out of the app folder. Set-Location only moves PowerShell's own location; the powershell.exe process
    # keeps the Win32 current directory it was started with, and Windows refuses to rename (swap) a folder that
    # is any process's current directory. [Environment]::CurrentDirectory is the Win32 one.
    $nsHome = Get-NsHome
    if (Test-Path -LiteralPath $nsHome) {
        Set-Location -LiteralPath $nsHome
        [Environment]::CurrentDirectory = $nsHome
    }
}

function Get-NsAppPath {
    return (Split-Path -Parent $PSScriptRoot)
}

function Get-NsSupervisorPort {
    $file = Join-Path (Get-NsHome) 'config\runtime.json'
    # the engine rewrites runtime.json by swapping a new file in; a read that Windows refuses for a moment is
    # retried, never taken for "no file" (that would send this launcher to the default port of another engine)
    $text = Read-NsTextRetry -Path $file
    if ($text) {
        $json = $text | ConvertFrom-Json
        if ($json.PSObject.Properties.Name -contains 'supervisor_port') { return [int]$json.supervisor_port }
    }
    return 8099
}

function Get-NsSupervisorUrl {
    return ('http://127.0.0.1:' + (Get-NsSupervisorPort))
}

function Get-NsVenvExe {
    param([string]$Venv, [string]$Name)
    return (Join-Path (Get-NsHome) ('venvs\' + $Venv + '\Scripts\' + $Name))
}

# --- Format-NsCommands: generated by ns_common.commandline.ps_formatter (FN-3); do not edit ---
function Format-NsCommands {
    # A stored text names the plain 'ns ...' and 'studio ...' words; shown here, each names this
    # install's own program by its full path, which a Command Prompt and PowerShell both take.
    param([string]$Text, [string]$NsHome)
    if (-not $Text -or -not $NsHome) { return $Text }
    try {
        $out = $Text
        foreach ($tool in @(@('ns', 'supervisor', 'ns.exe', 'backup|doctor|models|notify|restore|schedule|stop|tune|update|verify'), @('studio', 'studio', 'studio.exe', 'ask|calibrate|channel|clip|dashboard|learn|mark-uploaded|pause|resume|review|run|series|status|voice'))) {
            $file = Join-Path $NsHome ('cmd\' + $tool[0] + '.cmd')
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
                $file = Join-Path $NsHome ('venvs\' + $tool[1] + '\Scripts\' + $tool[2])
            }
            if ($file -cmatch '^[A-Za-z0-9:\\._-]+$') { $form = $file }
            elseif (($file -notmatch '[$`\u201C\u201D\u201E]') -and ($file -match '^[A-Za-z]:\\')) {
                $form = $file.Substring(0, 2) + '"' + $file.Substring(2) + '"'
            }
            else { $form = "& '" + [regex]::Replace($file, '[\u0027\u2018\u2019\u201A\u201B]', '$0$0') + "'" }
            $pattern = '(?<![\w.\\/:%-])' + $tool[0] + ' (?=(?:' + $tool[3] + ')(?![\w-]))'
            $out = [regex]::Replace($out, $pattern, $form.Replace('$', '$$') + ' ')
        }
        return $out
    }
    catch { return $Text }
}
# --- end of Format-NsCommands ---

function Test-NsSupervisor {
    try {
        # the liveness route answers at once; the status route runs nvidia-smi on every call
        Invoke-RestMethod -Uri ((Get-NsSupervisorUrl) + '/health') -TimeoutSec 3 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Get-NsStatus {
    # The engine's /status (it runs nvidia-smi, so only the Dashboard launcher asks); $null when it does not answer.
    try { return (Invoke-RestMethod -Uri ((Get-NsSupervisorUrl) + '/status') -TimeoutSec 20) }
    catch { return $null }
}

function Get-NsOwnSessionId {
    return [int][System.Diagnostics.Process]::GetCurrentProcess().SessionId
}

function Get-NsEngineSessionAction {
    # What the Dashboard shortcut does about the Windows session of a running engine.
    #   'ok'      the engine has a desktop (or an older engine does not say): use it
    #   'restart' the 03:00 task started it in session 0 (no desktop: Open Outbox, folder pickers and the
    #             Credential Manager do nothing there) and it is idle: restart it in this session
    #   'busy'    same, but a run is still using it: never interrupt that, open the dashboard on it
    param($Status, [int]$MySession)
    if ($null -eq $Status) { return 'ok' }
    $session = $Status.PSObject.Properties['session']
    if ($null -eq $session -or $null -eq $session.Value) { return 'ok' }
    if ($session.Value.interactive) { return 'ok' }
    if ($MySession -eq 0) { return 'ok' }
    $busy = $Status.PSObject.Properties['busy']
    if ($null -ne $busy -and $busy.Value) { return 'busy' }
    return 'restart'
}

function Stop-NsEngine {
    # Ask an idle engine to shut down and wait until it stops answering.
    param([int]$TimeoutSec = 60)
    try { Invoke-NsPost -Path '/shutdown' -Body @{ grace_s = 10 } -TimeoutSec 60 | Out-Null } catch { Write-Verbose 'shutdown request failed' }
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-NsSupervisor)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Close-NsHostWindows {
    # Close the Nightshift windows (dashboard and setup hosts) recorded in <home>\run\apphost-*.json: WM_CLOSE
    # first (the host saves its window state), the process after a grace period. Stale files are removed.
    param([int]$GraceSec = 8)
    $runDir = Join-Path (Get-NsHome) 'run'
    if (-not (Test-Path -LiteralPath $runDir)) { return }
    if ($null -eq ('NsCommon.Native' -as [type])) {
        Add-Type -Namespace NsCommon -Name Native -MemberDefinition '[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool PostMessage(System.IntPtr h, int msg, System.IntPtr w, System.IntPtr l);'
    }
    foreach ($file in @(Get-ChildItem -LiteralPath $runDir -Filter 'apphost-*.json' -ErrorAction SilentlyContinue)) {
        $hostPid = 0
        $hwnd = [IntPtr]::Zero
        # A file that cannot be read right now (the host is swapping it, Windows refuses the open past the
        # retries) is neither stale nor proof of "no window": leave it, and the window, alone. Only a file
        # that is gone or whose content is not a window record counts as stale.
        $text = $null
        try { $text = Read-NsTextRetry -Path $file.FullName }
        catch {
            Write-Host ('Could not read ' + $file.Name + ' (' + $_.Exception.Message + '); left alone.')
            continue
        }
        if ($null -eq $text) { continue }
        try {
            $j = $text | ConvertFrom-Json
            $hostPid = [int]$j.pid
            $hwnd = [IntPtr][int64]$j.hwnd
        }
        catch {
            $hostPid = 0
        }
        $proc = $null
        if ($hostPid -gt 0) { $proc = Get-Process -Id $hostPid -ErrorAction SilentlyContinue }
        if ($null -eq $proc) {
            Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue
            continue
        }
        if ($hwnd -ne [IntPtr]::Zero) { [void][NsCommon.Native]::PostMessage($hwnd, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) }
        if (-not $proc.WaitForExit($GraceSec * 1000)) { Stop-Process -Id $hostPid -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue
        Write-Host ('Closed the Nightshift window (' + $file.BaseName.Replace('apphost-', '') + ').')
    }
}

function Assert-NsVenvsFinished {
    param([Parameter(Mandatory = $true)][string]$Python)
    # D-39b: no engine on Python environments an update left unfinished (run\venv-repair.json). The check is
    # installer/stage2/web/venvrepair.py, run by the venv's interpreter with none of the venv's packages (-I -S):
    # it needs only the standard library, so it works on a half-filled environment too. Exit 4: refused, and
    # it printed why; then nothing is started. Any other failure of the check leaves the decision to the
    # engine, which runs the same check first thing (ns_supervisor/app.py refuse_unfinished).
    $gate = 'import sys; sys.path.insert(0, sys.argv[1]); from installer.stage2.web import venvrepair; sys.exit(venvrepair.main(sys.argv[1:]))'
    # (like the engine, the check never sees the setup window's NS_DASHBOARD_NO_HOST: see Start-NsSupervisor)
    $eap = $ErrorActionPreference
    $noHost = $env:NS_DASHBOARD_NO_HOST
    Remove-Item Env:NS_DASHBOARD_NO_HOST -ErrorAction SilentlyContinue
    $ErrorActionPreference = 'Continue'
    try {
        $said = @(& $Python -I -S -c $gate (Get-NsAppPath) (Get-NsHome) 2>&1 | ForEach-Object { [string]$_ })
        $gateCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $eap
        if ($null -ne $noHost) { $env:NS_DASHBOARD_NO_HOST = $noHost }
    }
    if ($gateCode -eq 4) { Exit-NsWithPause (($said -join ' ').Trim()) 1 }
}

function Start-NsSupervisor {
    param([switch]$Headless)
    $py = Get-NsVenvExe 'supervisor' 'python.exe'
    if (-not (Test-Path -LiteralPath $py)) { throw ('Supervisor venv not found: ' + $py + '. Run INSTALL.cmd first.') }
    Assert-NsVenvsFinished $py
    $argList = @('-m', 'ns_supervisor.app')
    $style = 'Minimized'
    if ($Headless) {
        $argList += '--headless'
        $style = 'Hidden'
    }
    # The engine must run from <home>\app, never from the folder setup was extracted to (stage 1 sets
    # PYTHONPATH to it), and never with the program folder as its working directory (a running process there
    # blocks the folder swap of an update). The venv has the app as an editable install.
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:NIGHTSHIFT_BOOTSTRAP_UV -ErrorAction SilentlyContinue
    # NS_DASHBOARD_NO_HOST (set by the setup window) is for the dashboard launcher alone, which still reads it
    # after this returns to decide on the host window: the engine and everything it starts must not inherit
    # it, or a later dashboard would never raise its window. Hide it for the start only.
    $noHost = $env:NS_DASHBOARD_NO_HOST
    Remove-Item Env:NS_DASHBOARD_NO_HOST -ErrorAction SilentlyContinue
    try {
        Start-Process -FilePath $py -ArgumentList $argList -WindowStyle $style -WorkingDirectory (Get-NsHome)
    }
    finally {
        if ($null -ne $noHost) { $env:NS_DASHBOARD_NO_HOST = $noHost }
    }
}

function Wait-NsHostReady {
    # After the window host was started as its own hidden process: 0 once its window is up (the host marks its
    # state file ready after WebView2 initialised), the host's exit code when it ended first (3 = WebView2
    # unavailable, the caller then opens a browser app window), and 0 when it is still starting at the deadline
    # (never open a second window beside a slow one).
    param($Process, [string]$Role = 'dashboard', [int]$TimeoutSec = 45)
    $null = $Process.Handle     # without it Windows PowerShell 5.1 cannot read ExitCode of a process it did not wait for
    $runFile = Join-Path (Get-NsHome) ('run\apphost-' + $Role + '.json')
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) { return [int]$Process.ExitCode }
        try {
            $stateText = Read-NsTextRetry -Path $runFile
            if ($stateText) {
                $state = $stateText | ConvertFrom-Json
                $names = $state.PSObject.Properties.Name
                if (($names -contains 'pid') -and ($names -contains 'ready') -and [int]$state.pid -eq $Process.Id -and $state.ready) { return 0 }
            }
        }
        catch {
            Write-Verbose 'the window host state file is not readable yet'
        }
        Start-Sleep -Milliseconds 300
    }
    return 0
}

function Wait-NsSupervisor {
    param([int]$TimeoutSec = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-NsSupervisor) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Invoke-NsPost {
    param([string]$Path, [hashtable]$Body, [int]$TimeoutSec = 30)
    $json = $Body | ConvertTo-Json
    $uri = (Get-NsSupervisorUrl) + $Path
    return (Invoke-RestMethod -Method Post -Uri $uri -ContentType 'application/json' -Body $json -TimeoutSec $TimeoutSec)
}

function Exit-NsWithPause {
    param([string]$Message, [int]$Code = 1)
    Write-Host $Message
    Read-Host 'Press Enter to close' | Out-Null
    exit $Code
}

function Start-NsDashboardService {
    # Ask a running engine to start the dashboard service. An engine left by `ns verify` or the nightly
    # task runs headless and never starts it on its own, so this is needed even when Test-NsSupervisor is true.
    param([int]$TimeoutSec = 120)
    try {
        Invoke-NsPost -Path '/warmup' -Body @{ service = 'dashboard' } -TimeoutSec $TimeoutSec | Out-Null
        return $true
    }
    catch {
        Write-Host ('The engine could not start the dashboard service: ' + $_.Exception.Message)
        return $false
    }
}

function Wait-NsPort {
    param([string]$Url, [int]$TimeoutSec = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 | Out-Null
            return $true
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

function Get-NsJsonProp {
    # The value of property -Name of a parsed JSON object, or $null when it is not there (or -Object is not an
    # object). Safe under strict mode, also for an object with no property at all ("tools": {} in a file the
    # owner edited: listing the names of nothing is an error there, asking for one by name is not).
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -isnot [System.Management.Automation.PSCustomObject]) { return $null }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -ne $prop) { return $prop.Value }
    return $null
}

function Set-NsJsonProp {
    # Puts -Value in property -Name of the parsed JSON object -Object; true when the object changed. With
    # -OnlyIfMissing a property that is already there (whatever its value) stays as the owner set it.
    param($Object, [string]$Name, $Value, [switch]$OnlyIfMissing)
    $prop = $Object.PSObject.Properties[$Name]   # by name: an object with no property at all is no error
    if ($null -ne $prop) {
        if ($OnlyIfMissing -and $null -ne $prop.Value) { return $false }   # JSON null counts as missing
        if ($prop.Value -ceq $Value) { return $false }
        $prop.Value = $Value
        return $true
    }
    Add-Member -InputObject $Object -NotePropertyName $Name -NotePropertyValue $Value
    return $true
}

function Read-NsLlamaConfig {
    # What the launchers need from config\runtime.json: Host, Port, Ctx (llama.flags.ctx) and ModelPath, each
    # with the value Python's Runtime model gives it when the file does not write it. $null when there is no
    # runtime.json; throws when it cannot be read as JSON.
    $runtime = Join-Path (Get-NsHome) 'config\runtime.json'
    if (-not (Test-Path -LiteralPath $runtime)) { return $null }
    $rt = (Read-NsTextRetry -Path $runtime) | ConvertFrom-Json
    $llama = Get-NsJsonProp $rt 'llama'
    $flags = Get-NsJsonProp $llama 'flags'
    $hostName = Get-NsJsonProp $llama 'host'
    $port = Get-NsJsonProp $llama 'port'
    $ctx = Get-NsJsonProp $flags 'ctx'
    $modelPath = Get-NsJsonProp $llama 'model_path'
    return [pscustomobject]@{
        Host      = $(if ($null -ne $hostName) { [string]$hostName } else { '127.0.0.1' })
        Port      = $(if ($null -ne $port) { [int]$port } else { 8080 })
        Ctx       = $(if ($null -ne $ctx) { [int]$ctx } else { 16384 })
        ModelPath = $(if ($null -ne $modelPath) { [string]$modelPath } else { '' })
    }
}

function Get-NsEngineLlamaCtx {
    # The context the running engine starts llama-server with: services.llama.ctx of its /status answer
    # (supervisor core.py status). $null when there is no answer or an older engine does not report it.
    param($Status)
    $llama = Get-NsJsonProp (Get-NsJsonProp $Status 'services') 'llama'
    $ctx = Get-NsJsonProp $llama 'ctx'
    if ($null -eq $ctx) { return $null }
    return [int]$ctx
}

function Get-NsCodeContextRefusal {
    # D-34: the text that stops Nightshift Code, or '' when it may go on. It is shown to the person who
    # started Nightshift Code, in its console, before the engine is asked for the graphics card.
    #   The number that counts is the one llama-server runs with. A running engine says it (-EngineCtx); it
    #   read config\runtime.json only when it started, so an edit of the file is not seen until it restarts.
    #   No engine running: the engine this launcher starts will read the file, so the file's number counts.
    #   Running engine that does not say (older, or its answer failed): the file's number is the best there is.
    #   No readable runtime.json and no engine answer: nothing to compare, no refusal.
    param([int]$MinCtx, $FileCtx, [bool]$EngineUp, $EngineCtx)
    $more = ' This context is also the one the nightly run uses, so it needs more memory for every job.'
    if ($EngineUp -and $null -ne $EngineCtx) { $ctx = [int]$EngineCtx; $fromEngine = $true }
    else { $ctx = $FileCtx; $fromEngine = $false }
    if ($null -eq $ctx) { return '' }
    if ($ctx -ge $MinCtx) {
        if ($EngineUp -and $null -eq $EngineCtx) {
            Write-Host ('The running engine does not say which context it uses; using the ' + $ctx + ' in config\runtime.json.')
        }
        return ''
    }
    $head = 'Nightshift Code needs ' + $MinCtx + ' tokens of context; '
    if (-not $fromEngine) {
        return ($head + 'this install has ' + $ctx + '. Set llama.flags.ctx in config\runtime.json to ' + $MinCtx + ' or more, then start Nightshift Code again.' + $more)
    }
    if ($null -ne $FileCtx -and [int]$FileCtx -ge $MinCtx) {
        return ($head + 'the running engine has ' + $ctx + ' (config\runtime.json says ' + $FileCtx + ', but the engine reads it only when it starts). Restart Nightshift: run Nightshift - Stop All, then start Nightshift Code again.' + $more)
    }
    return ($head + 'this install has ' + $ctx + '. Set llama.flags.ctx in config\runtime.json to ' + $MinCtx + ' or more, then run Nightshift - Stop All and start Nightshift Code again.' + $more)
}

# D-34: OpenCode's own prompt and tools come to about 15,300 tokens; the first request of a one-line question
# must stay at most half of the context. These are the values installer\stage2\steps\s07_configs.py writes
# (OPENCODE_PROMPT, TOOLS_OFF, CODE_OUTPUT_TOKENS); tests\test_opencode_budget.py keeps the two copies equal.
# What the pinned OpenCode (1.19.5) needs to change a file is read, edit and write, and there a tool switch is
# a permission: edit, write, patch and multiedit are ONE permission, so any of them set to false removes both
# edit and write. None of $script:NsOpenCodeEditingSwitches may ever be in $script:NsOpenCodeToolsOff.
$script:NsOpenCodeToolsOff = @('todowrite', 'todoread', 'webfetch', 'task', 'skill')
$script:NsOpenCodeEditingSwitches = @('read', 'edit', 'write', 'patch', 'multiedit')
# Switches an earlier build wrote and this one takes back (Sync-NsOpenCodePort says when it is Nightshift's).
$script:NsOpenCodeToolsWithdrawn = @('patch')
# SHA-256 of every earlier Nightshift prompt (its text with LF line ends, as UTF-8). A build prompt that is
# exactly one of these was written by Nightshift and is replaced; any other text is the owner's and stays.
$script:NsOpenCodeSupersededPrompts = @('8090d0ea5ad3c9800117cf679e06ce23f841b4e744095e0a4d963e12af31f287')
$script:NsOpenCodePrompt = @'
You are Nightshift Code, a coding assistant that runs on this computer with a small local model. Nothing you read or write leaves this machine. Your context is small: work in small steps and keep answers short.

- Work in the Projects folder you start in; each folder in it is a project. Leave it, or touch the Nightshift program folders and settings, only when asked.
- Read before you edit: open the file, look at the part that matters, then change it. Never guess what a file contains.
- Search with grep or glob and read a few lines at a time instead of whole files.
- Change the least you can: one edit, then say in a sentence or two what you changed and why. Add nothing that was not asked for.
- Run a command only when it helps, and say what it showed.
- If the request is unclear, ask one short question first. If you do not know, say so; never invent file names, functions or results.
- Answer in plain short sentences and show only the lines of code that matter.
- When done, name the files you changed. If a task is too big for your context, say so and offer the first small step.
'@

function Get-NsTextSha256 {
    # Lower-case hex SHA-256 of -Text as UTF-8 with LF line ends.
    param([string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text.Replace("`r`n", "`n"))
        return (-join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }))
    }
    finally { $sha.Dispose() }
}

function Get-NsOpenCodeEditingOff {
    # The keys of a parsed opencode.json that switch file editing off in the pinned OpenCode, as dotted names
    # ('tools.edit', 'permission.edit', 'agent.build.tools.write', ...); empty when Nightshift Code can change
    # a file. A tool switch counts when it is false, a permission when it is the text "deny".
    param($Config)
    $found = @()
    $build = Get-NsJsonProp (Get-NsJsonProp $Config 'agent') 'build'
    foreach ($place in @(@('', $Config), @('agent.build.', $build))) {
        $prefix = $place[0]
        $tools = Get-NsJsonProp $place[1] 'tools'
        $permission = Get-NsJsonProp $place[1] 'permission'
        foreach ($name in $script:NsOpenCodeEditingSwitches) {
            $value = Get-NsJsonProp $tools $name
            if ($value -is [bool] -and -not $value) { $found += ($prefix + 'tools.' + $name) }
        }
        foreach ($name in @('read', 'edit')) {
            $value = Get-NsJsonProp $permission $name
            if ($value -is [string] -and $value -eq 'deny') { $found += ($prefix + 'permission.' + $name) }
        }
    }
    return $found
}

function Sync-NsOpenCodePort {
    # OpenCode talks straight to llama-server, and config\opencode.json bakes the address in when it is
    # written at install time. If the llama address changed since (ns tune, a hand edit), OpenCode would talk
    # to the wrong port, so point the baseURL at the current one right before it starts. -BaseUrl is the
    # address the engine reported when it started llama-server; without it, runtime.json llama.host/port is
    # used.
    # The same pass brings the file up to the budget of D-34 (a file written by 0.1.14 has none): the model's
    # limit.context follows runtime.json llama.flags.ctx, its name follows llama.model_path, and the tool
    # switches and the short build prompt are added when they are missing. Only those values are Nightshift's:
    # baseURL, limit.context and the model name are set; every other key, including a limit.output, a tool
    # switch or a prompt the owner changed, stays as it is. The file is left alone when everything matches or
    # when it does not exist, and a problem here is a note, never a reason to stop the launcher.
    # Two things an earlier build of 0.1.15 wrote are taken back, each only where it is Nightshift's:
    #   "patch": false under tools. In the pinned OpenCode it removes the edit and write tools, so Nightshift
    #     Code could not change a file. No release ever wrote it and OpenCode gives this model no patch tool,
    #     so the key has no other effect: it is Nightshift's and is removed, with a note. The owner's way to
    #     say "read only" is a key Nightshift never wrote ("edit" or "write": false, or permission edit
    #     "deny"); when one of those is there, editing is off by the owner's choice, "patch" is left as it
    #     is, and the note names the key instead, at every start.
    #   The longer build prompt. A prompt that is exactly an earlier Nightshift text (by SHA-256) is replaced
    #     by the current one; any other text is the owner's and stays.
    # -Ctx is the context llama-server really runs with (the engine's answer, see Get-NsEngineLlamaCtx); without
    # it runtime.json says (an engine reads that file only when it starts, so the two can differ).
    # A key the owner set to null counts as missing.
    param([string]$BaseUrl = '', [int]$Ctx = 0)
    $file = Join-Path (Get-NsHome) 'config\opencode.json'
    $withdrawn = @()   # switches taken out of the parsed file; said only once the file is really written
    try {
        if (-not (Test-Path -LiteralPath $file)) { return }
        $llama = $null
        try { $llama = Read-NsLlamaConfig }
        catch {
            if ($BaseUrl -notmatch '^https?://') { throw }
            Write-Host ('Could not read runtime.json (' + $_.Exception.Message + '); the context and model name in ' + $file + ' are left as they are.')
        }
        if ($BaseUrl -match '^https?://') {
            $url = $BaseUrl
        }
        elseif ($null -ne $llama) {
            $url = 'http://' + $llama.Host + ':' + $llama.Port + '/v1'
        }
        else {
            $url = 'http://127.0.0.1:8080/v1'
        }
        # -Encoding UTF8: Windows PowerShell 5.1 reads a BOM-less file as ANSI, which would double-encode any
        # non-ASCII text the owner put in the file when it is written back as UTF-8 below.
        $cfg = (Read-NsTextRetry -Path $file) | ConvertFrom-Json
        $local = Get-NsJsonProp (Get-NsJsonProp $cfg 'provider') 'local'
        if ($null -eq $local) { return }
        $changed = $false
        $options = Get-NsJsonProp $local 'options'
        if ($null -ne $options -and (Set-NsJsonProp $options 'baseURL' $url)) { $changed = $true }

        $code = Get-NsJsonProp (Get-NsJsonProp $local 'models') 'code'
        if ($null -ne $code -and $null -ne $llama) {
            $name = 'Local model'
            if ($llama.ModelPath) {
                $stem = [System.IO.Path]::GetFileNameWithoutExtension($llama.ModelPath)
                if ($stem) { $name = $stem + ' (local)' }
            }
            if (Set-NsJsonProp $code 'name' $name) { $changed = $true }
            $codeCtx = $llama.Ctx
            if ($Ctx -gt 0) { $codeCtx = $Ctx }
            $limit = Get-NsJsonProp $code 'limit'
            if ($limit -is [System.Management.Automation.PSCustomObject]) {
                if (Set-NsJsonProp $limit 'context' $codeCtx) { $changed = $true }
                if (Set-NsJsonProp $limit 'output' 4096 -OnlyIfMissing) { $changed = $true }
            }
            elseif ($null -eq $limit -and (Set-NsJsonProp $code 'limit' ([pscustomobject][ordered]@{ context = $codeCtx; output = 4096 }) -OnlyIfMissing)) {
                $changed = $true
            }
        }

        $switches = [ordered]@{}
        foreach ($tool in $script:NsOpenCodeToolsOff) { $switches[$tool] = $false }
        if (Set-NsJsonProp $cfg 'tools' ([pscustomobject]$switches) -OnlyIfMissing) {
            $changed = $true
        }
        else {
            $tools = Get-NsJsonProp $cfg 'tools'
            if ($tools -is [System.Management.Automation.PSCustomObject]) {   # anything else is the owner's: left alone
                # the owner's own "read only" keys: whatever switches editing off except a withdrawn switch
                $ownerOff = @(Get-NsOpenCodeEditingOff $cfg | Where-Object { $script:NsOpenCodeToolsWithdrawn -notcontains ($_ -replace '^tools[.]', '') })
                foreach ($tool in $script:NsOpenCodeToolsWithdrawn) {
                    $value = Get-NsJsonProp $tools $tool
                    if ($value -is [bool] -and -not $value -and $ownerOff.Count -eq 0) {
                        $tools.PSObject.Properties.Remove($tool)
                        $changed = $true
                        $withdrawn += $tool
                    }
                }
                foreach ($tool in $script:NsOpenCodeToolsOff) {
                    if (Set-NsJsonProp $tools $tool $false -OnlyIfMissing) { $changed = $true }
                }
            }
        }
        $editingOff = @(Get-NsOpenCodeEditingOff $cfg)
        if ($editingOff.Count -gt 0) {
            Write-Host ('Nightshift Code can read files and run commands but cannot change a file: ' + $file + ' switches editing off (' + ($editingOff -join ', ') + '). Remove or change that setting and start Nightshift Code again to bring editing back.')
        }

        $prompt = $script:NsOpenCodePrompt.Replace("`r`n", "`n")
        $buildAgent = [pscustomobject][ordered]@{ prompt = $prompt }
        if (Set-NsJsonProp $cfg 'agent' ([pscustomobject][ordered]@{ build = $buildAgent }) -OnlyIfMissing) {
            $changed = $true
        }
        else {
            $agent = Get-NsJsonProp $cfg 'agent'
            if ($null -ne $agent) {
                if (Set-NsJsonProp $agent 'build' $buildAgent -OnlyIfMissing) {
                    $changed = $true
                }
                else {
                    $build = Get-NsJsonProp $agent 'build'
                    if ($null -ne $build) {
                        $has = Get-NsJsonProp $build 'prompt'
                        if ($has -is [string] -and $script:NsOpenCodeSupersededPrompts -contains (Get-NsTextSha256 $has)) {
                            if (Set-NsJsonProp $build 'prompt' $prompt) { $changed = $true }
                        }
                        elseif (Set-NsJsonProp $build 'prompt' $prompt -OnlyIfMissing) { $changed = $true }
                    }
                }
            }
        }

        if (-not $changed) { return }
        $json = ($cfg | ConvertTo-Json -Depth 20) + "`n"
        Write-NsFileAtomic -Path $file -Text $json
        foreach ($tool in $withdrawn) {
            Write-Host ('Removed "' + $tool + '": false from ' + $file + ': in this OpenCode it also removes the edit and write tools, so Nightshift Code could not change a file. To keep Nightshift Code read-only, set "edit": false under "tools" instead.')
        }
    }
    catch {
        Write-Host ('Could not update the port in ' + $file + ': ' + $_.Exception.Message)
        foreach ($tool in $withdrawn) {
            Write-Host ('So "' + $tool + '": false is still in that file, and with it Nightshift Code can read files and run commands but cannot change a file. Close whatever keeps the file open and start Nightshift Code again, or remove that line yourself.')
        }
    }
}
