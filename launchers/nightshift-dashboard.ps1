Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift Dashboard" shortcut: start the engine minimized if needed, then open the web UI.
. (Join-Path $PSScriptRoot '_common.ps1')

# Neither this launcher nor the window host it starts may hold the app folder as its working directory, or
# `ns update` cannot swap the folder while the dashboard window is open.
Set-NsHomeLocation

$url = 'http://127.0.0.1:8100'

# An engine the 03:00 task started runs in session 0, which has no desktop: the dashboard it starts shows, but
# Open Outbox, the folder pickers and the Credential Manager do nothing there. Restart an idle one in this
# session; never interrupt a run that is still going (the page then opens on it, with those limits).
if (Test-NsSupervisor) {
    $engineAction = Get-NsEngineSessionAction -Status (Get-NsStatus) -MySession (Get-NsOwnSessionId)
    if ($engineAction -eq 'restart') {
        Write-Host 'The engine was started by the nightly task and has no desktop. Restarting it in this session.'
        if (-not (Stop-NsEngine)) { Write-Host 'The old engine did not stop; opening the dashboard on it.' }
    }
    elseif ($engineAction -eq 'busy') {
        Write-Host 'The nightly run is still using the engine. The dashboard opens on it; folder buttons work after the run.'
    }
}

if (-not (Test-NsSupervisor)) {
    Start-NsSupervisor
    if (-not (Wait-NsSupervisor -TimeoutSec 90)) {
        Exit-NsWithPause 'The Nightshift engine did not start. Run "Nightshift - Diagnostics" and send the zip.'
    }
}

# A freshly started engine warms the dashboard itself. One that was already running may be headless (left by
# `ns verify --quick` or the nightly task) and would never start it, so always ask. It is a no-op when the
# service is up. Then wait for the page; never open a dead address.
[void](Start-NsDashboardService)
if (-not (Wait-NsPort -Url $url -TimeoutSec 60)) {
    Exit-NsWithPause 'The Nightshift dashboard did not start. Run "Nightshift - Diagnostics" and send the zip.'
}

# NS_DASHBOARD_NO_HOST=1 (set by the setup window) skips it: a running host window would be raised instead.
# First choice: the frameless WebView2 window (nightshift-host.ps1). It exits 3 after printing one line when
# WebView2 or its DLLs are missing, and 0 when the window closed or an open window was brought to the front.
# Closing that window never stops the engine.
# The host runs as its own hidden PowerShell process and this launcher ends as soon as the window is up: a
# launcher that kept the window in its own process left its console open behind the dashboard (Windows
# Terminal, the default console on Windows 11, ignores a request to hide it), and closing that console closed
# the dashboard window too.
$hostScript = Join-Path $PSScriptRoot 'nightshift-host.ps1'
if ((Test-Path -LiteralPath $hostScript) -and ($env:NS_DASHBOARD_NO_HOST -ne '1')) {
    $hostCode = 3
    try {
        $appPath = Get-NsAppPath
        $hostArguments = "-NoProfile -STA -ExecutionPolicy Bypass -File `"$hostScript`" -Url $url -AppPath `"$appPath`""
        $hostProcess = Start-Process -FilePath 'powershell.exe' -ArgumentList $hostArguments -WindowStyle Hidden -WorkingDirectory (Get-NsHome) -PassThru
        $hostCode = Wait-NsHostReady -Process $hostProcess
    }
    catch {
        Write-Host "The Nightshift window could not start ($($_.Exception.Message)); opening the browser app window instead."
        $hostCode = 3
    }
    # Only a clean 0 means the window runs (or an open one was raised). 3 = WebView2 unavailable, anything else
    # is a failure of the host: both fall through to the browser app window.
    if ($hostCode -eq 0) { return }
    if ($hostCode -ne 3) { Write-Host "The Nightshift window stopped with exit code $hostCode; opening the browser app window instead." }
}

# Fallback: an app window (no tabs or address bar) in Edge or Chrome when one is installed; otherwise the default browser.
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
        # --no-first-run / --no-default-browser-check: on a PC where Edge was never opened, its welcome and
        # sign-in pages would otherwise open in place of, or over, the dashboard.
        Start-Process -FilePath $browser -ArgumentList @("--app=$url", '--no-first-run', '--no-default-browser-check')
        return
    }
    catch {
        # fall through to the default browser
    }
}
Start-Process $url
