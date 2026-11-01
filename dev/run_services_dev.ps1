# Starts llama-server, the embed server and the TTS server in three visible windows using config\runtime.json.
# Usage: .\dev\run_services_dev.ps1   (on a machine where the installer has already placed models and venvs)
# Not run on the authoring laptop (nothing executes there); for GPU / target machines only.
param([string]$NsHome = $(if ($env:NIGHTSHIFT_HOME) { $env:NIGHTSHIFT_HOME } else { "$env:LOCALAPPDATA\Nightshift" }))
$rt = Get-Content "$NsHome\config\runtime.json" -Raw | ConvertFrom-Json
$llama = "$NsHome\bin\llama\llama-server.exe"
$f = $rt.llama.flags
$llamaArgs = @("-m", $rt.llama.model_path, "--port", $rt.llama.port, "--host", "127.0.0.1", "-c", $f.ctx,
          "-ngl", $f.n_gpu_layers, "--alias", ($rt.llama.aliases -join ","), "-ctk", $f.kv_type, "-ctv", $f.kv_type)
if ($f.n_cpu_moe -gt 0) { $llamaArgs += @("--n-cpu-moe", $f.n_cpu_moe) }
if ($f.spec_type) { $llamaArgs += @("--spec-type", $f.spec_type, "--spec-draft-n-max", $f.spec_draft_n_max, "-np", "1") }
$ttsPython = "$NsHome\venvs\supervisor\Scripts\python.exe"
if (-not (Test-Path $ttsPython)) { $ttsPython = "$NsHome\venvs\tts\Scripts\python.exe" }
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$llama' $($llamaArgs -join ' ')"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$llama' -m '$($rt.embed.model_path)' --embeddings --port $($rt.embed.port) --host 127.0.0.1 -ngl 0"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '$ttsPython' -m ns_tts.server --port $($rt.tts.port)"
Write-Host "Started llama ($($rt.llama.port)), embed ($($rt.embed.port)), tts ($($rt.tts.port)). Close the windows to stop."
