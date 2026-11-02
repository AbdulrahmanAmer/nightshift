Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift Studio" shortcut: show the studio status, then offer a prompt for one command.
. (Join-Path $PSScriptRoot '_common.ps1')
Set-NsHomeLocation      # an open Studio window must not block `ns update` by holding the app folder

$studio = Get-NsVenvExe 'studio' 'studio.exe'
if (-not (Test-Path -LiteralPath $studio)) {
    Exit-NsWithPause ('The studio tools were not found at ' + $studio + '. Run INSTALL.cmd first.')
}

& $studio status
Write-Host ''
$line = Read-Host 'Command to run, for example: run --format shorts_only --count 1 (Enter to close)'
if ($line -and $line.Trim().Length -gt 0) {
    $cmdArgs = $line.Trim() -split '\s+'
    & $studio @cmdArgs
    Read-Host 'Finished. Press Enter to close' | Out-Null
}
