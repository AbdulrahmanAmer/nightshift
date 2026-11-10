param([string]$DesktopDir = '')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift - Diagnostics" shortcut: a diagnostics zip on the Desktop, and the window says which one it is.
#  1. 'ns doctor --bundle', the full zip (ns_supervisor/doctor.py build_bundle). It runs from the supervisor
#     environment, which an interrupted update may have left half-filled (the launch script lets this
#     shortcut through after a repair that failed): its exit code and the zip it names are checked.
#  2. When that did not make a zip: a smaller zip made WITHOUT Python (Compress-Archive of Windows
#     PowerShell 5.1). It holds only files the full zip itself carries unchanged, with no redaction step:
#     the logs folder, config\install_state.json, the program's VERSION, and two files of run: the repair
#     record venv-repair.json and update_state.json (paths, versions, uv command lines and messages). Not
#     config\answers.json (the full zip removes its keys first), not the key store, not the settings.
#  3. When that fails too: no zip, said plainly, with what to send instead.
# The Desktop: -DesktopDir (the launch script passes its own); otherwise the one Windows names. A test run
# never names the real Desktop (NS_UNDER_TEST, PYTEST_CURRENT_TEST: then only -DesktopDir counts), and
# never opens Explorer.
. (Join-Path $PSScriptRoot '_common.ps1')

$underTest = [bool]($env:NS_UNDER_TEST -or $env:PYTEST_CURRENT_TEST)
$nsHome = Get-NsHome
$desk = $DesktopDir
if (-not $desk) {
    if ($underTest) { Exit-NsWithPause 'No diagnostics zip was made: a test run never writes to the real Desktop (pass -DesktopDir).' 1 }
    $desk = [Environment]::GetFolderPath('Desktop')
}
$stamp = [DateTime]::Now.ToString('yyyy-MM-dd-HHmm', [Globalization.CultureInfo]::InvariantCulture)

function Get-FullZip {
    # The Python route: the zip 'ns doctor --bundle' names, when it ended with 0 and that file is there; else
    # @{ zip = $null; why = <what went wrong> }.
    $ns = Get-NsVenvExe 'supervisor' 'ns.exe'
    if (-not (Test-Path -LiteralPath $ns -PathType Leaf)) { return @{ zip = $null; why = ($ns + ' is missing') } }
    $eap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        $out = @(& $ns doctor --bundle --out-dir $desk 2>&1 | ForEach-Object { [string]$_ })
        $code = $LASTEXITCODE
    } catch {
        return @{ zip = $null; why = ('it could not be run: ' + $_.Exception.Message) }
    } finally { $ErrorActionPreference = $eap }
    $out | ForEach-Object { Write-Host $_ }
    $named = $null
    foreach ($line in $out) { if ($line -match '^Diagnostics written to (.+\.zip)\s*$') { $named = $Matches[1] } }
    if ($code -eq 0 -and $named -and (Test-Path -LiteralPath $named -PathType Leaf)) { return @{ zip = $named; why = '' } }
    $last = [string](@($out | Where-Object { $_.Trim() }) | Select-Object -Last 1)
    $why = 'ns doctor --bundle ended with exit code ' + $code
    if ($code -eq 0) { $why = 'ns doctor --bundle named no zip that exists' }
    if ($last) { $why += ': ' + $last.Trim() }
    return @{ zip = $null; why = $why }
}

function Get-SmallZip {
    # Without Python. @{ zip = <path or $null>; why = ''; left = <files that could not be read> }
    $stage = Join-Path (Join-Path $nsHome 'run') ('diagnostics-' + $PID)
    $zip = Join-Path $desk ('nightshift-diagnostics-small-' + $stamp + '.zip')
    $left = @()
    try {
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
        [void](New-Item -ItemType Directory -Path $stage -Force)
        $items = @(
            @((Join-Path $nsHome 'logs'), 'logs'),
            @((Join-Path $nsHome 'config\install_state.json'), 'install_state.json'),
            @((Join-Path (Get-NsAppPath) 'VERSION'), 'VERSION'),
            @((Join-Path $nsHome 'run\venv-repair.json'), 'run\venv-repair.json'),
            @((Join-Path $nsHome 'run\update_state.json'), 'run\update_state.json')
        )
        foreach ($item in $items) {
            if (-not (Test-Path -LiteralPath $item[0])) { $left += ($item[1] + ': not there'); continue }
            $files = New-Object System.Collections.ArrayList
            if (Test-Path -LiteralPath $item[0] -PathType Container) {
                foreach ($fi in @(Get-ChildItem -LiteralPath $item[0] -Recurse -File -ErrorAction SilentlyContinue)) {
                    [void]$files.Add([pscustomobject]@{ From = $fi.FullName; To = (Join-Path $item[1] $fi.FullName.Substring($item[0].Length).TrimStart('\')) })
                }
            } else { [void]$files.Add([pscustomobject]@{ From = $item[0]; To = $item[1] }) }
            foreach ($f in $files) {
                $to = Join-Path $stage $f.To
                try {
                    [void](New-Item -ItemType Directory -Path (Split-Path -Parent $to) -Force)
                    # (read with every share mode: a log another program still writes is copied as it is now)
                    $src = New-Object System.IO.FileStream($f.From, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
                    try {
                        $dst = [IO.File]::Create($to)
                        try { $src.CopyTo($dst) } finally { $dst.Dispose() }
                    } finally { $src.Dispose() }
                } catch { $left += ($f.To + ': ' + $_.Exception.Message) }
            }
        }
        $notes = @('A smaller diagnostics zip, made without Python on ' + [DateTime]::Now.ToString('s') + ' because the full one could not be made.')
        $notes += $left
        [IO.File]::WriteAllLines((Join-Path $stage 'bundle-notes.txt'), [string[]]$notes)
        if (-not (Test-Path -LiteralPath $desk -PathType Container)) { throw ('the Desktop folder ' + $desk + ' is not there') }
        Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force
        return @{ zip = $zip; why = ''; left = $left }
    } catch {
        try { if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force } } catch { Write-Verbose 'partial zip left' }
        return @{ zip = $null; why = $_.Exception.Message; left = $left }
    } finally {
        try { if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force } } catch { Write-Verbose 'staging folder left' }
    }
}

function Show-Zip {
    param([string]$Zip)
    if (-not $underTest) { Start-Process -FilePath 'explorer.exe' -ArgumentList ('/select,"' + $Zip + '"') }
}

$full = Get-FullZip
if ($full.zip) {
    Show-Zip $full.zip
    Exit-NsWithPause ('The diagnostics zip is on your Desktop: ' + $full.zip) 0
}
Write-Host ''
Write-Host ('The full diagnostics zip could not be made (' + $full.why + ').')
$small = Get-SmallZip
if ($small.zip) {
    Show-Zip $small.zip
    $said = 'A smaller diagnostics zip was made without Python instead: ' + $small.zip + '. It holds the logs folder, config\install_state.json, the VERSION file, run\venv-repair.json and run\update_state.json (no settings and no keys; bundle-notes.txt in it names what was missing). Send that zip.'
    Exit-NsWithPause $said 0
}
$logs = Join-Path $nsHome 'logs'
Exit-NsWithPause ('No diagnostics zip was made: the smaller zip without Python failed too (' + $small.why + '). Instead, send the folder ' + $logs + ' (in Explorer: right-click it, Send to, Compressed (zipped) folder) and, if it is there, the file ' + (Join-Path $nsHome 'run\venv-repair.json') + '.') 1
