<#
.SYNOPSIS
    Frameless Nightshift window around WebView2 (docs/design/installer-ui.md section 6).
    nightshift-host.ps1 -Url <http://127.0.0.1:port/...> [-Title 'Nightshift'] [-Width 1366] [-Height 768]
                        [-AppPath <dir>] [-Role setup|dashboard] [-CheckOnly]
    Exit codes: 0 window closed (or an already running window was brought to the front), 3 WebView2
    unavailable (the caller falls back to an Edge/Chrome app window), 4 bad arguments.
    Needs an STA thread (Windows PowerShell 5.1 consoles are STA by default).
    Single instance per role (-Role, default dashboard): the named mutex Global\Nightshift.AppHost.<role>; the
    running host records its window in %NIGHTSHIFT_HOME%\run\apphost-<role>.json {hwnd, pid} and a second
    start of the same role raises that window instead. The setup window (-Role setup) and the dashboard window
    (-Role dashboard) are independent, so opening the dashboard while setup is open starts a second window.
    Closing the window never stops the engine.
    Messages from the page (window.chrome.webview.postMessage), all JSON:
      {cmd: 'minimize'} {cmd: 'toggle-maximize'} {cmd: 'drag-start'} {cmd: 'close'} {cmd: 'close-ack'}
      {cmd: 'ready'} {cmd: 'open-external', url: 'https://...'}
    The window icon is <AppPath>\ns_dashboard\static\brand\nightshift.ico when that file exists.
    Messages to the page: {event: 'window-state', state: 'normal|maximized|minimized', maximized: bool}
    and {event: 'close-requested'}.
#>
[CmdletBinding()]
param(
    [string]$Url = '',
    [string]$Title = 'Nightshift',
    [int]$Width = 1366,
    [int]$Height = 768,
    [string]$AppPath = '',
    [ValidateSet('setup', 'dashboard')][string]$Role = 'dashboard',
    [switch]$CheckOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $AppPath) { $AppPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }

$ClientKey = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$MutexName = "Global\Nightshift.AppHost.$Role"
$BackgroundHex = '#15131d'
$MinWidth = 1024
$MinHeight = 640
$CloseGraceMs = 1500

function Get-HostHome {
    if ($env:NIGHTSHIFT_HOME) { return $env:NIGHTSHIFT_HOME }
    return (Join-Path $env:LOCALAPPDATA 'Nightshift')
}

function Get-WebView2Version {
    $roots = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$ClientKey",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$ClientKey",
        "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$ClientKey"
    )
    foreach ($r in $roots) {
        try {
            $item = Get-ItemProperty -LiteralPath $r -ErrorAction Stop
            if ($item.PSObject.Properties.Name -contains 'pv') {
                $pv = [string]$item.pv
                if ($pv -and $pv -ne '0.0.0.0') { return $pv }
            }
        }
        catch {
            continue
        }
    }
    return $null
}

# The installer unpacks the three DLLs into <NIGHTSHIFT_HOME>\bin\webview2; a copy next to the app also works.
$dlls = @('Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.Wpf.dll', 'WebView2Loader.dll')
$dir = Join-Path $AppPath 'bin\webview2'
$missing = @()
foreach ($candidate in @((Join-Path (Get-HostHome) 'bin\webview2'), (Join-Path $AppPath 'bin\webview2'))) {
    $gone = @($dlls | Where-Object { -not (Test-Path -LiteralPath (Join-Path $candidate $_)) })
    $dir = $candidate
    $missing = $gone
    if ($gone.Count -eq 0) { break }
}
$runtime = Get-WebView2Version

if ($CheckOnly) {
    [pscustomobject]@{ webview2 = ($null -ne $runtime -and $missing.Count -eq 0); runtime = $runtime; missing = $missing } |
        ConvertTo-Json -Compress
    exit 0
}
if (-not $Url) {
    Write-Host 'nightshift-host: -Url is required.'
    exit 4
}
$startUri = $null
if (-not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$startUri) -or $startUri.Scheme -notin @('http', 'https')) {
    Write-Host 'nightshift-host: -Url must be an http or https address.'
    exit 4
}
if ($null -eq $runtime -or $missing.Count -gt 0) {
    Write-Host "WebView2 unavailable (runtime=$runtime missing=$($missing -join ',')); opening the browser app window instead."
    exit 3
}
if (-not [Environment]::Is64BitProcess) {
    Write-Host 'WebView2 unavailable (32-bit PowerShell); opening the browser app window instead.'
    exit 3
}
if ([Threading.Thread]::CurrentThread.GetApartmentState() -ne [Threading.ApartmentState]::STA) {
    Write-Host 'WebView2 unavailable (PowerShell is not in STA mode); opening the browser app window instead.'
    exit 3
}

try {
    Add-Type -Namespace NsHost -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ReleaseCapture();
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern System.IntPtr SendMessage(System.IntPtr h, int msg, System.IntPtr w, System.IntPtr l);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool IsIconic(System.IntPtr h);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool IsWindow(System.IntPtr h);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int cmd);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool BringWindowToTop(System.IntPtr h);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
'@
}
catch {
    Write-Host "WebView2 unavailable (could not load the window helpers: $($_.Exception.Message)); opening the browser app window instead."
    exit 3
}

# ---------------------------------------------------------------------------------------------------------
# Single instance: a second start raises the running window and exits 0.
# ---------------------------------------------------------------------------------------------------------
$runFile = Join-Path (Get-HostHome) ("run\apphost-$Role.json")

function Test-ProcessAlive {
    param([int]$ProcessId)
    try {
        $null = Get-Process -Id $ProcessId -ErrorAction Stop
        return $true
    }
    catch {
        return $false
    }
}

function Get-RunningHost {
    # {hwnd, pid} of a live host window, or $null (missing file, unreadable file, dead PID, dead window).
    try {
        if (-not (Test-Path -LiteralPath $runFile)) { return $null }
        $j = Get-Content -Raw -LiteralPath $runFile | ConvertFrom-Json
        $names = $j.PSObject.Properties.Name
        if (-not (($names -contains 'hwnd') -and ($names -contains 'pid'))) { return $null }
        if (-not (Test-ProcessAlive -ProcessId ([int]$j.pid))) { return $null }
        $h = [IntPtr][int64]$j.hwnd
        if (-not [NsHost.Native]::IsWindow($h)) { return $null }
        return [pscustomobject]@{ hwnd = $h; pid = [int]$j.pid }
    }
    catch {
        return $null
    }
}

function Show-RunningHost {
    # The first host may still be starting (mutex taken, window not yet recorded): wait a few seconds for it.
    $deadline = (Get-Date).AddSeconds(5)
    do {
        $r = Get-RunningHost
        if ($null -ne $r) {
            if ([NsHost.Native]::IsIconic($r.hwnd)) { [void][NsHost.Native]::ShowWindow($r.hwnd, 9) }   # SW_RESTORE
            [void][NsHost.Native]::BringWindowToTop($r.hwnd)
            [void][NsHost.Native]::SetForegroundWindow($r.hwnd)
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    return $false
}

$createdNew = $false
$script:instanceMutex = New-Object System.Threading.Mutex($true, $MutexName, [ref]$createdNew)
if (-not $createdNew) {
    $script:instanceMutex.Dispose()
    if (Show-RunningHost) {
        Write-Host 'Nightshift is already open; brought its window to the front.'
    }
    else {
        Write-Host 'Nightshift is already starting in another window.'
    }
    exit 0
}

function Remove-RunFile {
    try {
        if (Test-Path -LiteralPath $runFile) { Remove-Item -LiteralPath $runFile -Force }
    }
    catch {
        Write-Verbose 'the apphost state file could not be removed'
    }
}

function Write-RunFile {
    # ready = WebView2 initialised and the page is loading: the Dashboard launcher waits for it, then ends.
    param([IntPtr]$Handle, [switch]$Ready)
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $runFile) | Out-Null
        $obj = [ordered]@{ hwnd = $Handle.ToInt64(); pid = $PID; ready = [bool]$Ready }
        [IO.File]::WriteAllText($runFile, ($obj | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
    }
    catch {
        Write-Verbose 'the apphost state file could not be written'
    }
}

$script:exitCode = 0
try {
    try {
        Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Xaml, System.Drawing
        $env:PATH = "$dir;$env:PATH"                          # WebView2Loader.dll is found next to the Core assembly
        Add-Type -Path (Join-Path $dir 'Microsoft.Web.WebView2.Core.dll')
        Add-Type -Path (Join-Path $dir 'Microsoft.Web.WebView2.Wpf.dll')
    }
    catch {
        Write-Host "WebView2 unavailable (could not load the assemblies: $($_.Exception.Message)); opening the browser app window instead."
        $script:exitCode = 3
        throw [System.OperationCanceledException]::new('assemblies')
    }

    # -----------------------------------------------------------------------------------------------------
    # Remembered window geometry: %NIGHTSHIFT_HOME%\config\window.json  {left, top, width, height, maximized}
    # -----------------------------------------------------------------------------------------------------
    $stateFile = Join-Path (Get-HostHome) 'config\window.json'

    function Read-WindowState {
        try {
            if (-not (Test-Path -LiteralPath $stateFile)) { return $null }
            $j = Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json
            foreach ($k in @('left', 'top', 'width', 'height')) {
                if (-not ($j.PSObject.Properties.Name -contains $k)) { return $null }
            }
            return $j
        }
        catch {
            return $null
        }
    }

    function Save-WindowState {
        try {
            $b = $script:win.RestoreBounds                    # the normal-state rectangle, also while maximized
            $obj = [ordered]@{
                left      = [math]::Round($b.Left)
                top       = [math]::Round($b.Top)
                width     = [math]::Round($b.Width)
                height    = [math]::Round($b.Height)
                maximized = ($script:win.WindowState -eq [System.Windows.WindowState]::Maximized)
            }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $stateFile) | Out-Null
            [IO.File]::WriteAllText($stateFile, ($obj | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
        }
        catch {
            Write-Verbose 'window.json could not be written'
        }
    }

    function Set-MaxBounds {
        # Frameless WPF windows cover the taskbar when maximized; cap them to the work area.
        $a = [System.Windows.SystemParameters]::WorkArea
        $script:win.MaxWidth = $a.Width + 12
        $script:win.MaxHeight = $a.Height + 12
    }

    function Get-StateName {
        return ("$($script:win.WindowState)".ToLowerInvariant())
    }

    function Send-Web {
        param([hashtable]$Msg)
        if ($script:ready) {
            try { $script:wv.CoreWebView2.PostWebMessageAsJson(($Msg | ConvertTo-Json -Compress)) }
            catch { Write-Verbose 'web message could not be posted' }
        }
    }

    function Send-WindowState {
        Send-Web @{
            event     = 'window-state'
            state     = (Get-StateName)
            maximized = ($script:win.WindowState -eq [System.Windows.WindowState]::Maximized)
        }
    }

    function Open-External {
        param([string]$Target)
        $uri = $null
        if ([Uri]::TryCreate($Target, [UriKind]::Absolute, [ref]$uri) -and $uri.Scheme -in @('http', 'https')) {
            try { Start-Process -FilePath $uri.AbsoluteUri } catch { Write-Verbose 'external link could not be opened' }
        }
    }

    # -----------------------------------------------------------------------------------------------------
    # Window
    # -----------------------------------------------------------------------------------------------------
    $brush = (New-Object System.Windows.Media.BrushConverter).ConvertFrom($BackgroundHex)   # dark canvas: no white flash
    $script:win = New-Object System.Windows.Window
    $script:win.Title = $Title
    $script:win.MinWidth = $MinWidth
    $script:win.MinHeight = $MinHeight
    $script:win.WindowStyle = [System.Windows.WindowStyle]::None
    $script:win.AllowsTransparency = $false
    $script:win.ResizeMode = [System.Windows.ResizeMode]::CanResize
    $script:win.Background = $brush

    # Window and taskbar icon: the brand icon shipped with the dashboard build; no icon when the file is missing.
    $iconFile = Join-Path $AppPath 'ns_dashboard\static\brand\nightshift.ico'
    if (Test-Path -LiteralPath $iconFile) {
        try {
            $dec = [System.Windows.Media.Imaging.BitmapDecoder]::Create(
                (New-Object System.Uri $iconFile),
                [System.Windows.Media.Imaging.BitmapCreateOptions]::None,
                [System.Windows.Media.Imaging.BitmapCacheOption]::OnLoad)
            $script:win.Icon = @($dec.Frames | Sort-Object { $_.PixelWidth } -Descending)[0]   # the largest size in the .ico
        }
        catch {
            Write-Verbose 'window icon could not be loaded'
        }
    }

    $chrome = New-Object System.Windows.Shell.WindowChrome
    $chrome.CaptionHeight = 0
    $chrome.ResizeBorderThickness = New-Object System.Windows.Thickness 6
    $chrome.GlassFrameThickness = New-Object System.Windows.Thickness 0
    $chrome.UseAeroCaptionButtons = $false
    [System.Windows.Shell.WindowChrome]::SetWindowChrome($script:win, $chrome)

    # Size and place: the saved geometry when it still lands on a screen, else centred at the default size.
    $wa = [System.Windows.SystemParameters]::WorkArea
    $saved = Read-WindowState
    $placed = $false
    if ($null -ne $saved) {
        $sw = [math]::Max($MinWidth, [double]$saved.width)
        $sh = [math]::Max($MinHeight, [double]$saved.height)
        $sl = [double]$saved.left
        $st = [double]$saved.top
        $vl = [System.Windows.SystemParameters]::VirtualScreenLeft
        $vt = [System.Windows.SystemParameters]::VirtualScreenTop
        $vw = [System.Windows.SystemParameters]::VirtualScreenWidth
        $vh = [System.Windows.SystemParameters]::VirtualScreenHeight
        $onScreen = ($sl + 80 -lt $vl + $vw) -and ($sl + $sw - 80 -gt $vl) -and ($st -lt $vt + $vh - 40) -and ($st -gt $vt - 20)
        if ($onScreen) {
            $script:win.WindowStartupLocation = [System.Windows.WindowStartupLocation]::Manual
            $script:win.Left = $sl
            $script:win.Top = $st
            $script:win.Width = $sw
            $script:win.Height = $sh
            $placed = $true
            if ($saved.PSObject.Properties.Name -contains 'maximized' -and [bool]$saved.maximized) {
                Set-MaxBounds
                $script:win.WindowState = [System.Windows.WindowState]::Maximized
            }
        }
    }
    if (-not $placed) {
        $script:win.Width = $Width
        $script:win.Height = $Height
        $script:win.WindowStartupLocation = [System.Windows.WindowStartupLocation]::CenterScreen
        if ($Width -gt $wa.Width -or $Height -gt $wa.Height) {
            Set-MaxBounds
            $script:win.WindowState = [System.Windows.WindowState]::Maximized
        }
    }

    $script:wv = New-Object Microsoft.Web.WebView2.Wpf.WebView2
    $props = New-Object Microsoft.Web.WebView2.Wpf.CoreWebView2CreationProperties
    $props.UserDataFolder = Join-Path (Get-HostHome) 'state\webview2'
    $script:wv.CreationProperties = $props
    $script:wv.DefaultBackgroundColor = [System.Drawing.ColorTranslator]::FromHtml($BackgroundHex)
    $script:win.Content = $script:wv

    $script:ready = $false
    $script:confirmedClose = $false
    $script:uiAcked = $false
    $script:closeTimer = $null

    $script:win.add_SourceInitialized({
        # The HWND exists now: publish it so a second start can raise this window.
        Write-RunFile -Handle (New-Object System.Windows.Interop.WindowInteropHelper $script:win).Handle
    })

    $script:wv.add_CoreWebView2InitializationCompleted({
        param($sender, $e)
        if (-not $e.IsSuccess) {
            Write-Host "WebView2 unavailable (initialisation failed: $($e.InitializationException.Message)); opening the browser app window instead."
            $script:exitCode = 3
            $script:confirmedClose = $true
            $script:win.Close()
            return
        }
        $script:ready = $true
        Write-RunFile -Handle (New-Object System.Windows.Interop.WindowInteropHelper $script:win).Handle -Ready
        $core = $script:wv.CoreWebView2
        $core.Settings.AreDefaultContextMenusEnabled = $false
        $core.Settings.AreDevToolsEnabled = ($env:NS_DEVTOOLS -eq '1')
        $core.Settings.IsStatusBarEnabled = $false
        $core.Settings.IsZoomControlEnabled = $false
        # Links that open a new window, or leave the dashboard origin, go to the default browser.
        $core.add_NewWindowRequested({
            param($s2, $a)
            $a.Handled = $true
            Open-External $a.Uri
        })
        $core.add_NavigationStarting({
            param($s2, $a)
            $target = $null
            if ([Uri]::TryCreate($a.Uri, [UriKind]::Absolute, [ref]$target) -and $target.Scheme -in @('http', 'https')) {
                if ($target.Authority -ne $startUri.Authority) {
                    $a.Cancel = $true
                    Open-External $a.Uri
                }
            }
        })
        $core.add_WebMessageReceived({
            param($s2, $a)
            try { $m = $a.WebMessageAsJson | ConvertFrom-Json } catch { return }
            if ($null -eq $m -or -not ($m.PSObject.Properties.Name -contains 'cmd')) { return }
            switch ([string]$m.cmd) {
                'minimize' { $script:win.WindowState = [System.Windows.WindowState]::Minimized }
                'toggle-maximize' {
                    if ($script:win.WindowState -eq [System.Windows.WindowState]::Maximized) {
                        $script:win.WindowState = [System.Windows.WindowState]::Normal
                    }
                    else {
                        Set-MaxBounds
                        $script:win.WindowState = [System.Windows.WindowState]::Maximized
                    }
                }
                'drag-start' {
                    if ($script:win.WindowState -ne [System.Windows.WindowState]::Maximized) {
                        # The page owns the mouse capture, so WPF DragMove would throw; hand the drag to Windows.
                        $h = (New-Object System.Windows.Interop.WindowInteropHelper $script:win).Handle
                        [void][NsHost.Native]::ReleaseCapture()
                        [void][NsHost.Native]::SendMessage($h, 0xA1, [IntPtr]2, [IntPtr]::Zero)   # WM_NCLBUTTONDOWN, HTCAPTION
                    }
                }
                'close-ack' { $script:uiAcked = $true }
                'close' {
                    $script:confirmedClose = $true
                    $script:win.Close()
                }
                'ready' { Send-WindowState }
                'open-external' {
                    if ($m.PSObject.Properties.Name -contains 'url') { Open-External ([string]$m.url) }
                }
            }
        })
    })

    $script:win.add_StateChanged({
        if ($script:win.WindowState -eq [System.Windows.WindowState]::Maximized) { Set-MaxBounds }
        Send-WindowState
    })

    $script:win.add_Closing({
        param($sender, $e)
        if ($script:confirmedClose -or -not $script:ready) {
            Save-WindowState
            return
        }
        # Ask the page first (Alt+F4, taskbar close). It answers {cmd: 'close'}, or {cmd: 'close-ack'} while it
        # shows its own dialog; a page that stays silent is closed after the grace period.
        $e.Cancel = $true
        $script:uiAcked = $false
        Send-Web @{ event = 'close-requested' }
        if ($null -ne $script:closeTimer) { $script:closeTimer.Stop() }
        $script:closeTimer = New-Object System.Windows.Threading.DispatcherTimer
        $script:closeTimer.Interval = [TimeSpan]::FromMilliseconds($CloseGraceMs)
        $script:closeTimer.add_Tick({
            $script:closeTimer.Stop()
            if (-not $script:uiAcked) {
                $script:confirmedClose = $true
                $script:win.Close()
            }
        })
        $script:closeTimer.Start()
    })

    $script:wv.Source = $startUri
    [void]$script:win.ShowDialog()
}
catch [System.OperationCanceledException] {
    Write-Verbose 'host not started'
}
finally {
    Remove-RunFile
    try { $script:instanceMutex.ReleaseMutex() } catch { Write-Verbose 'mutex was not held' }
    $script:instanceMutex.Dispose()
}
exit $script:exitCode
