Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift Code" shortcut: take the GPU as owner "opencode", run OpenCode, release on exit.
. (Join-Path $PSScriptRoot '_common.ps1')

$owner = 'opencode'
$opencode = Join-Path (Get-NsHome) 'bin\opencode\opencode.exe'
if (-not (Test-Path -LiteralPath $opencode)) {
    Exit-NsWithPause ('OpenCode is not installed (missing ' + $opencode + '). Re-run INSTALL.cmd with Nightshift Code enabled.')
}

# D-34: OpenCode's first request is about half of a 16384-token context (installer s07_configs MIN_CODE_CONTEXT).
# With less, the first question would fail after the model has been loaded, so say so now and take nothing.
$minCtx = 16384
$llamaCfg = $null
try { $llamaCfg = Read-NsLlamaConfig }
catch { Write-Host ('Could not read runtime.json to check the context: ' + $_.Exception.Message) }
if ($null -ne $llamaCfg -and $llamaCfg.Ctx -lt $minCtx) {
    Exit-NsWithPause ('Nightshift Code needs ' + $minCtx + ' tokens of context; this install has ' + $llamaCfg.Ctx + '. Set llama.flags.ctx in config\runtime.json to 16384 or more (it needs more memory), then start Nightshift Code again.')
}

if (-not (Test-NsSupervisor)) {
    Start-NsSupervisor -Headless
    if (-not (Wait-NsSupervisor -TimeoutSec 90)) { Exit-NsWithPause 'The Nightshift engine did not start.' }
}

Write-Host 'Loading the coding model (a minute or two the first time)...'
# pid: this console's own process. Closing the window with X skips the finally below; the engine then frees
# the GPU itself once this process is gone.
$body = @{ service = 'llama'; owner = $owner; model_alias = 'code'; wait = $true; timeout_s = 7200; pid = $PID }
$ep = Invoke-NsPost -Path '/acquire' -Body $body -TimeoutSec 7500

# /acquire answers with the exact address of the llama-server it started; use it rather than guessing.
$baseUrl = ''
if ($null -ne $ep -and ($ep.PSObject.Properties.Name -contains 'endpoint')) {
    $endpoint = [string]$ep.endpoint
    if ($endpoint -match '^https?://') { $baseUrl = $endpoint.TrimEnd('/') + '/v1' }
}
Sync-NsOpenCodePort -BaseUrl $baseUrl

try {
    $env:OPENCODE_CONFIG = Join-Path (Get-NsHome) 'config\opencode.json'
    $projects = Join-Path $env:USERPROFILE 'Projects'
    New-Item -ItemType Directory -Force -Path $projects | Out-Null
    Push-Location $projects
    try {
        & $opencode
    }
    finally {
        Pop-Location
    }
}
finally {
    try {
        Invoke-NsPost -Path '/release' -Body @{ service = 'llama'; owner = $owner } | Out-Null
    }
    catch {
        Write-Host ('Could not release the GPU automatically: ' + $_.Exception.Message)
    }
}
