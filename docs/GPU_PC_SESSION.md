# Nightshift on the GPU PC: the first real install and the first runs

Written for the candidate **v0.1.15-rc5** (a GitHub prerelease). A file written for a newer candidate replaces
this one: then follow that file, not this one.

The whole brief for a Claude Code session on the owner's GPU PC; read it to the end first. You drive a shell; the
owner does every click, every Windows prompt and every answer only a person can give.

## 0. If you are a NEW session picking this up

An earlier session may have done part of this. Run these three checks first.
```powershell
Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root 'Nightshift-trial\vars.ps1' } | Where-Object { Test-Path $_ }
```
Nothing printed: begin at section 1. A path printed: use it in every `Invoke-Expression` line of this file, then:
```powershell
Invoke-Expression (Get-Content -Raw 'D:\Nightshift-trial\vars.ps1')
Get-Content "$N\findings.md" -TotalCount 8; Get-Content "$H\logs\install-report.json" -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json | Select-Object outcome
"setup running: $(Test-Running "$N\setup-runs.txt")"
```
The third is 5.4's follow block (process alive, state file, the log's last line). Read all of `$N\findings.md`:

| What you see | Go on at |
|---|---|
| no `outcome`, no `$N\setup-runs.txt` | the first step of 3 or 4 not yet in the findings |
| no `outcome`, `setup running: True` | 4.6 (setup is still working) |
| no `outcome`, `setup running: False` | 4.5 once (setup resumes), then 4.6 |
| `outcome` is `failed` | 4.8 |
| `outcome` is `done`, no `$K\kit-state.json` | 5.1 |
| `kit running: True` | 5.4 |
| `kit running: False` | the row of section 6 for the last line's `exit_code` |

## 1. Who you are and what this is

You act for the owner, on their own PC, with them present. Nightshift (`AbdulrahmanAmer/nightshift`) is a local AI
video studio for Windows: at night it writes a story, narrates it, adds captions and stock footage, and renders a
long video plus Shorts into an Outbox; the owner posts them by hand. It never ran on this PC or with real models.
Goal: install v0.1.15-rc5, run the GPU kit (one real story video, a clip, two test voices, Nightshift Code, the
Assistant, two nights by themselves, a collection run), report what happened, and apply the developers' patch.

The one rule of reporting: report truthfully what happened, also when everything worked. Never write that a step
passed unless you saw its result yourself (a line, a file, an exit code). What you did not see is "not observed".

## 2. Hard rules on this PC

1. This is the owner's real machine. Write only into the work folder `$W` and the install folder `$H` (step 3.2).
   Delete nothing anywhere, also not inside those two.
2. Change no Windows setting, driver, power or sleep setting, scheduled task, PATH, saved environment variable or
   registry key yourself. When something must change (a driver, wake timers, disk space, Smart App Control, an
   antivirus block, the Visual C++ runtime), stop that step, tell the owner, record it, and go on only when the
   owner did it or said to go on without it. Nightshift changes its own nightly task through its own routes.
3. No keyboard or mouse automation, no clicking or typing in windows. A Windows permission prompt, a window that
   needs a click and every question only a person can answer belong to the owner. The kit opens a Nightshift Code
   window by itself in its step 8: leave it alone; the kit closes it.
4. Never start anything that would download the 22 GB of models again (an uninstall, a new install folder,
   removing `models`) without the owner's yes. Running setup again does not download a finished file again.
5. The stock-footage key is the owner's. Only the owner types or pastes it (step 4.4 or 5.1). You never read,
   print, copy, store or send it: you only check THAT one is stored (step 4.4 prints True or False; `ns doctor`
   prints "accepted the key" and never the key). Also never paste, print or upload any token or password, the
   Windows user name, the profile path (`C:\Users\<name>`) or the PC's name.
6. Do not edit Nightshift's installed files (`app`, `venvs`, `config`, `data`, `kit`) to make a step pass. A
   failure is a finding: record it and follow section 6.
7. Post nothing to YouTube. Upload nothing anywhere except to the GitHub issue of section 7.
8. Stop only what Nightshift started, and only by its own routes (`ns stop` in step 8.2, the kit's own end).
   Never `Stop-Process` or `taskkill`.
9. Setup and the kit run for hours in one process; your shell tool cuts a call off after minutes and may end what
   that call started. So they start DETACHED (`Start-Detached`, step 3.2: Windows' process service starts them, so
   they are no child of your shell), log to `$N`, and you record the pid and return. Then short checks, with your
   harness's own wait between them (one `Start-Sleep -Seconds 900`, or a background wait); never a printing loop.
   Begin every block with the `Invoke-Expression` line of step 3.2. All commands are Windows PowerShell 5.1.

## 3. Before installing

Do the steps in order; write every result into the findings (section 7). Tell the owner first: setup downloads
for up to a few hours; the kit runs 5 to 8 hours on day 1, two nights by themselves, 15 minutes on day 3.

3.1 Pick the drive.
```powershell
Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object DeviceID, @{n='FreeGB';e={[math]::Round($_.FreeSpace/1GB,1)}}, @{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}}
Get-PhysicalDisk | Select-Object FriendlyName, MediaType, BusType, @{n='SizeGB';e={[math]::Round($_.Size/1GB)}}
```
Acceptable: a local SSD or NVMe drive with 75 GB free, preferably not C: (setup's own need is about 64 GB). If
not: none has 75 GB: tell the owner what each has; they free space. Only a hard disk: setup warns NS-W106 (slow
model loading); go on only with the owner's yes.

3.2 Make the work folder and the variables file. Replace `D:` with the drive of 3.1 in all five places.
```powershell
New-Item -ItemType Directory -Force 'D:\Nightshift-trial\notes', 'D:\Nightshift-trial\download' | Out-Null
@'
$T = 'v0.1.15-rc5'; $V = $T.TrimStart('v'); $R = 'AbdulrahmanAmer/nightshift'
$W = 'D:\Nightshift-trial'; $N = "$W\notes"; $X = "$W\setup"; $H = 'D:\Nightshift'; $K = "$H\kit"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Start-Detached([string]$CommandLine, [string]$Dir) {
  $su = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0; EnvironmentVariables = [string[]](Get-ChildItem Env: | ForEach-Object { "$($_.Name)=$($_.Value)" }) }
  $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $CommandLine; CurrentDirectory = $Dir; ProcessStartupInformation = $su }
  if ($r.ReturnValue -ne 0) { "NOT STARTED (Win32_Process.Create returned $($r.ReturnValue))" } else { $r.ProcessId } }
function Test-Running([string]$File) { $l = @(Get-Content $File -ErrorAction SilentlyContinue)[-1]; if ($l -match '^(\d+) ') { $q = Get-Process -Id $Matches[1] -ErrorAction SilentlyContinue; [bool]($q -and $q.ProcessName -eq 'cmd') } else { $false } }
'@ | Set-Content -Encoding ASCII 'D:\Nightshift-trial\vars.ps1'
```
From here on, every block starts with this line (it is not repeated below):
```powershell
Invoke-Expression (Get-Content -Raw 'D:\Nightshift-trial\vars.ps1')
```
`$H` (setup creates it): plain letters, digits, `-` and `_` (NS-E117), not in Downloads or OneDrive, not `$X`
(NS-E115). `Start-Detached` starts a command hidden, outside your shell, with this shell's environment, and prints
its pid; `Test-Running` says whether the pid on the last line of a runs file is still that command.

3.3 Windows and memory.
```powershell
Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, OSArchitecture | Tee-Object -FilePath "$N\preflight.txt" -Append
[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1) | Tee-Object -FilePath "$N\preflight.txt" -Append
```
Acceptable: Windows 10 or 11, 64-bit; memory 16 or more (31 or more: the story model is Balanced, setup's own rule;
below: Small). If not: another Windows: stop and report (NS-E101). Below 16: stop and ask the owner.

3.4 Graphics card and driver.
```powershell
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used --format=csv,noheader | Tee-Object -FilePath "$N\preflight.txt" -Append
nvidia-smi | Select-String 'CUDA Version' | Tee-Object -FilePath "$N\preflight.txt" -Append
```
Acceptable: an RTX 5060 Ti with about 16 GB (`memory.total` at least 15000 MiB), driver 580 or newer, "CUDA
Version: 13.0" or higher (an RTX 50 card runs only the CUDA 13 build; setup refuses an older driver with NS-E103).
Write down `memory.used`: a program already holding video memory is a finding.
If not: `nvidia-smi` not found: stop and report (NS-E102). Older driver: stop; the owner installs the newest
driver from nvidia.com/drivers and restarts the PC; then do 3.4 again.

3.5 Power and sleep, read only.
```powershell
& { powercfg /getactivescheme; powercfg /a; powercfg /q SCHEME_CURRENT SUB_SLEEP RTCWAKE } | Tee-Object -FilePath "$N\power.txt"
```
Acceptable for the nights: "Allow wake timers" (RTCWAKE) has `Current AC Power Setting Index: 0x00000001`.
`0x00000000` or `0x00000002` means a sleeping PC will not wake at 03:00 (setup shows NS-W107). Then tell the owner
now: they set Sleep > Allow wake timers to Enable themselves, or keep the PC awake for the two nights. Record which.

3.6 Internet (any HTTP answer counts as reachable).
```powershell
foreach ($u in 'https://huggingface.co','https://github.com','https://download.pytorch.org','https://api.pexels.com','https://pixabay.com','https://pypi.org','https://files.pythonhosted.org','https://api.github.com') {
  try { $null = Invoke-WebRequest -Uri $u -Method Head -UseBasicParsing -TimeoutSec 10; "$u reachable" }
  catch { if ($_.Exception.Response) { "$u reachable (HTTP error)" } else { "$u NO ANSWER" } } }
```
Acceptable: all reachable. If not: `github.com`, `pypi.org`, `files.pythonhosted.org` or `huggingface.co` with
NO ANSWER: stop; ask the owner about a VPN, proxy or firewall, then repeat. Any other site: record it, go on.

3.7 An older Nightshift, and `gh`.
```powershell
[Environment]::GetEnvironmentVariable('NIGHTSHIFT_HOME','User'); Test-Path "$env:LOCALAPPDATA\Nightshift\installs.txt"
Get-ScheduledTask -TaskName 'Nightshift Daily' -ErrorAction SilentlyContinue | Select-Object TaskName, State
Test-Path $H; gh --version; gh auth status
```
Acceptable: nothing for `NIGHTSHIFT_HOME`, `False`, no task, `False`. Anything else: a Nightshift was installed
before: stop and ask the owner whether to install beside it (a new `$H`) or not at all; never uninstall it. `gh`
missing or not logged in is not a stop (section 7 says what then; do not install it; the owner may).

3.8 The Visual C++ runtime (setup asks Windows for permission to install it, and a prompt from a background
process shows only as a flashing taskbar button).
```powershell
$s = [Environment]::GetFolderPath('System'); 'msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll' | ForEach-Object { "$_ $(Test-Path (Join-Path $s $_))" }
```
Acceptable: three times `True`. If not: the owner downloads https://aka.ms/vs/17/release/vc_redist.x64.exe,
runs it and presses Yes themselves; then repeat 3.8.

## 4. The install (unattended, from an answers file)

4.1 Download the Setup zip and its checksum.
```powershell
gh release download $T -R $R -p "Nightshift-Setup-$V.zip" -p "Nightshift-Setup-$V.zip.sha256" -D "$W\download"
```
Without `gh`: `Invoke-WebRequest "https://github.com/$R/releases/download/$T/Nightshift-Setup-$V.zip" -OutFile
"$W\download\Nightshift-Setup-$V.zip" -UseBasicParsing`, and the same with `.sha256` added to both names.
If not: a 404 means the tag or file name is wrong: stop and report.

4.2 Check the hash, unblock, extract.
```powershell
$zip = "$W\download\Nightshift-Setup-$V.zip"
$want = ((Get-Content "$zip.sha256" -Raw).Trim() -split '\s+')[0].ToLower()
$got = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
if ($want -eq $got) { 'HASH OK' } else { "HASH MISMATCH: want $want got $got" }
Unblock-File -LiteralPath $zip; Expand-Archive -LiteralPath $zip -DestinationPath $X
Test-Path "$X\INSTALL.cmd"; Test-Path "$X\GPU-KIT.cmd"; Get-Content "$X\VERSION"
```
Expected: `HASH OK`, `True`, `True`, `0.1.15-rc5`. If not: a mismatch: download once more; a second mismatch:
stop and report both hashes. No `GPU-KIT.cmd`: stop and report (wrong candidate).

4.3 Write the answers file (setup's unattended route: with `-Answers` it runs without a window and asks nothing).
Ask the owner first for a channel name and topic; without a wish use the ones below.
```powershell
$tier = if ([math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1) -ge 31) { 'balanced' } else { 'small' }
[ordered]@{ home_path = $H; models_source = $null; models_source_kind = 'internet'; llm_tier = $tier; llm_custom = $null
  channel = [ordered]@{ display_name = 'After Dark Tales'; niche = 'horror fiction'; language = 'en'; seed_keywords = @('haunted house','night shift','small town') }
  voice = [ordered]@{ mode = 'preset'; speaker = 'Ryan'; description = $null; name = $null; speed = 1.0 }
  voice_ref_audio_path = $null; voice_ref_text = $null; voice_rights_confirmed = $false
  pexels_api_key = ''; pixabay_api_key = ''; masters_per_week = 1; run_time = '03:00'; nightly = $false
  install_code = $true; include_voicedesign = $true; allow_cpu = $false; restore_from = $null; owner_name = $null; ui_theme = 'dark'
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 "$W\answers.json"; "llm_tier $tier"
```
`nightly` false: the kit switches Automatic runs on for its nights and off again; `include_voicedesign` true
(about 2 GB): the kit's step 7 designs a voice; `install_code` true: its step 8.

4.4 The owner puts in their Pixabay key. From this moment until 4.7 confirms the keys were removed, never open,
print or copy `answers.json`.
```powershell
Start-Process notepad.exe "$W\answers.json"
```
Tell the owner: find the line `"pixabay_api_key":  ""`, paste your key between the two quotes, save, close
Notepad. (A Pexels key, if they have one, goes the same way into `pexels_api_key`.) Then check without seeing it:
```powershell
try { $j = Get-Content "$W\answers.json" -Raw | ConvertFrom-Json -ErrorAction Stop; "pixabay key filled in: $([bool]$j.pixabay_api_key)" } catch { 'answers.json is not valid JSON' }; Remove-Variable j -ErrorAction SilentlyContinue
```
Expected: `pixabay key filled in: True`. If not: `False`: the owner may instead add the key after setup in the
Dashboard (step 5.1); record which. Not valid JSON: the owner opens it again and fixes the quotes.

4.5 Start setup in the background (also the command to resume it after a stop, a closed session or a restart).
```powershell
$log = "$N\setup-$(Get-Date -Format yyyyMMdd-HHmm).log"
$id = Start-Detached ('cmd.exe /d /s /c ""{0}\INSTALL.cmd" -Answers "{1}\answers.json" > "{2}" 2>&1"' -f $X, $W, $log) $X
"$id $log" | Add-Content "$N\setup-runs.txt"; "setup pid $id, log $log"
```
With `-Answers`, INSTALL.cmd and setup read no input and never pause (the wizard runs `--non-interactive`).
Expected: a number as the pid, and within two minutes, in the log: "Nightshift setup - stage 1", "Using answers
from ... (unattended mode).", then the PC check's rows `[ OK ]`, `[WARN]` or `[FAIL]`, each non-OK row with its
code. If not: `NOT STARTED`: record it, stop and report. "Setup is already running on this PC": an earlier start
still works: 4.6. "Setup cannot continue until the red items are fixed": 4.8.

4.6 Follow it every 15 minutes. Balanced downloads about 22 GB of models and 3 GB of programs (about 35 minutes at
100 Mbit/s, 3 hours at 20 Mbit/s), then installs and tests a voice and a short video.
```powershell
$log = (@(Get-Content "$N\setup-runs.txt")[-1] -split ' ', 2)[1]; "setup running: $(Test-Running "$N\setup-runs.txt")"
'{0:N2} GB in models' -f ((Get-ChildItem "$H\models" -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1GB)
Get-Item $log | Select-Object LastWriteTime; Get-Content $log -Tail 8; Test-Path "$H\logs\install-report.json"
```
Expected: the GB number grows or the log changes (retries and waits for a busy site are said in the log). If not:
nothing changed for 30 minutes: record the last 30 log lines and the `.part` files under `$H\models` with their
sizes; stop and report.

4.7 Done when `install-report.json` exists.
```powershell
$r = Get-Content "$H\logs\install-report.json" -Raw | ConvertFrom-Json; $r.outcome; Get-Content "$H\app\VERSION"
Copy-Item "$H\logs\install-report.json" $N; Select-String -Path "$N\setup-2*.log" -Pattern 'Removed the API keys from', 'Setup stopped'
```
Expected: `done`, `0.1.15-rc5`, and (when a key was in the file) "Removed the API keys from answers.json (they
are in the Windows keyring now).": the file holds no key now. Record setup's total time, every `[WARN]` row with
its code, and the step times in the report. If not: `failed`, or "Setup stopped (exit code N)": 4.8.

4.8 When setup stops, look up its code's meaning and "what to do" (replace the code):
```powershell
$e = Get-Content "$X\contracts\errors.json" -Raw | ConvertFrom-Json; $e.'NS-E104'
```
- A download that failed (NS-E105, NS-E109), the PC restarted, or your session ended: 4.5 once (it resumes).
- A wrong stock key (NS-E502 Pixabay, NS-E501 Pexels), or exit code 11 (answers.json unreadable): the owner fixes
  the file (4.4), then 4.5.
- Disk full (NS-E104), antivirus or Smart App Control (NS-E113), a port in use (NS-E116), a driver (NS-E102,
  NS-E103): a Windows matter for the owner (rule 2.2). Report at once, wait for the owner, then 4.5 once.
- Any other code (NS-E705, NS-E706 included), or the same error after one retry: stop and report the code, its
  text, the last 30 log lines and what was tried.

## 5. First check and the GPU kit

5.1 `ns doctor` (full path: a shell opened before setup does not have `$H\cmd` on its PATH).
```powershell
& "$H\cmd\ns.cmd" doctor 2>&1 | Tee-Object -FilePath "$N\doctor-1.txt"; "exit $LASTEXITCODE"
```
Each row is `[ok]`, `[WARN]` or `[FAIL]` with its name and detail; exit 1 when any row is FAIL. Expected:
`[FAIL] supervisor: not running (opening the Dashboard starts it)` is normal while Nightshift is not running (it
alone makes the exit 1). `[ok] stock key pixabay: Pixabay accepted the key`. Every other row `[ok]` or `[WARN]`.
If not: another `[FAIL]`: copy it verbatim; one naming INSTALL.cmd's Repair: 4.5 once, then 5.1; still FAIL: stop
and report. `no key stored` or `NS-E502`: the owner opens the Dashboard shortcut, Settings > Stock footage keys,
pastes the key, saves, closes the Dashboard; then 5.1. `could not be checked now`: record; go on.

5.2 What the kit does, in its own order (13 steps; state `$K\kit-state.json`, one for every version; lock
`$K\kit.lock`; zip `$K\nightshift-gpu-kit-<date>.zip`). It sends nothing anywhere.

| # | Id | Step | Time (its bound) |
|---|---|---|---|
| 1 | K0 | start: refuses a wrong install | seconds |
| 2 | K1 | record this PC: nvidia-smi, disk, power, `ns doctor`, `ns verify --downloads` | up to 15 min (45) |
| 3 | K2 | `ns verify` (loads every model) and the proof scripts; only while nobody else uses Nightshift | 10 to 40 min (40 + 60) |
| 4 | KS | each stored stock key: one real search and one download, the clip removed again | a minute |
| 5 | K3 | one story video with the real models on its own channel "GPU kit (test video)" | about 3 h (3 x estimate + 1 h) |
| 6 | KC | one clip cut by Create > Clips from 3 minutes of that video | (90 min) |
| 7 | KV | a designed voice and one cloned from the video's narration, each says one sentence; both removed | (30 min) |
| 8 | KN | Nightshift Code in its own window, one prompt, window closed, card freed | (15 min load, 5 min prompt, then the idle unload) |
| 9 | KA | one question to the Assistant | (10 min) |
| 10 | K4 | Automatic runs on for the next two nights; the run ends with "Run GPU-KIT.cmd again after <time>" | seconds |
| 11, 12 | K5, K6 | night 1 and night 2 each make one more story video on the kit's channel, judged later | the night |
| 13 | K7 | collect into the zip; Automatic runs back off; the kit's channel paused | about 5 min |

How long `ns verify` takes with the real models is unknown until this PC: record step 3's time. Each step ends in
one line, `PASS`, `FAIL`, `WAIT` or `NOT RUN`, then `Step N of 13 (<name>): <what happened>`.
- Time guard: a step that needs the graphics card starts only when it can end 30 minutes before the next run time
  (03:00); else WAIT until the run time plus 6 hours. Start the first run by about 12:00 and day 1's steps fit.
- Entry rule: a step that needs the card is NOT RUN, nothing started or ended, when something the kit did not start
  holds it ("Nightshift Code is open: close it and run the kit again"); step 3 also while anyone uses Nightshift; an
  unfinished update: NOT RUN with the product's own code (NS-W210), message and hint.
- Exit rule: the kit ends only what it started, only while nothing else uses it, never by Stop All; it says
  "Nightshift was stopped again: ..." or "Nightshift was left running because ...". Record that line.
- It puts back its channel, Automatic runs (record `$H\run\gpu-kit-nights.json`), a test clip, two test voices and
  the Code window; it leaves its videos and clip in the Queue, the clip's input, one Assistant conversation.

5.3 Start the kit, unattended and detached. Ask the owner first: "May I start the GPU kit now? Please do not open
Nightshift Code or start a video until I say it has finished, and leave the PC on." Only on their yes:
```powershell
$log = "$N\kit-run-$(Get-Date -Format yyyyMMdd-HHmm).log"
$id = Start-Detached ('cmd.exe /d /s /c ""{0}\app\GPU-KIT.cmd" -Unattended -Home "{0}" > "{1}" 2>&1"' -f $H, $log) "$H\app"
"$id $log" | Add-Content "$N\kit-runs.txt"; "kit pid $id, log $log"
```
`-Unattended`: no input read, no key waited for, no pause, none of the owner's questions asked. Day 1 runs for
hours in this one process, far past your tool's command timeout, so it is detached (rule 9); you return at once.
Expected: a pid; within two minutes the log has "NIGHTSHIFT_HOME for this kit: <home> (given with -Home), version
0.1.15-rc5", the plan, `PASS    Step 1 of 13 (start): ...`. Every later run is the same command; it goes on from
the first step not done. If not: `NOT STARTED`: stop and report; the log already ends in JSON: 5.4.

5.4 Follow it every 15 minutes (every 30 during step 5); never type into anything of it:
```powershell
$log = (@(Get-Content "$N\kit-runs.txt")[-1] -split ' ', 2)[1]; "kit running: $(Test-Running "$N\kit-runs.txt")"
$s = Get-Content "$K\kit-state.json" -Raw | ConvertFrom-Json; "come back: $($s.come_back)  zip: $($s.zip)"
foreach ($k in 'K0','K1','K2','KS','K3','KC','KV','KN','KA','K4','K5','K6','K7') { $r = $s.steps.$k; if ($r) { '{0,-3} {1,-8} {2}' -f $k, $r.result, $r.line } else { "$k -        not reached" } }
$last = @(Get-Content $log -Tail 1)[0]; $last
```
While it runs, the last line is the kit's newest (step 5: "still making the video: ..." every minute). When
`kit running: False`, the run has ended and its last line is ONE JSON object:
```powershell
$j = $last | ConvertFrom-Json; $j | Select-Object state, exit_code, step, step_title, next_run_after, zip, reason, @{n='failed';e={$_.failed_steps -join ', '}}, @{n='questions';e={@($_.open_questions).Count}}
```
Act on `exit_code` and `state` by section 6's first table, on each step's line by its second. Above the JSON: the
exit rule's line, "The zip so far: ..." or "Send this one file: ...", and "For the owner:" with open questions.
If not: no JSON last line (the process was cut off, the PC restarted): 5.3 once; the kit continues.

5.5 The run after the nights. The first run ends after step 10: state `waiting`, `next_run_after` the second
morning; the nights need nothing from you. After that time, 5.3 again. Expected: "Night 1: run ... ran 03:00 to
...: made the kit's video, job ..., in ...", the same for night 2, step 13 with what it put back, "Send this one
file: <home>\kit\nightshift-gpu-kit-<date>.zip (<size>). ...", and the JSON with `step` `done`.

5.6 What you check yourself from files once step 5 says PASS:
```powershell
$st = Get-Content "$K\story\story.json" -Raw | ConvertFrom-Json; $st | Select-Object video, length_s, lufs, true_peak_dbtp, qc_passed, qc_reason
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User'); $env:NIGHTSHIFT_HOME = $H
Push-Location "$H\app"; $fp = & "$H\venvs\studio\Scripts\python.exe" -c "from ns_common import ffmpeg; print(ffmpeg.ffprobe_exe())"; Pop-Location
& $fp -v error -show_entries format=duration:stream=codec_type,codec_name,width,height,sample_rate,channels -of json $st.video
$sh = Get-Content "$K\story\sheet.txt"; $ti = ($sh -match '^TITLE: ') -replace '^TITLE: '; $tg = ($sh -match '^TAGS: ') -replace '^TAGS: '
"title $("$ti".Length) chars, tags line $("$tg".Length) chars, < or > in title: $("$ti" -match '[<>]')"; $sh -match 'Made for kids|Altered'
```
The python line asks the product's own ffprobe finder (PATH as Windows saved it, then `$H\bin\ffmpeg`). Expected:
one video stream 1920x1080 and one audio stream; duration equal to `length_s`; -14 LUFS within 1 and a true peak
of -1 dBTP or lower (the QC limits); a title of at most 100 characters without `<` or `>`; a tags line of at most
500; a made-for-kids and an altered-content line. Each miss is a finding with the number. If not: the finder
prints NS-E401 (no ffprobe): record "not observed" for stream, resolution and duration.

5.7 The questions only the owner can answer. An unattended run asks and answers none: each stays open, listed in
the log under "For the owner:" and in the JSON's `open_questions` (group, id, exact question, file to look at):
```powershell
($last | ConvertFrom-Json).open_questions | ForEach-Object { "[$($_.group)] $($_.question) Look at: $($_.look_at)" }
```
Pass them to the owner verbatim, numbered, in the chat and in your report (`<home>` for `$H` there). `paste` is
optional and the owner's alone (a Private upload; you never touch YouTube); `task_end` and `standby` need a
separate test install, not this trial: `later`. The owner answers them by double-clicking `<home>\app\GPU-KIT.cmd`
once (without `-Unattended`): `a` and Enter, then for each `y`, `n` and what they saw, or `later`; a key closes
the window. That attended run asks exactly the questions still open (the same list), but first goes on with any
step not done: only while `kit running: False` and the last state is `done`, or `waiting` at K5 outside 03:00 to
09:00. It rewrites the zip with the answers. Then record them:
```powershell
'watch','morning','paste' | ForEach-Object { "== $_"; Get-Content "$K\$_.json" -Raw -ErrorAction SilentlyContinue }
```
Write each answer into the findings as the owner gave it.

## 6. Judging

When a kit run has ended (5.4), its exit code decides the one next action:

| `exit_code` `state` | What you do |
|---|---|
| 0 `done` | every step is done, none failed: hand `open_questions` to the owner (5.7), then report 2 (7.4) |
| 1 `failed`, `step` is `done` | every step is done, some failed: the same as 0, with `failed_steps` and their lines in the report |
| 1 `failed`, `next_run_after` set | a step failed and the kit goes on later: report `failed_steps` with their lines now; 5.3 after `next_run_after` |
| 1 `failed`, `next_run_after` empty | report `failed_steps` with their lines and `reason`, and stop; start again only where the line table says so |
| 2 `refused` | nothing was started or changed: the line table's row for `reason`; otherwise report and stop |
| 3 `waiting` | 5.3 after `next_run_after` (after step 10: 5.5); report 1 and the owner's questions if not yet sent |
| 4 `locked` | another kit run (perhaps the owner's window) holds the lock: never start a second; 5.4 every 15 minutes until it has ended |
| 5 `blocked` | the line of `step` (state file) says what must end first (Nightshift Code open, a job, an unfinished update with its code and hint): the owner ends it, or it ends by itself; 5.3 after 30 minutes |
| 130 `interrupted` | 5.3 once |

The steps' own lines (the state file, the log):

| Line | What it means | What you do |
|---|---|---|
| REFUSED "... is not a GPU install" | the install did not take the graphics card | stop and report at once |
| REFUSED "The Nightshift of another install ... is running" | another install's engine runs | the owner closes that Dashboard and presses Stop All there; 5.3 |
| REFUSED "... kit-state.json is damaged ..." | the state cannot be read | stop and report; never run the kit again until the Head answers |
| NOT RUN or FAIL with a code (NS-W210 and others) | the product's own refusal | do what its hint says (the owner's hands where a click is needed); 5.3 once |
| Step 2 FAIL, the driver did not answer | blocks real use | stop and report at once |
| Step 2 FAIL, a model file is missing or damaged | maybe a broken download | 4.5 once, then 5.3; same FAIL: stop and report |
| Step 3 FAIL "a model did not answer its short test" | blocks real use | stop and report at once |
| Step 3 FAIL with "ns verify exit 0 ...; proofs: ...", or steps 6 to 9 FAIL | works but worse | record; the kit goes on |
| Step 4 FAIL, the key was not accepted | no video without footage | the owner pastes the key again in Settings (5.1); 5.3 |
| Step 4 FAIL, the rate limit / the site or connection | not the key | wait an hour; 5.3 |
| Step 4 NOT RUN, no key stored | step 5 will fail for lack of footage | 5.1 for the key first; record |
| Step 5 FAIL "the video stopped (<code>)" | blocks real use | report at once with the code; the kit goes on to the nights |
| Step 10 FAIL "Automatic runs could not be switched on" | blocks the nights | stop and report at once |
| Step 11 or 12 FAIL (did not run, past its limit, made nothing) | blocks real use | report after step 13 |
| Step 13 FAIL "Automatic runs could not be switched off" | the PC is left changed | tell the owner at once; they switch them off in Settings; 5.3 |
| "the kit stopped on an error it did not expect" | a kit bug | report at once with the zip |

"Blocks real use": setup cannot finish; no video; a night does not run, overruns or makes nothing; the video cannot
be found or played; the same code after its own retry. Everything else is recorded and does not stop you. Retry a
step at most once, and only where these tables or the message's own hint say so.

## 7. Reporting back

**The repository is public: anyone can read the issue and everything attached to it. Send only the kit's scrubbed
zip and your own findings file, each after the check of 7.3; never a setup log, a diagnostics zip or anything else.**

7.1 `$N\findings.md`, kept up to date, in exactly this form (`<home>` for `$H`, `<work>` for `$W`):

```text
# GPU PC first run: v0.1.15-rc5: <date>
Header: candidate v0.1.15-rc5; install <home> on drive <X:>; work <work>; issue #<n or none yet>; report <1|2|3>
## The three most important findings
1. ...
## Outcome so far
Installed: yes/no. One video made: yes/no/not yet. Night 1: ran/failed/not yet. Night 2: same.
## Environment
Windows <caption, version>; GPU <name>, driver <x>, CUDA <y>, VRAM <total>, in use before <MiB>;
RAM <GB>, story model <balanced/small>; drive <X:> <SSD/HDD>, free before <GB>; wake timers <Enable/Disable/Important
only>; sites not reachable <none/list>; gh <logged in/not>.
## Steps
| Step | Result | Time taken | Exact error text and code | What was tried |
| 3.x checks | ... | | | |
| 4 install | ... | | | |
| 5.1 ns doctor | ... | | | |
| kit 1 to 13 | one row each, the kit's own line | | | |
| Nightshift at the kit's end | the exit rule's line | | | |
## The video (from files, 5.6)
length, resolution, audio, LUFS, true peak, QC, title and tags length, sheet lines
## The owner's answers (5.7)
## What the owner did by hand
(keys, clicks, settings they changed themselves, prompts they answered)
## What I could not judge
(everything not observed, and why)
```

7.2 The channel is one GitHub issue on `AbdulrahmanAmer/nightshift`, titled `GPU PC first run: v0.1.15-rc5:
<date>`, with the findings as its body. The first report opens the issue; every later report is a comment on it.
```powershell
gh issue create -R $R --title "GPU PC first run: ${T}: $(Get-Date -Format yyyy-MM-dd)" --body-file "$N\findings.md"
gh issue comment <number> -R $R --body-file "$N\findings.md"
```
Write the issue number into the header. `gh` cannot attach a file: put the zip's path (`<home>\kit\...`) and size
in MB into the body; the owner drags that zip into a comment in the browser (if GitHub refuses it, record that).
`gh` missing or not logged in: copy `findings.md` and the zip into `$H\gpu-session-report\`; tell the owner both paths.

7.3 Before every report, send nothing that this check has not passed. Re-read `findings.md` yourself. Then:
```powershell
$s = Get-Content "$K\kit-state.json" -Raw | ConvertFrom-Json; $z = "$N\zipcheck-$(Get-Date -Format HHmmss)"
Expand-Archive -LiteralPath $s.zip -DestinationPath $z; Copy-Item "$N\findings.md" $z
$rx = @($env:USERNAME, $env:COMPUTERNAME, $env:USERPROFILE, $H) | Where-Object { $_ } | ForEach-Object { '(?<![A-Za-z0-9])' + [regex]::Escape($_) + '(?![A-Za-z0-9])' }
$rx += '\b\d{6,10}-[0-9a-f]{32}\b', '(?<![A-Za-z0-9])[A-Za-z0-9]{56}(?![A-Za-z0-9])', '\bhf_[A-Za-z0-9]{30,}', '\bgh[pousr]_[A-Za-z0-9]{30,}', '(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}'
Get-ChildItem $z -Recurse -File | Select-String -Pattern $rx -List | Select-Object -ExpandProperty Path
```
Expected: nothing printed (only paths, never the matching text; the kit masks keys, tokens, names and paths
already: this check is on top). If not: look at the match yourself, copy it nowhere. A real name, key or token:
fix `findings.md`, or report without the zip and name the unscrubbed file. An ordinary word equal to a short user
name: note it and send. Put the step lines of the zip's `README.txt` and `MISSING.txt` in the findings. No zip
yet: the first two lines become `$z = "$N\zipcheck-$(Get-Date -Format HHmmss)"; New-Item -ItemType Directory $z |
Out-Null; Copy-Item "$N\findings.md" $z`.

7.4 When to report:
- A blocker (section 6): at once, with what you have; do not wait for the rest.
- Report 1: after the first kit run ended, with the zip so far and the owner's numbered list of 5.7.
- Report 2: after 5.5 and 5.7 (step 13 done, the owner's answers in), with the final zip.
- After a patch was applied and the kit ran again (section 8): one more comment.
Report also when everything worked: step by step, with the observed lines.

## 8. After a patch

8.1 The Head answers on the issue with the fixed tag. Add it to the variables file (in place of `<FIXED>`):
```powershell
gh issue view <number> -R $R --comments
Add-Content -Encoding ASCII "$W\vars.ps1" '$F = ''<FIXED>''; $FV = $F.TrimStart(''v'')'
```
`ns update --github` and the Dashboard's update check take only a final release: apply a prerelease's Update zip.

8.2 Never update during a night (the run time plus six hours), while `kit running: True` or a video is made.
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$H\app\dev\kit\tools\nsproc.ps1" -NsHome $H
```
Expected: `CLEAN: no process of this install runs`. If not: the owner closes the Dashboard and any Nightshift Code
window; still listed, with no night, kit or video running: `& "$H\cmd\ns.cmd" stop` (its own route); repeat 8.2.

8.3 Download the Update zip with its checksum (the update refuses a zip without its `.sha256`, NS-E701), check
the hash as in 4.2 (with the Update zip's name), and apply it:
```powershell
gh release download $F -R $R -p "Nightshift-Update-$FV.zip" -p "Nightshift-Update-$FV.zip.sha256" -D "$W\download\$F"
& "$H\cmd\ns.cmd" update "$W\download\$F\Nightshift-Update-$FV.zip" --detach
```
Expected: "The update runs in the background; Nightshift will close. Progress: <log>" and a JSON line with
`follow` (`--wait` is refused from Nightshift's own `ns`). Then, in the background (it ends with the update):
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<the follow path from the JSON line>" *> "$N\update-$FV.log"; "exit $LASTEXITCODE"
Get-Content "$H\app\VERSION"; & "$H\cmd\ns.cmd" doctor
```
Expected: exit 0, the version `$FV`, no FAIL row but the supervisor row of 5.1. If not: record the last 30 lines
of the log the JSON names and the code (NS-E701 a bad or unchecked zip, NS-E705 another update runs, NS-E706 a
program still runs from Nightshift's environments: 8.2, then once more). Still failing: stop and report.

8.4 Re-run only what failed: 5.3. The kit keeps one state for every version: steps not done run on the new version,
done steps are not repeated ("going on from the kit of ..."). A FAIL recorded as done (steps 5 to 9 go on after
one) is never rerun and keeps every later run at exit 1: do not edit the state; report it and wait for the Head.

8.5 Roll back when the update made things worse (something that worked before now fails):
```powershell
& "$H\cmd\ns.cmd" update --rollback --detach
```
Then its `follow` script as in 8.3; `$H\app\VERSION` shows the previous version. Report what failed on the new
version and that you rolled back. If not: record the rollback's message and stop.

## 9. What you must never conclude

Do not call Nightshift "working", "ready", "broken" or "failed" overall, in the findings or to the owner. Give the
facts per step: what ran, what it printed, how long it took, what you could not see. The developers draw the
conclusion from those facts.
