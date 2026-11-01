Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift Dashboard" shortcut: start the engine minimized if needed, then open the web UI.
. (Join-Path $PSScriptRoot '_common.ps1')

$url = 'http://127.0.0.1:8100'

if (-not (Test-NsSupervisor)) {
    Start-NsSupervisor
    if (-not (Wait-NsSupervisor -TimeoutSec 90)) {
        Exit-NsWithPause 'The Nightshift engine did not start. Run "Nightshift - Diagnostics" and send the zip.'
    }
}

# The engine starts the dashboard service itself; give the page a moment to come up.
$deadline = (Get-Date).AddSeconds(60)
$up = $false
while (-not $up -and (Get-Date) -lt $deadline) {
    try {
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
        $up = $true
    }
    catch {
        Start-Sleep -Milliseconds 500
    }
}

# Prefer an app window (no tabs or address bar) in Edge or Chrome when one is installed; otherwise the default browser.
function Find-NsAppBrowser {
    foreach ($leaf in @('Microsoft\Edge\Application\msedge.exe', 'Google\Chrome\Application\chrome.exe')) {
        foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
            if ($root) {
                $candidate = Join-Path $root $leaf
                if (Test-Path -LiteralPath $candidate) { return $candidate }
            }
        }
    }
    foreach ($name in @('msedge.exe', 'chrome.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

$browser = Find-NsAppBrowser
if ($browser) {
    try {
        Start-Process -FilePath $browser -ArgumentList @("--app=$url")
        return
    }
    catch {
        # fall through to the default browser
    }
}
Start-Process $url
