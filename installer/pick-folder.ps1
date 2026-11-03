<#
.SYNOPSIS
  Shows the Windows "choose a folder" dialog in front of every other window and writes the chosen folder to a file.

.DESCRIPTION
  The setup server (installer/stage2/web/folderdialog.py) starts this with powershell.exe -STA: the dialog needs a
  single-threaded apartment. An invisible, topmost owner window makes the dialog appear in front of the setup window
  instead of behind it. The result goes to -Out as UTF-8 without a BOM (an empty file means the dialog was cancelled),
  so paths with spaces and non-ASCII letters survive. Nothing is created or changed on disk.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Title = 'Choose a folder',
    [string]$Start = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-NearestFolder {
    # the folder the dialog opens in: the typed path, else its nearest parent that exists
    param([string]$Path)
    $p = $Path
    while ($p) {
        if (Test-Path -LiteralPath $p -PathType Container) { return $p }
        $parent = Split-Path -Parent $p
        if (-not $parent -or $parent -eq $p) { break }
        $p = $parent
    }
    return ''
}

Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()

$owner = New-Object System.Windows.Forms.Form
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$chosen = ''
try {
    $owner.TopMost = $true
    $owner.ShowInTaskbar = $false
    $owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
    $owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $owner.Opacity = 0
    $owner.Show()
    $owner.Activate()

    $dlg.Description = $Title
    $dlg.ShowNewFolderButton = $true
    $near = Get-NearestFolder -Path $Start
    if ($near) { $dlg.SelectedPath = $near }
    if ($dlg.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { $chosen = $dlg.SelectedPath }
}
finally {
    $dlg.Dispose()
    $owner.Dispose()
}
[System.IO.File]::WriteAllText($Out, $chosen, (New-Object System.Text.UTF8Encoding($false)))
exit 0
