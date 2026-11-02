<#
.SYNOPSIS
    Nightshift installer, stage 1 (component C1): the splash.

.DESCRIPTION
    Probes the PC (spec 1.2 table), gets uv and Python 3.12, starts the setup server
    (python -m installer.stage2.web) and waits on it. The server opens the setup window and does the rest:
    the free tools, the install folder, the setup environment and the wizard (docs/design/installer-ui.md).

    -DryRun     prints every action as "[dry-run] <action>", changes nothing and exits 0.
    -Answers    path of an answers.json (default: next to INSTALL.cmd). With one, setup runs headless.
    -Ui         open the setup window even though an answers.json exists (it prefills the choices).
    -NoUi       headless, even without an answers.json (same as NS_NONINTERACTIVE=1).
    -ProbeOnly  with -Out <file> [-HomePath <dir>]: write the hand-over JSON and exit 0, nothing else.
    -FakeProbe  (CI only, requires -DryRun or -ProbeOnly) JSON file that replaces the machine probes.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AppPath,
    [switch]$DryRun,
    [string]$Answers = '',
    [string]$FakeProbe = '',
    [switch]$Ui,
    [switch]$NoUi,
    [switch]$ProbeOnly,
    [string]$Out = '',
    [string]$HomePath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------------------------------------
# Constants (spec 1.2, 5, 21.1)
# ---------------------------------------------------------------------------------------------------------
$script:IsDry = [bool]$DryRun
$script:Unattended = $false
$script:ErrorsJson = $null
$script:MinFreeGbHard = 40
$script:MinFreeGbRecommended = 60
$script:MinRamGb = 15
$script:MinVramMb = 15000
$script:NetworkHosts = @(
    'https://huggingface.co', 'https://github.com', 'https://download.pytorch.org',
    'https://api.pexels.com', 'https://pixabay.com', 'https://pypi.org', 'https://files.pythonhosted.org'
)
# Setup cannot work without these (programs and Python packages come from them); any other site is a warning.
$script:CriticalHosts = @('github.com', 'pypi.org', 'files.pythonhosted.org')
$script:FallbackErrors = @{
    'NS-E101' = @('Unsupported Windows architecture (ARM64 or 32-bit).', 'Nightshift needs 64-bit Windows 10 or 11 on an x64 PC.')
    'NS-E102' = @('No NVIDIA GPU detected.', 'Nightshift needs an NVIDIA RTX GPU with 16 GB. Check that the GPU driver is installed (nvidia-smi must work).')
    'NS-E103' = @('NVIDIA driver too old (CUDA too low).', 'Install the latest Game Ready or Studio driver from nvidia.com/drivers, reboot, then re-run setup.')
    'NS-E104' = @('Not enough free disk space.', 'Free at least 60 GB on the drive that holds NIGHTSHIFT_HOME, or choose a bigger drive.')
    'NS-E105' = @('A required download site is unreachable.', 'Check the internet connection and any firewall or proxy.')
    'NS-W106' = @('NIGHTSHIFT_HOME is on a hard disk, not an SSD.', 'Model loading will be slow. Consider an NVMe/SSD drive.')
    'NS-W107' = @('Wake timers are disabled in the power plan.', 'Enable Allow wake timers in Power Options > Advanced > Sleep.')
    'NS-E110' = @('A required tool failed to install via winget.', 'Install it manually (Git, Windows Terminal, FFmpeg) and re-run setup.')
    'NS-E111' = @('winget (Windows App Installer) is missing.', 'Install App Installer from the Microsoft Store (or use the direct links shown), then re-run setup.')
    'NS-W112' = @('Windows long paths are disabled.', 'Run setup once as administrator to enable them, or keep NIGHTSHIFT_HOME on a short path such as D:\Nightshift.')
}
$script:SplashLabels = @('This PC', 'uv', 'Python', 'Setup window')
$script:SplashTop = -1
$script:CanMove = $false

function Show-Action {
    param([Parameter(Mandatory = $true)][string]$Text)
    Write-Host "[dry-run] $Text"
}

function Get-Prop {
    param($Obj, [string]$Name, $Default)
    if ($null -ne $Obj -and $null -ne $Obj.PSObject.Properties[$Name]) {
        return $Obj.PSObject.Properties[$Name].Value
    }
    return $Default
}

function Get-ErrorInfo {
    param([string]$Code)
    $msg = $Code
    $hint = ''
    if ($null -ne $script:ErrorsJson -and $null -ne $script:ErrorsJson.PSObject.Properties[$Code]) {
        $entry = $script:ErrorsJson.PSObject.Properties[$Code].Value
        $msg = [string]$entry.message
        $hint = [string]$entry.hint
    }
    elseif ($script:FallbackErrors.ContainsKey($Code)) {
        $msg = $script:FallbackErrors[$Code][0]
        $hint = $script:FallbackErrors[$Code][1]
    }
    return [pscustomobject]@{ Code = $Code; Message = $msg; Hint = $hint }
}

function Stop-Setup {
    param([string]$Code, [string]$Detail, [int]$ExitCode)
    $info = Get-ErrorInfo -Code $Code
    Write-Host ''
    Write-Host ("{0}  {1}" -f $Code, $info.Message) -ForegroundColor Red
    if ($Detail) { Write-Host "    $Detail" }
    if ($info.Hint) { Write-Host "    Fix: $($info.Hint)" }
    exit $ExitCode
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Join-Str {
    param([string]$Base, [string]$Leaf)
    return ($Base.TrimEnd('\') + '\' + $Leaf)
}

function Test-Tool {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-DataDir {
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'AppData\Local' }
    return (Join-Path $base 'Nightshift')
}

# ---------------------------------------------------------------------------------------------------------
# Parsing helpers (pure functions, covered by the fake-probe tests)
# ---------------------------------------------------------------------------------------------------------
function Get-CudaVersion {
    param([string]$Text)
    if ($Text -match 'CUDA Version:\s*(\d+)\.(\d+)') {
        return (New-Object System.Version -ArgumentList ([int]$Matches[1]), ([int]$Matches[2]))
    }
    return $null
}

function Get-LlamaPair {
    param($Cuda)
    if ($null -eq $Cuda) { return '' }
    if ($Cuda -ge (New-Object System.Version -ArgumentList 13, 0)) { return 'cuda13' }
    if ($Cuda -ge (New-Object System.Version -ArgumentList 12, 4)) { return 'cuda12' }
    return ''
}

function Select-DefaultHome {
    param($Drives)
    $all = @($Drives)
    if ($all.Count -eq 0) { return '' }
    $roomy = @($all | Where-Object { [double]$_.free_gb -ge $script:MinFreeGbRecommended })
    if ($roomy.Count -eq 0) { $roomy = $all }
    $ssd = @($roomy | Where-Object { $_.type -eq 'SSD' })
    if ($ssd.Count -gt 0) { $roomy = $ssd }
    $best = $roomy | Sort-Object -Property @{ Expression = { [double]$_.free_gb }; Descending = $true } | Select-Object -First 1
    return ('{0}:\Nightshift' -f $best.letter)
}

# ---------------------------------------------------------------------------------------------------------
# Machine probes
# ---------------------------------------------------------------------------------------------------------
function Find-NvidiaSmi {
    $cmd = Get-Command 'nvidia-smi.exe' -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'),
        (Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    return $null
}

function Get-DriveMediaType {
    param([string]$Letter)
    try {
        $disk = Get-Partition -DriveLetter $Letter -ErrorAction Stop | Get-Disk -ErrorAction Stop | Select-Object -First 1
        $pd = Get-PhysicalDisk -ErrorAction Stop | Where-Object { [string]$_.DeviceId -eq [string]$disk.Number } | Select-Object -First 1
        if ($null -ne $pd) {
            if ([string]$pd.BusType -eq 'NVMe') { return 'SSD' }
            $mt = [string]$pd.MediaType
            if ($mt -eq 'SSD' -or $mt -eq 'HDD') { return $mt }
        }
    }
    catch {
        Write-Verbose "disk type unknown for ${Letter}: $($_.Exception.Message)"
    }
    return 'Unknown'
}

function Get-DriveInfo {
    $list = New-Object System.Collections.Generic.List[object]
    $disks = @(Get-CimInstance -ClassName Win32_LogicalDisk -Filter 'DriveType=3')
    foreach ($d in $disks) {
        $letter = ([string]$d.DeviceID).Substring(0, 1)
        $list.Add([pscustomobject]@{
                letter  = $letter
                free_gb = [math]::Round(([double]$d.FreeSpace) / 1GB, 1)
                type    = (Get-DriveMediaType -Letter $letter)
            })
    }
    return $list.ToArray()
}

function Test-HostReachable {
    param([string]$Url)
    try {
        $null = Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing -TimeoutSec 10
        return $true
    }
    catch {
        $resp = $null
        if ($null -ne $_.Exception.PSObject.Properties['Response']) { $resp = $_.Exception.Response }
        return ($null -ne $resp)   # any HTTP answer means the site is reachable
    }
}

function Test-WingetWorks {
    if (-not (Test-Tool 'winget.exe')) { return $false }
    try {
        $null = & winget.exe --version
        return ($LASTEXITCODE -eq 0)
    }
    catch { return $false }
}

function Get-LongPathsEnabled {
    try {
        $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
        $v = (Get-ItemProperty -Path $key -Name 'LongPathsEnabled' -ErrorAction Stop).LongPathsEnabled
        return ([int]$v -eq 1)
    }
    catch { return $false }
}

function Get-WakeTimersEnabled {
    try {
        $out = & powercfg.exe /q SCHEME_CURRENT SUB_SLEEP BD3B718A-0680-4D9D-8AB2-E1D2B4AC806D | Out-String
        if ($out -match 'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)') {
            return ([Convert]::ToInt32($Matches[1], 16) -ne 0)
        }
    }
    catch {
        Write-Verbose "powercfg failed: $($_.Exception.Message)"
    }
    return $null
}

function Get-RealProbe {
    $arch = $env:PROCESSOR_ARCHITEW6432
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    $caption = 'Windows'
    try { $caption = [string](Get-CimInstance -ClassName Win32_OperatingSystem).Caption } catch { Write-Verbose 'no OS caption' }

    $smi = Find-NvidiaSmi
    $smiText = ''
    $gpuName = ''
    $vram = 0
    $driver = ''
    if ($null -ne $smi) {
        try {
            $smiText = (& $smi | Out-String)
            $csv = @(& $smi '--query-gpu=name,memory.total,driver_version' '--format=csv,noheader,nounits')
            if ($csv.Count -gt 0) {
                $parts = ([string]$csv[0]).Split(',')
                if ($parts.Count -ge 3) {
                    $gpuName = $parts[0].Trim()
                    $vram = [int]([double]$parts[1].Trim())
                    $driver = $parts[2].Trim()
                }
            }
        }
        catch { Write-Verbose "nvidia-smi failed: $($_.Exception.Message)" }
    }

    $ram = [math]::Round(([double](Get-CimInstance -ClassName Win32_ComputerSystem).TotalPhysicalMemory) / 1GB, 1)
    $unreachable = New-Object System.Collections.Generic.List[string]
    foreach ($u in $script:NetworkHosts) {
        if (-not (Test-HostReachable -Url $u)) { $unreachable.Add(([uri]$u).Host) }
    }
    return [pscustomobject]@{
        os_arch      = $arch
        os_caption   = $caption
        os_version   = [string][Environment]::OSVersion.Version
        is_admin     = (Test-IsAdmin)
        nvidia_smi   = ($null -ne $smi)
        smi_text     = $smiText
        gpu_name     = $gpuName
        vram_mb      = $vram
        driver       = $driver
        ram_gb       = $ram
        drives       = (Get-DriveInfo)
        unreachable  = $unreachable.ToArray()
        winget       = (Test-WingetWorks)
        tools        = [pscustomobject]@{
            git    = (Test-Tool 'git.exe')
            ffmpeg = (Test-Tool 'ffmpeg.exe')
            wt     = (Test-Tool 'wt.exe')
            uv     = (Test-Tool 'uv.exe')
        }
        long_paths   = (Get-LongPathsEnabled)
        wake_timers  = (Get-WakeTimersEnabled)
    }
}

function Get-FakeProbe {
    param([string]$Path)
    $j = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    $tools = Get-Prop $j 'tools' $null
    return [pscustomobject]@{
        os_arch      = [string](Get-Prop $j 'os_arch' 'AMD64')
        os_caption   = [string](Get-Prop $j 'os_caption' 'Windows 11 Pro (fake)')
        os_version   = [string](Get-Prop $j 'os_version' '10.0.26200')
        is_admin     = [bool](Get-Prop $j 'is_admin' $false)
        nvidia_smi   = [bool](Get-Prop $j 'nvidia_smi' $true)
        smi_text     = [string](Get-Prop $j 'smi_text' 'NVIDIA-SMI 572.16   Driver Version: 572.16   CUDA Version: 12.8')
        gpu_name     = [string](Get-Prop $j 'gpu_name' 'NVIDIA GeForce RTX 5060 Ti')
        vram_mb      = [int](Get-Prop $j 'vram_mb' 16311)
        driver       = [string](Get-Prop $j 'driver' '572.16')
        ram_gb       = [double](Get-Prop $j 'ram_gb' 32)
        drives       = @(Get-Prop $j 'drives' @([pscustomobject]@{ letter = 'C'; free_gb = 500; type = 'SSD' }))
        unreachable  = @(Get-Prop $j 'unreachable' @())
        winget       = [bool](Get-Prop $j 'winget' $true)
        tools        = [pscustomobject]@{
            git    = [bool](Get-Prop $tools 'git' $true)
            ffmpeg = [bool](Get-Prop $tools 'ffmpeg' $true)
            wt     = [bool](Get-Prop $tools 'wt' $true)
            uv     = [bool](Get-Prop $tools 'uv' $true)
        }
        long_paths   = [bool](Get-Prop $j 'long_paths' $true)
        wake_timers  = Get-Prop $j 'wake_timers' $true
    }
}

# ---------------------------------------------------------------------------------------------------------
# Preflight rows (spec 1.2)
# ---------------------------------------------------------------------------------------------------------
function New-Row {
    param([string]$Id, [string]$Label, [string]$Status, [string]$Code, [string]$Value)
    $hint = ''
    if ($Status -ne 'ok' -and $Code) { $hint = (Get-ErrorInfo -Code $Code).Hint }
    return [pscustomobject]@{ id = $Id; label = $Label; status = $Status; code = $Code; value = $Value; hint = $hint }
}

function Get-PreflightRows {
    param($Probe, [string]$NsHome, [bool]$AllowCpu)
    $rows = New-Object System.Collections.Generic.List[object]

    # os
    if ($Probe.os_arch -eq 'AMD64') {
        $rows.Add((New-Row 'os' 'Windows 10/11, 64-bit' 'ok' '' "$($Probe.os_caption) $($Probe.os_version)"))
    }
    else {
        $rows.Add((New-Row 'os' 'Windows 10/11, 64-bit' 'fail' 'NS-E101' "architecture $($Probe.os_arch)"))
    }

    # gpu + driver
    $cuda = Get-CudaVersion -Text $Probe.smi_text
    $gpuMissing = (-not $Probe.nvidia_smi) -or (-not $Probe.gpu_name)
    $gpuFail = 'fail'
    if ($AllowCpu) { $gpuFail = 'warn' }
    if ($gpuMissing) {
        $rows.Add((New-Row 'gpu' 'NVIDIA RTX GPU, 16 GB' $gpuFail 'NS-E102' 'nvidia-smi found no GPU'))
    }
    elseif ($Probe.vram_mb -lt $script:MinVramMb) {
        $rows.Add((New-Row 'gpu' 'NVIDIA RTX GPU, 16 GB' 'warn' '' "$($Probe.gpu_name), only $($Probe.vram_mb) MB of video memory"))
    }
    else {
        $rows.Add((New-Row 'gpu' 'NVIDIA RTX GPU, 16 GB' 'ok' '' "$($Probe.gpu_name), $($Probe.vram_mb) MB"))
    }

    $cudaText = 'not reported'
    if ($null -ne $cuda) { $cudaText = "CUDA $cuda, driver $($Probe.driver)" }
    if ($gpuMissing -and $AllowCpu) {
        $rows.Add((New-Row 'driver' 'NVIDIA driver, CUDA 12.8 or newer' 'warn' '' 'skipped (cpu-dev profile)'))
    }
    elseif ($null -eq $cuda -or $cuda -lt (New-Object System.Version -ArgumentList 12, 4)) {
        $rows.Add((New-Row 'driver' 'NVIDIA driver, CUDA 12.8 or newer' 'fail' 'NS-E103' $cudaText))
    }
    elseif ($cuda -lt (New-Object System.Version -ArgumentList 12, 8)) {
        $rows.Add((New-Row 'driver' 'NVIDIA driver, CUDA 12.8 or newer' 'warn' 'NS-E103' "$cudaText (12.8 is needed for Blackwell GPUs)"))
    }
    else {
        $rows.Add((New-Row 'driver' 'NVIDIA driver, CUDA 12.8 or newer' 'ok' '' $cudaText))
    }

    # ram
    $ramNote = 'Balanced and Best models available'
    if ($Probe.ram_gb -lt 31) { $ramNote = 'Small model tier only' }
    if ($Probe.ram_gb -ge $script:MinRamGb) {
        $rows.Add((New-Row 'ram' 'Memory, 16 GB minimum' 'ok' '' "$($Probe.ram_gb) GB ($ramNote)"))
    }
    else {
        $rows.Add((New-Row 'ram' 'Memory, 16 GB minimum' 'warn' '' "$($Probe.ram_gb) GB is below the 16 GB minimum"))
    }

    # disk on the home drive
    $letter = ''
    if ($NsHome.Length -ge 2 -and $NsHome[1] -eq ':') { $letter = $NsHome.Substring(0, 1).ToUpper() }
    $drive = @(@($Probe.drives) | Where-Object { ([string]$_.letter).ToUpper() -eq $letter }) | Select-Object -First 1
    if ($null -eq $drive) {
        $rows.Add((New-Row 'disk' 'Free disk space, 60 GB' 'fail' 'NS-E104' "drive of $NsHome not found"))
        $rows.Add((New-Row 'disktype' 'Disk type, SSD' 'ok' '' 'unknown'))
    }
    else {
        $free = [double]$drive.free_gb
        $dtype = [string](Get-Prop $drive 'type' 'Unknown')
        if ($free -lt $script:MinFreeGbHard) {
            $rows.Add((New-Row 'disk' 'Free disk space, 60 GB' 'fail' 'NS-E104' "$free GB free on ${letter}:"))
        }
        elseif ($free -lt $script:MinFreeGbRecommended) {
            $rows.Add((New-Row 'disk' 'Free disk space, 60 GB' 'warn' '' "$free GB free on ${letter}: (60 GB recommended)"))
        }
        else {
            $rows.Add((New-Row 'disk' 'Free disk space, 60 GB' 'ok' '' "$free GB free on ${letter}:"))
        }
        if ($dtype -eq 'HDD') {
            $rows.Add((New-Row 'disktype' 'Disk type, SSD' 'warn' 'NS-W106' "${letter}: is a hard disk"))
        }
        else {
            $rows.Add((New-Row 'disktype' 'Disk type, SSD' 'ok' '' "${letter}: $dtype"))
        }
    }

    # network: GitHub, PyPI and the PyPI file host block setup; any other unreachable site is a warning
    $unreachable = @($Probe.unreachable)
    $blocked = @($unreachable | Where-Object { $script:CriticalHosts -contains $_ })
    if ($unreachable.Count -eq 0) {
        $rows.Add((New-Row 'network' 'Internet (Hugging Face, GitHub, PyPI, PyTorch, Pexels, Pixabay)' 'ok' '' 'all reachable'))
    }
    elseif ($blocked.Count -gt 0) {
        $rows.Add((New-Row 'network' 'Internet (Hugging Face, GitHub, PyPI, PyTorch, Pexels, Pixabay)' 'fail' 'NS-E105' ('unreachable: ' + ($unreachable -join ', '))))
    }
    else {
        $rows.Add((New-Row 'network' 'Internet (Hugging Face, GitHub, PyPI, PyTorch, Pexels, Pixabay)' 'warn' 'NS-E105' ('unreachable: ' + ($unreachable -join ', '))))
    }

    # winget (only needed when Git or FFmpeg must be installed)
    $needWinget = (-not $Probe.tools.git) -or (-not $Probe.tools.ffmpeg)
    if ($Probe.winget) {
        $rows.Add((New-Row 'winget' 'winget (App Installer)' 'ok' '' 'available'))
    }
    elseif (-not $needWinget) {
        $rows.Add((New-Row 'winget' 'winget (App Installer)' 'ok' '' 'not needed, tools already installed'))
    }
    else {
        $rows.Add((New-Row 'winget' 'winget (App Installer)' 'fail' 'NS-E111' 'winget --version failed'))
    }

    # long paths
    if ($Probe.long_paths) {
        $rows.Add((New-Row 'longpaths' 'Windows long paths enabled' 'ok' '' 'LongPathsEnabled = 1'))
    }
    else {
        $rows.Add((New-Row 'longpaths' 'Windows long paths enabled' 'warn' 'NS-W112' 'LongPathsEnabled is not 1'))
    }

    # wake timers
    if ($null -eq $Probe.wake_timers) {
        $rows.Add((New-Row 'waketimers' 'Wake timers allowed (nightly run)' 'ok' '' 'could not be read'))
    }
    elseif ($Probe.wake_timers) {
        $rows.Add((New-Row 'waketimers' 'Wake timers allowed (nightly run)' 'ok' '' 'enabled'))
    }
    else {
        $rows.Add((New-Row 'waketimers' 'Wake timers allowed (nightly run)' 'warn' 'NS-W107' 'disabled in the power plan'))
    }
    return $rows.ToArray()
}

function Show-Rows {
    param($Rows)
    Write-Host ''
    Write-Host 'Machine check' -ForegroundColor Cyan
    foreach ($r in $Rows) {
        $tag = '[ OK ]'
        $color = 'Green'
        if ($r.status -eq 'warn') { $tag = '[WARN]'; $color = 'Yellow' }
        if ($r.status -eq 'fail') { $tag = '[FAIL]'; $color = 'Red' }
        Write-Host ('{0} {1,-56} {2}' -f $tag, $r.label, $r.value) -ForegroundColor $color
        if ($r.status -ne 'ok' -and $r.code) {
            $info = Get-ErrorInfo -Code $r.code
            Write-Host ('       {0}: {1}' -f $r.code, $info.Message) -ForegroundColor $color
            if ($r.hint) { Write-Host ('       Fix: {0}' -f $r.hint) }
        }
    }
    Write-Host ''
}

# ---------------------------------------------------------------------------------------------------------
# The splash (docs/design/installer-ui.md 1.2). ASCII only: Windows PowerShell 5.1 consoles use the OEM code page.
# ---------------------------------------------------------------------------------------------------------
function Show-Splash {
    param([string]$Version)
    $art = @'

      _.._
    .' .-'`     N I G H T S H I F T
   /  /         setup __VERSION__
   |  |
   \  '.___.;   This window is the setup log. Keep it open until setup says it is done.
    '._  _.'
       ``

'@
    Write-Host ($art.Replace('__VERSION__', $Version)).TrimEnd()
    Write-Host ''
    $script:CanMove = $false
    try {
        if (-not [Console]::IsOutputRedirected) {
            $null = [Console]::CursorTop
            $script:CanMove = $true
        }
    }
    catch { $script:CanMove = $false }
    if ($script:CanMove) {
        for ($i = 0; $i -lt 4; $i++) { Write-Host ('  [    ] {0}' -f $script:SplashLabels[$i]) }
        Write-Host ''
        $script:SplashTop = [Console]::CursorTop - 5
    }
}

function Set-Status {
    # Index 0..3 is the status line; Tag is waiting, working, ok, warn or fail.
    param([int]$Index, [string]$Tag, [string]$Text)
    $tags = @{ waiting = '[    ]'; working = '[ .. ]'; ok = '[ ok ]'; warn = '[warn]'; fail = '[FAIL]' }
    $colors = @{ waiting = $null; working = $null; ok = 'Green'; warn = 'Yellow'; fail = 'Red' }
    $line = '  {0} {1,-13} {2}' -f $tags[$Tag], $script:SplashLabels[$Index], $Text
    $color = $colors[$Tag]
    if ($script:CanMove -and $script:SplashTop -ge 0) {
        try {
            $left = [Console]::CursorLeft
            $top = [Console]::CursorTop
            [Console]::SetCursorPosition(0, $script:SplashTop + $Index)
            $width = [Console]::BufferWidth - 1
            $padded = $line.PadRight($width)
            if ($padded.Length -gt $width) { $padded = $padded.Substring(0, $width) }
            if ($color) { Write-Host $padded -NoNewline -ForegroundColor $color } else { Write-Host $padded -NoNewline }
            [Console]::SetCursorPosition($left, $top)
            return
        }
        catch { $script:CanMove = $false }
    }
    if ($Tag -eq 'waiting') { return }
    if ($color) { Write-Host $line -ForegroundColor $color } else { Write-Host $line }
}

# ---------------------------------------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------------------------------------
function Read-AnswersFile {
    param([string]$Path)
    try {
        return (Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json)
    }
    catch {
        Write-Host "answers.json could not be read: $Path" -ForegroundColor Red
        Write-Host "    $($_.Exception.Message)"
        exit 11
    }
}

function Resolve-NsHome {
    # The default install folder; the setup window lets the partner change it. Never asks a question.
    param($Drives, $AnswersObj, [string]$Forced)
    if ($Forced) { return $Forced }
    $fromAnswers = [string](Get-Prop $AnswersObj 'home_path' '')
    if ($fromAnswers) { return $fromAnswers }
    $existing = [Environment]::GetEnvironmentVariable('NIGHTSHIFT_HOME', 'User')
    if ($existing) { return $existing }
    if ($env:NIGHTSHIFT_HOME) { return $env:NIGHTSHIFT_HOME }
    $default = Select-DefaultHome -Drives $Drives
    if (-not $default) { $default = Join-Path (Get-DataDir) 'Home' }
    return $default
}

function Find-Uv {
    param([string]$NsHome)
    $c = Get-Command 'uv.exe' -ErrorAction SilentlyContinue
    if ($null -ne $c) { return $c.Source }
    foreach ($p in @((Join-Str (Get-DataDir) 'bootstrap\uv\uv.exe'), (Join-Str $NsHome 'bin\uv\uv.exe'))) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

function Install-Uv {
    param($Manifest)
    $pin = $Manifest.bins.uv
    $dir = Join-Str (Get-DataDir) 'bootstrap\uv'
    if ($script:IsDry) {
        Show-Action "download $($pin.url) (sha256 $($pin.sha256)) and extract to $dir"
        return (Join-Str $dir 'uv.exe')
    }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $zip = Join-Path $env:TEMP 'nightshift-uv.zip'
    try {
        Invoke-WebRequest -Uri $pin.url -OutFile $zip -UseBasicParsing
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLower()
        if ($actual -ne ([string]$pin.sha256).ToLower()) { throw "SHA256 mismatch for the uv download ($actual)" }
        Expand-Archive -LiteralPath $zip -DestinationPath $dir -Force
        $found = Get-ChildItem -LiteralPath $dir -Recurse -Filter 'uv.exe' | Select-Object -First 1
        if ($null -eq $found) { throw 'uv.exe not found in the downloaded archive' }
        if ($found.DirectoryName -ne $dir) {
            Get-ChildItem -LiteralPath $found.DirectoryName -File | Move-Item -Destination $dir -Force
        }
        Remove-Item -LiteralPath $zip -Force
        return (Join-Str $dir 'uv.exe')
    }
    catch {
        Write-Verbose "uv direct download failed: $($_.Exception.Message); trying winget"
        $null = & winget.exe install --id astral-sh.uv -e --silent --accept-package-agreements --accept-source-agreements
        Update-SessionPath
        $c = Get-Command 'uv.exe' -ErrorAction SilentlyContinue
        if ($null -eq $c) {
            Set-Status -Index 1 -Tag 'fail' -Text 'could not be downloaded or installed'
            Stop-Setup -Code 'NS-E110' -Detail 'uv could not be installed (direct download and winget both failed).' -ExitCode 13
        }
        return $c.Source
    }
}

function Get-UvVersion {
    param([string]$Uv)
    try {
        $text = (& $Uv --version | Out-String).Trim()
        if ($text -match 'uv\s+(\S+)') { return $Matches[1] }
    }
    catch { Write-Verbose "uv --version failed: $($_.Exception.Message)" }
    return '?'
}

function Find-PythonExe {
    param([string]$Uv, [string]$Version)
    try {
        $found = (& $Uv python find $Version 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $found -and (Test-Path -LiteralPath $found.Split("`n")[0].Trim())) {
            return $found.Split("`n")[0].Trim()
        }
    }
    catch { Write-Verbose "uv python find failed: $($_.Exception.Message)" }
    return $null
}

function Read-ServerInfo {
    $f = Join-Path (Get-DataDir) 'setup-server.json'
    if (-not (Test-Path -LiteralPath $f)) { return $null }
    try { return (Get-Content -Raw -LiteralPath $f | ConvertFrom-Json) }
    catch { return $null }
}

function Invoke-ServerPing {
    param($Info, [int]$TimeoutSec = 2)
    try {
        $uri = 'http://127.0.0.1:{0}/api/ping' -f $Info.port
        $headers = @{ Cookie = ('ns_setup=' + $Info.token) }
        $r = Invoke-RestMethod -Uri $uri -Headers $headers -TimeoutSec $TimeoutSec -UseBasicParsing
        return [bool]$r.ok
    }
    catch { return $false }
}

function Open-RunningWindow {
    # A second INSTALL.cmd: when a setup server is alive, only bring its window back.
    $info = Read-ServerInfo
    if ($null -eq $info) { return $false }
    $alive = $null -ne (Get-Process -Id ([int]$info.pid) -ErrorAction SilentlyContinue)
    if ($alive -and (Invoke-ServerPing -Info $info)) {
        Write-Host 'Setup is already running. Opening its window.'
        try {
            $uri = 'http://127.0.0.1:{0}/api/internal/open-window' -f $info.port
            $null = Invoke-RestMethod -Uri $uri -Method Post -Headers @{ 'X-NS-Token' = [string]$info.token } -TimeoutSec 5 -UseBasicParsing
        }
        catch { Write-Verbose "open-window failed: $($_.Exception.Message)" }
        return $true
    }
    Remove-Item -LiteralPath (Join-Path (Get-DataDir) 'setup-server.json') -Force -ErrorAction SilentlyContinue
    return $false
}

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [int]$ExitCode, [string]$What)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host "$What failed (exit code $LASTEXITCODE)." -ForegroundColor Red
        exit $ExitCode
    }
}

function Get-HandOver {
    param($Probe, $Rows, [string]$App, [string]$NsHome, [string]$ProfileName, [string]$LlamaPair, [string]$CudaString)
    return [ordered]@{
        schema      = 1
        created     = (Get-Date).ToString('s')
        app_path    = $App
        home        = $NsHome
        profile     = $ProfileName
        llama_pair  = $LlamaPair
        cuda        = $CudaString
        ram_gb      = $Probe.ram_gb
        is_admin    = $Probe.is_admin
        os_caption  = $Probe.os_caption
        os_version  = $Probe.os_version
        gpu         = [ordered]@{ name = $Probe.gpu_name; vram_mb = $Probe.vram_mb; driver = $Probe.driver }
        drives      = @($Probe.drives)
        winget      = $Probe.winget
        tools       = $Probe.tools
        unreachable = @($Probe.unreachable)
        checks      = @($Rows)
    }
}

# =========================================================================================================
# Main
# =========================================================================================================
if ($FakeProbe -and -not $script:IsDry -and -not $ProbeOnly) {
    throw '-FakeProbe is a CI test hook and can only be used together with -DryRun.'
}
if ($ProbeOnly -and -not $Out) {
    throw '-ProbeOnly needs -Out <file>.'
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}
catch { Write-Verbose 'could not force TLS 1.2' }

$App = (Resolve-Path -LiteralPath $AppPath).Path.TrimEnd('\')
$manifestPath = Join-Path $App 'manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) {
    Write-Host "manifest.json not found in $App. Extract the whole Nightshift zip and run INSTALL.cmd from there." -ForegroundColor Red
    exit 11
}
$Manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$errorsPath = Join-Path $App 'contracts\errors.json'
if (Test-Path -LiteralPath $errorsPath) {
    try { $script:ErrorsJson = Get-Content -Raw -LiteralPath $errorsPath | ConvertFrom-Json }
    catch { Write-Verbose 'errors.json unreadable; using built-in messages' }
}
$versionPath = Join-Path $App 'VERSION'
$Version = '0.0.0'
if (Test-Path -LiteralPath $versionPath) { $Version = (Get-Content -Raw -LiteralPath $versionPath).Trim() }

# answers.json (unattended mode)
$answersPath = ''
if ($Answers) { $answersPath = $Answers }
elseif (Test-Path -LiteralPath (Join-Path $App 'answers.json')) { $answersPath = Join-Path $App 'answers.json' }
$answersObj = $null
if ($answersPath) { $answersObj = Read-AnswersFile -Path $answersPath }
$allowCpu = [bool](Get-Prop $answersObj 'allow_cpu' $false)

# headless: answers.json without -Ui, -NoUi, or NS_NONINTERACTIVE=1 (the unattended path CI and the operator use)
$script:Headless = (($answersPath -and -not $Ui) -or [bool]$NoUi -or ($env:NS_NONINTERACTIVE -eq '1'))
$showSplash = (-not $script:Headless) -and (-not $ProbeOnly)

# a second INSTALL.cmd while the first one is alive only reopens the window
if ($showSplash -and -not $script:IsDry) {
    if (Open-RunningWindow) { exit 0 }
}

if ($showSplash) {
    Show-Splash -Version $Version
    Set-Status -Index 0 -Tag 'working' -Text 'looking at this PC'
}
elseif (-not $ProbeOnly) {
    Write-Host ''
    Write-Host 'Nightshift setup - stage 1' -ForegroundColor Cyan
    if ($script:IsDry) { Write-Host 'Dry run: nothing will be changed on this PC.' }
    if ($answersPath) { Write-Host "Using answers from $answersPath (unattended mode)." }
}

# probe and the default install folder
if ($FakeProbe) { $probe = Get-FakeProbe -Path $FakeProbe } else { $probe = Get-RealProbe }
$NsHome = Resolve-NsHome -Drives @($probe.drives) -AnswersObj $answersObj -Forced $HomePath
if ($script:IsDry -and -not $ProbeOnly) { Show-Action "use NIGHTSHIFT_HOME = $NsHome" }

$rows = Get-PreflightRows -Probe $probe -NsHome $NsHome -AllowCpu $allowCpu
$cuda = Get-CudaVersion -Text $probe.smi_text
$llamaPair = Get-LlamaPair -Cuda $cuda
$profileName = 'gpu'
if ((-not $probe.nvidia_smi) -or (-not $probe.gpu_name)) { $profileName = 'cpu-dev' }
$cudaString = ''
if ($null -ne $cuda) { $cudaString = [string]$cuda }
$handOver = Get-HandOver -Probe $probe -Rows $rows -App $App -NsHome $NsHome -ProfileName $profileName -LlamaPair $llamaPair -CudaString $cudaString

if ($ProbeOnly) {
    $outDir = Split-Path -Parent $Out
    if ($outDir -and -not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
    [IO.File]::WriteAllText($Out, ($handOver | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
    exit 0
}

# the first status line, or the old console table when running headless
if ($showSplash) {
    $osRow = $rows | Where-Object { $_.id -eq 'os' } | Select-Object -First 1
    if ($osRow.status -eq 'fail') {
        Set-Status -Index 0 -Tag 'fail' -Text 'this PC cannot run Nightshift'
        Stop-Setup -Code 'NS-E101' -Detail 'This PC cannot run Nightshift. Tell the developer.' -ExitCode 10
    }
    $ramText = '{0:0.#} GB memory' -f ([double]$probe.ram_gb)
    if ($probe.gpu_name) {
        $gpuGb = [int][math]::Round(([double]$probe.vram_mb) / 1024)
        Set-Status -Index 0 -Tag 'ok' -Text ("{0}, {1} ({2} GB), {3}" -f $probe.os_caption, $probe.gpu_name, $gpuGb, $ramText)
    }
    else {
        Set-Status -Index 0 -Tag 'warn' -Text ("{0}, no NVIDIA graphics card found, {1}" -f $probe.os_caption, $ramText)
    }
}
else {
    Show-Rows -Rows $rows
    $fails = @($rows | Where-Object { $_.status -eq 'fail' })
    if ($fails.Count -gt 0) {
        if ($script:IsDry) {
            Write-Host ('Preflight would block setup: ' + (($fails | ForEach-Object { $_.code }) -join ', '))
        }
        else {
            Write-Host 'Setup cannot continue until the red items are fixed.' -ForegroundColor Red
            exit 10
        }
    }
}

# uv
if ($showSplash) { Set-Status -Index 1 -Tag 'working' -Text 'looking for it' }
$uv = Find-Uv -NsHome $NsHome
if ($null -ne $uv) {
    if ($script:IsDry) { Show-Action "uv already present: $uv" }
    elseif ($showSplash) { Set-Status -Index 1 -Tag 'ok' -Text ('{0}, already here' -f (Get-UvVersion -Uv $uv)) }
    else { Write-Host "  uv : already present, skipped ($uv)" }
}
else {
    if ($showSplash) { Set-Status -Index 1 -Tag 'working' -Text ('{0}, downloading' -f $Manifest.bins.uv.version) }
    $uv = Install-Uv -Manifest $Manifest
    if ($showSplash -and -not $script:IsDry) { Set-Status -Index 1 -Tag 'ok' -Text ('{0}, downloaded' -f (Get-UvVersion -Uv $uv)) }
}

# Python
$pyVersion = [string]$Manifest.python_version
$env:UV_LINK_MODE = 'copy'
if ($script:IsDry) {
    Show-Action "$uv python install $pyVersion"
    $py = Join-Str (Get-DataDir) 'python\python.exe'
}
else {
    if ($showSplash) { Set-Status -Index 2 -Tag 'working' -Text ('{0}, looking for it' -f $pyVersion) }
    $py = Find-PythonExe -Uv $uv -Version $pyVersion
    $pyNote = 'already here'
    if ($null -eq $py) {
        if ($showSplash) { Set-Status -Index 2 -Tag 'working' -Text ('{0}, downloading (about 30 MB)' -f $pyVersion) }
        $pyNote = 'downloaded'
        $null = & $uv python install $pyVersion
        if ($LASTEXITCODE -ne 0) {
            if ($showSplash) { Set-Status -Index 2 -Tag 'fail' -Text 'could not be installed' }
            Stop-Setup -Code 'NS-E105' -Detail "uv python install $pyVersion failed (exit code $LASTEXITCODE)." -ExitCode 13
        }
        $py = Find-PythonExe -Uv $uv -Version $pyVersion
        if ($null -eq $py) {
            if ($showSplash) { Set-Status -Index 2 -Tag 'fail' -Text 'could not be found after the install' }
            Stop-Setup -Code 'NS-E105' -Detail "uv python find $pyVersion found nothing after the install." -ExitCode 13
        }
    }
    if ($showSplash) { Set-Status -Index 2 -Tag 'ok' -Text ('{0}, {1}' -f $pyVersion, $pyNote) }
}

# hand-over file for the setup server (hardware summary)
$preflightFile = Join-Path ([IO.Path]::GetTempPath()) 'nightshift-preflight.json'
$handJson = $handOver | ConvertTo-Json -Depth 6
if ($script:IsDry) {
    Show-Action "write hardware summary to $preflightFile"
    Show-Action ('preflight-json: ' + ($handOver | ConvertTo-Json -Depth 6 -Compress))
}
else {
    [IO.File]::WriteAllText($preflightFile, $handJson, (New-Object System.Text.UTF8Encoding($false)))
}

# the setup server
$serverArgs = @('-X', 'utf8', '-m', 'installer.stage2.web', '--app-path', ('"{0}"' -f $App), '--preflight', ('"{0}"' -f $preflightFile))
if ($answersPath) { $serverArgs += @('--answers', ('"{0}"' -f $answersPath)) }
if ($script:Headless) { $serverArgs += '--headless' }
if ($script:IsDry) {
    Show-Action "start setup server: $py $($serverArgs -join ' ')"
    Write-Host 'Dry run finished. Nothing was changed.'
    exit 0
}

$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = $App
if ($script:Headless) {
    Push-Location $App
    try {
        & $py @serverArgs
        $serverExit = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    exit $serverExit
}

# window mode: start the server on this console, wait for it, show the address, then wait until it ends
$dataDir = Get-DataDir
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$errFile = Join-Path $dataDir 'setup-server.err'
Remove-Item -LiteralPath (Join-Path $dataDir 'setup-server.json') -Force -ErrorAction SilentlyContinue
Set-Status -Index 3 -Tag 'working' -Text 'starting the setup program'
$proc = Start-Process -FilePath $py -ArgumentList $serverArgs -WorkingDirectory $App -NoNewWindow -PassThru -RedirectStandardError $errFile
$null = $proc.Handle

$info = $null
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline -and -not $proc.HasExited) {
    $info = Read-ServerInfo
    if ($null -ne $info -and [int]$info.pid -eq $proc.Id -and (Invoke-ServerPing -Info $info)) { break }
    $info = $null
    Start-Sleep -Milliseconds 250
}
if ($null -eq $info) {
    Set-Status -Index 3 -Tag 'fail' -Text 'the setup program did not start'
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $errFile) {
        Write-Host ''
        Get-Content -LiteralPath $errFile -Tail 20 | ForEach-Object { Write-Host $_ }
    }
    exit 14
}

# the server opens the window; it records which launcher worked in setup-server.json
$hostText = $null
$deadline = (Get-Date).AddSeconds(25)
while ((Get-Date) -lt $deadline -and -not $proc.HasExited) {
    $info = Read-ServerInfo
    if ($null -ne $info -and $null -ne $info.PSObject.Properties['host_text']) { $hostText = [string]$info.host_text; break }
    Start-Sleep -Milliseconds 250
}
$tag = 'ok'
if (-not $hostText -or $info.host -eq 'none') { $tag = 'warn'; $hostText = 'open the address below in Edge or Chrome' }
Set-Status -Index 3 -Tag $tag -Text $hostText
$addressFile = Join-Path $dataDir 'setup-address.txt'
$address = ''
if (Test-Path -LiteralPath $addressFile) { $address = (Get-Content -LiteralPath $addressFile -TotalCount 1).Trim() }
Write-Host ''
Write-Host ('         Setup address: {0}' -f $address)
Write-Host '         If no window appeared, paste that address into Edge or Chrome.'
Write-Host ''

$proc.WaitForExit()
$code = $proc.ExitCode
if ($code -ne 0 -and $code -ne 130 -and (Test-Path -LiteralPath $errFile)) {
    $tail = @(Get-Content -LiteralPath $errFile -Tail 20)
    if ($tail.Count -gt 0) {
        Write-Host ''
        $tail | ForEach-Object { Write-Host $_ }
    }
}
exit $code
