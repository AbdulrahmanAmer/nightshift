param(
    [Parameter(Mandatory = $true)][string]$Launcher
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# What the Desktop shortcuts start. Setup copies this file to <home>\nightshift-launch.ps1, BESIDE the program
# folder (<home>\app), so it is still there when the program folder is not: an update or a rollback that lost
# power between two folder renames leaves no <home>\app, and then no launcher, no ns command and no Dashboard
# can start (they all live in, or import from, that folder). A shortcut that pointed straight at the launcher
# only flashed a window. This file needs nothing but Windows PowerShell 5.1: it runs the launcher when it is
# there, and says in plain words what to do when it is not.
if (Test-Path -LiteralPath $Launcher -PathType Leaf) {
    $global:LASTEXITCODE = 0
    & $Launcher
    exit $LASTEXITCODE
}

$app = Split-Path -Parent (Split-Path -Parent $Launcher)
$swap = $app + '.swap'
Write-Host ''
Write-Host 'Nightshift cannot start: its program folder is missing or incomplete.'
Write-Host ('  not found: ' + $Launcher)
Write-Host ''
Write-Host 'This happens when an update or a rollback was interrupted, for example by a power cut.'
if (Test-Path -LiteralPath $swap -PathType Container) {
    Write-Host ('The program is waiting in ' + $swap + ' and nothing in it was deleted.')
}
Write-Host ''
Write-Host 'What to do:'
Write-Host '  1. Close every Nightshift window and any Explorer window that shows the Nightshift folder.'
Write-Host '  2. Run INSTALL.cmd from the Nightshift zip (extract the zip again if that folder is gone)'
Write-Host '     and choose the same install folder. Setup puts the program folder back.'
Write-Host '  3. Start Nightshift from this shortcut again.'
Write-Host ''
Read-Host 'Press Enter to close' | Out-Null
exit 1
