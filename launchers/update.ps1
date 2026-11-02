Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift - Update" shortcut: pick the release zip, then let `ns update` verify and apply it.
. (Join-Path $PSScriptRoot '_common.ps1')

Add-Type -AssemblyName System.Windows.Forms
$dlg = New-Object System.Windows.Forms.OpenFileDialog
$dlg.Title = 'Choose the Nightshift update'
$dlg.Filter = 'Nightshift update (Nightshift-*.zip)|Nightshift-*.zip|All zip files (*.zip)|*.zip'
$dlg.InitialDirectory = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Downloads'
if ($dlg.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }

$ns = Get-NsVenvExe 'supervisor' 'ns.exe'
if (-not (Test-Path -LiteralPath $ns)) { Exit-NsWithPause ('Nightshift is not installed (missing ' + $ns + ').') }

# The swap renames the app folder, and Windows refuses while any process has it as its working directory.
# Older shortcuts start this script with the app folder as cwd, so step out of it for real (Win32 cwd too).
Set-NsHomeLocation
& $ns update $dlg.FileName
$code = $LASTEXITCODE
if ($code -ne 0) {
    Exit-NsWithPause ('The update did not complete (exit code ' + $code + '). Your previous version is still in place.') $code
}
Read-Host 'Update finished. Press Enter to close' | Out-Null
