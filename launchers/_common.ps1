Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Shared helpers for the Nightshift launchers (dot-sourced; not a shortcut target).

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
    $fromEnv = $env:NIGHTSHIFT_HOME
    if (Test-NsHomeFolder $fromEnv) { return $fromEnv }
    $fromHere = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    if (Test-NsHomeFolder $fromHere) { return $fromHere }
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
    if (Test-Path -LiteralPath $file) {
        $json = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
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
    Start-Process -FilePath $py -ArgumentList $argList -WindowStyle $style -WorkingDirectory (Get-NsHome)
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
            if (Test-Path -LiteralPath $runFile) {
                $state = Get-Content -Raw -LiteralPath $runFile | ConvertFrom-Json
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
