<#
.SYNOPSIS
  Builds the Nightshift partner bundle (BUILD_SPEC section 1.3, docs/ENGINEERING.md section 7).

.DESCRIPTION
  git archive of HEAD (so the committed dashboard build ns_dashboard/static/** is included and a local
  ns_dashboard/ui/node_modules never is), minus .github, docs/superpowers/plans/_* and test fixtures over 1 MB, plus a VERSION
  file and the operator files at the root. The finished zip is verified (no .venv, models, binaries or videos;
  under 25 MB) and written as
    <OutDir>\Nightshift-Setup-<ver>.zip         (the installer bundle)
    <OutDir>\Nightshift-Setup-<ver>.zip.sha256  ("<hash>  <name>", sha256sum format)
    <OutDir>\Nightshift-Update-<ver>.zip        (same bytes; the update-from-file package the update flow expects)
    <OutDir>\Nightshift-Update-<ver>.zip.sha256
  -DryRun runs the same steps in a temporary folder, lists every entry and writes nothing to OutDir.

  Works under Windows PowerShell 5.1 and PowerShell 7. Run in CI or on the GPU PC, never on the authoring laptop.

.PARAMETER Version
  SemVer, with or without a leading v (the tag name works). Defaults to the VERSION file; a value that differs
  from the VERSION file is an error.

.PARAMETER OutDir
  Output folder. Defaults to <repo>\dist.

.PARAMETER DryRun
  Build and verify in a temp folder, list the contents, write nothing to OutDir.

.PARAMETER WebView2Dir
  A folder holding exactly Microsoft.Web.WebView2.Core.dll, Microsoft.Web.WebView2.Wpf.dll and WebView2Loader.dll
  (the release workflow extracts them from the pinned nupkg after checking its SHA256). They are copied to
  bin/webview2/ INSIDE the zip, so the very first setup window already uses the frameless host. Those three files
  are the only thing allowed under bin/ in the zip.
#>
[CmdletBinding()]
param(
    [string]$Version = '',
    [string]$OutDir = '',
    [switch]$DryRun,
    [string]$WebView2Dir = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$MaxZipBytes = 25MB
$MaxFixtureBytes = 1MB
$ForbiddenNames = @('*.gguf', '*.onnx', '*.safetensors', '*.mp4', '*.exe')
$ForbiddenDirs = @('.venv', 'venv', '__pycache__', '.git', '.github', 'node_modules')
# The dashboard and installer builds are committed (CI job ui-build keeps them equal to ns_dashboard/ui); the partner PC has no node.
# The only binaries a bundle may carry: the WebView2 host DLLs, in bin/webview2/ (the *.exe rule still applies).
$WebView2Files = @('Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.Wpf.dll', 'WebView2Loader.dll')
$AllowedBinEntries = @($WebView2Files | ForEach-Object { 'bin/webview2/' + $_ })
$RequiredEntries = @('VERSION', 'pyproject.toml', 'ns_common/__init__.py', 'ns_dashboard/static/index.html', 'ns_dashboard/static/app.js', 'ns_dashboard/static/brand/nightshift.ico', 'installer/ui/static/index.html', 'installer/ui/static/app.js', 'DEMO.cmd', 'dev/demo/serve.js')

function Invoke-Git {
    param([string[]]$GitArgs)
    & git -C $Repo @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Get-RelPath {
    param([string]$Root, [string]$Full)
    return $Full.Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
}

function New-ZipFromDirectory {
    param([string]$SourceDir, [string]$ZipPath)
    $fs = [System.IO.File]::Open($ZipPath, [System.IO.FileMode]::Create)
    try {
        $zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create)
        try {
            $files = Get-ChildItem -LiteralPath $SourceDir -Recurse -Force -File | Sort-Object FullName
            foreach ($f in $files) {
                $entry = Get-RelPath $SourceDir $f.FullName
                $level = [System.IO.Compression.CompressionLevel]::Optimal
                [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $f.FullName, $entry, $level)
            }
        }
        finally {
            $zip.Dispose()
        }
    }
    finally {
        $fs.Dispose()
    }
}

function Get-ZipEntries {
    param([string]$ZipPath)
    $z = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $list = @()
        foreach ($e in $z.Entries) {
            $list += [pscustomobject]@{ Name = $e.FullName; Length = [int64]$e.Length }
        }
        return $list
    }
    finally {
        $z.Dispose()
    }
}

function Test-BundleZip {
    param([string]$ZipPath)
    $problems = New-Object System.Collections.Generic.List[string]
    $entries = @(Get-ZipEntries $ZipPath)
    foreach ($e in $entries) {
        $segments = $e.Name.Split('/')
        for ($i = 0; $i -lt ($segments.Length - 1); $i++) {
            if ($ForbiddenDirs -contains $segments[$i]) {
                $problems.Add("forbidden folder '$($segments[$i])' in entry $($e.Name)")
            }
        }
        $leaf = $segments[$segments.Length - 1]
        foreach ($pattern in $ForbiddenNames) {
            if ($leaf -like $pattern) {
                $problems.Add("forbidden file type $pattern in entry $($e.Name)")
            }
        }
        if ($e.Name.Contains('\')) {
            $problems.Add("backslash in entry name $($e.Name)")
        }
        if ($e.Name -like 'bin/*' -and ($AllowedBinEntries -notcontains $e.Name)) {
            $problems.Add("entry not allowed under bin/ (only $($AllowedBinEntries -join ', ')): $($e.Name)")
        }
    }
    foreach ($req in $RequiredEntries) {
        if (-not ($entries | Where-Object { $_.Name -eq $req })) {
            $problems.Add("required entry missing: $req")
        }
    }
    if ($WebView2Dir) {
        foreach ($req in $AllowedBinEntries) {
            if (-not ($entries | Where-Object { $_.Name -eq $req })) {
                $problems.Add("required entry missing: $req")
            }
        }
    }
    $size = (Get-Item -LiteralPath $ZipPath).Length
    if ($size -gt $MaxZipBytes) {
        $problems.Add(('zip is {0:N1} MB, limit is {1} MB' -f ($size / 1MB), ($MaxZipBytes / 1MB)))
    }
    return $problems
}

# --- version ---------------------------------------------------------------------------------------
$versionFile = Join-Path $Repo 'VERSION'
if (-not (Test-Path -LiteralPath $versionFile)) {
    throw "VERSION file missing: $versionFile"
}
$fileVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
if ($Version) {
    $Version = $Version.Trim().TrimStart('v')
}
else {
    $Version = $fileVersion
}
if ($Version -notmatch '^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$') {
    throw "Version '$Version' is not SemVer (expected e.g. 0.1.0)."
}
if ($Version -ne $fileVersion) {
    throw "Version '$Version' does not match the VERSION file ('$fileVersion'). Update VERSION first."
}

if (-not $OutDir) {
    $OutDir = Join-Path $Repo 'dist'
}
elseif (-not [System.IO.Path]::IsPathRooted($OutDir)) {
    $OutDir = Join-Path $PWD.Path $OutDir
}

$dirty = & git -C $Repo status --porcelain
if ($dirty) {
    Write-Warning 'Working tree has uncommitted changes; the bundle contains HEAD only.'
}

$zipName = "Nightshift-Setup-$Version.zip"
$work = Join-Path ([System.IO.Path]::GetTempPath()) ('nightshift-bundle-' + [System.Guid]::NewGuid().ToString('N').Substring(0, 8))
$stage = Join-Path $work 'stage'
New-Item -ItemType Directory -Force -Path $stage | Out-Null

try {
    # --- archive HEAD and extract -------------------------------------------------------------------
    $rawZip = Join-Path $work 'archive.zip'
    Invoke-Git @('archive', '--format=zip', '-o', $rawZip, 'HEAD')
    [System.IO.Compression.ZipFile]::ExtractToDirectory($rawZip, $stage)
    Remove-Item -LiteralPath $rawZip -Force

    # --- exclusions ---------------------------------------------------------------------------------
    $removed = New-Object System.Collections.Generic.List[string]
    $github = Join-Path $stage '.github'
    if (Test-Path -LiteralPath $github) {
        Remove-Item -LiteralPath $github -Recurse -Force
        $removed.Add('.github/')
    }
    # git archive only holds tracked files, so node_modules cannot appear; drop it anyway in case it was ever committed.
    $nodeModules = Join-Path $stage 'ns_dashboard/ui/node_modules'
    if (Test-Path -LiteralPath $nodeModules) {
        Remove-Item -LiteralPath $nodeModules -Recurse -Force
        $removed.Add('ns_dashboard/ui/node_modules/')
    }
    $plans = Join-Path $stage 'docs/superpowers/plans'
    if (Test-Path -LiteralPath $plans) {
        foreach ($p in @(Get-ChildItem -LiteralPath $plans -File -Filter '_*')) {
            $removed.Add((Get-RelPath $stage $p.FullName))
            Remove-Item -LiteralPath $p.FullName -Force
        }
    }
    $fixtures = Join-Path $stage 'tests/fixtures'
    if (Test-Path -LiteralPath $fixtures) {
        $big = @(Get-ChildItem -LiteralPath $fixtures -Recurse -File | Where-Object { $_.Length -gt $MaxFixtureBytes })
        foreach ($b in $big) {
            $removed.Add((Get-RelPath $stage $b.FullName))
            Remove-Item -LiteralPath $b.FullName -Force
        }
    }
    foreach ($r in $removed) {
        Write-Host "  excluded: $r"
    }

    # --- additions ----------------------------------------------------------------------------------
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText((Join-Path $stage 'VERSION'), "$Version`n", $utf8)

    # manifest.json "version" is what setup compares on a re-run (s04 venvs, s08 smoke test) to decide whether
    # to refresh them. The repository copy is never bumped, so the bundle takes the number from VERSION.
    $manifestFile = Join-Path $stage 'manifest.json'
    $manifestText = [System.IO.File]::ReadAllText($manifestFile)
    $versionLine = [regex]::Match($manifestText, '(?m)^(  "version"\s*:\s*")[^"]*(")')
    if (-not $versionLine.Success) { throw 'manifest.json has no top-level "version" line to update.' }
    $manifestText = $manifestText.Substring(0, $versionLine.Index) + $versionLine.Groups[1].Value + $Version + $versionLine.Groups[2].Value + $manifestText.Substring($versionLine.Index + $versionLine.Length)
    [System.IO.File]::WriteAllText($manifestFile, $manifestText, $utf8)
    Write-Host "  manifest.json version: $Version"

    $answersSrc = Join-Path $stage 'installer/answers.example.json'
    $answersRoot = Join-Path $stage 'answers.example.json'
    if ((Test-Path -LiteralPath $answersSrc) -and -not (Test-Path -LiteralPath $answersRoot)) {
        Copy-Item -LiteralPath $answersSrc -Destination $answersRoot
    }
    if ($WebView2Dir) {
        $wvSrc = (Resolve-Path -LiteralPath $WebView2Dir).Path
        $found = @(Get-ChildItem -LiteralPath $wvSrc -Force -File | ForEach-Object { $_.Name } | Sort-Object)
        $want = @($WebView2Files | Sort-Object)
        if (($found -join '|') -ne ($want -join '|')) {
            throw "WebView2Dir must hold exactly $($WebView2Files -join ', '); found: $($found -join ', ')"
        }
        $wvDest = Join-Path $stage 'bin/webview2'
        New-Item -ItemType Directory -Force -Path $wvDest | Out-Null
        foreach ($f in $WebView2Files) {
            Copy-Item -LiteralPath (Join-Path $wvSrc $f) -Destination (Join-Path $wvDest $f)
        }
        Write-Host '  window files: bin/webview2 (3 DLLs)'
    }

    foreach ($name in @('OPERATOR.md', 'answers.example.json', 'INSTALL.cmd', 'DEMO.cmd')) {
        if (Test-Path -LiteralPath (Join-Path $stage $name)) {
            Write-Host "  root file present: $name"
        }
        else {
            Write-Warning "$name is not in the bundle (not committed yet)"
        }
    }

    # --- zip and verify -----------------------------------------------------------------------------
    $zipDir = $work
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
        $zipDir = $OutDir
    }
    $zipPath = Join-Path $zipDir $zipName
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    New-ZipFromDirectory $stage $zipPath

    $problems = @(Test-BundleZip $zipPath)
    if ($problems.Count -gt 0) {
        foreach ($p in $problems) {
            Write-Error $p -ErrorAction Continue
        }
        throw "Bundle verification failed with $($problems.Count) problem(s)."
    }
    $zr = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $entry = $zr.GetEntry('manifest.json')
        $reader = New-Object System.IO.StreamReader($entry.Open())
        try { $zipManifestVersion = [string](($reader.ReadToEnd() | ConvertFrom-Json).version) } finally { $reader.Dispose() }
    }
    finally { $zr.Dispose() }
    if ($zipManifestVersion -ne $Version) {
        throw "manifest.json in the zip says version '$zipManifestVersion', expected '$Version'."
    }
    Write-Host "  verified: manifest.json version $zipManifestVersion = VERSION"

    $entries = @(Get-ZipEntries $zipPath)
    $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
    $sizeMb = (Get-Item -LiteralPath $zipPath).Length / 1MB

    if ($DryRun) {
        Write-Host "DRY RUN: $zipName would contain $($entries.Count) entries:"
        foreach ($e in $entries) {
            Write-Host ('  {0,10}  {1}' -f $e.Length, $e.Name)
        }
    }
    else {
        $ascii = New-Object System.Text.ASCIIEncoding
        [System.IO.File]::WriteAllText("$zipPath.sha256", "$hash  $zipName`n", $ascii)
        $appName = "Nightshift-Update-$Version.zip"
        $appZip = Join-Path $OutDir $appName
        Copy-Item -LiteralPath $zipPath -Destination $appZip -Force
        [System.IO.File]::WriteAllText("$appZip.sha256", "$hash  $appName`n", $ascii)
        Write-Host "Wrote $zipPath"
        Write-Host "Wrote $zipPath.sha256"
        Write-Host "Wrote $appZip"
        Write-Host "Wrote $appZip.sha256"
    }
    Write-Host ('{0}: {1} entries, {2:N2} MB, sha256 {3}' -f $zipName, $entries.Count, $sizeMb, $hash)
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
