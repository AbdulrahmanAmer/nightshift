# The GPU kit's process list (card gpu-kit; dev\prove.py runs it with --tools dev\kit\tools): the processes
# whose program file is under the install's home, except the kit and the proof runner themselves.
# It only lists. -Down, which the proof runner sends after an engine it started itself, stops NOTHING here:
# on the owner's PC the kit never sends Stop All (it would end a job, a Nightshift Code session or the
# Dashboard somebody else is using). The kit brings Nightshift up before the proofs under its own record
# and ends that engine itself at the end of its run, only while nothing else uses it (gpukit.py
# leave_engine); an engine nobody uses ends by itself after 30 minutes.
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
    Write-Output 'NOT STOPPED: the GPU kit never sends Stop All. Nightshift is left as it is; the kit ends its own engine at the end of its run when nothing else uses it.'
}
$now = @(Get-KitProcesses)
if ($now.Count -eq 0) { Write-Output 'CLEAN: no process of this install runs' }
else {
    foreach ($p in $now) {
        $line = ([string]$p.CommandLine) -replace '\s+', ' '
        Write-Output ('{0,6} {1,6:N0} MB  {2}' -f $p.ProcessId, ($p.WorkingSetSize / 1MB), $line.Substring(0, [Math]::Min(100, $line.Length)))
    }
}
