<#
.SYNOPSIS
    Makes the "files" folder that lets Nightshift setup run while GitHub is down (D-17).

.DESCRIPTION
    Run it with Windows PowerShell on any PC with internet, from the extracted Nightshift folder (OPERATOR.md,
    "If GitHub is down on install day", has the command line). It reads manifest.json, downloads every pinned program (uv, the Visual C++ runtime, FFmpeg, every llama.cpp
    build and its CUDA runtime, the voice engine, OpenCode, yt-dlp, the window files, the face finder and the
    spaCy model) and the Python build that the pinned uv installs for manifest.python_version, checks each
    SHA256 against the manifest, and writes them to a folder called files with a list, files\files.json.
    Copy that folder next to INSTALL.cmd on the PC being set up and run INSTALL.cmd: setup takes each program
    from it after checking its SHA256 again, and downloads only what is missing or does not match.

    A file that is already in the folder and matches its pin is kept, so a run that stopped can be repeated.
    A download that fails or does not match its pin is not kept; the script goes on and ends with exit code 1.
    Python has no pin in the manifest: uv checks that archive itself when setup installs it.

    -Only      keys to fetch, for example -Only uv,python,ffmpeg,yt-dlp,spacy,webview2 (default: everything).
               A key is a name under "bins" in manifest.json (yt-dlp and yt_dlp are the same), "spacy" or "python".
    -Out       the folder to fill (default: files in the current folder).
    -Manifest  the manifest to read (default: manifest.json of the folder this script is in).
    -Uv        a uv.exe to ask for the Python download (default: the pinned uv of this kit).
#>
[CmdletBinding()]
param(
    [string]$Manifest = '',
    [string]$Out = '',
    [string[]]$Only = @(),
    [string]$Uv = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Windows PowerShell 5.1 may still offer TLS 1.0 first; GitHub and the other sites need 1.2 (as stage1.ps1 does)
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 }
catch { Write-Verbose 'could not force TLS 1.2' }

$script:Listed = New-Object System.Collections.ArrayList
$script:Failed = New-Object System.Collections.ArrayList

function Get-UrlLeaf {
    param([string]$Url)
    return [System.Uri]::UnescapeDataString([System.IO.Path]::GetFileName(([System.Uri]$Url).AbsolutePath))
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLower()
}

function Save-Pinned {
    # One file: kept when it is there and matches; else downloaded to <name>.part, checked, renamed.
    # -Sha256 '' (Python only) records the digest of what was downloaded instead of checking it.
    param([string]$Key, [string]$Url, [string]$Sha256, [string]$Folder, [string]$Root)
    $name = Get-UrlLeaf -Url $Url
    $dest = Join-Path $Folder $name
    $part = $dest + '.part'
    $pin = $Sha256.ToLower()
    New-Item -ItemType Directory -Force -Path $Folder | Out-Null
    $have = ''
    if (Test-Path -LiteralPath $dest -PathType Leaf) {
        $have = Get-Sha256 -Path $dest
        if ($pin -and $have -ne $pin) {
            Write-Host ('  {0}: the copy in the folder does not match its pin; downloading it again' -f $name)
            Remove-Item -LiteralPath $dest -Force
            $have = ''
        }
    }
    if ($have) {
        Write-Host ('  {0}: already here' -f $name)
    }
    else {
        Write-Host ('  {0}: downloading {1}' -f $name, $Url)
        try {
            Invoke-WebRequest -Uri $Url -OutFile $part -UseBasicParsing
            $have = Get-Sha256 -Path $part
            if ($pin -and $have -ne $pin) {
                throw ('{0} does not match its pin (got {1}, the manifest pins {2}); it was not kept' -f $name, $have, $pin)
            }
            Move-Item -LiteralPath $part -Destination $dest -Force
        }
        catch {
            [void]$script:Failed.Add(('{0}: {1}' -f $Key, $_.Exception.Message))
            Write-Host ('  {0}' -f $_.Exception.Message) -ForegroundColor Yellow
            return $false
        }
        finally {
            if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue }
        }
    }
    $relative = $dest.Substring($Root.Length).TrimStart('\')
    [void]$script:Listed.Add([ordered]@{
            key = $Key; file = $relative; sha256 = $have; bytes = (Get-Item -LiteralPath $dest).Length
            pinned = [bool]$pin; url = $Url
        })
    return $true
}

function Get-PythonUrl {
    # The download the given uv installs for this Python version: the first CPython line of
    # "uv python list <version> --show-urls --only-downloads" for 64-bit Windows.
    param([string]$UvExe, [string]$Version)
    $lines = @(& $UvExe python list $Version --show-urls --only-downloads)
    $pattern = '^cpython-' + [regex]::Escape($Version) + '\.\d+-windows-x86_64-none\s+(https?://\S+)'
    foreach ($line in $lines) {
        if ([string]$line -match $pattern) { return $Matches[1] }
    }
    return ''
}

if (-not $Manifest) { $Manifest = Join-Path (Split-Path -Parent $PSScriptRoot) 'manifest.json' }
if (-not (Test-Path -LiteralPath $Manifest -PathType Leaf)) {
    Write-Host "manifest.json not found at $Manifest. Run this script from the extracted Nightshift folder, or pass -Manifest." -ForegroundColor Red
    exit 2
}
$m = Get-Content -Raw -LiteralPath $Manifest | ConvertFrom-Json
if (-not $Out) { $Out = Join-Path (Get-Location).Path 'files' }
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$Out = (Resolve-Path -LiteralPath $Out).Path.TrimEnd('\')

# the pins: every bins entry with a url and a real SHA256, then the spaCy wheel
$pins = [ordered]@{}
if ($null -eq $m.PSObject.Properties['bins'] -or $null -eq $m.bins) {
    Write-Host "$Manifest has no bins section: it is not a Nightshift manifest." -ForegroundColor Red
    exit 2
}
foreach ($p in $m.bins.PSObject.Properties) {
    $v = $p.Value
    if ($null -eq $v -or $null -eq $v.PSObject.Properties['url'] -or $null -eq $v.PSObject.Properties['sha256']) { continue }
    if ([string]$v.sha256 -notmatch '^[0-9A-Fa-f]{64}$') { continue }
    $pins[$p.Name] = $v
}
$python = $m.PSObject.Properties['python']
if ($null -ne $python -and $null -ne $python.Value -and $null -ne $python.Value.PSObject.Properties['spacy_wheel']) {
    $wheel = $python.Value.spacy_wheel
    if ($null -ne $wheel -and [string]$wheel.sha256 -match '^[0-9A-Fa-f]{64}$') { $pins['spacy'] = $wheel }
}

# -Only: "uv,yt-dlp" arrives as one string from powershell -File, so split it here
$wanted = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().ToLower().Replace('-', '_') } | Where-Object { $_ })
$known = @($pins.Keys) + 'python'
$unknown = @($wanted | Where-Object { $known -notcontains $_ })
if ($unknown.Count -gt 0) {
    Write-Host ('Unknown key for -Only: {0}. Known keys: {1}' -f ($unknown -join ', '), ($known -join ', ')) -ForegroundColor Red
    exit 2
}
if ($wanted.Count -eq 0) { $wanted = $known }

Write-Host "Nightshift files kit -> $Out"
foreach ($key in $pins.Keys) {
    if ($wanted -notcontains $key) { continue }
    [void](Save-Pinned -Key $key -Url ([string]$pins[$key].url) -Sha256 ([string]$pins[$key].sha256) -Folder $Out -Root $Out)
}

if ($wanted -contains 'python') {
    # uv asks a mirror for <tag>\<file>, so that is the layout: files\python\<tag>\<file>
    $temp = ''
    try {
        $uvExe = $Uv
        if (-not $uvExe) {
            if (-not $pins.Contains('uv')) { throw 'manifest.json pins no uv to ask for the Python download; pass -Uv <path to uv.exe>' }
            $uvZip = Join-Path $Out (Get-UrlLeaf -Url ([string]$pins['uv'].url))
            if (-not (Test-Path -LiteralPath $uvZip -PathType Leaf) -or (Get-Sha256 -Path $uvZip) -ne ([string]$pins['uv'].sha256).ToLower()) {
                throw 'the pinned uv is not in the folder (its download failed, or -Only left it out); add uv to -Only or pass -Uv <path to uv.exe>'
            }
            $temp = Join-Path $Out ('.uv-' + $PID)
            Expand-Archive -LiteralPath $uvZip -DestinationPath $temp -Force
            $found = Get-ChildItem -LiteralPath $temp -Recurse -Filter 'uv.exe' | Select-Object -First 1
            if ($null -eq $found) { throw 'uv.exe not found in the pinned uv archive' }
            $uvExe = $found.FullName
        }
        $version = '3.12'
        if ($null -ne $m.PSObject.Properties['python_version']) { $version = [string]$m.python_version }
        $url = Get-PythonUrl -UvExe $uvExe -Version $version
        if (-not $url) { throw "uv lists no CPython $version download for 64-bit Windows" }
        $segments = @(([System.Uri]$url).AbsolutePath.Trim('/') -split '/')
        $tag = [System.Uri]::UnescapeDataString($segments[$segments.Count - 2])
        [void](Save-Pinned -Key 'python' -Url $url -Sha256 '' -Folder (Join-Path (Join-Path $Out 'python') $tag) -Root $Out)
    }
    catch {
        [void]$script:Failed.Add(('python: {0}' -f $_.Exception.Message))
        Write-Host ('  python: {0}' -f $_.Exception.Message) -ForegroundColor Yellow
    }
    finally {
        if ($temp -and (Test-Path -LiteralPath $temp)) { Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# files.json lists what is in the folder and was checked in this run (a record for the owner; setup itself
# trusts only manifest.json and hashes every file again)
$manifestVersion = ''
if ($null -ne $m.PSObject.Properties['version']) { $manifestVersion = [string]$m.version }
$list = [ordered]@{ made = (Get-Date).ToString('s'); manifest_version = $manifestVersion; files = @($script:Listed) }
$json = $list | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText((Join-Path $Out 'files.json'), $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ''
if ($script:Failed.Count -gt 0) {
    Write-Host ('{0} of the files could not be fetched:' -f $script:Failed.Count) -ForegroundColor Red
    foreach ($line in $script:Failed) { Write-Host "  $line" }
    Write-Host 'Run the script again to fetch the rest (what is here is kept). Setup downloads whatever the folder lacks.'
    exit 1
}
Write-Host ('{0} files are in {1}. Copy that folder next to INSTALL.cmd and run INSTALL.cmd.' -f $script:Listed.Count, $Out) -ForegroundColor Green
exit 0
