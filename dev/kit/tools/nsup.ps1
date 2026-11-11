# The GPU kit's engine start (card gpu-kit; dev\prove.py runs it with --tools dev\kit\tools): engine and
# dashboard service through the install's own launcher functions, headless, no window of any kind.
param([Parameter(Mandatory = $true)][string]$NsHome)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$env:NIGHTSHIFT_HOME = $NsHome
. (Join-Path $NsHome 'app\launchers\_common.ps1')
Set-NsHomeLocation
if (-not (Test-NsSupervisor)) {
    Start-NsSupervisor -Headless
    if (-not (Wait-NsSupervisor -TimeoutSec 90)) { Write-Output 'ENGINE DID NOT START'; exit 1 }
}
[void](Start-NsDashboardService)
if (-not (Wait-NsPort -Url 'http://127.0.0.1:8100' -TimeoutSec 60)) { Write-Output 'DASHBOARD DID NOT START'; exit 1 }
Write-Output 'engine + dashboard up (headless)'
