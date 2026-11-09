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
    # opened with sharing that lets the writer's swap through (read, write AND delete), and an open that is
    # refused anyway is retried (-Tries, a growing pause) before the error is thrown: a refused open is never
    # reported as "no file". Decodes UTF-8 with or without a BOM (Get-Content reads a BOM-less file as ANSI).
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
    $fromEnv = $env:NIGHTSHIFT_HOME
    if (Test-NsHomeFolder $fromEnv) { return $fromEnv }
    $fromHere = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
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

function Start-NsSupervisor {
    param([switch]$Headless)
    $py = Get-NsVenvExe 'supervisor' 'python.exe'
    if (-not (Test-Path -LiteralPath $py)) { throw ('Supervisor venv not found: ' + $py + '. Run INSTALL.cmd first.') }
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

function Sync-NsOpenCodePort {
    # OpenCode talks straight to llama-server, and config\opencode.json bakes the address in when it is
    # written at install time. If the llama address changed since (ns tune, a hand edit), OpenCode would talk
    # to the wrong port, so point the baseURL at the current one right before it starts. -BaseUrl is the
    # address the engine reported when it started llama-server; without it, runtime.json llama.host/port is
    # used. Only that one value changes; the file is left alone when it already matches or does not exist,
    # and a problem here is a note, never a reason to stop the launcher.
    param([string]$BaseUrl = '')
    $file = Join-Path (Get-NsHome) 'config\opencode.json'
    try {
        if (-not (Test-Path -LiteralPath $file)) { return }
        if ($BaseUrl -match '^https?://') {
            $url = $BaseUrl
        }
        else {
            $hostName = '127.0.0.1'
            $port = 8080
            $runtime = Join-Path (Get-NsHome) 'config\runtime.json'
            if (Test-Path -LiteralPath $runtime) {
                $rt = (Read-NsTextRetry -Path $runtime) | ConvertFrom-Json
                if ($rt.PSObject.Properties.Name -contains 'llama') {
                    $llama = $rt.llama
                    if ($llama.PSObject.Properties.Name -contains 'host') { $hostName = [string]$llama.host }
                    if ($llama.PSObject.Properties.Name -contains 'port') { $port = [int]$llama.port }
                }
            }
            $url = 'http://' + $hostName + ':' + $port + '/v1'
        }
        # -Encoding UTF8: Windows PowerShell 5.1 reads a BOM-less file as ANSI, which would double-encode any
        # non-ASCII text the owner put in the file when it is written back as UTF-8 below.
        $cfg = (Read-NsTextRetry -Path $file) | ConvertFrom-Json
        if (-not ($cfg.PSObject.Properties.Name -contains 'provider')) { return }
        if (-not ($cfg.provider.PSObject.Properties.Name -contains 'local')) { return }
        $options = $cfg.provider.local.options
        if ($options.baseURL -eq $url) { return }
        $options.baseURL = $url
        $json = ($cfg | ConvertTo-Json -Depth 20) + "`n"
        Write-NsFileAtomic -Path $file -Text $json
    }
    catch {
        Write-Host ('Could not update the port in ' + $file + ': ' + $_.Exception.Message)
    }
}
