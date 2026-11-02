Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Shared helpers for the Nightshift launchers (dot-sourced; not a shortcut target).

function Get-NsHome {
    if ($env:NIGHTSHIFT_HOME) { return $env:NIGHTSHIFT_HOME }
    return (Join-Path $env:LOCALAPPDATA 'Nightshift')
}

function Set-NsHomeLocation {
    # Step out of the app folder. Set-Location only moves PowerShell's own location; the powershell.exe process
    # keeps the Win32 current directory it was started with, and Windows refuses to rename (swap) a folder that
    # is any process's current directory. [Environment]::CurrentDirectory is the Win32 one.
    $nsHome = Get-NsHome
    if (Test-Path -LiteralPath $nsHome) {
        Set-Location -LiteralPath $nsHome
        [Environment]::CurrentDirectory = $nsHome
    }
}

function Get-NsAppPath {
    return (Split-Path -Parent $PSScriptRoot)
}

function Get-NsSupervisorPort {
    $file = Join-Path (Get-NsHome) 'config\runtime.json'
    if (Test-Path -LiteralPath $file) {
        $json = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
        if ($json.PSObject.Properties.Name -contains 'supervisor_port') { return [int]$json.supervisor_port }
    }
    return 8099
}

function Get-NsSupervisorUrl {
    return ('http://127.0.0.1:' + (Get-NsSupervisorPort))
}

function Get-NsVenvExe {
    param([string]$Venv, [string]$Name)
    return (Join-Path (Get-NsHome) ('venvs\' + $Venv + '\Scripts\' + $Name))
}

function Test-NsSupervisor {
    try {
        Invoke-RestMethod -Uri ((Get-NsSupervisorUrl) + '/status') -TimeoutSec 3 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Start-NsSupervisor {
    param([switch]$Headless)
    $py = Get-NsVenvExe 'supervisor' 'python.exe'
    if (-not (Test-Path -LiteralPath $py)) { throw ('Supervisor venv not found: ' + $py + '. Run INSTALL.cmd first.') }
    $argList = @('-m', 'ns_supervisor.app')
    $style = 'Minimized'
    if ($Headless) {
        $argList += '--headless'
        $style = 'Hidden'
    }
    Start-Process -FilePath $py -ArgumentList $argList -WindowStyle $style -WorkingDirectory (Get-NsAppPath)
}

function Wait-NsSupervisor {
    param([int]$TimeoutSec = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-NsSupervisor) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Invoke-NsPost {
    param([string]$Path, [hashtable]$Body, [int]$TimeoutSec = 30)
    $json = $Body | ConvertTo-Json
    $uri = (Get-NsSupervisorUrl) + $Path
    return (Invoke-RestMethod -Method Post -Uri $uri -ContentType 'application/json' -Body $json -TimeoutSec $TimeoutSec)
}

function Exit-NsWithPause {
    param([string]$Message, [int]$Code = 1)
    Write-Host $Message
    Read-Host 'Press Enter to close' | Out-Null
    exit $Code
}

function Start-NsDashboardService {
    # Ask a running engine to start the dashboard service. An engine left by `ns verify` or the nightly
    # task runs headless and never starts it on its own, so this is needed even when Test-NsSupervisor is true.
    param([int]$TimeoutSec = 120)
    try {
        Invoke-NsPost -Path '/warmup' -Body @{ service = 'dashboard' } -TimeoutSec $TimeoutSec | Out-Null
        return $true
    }
    catch {
        Write-Host ('The engine could not start the dashboard service: ' + $_.Exception.Message)
        return $false
    }
}

function Wait-NsPort {
    param([string]$Url, [int]$TimeoutSec = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 | Out-Null
            return $true
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}
