Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift Code" shortcut: take the GPU as owner "opencode", run OpenCode, release on exit.
. (Join-Path $PSScriptRoot '_common.ps1')

# WP3: every step of this launcher goes to logs\code-<date>.log (Write-NsLaunchLog), so a Code window that
# ended with the model still loaded, or a model that died under it, can be read back afterwards.
$script:NsLaunchLogName = 'code'
$owner = 'opencode'
$script:NsCodeHeld = $false

function Invoke-NsCodeRelease {
    # Give the card back once (an error after the acquire, or OpenCode ended). The engine unloads the coding
    # model at its next check unless another owner takes it (WP3); a release the engine did no longer need
    # (it freed the card itself) answers ok=False, which is fine.
    param([string]$Why)
    if (-not $script:NsCodeHeld) { return }
    $script:NsCodeHeld = $false
    try {
        $answer = Invoke-NsPost -Path '/release' -Body @{ service = 'llama'; owner = $owner }
        $ok = ''
        if ($null -ne $answer -and $null -ne $answer.PSObject.Properties['ok']) { $ok = [string]$answer.ok }
        Write-NsLaunchLog 'release' ($Why + ': the engine answered ok=' + $ok)
    }
    catch {
        Write-NsLaunchLog 'release' ($Why + ': failed: ' + $_.Exception.Message) 'ERROR'
        Write-Host ('Could not release the GPU automatically: ' + $_.Exception.Message)
    }
}

# An error nothing below catches: logged, the card given back if it was taken, and the window kept open
# with the message (the shortcut's console would otherwise close with it).
trap {
    $failure = [string]$_.Exception.Message
    $where = ''
    try { $where = [string]$_.InvocationInfo.PositionMessage } catch { $where = '' }
    Write-NsLaunchLog 'error' ($failure + ' ' + $where) 'ERROR'
    Invoke-NsCodeRelease 'after the error'
    Exit-NsWithPause ('Nightshift Code stopped: ' + $failure + ' (The step it stopped at is in the newest code-*.log in the logs folder of the Nightshift home.)')
}

Write-NsLaunchLog 'launcher' ('started: ' + $PSCommandPath + ', pid ' + $PID + ', session ' + (Get-NsOwnSessionId) + ', PowerShell ' + $PSVersionTable.PSVersion)

$opencode = Join-Path (Get-NsHome) 'bin\opencode\opencode.exe'
if (-not (Test-Path -LiteralPath $opencode)) {
    Exit-NsWithPause ('OpenCode is not installed (missing ' + $opencode + '). Re-run INSTALL.cmd with Nightshift Code enabled.')
}

# D-34: OpenCode's first request is about half of a 16384-token context (installer s07_configs MIN_CODE_CONTEXT).
# With less, the first question would fail after the model has been loaded, so say so now and take nothing.
# The number that counts is the one llama-server runs with, which the running engine reports in /status; the
# engine read config\runtime.json only when it started (see Get-NsCodeContextRefusal for the cases).
$minCtx = 16384
$fileCtx = $null
try {
    $llamaCfg = Read-NsLlamaConfig
    if ($null -ne $llamaCfg) { $fileCtx = $llamaCfg.Ctx }
}
catch { Write-Host ('Could not read runtime.json to check the context: ' + $_.Exception.Message) }
$engineUp = Test-NsSupervisor
$engineCtx = $null
if ($engineUp) { $engineCtx = Get-NsEngineLlamaCtx -Status (Get-NsStatus) }
$refusal = Get-NsCodeContextRefusal -MinCtx $minCtx -FileCtx $fileCtx -EngineUp $engineUp -EngineCtx $engineCtx
if ($refusal) { Exit-NsWithPause $refusal }

if (-not (Test-NsSupervisor)) {
    Write-NsLaunchLog 'engine' 'not running: starting it headless'
    Start-NsSupervisor -Headless
    if (-not (Wait-NsSupervisor -TimeoutSec 90)) { Exit-NsWithPause 'The Nightshift engine did not start.' }
}

Write-Host 'Loading the coding model (a minute or two the first time)...'
# pid: this console's own process. However Code is closed, the card comes back: OpenCode ending (normally or
# ended by its pid) runs the finally below; closing the window with X ends this process too, and the engine's
# next check (about a second) sees the pid gone, frees the GPU and unloads the model (WP3, F70).
$body = @{ service = 'llama'; owner = $owner; model_alias = 'code'; wait = $true; timeout_s = 7200; pid = $PID }
Write-NsLaunchLog 'acquire' ('asking the engine for the coding model as ' + $owner + ', pid ' + $PID)
$askedAt = Get-Date
$script:NsCodeHeld = $true      # from here an error releases: the engine may have granted before failing
$ep = Invoke-NsPost -Path '/acquire' -Body $body -TimeoutSec 7500

# /acquire answers with the exact address of the llama-server it started; use it rather than guessing.
$baseUrl = ''
if ($null -ne $ep -and ($ep.PSObject.Properties.Name -contains 'endpoint')) {
    $endpoint = [string]$ep.endpoint
    if ($endpoint -match '^https?://') { $baseUrl = $endpoint.TrimEnd('/') + '/v1' }
}
Write-NsLaunchLog 'acquire' ('granted after ' + [int]((Get-Date) - $askedAt).TotalSeconds + ' s: endpoint ' + $baseUrl)
# OpenCode is told the context llama-server runs with now (the engine started it from its own copy of the
# settings), not what the file says; 0 = the engine did not say, the file's number is used.
$runningCtx = Get-NsEngineLlamaCtx -Status (Get-NsStatus)
$syncCtx = 0
if ($null -ne $runningCtx) { $syncCtx = $runningCtx }
Sync-NsOpenCodePort -BaseUrl $baseUrl -Ctx $syncCtx

try {
    $env:OPENCODE_CONFIG = Join-Path (Get-NsHome) 'config\opencode.json'
    $projects = Join-Path $env:USERPROFILE 'Projects'
    New-Item -ItemType Directory -Force -Path $projects | Out-Null
    Push-Location $projects
    try {
        Write-NsLaunchLog 'opencode' ('starting ' + $opencode + ' (context ' + $syncCtx + ')')
        $ranAt = Get-Date
        & $opencode
        Write-NsLaunchLog 'opencode' ('ended with exit code ' + $LASTEXITCODE + ' after ' + [int]((Get-Date) - $ranAt).TotalSeconds + ' s')
    }
    finally {
        Pop-Location
    }
}
finally {
    Invoke-NsCodeRelease 'OpenCode ended'
}
