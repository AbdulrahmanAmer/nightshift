Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift - Diagnostics" shortcut: write the diagnostics zip to the Desktop and show it.
. (Join-Path $PSScriptRoot '_common.ps1')

$ns = Get-NsVenvExe 'supervisor' 'ns.exe'
if (-not (Test-Path -LiteralPath $ns)) { Exit-NsWithPause ('Nightshift is not installed (missing ' + $ns + ').') }

& $ns doctor --bundle
Start-Process -FilePath 'explorer.exe' -ArgumentList ([Environment]::GetFolderPath('Desktop'))
Read-Host 'The diagnostics zip is on your Desktop. Press Enter to close' | Out-Null
