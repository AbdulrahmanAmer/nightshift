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
    -Dev        a dev install (also INSTALL.cmd --dev, or NIGHTSHIFT_DEV_MODE=1; docs/DEV_MODE.md): the real
                installer with stub models and fake engines, for a PC with no NVIDIA card. The graphics card,
                driver and video memory rows become information rows and 10 GB of free disk is enough.
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
    [string]$HomePath = '',
    [switch]$Dev
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
# a dev install (docs/DEV_MODE.md) writes stub models, so it needs far less
$script:DevMinFreeGb = 10
$script:DevMinRamGb = 4
$script:DevBanner = 'DEV MODE: fake engines, no models'
$script:NetworkHosts = @(
    'https://huggingface.co', 'https://github.com', 'https://download.pytorch.org',
    'https://api.pexels.com', 'https://pixabay.com', 'https://pypi.org', 'https://files.pythonhosted.org',
    'https://api.github.com', 'https://api.nuget.org', 'https://download.visualstudio.microsoft.com'
)
# Setup cannot work without these (programs and Python packages come from them); any other site is a warning.
$script:CriticalHosts = @('github.com', 'pypi.org', 'files.pythonhosted.org')
$script:FallbackErrors = @{
    'NS-E101' = @('Unsupported Windows architecture (ARM64 or 32-bit).', 'Nightshift needs 64-bit Windows 10 or 11 on an x64 PC.')
    'NS-E102' = @('No NVIDIA GPU detected.', 'Nightshift needs an NVIDIA RTX GPU with 16 GB. Check that the GPU driver is installed (nvidia-smi must work).')
    'NS-E103' = @('NVIDIA driver too old (CUDA too low).', 'Install the latest Game Ready or Studio driver from nvidia.com/drivers, reboot, then re-run setup.')
    'NS-W115' = @('Could not read the NVIDIA driver version.', 'Setup continues with the CUDA 12.4 build. If a video later fails on the graphics card, install the latest driver from nvidia.com/drivers.')
    'NS-E104' = @('Not enough free disk space.', 'Free at least 60 GB on the drive that holds NIGHTSHIFT_HOME, or choose a bigger drive.')
    'NS-E105' = @('A required download site is unreachable.', 'Check the internet connection and any firewall or proxy.')
    'NS-W106' = @('NIGHTSHIFT_HOME is on a hard disk, not an SSD.', 'Model loading will be slow. Consider an NVMe/SSD drive.')
    'NS-W107' = @('Wake timers are not fully enabled in the power plan.', 'In Power Options > Change advanced power settings > Sleep > Allow wake timers, choose Enable (not Disable, and not Important wake timers only).')
    'NS-E110' = @('A required tool failed to install.', 'Install FFmpeg manually (winget install Gyan.FFmpeg) and re-run setup. Git is not needed.')
    'NS-E111' = @('winget (Windows App Installer) is missing.', 'Setup downloads FFmpeg and the Visual C++ runtime directly instead. To use winget, install App Installer from the Microsoft Store.')
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
    # Source 1: the banner nvidia-smi prints ("CUDA Version: 12.8").
    param([string]$Text)
    if ($Text -match 'CUDA\s+Version\s*:\s*(\d+)\.(\d+)') {
        return (New-Object System.Version -ArgumentList ([int]$Matches[1]), ([int]$Matches[2]))
    }
    return $null
}

# Source 2: the driver version mapped to the newest CUDA it supports. ONE table, newest first: the first row whose
# Min is at or below the driver's major number wins. Based on NVIDIA's "CUDA Toolkit and Compatible Driver Versions"
# table (CUDA Toolkit Release Notes, https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/) and the CUDA
# Compatibility guide (https://docs.nvidia.com/deploy/cuda-compatibility/): 580 = CUDA 13.0, 570 = 12.8,
# 560 = 12.6, 550 = 12.4, 545 = 12.3, and older branches below that.
$script:DriverCudaTable = @(
    [pscustomobject]@{ Min = 580; Cuda = '13.0' }
    [pscustomobject]@{ Min = 570; Cuda = '12.8' }
    [pscustomobject]@{ Min = 560; Cuda = '12.6' }
    [pscustomobject]@{ Min = 550; Cuda = '12.4' }
    [pscustomobject]@{ Min = 545; Cuda = '12.3' }
    [pscustomobject]@{ Min = 535; Cuda = '12.2' }
    [pscustomobject]@{ Min = 530; Cuda = '12.1' }
    [pscustomobject]@{ Min = 525; Cuda = '12.0' }
    [pscustomobject]@{ Min = 0; Cuda = '11.8' }
)

function Get-CudaFromDriver {
    param([string]$Driver)
    if ($Driver -notmatch '^\s*(\d{3})(\.\d+)?\s*$') { return $null }
    $major = [int]$Matches[1]
    foreach ($row in $script:DriverCudaTable) {
        if ($major -ge $row.Min) { return (New-Object System.Version -ArgumentList $row.Cuda) }
    }
    return $null
}

function Resolve-CudaVersion {
    # banner first, then the driver table; version is $null only when neither source gave an answer
    param($Probe)
    $v = Get-CudaVersion -Text ([string]$Probe.smi_text)
    if ($null -ne $v) { return [pscustomobject]@{ version = $v; source = 'nvidia-smi' } }
    $v = Get-CudaFromDriver -Driver ([string]$Probe.driver)
    if ($null -ne $v) { return [pscustomobject]@{ version = $v; source = 'driver' } }
    return [pscustomobject]@{ version = $null; source = 'none' }
}

function ConvertTo-NvidiaDriverVersion {
    # Windows reports an NVIDIA driver as 32.0.15.8157; NVIDIA's own number is the last five digits: 581.57.
    param([string]$WindowsVersion)
    $v = $WindowsVersion.Trim()
    if ($v -match '^\d{3}\.\d{2}$') { return $v }
    if ($v -match '^\d+\.\d+\.(\d+)\.(\d+)$') {
        $all = $Matches[1] + $Matches[2]
        if ($all.Length -ge 5) {
            $d = $all.Substring($all.Length - 5)
            return ($d.Substring(0, 3) + '.' + $d.Substring(3))
        }
    }
    return ''
}

function Test-BlackwellName {
    # RTX 50-series / RTX PRO (Blackwell); the same pattern as BLACKWELL_RE in installer/stage2/sysinfo.py.
    param([string]$GpuName)
    return [bool]($GpuName -match '(?i)(RTX|GeForce)\D{0,12}50[5-9]0|Blackwell|RTX\s+PRO')
}

function Get-LlamaPair {
    # The hand-over pair stage 2 downloads. RTX 50-series (Blackwell) cards can only run the CUDA 13 build,
    # so for them a CUDA 12.x driver yields no pair (the driver row fails with NS-E103 at the same time).
    param($Cuda, [string]$GpuName = '')
    if ($null -eq $Cuda) { return '' }
    if ($Cuda -ge (New-Object System.Version -ArgumentList 13, 0)) { return 'cuda13' }
    if (Test-BlackwellName $GpuName) { return '' }
    if ($Cuda -ge (New-Object System.Version -ArgumentList 12, 4)) { return 'cuda12' }
    return ''
}

function Test-PathInside {
    # True when $Path is $Folder or below it (Windows rules: case does not matter).
    param([string]$Path, [string]$Folder)
    if (-not $Path -or -not $Folder) { return $false }
    $p = $Path.Trim().TrimEnd('\').ToLowerInvariant()
    $f = $Folder.Trim().TrimEnd('\').ToLowerInvariant()
    return ($p -eq $f) -or $p.StartsWith($f + '\')
}

function Test-InsideExtracted {
    # True when $Path is the folder the zip was extracted to or lies in it. A drive root (the zip extracted
    # straight into D:\) holds every folder of the drive, so for a root only the root itself counts.
    param([string]$Path, [string]$AppFolder)
    if (-not $AppFolder) { return $false }
    if ($AppFolder.Trim() -match '^[A-Za-z]:\\?$') {
        return ($Path.Trim().TrimEnd('\') -ieq $AppFolder.Trim().TrimEnd('\'))
    }
    return (Test-PathInside -Path $Path -Folder $AppFolder)
}

function Select-DefaultHome {
    # $AppFolder is where the zip was extracted. The install folder must not be that folder or inside it:
    # setup copies the program into <home>\app. NTFS ignores case, so D:\NightShift (the extract folder
    # OPERATOR.md used to suggest) and D:\Nightshift (the default home) are the same folder.
    # Order: <best drive>\Nightshift, <best drive>\Nightshift-home, then the same two on the next drives;
    # '' when none is usable (the caller then uses a folder under the user's data directory).
    param($Drives, [string]$AppFolder = '')
    $all = @($Drives)
    if ($all.Count -eq 0) { return '' }
    $roomy = @($all | Where-Object { [double]$_.free_gb -ge $script:MinFreeGbRecommended })
    if ($roomy.Count -eq 0) { $roomy = $all }
    $ssd = @($roomy | Where-Object { $_.type -eq 'SSD' })
    if ($ssd.Count -gt 0) { $roomy = $ssd }
    $ranked = @($roomy | Sort-Object -Property @{ Expression = { [double]$_.free_gb }; Descending = $true })
    $ranked += @($all | Where-Object { $ranked -notcontains $_ } |
        Sort-Object -Property @{ Expression = { [double]$_.free_gb }; Descending = $true })
    foreach ($d in $ranked) {
        foreach ($name in @('Nightshift', 'Nightshift-home')) {
            $candidate = ('{0}:\{1}' -f $d.letter, $name)
            if (-not (Test-InsideExtracted -Path $candidate -AppFolder $AppFolder)) { return $candidate }
        }
    }
    return ''
}

# ---------------------------------------------------------------------------------------------------------
# Machine probes
# ---------------------------------------------------------------------------------------------------------
function Find-NvidiaSmi {
    # PATH, System32, the old NVSMI folder, then the driver store: DCH drivers keep nvidia-smi.exe only in
    # System32\DriverStore\FileRepository\nv*\ and it may be on neither PATH nor System32.
    param([string]$SystemRoot = $env:SystemRoot, [string]$ProgramFilesDir = $env:ProgramFiles, [switch]$SkipPath)
    if (-not $SkipPath) {
        $cmd = Get-Command 'nvidia-smi.exe' -ErrorAction SilentlyContinue
        if ($null -ne $cmd) { return $cmd.Source }
    }
    $candidates = @(
        (Join-Path $SystemRoot 'System32\nvidia-smi.exe'),
        (Join-Path $ProgramFilesDir 'NVIDIA Corporation\NVSMI\nvidia-smi.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    $store = Join-Path $SystemRoot 'System32\DriverStore\FileRepository'
    if (Test-Path -LiteralPath $store) {
        $hit = @(Get-ChildItem -Path (Join-Path $store 'nv*\nvidia-smi.exe') -ErrorAction SilentlyContinue |
                Sort-Object -Property LastWriteTime -Descending)
        if ($hit.Count -gt 0) { return $hit[0].FullName }
    }
    return $null
}

function Get-NvidiaAdapterMemoryMb {
    # The video memory size from the display class registry key (a 64-bit value; WMI's AdapterRAM caps at 4 GB).
    param([string]$Name)
    try {
        $class = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
        foreach ($key in @(Get-ChildItem -Path $class -ErrorAction SilentlyContinue)) {
            $p = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
            if ($null -eq $p -or $null -eq $p.PSObject.Properties['DriverDesc'] -or $p.DriverDesc -ne $Name) { continue }
            if ($null -eq $p.PSObject.Properties['HardwareInformation.qwMemorySize']) { continue }
            $raw = $p.'HardwareInformation.qwMemorySize'
            if ($raw -is [byte[]]) { $bytes = [BitConverter]::ToInt64($raw, 0) } else { $bytes = [int64]$raw }
            if ($bytes -gt 0) { return [int][math]::Round($bytes / 1MB) }
        }
    }
    catch { Write-Verbose "adapter memory lookup failed: $($_.Exception.Message)" }
    return 0
}

function Get-NvidiaAdapterInfo {
    # Used when nvidia-smi is missing or silent: the NVIDIA display adapter and driver as Windows reports them.
    try {
        $all = @(Get-CimInstance -ClassName Win32_VideoController |
                Where-Object { ([string]$_.Name) -match 'NVIDIA' -or ([string]$_.AdapterCompatibility) -match 'NVIDIA' })
        if ($all.Count -eq 0) { return $null }
        $pick = @($all | Where-Object { ([string]$_.Name) -match 'RTX|GeForce|Quadro|Tesla' })
        if ($pick.Count -eq 0) { $pick = $all }
        $a = $pick[0]
        return [pscustomobject]@{
            name    = ([string]$a.Name).Trim()
            driver  = (ConvertTo-NvidiaDriverVersion -WindowsVersion ([string]$a.DriverVersion))
            vram_mb = (Get-NvidiaAdapterMemoryMb -Name ([string]$a.Name))
        }
    }
    catch {
        Write-Verbose "display adapter lookup failed: $($_.Exception.Message)"
        return $null
    }
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

function Test-VcRuntime {
    # llama-server.exe and the voice programs need the Visual C++ 2015-2022 runtime (x64). It puts these three
    # files in System32; a clean Windows 11 does not have them until some program installs the redistributable.
    $dir = [Environment]::GetFolderPath('System')
    foreach ($name in @('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')) {
        if (-not (Test-Path -LiteralPath (Join-Path $dir $name))) { return $false }
    }
    return $true
}

function ConvertTo-WakeSetting {
    # "Allow wake timers" has three values: 0 = Disable, 1 = Enable, 2 = Important wake timers only.
    # Old fake probes used true/false; keep reading them (true = 1, false = 0). $null = could not be read.
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [bool]) { if ($Value) { return 1 } else { return 0 } }
    return [int]$Value
}

function Get-WakeTimersSetting {
    # The AC value of the power plan in use (the 03:00 task is for a PC on the wall). Returns 0, 1, 2 or $null.
    try {
        $out = & powercfg.exe /q SCHEME_CURRENT SUB_SLEEP BD3B718A-0680-4D9D-8AB2-E1D2B4AC806D | Out-String
        if ($out -match 'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)') {
            return [Convert]::ToInt32($Matches[1], 16)
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
        # two separate tries: a failure of the banner must not hide the driver version, nor the other way round
        try {
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
        catch { Write-Verbose "nvidia-smi query failed: $($_.Exception.Message)" }
        try { $smiText = (& $smi | Out-String) }
        catch { Write-Verbose "nvidia-smi failed: $($_.Exception.Message)" }
    }
    if (-not $gpuName -or -not $driver) {
        $adapter = Get-NvidiaAdapterInfo
        if ($null -ne $adapter) {
            if (-not $gpuName) {
                $gpuName = $adapter.name
                if ($vram -le 0) { $vram = $adapter.vram_mb }
            }
            if (-not $driver) { $driver = $adapter.driver }
        }
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
            ffmpeg = (Test-Tool 'ffmpeg.exe')
            wt     = (Test-Tool 'wt.exe')
            uv     = (Test-Tool 'uv.exe')
            vcruntime = (Test-VcRuntime)
        }
        vc_runtime   = (Test-VcRuntime)
        long_paths   = (Get-LongPathsEnabled)
        wake_timers  = (Get-WakeTimersSetting)
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
            ffmpeg = [bool](Get-Prop $tools 'ffmpeg' $true)
            wt     = [bool](Get-Prop $tools 'wt' $true)
            uv     = [bool](Get-Prop $tools 'uv' $true)
            vcruntime = [bool](Get-Prop $j 'vc_runtime' $true)
        }
        vc_runtime   = [bool](Get-Prop $j 'vc_runtime' $true)
        long_paths   = [bool](Get-Prop $j 'long_paths' $true)
        wake_timers  = ConvertTo-WakeSetting (Get-Prop $j 'wake_timers' 1)
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
    # $DevMode (docs/DEV_MODE.md): the graphics card, driver and video memory are information rows, the memory
    # row asks for 4 GB and the disk row for 10 GB; everything else is checked as usual.
    param($Probe, [string]$NsHome, [bool]$AllowCpu, [bool]$DevMode = $false)
    $rows = New-Object System.Collections.Generic.List[object]

    # os
    if ($Probe.os_arch -eq 'AMD64') {
        $rows.Add((New-Row 'os' 'Windows 10/11, 64-bit' 'ok' '' "$($Probe.os_caption) $($Probe.os_version)"))
    }
    else {
        $rows.Add((New-Row 'os' 'Windows 10/11, 64-bit' 'fail' 'NS-E101' "architecture $($Probe.os_arch)"))
    }

    # gpu + driver
    $cudaInfo = Resolve-CudaVersion -Probe $Probe
    $cuda = $cudaInfo.version
    $gpuMissing = -not $Probe.gpu_name
    $gpuFail = 'fail'
    if ($AllowCpu) { $gpuFail = 'warn' }
    if ($DevMode) {
        $gpuNote = 'no NVIDIA graphics card'
        if (-not $gpuMissing) { $gpuNote = [string]$Probe.gpu_name }
        $rows.Add((New-Row 'gpu' 'NVIDIA RTX GPU, 16 GB' 'ok' '' "$gpuNote (dev mode, not needed)"))
    }
    elseif ($gpuMissing) {
        $rows.Add((New-Row 'gpu' 'NVIDIA RTX GPU, 16 GB' $gpuFail 'NS-E102' 'nvidia-smi found no GPU'))
    }
    elseif ($Probe.vram_mb -le 0) {
        $rows.Add((New-Row 'gpu' 'NVIDIA RTX GPU, 16 GB' 'warn' '' "$($Probe.gpu_name), video memory size not read"))
    }
    elseif ($Probe.vram_mb -lt $script:MinVramMb) {
        $rows.Add((New-Row 'gpu' 'NVIDIA RTX GPU, 16 GB' 'warn' '' "$($Probe.gpu_name), only $($Probe.vram_mb) MB of video memory"))
    }
    else {
        $rows.Add((New-Row 'gpu' 'NVIDIA RTX GPU, 16 GB' 'ok' '' "$($Probe.gpu_name), $($Probe.vram_mb) MB"))
    }

    # The label, the code and the state are decided separately: a FAIL (NS-E103) needs a version that was actually
    # read and is below 12.4; "not reported" is a WARN (NS-W115) and setup goes on with the CUDA 12.4 build; a
    # driver between 12.4 and 12.8 only matters to RTX 50-series (Blackwell) cards and warns without a code.
    $driverLabel = 'NVIDIA driver (CUDA 12.4 or newer; 13.0+ for RTX 50-series)'
    $driverPart = ''
    if ($Probe.driver) { $driverPart = ", driver $($Probe.driver)" }
    $cudaText = ''
    if ($null -ne $cuda -and $cudaInfo.source -eq 'driver') { $cudaText = "CUDA $cuda (from driver $($Probe.driver))" }
    elseif ($null -ne $cuda) { $cudaText = "CUDA $cuda$driverPart" }
    $blackwell = ([string]$Probe.gpu_name) -match '(?i)(RTX|GeForce)\D{0,12}50[5-9]0|Blackwell|RTX\s+PRO'
    # The only llama.cpp builds in manifest.json are CUDA 12.4 and CUDA 13.x. The 12.4 build has no code for
    # RTX 50-series (Blackwell) cards, so on those a driver below CUDA 13 (driver 580) must stop setup here,
    # before the 20 GB download, not at the smoke test afterwards.
    $min = New-Object System.Version -ArgumentList 12, 4
    $minBlackwell = New-Object System.Version -ArgumentList 13, 0
    if ($DevMode) {
        $rows.Add((New-Row 'driver' $driverLabel 'ok' '' 'not checked (dev mode, not needed)'))
    }
    elseif ($gpuMissing -and $AllowCpu) {
        $rows.Add((New-Row 'driver' $driverLabel 'warn' '' 'skipped (cpu-dev profile)'))
    }
    elseif ($gpuMissing) {
        $rows.Add((New-Row 'driver' $driverLabel 'warn' '' 'not checked (no NVIDIA graphics card found)'))
    }
    elseif ($null -eq $cuda) {
        $rows.Add((New-Row 'driver' $driverLabel 'warn' 'NS-W115' 'driver version not reported; the CUDA 12.4 build will be used'))
    }
    elseif ($cuda -lt $min) {
        $rows.Add((New-Row 'driver' $driverLabel 'fail' 'NS-E103' $cudaText))
    }
    elseif ($cuda -lt $minBlackwell -and $blackwell) {
        $rows.Add((New-Row 'driver' $driverLabel 'fail' 'NS-E103' "$cudaText; RTX 50-series cards need a driver with CUDA 13 (driver 580 or newer)"))
    }
    else {
        $rows.Add((New-Row 'driver' $driverLabel 'ok' '' $cudaText))
    }

    # ram
    $ramNote = 'Balanced and Best models available'
    if ($Probe.ram_gb -lt 31) { $ramNote = 'Small model tier only' }
    if ($DevMode) {
        if ($Probe.ram_gb -ge $script:DevMinRamGb) {
            $rows.Add((New-Row 'ram' 'Memory, 4 GB for dev mode' 'ok' '' "$($Probe.ram_gb) GB (dev mode needs $($script:DevMinRamGb) GB)"))
        }
        else {
            $rows.Add((New-Row 'ram' 'Memory, 4 GB for dev mode' 'warn' '' "$($Probe.ram_gb) GB is below the $($script:DevMinRamGb) GB dev mode needs"))
        }
    }
    elseif ($Probe.ram_gb -ge $script:MinRamGb) {
        $rows.Add((New-Row 'ram' 'Memory, 16 GB minimum' 'ok' '' "$($Probe.ram_gb) GB ($ramNote)"))
    }
    else {
        $rows.Add((New-Row 'ram' 'Memory, 16 GB minimum' 'warn' '' "$($Probe.ram_gb) GB is below the 16 GB minimum"))
    }

    # disk on the home drive
    $letter = ''
    if ($NsHome.Length -ge 2 -and $NsHome[1] -eq ':') { $letter = $NsHome.Substring(0, 1).ToUpper() }
    $drive = @(@($Probe.drives) | Where-Object { ([string]$_.letter).ToUpper() -eq $letter }) | Select-Object -First 1
    $diskLabel = 'Free disk space, 60 GB'
    $diskHard = $script:MinFreeGbHard
    $diskRecommended = $script:MinFreeGbRecommended
    if ($DevMode) {
        $diskLabel = 'Free disk space, 10 GB for dev mode'
        $diskHard = $script:DevMinFreeGb
        $diskRecommended = $script:DevMinFreeGb
    }
    if ($null -eq $drive) {
        $rows.Add((New-Row 'disk' $diskLabel 'fail' 'NS-E104' "drive of $NsHome not found"))
        $rows.Add((New-Row 'disktype' 'Disk type, SSD' 'ok' '' 'unknown'))
    }
    else {
        $free = [double]$drive.free_gb
        $dtype = [string](Get-Prop $drive 'type' 'Unknown')
        if ($free -lt $diskHard) {
            $rows.Add((New-Row 'disk' $diskLabel 'fail' 'NS-E104' "$free GB free on ${letter}:"))
        }
        elseif ($free -lt $diskRecommended) {
            $rows.Add((New-Row 'disk' $diskLabel 'warn' '' "$free GB free on ${letter}: (60 GB recommended)"))
        }
        else {
            $rows.Add((New-Row 'disk' $diskLabel 'ok' '' "$free GB free on ${letter}:"))
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

    # winget: setup tries it first for FFmpeg and the Visual C++ runtime, then downloads a pinned copy itself,
    # so a PC without a working winget is only a warning (Git is not installed at all)
    $needWinget = (-not $Probe.tools.ffmpeg) -or (-not $Probe.vc_runtime)
    if ($Probe.winget) {
        $rows.Add((New-Row 'winget' 'winget (App Installer)' 'ok' '' 'available'))
    }
    elseif (-not $needWinget) {
        $rows.Add((New-Row 'winget' 'winget (App Installer)' 'ok' '' 'not needed, tools already installed'))
    }
    else {
        $rows.Add((New-Row 'winget' 'winget (App Installer)' 'warn' '' 'not available; FFmpeg and the Visual C++ runtime are downloaded directly'))
    }

    # Visual C++ runtime (llama.cpp and the voice programs do not start without it)
    if ($Probe.vc_runtime) {
        $rows.Add((New-Row 'vcruntime' 'Visual C++ runtime' 'ok' '' 'installed'))
    }
    else {
        $rows.Add((New-Row 'vcruntime' 'Visual C++ runtime' 'warn' '' 'not installed; stage 1 installs it now (Windows asks for permission: press Yes)'))
    }

    # long paths
    if ($Probe.long_paths) {
        $rows.Add((New-Row 'longpaths' 'Windows long paths enabled' 'ok' '' 'LongPathsEnabled = 1'))
    }
    else {
        $rows.Add((New-Row 'longpaths' 'Windows long paths enabled' 'warn' 'NS-W112' 'LongPathsEnabled is not 1'))
    }

    # wake timers: 0 = Disable, 1 = Enable, 2 = Important wake timers only. The 03:00 task is a plain
    # Task Scheduler wake timer, not an "important" one, so 2 does not wake the PC either.
    $wake = ConvertTo-WakeSetting $Probe.wake_timers
    if ($null -eq $wake) {
        $rows.Add((New-Row 'waketimers' 'Wake timers allowed (nightly run)' 'ok' '' 'could not be read'))
    }
    elseif ($wake -eq 1) {
        $rows.Add((New-Row 'waketimers' 'Wake timers allowed (nightly run)' 'ok' '' 'enabled'))
    }
    elseif ($wake -eq 2) {
        $rows.Add((New-Row 'waketimers' 'Wake timers allowed (nightly run)' 'warn' 'NS-W107' 'set to "Important wake timers only"; change Allow wake timers to Enable'))
    }
    else {
        $rows.Add((New-Row 'waketimers' 'Wake timers allowed (nightly run)' 'warn' 'NS-W107' 'disabled in the power plan; change Allow wake timers to Enable'))
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
    param($Drives, $AnswersObj, [string]$Forced, [string]$AppFolder = '')
    if ($Forced) { return $Forced }
    $fromAnswers = [string](Get-Prop $AnswersObj 'home_path' '')
    if ($fromAnswers) { return $fromAnswers }
    $existing = [Environment]::GetEnvironmentVariable('NIGHTSHIFT_HOME', 'User')
    if ($existing) { return $existing }
    if ($env:NIGHTSHIFT_HOME) { return $env:NIGHTSHIFT_HOME }
    $default = Select-DefaultHome -Drives $Drives -AppFolder $AppFolder
    if (-not $default) { $default = Join-Path (Get-DataDir) 'Home' }
    return $default
}

function Find-Uv {
    # The pinned uv (manifest.json bins.uv) is preferred: setup relies on flags (`uv venv --clear`,
    # `--require-hashes` with a direct URL, UV_CACHE_DIR) that an older uv on PATH does not have. A copy
    # from an earlier setup is used only when it is that version; a uv on PATH likewise. Without a match
    # the caller downloads the pin. Without -Version (no pin in the manifest) any uv is taken, PATH first.
    param([string]$NsHome, [string]$Version = '')
    $candidates = @((Join-Str (Get-DataDir) 'bootstrap\uv\uv.exe'), (Join-Str $NsHome 'bin\uv\uv.exe'))
    $c = Get-Command 'uv.exe' -ErrorAction SilentlyContinue
    if ($null -ne $c) {
        if ($Version) { $candidates += $c.Source } else { $candidates = @($c.Source) + $candidates }
    }
    foreach ($p in $candidates) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        if ($Version -and (Get-UvVersion -Uv $p) -ne $Version) { continue }
        return $p
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
        $downloadError = $_.Exception.Message
        Write-Verbose "uv direct download failed: $downloadError; trying winget"
        $c = $null
        $haveWinget = Test-Tool 'winget.exe'
        if ($haveWinget) {
            try {
                $null = & winget.exe install --id astral-sh.uv -e --silent --accept-package-agreements --accept-source-agreements
            }
            catch {
                Write-Verbose "winget failed: $($_.Exception.Message)"
            }
            Update-SessionPath
            $c = Get-Command 'uv.exe' -ErrorAction SilentlyContinue
        }
        if ($null -eq $c) {
            Set-Status -Index 1 -Tag 'fail' -Text 'could not be downloaded or installed'
            if ($haveWinget) {
                Stop-Setup -Code 'NS-E110' -Detail "uv could not be installed (direct download: $downloadError; winget failed too)." -ExitCode 13
            }
            Stop-Setup -Code 'NS-E105' -Detail "uv could not be downloaded ($downloadError) and winget is not available." -ExitCode 13
        }
        return $c.Source
    }
}

function Install-VcRuntime {
    # The runtime is installed here, with this console in front, because Windows shows the permission prompt of
    # a hidden background process only as a flashing taskbar button that nobody finds. Returns $true when the
    # runtime is there afterwards. A refusal or a failure is not fatal: the setup window tries again in its
    # "Visual C++ runtime" row and says how to install it by hand.
    param($Manifest)
    $pin = Get-Prop (Get-Prop $Manifest 'bins' $null) 'vcredist' $null
    if ($null -eq $pin -or -not $pin.url -or -not $pin.sha256) {
        Write-Host 'The Visual C++ runtime is missing and manifest.json pins no download for it; the setup window will try.'
        return $false
    }
    if ($script:IsDry) {
        Show-Action "install the Visual C++ runtime: download $($pin.url) (sha256 $($pin.sha256)), run it with -Verb RunAs: /install /quiet /norestart"
        return $false
    }
    $exe = Join-Path ([IO.Path]::GetTempPath()) 'nightshift-vc_redist.x64.exe'
    Write-Host ''
    Write-Host 'The Microsoft Visual C++ runtime is not installed on this PC. Nightshift needs it (about 25 MB).'
    try {
        Invoke-WebRequest -Uri $pin.url -OutFile $exe -UseBasicParsing
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLower()
        if ($actual -ne ([string]$pin.sha256).ToLower()) { throw "SHA256 mismatch for the download ($actual)" }
    }
    catch {
        Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
        Write-Host ('Could not download the Visual C++ runtime: {0}' -f $_.Exception.Message) -ForegroundColor Yellow
        Write-Host '    Fix: install it from https://aka.ms/vs/17/release/vc_redist.x64.exe, or let the setup window try again.'
        return $false
    }
    Write-Host 'Windows will ask for permission to install the Microsoft Visual C++ runtime; press Yes.' -ForegroundColor Cyan
    $code = 1223
    try {
        $p = Start-Process -FilePath $exe -ArgumentList '/install', '/quiet', '/norestart' -Verb RunAs -Wait -PassThru -ErrorAction Stop
        $code = [int]$p.ExitCode
    }
    catch { Write-Verbose "elevated start failed: $($_.Exception.Message)" }
    Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
    if (@(0, 1638, 1641, 3010) -contains $code -and (Test-VcRuntime)) {
        Write-Host 'The Microsoft Visual C++ runtime is installed.' -ForegroundColor Green
        return $true
    }
    if ($code -eq 1223) {
        Write-Host 'The permission window was closed or declined, so the runtime was not installed.' -ForegroundColor Yellow
    }
    else {
        Write-Host ('The Visual C++ runtime installer stopped with exit code {0}.' -f $code) -ForegroundColor Yellow
    }
    Write-Host '    Fix: install it from https://aka.ms/vs/17/release/vc_redist.x64.exe (press Yes when Windows asks), or let the setup window try again.'
    return $false
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

function Test-StorePythonAlias {
    # %LOCALAPPDATA%\Microsoft\WindowsApps holds the Microsoft Store "python.exe" aliases and the Store
    # package's own interpreter: writes to %LOCALAPPDATA% are virtualized there, so the setup server's
    # files may never appear where stage 1 looks. Never use a Python from that folder.
    param([string]$Path)
    return ($Path -match '(?i)[\\/]WindowsApps[\\/]')
}

function Find-PythonExe {
    # Only a uv-managed Python (UV_PYTHON_PREFERENCE=only-managed is set before this runs), never one from the
    # Microsoft Store folder, so the server and every venv run on the interpreter setup installed itself.
    param([string]$Uv, [string]$Version)
    try {
        $found = (& $Uv python find $Version 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $found) {
            $first = $found.Split("`n")[0].Trim()
            if ((Test-Path -LiteralPath $first) -and -not (Test-StorePythonAlias -Path $first)) { return $first }
        }
    }
    catch { Write-Verbose "uv python find failed: $($_.Exception.Message)" }
    return $null
}

function Unblock-ExtractedFiles {
    # A zip downloaded with a browser and extracted with Explorer carries the Mark of the Web on every file.
    # .NET Framework refuses to load a marked assembly (Add-Type -Path fails with 0x80131515), so the
    # WebView2 window helpers did not load and the first setup window opened in Edge. Best effort: a file
    # that cannot be unblocked changes nothing for the rest.
    param([string]$Folder)
    $roots = @($Folder)
    if ($Folder -match '^[A-Za-z]:\\$') {
        # extracted straight into a drive root: never walk the whole drive, only the bundle's own folders
        $roots = @('bin', 'launchers', 'installer', 'ns_common', 'ns_dashboard', 'ns_studio', 'ns_supervisor', 'ns_tts', 'tools') |
            ForEach-Object { Join-Path $Folder $_ }
    }
    $kinds = @('.dll', '.exe', '.ps1', '.cmd', '.pyd')
    foreach ($root in $roots) {
        try {
            if (-not (Test-Path -LiteralPath $root)) { continue }
            Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue |
                Where-Object { $kinds -contains $_.Extension.ToLower() } |
                Unblock-File -ErrorAction SilentlyContinue
        }
        catch { Write-Verbose "Unblock-File failed under ${root}: $($_.Exception.Message)" }
    }
}

function Read-ServerInfo {
    $f = Join-Path (Get-DataDir) 'setup-server.json'
    if (-not (Test-Path -LiteralPath $f)) { return $null }
    try { return (Get-Content -Raw -LiteralPath $f | ConvertFrom-Json) }
    catch { return $null }
}

function Invoke-ServerPing {
    # Windows PowerShell 5.1 silently drops a Cookie given through -Headers (a "restricted" header), which made
    # every first install stop with "the setup program did not start" (v0.1.1). The cookie goes through a
    # WebRequestSession instead, and the token is also sent as X-NS-Token, which the server accepts as well.
    param($Info, [int]$TimeoutSec = 2)
    try {
        $uri = 'http://127.0.0.1:{0}/api/ping' -f $Info.port
        $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
        $cookie = New-Object System.Net.Cookie('ns_setup', [string]$Info.token, '/', '127.0.0.1')
        $session.Cookies.Add($cookie)
        $headers = @{ 'X-NS-Token' = [string]$Info.token }
        $r = Invoke-RestMethod -Uri $uri -WebSession $session -Headers $headers -TimeoutSec $TimeoutSec -UseBasicParsing
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

function Get-ServerArgs {
    # Arguments of the setup server as plain strings. `& $py @args` quotes the ones with spaces itself; literal
    # quotes added here made Windows PowerShell 5.1 re-quote them, which broke paths like C:\Users\John Doe\...
    param([string]$App, [string]$PreflightFile, [string]$AnswersPath = '', [bool]$Headless = $false, [bool]$DevMode = $false)
    $list = @('-X', 'utf8', '-m', 'installer.stage2.web', '--app-path', $App, '--preflight', $PreflightFile)
    if ($AnswersPath) { $list += @('--answers', $AnswersPath) }
    if ($Headless) { $list += '--headless' }
    if ($DevMode) { $list += '--dev' }
    return [string[]]$list
}

function ConvertTo-ArgumentString {
    # Start-Process -ArgumentList joins an array with spaces and quotes nothing, so quote here
    # (CommandLineToArgvW rules: backslashes before a quote are doubled, so are trailing ones).
    param([string[]]$Arguments)
    $parts = foreach ($a in $Arguments) {
        if ($a -eq '' -or $a -match '[\s"]') {
            '"' + (($a -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
        }
        else { $a }
    }
    return ($parts -join ' ')
}

function Get-HandOver {
    param($Probe, $Rows, [string]$App, [string]$NsHome, [string]$ProfileName, [string]$LlamaPair, [string]$CudaString, [string]$CudaSource = '', [bool]$DevMode = $false)
    return [ordered]@{
        schema      = 1
        created     = (Get-Date).ToString('s')
        app_path    = $App
        home        = $NsHome
        profile     = $ProfileName
        llama_pair  = $LlamaPair
        cuda        = $CudaString
        cuda_source = $CudaSource
        dev_mode    = $DevMode
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

$App = (Resolve-Path -LiteralPath $AppPath).Path
if ($App -notmatch '^[A-Za-z]:\\$') { $App = $App.TrimEnd('\') }     # keep the backslash of a drive root (D:\)
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

# Remove the Mark of the Web from the extracted program files (best effort), so the WebView2 window helpers load
if ($script:IsDry) { Show-Action "unblock the extracted program files in $App (remove the Mark of the Web)" }
else { Unblock-ExtractedFiles -Folder $App }

# answers.json (unattended mode)
$answersPath = ''
if ($Answers) { $answersPath = $Answers }
elseif (Test-Path -LiteralPath (Join-Path $App 'answers.json')) { $answersPath = Join-Path $App 'answers.json' }
$answersObj = $null
if ($answersPath) { $answersObj = Read-AnswersFile -Path $answersPath }
$allowCpu = [bool](Get-Prop $answersObj 'allow_cpu' $false)

# a dev install: -Dev, INSTALL.cmd --dev, or NIGHTSHIFT_DEV_MODE=1 (docs/DEV_MODE.md)
$script:DevMode = ([bool]$Dev) -or ([string]$env:NIGHTSHIFT_DEV_MODE -match '^(1|true|yes)$')
if ($script:DevMode) { $env:NIGHTSHIFT_DEV_MODE = '1' }

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
    if ($script:DevMode) { Write-Host $script:DevBanner -ForegroundColor Yellow }
    if ($script:IsDry) { Write-Host 'Dry run: nothing will be changed on this PC.' }
    if ($answersPath) { Write-Host "Using answers from $answersPath (unattended mode)." }
}

# probe and the default install folder
if ($FakeProbe) { $probe = Get-FakeProbe -Path $FakeProbe } else { $probe = Get-RealProbe }
$NsHome = Resolve-NsHome -Drives @($probe.drives) -AnswersObj $answersObj -Forced $HomePath -AppFolder $App
if ($script:IsDry -and -not $ProbeOnly) { Show-Action "use NIGHTSHIFT_HOME = $NsHome" }

$rows = Get-PreflightRows -Probe $probe -NsHome $NsHome -AllowCpu $allowCpu -DevMode $script:DevMode
$cudaInfo = Resolve-CudaVersion -Probe $probe
$cuda = $cudaInfo.version
$llamaPair = Get-LlamaPair -Cuda $cuda -GpuName ([string]$probe.gpu_name)
$profileName = 'gpu'
if ((-not $probe.gpu_name) -or $script:DevMode) { $profileName = 'cpu-dev' }
if ($script:DevMode) { $llamaPair = '' }
# a GPU whose driver version could not be read gets the CUDA 12.4 build (NS-W115), not a refusal
# (an RTX 50-series card only runs the CUDA 13 build: it gets that pair, as in s02 choose_pair, and the
# setup window lists those files)
if ($null -eq $cuda -and $profileName -eq 'gpu') {
    if (Test-BlackwellName ([string]$probe.gpu_name)) { $llamaPair = 'cuda13' } else { $llamaPair = 'cuda12' }
}
$cudaString = ''
if ($null -ne $cuda) { $cudaString = [string]$cuda }
$handOver = Get-HandOver -Probe $probe -Rows $rows -App $App -NsHome $NsHome -ProfileName $profileName -LlamaPair $llamaPair -CudaString $cudaString -CudaSource $cudaInfo.source -DevMode $script:DevMode

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
    if ($script:DevMode) {
        Set-Status -Index 0 -Tag 'ok' -Text ("{0}, {1}, {2}" -f $probe.os_caption, $ramText, $script:DevBanner)
    }
    elseif ($probe.gpu_name) {
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
$uvPin = Get-Prop (Get-Prop $Manifest 'bins' $null) 'uv' $null
$uv = Find-Uv -NsHome $NsHome -Version ([string](Get-Prop $uvPin 'version' ''))
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

# Hand the uv path to the setup server: its window copies uv into <home>\bin\uv and ns_common.uv reads this.
if ($script:IsDry) { Show-Action "set NIGHTSHIFT_BOOTSTRAP_UV=$uv for the setup server" }
else { $env:NIGHTSHIFT_BOOTSTRAP_UV = $uv }

# Python
$pyVersion = [string]$Manifest.python_version
$env:UV_LINK_MODE = 'copy'
$env:UV_PYTHON_PREFERENCE = 'only-managed'      # the uv-managed Python 3.12, never one found on this PC
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

# Visual C++ runtime (stage 1 so the permission prompt appears in front of this console)
if (-not [bool]$probe.vc_runtime) {
    if ($showSplash -and -not $script:IsDry) { Write-Host '' }
    if (Install-VcRuntime -Manifest $Manifest) {
        $probe.vc_runtime = $true
        $probe.tools.vcruntime = $true
        $rows = Get-PreflightRows -Probe $probe -NsHome $NsHome -AllowCpu $allowCpu -DevMode $script:DevMode
        $handOver = Get-HandOver -Probe $probe -Rows $rows -App $App -NsHome $NsHome -ProfileName $profileName -LlamaPair $llamaPair -CudaString $cudaString -CudaSource $cudaInfo.source -DevMode $script:DevMode
    }
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
$serverArgs = Get-ServerArgs -App $App -PreflightFile $preflightFile -AnswersPath $answersPath -Headless ([bool]$script:Headless) -DevMode ([bool]$script:DevMode)
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
$proc = Start-Process -FilePath $py -ArgumentList (ConvertTo-ArgumentString $serverArgs) -WorkingDirectory $App -NoNewWindow -PassThru -RedirectStandardError $errFile
$null = $proc.Handle

$info = $null
# The first start of a freshly installed Python can take a while (Defender scans it, slow disks): wait up to 60 s
# and say so after 15 s, so a slow start does not look like a hang.
$serverStart = Get-Date
$deadline = $serverStart.AddSeconds(60)
$saidWaiting = $false
while ((Get-Date) -lt $deadline -and -not $proc.HasExited) {
    $info = Read-ServerInfo
    if ($null -ne $info -and [int]$info.pid -eq $proc.Id -and (Invoke-ServerPing -Info $info)) { break }
    $info = $null
    if (-not $saidWaiting -and ((Get-Date) - $serverStart).TotalSeconds -ge 15) {
        $saidWaiting = $true
        Set-Status -Index 3 -Tag 'working' -Text 'starting the setup program, still waiting (the first start can take a minute)'
    }
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
