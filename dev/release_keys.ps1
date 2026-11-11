# dev\release_keys.ps1 -Create -BackupFolder <a USB stick or other folder off this PC>
#
# The owner's ONE step for signed update packages (card update-signing, OPERATOR.md "The release key").
# Run once, on the laptop with the repository, signed in to GitHub with gh, in Windows PowerShell:
#
#   powershell -ExecutionPolicy Bypass -File dev\release_keys.ps1 -Create -BackupFolder E:\
#
# What it does, in this order, and what is true when it stops at each point:
#   1. checks (nothing changed when one fails): the backup folder exists, is not inside this checkout and holds
#      no backup key file yet; ns_common\release_keys.py pins no key yet; Python and gh are there; gh is signed in
#   2. makes two Ed25519 private keys in memory (32 random bytes each) and asks dev\sign_release.py
#      --public-key for their public halves (the private key goes to it on stdin, never as an argument)
#   3. gives the PRIMARY private key to `gh secret set NS_RELEASE_SIGNING_KEY` on stdin: it is never written
#      to a disk of this PC and never printed (gh fails: nothing was changed)
#   4. writes the BACKUP private key to <BackupFolder>\nightshift-release-backup-key.txt, and nowhere else
#      (fails: the secret holds a key no file knows; run the script again, it replaces the secret)
#   5. writes the two PUBLIC keys into ns_common\release_keys.py (fails: the backup file of this run is
#      removed again; run the script again)
#   6. prints the two key ids and what to keep
# A second run refuses: the keys already exist (to replace them see "Lost key" in OPERATOR.md).
#
# -DryRun -GhExe <stand-in> -KeysFile <copy> [-Python <exe>] is for the tests: every outward effect goes to a
# stand-in. The stand-ins are accepted only with -DryRun, and a test run without -DryRun is refused.
# Windows PowerShell 5.1, ASCII only.
param(
    [switch]$Create,
    [string]$BackupFolder = "",
    [switch]$DryRun,
    [string]$GhExe = "",
    [string]$KeysFile = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Continue"
$OutputEncoding = New-Object System.Text.ASCIIEncoding
$Repo = "AbdulrahmanAmer/nightshift"
$SecretName = "NS_RELEASE_SIGNING_KEY"
$BackupName = "nightshift-release-backup-key.txt"
$Unchanged = "Nothing was changed."
$Root = Split-Path -Parent $PSScriptRoot
$SignScript = Join-Path $PSScriptRoot "sign_release.py"

function Stop-Here([string]$Text) {
    Write-Output $Text
    exit 1
}

if (-not $Create) {
    Stop-Here ("Usage: powershell -ExecutionPolicy Bypass -File dev\release_keys.ps1 -Create -BackupFolder " +
        "<a USB stick or other folder off this PC>. It makes the release key once (OPERATOR.md, 'The release key'). $Unchanged")
}
if (-not $DryRun) {
    if ($env:PYTEST_CURRENT_TEST) {
        Stop-Here "A test run must pass -DryRun with -GhExe and -KeysFile: the real GitHub secret and this checkout are not for tests. $Unchanged"
    }
    if ($GhExe -or $KeysFile -or $Python) {
        Stop-Here "-GhExe, -KeysFile and -Python are stand-ins for tests and count only with -DryRun. $Unchanged"
    }
    $GhExe = "gh"
    $KeysFile = Join-Path $Root "ns_common\release_keys.py"
    $Python = "python"
} else {
    if (-not $GhExe -or -not $KeysFile) {
        Stop-Here "-DryRun needs -GhExe <stand-in> and -KeysFile <copy>: it never reaches the real gh or this checkout. $Unchanged"
    }
    if (-not $Python) { $Python = "python" }
}

# ---- 1. checks: nothing is made or changed before all of them pass
if (-not $BackupFolder) {
    Stop-Here "Name the folder for the backup key: -BackupFolder <a USB stick or other folder off this PC>. $Unchanged"
}
if (-not (Test-Path -LiteralPath $BackupFolder -PathType Container)) {
    Stop-Here "The backup folder $BackupFolder does not exist (is the USB stick plugged in?). $Unchanged"
}
$BackupFull = (Resolve-Path -LiteralPath $BackupFolder).ProviderPath.TrimEnd("\")
$RootFull = (Resolve-Path -LiteralPath $Root).ProviderPath.TrimEnd("\")
if (($BackupFull -ieq $RootFull) -or $BackupFull.StartsWith($RootFull + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
    Stop-Here "The backup folder $BackupFull is inside the repository: a private key there could be committed. Name a folder off this PC. $Unchanged"
}
$BackupFile = Join-Path $BackupFull $BackupName
if (Test-Path -LiteralPath $BackupFile) {
    Stop-Here ("$BackupFile already exists, and this script never overwrites or deletes a key file. $Unchanged " +
        "If it is the backup key of an earlier run, the keys already exist; to replace them see ""Lost key"" in OPERATOR.md.")
}
if (-not (Test-Path -LiteralPath $KeysFile -PathType Leaf)) {
    Stop-Here "$KeysFile is missing: run this from the Nightshift repository. $Unchanged"
}
$Utf8 = New-Object System.Text.UTF8Encoding $false
$KeysText = [System.IO.File]::ReadAllText($KeysFile, $Utf8)
$Block = [regex]::Match($KeysText, "(?s)(# BEGIN TRUSTED[^\n]*\n)(.*?)(# END TRUSTED)")
if (-not $Block.Success) {
    Stop-Here "$KeysFile has no BEGIN TRUSTED and END TRUSTED lines, so it is not the file this script writes. $Unchanged"
}
if ($Block.Groups[2].Value.Trim() -notmatch '^TRUSTED: dict\[str, str\] = \{\s*\}$') {
    Stop-Here "The release keys already exist ($KeysFile pins them); to replace them see ""Lost key"" in OPERATOR.md. $Unchanged"
}
if (-not (Test-Path -LiteralPath $SignScript -PathType Leaf)) {
    Stop-Here "$SignScript is missing: run this from the Nightshift repository. $Unchanged"
}
if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    Stop-Here "Python was not found ($Python). $Unchanged"
}
if (-not (Get-Command $GhExe -ErrorAction SilentlyContinue)) {
    Stop-Here "The GitHub command line (gh) was not found. Install it from https://cli.github.com, run 'gh auth login', then run this again. $Unchanged"
}
& $GhExe auth status | Out-Null
if ($LASTEXITCODE -ne 0) {
    Stop-Here "gh is not signed in to GitHub. Run 'gh auth login', then run this again. $Unchanged"
}

# ---- 2. two private keys, in memory only; their public halves from the shipped code
function New-Seed {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $text = [System.Convert]::ToBase64String($bytes)
    [System.Array]::Clear($bytes, 0, 32)
    return $text
}

$Primary = New-Seed
$Backup = New-Seed
# (the private key is the pipe's input, never an argument: arguments can be read by other programs)
$PrimaryPublic = [string]($Primary | & $Python $SignScript --public-key)
$CodeA = $LASTEXITCODE
$BackupPublic = [string]($Backup | & $Python $SignScript --public-key)
$CodeB = $LASTEXITCODE
$Shape = '^([0-9a-f]{16}) ([A-Za-z0-9+/]{43}=)$'
if (($CodeA -ne 0) -or ($CodeB -ne 0) -or ($PrimaryPublic.Trim() -notmatch $Shape) -or ($BackupPublic.Trim() -notmatch $Shape)) {
    $Primary = $null; $Backup = $null
    Stop-Here "Python could not work out the public keys (dev\sign_release.py --public-key failed; its own message is above). $Unchanged"
}
$PrimaryId, $PrimaryKey = $PrimaryPublic.Trim().Split(" ")
$BackupId, $BackupKey = $BackupPublic.Trim().Split(" ")
if (($Primary -eq $Backup) -or ($PrimaryId -eq $BackupId)) {
    $Primary = $null; $Backup = $null
    Stop-Here "The two keys came out the same, which must not happen. $Unchanged Run this again."
}

# ---- 3. the primary private key goes to the GitHub secret, on stdin
$Primary | & $GhExe secret set $SecretName --repo $Repo | Out-Null
$Code = $LASTEXITCODE
$Primary = $null
if ($Code -ne 0) {
    $Backup = $null
    Stop-Here ("gh could not set the secret $SecretName of $Repo (it ended with code $Code; its own message is above). " +
        "$Unchanged Check that this GitHub account may change the repository's secrets, then run this again.")
}

# ---- 4. the backup private key goes to the folder named, and nowhere else (never over an existing file)
$Day = (Get-Date).ToString("yyyy-MM-dd")
$BackupText = "# Nightshift release BACKUP key (Ed25519 private key, base64 of 32 bytes). SECRET: keep this file off the PC.`n" +
    "# Key id $BackupId, made $Day. It signs a release when the GitHub secret is lost (OPERATOR.md, 'Lost key').`n" +
    "$Backup`n"
$Backup = $null
$Wrote = $false
try {
    $stream = [System.IO.File]::Open($BackupFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
    try {
        $raw = [System.Text.Encoding]::ASCII.GetBytes($BackupText)
        $stream.Write($raw, 0, $raw.Length)
        $stream.Flush($true)
        $Wrote = $true
    } finally {
        $stream.Dispose()
    }
} catch {
    $Why = $_.Exception.Message
}
$BackupText = $null
if (-not $Wrote) {
    if (Test-Path -LiteralPath $BackupFile) { Remove-Item -LiteralPath $BackupFile -Force -ErrorAction SilentlyContinue }
    Stop-Here ("The backup key could not be written to $BackupFile ($Why). The GitHub secret $SecretName was set, but " +
        "to a key that no file and no version of Nightshift knows, so it must not be used: run this again with a " +
        "folder that can be written to (it makes new keys and replaces the secret). ns_common\release_keys.py was not changed.")
}

# ---- 5. the two public keys go into the checkout (public halves only)
$NewBlock = $Block.Groups[1].Value +
    "TRUSTED: dict[str, str] = {`n" +
    "    ""$PrimaryId"": ""$PrimaryKey"",  # primary, ${Day}`n" +
    "    ""$BackupId"": ""$BackupKey"",  # backup, ${Day}`n" +
    "}`n" +
    $Block.Groups[3].Value
$NewText = $KeysText.Substring(0, $Block.Index) + $NewBlock + $KeysText.Substring($Block.Index + $Block.Length)
$Tmp = "$KeysFile.new"
try {
    [System.IO.File]::WriteAllText($Tmp, $NewText, $Utf8)
    Move-Item -LiteralPath $Tmp -Destination $KeysFile -Force -ErrorAction Stop
} catch {
    $Why = $_.Exception.Message
    Remove-Item -LiteralPath $Tmp -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $BackupFile -Force -ErrorAction SilentlyContinue   # the file this run made, no other
    Stop-Here ("The public keys could not be written to $KeysFile ($Why). The backup file of this run was removed " +
        "again; the GitHub secret $SecretName was set to a key no version of Nightshift knows, so it must not be " +
        "used: close whatever has that file open and run this again (it makes new keys and replaces the secret).")
}

# ---- 6. what the owner reads
Write-Output "The release keys were made."
Write-Output "  primary key $PrimaryId : its private half is the GitHub secret $SecretName of $Repo (not on this PC)"
Write-Output "  backup key  $BackupId : its private half is $BackupFile"
Write-Output "Keep the backup file safe and off this PC; it is the only way to sign a release if the GitHub secret is lost."
$Drive = [System.IO.Path]::GetPathRoot($BackupFull)
try {
    if ((New-Object System.IO.DriveInfo $Drive).DriveType -eq [System.IO.DriveType]::Fixed) {
        Write-Output "Note: $BackupFull is on a disk of this PC. Move the file to a USB stick or another place off this PC, and delete it here."
    }
} catch { }
Write-Output "The two public keys are in $KeysFile (public halves only): commit that file. From the release that carries it, every update package must be signed."
exit 0
