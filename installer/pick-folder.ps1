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

function Write-NsFileSwap {
    # The result file is either absent or whole, never empty for a moment: the text goes to a temp file in the
    # same folder, is flushed, and is then swapped over -Path (File.Replace, or Move for a new file). A crash
    # in the middle used to leave an empty file, which the setup server reads as "dialog cancelled". Windows
    # refuses the swap for a moment while another process has -Path open without sharing delete (the setup
    # server reading it, an antivirus scan): that is retried, as launchers/_common.ps1 Write-NsFileAtomic
    # does (-Tries, a growing pause, the error thrown after the last try, the old file untouched). This
    # script does not load that file (it runs before the app is installed) and swaps with File.Replace,
    # not MoveFileEx. UTF-8 without a BOM.
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text,
        [int]$Tries = 20
    )
    $full = [System.IO.Path]::GetFullPath($Path)
    $tmp = Join-Path ([System.IO.Path]::GetDirectoryName($full)) ('~ns' + $PID + '-0-' + (Get-Random -Maximum 2000000000) + '.tmp')
    try {
        $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($Text)
        $fs = New-Object System.IO.FileStream($tmp, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) }
        finally { $fs.Dispose() }
        for ($attempt = 1; ; $attempt++) {
            try {
                if ([System.IO.File]::Exists($full)) { [System.IO.File]::Replace($tmp, $full, [NullString]::Value) }
                else { [System.IO.File]::Move($tmp, $full) }
                break
            }
            catch [System.IO.IOException], [System.UnauthorizedAccessException] {
                # a sharing or lock violation, or access denied while a reader has the target open
                if ($attempt -ge $Tries) { throw }
                Start-Sleep -Milliseconds ([Math]::Min(10 * $attempt, 100))
            }
        }
    }
    finally {
        try { if ([System.IO.File]::Exists($tmp)) { [System.IO.File]::Delete($tmp) } }
        catch { Write-Verbose ('temp file not removed: ' + $tmp) }
    }
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
Write-NsFileSwap -Path $Out -Text $chosen
exit 0
