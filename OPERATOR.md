# Nightshift: operator guide

One page. You run the PC with the graphics card. You do not need to know how Nightshift works.
Keep this file next to `INSTALL.cmd`.

## What you need

- A Windows 10 or 11 PC with an NVIDIA RTX graphics card (16 GB) and 32 GB of memory for the recommended
  Balanced model. With less memory setup picks the smaller Small model for you.
- About 64 GB of free disk space for the recommended Balanced model on a first install (60 GB with Small, 72 GB
  with Best). The setup window says the exact number for your PC and your choices, and what it is made of; it
  asks for less when the models are already on the drive.
- An internet connection: setup downloads about 20 GB with Balanced (about 15 GB with Small), or use a
  `models` folder you were given on Drive.
- Optional: a free stock footage key from Pixabay (or Pexels, if you already have one). One is recommended, but
  setup finishes without any and you can add it later in Settings. Setup shows you where to get one.
  If you were given an `answers.json` file it may already be filled in.

## Install (about 1 hour, mostly waiting)

1. Download `Nightshift-Setup-<version>.zip` from the Google Drive folder (on GitHub Releases it is the file marked "Download this"; releases up to 0.1.1 call it `Nightshift-<version>.zip`). If there is also a `models` folder and an
   `answers.json`, download those too.
2. Right-click the zip, choose **Properties**, tick **Unblock** if you see it, and press OK.
   (Windows marks downloaded files; unblocking stops warning pop-ups later.)
3. Right-click the zip, **Extract All**, and pick **any folder** (the default next to the zip is fine, or a short one
   such as `C:\Users\<you>\Nightshift-Setup`). Setup copies the program into an `app` folder inside the install
   folder it asks for later, so **you can delete the extracted folder after setup has finished**. The install folder
   must be a different folder (the window tells you if it is not) and should not be in Downloads or OneDrive.
4. Optional, for an unattended install: put `answers.json` in the extracted folder, next to `INSTALL.cmd`. With it
   setup runs in the Nightshift setup window with text only: there is no setup window, so steps 6 to 8 do not apply
   (that text window is the log and prints the result). To get the window with your answers filled in, start it from a command
   prompt as `INSTALL.cmd -Ui`. Without `answers.json`, skip this step: the setup window asks everything.
5. Double-click **INSTALL.cmd**. If Windows says "Windows protected your PC", press **More info**, then
   **Run anyway**. Windows may ask for permission to install programs: say **Yes**.
6. A window called **Nightshift setup**, with text and a small crescent, appears first. It looks at your PC, gets
   Python, and then opens the setup window. Keep the window called Nightshift setup open: it does the work and is
   the log. If no setup window appears, that window prints an address (`http://127.0.0.1:...`): paste it into Edge
   or Chrome.
7. In the setup window: read the PC check (green and yellow rows are fine, red rows say what to do), choose
   your channel, narrator, model and night run time (Balanced is
   preselected; on a PC with less than 32 GB of memory Small is, and the window says why), paste a stock footage key if you have one (optional), and press **Install**.
   Setup then downloads the AI models (you see every file, the speed and the time left; **Pause** and **Resume**
   work), installs, tunes itself for 1 to 3 minutes and plays you a test voice and a short test video. On a PC that
   runs on the processor (no NVIDIA card: the engine test) the tune keeps the context small, 8192 tokens, or 16384
   when Nightshift Code is installed, so the jobs keep their memory; an install tuned by an earlier version keeps
   its settings until `ns tune` is run again.
8. At the end the window says **Nightshift is ready**, shows the **next three steps** (open the dashboard, press
   **Create it now** once while nothing else uses the graphics card, read tomorrow morning's note and check the
   schedule) and names a **help file** (a zip) on your Desktop. Send it back (see below). **Open dashboard** starts
   the dashboard. If no stock footage key is stored, the window reminds you to add a Pixabay key in Settings before the
   first video.
   Any time during setup, **Copy status** (next to the other buttons at the bottom) puts one line on the clipboard,
   for example `Nightshift setup 0.1.7 · step 6/14 Download AI models · 41% · 12:03 elapsed · last error NS-E105`.
   Paste it into a message to the person who sent you Nightshift.
   Setup does not leave the engine running: **Open dashboard** and the Desktop **Nightshift Dashboard** shortcut start it
   (and the dashboard) when needed. The dashboard and the setup window are separate windows, so you can open the
   dashboard while setup is still open.
   Setup also registers Nightshift for Windows notifications (the `ns notify` command and the nightly task use it). If you
   never see one, check Windows **Settings -> System -> Notifications** for "Nightshift".

With an `answers.json` next to `INSTALL.cmd` setup runs without a window, as before (add `-Ui` after
`INSTALL.cmd` to open the window with your answers filled in; `-NoUi` forces the no-window run). A run with an
answers file next to `INSTALL.cmd` or named with `-Answers` never stops to ask a question, whatever its console is: a key the file names (an empty `""` means no key)
is not asked for, and what the file leaves out takes its default or, for the channel name and niche, stops setup with a
message. `INSTALL.cmd -NoUi` and `INSTALL.cmd -Answers <file>` also end without waiting at "Press any key"; a
double-click on `INSTALL.cmd` still waits, so the message can be read.

You can close the setup window at any time: setup keeps running in the window called Nightshift setup, and running
**INSTALL.cmd** again opens the setup window again. If you close the Nightshift setup window, setup stops; run **INSTALL.cmd** again and it carries on
where it stopped. Do not turn the PC off while a download is moving; if you do, just run INSTALL.cmd again.

**Opening the dashboard in the morning.** The 03:00 run starts its own engine without a desktop. When you click
**Nightshift Dashboard** and that engine has finished its work, the shortcut restarts it in your session, so
**Open outbox** and the folder buttons work. If the night's run is still going, the shortcut never interrupts it:
the dashboard opens on the running engine, and the folder buttons start working after the run (close the dashboard
window and open the shortcut again).

The model files can also come from a folder on this PC or from the zip files in the Google Drive models folder:
choose that under **Model files come from** in the window.

## If a download stops

Downloads are built to be interrupted. Nothing is lost when the internet drops, the PC restarts, you press **Stop**
or **Pause**, or you close the Nightshift setup window.

- **What you see.** Every file has its own row. A file that is half done keeps a `.part` file in its folder
  (for example `models\llm\<name>.gguf.part`) and, when you run setup again, its row says *will continue from
  N GB*. A file that finished and passed its check has a `.ok` marker next to it and says *already here*.
- **What running INSTALL.cmd again does.** It checks every file in a moment (no download, no hashing: the `.ok`
  marker, the size and the date must still match). A finished file is left alone. A half file continues from
  the byte where it stopped, not from zero. A file that is missing, changed or has a marker that no longer matches
  is fetched again. Nothing that is fine is downloaded twice.
- **When the internet is slow or drops.** A file that gets no data for 60 seconds is dropped and tried again
  after 2, then 4 seconds (never more than 30), at most three tries; the row says *The connection dropped, trying
  again in 2 s (try 2 of 3)*. After the third try the row turns red with `NS-E105`: check the internet and press
  **Retry**, the file continues.
- **When the download site is down.** The files that setup itself downloads in step 2 (the models and the model
  programs: llama, cudart, qwentts and the rest of the download plan) wait out a site that answers 5xx, 408, 425
  or 429 (GitHub's 504 incident) for up to ten minutes of continuous trouble. Between tries it waits 2, 4, 8 ... 60
  seconds, or the site's own `Retry-After` when that is longer (at most 5 minutes). The row and the console say
  *github.com answers 504 (their side). Trying again in 40 s, 7 min left*; `ns models get` and the dashboard's model
  download print the same line. After that the file stops with `NS-E105` naming the site and the answer; run
  INSTALL.cmd again later and it continues at the failed step. A 404 or 403 stops at once. Other downloads give up
  as before: uv, FFmpeg and the Visual C++ runtime (stage 1 and the bootstrap), Python packages (uv and pip), the
  Hugging Face file listing and `ns update`.
- **A damaged file.** Every file is checked against the published SHA256 before it is kept. A file that fails is
  deleted and downloaded once more from the start; if it fails twice the row shows `NS-E109`.
- **The disk is full.** Setup stops with `NS-E104` and names the free space at the install folder. Free some space
  and press **Retry** (or run INSTALL.cmd again); the half file is still there and continues.
- **Check everything later.** Open a terminal in the install folder and run `ns verify --downloads`. It re-reads
  every model and program, compares it with `manifest.json` and prints one line per file: `ok`, `missing` or
  `bad hash`, with what to do for each (almost always: run INSTALL.cmd again). It changes nothing and needs no
  internet. It takes a few minutes because it reads about 20 GB.

## If GitHub is down on install day

The models can come from a folder (**Model files come from**, above). The programs can too: setup takes them from a
folder called `files` next to `INSTALL.cmd`, so an outage at GitHub does not stop the install.

1. **Make the folder, on any PC with internet** (before install day, or on another PC while GitHub is down for
   you). Extract the Nightshift zip there, open PowerShell in the extracted folder and run
   `powershell -ExecutionPolicy Bypass -File tools/make-files-kit.ps1`. It reads `manifest.json`, downloads every
   pinned program (uv, the Python build uv installs, the Visual C++ runtime, FFmpeg, every llama.cpp build with its
   CUDA runtime, the voice engine, OpenCode, yt-dlp, the window files, the face finder and the spaCy language
   model), checks each SHA256 and writes them to a folder called `files`, with a list in `files/files.json`. Add
   `-Only uv,python,ffmpeg,yt-dlp,spacy,webview2` (any keys) to fetch only some of them. A file that is already
   there and matches is kept, so you can run it again after a failure; it ends with exit code 1 and names what is
   still missing.
2. **Copy the folder `files` next to `INSTALL.cmd`** on the PC you are setting up.
3. **Run INSTALL.cmd** (again, if setup had stopped: it carries on at the failed step).

What setup does with it:

- For each program it looks in `files` first (by the file's name), then in your models folder, then uses winget
  where it always did, then the internet. A program it already installed is not looked for again.
- A file is used only when its SHA256 is the one `manifest.json` pins, and setup says so once per program:
  `llama_cpu: taken from files\llama-...zip (SHA256 checked)` in the console, the setup window's log and
  `logs`; the window's row says *copied from your folder* or *installed from the files folder*. Python is
  checked by uv itself, against the SHA256 uv carries for that build.
- A file with other bytes is never used. Setup says `files\llama-...zip does not match its pin; downloading it`
  once and goes on to the next source. Run `tools/make-files-kit.ps1` again to replace it.
- When a program is not in the folder and its site is down, the step stops with `NS-E105` as before, and the
  message now ends with these steps. What was copied or downloaded so far is kept.
- The folder is read where it is. It is not copied into the install folder; delete it after setup if you like.
- Not in the folder: the Python packages (PyPI), the models (Hugging Face: use **Model files come from**),
  Windows Terminal, and a voice engine that `manifest.json` does not pin.
- **Where the folder goes when you repair an install.** `files` is looked for next to the `INSTALL.cmd` you
  run. For a new install or an update that is the folder you extracted. If you run the `INSTALL.cmd` inside the
  install itself (`<install folder>/app/INSTALL.cmd`), put it there: `<install folder>/app/files`. An update
  replaces `app`, so keep your own copy of the folder somewhere else.

**What the PC check says about the internet** (the *Internet* row; the same in the setup window, in a run without
a window, when you press **Check again** and when you run INSTALL.cmd again). A site that answers with an error
(a 504, a rate limit) counts as reachable: the downloads wait for it as described above. The table is about a
site that gives no answer at all (its name does not resolve, a firewall drops it, the connection times out
after 10 seconds).

| Site with no answer | No `files` folder | `files` holds everything this PC takes from GitHub | `files` holds only some of it |
|---|---|---|---|
| github.com only | red, and the row names the kit steps | **warning, setup goes on**: *github.com does not answer; setup takes the pinned programs from files\ (SHA256 checked)* | red: *the files folder lacks:* and the names of the missing files; run the kit again and press **Check again** |
| pypi.org or files.pythonhosted.org (with or without GitHub) | red | red: *the files folder covers GitHub, but the Python packages need pypi.org and files.pythonhosted.org, so setup cannot start* | red |
| huggingface.co, download.pytorch.org, api.github.com, api.nuget.org, the Microsoft download site, Pexels, Pixabay | warning, as before | warning, as before | warning, as before |

- "Everything this PC takes from GitHub" is: uv, the llama.cpp build for this PC's driver and its CUDA runtime,
  yt-dlp, the face finder, the spaCy language model, FFmpeg unless it is already installed, the voice engine,
  OpenCode unless your answers switch Nightshift Code off, and the Python build under `files/python`. Each file is
  hashed for this check, so with a large folder on a slow stick the check takes a little longer.
- **Python** has no SHA256 in `manifest.json` (uv chooses the build and checks it when it installs it), so the
  check asks the pinned uv which archive it wants and looks for exactly that file; an older 3.12 archive does not
  pass. If the kit's `files.json` lists the archive, its bytes must match what the kit saved.
- **A kit made without OpenCode** keeps the row red, because the window asks about Nightshift Code only after
  the check. The row says so and gives both ways out: add the file with the kit, or install without Nightshift
  Code (put `"install_code": false` in an `answers.json` next to `INSTALL.cmd` and start `INSTALL.cmd -Ui`).
- The warning itself says that a program missing from the folder after all stops its step after a few minutes
  of trying; the bullets below give the exact waits.
- **With no internet at all setup does not start**, whatever the folder holds: the Python packages come from
  PyPI and nothing replaces them. The row says so before anything is installed. With GitHub alone gone, setup
  runs to the end as long as PyPI and the model source (Hugging Face, or your models folder) answer.
- If a site that was only a warning is needed after all (a file is missing from the folder although the check
  passed, or you took the folder away), that step tries for a bounded time and then stops with `NS-E105`, naming
  the file and the kit steps: stage 1 (uv, the Visual C++ runtime) waits at most 100 seconds for the site to
  start answering; the tools step tries three times with 120 seconds each; a program download makes three
  tries and waits up to two minutes for the network before each counts, about eight minutes for one file. It
  never waits forever. Add the file and run INSTALL.cmd again.

## If a row is red or a message shows an error code

Every message has a code like `NS-E104`, a sentence and a "What to do". In the setup window press **Retry** where this table says "run INSTALL.cmd again". The usual ones:

| Code | What it means | What to do |
|---|---|---|
| NS-E101 | Not 64-bit Windows 10/11 | This PC cannot run Nightshift. Tell the developer. |
| NS-E102 | No NVIDIA graphics card found | Install the NVIDIA driver from nvidia.com/drivers, restart, run INSTALL.cmd again. |
| NS-E103 | NVIDIA driver too old (CUDA below 12.4) | Setup opens the driver page. Install the newest driver, restart, run INSTALL.cmd again. |
| NS-W115 | The driver version could not be read | Nothing: setup uses the CUDA 12.4 build. If a video fails on the graphics card later, install the newest driver. |
| NS-W404 | Captions will be slow: speech recognition runs on the processor, not the graphics card | Nothing blocks you; videos still work. Send the diagnostics zip if the captions step takes too long. |
| NS-E104 | Not enough free disk space | Free some space (the message says what this install or this video needs and what the drive has, when it knows), or choose another drive when asked for the install folder. |
| NS-E105 | A download site cannot be reached | If the detail says the download site answered a 5xx, 408, 425 or 429 (the setup plan's file downloads wait up to 10 minutes for that, the other downloads do not), it is the site's side (for example GitHub having an outage): nothing on this PC is wrong, run INSTALL.cmd again later (or press Retry; for a model download from the dashboard or `ns models get`, start it again) and it continues where it stopped. Otherwise check the internet, switch off any VPN or proxy, run INSTALL.cmd again. |
| NS-E109 | A downloaded file is damaged | Run INSTALL.cmd again. If it repeats, send the diagnostics zip. |
| NS-E110 | FFmpeg could not be installed | Setup tries winget first and then downloads a fixed FFmpeg build itself. Press Retry once; if it repeats, install FFmpeg yourself (`winget install Gyan.FFmpeg`) and press Retry. Git is not needed. |
| NS-E111 | winget (the Windows app installer) is not available | Nothing to do: setup downloads FFmpeg and the Visual C++ runtime directly. |
| NS-E114 | The Visual C++ runtime is missing (the model runner cannot start without it) | Stage 1 (the Nightshift setup window with text) asks for permission first: press **Yes**. If it was declined, look for a flashing shield icon on the taskbar and press **Yes**, or install it from `https://aka.ms/vs/17/release/vc_redist.x64.exe` and press Retry. |
| NS-E116 | A network port Nightshift needs is already in use | The message names the port and the program that holds it. Close that program (or restart the PC) and press Retry. |
| NS-E117 | The install folder name has characters the model programs cannot open | Use a folder name with plain English letters, digits, spaces, - and _, then press Retry. |
| NS-E121 | The install folder or the NIGHTSHIFT_HOME setting could not be set up (not a space problem) | Check that the install folder is on a drive this account can write to and that no program or policy locks the account's settings, then press Retry. |
| NS-E115 | The program files could not be copied into the install folder | Make sure the install folder is not the folder you extracted the zip to, close any window that shows it, press Retry. |
| NS-E113 | Antivirus blocked a file | Press **Open Protection history**, allow the Nightshift file there, then press Retry. Adding the install folder as an exclusion stops it happening again. If the message says Smart App Control: it blocks programs it does not know and cannot allow one file; turn it off in Windows Security > App & browser control > Smart App Control settings (it cannot be turned back on without reinstalling Windows), then press Retry. It is also the code when a downloaded program (`llama-server.exe`, `qwen-tts.exe`, `yt-dlp.exe`) is missing after it was unpacked: the scanner removed it. |
| NS-E120 | The file that holds your API keys (`config\secrets.dpapi`) could not be limited to your Windows account | Press Retry (or save the key again in Settings). If the drive is FAT or exFAT Windows cannot keep it private: move NIGHTSHIFT_HOME to an NTFS drive. On an NTFS drive, check that your account owns the Nightshift folder and that no sync or backup tool changes its permissions, then send the diagnostics zip. The keys in Credential Manager are not affected. |
| NS-W116 | On this drive Windows cannot keep the keys file private | The drive that holds NIGHTSHIFT_HOME is FAT or exFAT (no file permissions), so `config\secrets.dpapi` was saved without a lock: it still works and is useless on another PC, but any account on this PC could read it. To keep it private, move NIGHTSHIFT_HOME to an NTFS drive. Shown once. |
| NS-E501 / NS-E502 | Pexels or Pixabay key rejected | Copy the key again from the website (no spaces) and paste it. |
| NS-E108 / NS-E112 | Graphics card libraries not found | Press **Save a help file** and send that file. Nothing else to do. |

Yellow rows (codes starting `NS-W`) are warnings. `NS-W403` means the setup test video could not be made: setup is complete, make your first video from the dashboard. Setup carries on. Examples: the install folder is on a hard
disk (slow), the PC cannot wake itself at night (press **Open power settings**, and under Sleep set "Allow wake timers" to
**Enable**; "Important wake timers only" is not enough for the 03:00 run), very long file names are off (press
**Fix it** and say Yes when Windows asks), the Visual C++ runtime is missing (setup installs it; click
**Yes** when Windows asks), or a fallback was used for the voice or video encoder.

If the same red error comes back after one retry, do not guess: press **Save a help file** and send that file.

## Send diagnostics back

1. At the end of setup a file called `nightshift-diagnostics-<date>-<time>.zip` is saved on your **Desktop**.
   If setup stops with an error, press **Save a help file** on that screen (it saves the same kind of zip), or
   **Open log folder** to look at the logs. Any time later you can create a new one with the
   **Nightshift - Diagnostics** shortcut. Two files made on the same day do not overwrite each other.
2. Send that file to the person who gave you Nightshift (the window names who, when the developer filled it in), for
   example in the **diagnostics** folder in the Google Drive folder you were given.
3. It holds logs, settings (without your keys), the hardware check, the setup files (`setup-server.err`,
   `wizard_flags.json`, your choices) and test results, plus `install-report.json`, which lists every step with its
   result and how long it took. It never contains your keys, videos or the models.
4. In the Dashboard you can do the same from **Settings, Updates, Make report**: it saves the zip on your Desktop and
   shows a **Download** button.

## Make your first video (about 20 minutes)

1. Open **Nightshift Dashboard** on your Desktop. A small "Nightshift Engine" window appears and the dashboard opens
   in its own Nightshift window (no browser tabs; it opens once, a second click brings it to the front). If that window
   cannot start on this PC, Nightshift opens an Edge or Chrome app window instead by itself (your default browser
   when neither is installed) and you do nothing. The
   dashboard opens on the **Queue** page.
2. Open **Create**, choose **A new story video**, then **One Short** (fastest, a few minutes) or
   **Full video + Shorts** (a long video plus Shorts, about an hour). Pick the channel (change the idea or the voice
   if you want to), leave **When to start** on **Now** and press **Start**. The job's card in the Queue names the stage it is at:
   Writing the story, Recording narration, Adding captions, Finding footage, Rendering, Checking quality. A full
   video says Writing the chapters and Rendering the full video, and adds making thumbnails and cutting the Shorts.
3. When it is done, open **Queue**, tab **Ready**. Watch the video, read the notes, press **Approve** or **Reject**
   (Reject asks for a short reason).
4. The **Post it** list next to the video follows YouTube's upload order: **Open** the video file, open YouTube Studio
   and drag it in, copy the title, description and tags, set the answers it shows for the audience ("made for
   kids") and "Altered content", and publish.
5. Paste the YouTube link into the box at the end of the list and press **Done**.

## Make a narrator voice

In the Dashboard open **Library**, then **Voices**. Two ways to make a new voice:

- **Design**: type how the voice should sound ("a warm, slow baritone"), a name, and a sentence to hear it say.
  The first time, Nightshift needs the 2 GB voice-design model; if it says the model is missing, get it in
  **Settings, Models** (see "Get the voice design model" below).
- **Clone**: pick a recording of **one** speaker, 3 to 30 seconds, quiet room, no music. Tick the box saying you have
  the right to use that voice. Only clone your own voice or one you have written permission for.
  Nightshift refuses a recording that is too short, too quiet, distorted or noisy (code `NS-E312`): record a longer,
  cleaner one and try again.

It takes up to a minute (the graphics card is used, so it waits for a running video). When it finishes you hear the
sentence in the new voice, and the voice shows up in the voice list of every channel. The voice files are in
`models\tts\voices\<name>\` (`preview.wav` is the sentence you heard); `ns backup` keeps them.
You can also ask the Assistant ("make me a deep calm voice called Narrator2"); it shows a confirmation card first.

If you chose a designed or cloned voice during setup, Nightshift creates it the first time it runs (the first
video or the first night), because the voice engine is only settled at the end of setup. To make it right away,
open a command prompt and run `studio voice apply-pending`; the Library shows it afterwards. Command line:
`studio voice design NAME --description "a warm, slow baritone"`, or
`studio voice clone NAME --audio C:\Users\me\voice.wav --text "the words in the recording" --rights-confirmed`.
Code `NS-E311` means a stored voice was made by the other voice engine: make that voice again.

**If a voice fails.** Nightshift speaks with the `qwentts` engine (the three programs `qwen-tts.exe`, `qwen-codec.exe` and
`tts-server.exe` in `bin\qwentts`) and switches to the slower PyTorch engine (`NS-W301`) only when the first one
cannot run on your PC. Code `NS-E310` means one sentence could not be spoken: the message names the program and the last
lines it printed (often "out of memory": close other programs that use the graphics card and run it again).
Code `NS-E314` means the voice design model is not installed: see "Get the voice design model" below.
Code `NS-E315` means a new voice could not be built from your recording or description: use a clean 3 to 30 second
recording of one speaker and type exactly the words that are said in it. With the PyTorch engine the voices are stored
as one `speaker.bin`; with `qwentts` as `speaker.spk`, `speaker.rvq` and `ref.txt` in the voice folder. If you switch
engine, make the voices again (`NS-E311`). The speed setting of a channel (0.7 to 1.3) changes how fast the narrator
talks, not how high the voice is.

## If the voice engine fell back

A yellow `NS-W301` ("the voice engine fell back", in the setup summary or in `ns verify`) means the fast `qwentts` programs
could not run on your PC, so Nightshift speaks with the slower PyTorch engine instead. Videos still get a voice, only
the voice step takes longer. Nothing to fix right away: update the NVIDIA driver, restart, and run `ns verify` again to
see whether `qwentts` works now. If the warning stays, send the diagnostics zip. Voices made with one engine are not
read by the other (`NS-E311`): after the engine changes, make those voices again.

## Get the voice design model

Designing a voice from a description needs a 2 GB model that is not part of setup. In the Dashboard open
**Settings, Models** and press **Get voice design** (or run `ns models get voicedesign`). It is checked against the
published SHA256 and resumes if stopped. When it is done, design the voice again: the voice service notices the new file
on the next request, there is nothing to restart. Code `NS-E314` ("the voice design model is not installed") means this
step is still to do.

## If stock footage stops at night

Your stock footage keys (Pixabay, and Pexels if you added one) are kept in Windows Credential Manager. The night run
may start in a Windows session that cannot read it, so setup also stores the keys in `config\secrets.dpapi`, a file
Windows encrypts for this PC. The night run
uses that file when Credential Manager has nothing. The file is useless on another PC and never goes into the
diagnostics zip. Nightshift limits the file to your Windows account; on a FAT or exFAT drive (no file
permissions) it cannot, so the file is saved anyway and you are told once (`NS-W116`), a restore leaves the keys
file as it was and a backup to such a drive leaves it out. If footage is missing after you moved to another PC, open **Settings > Stock footage keys** in the
dashboard and save the keys again: it writes both places. Running INSTALL.cmd again does not ask for keys when
the install is finished: the window then opens on **Nightshift is ready** (same version) or offers the update
(newer zip).

## Start a video later

In **Create**, the **When** step lets you choose **Pick a time** instead of **Now**. The job waits in the queue
(Queue shows "Scheduled for ...") and starts at that time or as soon as Nightshift is running after it.
You can cancel it any time before. A waiting job does not keep Nightshift awake: if the PC is asleep or Nightshift
was closed at that time, the job starts the next time the Dashboard opens or the nightly run begins. Nothing wakes a closed Nightshift at that time, and **Quit Nightshift** warns you when jobs are waiting.
The nightly run itself is unchanged: **Settings -> Schedule** (the **Automatic runs** switch and the run time).

## Every night

Nightshift wakes the PC at the time you chose (default 03:00), makes a video, saves it in the Outbox and the PC
may go back to sleep. In the morning open the Dashboard and review. If the PC was off, it runs when you switch
it on. Do not use Nightshift Code while a night run is due: the graphics card can serve only one at a time.
Closing the Nightshift Code window any way (its X, Task Manager, signing out) gives the graphics card back within a few seconds.
Nightshift Code needs a context of at least 16384 tokens. Its OpenCode setup is trimmed (the to-do, web fetch, task and skill tools are off and a short Nightshift prompt replaces OpenCode's long one; the read, edit and write tools stay on, because without them it cannot change a file) so that a one-line question is meant to use at most half of that context, which leaves the rest for files and the answer. With less context it does not start: it says "Nightshift Code needs 16384 tokens of context; this install has N" before it takes the graphics card. The number that counts is the one the running engine uses, because the engine reads `llama.flags.ctx` in `config/runtime.json` only when it starts. If you raise it in the file, run **Nightshift - Stop All** and start Nightshift Code again; if the engine is still running with the old number, the message says so and tells you to restart. That context is also the one the nightly run uses (there is one model server), so a bigger context needs more memory for every job. Every start brings `config/opencode.json` up to this setup and sets the model's context and name from what the engine runs and the installed model; keys you added yourself stay. To make Nightshift Code read-only, set `"edit": false` under `"tools"` in that file: every start then says that it cannot change a file and names the key. A `"patch": false` on its own is removed at the start with a note, because an earlier build wrote it and in this OpenCode it switches the edit and write tools off.
If the graphics card is busy at that time, the run waits up to 2 hours and then skips the night (code `NS-E201`);
it tries again the next night while Automatic runs are on.

When the run ends you get a Windows notification ("1 master + 5 Shorts ready for review", or "run failed
(NS-E203), open Dashboard"). Switch it off in **Settings -> Notifications**. Right after the run Nightshift shuts
its engine down by itself, unless the Dashboard is open.

**Automatic runs are a switch.** Settings -> Schedule -> **Automatic runs** adds the Windows task "Nightshift Daily"
(on) or removes it (off); the run time beside it is kept either way. The top bar, the Queue and the Assistant say
which it is: "Next run ...", "Runs at ... only while you are signed in", or "Automatic runs off". The page shows
what Windows has, not what you clicked: if you delete or disable the task in Task Scheduler, Nightshift reads that
as off within a minute, tells you once (`NS-W207`) and leaves the task as you set it; switch Automatic runs on in
Settings to get the nightly run back. An update and a rollback never switch it on or off. A backup restore leaves
them as they are. If Windows refuses the change, the switch stays where it was and the message says so (`NS-E204` when switching on or
moving the time; `NS-E206` when switching off, with the way to disable the task in Task Scheduler yourself).

**Setup asks once, and the answer is off unless you say yes.** On a first install the setup window has the switch
**Run Nightshift by itself every night** (the console setup asks the same question; an answers file says
`"nightly": true`). Without a yes no task is registered, and the run time you chose waits for the switch in Settings.
Setup run again over an install (a repair, or a newer zip) leaves Automatic runs as Windows has them: an answers file
changes nothing there, a nightly task that is on is registered again at its own time, and a new run time in the
answers moves it only if you did not change the time in Settings since the last setup. The window's switch is used
there only if Automatic runs still are what the window showed under it ("Now: ..."); if they changed meanwhile, setup
changes nothing and its last line says so. "Uninstall, keep my data" removes the task, so the setup after it asks
again. The last line of setup, and the Done screen, say what Automatic runs are.

A PC has one "Nightshift Daily" task, and it runs the Nightshift folder it was switched on in. If a first install
finds such a task and you did not say yes: a task left by an earlier Nightshift in the same folder, or in a folder
that no longer exists, is removed (its time is kept for the switch); a task that runs another Nightshift folder on
this PC is left alone, and setup names that folder and says this one does not run by itself. Switching Automatic
runs on in one Nightshift (or saying yes in its setup) moves the task to it; the message says so, and the other
Nightshift then shows Automatic runs off and tells you once (`NS-W207`).

If Nightshift cannot write `config/schedule.json` after Windows took the task, it puts the task back as it was and
says so (`NS-E204`, "the change was undone"). Only if that fails too it says `NS-W208`: Windows then runs the task
while the pages still say off (or the old time) until the folder `config` can be written again; to stop the run
before that, delete or disable "Nightshift Daily" in Task Scheduler.

If Windows will not let Nightshift register the task for unattended runs, it registers it for "only while you are
logged on" and says so at the end of setup (`NS-W205`). Then keep the PC logged in overnight (lock the screen, do not
sign out), or run `ns schedule install` once from an elevated PowerShell. If the task cannot be registered at all, setup
still finishes (`NS-W206`) and the run starts only when you start it. A message that could not be shown as a
notification (the night run has no screen) waits in the Dashboard for the morning.

The Dashboard also counts as "somebody is here": the engine does not shut itself down after 30 idle minutes while the
Nightshift window (or a browser tab on the Dashboard) is open.

About once a week the engine also asks GitHub whether a newer version exists and tells you with a notification
(same switch). It sends nothing else. To check yourself: `ns update --check`; to install the newest version:
`ns update --github` (the download is checked against its published SHA256 before anything is replaced, and the old
version is put back if a step fails).

If one job of the night fails for a reason Nightshift did not expect, it is recorded as `NS-E327` in
`logs\nightly-<date>.json` and the other jobs still run. A clip that fails its checks is left out of the Outbox
(the reason is in `outbox\skipped.json` of its job folder) and the other clips are delivered.

A night never runs without an owner mark: a small lock file in `data\owners` tells a restart that the run is alive. A
file that is missing is simply made again, and a file that cannot be opened for a moment (an antivirus scan) is tried
again for about a second. If it still cannot be made (the folder is read-only, full or blocked) or another Nightshift
run really is running with the same name, the night does not start: **Last night** in the Queue shows a red
"Something stopped" line, "The night: NS-E327." followed by the reason, and the Dashboard leaves a morning note with
the same reason in plain words (unless notifications are off in Settings; the **Last night** line stays either way).
The next nightly run tries again (while Automatic runs are on in Settings).

### Your own examples and prompts

Put your own example stories in `exemplars\` (a `.md` or `.txt` file per story, or a folder named `horror`,
`mystery`, `revenge` or `wholesome_twist` with the same `meta.yaml` as ours): the writing prompts then use them
instead of ours. To change a prompt, copy its file from `ns_studio\prompts\` to `prompt_overrides\` and edit it
(keep the lines between the `---` marks at the top); a file that does not parse is ignored. Both folders are in
**Export settings**. Upload sheets show the time zone after the publish time.

## If YouTube sends a warning or strike

If YouTube sends a warning, a strike or a monetization notice about a channel, stop that channel first and look at
what it made. A pause holds the channel's story videos that have not started: the night run skips a paused
channel (and, if the pause comes during the night, it starts no further video of that channel, new or half-made
from an earlier night; the morning report lists them under `paused`, not under `skipped`; a half-made video it left
waiting is continued by the first night after you resume the channel only if it started no more than three days before
that night, an older one waits for **Retry** in the Queue), and the Dashboard
refuses to start a story video for it (the Clipper does not check whether the channel is paused). A story video that
is already queued or scheduled (Create > **Pick a time**) does not start while the channel is paused: it stays
waiting under **Working now**, also when its time comes, and starts by itself, in its turn, after you
resume the channel. This holds also for a video that was already waiting for the engine because the night run was
using it: when the engine becomes free the Dashboard looks at the channel again, makes no video and puts the job
back to waiting (until then its card still reads "Waiting for GPU"; if that wait for the engine runs out after two
hours, the job fails with NS-E201 and no video is made). The job's status text says why ("Paused: <channel> is paused since <time>; resume it to release
this job"); the Queue's **Render jobs** card shows it (Stage: Paused, with that text); the Create page does not
print it yet: Create says "Waiting for the engine" for it, or "Scheduled for <time>" while the time it was set for
is still ahead, which is not the real reason. **Retry from where it stopped** (on a
stopped card in **Create** and **Queue**) and the Assistant's resume are refused for a paused channel with
"'<channel>' is paused. Resume it first.", the same answer as a new video. A story video that is already being
made stops before its next step: the step under way is finished first (press **Cancel** under **Working now** to
stop at once), what it made so far is kept, the job goes back to waiting under **Working now** and continues from
that step by itself after you resume the channel. Nothing you asked for is lost: a Create request for several
videos does not start the next while paused, and after the resume it finishes the video it was making and then
makes the ones still owed (the status text says how many, for example "(3 videos still to make, the first one half
made)"); none is made twice. If you resume before the Dashboard has looked at the stopped job, nothing is lost
either: the job goes back to waiting as "Queued" with the same videos still to make and continues in its turn
(it does not stop with an error). Its card, while it waits and when it is finished, lists every video of the
request, also those made before the pause. **Cancel** on a waiting request stops it for good: the card then says
how many videos were made and names the half-made one, whose files stay in its job folder (nothing is deleted).
**Retry from where it stopped** on a story card that stopped with an error or after a restart continues the video
that stopped and also makes the videos of that request that had not started; once that has started work, pressing
Retry on the old card again is refused, so nothing is made twice (the message that appears when you press
Retry says how many videos it will make). A video the night run was making when you paused
waits the same way and is continued by the first night after the resume if it started no more than three days
before that night (an older one waits for **Retry** in the Queue; a resume during the same night does not
continue it that night). Its Queue card says "its channel was paused during the night's run" and that the next
nightly run continues it after you resume. A video that a power cut or a restart interrupted, and whose channel
you paused afterwards, says the same way back ("Resume the channel: the next nightly run then continues it"),
not "Press Retry": Retry is refused while the channel is paused. All of this needs a nightly run, and one comes
only while **Automatic runs** are on in Settings (every such card says so); with them off, press **Retry** after
the resume or start the run yourself. A waiting video holds no part of the graphics card: the engine is
free for other work until it continues, and a job that is paused between two steps does not ask for the
graphics card again, so no model is loaded for it. A `studio run` typed by hand in a
terminal refuses a paused channel with NS-E330 ("The channel is paused."): `--channel <it>` and `--resume <its job>` start
nothing, and a run of every channel leaves it out. A pause typed while a `studio run` works stops it before its
next step: the video under way is left half made and recorded as failed with NS-E330, and the terminal prints
`studio run --resume <job id>`, which continues it from that step once the channel is resumed (with `--count`,
the videos it had not started are not made: type the run again). After the night, the Queue's **Last night** card says how many jobs a pause held ("1 job not started:
<channel> is paused"). Videos that are already made stay in the Queue.

Type the commands in a terminal opened after setup finished, with the full path of the studio program in the
install folder. Windows Terminal opens PowerShell, and there the quoted path needs `&` in front of it, or
PowerShell stops with "Unexpected token":

- PowerShell: `& "<install folder>\venvs\studio\Scripts\studio.exe" pause <channel-id> --reason "<notice text>"`
- Command Prompt: `"<install folder>\venvs\studio\Scripts\studio.exe" pause <channel-id> --reason "<notice text>"`

To resume, the same with `resume <channel-id>` in place of the `pause` part (`& "<install folder>\venvs\studio\Scripts\studio.exe" resume <channel-id>`
in PowerShell). The same goes for every other command below (`status`, `channel`); they are written as `studio` from
here on. The **Nightshift Studio** shortcut takes the same commands but splits what you type at every space, so it cannot
take a notice with several words: use a terminal for step 1.

1. **Pause the channel and keep the notice.** `studio pause <channel-id> --reason "<notice text>"` (`studio channel
   list` shows the ids). The reason is required. It is stored as an incident and `studio status` shows it. The
   switch on the channel's card in **Channels** pauses it too, but it records no notice text. To have the text on
   record without the command, ask the **Assistant**: it can pause the channel with your words as the note.
2. **Review the last 10 outputs** of the channel (`studio resume <channel-id>` lists them, newest first, and asks
   before it does anything: answer `n` to stay paused; the Queue tabs show the same videos). Check each one against
   the policy notes the app is built around (spec 18.2 to 18.4):
   - Policies: is it original writing, not repetitive or template-like content or a repost? Does the sheet answer
     "Altered content" with Yes when anything could be taken for real footage of a real person, place or event?
   - Content safety: no real, named people (living or dead) in any role, no real brands as villains, no real crimes or tragedies presented as fact, no
     minors in sexual, violent or endangering situations, no graphic gore, self-harm methods, weapon or drug
     instructions, hate or harassment, no medical, legal, financial or political advice, nothing that imitates
     news or emergency alerts.
   - Honest metadata: no "true story", "real" or "happened to me" in a title, description or tag of a fiction
     video; the hook and the title match what happens; the tags are relevant; the thumbnail shows the story.
3. **Raise the bar or trim the pools.** `studio channel edit <channel-id> --set critic_bar=8.5` raises the critic's
   bar (the default is 8; the highest is 10); the Assistant can change it too. To drop the openings, structures,
   caption looks or footage looks behind the flagged videos, open **Channels**, **Edit channel**, switch them off
   under **How stories open**, **How stories are built**, **Caption look** and **Footage look**, or add a subject
   under **Topics to avoid**. The editor warns when a list gets too short (at least 3 openings, 3 structures, 2
   caption looks and 2 footage looks). Reject the videos you would not post: when at least 5 videos are reviewed and
   you rejected 40 % or more of the last 10 reviewed, the night run raises the bar by 0.5 by itself, at most once a
   week.
4. **Resume.** `studio resume <channel-id>` (add `--yes` to skip the question). The switch in **Channels** resumes at
   once and shows no list, so do steps 2 and 3 first.

## Update to a new version

1. Download the new `Nightshift-Setup-<version>.zip` and its `.sha256` file from Drive. For **Settings > Update from a file** use `Nightshift-Update-<version>.zip` instead (older releases: `nightshift-app-<version>.zip`).
2. Easiest: double-click **Nightshift - Update** on the Desktop and pick the zip (from version 0.1.1 on). It works
   straight from the zip; nothing has to be extracted. Your models, settings, videos and history are stored
   elsewhere and are not touched.
3. Or extract the Setup zip anywhere (a new folder is fine; the old one can be deleted) and double-click
   **INSTALL.cmd** again. The window finds the finished install and shows the first screen with **Continue**: it
   updates the program in `<install folder>\app` (the old one is kept as `app.prev`), keeps everything that is
   done and refreshes the Python environments, the self-test, the shortcuts and the night task. If the zip is the
   same version as the one installed, the window opens on **Nightshift is ready** instead, with **Open dashboard**.
4. Send a new diagnostics zip so the developer can see the test table is green.
5. In the Dashboard, **Settings, Updates, Check now** asks GitHub for a newer version. In version 0.1.0 this part is not
   in the engine yet: the page then says so, and you use the Update shortcut with the zip as above.

Notes (from 0.1.1): the update runs as its own background process, started in your Nightshift data folder, so it
survives the engine stopping (also when you press **Update now** in the Dashboard: the window closes, the update goes
on, open Nightshift again when `run/update_state.json` says `done`). Close the Dashboard and Studio windows first if the
update says another program still has the folder open. The log is `logs/update-<time>.log`.
A cloned-repo install updates with `ns update --git` (newest release) or `ns update --git --channel main`; that one
needs Git for Windows (setup does not install it: without Git the command says "install Git for this"). The
update installs exactly the packages setup installed. The old version is kept next to it as `<folder>.prev`;
run `ns update --rollback` to go back. If a step fails, the old version is put back and the message names the step.
In a terminal (from 0.1.15): `ns update <zip>`, `ns update --github`, `ns update --git` and `ns update --rollback`
return at once and open a window, **Nightshift update**, that shows each step and the result. Closing the terminal
or that window stops nothing. The command ends before the result is known: the result is the last line of that
window and of the log. With `--detach` no window opens. A script that needs the result runs the command with
`--detach` and then the script its JSON line names as `follow` (`powershell -NoProfile -ExecutionPolicy Bypass
-File <follow>`): it prints the progress and exits 0 when the update is done, 1 otherwise; or it reads the entry of
`run/update_state.json` whose `log` is the one the command printed. `--wait` (follow and exit with the result) is
for a program that does not run from Nightshift's own Python environments; `ns` itself refuses it, because it
would hold the files the update replaces. Only one
update, rollback or repair runs at a time; a second one stops with `NS-E705` and names the one that runs. Nothing
starts while a program still runs from Nightshift's Python environments (a Studio window, a terminal that runs `ns`
or `studio`, Nightshift Code, a Python prompt): after a wait of up to a minute the message `NS-E706` names each
program and nothing is changed; close them and run it again. If the Python environments cannot be refreshed after
the program was changed, the message says which program is in place, which environment is left and how to finish,
and Nightshift is not started by the update: the progress window opens for that result on every route (also from
Settings > Updates), a note waits on the Queue page, and Settings > Updates shows the last failed update or rollback
until the next one. The commands such a message names are the `--detach` forms, because after a rollback the `ns`
you type is the older version's, and from 0.1.14 to 0.1.15-rc3 only its `--detach` form works; for a version that
has no `--detach` it names INSTALL.cmd. A failed update or rollback never says more than what is on the disk.
Before it changes the program, an update or rollback writes `run/venv-repair.json` (the uv commands that fill the
environments); it deletes it after the last one. When a program holds that file open at that moment and Windows
keeps refusing the delete for 15 seconds, `run/venv-repair.done` marks it finished instead and every start reads it
as done; only when neither can be written does the update stop before its check and say so, with Repair as the way
out, keeping the program it put in place (it is not rolled back for that). If it stops in between (a program held a file, the packages were
unreachable, the power went off), the next start from a Desktop shortcut or the nightly task runs those commands
first and starts Nightshift only when they worked (a note on the Queue page says so); otherwise it says why
(`NS-W210`) and starts nothing. Nothing starts from a shortcut while an update runs, and the nightly task waits up
to 30 minutes for one. An environment an older updater left on another version is named by `ns doctor` (row
"python environments") and at each start, with INSTALL.cmd's Repair as the way out. INSTALL.cmd and Repair take
the same lock as the update and refuse while a program runs from the environments (`NS-E705`, `NS-E706`, Retry);
an unattended setup (an answers.json beside INSTALL.cmd, or `-NoUi`) prints the same message and exits with 15
when an update, rollback or repair holds the lock, and with 14 when programs still run from the environments or
the running programs could not be listed (setup then refuses rather than guess). Every other start refuses as long
as the record waits: `studio run`, the `studio voice` commands, `studio dashboard`, `ns verify` and a launcher
started without its shortcut print `NS-W210` with the same way out before they write anything and start no engine;
while an update is running they say that instead. After a repair that failed, the Diagnostics shortcut still opens:
it makes the full diagnostics zip with `ns doctor --bundle`, and when that fails (the environment it runs from is
the half-filled one) a smaller zip made without Python (the logs folder, `config/install_state.json`, `VERSION`,
`run/venv-repair.json`, `run/update_state.json`; no settings and no keys), and says which one it made, or that none
was made and which folder to send instead.
From 0.1.14 and the 0.1.15 candidates up to rc3, update or go back with Settings > Updates, the Update shortcut or
`ns update ... --detach`: their own `ns update` typed in a terminal without `--detach` fails on its own `ns.exe`
(0.1.14's `ns update <zip>` then puts 0.1.14 back; rc3's `ns update --rollback` leaves one environment on the newer
version: update again from Settings > Updates, then run `ns update --rollback --detach`).
Locked packages: from the first release that carries `uv.lock`, setup, a repair (`INSTALL.cmd` again) and every
update install the exact package versions that release was tested with (each download is checked against its
SHA256), not the newest ones on the internet that day. The lists are kept as `logs/pins-<environment>.txt`. One
update is the exception: the update FROM 0.1.14 (or a 0.1.15 candidate up to rc3) to a locked release is done by
the old updater, which installs the newest versions inside the allowed ranges, as it always did. The next
update after that one is done by the new updater and installs the locked versions.
A package whose `uv.lock` is missing or does not belong to it is refused with NS-E124 before anything is changed:
download it again. `ns update --rollback` to a version from before the lock installs that version its own way.
An update also keeps a copy of your settings files (engine settings, setup answers, the nightly time, dashboard
settings, channels and formats) as `<folder>.prev-config.zip`, and `ns update --rollback` puts them back with the old
version, because an older version cannot always read a file a newer one saved. What you changed in those settings
after the update is lost by the rollback, and the rollback names the files it put back and the ones that had
changed; running `ns update --rollback` a second time goes forward again with the settings as they were. Only the
latest update is kept this way (a second update replaces the first one's copy). The copy is made by the version
that does the update, so it exists for an update made through `INSTALL.cmd` from a new zip, and for any update
made by a Nightshift that already has this feature (0.1.15 or later). An update made BY an older version with
`ns update <zip>`, `ns update --github` or Settings > Update (for example 0.1.14 going to 0.1.15) runs the older
updater, which saves no copy; so the new version saves it itself the first time it starts, before it changes
any settings file (it sees `<folder>.prev` with no copy that belongs to it). If that copy cannot be saved (a full
disk, a locked file, or a copy of another version is in its place), Nightshift still starts and writes one line
about it in `logs\first_start-<date>.jsonl`. Whenever `ns update --rollback` has no usable copy (none was saved,
or it is damaged), it keeps your settings files as they are but takes out of them every setting added since 0.1.14
that the older version does not know, and names each one (for example `runtime.json (tiny_story)`), so the older
version can read them. Two limits: a file the rollback cannot change (it is in use or read-only) keeps those
settings, and the rollback names it as one the older version may refuse (restore a backup with `ns restore` if
that version then does not start); and going back to a version older than 0.1.14 checks only the settings added
since 0.1.14. When there was simply no copy and nothing had to be taken out, the rollback says there was nothing
to put back, not that something failed. And from 0.1.15 on, a setting that a newer version added is written to
its file only when you use it: an install that never used a new feature keeps files the older version reads as
its own. One kind of job file is not changed by a rollback: a job made with the engine-test tiny-story profile
(a `plan.json` target under 9 minutes) cannot be resumed by 0.1.14; every other job can.
A rollback is safe to interrupt (the PC loses power, the window is closed): it first writes
`<folder>.rollback-pending.json`, then makes the settings files ones the older version reads, and only then puts
the older version in place. Whichever version is in `<folder>` afterwards finds settings it can load (apart from
a file the rollback could not change, which it names, as above); when the newer version starts again it puts the
settings back as they were before the rollback, and finishes that even if it is interrupted itself (then run
`ns update --rollback` again), and a later version that has this code finishes an interrupted rollback by itself.
While the settings of an interrupted rollback are not back yet, `ns update`, `ns update --rollback` and
`INSTALL.cmd` stop with a message and change nothing; start Nightshift once (or close the program that holds a
settings file) and try again. If the older version stays in place and you later install a version again (even the
same one), the record is dropped and the settings stay as the older version left them. Only if the power goes off
in the instant between two folder renames can `<folder>` be missing with `<folder>.swap` beside it: rename
`<folder>.swap` back to `<folder>` and start Nightshift. Close the Dashboard and Studio windows before
`ns update --rollback`: a window that is already open saves its settings at once, into the settings the rollback is
changing, because it made its first-start check when it opened (or at its first save) and does not look again. Only a
window whose first settings save falls during the rollback waits for the rollback's lock, up to 60 seconds, once; if
the rollback still holds it then, the window logs a warning (`first_start_lock_busy`) and saves anyway.
A copy is replaced only together with the program it belongs to (an update replaces the
program backup and its copy together, and a rollback swaps the pair). Two things are never done to a copy: it is never overwritten with settings
that are not its program's, and a whole copy of a version that is not installed (it may be the only copy of that
version's settings) is never deleted. If the settings could not be put back by a rollback (a file was in use), the
copy stays for the next `ns update --rollback`, and the next update keeps it as it is. If an update finds a whole
copy of a version that is neither the one running nor the program backup (for example after you deleted the
`<folder>.prev` folder), it goes on, leaves that copy where it is, keeps no copy for its own program backup, and
says whose copy it is and where; move `<folder>.prev-config.zip` out of the folder (or delete it) before the next
update if you want a copy kept again. A damaged copy is replaced and the result says so. A copy that cannot be read
at all (a program holds it open) stops the update before anything changes (`NS-E702`, or `NS-E115` from
`INSTALL.cmd`); close the program and run the update again.

## If Nightshift did not run because its program folder is missing

You find a file `Nightshift did not run.txt` on the Desktop, or a notification "Nightshift did not run last
night", or a shortcut opens a window that says "Nightshift cannot start: its program folder is missing or
incomplete". An update or a rollback was interrupted (a power cut, for example) and the folder `app` inside
the Nightshift folder is not there. Your channels, videos, settings and models are not touched. The log
names it NS-W209.

1. Close every Nightshift window and any Explorer window that shows the Nightshift folder.
2. If you moved the folder `app` or `app.swap` somewhere else, move it back into the Nightshift folder as
   `app.swap` first: otherwise setup starts a fresh program folder.
3. Run `INSTALL.cmd` from a Nightshift zip of version 0.1.15 or later and choose the same install folder.
   Setup puts the program folder back.
4. Nothing else. The next night runs by itself. The next time Nightshift starts, the file on the Desktop is
   removed and the Dashboard shows one note that names the nights that were missed.

Until then the task tries again every night and counts the nights in the file. To stop that, disable
`Nightshift Daily` in Task Scheduler and enable it again after step 3.

`ns doctor` has a row "launch script". It fails when Nightshift could not point a Desktop shortcut or the
nightly task at the script that makes all of the above possible; the row says which, and `INSTALL.cmd` with
Repair fixes it. If the row "program folders" names a file `app.rollback-settings-<date>.zip`, it holds the
settings from before an interrupted rollback: to use them, stop Nightshift and unzip it into `config`.

## Move your settings to another PC

In the Dashboard open **Settings, Your settings**. **Export** saves a zip on your Desktop with your channels, voices, run
time and what Nightshift has learned (never models or videos). On the new PC, install Nightshift, then use **Import**
and pick that zip. What it replaces is kept first in `data/backup/pre-restore-...`. The same can be done from a command
prompt with `ns backup` and `ns restore <zip>`.

## Commands

Some messages name a command, such as `ns doctor --bundle` or `studio resume <slug>`. Setup puts two small command
files in the folder `cmd` inside the install folder (`ns.cmd` and `studio.cmd`) and adds that folder to your PATH,
and its last lines and the Done screen say: "Commands: 'ns' and 'studio' work in any PowerShell or Command Prompt
window you open from now on, and open this install". A window that was already open before setup does not have
them: open a new one. The commands always open the install whose `cmd` folder they are in, whatever
`NIGHTSHIFT_HOME` says in the window. When the commands could not be put on your PATH, or another Nightshift
install has them, setup says so (`NS-W120`) and gives this install's own files by their full path, for example
`D:/Nightshift/cmd/ns.cmd doctor`; that works in any window. `ns doctor` says in its row "commands" which install
they open now. Setup also says so when another program's `ns` or `studio` comes first on your PATH (the
NativeScript `ns`, an Android Studio or JetBrains `studio`). While a plain `ns` does not reach this install, the
messages, notes and pages of this install name its own command file by its full path instead of the plain
word, so the command they give is always this install's; this guide keeps the plain words: put the full path
in their place in that case. They decide that when they are shown, so a note written before a second install
took the commands names the right file when you read it, and they also give the full path when Windows does
not say what your PATH holds (the Update window always does). A path with a space is given with its drive
outside the quotes, `D:"\My Videos\Nightshift\cmd\ns.cmd" doctor`, which a Command Prompt and PowerShell both
run as it is.

- **Quotes and special characters.** In Windows PowerShell 5.1, an argument that holds `&`, `|`, `<`, `>` or `^`
  and no space reaches the command file without quotes, so cmd.exe splits it or drops the `^` before Nightshift
  sees it: put it in quotes twice (`'"a&b"'`). An argument with an odd number of double quotes takes the rest
  of the command file's line with it; avoid it.

- **A PowerShell opened as administrator** has the commands when it runs as your own Windows account (the usual
  "Run as administrator"). Another administrator account has its own PATH: type the full path there.
- **A PATH that is already long.** Setup never cuts your PATH. When adding the folder would make it longer than
  2047 characters (the most the Windows window for environment variables can edit), it changes nothing and says
  so: remove PATH entries you no longer use, then run INSTALL.cmd and choose Repair.
- **A dev install** never changes the PATH of the PC it is developed on: type its command files by their full path.
- **Going back to 0.1.14** (`ns update --rollback` to that version) keeps the commands working: they start the
  same programs, which 0.1.14 has too. 0.1.14's own uninstaller does not know them: after uninstalling 0.1.14,
  delete the folder `cmd` in the install folder and its entry in your PATH by hand (Start, "Edit environment
  variables for your account", Path).

## Two Nightshift installs on one PC

A PC has one Desktop, one nightly task (`Nightshift Daily`), one notification registration, one `NIGHTSHIFT_HOME`
variable and one PATH. It can hold more than one Nightshift folder. A second install takes none of these from the
first without your yes:

- **Desktop shortcuts.** When they open another Nightshift folder, setup leaves them as they are and says so on its
  last lines and on the Done screen (`NS-W119`, with the folder). Every install also has its own full set in the
  folder `Shortcuts` inside its install folder: open them from there, or pin them. To make the Desktop shortcuts
  open this install, run INSTALL.cmd again, choose this folder and switch **Make the Desktop shortcuts and the ns
  and studio commands open this install** on (the console setup asks; an answers file says `"shortcuts_here":
  true`). The yes is used once: a
  later repair of either install does not move them again. Shortcuts of a Nightshift folder that was deleted are
  taken without a question, and setup says which folder they opened. A file under one of those names that
  Nightshift did not make is left alone.
- **NIGHTSHIFT_HOME.** The value Windows keeps for your account is only the PC's default, for a command typed in
  a terminal that belongs to no install. The shortcuts, the nightly task, and INSTALL.cmd and UNINSTALL.cmd
  started from an install's `app` folder always work on the install they are in. `ns` or `studio` started from
  an install's own `venvs` folder works on that install too when the variable is only that default, and says so
  in one line ("Nightshift: using the install in ..."). A plain `ns` or `studio` (the command files, see
  "Commands") always opens the install whose `cmd` folder holds it. To work on another install from the same
  terminal, type that install's own command file (`D:/Other/cmd/ns.cmd doctor`); for its `ns.exe` started by its
  full path, setting the variable in that window first (`$env:NIGHTSHIFT_HOME = 'D:/Other'`) also works: a value
  that differs from the PC's default was set on purpose and is followed, again with one line ("Nightshift: using
  D:/Other: ..."). The variable follows the same yes as the shortcuts, and
  the switch (or the console question) is offered while the shortcuts, the variable or the commands are another
  install's; its line says what it moves. After it moved, open a new terminal: programs that are already open
  keep the old value.
- **The `ns` and `studio` commands.** The first Nightshift `cmd` folder on your PATH decides which install a plain
  `ns` opens. Setup adds this install's folder when no other existing install's comes first, and otherwise leaves
  the PATH as it is and says which install the commands open (`NS-W120`) and where this install's own are; the
  same yes moves them here. A folder of a deleted install is replaced without a question.
- **The nightly task.** As described under "Every night": it runs one install, and switching Automatic runs on in
  another install's Settings moves it there. A dev install never takes another install's task.
- **Notifications.** Both installs share the one registration; the second leaves it as it is unless you said yes.

Uninstalling one of them removes only what is its own: the task when it runs that install, the Desktop shortcuts
that open it, `NIGHTSHIFT_HOME` when it names it, its own `cmd` folder and its PATH entry (the rest of the PATH stays
exactly as it was), and its own `Shortcuts` folder. When the plain commands were the removed install's, a line says
how the other install gets them (its INSTALL.cmd, Repair) and gives its command files by their full path. What the other install uses is
left, and each line that starts with "Left" says what and for which folder; the notification registration and the
stored stock footage keys stay while another install is found. A shortcut or a task that does not say which
Nightshift it belongs to is left too, and named. UNINSTALL.cmd started from an install's own program folder
(`app` inside the install folder) removes that install, whatever `NIGHTSHIFT_HOME` names. Started from anywhere
else (a repository, an extracted zip) it follows the variable only to an install that runs that copy of the
program; otherwise it changes nothing and tells you to run the install's own UNINSTALL.cmd or to pass `-HomeDir`.
When the other install held none of the shared things (you never said yes for it), the uninstall still knows it
from the list setup keeps (`installs.txt` in `%LOCALAPPDATA%\Nightshift`): it keeps the notification
registration and the stored keys, and its last lines say "Another Nightshift is still installed at ..." with what
to run there (its INSTALL.cmd, Repair) so that `NIGHTSHIFT_HOME`, the Desktop shortcuts and the notification icon
become its own. An install made by a version before this one is not in that list until its setup runs once.

## Uninstall

Double-click **UNINSTALL.cmd** (Settings, About, Uninstall copies its full path). A window shows what goes and lets you tick the two things to delete. It removes the nightly task, the shortcuts and the notification registration (each only when it is this install's: see "Two Nightshift installs on one PC"), programs and environments. It asks
separately whether to delete the AI models (15 GB) and your videos and history. The default answer to both is
**No**. At the end it tells you what it kept and where. The window itself still uses a few program files, so the very
last ones are removed by a small hidden task as soon as the window closes; the list says so.

Uninstall does **not** change everything on the PC. These stay, and the closing screen lists them: your settings and
logs (`config` and `logs` in the install folder, so the folder itself stays), the Visual C++ runtime, FFmpeg and
Windows Terminal (other programs use them), and the Python that setup fetched together with its download cache
(several GB, in your user folders). Your stock footage keys are removed from Windows Credential Manager and from
`config\secrets.dpapi`, except that Credential Manager is left as it is while another Nightshift folder on this PC
uses it; the closing screen has a line for each, saying what was removed, what was left and what could not be
removed (with how to remove it by hand).

## Optional models

The voice design model (2 GB) and the second story model (21 GB) are not part of setup. Download them when you need
them: **Settings, Models** in the Dashboard, or `ns models get voicedesign` / `ns models get story_llm` (checked against
the published SHA256, resumes if stopped). In `answers.json`, `"include_voicedesign": true` downloads the voice design
model with setup.
