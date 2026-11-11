# Nightshift on the GPU PC: the first real install and the first runs

This file is the whole brief for a Claude Code session on the owner's own GPU PC. Read it to the end before the
first command. The owner may read along: every step gives the command, what you should see, and what to do when
you do not see it.

The Head fills in one placeholder before handing this file over: `<CANDIDATE>`, the release candidate's tag (for
example `v0.1.15-rc5`). It must be a candidate whose Setup zip holds `GPU-KIT.cmd`. <!-- CHECK AFTER KIT MERGE -->

## 1. Who you are and what this is

You act for the owner of this PC, on their own PC, with them watching. Nightshift (repository
`AbdulrahmanAmer/nightshift`) is a local AI video studio for Windows: at night it writes an original story,
narrates it, adds captions and stock footage, and renders a long video plus Shorts into an Outbox; the owner posts
them to YouTube by hand. Until now it ran only with fake engines on a laptop with no graphics card; nothing of it
has ever run on this PC or with the real models.

Goal: install `<CANDIDATE>` the way an owner does, make one real story video with the real models and let two
nights run by themselves (the GPU kit does both). Then report what happened, so the developers can patch it and
you can apply the update.

The one rule of reporting: report truthfully what happened, also when everything worked. Never write that a step
passed unless you saw its result yourself (a line, a file, an exit code). What you did not see is "not observed".

## 2. Hard rules on this PC

1. This is the owner's real machine. Write only into two folders: the work folder `$W` you create and the install
   folder `$H` setup creates (step 3.2). Delete nothing anywhere, also not inside those two.
2. Change no Windows setting, driver, power plan, sleep or wake setting, scheduled task, PATH, environment
   variable or registry key yourself. When setup, `ns doctor` or the kit says something must change (a driver
   update, wake timers, long paths, free disk space, Smart App Control, an antivirus block), stop that step, tell the
   owner in plain words, write it in the findings, and go on only when the owner did it themselves or said to go on
   without it. Setup's own Windows prompts (the Visual C++ runtime) are the owner's to answer. The product changes
   its own nightly task through its own routes (setup, Settings, the kit): that is the product, not you.
3. No global keyboard or mouse automation and no clicking or typing in windows. Where a window needs a click or a
   typed answer, ask the owner to do it.
4. Setup downloads about 20 GB of models (section 4). Never start anything that would fetch them again (an
   uninstall, a new install folder, removing `models`) without telling the owner first and getting their yes.
   Running INSTALL.cmd again does not download a finished file again.
5. Never paste, print, write into your notes or upload a stock-footage key, any token or password, the Windows
   user name, the profile path (`C:\Users\<name>`) or the PC's name. The owner types the stock-footage key into
   Nightshift themselves; you never invent, read or share one. You send only the kit's zip (the kit scrubs it) and
   your own findings file, both checked as in step 7.3 first.
6. Do not edit Nightshift's installed files (`app`, `venvs`, `config`, `data`, `kit`) to make a step pass. A
   failure is a finding: record it and follow section 6.
7. Post nothing to YouTube. Upload nothing anywhere except to the GitHub issue of section 7.
8. Stop only what Nightshift started, and only by its own routes (`ns stop`, Stop All in step 8.2, the kit's own
   end). Never `Stop-Process` or `taskkill`.
9. Long work (setup, the kit, an update) runs in its own window or in the background; your shell calls are short
   checks of minutes at most. Each shell call starts fresh: begin every command block with the line of step 3.2.
   All commands are PowerShell (Windows PowerShell 5.1 is fine).

## 3. Before installing

Do the steps in order. Write every result into the findings (section 7); from step 3.2 on the commands also save
their output in the notes folder `$N`.

3.1 Pick the drive.
```powershell
Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object DeviceID, @{n='FreeGB';e={[math]::Round($_.FreeSpace/1GB,1)}}, @{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}}
Get-PhysicalDisk | Select-Object FriendlyName, MediaType, BusType, @{n='SizeGB';e={[math]::Round($_.Size/1GB)}}
```
Acceptable: a local SSD or NVMe drive with at least 75 GB free; prefer one that is not C:. Setup works out the exact
need itself (about 64 GB with the recommended Balanced model, 60 GB with Small: the models, the programs and a
38 GB reserve for videos) and the release page asks for about 75 GB, which also leaves room for the kit's videos.
If not: no drive has 75 GB free: stop, tell the owner what each drive has; the owner frees space themselves. Only a
hard disk (MediaType HDD): setup warns NS-W106 (slow model loading); tell the owner and go on only with their yes.

3.2 Make the work folder and the variables file. Replace `D:` with the drive of 3.1 in all four places.
```powershell
New-Item -ItemType Directory -Force 'D:\Nightshift-trial\notes', 'D:\Nightshift-trial\download' | Out-Null
@'
$T = '<CANDIDATE>'; $V = $T.TrimStart('v'); $R = 'AbdulrahmanAmer/nightshift'
$W = 'D:\Nightshift-trial'; $N = "$W\notes"; $X = "$W\setup"; $H = 'D:\Nightshift'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
'@ | Set-Content -Encoding ASCII 'D:\Nightshift-trial\vars.ps1'
```
From here on, every block starts with this line (it is not repeated below):
```powershell
Invoke-Expression (Get-Content -Raw 'D:\Nightshift-trial\vars.ps1')
```
`$H` is the install folder setup will create: a short path of plain letters, digits, `-` and `_` (NS-E117), not
in Downloads or OneDrive, not the setup folder `$X` (NS-E115). `Test-Path $H` must say `False`; if not, see 3.8.

3.3 Windows.
```powershell
Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, OSArchitecture | Tee-Object -FilePath "$N\preflight.txt" -Append
```
Acceptable: Windows 10 or 11, 64-bit. If not: stop and report (setup refuses with NS-E101).

3.4 Graphics card and driver.
```powershell
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used --format=csv,noheader | Tee-Object -FilePath "$N\preflight.txt" -Append
nvidia-smi | Select-String 'CUDA Version' | Tee-Object -FilePath "$N\preflight.txt" -Append
```
Acceptable: an RTX 5060 Ti with about 16 GB (`memory.total` at least 15000 MiB), driver 580 or newer, "CUDA
Version: 13.0" or higher (an RTX 50 card runs only the CUDA 13 build; setup refuses an older driver with NS-E103).
Write down `memory.used`: a program already holding video memory is a finding.
If not: `nvidia-smi` is not found: stop and report (NS-E102). The driver is older: stop; the owner installs the
newest driver from nvidia.com/drivers and restarts the PC themselves; then do 3.4 again.

3.5 Memory.
```powershell
[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1) | Tee-Object -FilePath "$N\preflight.txt" -Append
```
Acceptable: 32 GB or more (setup then preselects Balanced). From 16 to 32 GB setup preselects Small: record it and go
on. Below 16 GB setup warns: stop and ask the owner.

3.6 Power and sleep, read only (change nothing).
```powershell
& { powercfg /getactivescheme; powercfg /a; powercfg /q SCHEME_CURRENT SUB_SLEEP RTCWAKE; powercfg /q SCHEME_CURRENT SUB_SLEEP STANDBYIDLE } | Tee-Object -FilePath "$N\power.txt"
```
Acceptable for the nights: "Allow wake timers" (RTCWAKE) has `Current AC Power
Setting Index: 0x00000001` (Enable). `0x00000000` (Disable) or `0x00000002` (Important wake timers only) means a
sleeping PC will not wake at 03:00 (setup shows NS-W107). If so: tell the owner now. They either set Sleep > Allow
wake timers to Enable themselves, or keep the PC awake for the two nights. Record which.

3.7 Internet: the sites setup's own PC check asks (any HTTP answer counts as reachable).
```powershell
foreach ($u in 'https://huggingface.co','https://github.com','https://download.pytorch.org','https://api.pexels.com','https://pixabay.com','https://pypi.org','https://files.pythonhosted.org','https://api.github.com','https://api.nuget.org','https://download.visualstudio.microsoft.com') {
  try { $null = Invoke-WebRequest -Uri $u -Method Head -UseBasicParsing -TimeoutSec 10; "$u reachable" }
  catch { if ($_.Exception.Response) { "$u reachable (HTTP error)" } else { "$u NO ANSWER" } } }
```
Acceptable: all reachable. If `github.com`, `pypi.org` or `files.pythonhosted.org` gives NO ANSWER: stop (setup cannot
start without them); ask the owner about a VPN, proxy or firewall, then repeat. `huggingface.co` with no answer
means the models cannot download: stop as well. Any other site: record it and go on.

3.8 An older Nightshift, and the tools you need.
```powershell
[Environment]::GetEnvironmentVariable('NIGHTSHIFT_HOME','User'); Test-Path "$env:LOCALAPPDATA\Nightshift\installs.txt"
Get-ScheduledTask -TaskName 'Nightshift Daily' -ErrorAction SilentlyContinue | Select-Object TaskName, State
Test-Path $H; gh --version; gh auth status
```
Acceptable: no `NIGHTSHIFT_HOME`, no `installs.txt`, no task, `Test-Path $H` False. Anything else means a Nightshift
was installed before: stop and ask the owner whether to install beside it (a new `$H`) or not at all; never
uninstall it. `gh` missing or not logged in is not a stop: section 7 says what to do then (do not install it
yourself; the owner may).

## 4. The install

4.1 Download the Setup zip and its checksum.
```powershell
gh release download $T -R $R -p "Nightshift-Setup-$V.zip" -p "Nightshift-Setup-$V.zip.sha256" -D "$W\download"
```
Without `gh`: `Invoke-WebRequest "https://github.com/$R/releases/download/$T/Nightshift-Setup-$V.zip" -OutFile
"$W\download\Nightshift-Setup-$V.zip" -UseBasicParsing`, and the same with `.sha256` added to both names. The zip
is small (about 6 MB); the models come later. If not: a 404 means the tag or file name is wrong: stop and report.

4.2 Check the hash, unblock, extract.
```powershell
$zip = "$W\download\Nightshift-Setup-$V.zip"
$want = ((Get-Content "$zip.sha256" -Raw).Trim() -split '\s+')[0].ToLower()
$got = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
if ($want -eq $got) { 'HASH OK' } else { "HASH MISMATCH: want $want got $got" }
Unblock-File -LiteralPath $zip; Expand-Archive -LiteralPath $zip -DestinationPath $X
Test-Path "$X\INSTALL.cmd"; Test-Path "$X\GPU-KIT.cmd"; Get-Content "$X\VERSION"
```
Expected: `HASH OK`, `True`, `True`, and the version `$V`. If not: a mismatch: download once more; a second
mismatch: stop and report both hashes. No `GPU-KIT.cmd`: stop and report (wrong candidate).

4.3 Start setup the way an owner does. The owner may double-click `$X\INSTALL.cmd` instead; this is the same.
```powershell
Start-Process -FilePath "$X\INSTALL.cmd" -WorkingDirectory $X; Get-Date -Format s
```
<!-- UNVERIFIED: that Start-Process of a .cmd from the session's shell gives setup its own console exactly as a
double-click does was not run; if setup behaves differently, the owner double-clicks INSTALL.cmd instead -->


Expected: a console window titled "Nightshift setup" (keep it open: it does the work and is the log), then the setup
window. If Windows says "Windows protected your PC": the owner presses More info, then Run anyway. If no setup window
appears, the console prints an address `http://127.0.0.1:...`: the owner pastes it into Edge or Chrome.

4.4 What the setup window asks, and the answers for this trial. The owner clicks and types; you say what to choose.
- PC check: green and yellow rows are fine. Read each yellow or red row aloud from the owner; a red row stops
  setup: look its code up (4.7). The disk meter shows setup's exact need for the drive: record the number.
- Model files come from: The internet.
- Story model: the preselected one (Balanced with 32 GB of memory, else Small). Record which.
- Channel name and What it is about: the owner's own channel if they have one in mind; otherwise
  `After Dark Tales` and `horror fiction`. Your name: optional.
- Narrator: a preset voice (Ryan if the owner has no wish). Designed and cloned voices are not part of this trial.
- Night run: Run time 03:00. "Run Nightshift by itself every night": as the owner wants it after the trial; if
  unsure, leave it off: the kit switches it on for its two nights and off again afterwards.
  <!-- CHECK AFTER KIT MERGE -->
- Stock footage keys: the owner pastes their own Pixabay key into "Pixabay key (optional)" themselves and waits
  for "Works". Without a key no video can be made (NS-E502); the owner can also add it later in the Dashboard under
  Settings > Stock footage keys, but before step 5.3. Never type, read or repeat a key yourself.
- More options: Install folder `$H` exactly; Long videos per week 1; "Also install Nightshift Code" as preselected;
  "Also download the voice design model now" off.
- Then the owner presses Install.

4.5 How long, and slow versus stuck. Setup downloads about 20 GB of models with Balanced (language model 14.3 GB,
voice 4.4 GB, speech recognition 1.5 GB, embeddings 0.15 GB; about 15 GB with Small) plus about 3 GB of programs
and CUDA libraries, then installs, tunes for 1 to 3 minutes and plays a test voice and a short test video. At
100 Mbit/s the download takes about 30 minutes, at 20 Mbit/s about 2.5 hours. Check every 10 minutes:
```powershell
'{0:N2} GB in models' -f ((Get-ChildItem "$H\models" -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1GB)
Get-ChildItem "$H\models" -Recurse -Filter *.part -ErrorAction SilentlyContinue | Select-Object Name, @{n='GB';e={[math]::Round($_.Length/1GB,2)}}
Get-ChildItem "$H\logs\installer-*.jsonl" -ErrorAction SilentlyContinue | Get-Content -Tail 5
Get-Content "$env:LOCALAPPDATA\Nightshift\bootstrap\setup-ui.jsonl" -Tail 5 -ErrorAction SilentlyContinue
```
Slow: the GB number grows. Not stuck either: a file that gets no data for 60 seconds is retried up to three times,
and a site answering 5xx or 429 is waited for up to 10 minutes; the console says so. Stuck: the number has not
grown for 20 minutes and no log line is newer than 20 minutes. Then ask the owner to press Copy status in the setup
window and give you that one line; record it, the newest log lines and the `.part` sizes; stop and report.

4.6 Done. The setup window says "Nightshift is ready" and shows the next three steps; a file
`nightshift-diagnostics-<date>-<time>.zip` is on the Desktop (do not send it: the kit's zip holds a scrubbed copy).
```powershell
$r = Get-Content "$H\logs\install-report.json" -Raw | ConvertFrom-Json; $r.outcome
Copy-Item "$H\logs\install-report.json" $N; Get-Content "$H\app\VERSION"
```
Expected: `done` and the version `$V`. Record setup's total time, every yellow row and its code, and the step
times from the report. Ask the owner whether the test voice played and was clear, and whether the test video played.

4.7 When setup stops. Each message has a code. Look up its meaning and its "what to do":
```powershell
$e = Get-Content "$X\contracts\errors.json" -Raw | ConvertFrom-Json; $e.'NS-E104'
```
(replace the code). Then:
- A download that failed (NS-E105, NS-E109): the owner presses Retry once (or you run 4.3 again: it resumes, a
  finished file is not fetched again). The same error again: stop and report with the code, its text and the log
  lines.
- Disk full (NS-E104), antivirus or Smart App Control (NS-E113), a port in use (NS-E116), a driver (NS-E102,
  NS-E103): a Windows matter for the owner (rule 2.2). Report at once, wait for the owner, then Retry once.
- The setup window was closed: setup goes on in the console window; run 4.3 again to reopen the window.
- The console "Nightshift setup" was closed, or the PC restarted: run 4.3 again; setup carries on where it stopped.
  If it stopped during the Python environments step and now refuses with NS-E705 or NS-E706, record the message and
  report.
- Any other red code, or the same error after one retry: the owner presses Save a help file (the zip goes to the
  Desktop); stop and report the code, its text and what was tried. Do not send that zip (rule 2.5).

## 5. First checks and the GPU kit

5.1 `ns doctor`. Setup put the `ns` command in `$H\cmd`; a shell opened before setup does not have it on PATH,
so use the full path.
```powershell
& "$H\cmd\ns.cmd" doctor 2>&1 | Tee-Object -FilePath "$N\doctor-1.txt"; "exit $LASTEXITCODE"
```
Each row is `[ok]`, `[WARN]` or `[FAIL]` with its name and detail; exit 0 when no row is FAIL. `[FAIL]` rows block:
copy each one verbatim into the findings. A FAIL that names INSTALL.cmd's Repair: run 4.3 again once, then 5.1 again;
still FAIL: stop and report. `[WARN]` rows: record them and go on.

5.2 `ns verify`: it starts the engine, loads every model once and times it (the kit allows it 40 minutes).
<!-- UNVERIFIED: how long ns verify takes with the real models was never measured -->
Run it in the background if your shell call would time out.
```powershell
& "$H\cmd\ns.cmd" verify 2>&1 | Tee-Object -FilePath "$N\verify-1.txt"; "exit $LASTEXITCODE"
```
Each check is `[PASS]`, `[WARN]` or `[FAIL]` with its value and limit; the last line is "Result: ALL CHECKS PASSED" or
"Result: SOME CHECKS FAILED"; the report is `$H\logs\verify-report.json`. A FAIL is a blocker (the models do not run
on this card): stop and report every FAIL line with the line under it. A WARN (a voice did not speak) is a finding:
record it; go on.

5.3 Before the kit. Ask the owner to open the Nightshift Dashboard shortcut, go to Settings > Stock footage keys and
confirm that a Pixabay key is saved, then to close the Dashboard window. No key: they add it there (never you).

5.4 What the kit does. `GPU-KIT.cmd` runs eight steps and keeps its state in `$H\kit\kit-state.json`; it sends
nothing anywhere and changes only its own paused channel "GPU kit (test video)" and Automatic runs for two nights.
<!-- CHECK AFTER KIT MERGE -->
1. start (seconds); 2. record this PC (up to 15 minutes); 3. test each model once and run the proof scripts (10 to
   40 minutes); 4. make one story video with the real models (about 3 hours); 5. switch Automatic runs on for the
   next two nights, after which the kit stops; 6. and 7. night 1 and night 2 run by themselves, each making one
   full story video on the kit's channel besides what the owner's channels have due; 8. on a run after the second
   night: collect everything into one zip and put Automatic runs back. <!-- CHECK AFTER KIT MERGE -->
Step 4 starts only when the video can end 30 minutes before the next run time (03:00): start the kit before about
21:00, or it says WAIT with the day and time to run it again. The whole trial takes about three days.

5.5 Start the kit in its own window, so it survives the end of your session.
```powershell
Start-Process -FilePath "$H\app\GPU-KIT.cmd" -ArgumentList "-Home `"$H`"" -WorkingDirectory "$H\app"
```
Expected: a window "Nightshift GPU kit" that names the folder (`NIGHTSHIFT_HOME for this kit: ...`), lists the
steps and asks "Press Enter to start". The owner presses Enter (with no answer it starts by itself after 15
minutes). <!-- CHECK AFTER KIT MERGE --> A line starting with REFUSED means nothing was changed: record it and
see section 6.

5.6 Follow it from the files (every 10 to 15 minutes; never type into its window):
```powershell
$s = Get-Content "$H\kit\kit-state.json" -Raw | ConvertFrom-Json
$s.steps.PSObject.Properties | ForEach-Object { '{0,-3} {1,-8} {2}' -f $_.Name, $_.Value.result, $_.Value.line }
"running: $(Test-Path "$H\kit\kit.lock")  come back: $($s.come_back)  zip: $($s.zip)"
Get-Content "$H\kit\story\studio-run.log" -Tail 10 -ErrorAction SilentlyContinue
```
Each step ends in one result: PASS, FAIL, WAIT (run the kit again at the time its line names) or NOT RUN (a step
before it failed). `running: False` with a step still open means the kit window has ended: look at its last lines
with the owner. <!-- CHECK AFTER KIT MERGE -->

5.7 The questions only a person can answer. After step 4 the kit window asks whether to answer its open questions
now (type `a`). All of them are the owner's to type, in the kit window:
- the video (3 questions: picture, narration, captions): the owner opens the video and answers;
- the paste into YouTube Studio (5 questions and 5 extra checks, `$H\app\dev\kit\K8_PASTE_CHECK.md`): optional, the
  owner's own decision and done by the owner only, as a Private upload; you never touch YouTube;
- hand checks 1 and 2 (`night-task-end.txt`): they need a separate test install and a changed task limit; they are
  not part of this trial: the owner answers `later`;
- the morning note (2 questions, after the nights): the owner looks at the Queue page.
`later` or no answer keeps a question open for the next run. <!-- CHECK AFTER KIT MERGE -->

5.8 What you check yourself from files, once step 4 says PASS, for the findings:
```powershell
$st = Get-Content "$H\kit\story\story.json" -Raw | ConvertFrom-Json; $st | Select-Object video, length_s, lufs, true_peak_dbtp, qc_passed, qc_reason
$dirs = ([Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')).Split(';') + "$H\bin\ffmpeg"
$fp = $dirs | Where-Object { $_ } | ForEach-Object { Join-Path $_ 'ffprobe.exe' } | Where-Object { Test-Path $_ } | Select-Object -First 1
& $fp -v error -show_entries format=duration:stream=codec_type,codec_name,width,height,sample_rate,channels -of json $st.video
$sh = Get-Content "$H\kit\story\sheet.txt"; $ti = ($sh -match '^TITLE: ') -replace '^TITLE: '; $tg = ($sh -match '^TAGS: ') -replace '^TAGS: '
"title $("$ti".Length) chars, tags line $("$tg".Length) chars, < or > in title: $("$ti" -match '[<>]')"; $sh -match 'Made for kids|Altered|synthetic'
```
Expected: one video stream 1920x1080 and one audio stream; a duration equal to `length_s` and about 10 minutes or
more; loudness -14 LUFS within 1 and a true peak of -1 dBTP or lower (the product's own QC limits); a title of 100
characters or fewer without `<` or `>`; a tags line of 500 characters or fewer; a made-for-kids line and an
altered-content line. Each miss is a finding with the number.
<!-- UNVERIFIED: the product finds ffprobe on PATH or in $H\bin\ffmpeg (ns_common/ffmpeg.py); that a winget
FFmpeg puts ffprobe.exe in a folder named on the saved PATH was not checked -->
The block reads the PATH Windows has saved, because a shell started before setup does not see setup's changes.
If `ffprobe` is in neither place, ask the owner to open the video and tell you its length; record "not observed".

5.9 If your session ends in the middle. The kit window and the nights do not depend on you. A new session: reads
this file, then `$N\findings.md` (its header holds `$H`, `$W`, the candidate and the issue number), runs 5.6, and
goes on from the first open step: kit running: keep following it; WAIT: start the kit (5.5) after the `come back`
time; done up to step 8: go to section 7.

## 6. Judging

Every kit step line, and every setup or check result, leads to one of three things.

| Result | What it means | What you do |
|---|---|---|
| REFUSED, not a GPU install | the install did not take the graphics card | stop and report at once |
| REFUSED, another install's Nightshift runs | the kit did not start | the owner closes that Dashboard and presses Stop All there; start the kit again |
| REFUSED, another GPU kit is running | a kit window is still open | wait for it (5.6) |
| Step 2 FAIL, the driver did not answer | blocks real use | stop and report at once |
| Step 2 FAIL, a model file is missing or damaged | may be a broken download | run 4.3 again once, then the kit again; same FAIL: stop and report |
| Step 3 FAIL, a model did not answer its test | blocks real use | stop and report at once |
| Step 3 FAIL, proofs only | works, a check failed | record; the kit goes on by itself |
| Step 4 PASS | one real video was made | go on; report (7.4) |
| Step 4 WAIT | time guard, or a night is running | run the kit again at the time in its line |
| Step 4 FAIL, the video stopped (a code) | blocks real use | report at once with the code; the kit goes on with the nights by itself <!-- CHECK AFTER KIT MERGE --> |
| Step 5 FAIL, Automatic runs could not be switched on | blocks the nights | stop and report at once |
| Step 6 or 7 FAIL, did not run / ran past its limit | blocks real use | report after step 8 (or at once when both failed) |
| Step 6 or 7 PASS | the night ran inside its limit | go on |
| Step 8 FAIL, Automatic runs not put back | the PC is left changed | tell the owner at once; report |
| Any NOT RUN | waits for a failed step | nothing; the failed step decides |

"Blocks real use" means any of: setup cannot finish; no video can be made; a night does not run, runs past the
task's limit or makes nothing; the owner cannot find or play the video in the Dashboard's Queue or the Outbox; an
error with no way out (the same code after the retry its own text gives). Everything else that works is "works but
worse than it should": slow steps, quality notes, warnings, a proof that failed, wording that misled the owner.
Write those in the findings too; they do not stop you. Retry a step at most once, and only where this table or the
message's own hint says so.

## 7. Reporting back

7.1 The findings file is `$N\findings.md`, kept up to date as you go, in exactly this form. Paths are written with
`<home>` for `$H` and `<work>` for `$W`.

```text
# GPU PC first run: <CANDIDATE>: <date>
Header: candidate <tag>; install <home> on drive <X:>; work <work>; issue #<n or none yet>; report <1|2|3>
## The three most important findings
1. ...
## Outcome so far
Installed: yes/no. One video made: yes/no/not yet. Night 1: ran/failed/not yet. Night 2: same.
## Environment
Windows <caption, version>; GPU <name>, driver <x>, CUDA <y>, VRAM <total>, in use before <MiB>;
RAM <GB>; drive <X:> <SSD/HDD>, free before <GB>, setup's need <GB>; wake timers <Enable/Disable/Important only>;
modern standby <yes/no>; sites not reachable <none/list>; gh <logged in/not>.
## Steps
| Step | Result | Time taken | Exact error text and code | What was tried |
| 3.x checks | ... | | | |
| 4 install | ... | | | |
| 5.1 ns doctor | ... | | | |
| 5.2 ns verify | ... | | | |
| kit 1 to 8 | one row each, the kit's own line | | | |
## The video (from files, 5.8)
length, resolution, audio, LUFS, true peak, QC, title and tags length, sheet lines
## What the owner did by hand
(keys, clicks, settings they changed themselves, answers they typed)
## What I could not judge
(everything not observed, and why)
```

7.2 The channel is a GitHub issue on `AbdulrahmanAmer/nightshift`, titled `GPU PC first run: <CANDIDATE>: <date>`,
with the findings as its body. The repository is public: anyone can read the issue and its attachments. The first
report opens the issue; every later report is a comment on it.
```powershell
gh issue create -R $R --title "GPU PC first run: ${T}: $(Get-Date -Format yyyy-MM-dd)" --body-file "$N\findings.md"
gh issue comment <number> -R $R --body-file "$N\findings.md"
```
Write the issue number into the findings header. `gh` cannot attach a file: state the kit zip's name, size and path
(as `<home>\kit\...`) in the body, and ask the owner to drag the zip into a comment on the issue page in the browser
(GitHub takes a zip up to 25 MB). <!-- UNVERIFIED: the 25 MB attachment limit is GitHub's documented limit, not in
this repository --> If `gh` is missing or not logged in: copy `findings.md` and the kit zip into
`$H\gpu-session-report\` and tell the owner where they are.

7.3 Before every report: send nothing that 7.3 has not passed. Re-read `findings.md` yourself. Then:
```powershell
$s = Get-Content "$H\kit\kit-state.json" -Raw | ConvertFrom-Json; $z = "$N\zipcheck-$(Get-Date -Format HHmmss)"
Expand-Archive -LiteralPath $s.zip -DestinationPath $z; Copy-Item "$N\findings.md" $z
$rx = @($env:USERNAME, $env:COMPUTERNAME, $env:USERPROFILE) | Where-Object { $_ } | ForEach-Object { '(?<![A-Za-z0-9])' + [regex]::Escape($_) + '(?![A-Za-z0-9])' }
$rx += '\b\d{6,10}-[0-9a-f]{32}\b', '\bhf_[A-Za-z0-9]{30,}', '\bgh[pousr]_[A-Za-z0-9]{30,}', '(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}'
Get-ChildItem $z -Recurse -File | Select-String -Pattern $rx -List | Select-Object -ExpandProperty Path
```
Expected: nothing printed (print paths only, never the matching text). A path printed: look at the match in that
file yourself without copying it anywhere. A real name, key or token: do not send; fix `findings.md` if it is
yours; if it is inside the kit zip, report without the zip and say which file was not scrubbed (its name only). An
ordinary word that only equals a short user name: note that and send. Also read the zip's `README.txt` and
`MISSING.txt` and put their step lines in the findings. No kit zip yet (a blocker before the kit ran): replace the
first two lines with `$z = "$N\zipcheck-$(Get-Date -Format HHmmss)"; New-Item -ItemType Directory $z | Out-Null;
Copy-Item "$N\findings.md" $z` and run the other three.

7.4 When to report:
- A blocker (section 6): at once, with what you have; do not wait for the rest.
- Otherwise after the first video (step 4 ended, the kit stopped after step 5): report 1.
- After the collection run (step 8 done, "Send this one file: ...zip"): report 2, with the zip.
- After a patch was applied and the kit ran again (section 8): one more comment.
Every report also when everything worked: say so step by step, with the observed lines.

## 8. After a patch

8.1 The Head answers on the issue with the fixed candidate's tag. Read it, then add it to the variables file (put
the tag in place of `<FIXED>`):
```powershell
gh issue view <number> -R $R --comments
Add-Content -Encoding ASCII "$W\vars.ps1" '$F = ''<FIXED>''; $FV = $F.TrimStart(''v'')'
```
A prerelease is never offered by `ns update --github` or the Dashboard's update check: you install it by hand from
its Update zip, as below.

8.2 Make sure nothing runs that the update would cut. Do not update during a night (from the run time until six
hours later) or while the kit makes a video.
```powershell
Test-Path "$H\kit\kit.lock"
powershell -NoProfile -ExecutionPolicy Bypass -File "$H\app\dev\kit\tools\nsproc.ps1" -NsHome $H
```
Expected: `False` and `CLEAN: no process of this install runs`. Otherwise: ask the owner to close the Dashboard and
any Nightshift Code or Studio window; if Nightshift processes are still listed and no night or video is running, run
the product's own Stop All with the same command plus `-Down`. <!-- CHECK AFTER KIT MERGE -->

8.3 Download the Update zip with its checksum (both are needed: the update refuses a zip without its `.sha256`),
check the hash as in 4.2, and apply it:
```powershell
gh release download $F -R $R -p "Nightshift-Update-$FV.zip" -p "Nightshift-Update-$FV.zip.sha256" -D "$W\download\$F"
& "$H\cmd\ns.cmd" update "$W\download\$F\Nightshift-Update-$FV.zip" --detach
```
Expected: "The update runs in the background; Nightshift will close. Progress: <log>" and a JSON line with `follow`.
Then (in the background, it ends when the update ends):
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<the follow path from the JSON line>"; "exit $LASTEXITCODE"
Get-Content "$H\app\VERSION"; & "$H\cmd\ns.cmd" doctor
```
Expected: exit 0, the version `$FV`, no FAIL row. If not: record the last 30 lines of the log the JSON names and the
message with its code (NS-E701 a bad or unchecked zip, NS-E705 another update runs, NS-E706 a program still runs
from Nightshift's environments: close it and try once more). Still failing: the alternative route is the new
Setup zip (4.1 to 4.3 with `$F` and `$FV` in place of `$T` and `$V`, extracted to a new folder `$W\setup-$FV`; the
window finds the install and offers the update with Continue, keeping models, settings and videos). Then stop and
report.

8.4 Run the kit again (5.5). It keeps one state for every version and goes on from where it was: steps not done
are run again on the new version, steps already done are not repeated. If the patch is for a step the kit
recorded as done (a FAIL that let the kit go on), do not edit the kit's state: say so in the report and wait for
the Head's instruction. <!-- CHECK AFTER KIT MERGE --> Report again (7.4).

8.5 Roll back when the update made things worse (something that worked before now fails):
```powershell
& "$H\cmd\ns.cmd" update --rollback --detach
```
Then run its `follow` script as in 8.3 and check `$H\app\VERSION` shows the previous version. Report what failed
on the new version and that you rolled back. If the rollback fails, record its message and stop.

## 9. What you must never conclude

Do not call Nightshift "working", "ready", "broken" or "failed" overall, in the findings or to the owner. Give the
facts per step: what ran, what it printed, how long it took, what you could not see. The developers draw the
conclusion from those facts.
