# Starts llama-server, the embed server and the TTS server in three visible windows using config\runtime.json.
# Usage: .\dev\run_services_dev.ps1   (on a machine where the installer has already placed models and venvs)
# Not run on the authoring laptop (nothing executes there); for GPU / target machines only.
param([string]$NsHome = $(if ($env:NIGHTSHIFT_HOME) { $env:NIGHTSHIFT_HOME } else { "$env:LOCALAPPDATA\Nightshift" }))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# The one way a value becomes a single-quoted string in the command a new window runs. PowerShell ends such a
# string at U+0027 and also at U+2018, U+2019, U+201A and U+201B (a path with one of them would end it early),
# and reads two in a row as one character; so each is written twice. The Python twin is ns_common/psquote.py
# (tests/test_ps_quoting.py runs this one against the same values).
function ConvertTo-NsPsLiteral {
    param([string]$Text)
    return "'" + [regex]::Replace($Text, '[\u0027\u2018\u2019\u201A\u201B]', '$0$0') + "'"
}
$rt = Get-Content "$NsHome\config\runtime.json" -Raw | ConvertFrom-Json
$llama = "$NsHome\bin\llama\llama-server.exe"
$f = $rt.llama.flags
$llamaArgs = @("-m", $rt.llama.model_path, "--port", $rt.llama.port, "--host", "127.0.0.1", "-c", $f.ctx,
          "-ngl", $f.n_gpu_layers, "--alias", ($rt.llama.aliases -join ","), "-ctk", $f.kv_type, "-ctv", $f.kv_type)
if ($f.n_cpu_moe -gt 0) { $llamaArgs += @("--n-cpu-moe", $f.n_cpu_moe) }
if ($f.spec_type) { $llamaArgs += @("--spec-type", $f.spec_type, "--spec-draft-n-max", $f.spec_draft_n_max, "-np", "1") }
$ttsPython = "$NsHome\venvs\supervisor\Scripts\python.exe"
if (-not (Test-Path $ttsPython)) { $ttsPython = "$NsHome\venvs\tts\Scripts\python.exe" }
function ConvertTo-NsCommand {
    param([string]$Program, [object[]]$Arguments)
    $parts = @(ConvertTo-NsPsLiteral $Program) + @($Arguments | ForEach-Object { ConvertTo-NsPsLiteral ([string]$_) })
    return '& ' + ($parts -join ' ')
}
$embedArgs = @("-m", $rt.embed.model_path, "--embeddings", "--port", $rt.embed.port, "--host", "127.0.0.1", "-ngl", 0)
$ttsArgs = @("-m", "ns_tts.server", "--port", $rt.tts.port)
Start-Process powershell -ArgumentList "-NoExit", "-Command", (ConvertTo-NsCommand $llama $llamaArgs)
Start-Process powershell -ArgumentList "-NoExit", "-Command", (ConvertTo-NsCommand $llama $embedArgs)
Start-Process powershell -ArgumentList "-NoExit", "-Command", (ConvertTo-NsCommand $ttsPython $ttsArgs)
Write-Host "Started llama ($($rt.llama.port)), embed ($($rt.embed.port)), tts ($($rt.tts.port)). Close the windows to stop."
