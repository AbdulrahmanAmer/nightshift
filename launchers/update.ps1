Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift - Update" shortcut: pick the release zip, start the update in a detached worker and follow it.
. (Join-Path $PSScriptRoot '_common.ps1')

Add-Type -AssemblyName System.Windows.Forms
$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Title = 'Choose the Nightshift update'
$dlg.Filter = 'Nightshift update (Nightshift-*.zip)|Nightshift-*.zip|All zip files (*.zip)|*.zip'
$dlg.InitialDirectory = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Downloads'
if ($dlg.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }

$py = Get-NsVenvExe 'supervisor' 'python.exe'
if (-not (Test-Path -LiteralPath $py)) { Exit-NsWithPause ('Nightshift is not installed (missing ' + $py + ').') }

# The swap renames the app folder, and Windows refuses while any process has it as its working directory.
# Older shortcuts start this script with the app folder as cwd, so step out of it for real (Win32 cwd too).
Set-NsHomeLocation

# Start the worker and let this script follow it. `ns.exe` is not used on purpose: a running ns.exe (and the
# python.exe behind it) would stay open inside venvs\supervisor for the whole update, and Windows would then
# refuse to replace it while uv re-installs Nightshift into that venv. `--detach` returns at once, so no
# program from venvs\supervisor is running while the worker works; the worker itself runs from a copy.
$started = @(& $py -m ns_supervisor.cli update $dlg.FileName --detach)
$startCode = $LASTEXITCODE
if ($startCode -ne 0) {
    Exit-NsWithPause ('The update could not start (exit code ' + $startCode + '). Your previous version is still in place.') $startCode
}
$info = $null
try { $info = ($started | Select-Object -Last 1) | ConvertFrom-Json } catch { $info = $null }
if ($null -eq $info -or -not $info.PSObject.Properties['status']) {
    Exit-NsWithPause 'The update started, but its progress file is not known. Look in the logs folder of the Nightshift home.'
}
Write-Host 'The update runs in the background. This window shows its progress.'

$statusFile = [string]$info.status
$logFile = [string]$info.log
$seen = 0
$final = $null
$waited = 0
while ($null -eq $final -and $waited -lt 14400) {
    if (Test-Path -LiteralPath $logFile) {
        $lines = @(Get-Content -LiteralPath $logFile -ErrorAction SilentlyContinue)
        for ($i = $seen; $i -lt $lines.Count; $i++) { Write-Host $lines[$i] }
        $seen = $lines.Count
    }
    $state = $null
    try { $state = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json } catch { $state = $null }
    if ($null -ne $state -and $state.PSObject.Properties['state']) {
        if ($state.state -eq 'done' -or $state.state -eq 'failed') {
            $final = $state
        }
        elseif ($state.state -eq 'running' -and $state.PSObject.Properties['pid']) {
            # A worker that died without a word (killed, power loss) leaves "running" behind.
            $workerPid = [int]$state.pid
            if ($workerPid -gt 0 -and -not (Get-Process -Id $workerPid -ErrorAction SilentlyContinue)) {
                Exit-NsWithPause 'The update process stopped before it finished. Your previous version is still in place.'
            }
        }
    }
    if ($null -eq $final) {
        Start-Sleep -Seconds 1
        $waited++
    }
}
if ($null -eq $final) {
    Exit-NsWithPause ('The update is still running. Progress: ' + $logFile) 0
}
# the last lines of the log can arrive just after the state changes
if (Test-Path -LiteralPath $logFile) {
    $lines = @(Get-Content -LiteralPath $logFile -ErrorAction SilentlyContinue)
    for ($i = $seen; $i -lt $lines.Count; $i++) { Write-Host $lines[$i] }
}
if ($final.state -eq 'failed') {
    Exit-NsWithPause ('The update did not complete: ' + [string]$final.message + ' Your previous version is still in place unless the message says otherwise.') 1
}
Read-Host 'Update finished. Press Enter to close' | Out-Null
