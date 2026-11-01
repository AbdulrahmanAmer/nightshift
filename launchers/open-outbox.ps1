Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# "Nightshift Outbox" shortcut: open the folder with the finished videos.
. (Join-Path $PSScriptRoot '_common.ps1')

$outbox = Join-Path (Get-NsHome) 'outbox'
New-Item -ItemType Directory -Force -Path $outbox | Out-Null
Start-Process -FilePath 'explorer.exe' -ArgumentList $outbox
