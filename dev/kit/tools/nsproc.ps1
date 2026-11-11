# The GPU kit's process list (card gpu-kit; dev\prove.py runs it with --tools dev\kit\tools): the processes
# whose program file is under the install's home, except the kit and the proof runner themselves.
# -Down runs the install's own Stop All (app\launchers\stop-all.ps1) and lists again. It never stops a
# process by name, by command line or by pid: what is still listed after Stop All is left as it is.
param([Parameter(Mandatory = $true)][string]$NsHome, [switch]$Down)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-KitProcesses {
    $root = [System.IO.Path]::GetFullPath($NsHome).TrimEnd('\') + '\'
    $all = @()
    try { $all = @(Get-CimInstance -ClassName Win32_Process) } catch { Write-Output ('the process list could not be read: ' + $_.Exception.Message) }
    return @($all | Where-Object {
            $exe = [string]$_.ExecutablePath
            $cmd = [string]$_.CommandLine
            $exe -and $exe.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase) -and
            $cmd -notlike '*gpukit.py*' -and $cmd -notlike '*prove.py*'
        })
}

if ($Down) {
    $stop = Join-Path $NsHome 'app\launchers\stop-all.ps1'
    if (Test-Path -LiteralPath $stop) {
        $env:NIGHTSHIFT_HOME = $NsHome
        try { & $stop | Out-Null } catch { Write-Output ('Stop All said: ' + $_.Exception.Message) }
        Start-Sleep -Seconds 3
    }
    else {
        Write-Output ('no Stop All at ' + $stop)
    }
}
$now = @(Get-KitProcesses)
if ($now.Count -eq 0) { Write-Output 'CLEAN: no process of this install runs' }
else {
    foreach ($p in $now) {
        $line = ([string]$p.CommandLine) -replace '\s+', ' '
        Write-Output ('{0,6} {1,6:N0} MB  {2}' -f $p.ProcessId, ($p.WorkingSetSize / 1MB), $line.Substring(0, [Math]::Min(100, $line.Length)))
    }
}
