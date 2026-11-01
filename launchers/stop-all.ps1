Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift - Stop All" shortcut: stop the engine and every model process.
. (Join-Path $PSScriptRoot '_common.ps1')

$ns = Get-NsVenvExe 'supervisor' 'ns.exe'
if (Test-Path -LiteralPath $ns) {
    & $ns stop
}
else {
    $py = Get-NsVenvExe 'supervisor' 'python.exe'
    if (-not (Test-Path -LiteralPath $py)) { Exit-NsWithPause ('Nightshift is not installed (missing ' + $py + ').') }
    & $py -m ns_supervisor.cli stop
}
Start-Sleep -Seconds 2
