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
$hostScript = Join-Path $PSScriptRoot 'nightshift-host.ps1'
if ((Test-Path -LiteralPath $hostScript) -and ($env:NS_DASHBOARD_NO_HOST -ne '1')) {
    $hostCode = 3
    $hidden = $false
    try {
        # Hide this launcher's own console while the window is open (best effort; ignored by Windows Terminal).
        Add-Type -Namespace NsDash -Name Console -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")] public static extern System.IntPtr GetConsoleWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int cmd);
'@
        $consoleHandle = [NsDash.Console]::GetConsoleWindow()
        if ($consoleHandle -ne [IntPtr]::Zero) { [void][NsDash.Console]::ShowWindow($consoleHandle, 0); $hidden = $true }
    }
    catch {
        $hidden = $false
    }
    try {
        $global:LASTEXITCODE = 0
        & $hostScript -Url $url -AppPath (Get-NsAppPath)
        $hostCode = $LASTEXITCODE
    }
    catch {
        Write-Host "The Nightshift window could not start ($($_.Exception.Message)); opening the browser app window instead."
        $hostCode = 3
    }
    if ($hostCode -ne 3) { return }
    if ($hidden) { [void][NsDash.Console]::ShowWindow($consoleHandle, 5) }
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
        Start-Process -FilePath $browser -ArgumentList @("--app=$url")
        return
    }
    catch {
        # fall through to the default browser
    }
}
Start-Process $url
