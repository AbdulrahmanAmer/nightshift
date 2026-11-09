Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift Code" shortcut: take the GPU as owner "opencode", run OpenCode, release on exit.
. (Join-Path $PSScriptRoot '_common.ps1')

$owner = 'opencode'
$opencode = Join-Path (Get-NsHome) 'bin\opencode\opencode.exe'
if (-not (Test-Path -LiteralPath $opencode)) {
    Exit-NsWithPause ('OpenCode is not installed (missing ' + $opencode + '). Re-run INSTALL.cmd with Nightshift Code enabled.')
}

if (-not (Test-NsSupervisor)) {
    Start-NsSupervisor -Headless
    if (-not (Wait-NsSupervisor -TimeoutSec 90)) { Exit-NsWithPause 'The Nightshift engine did not start.' }
}

Write-Host 'Loading the coding model (a minute or two the first time)...'
$body = @{ service = 'llama'; owner = $owner; model_alias = 'code'; wait = $true; timeout_s = 7200 }
Invoke-NsPost -Path '/acquire' -Body $body -TimeoutSec 7500 | Out-Null

Sync-NsOpenCodePort

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
