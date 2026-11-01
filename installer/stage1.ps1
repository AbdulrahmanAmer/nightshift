<#
.SYNOPSIS
    Nightshift installer, stage 1 (component C1).

.DESCRIPTION
    Checks the PC (spec 1.2 table), installs the free tools (Git, Windows Terminal, FFmpeg, uv, Python 3.12),
    creates the NIGHTSHIFT_HOME folder tree and user environment variable, bootstraps venvs\studio and hands
    over to the Python wizard (installer.stage2.wizard).

    -DryRun   prints every action as "[dry-run] <action>", changes nothing and exits 0.
    -Answers  path of an answers.json (default: next to INSTALL.cmd, then <home>\config\answers.json).
    -FakeProbe  (CI only, requires -DryRun) JSON file that replaces the machine probes.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AppPath,
    [switch]$DryRun,
    [string]$Answers = '',
    [string]$FakeProbe = ''
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
    'https://api.pexels.com', 'https://pixabay.com'
)
$script:DriverUrl = 'https://www.nvidia.com/Download/index.aspx'
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

function Test-Tool {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
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

    # network
    $unreachable = @($Probe.unreachable)
    if ($unreachable.Count -eq 0) {
        $rows.Add((New-Row 'network' 'Internet (Hugging Face, GitHub, PyTorch, Pexels, Pixabay)' 'ok' '' 'all reachable'))
    }
    else {
        $rows.Add((New-Row 'network' 'Internet (Hugging Face, GitHub, PyTorch, Pexels, Pixabay)' 'fail' 'NS-E105' ('unreachable: ' + ($unreachable -join ', '))))
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
# Actions
# ---------------------------------------------------------------------------------------------------------
function Open-FixPage {
    param([string]$Code)
    if ($Code -eq 'NS-E103' -or $Code -eq 'NS-E102') {
        Write-Host "Opening the NVIDIA driver page: $script:DriverUrl"
        Start-Process $script:DriverUrl
    }
    elseif ($Code -eq 'NS-E111') {
        Write-Host 'Opening the Microsoft Store App Installer page.'
        Write-Host '  Direct links: Git https://git-scm.com/download/win'
        Write-Host '                FFmpeg https://www.gyan.dev/ffmpeg/builds/'
        Write-Host '                winget https://github.com/microsoft/winget-cli/releases/latest'
        Start-Process 'ms-windows-store://pdp/?productid=9NBLGGH4NNS1'
    }
}

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
    param($Drives, $AnswersObj)
    $fromAnswers = [string](Get-Prop $AnswersObj 'home_path' '')
    if ($fromAnswers) { return $fromAnswers }
    $existing = [Environment]::GetEnvironmentVariable('NIGHTSHIFT_HOME', 'User')
    if ($existing) { return $existing }
    if ($env:NIGHTSHIFT_HOME) { return $env:NIGHTSHIFT_HOME }
    $default = Select-DefaultHome -Drives $Drives
    if (-not $default) { $default = Join-Path $env:LOCALAPPDATA 'Nightshift' }
    if ($script:IsDry -or $script:Unattended) { return $default }
    Write-Host ''
    Write-Host 'Nightshift keeps its models, environments, settings and videos in one folder.'
    $reply = Read-Host "Install folder [$default]"
    if ($reply -and $reply.Trim()) { return $reply.Trim().Trim('"') }
    return $default
}

function Install-WingetTool {
    param([string]$Id, [string]$Cmd, [string]$Label, [bool]$Required)
    if (Test-Tool $Cmd) {
        Write-Host "  $Label : already present, skipped"
        return
    }
    $wingetArgs = @('install', '--id', $Id, '-e', '--silent', '--accept-package-agreements', '--accept-source-agreements')
    if ($script:IsDry) {
        Show-Action ("winget " + ($wingetArgs -join ' '))
        return
    }
    Write-Host "  $Label : installing with winget (this can take a few minutes) ..."
    & winget.exe @wingetArgs
    Update-SessionPath
    if (-not (Test-Tool $Cmd)) {
        if ($Required) {
            Stop-Setup -Code 'NS-E110' -Detail "$Label ($Id) is still not available after the winget install." -ExitCode 12
        }
        Write-Host "  $Label : could not be installed; continuing without it (optional)." -ForegroundColor Yellow
    }
}

function Find-Uv {
    param([string]$NsHome)
    $c = Get-Command 'uv.exe' -ErrorAction SilentlyContinue
    if ($null -ne $c) { return $c.Source }
    $p = Join-Path $NsHome 'bin\uv\uv.exe'
    if (Test-Path -LiteralPath $p) { return $p }
    return $null
}

function Install-Uv {
    param([string]$NsHome, $Manifest)
    $pin = $Manifest.bins.uv
    $dir = Join-Path $NsHome 'bin\uv'
    if ($script:IsDry) {
        Show-Action "download $($pin.url) (sha256 $($pin.sha256)) and extract to $dir"
        return (Join-Path $dir 'uv.exe')
    }
    Write-Host "  uv : downloading $($pin.version) ..."
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
        return (Join-Path $dir 'uv.exe')
    }
    catch {
        Write-Host "  uv : direct download failed ($($_.Exception.Message)); trying winget" -ForegroundColor Yellow
        & winget.exe install --id astral-sh.uv -e --silent --accept-package-agreements --accept-source-agreements | Out-Host
        Update-SessionPath
        $c = Get-Command 'uv.exe' -ErrorAction SilentlyContinue
        if ($null -eq $c) {
            Stop-Setup -Code 'NS-E110' -Detail 'uv could not be installed (direct download and winget both failed).' -ExitCode 13
        }
        return $c.Source
    }
}

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [int]$ExitCode, [string]$What)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host "$What failed (exit code $LASTEXITCODE)." -ForegroundColor Red
        exit $ExitCode
    }
}

# =========================================================================================================
# Main
# =========================================================================================================
if ($FakeProbe -and -not $script:IsDry) {
    throw '-FakeProbe is a CI test hook and can only be used together with -DryRun.'
}

Write-Host ''
Write-Host 'Nightshift setup - stage 1' -ForegroundColor Cyan
if ($script:IsDry) { Write-Host 'Dry run: nothing will be changed on this PC.' }

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

# answers.json (unattended mode)
$answersPath = ''
if ($Answers) { $answersPath = $Answers }
elseif (Test-Path -LiteralPath (Join-Path $App 'answers.json')) { $answersPath = Join-Path $App 'answers.json' }
$answersObj = $null
if ($answersPath) {
    $answersObj = Read-AnswersFile -Path $answersPath
    $script:Unattended = $true
    Write-Host "Using answers from $answersPath (unattended mode)."
}
if ($env:NS_NONINTERACTIVE -eq '1') { $script:Unattended = $true }
$allowCpu = [bool](Get-Prop $answersObj 'allow_cpu' $false)

# probe, home, preflight table with fix-and-recheck loop
if ($FakeProbe) { $probe = Get-FakeProbe -Path $FakeProbe } else { $probe = Get-RealProbe }
$NsHome = Resolve-NsHome -Drives @($probe.drives) -AnswersObj $answersObj
if ($script:IsDry) { Show-Action "use NIGHTSHIFT_HOME = $NsHome" }

while ($true) {
    $rows = Get-PreflightRows -Probe $probe -NsHome $NsHome -AllowCpu $allowCpu
    Show-Rows -Rows $rows
    $fails = @($rows | Where-Object { $_.status -eq 'fail' })
    if ($fails.Count -eq 0) { break }
    if ($script:IsDry) {
        Write-Host ('Preflight would block setup: ' + (($fails | ForEach-Object { $_.code }) -join ', '))
        break
    }
    $blocking = $fails[0]
    if ($script:Unattended -or $blocking.code -eq 'NS-E101' -or $blocking.code -eq 'NS-E104' -or $blocking.code -eq 'NS-E105') {
        Write-Host 'Setup cannot continue until the red items are fixed.' -ForegroundColor Red
        exit 10
    }
    Open-FixPage -Code $blocking.code
    $reply = Read-Host 'Fix the red item, then press Enter to check again (or type Q to quit)'
    if ($reply -match '^[Qq]') { exit 10 }
    $probe = Get-RealProbe
}

$cuda = Get-CudaVersion -Text $probe.smi_text
$llamaPair = Get-LlamaPair -Cuda $cuda
$profileName = 'gpu'
if ((-not $probe.nvidia_smi) -or (-not $probe.gpu_name)) { $profileName = 'cpu-dev' }
$cudaString = ''
if ($null -ne $cuda) { $cudaString = [string]$cuda }

# long paths: set it when we are allowed to
if (-not $probe.long_paths) {
    if ($script:IsDry) {
        Show-Action 'set HKLM\SYSTEM\CurrentControlSet\Control\FileSystem LongPathsEnabled = 1 (only when run as administrator)'
    }
    elseif (Test-IsAdmin) {
        Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name 'LongPathsEnabled' -Value 1 -Type DWord
        Write-Host 'Enabled Windows long paths.'
    }
}

# tools
Write-Host 'Installing free tools (anything already present is skipped)'
Install-WingetTool -Id 'Git.Git' -Cmd 'git.exe' -Label 'Git' -Required $true
Install-WingetTool -Id 'Microsoft.WindowsTerminal' -Cmd 'wt.exe' -Label 'Windows Terminal' -Required $false
Install-WingetTool -Id 'Gyan.FFmpeg' -Cmd 'ffmpeg.exe' -Label 'FFmpeg' -Required $true

# home folder tree and user environment variable
$topDirs = @('bin', 'models', 'venvs', 'config', 'data', 'cache', 'imports', 'outbox', 'rejected', 'logs')
if ($script:IsDry) {
    Show-Action "create folder $NsHome with sub-folders $($topDirs -join ', ')"
    Show-Action "set user environment variable NIGHTSHIFT_HOME = $NsHome"
}
else {
    foreach ($d in $topDirs) { New-Item -ItemType Directory -Force -Path (Join-Path $NsHome $d) | Out-Null }
    [Environment]::SetEnvironmentVariable('NIGHTSHIFT_HOME', $NsHome, 'User')
    $env:NIGHTSHIFT_HOME = $NsHome
}

# uv and Python
$uv = Find-Uv -NsHome $NsHome
if ($null -ne $uv) {
    Write-Host "  uv : already present, skipped ($uv)"
}
else {
    $uv = Install-Uv -NsHome $NsHome -Manifest $Manifest
}
$pyVersion = [string]$Manifest.python_version
if ($script:IsDry) {
    Show-Action "$uv python install $pyVersion"
}
else {
    $env:UV_LINK_MODE = 'copy'
    Invoke-Native -Exe $uv -Arguments @('python', 'install', $pyVersion) -ExitCode 13 -What 'Installing Python'
}

# studio venv
$venv = Join-Path $NsHome 'venvs\studio'
$py = Join-Path $venv 'Scripts\python.exe'
if ($script:IsDry) {
    Show-Action "$uv venv --python $pyVersion $venv"
    Show-Action "$uv pip install --python $py -e `"${App}[studio]`""
}
else {
    if (-not (Test-Path -LiteralPath $py)) {
        Invoke-Native -Exe $uv -Arguments @('venv', '--python', $pyVersion, $venv) -ExitCode 14 -What 'Creating the studio environment'
    }
    Invoke-Native -Exe $uv -Arguments @('pip', 'install', '--python', $py, '-e', "${App}[studio]") -ExitCode 14 -What 'Installing the studio environment'
}

# hand-over file for the wizard (hardware summary)
$payload = [ordered]@{
    schema      = 1
    created     = (Get-Date).ToString('s')
    app_path    = $App
    home        = $NsHome
    profile     = $profileName
    llama_pair  = $llamaPair
    cuda        = $cudaString
    ram_gb      = $probe.ram_gb
    gpu         = [ordered]@{ name = $probe.gpu_name; vram_mb = $probe.vram_mb; driver = $probe.driver }
    drives      = @($probe.drives)
    winget      = $probe.winget
    checks      = @($rows)
}
$json = $payload | ConvertTo-Json -Depth 6
$preflightFile = Join-Path ([IO.Path]::GetTempPath()) 'nightshift-preflight.json'
if ($script:IsDry) {
    Show-Action "write hardware summary to $preflightFile"
    Show-Action ('preflight-json: ' + ($payload | ConvertTo-Json -Depth 6 -Compress))
}
else {
    [IO.File]::WriteAllText($preflightFile, $json, (New-Object System.Text.UTF8Encoding($false)))
}

# wizard
$wizardArgs = @('-m', 'installer.stage2.wizard', '--app-path', $App)
if ($answersPath) { $wizardArgs += @('--answers', $answersPath) }
if ($script:IsDry) {
    Show-Action "cd $App; $py $($wizardArgs -join ' ')"
    Write-Host 'Dry run finished. Nothing was changed.'
    exit 0
}
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = $App
Push-Location $App
try {
    & $py @wizardArgs
    $wizardExit = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $wizardExit
