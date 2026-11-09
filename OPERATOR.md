# Nightshift: operator guide

One page. You run the PC with the graphics card. You do not need to know how Nightshift works.
Keep this file next to `INSTALL.cmd`.

## What you need

- A Windows 10 or 11 PC with an NVIDIA RTX graphics card (16 GB) and 32 GB of memory for the recommended
  Balanced model. With less memory setup picks the smaller Small model for you.
- At least 60 GB of free disk space (74 GB with the recommended Balanced model, 81 GB with the Best one).
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
   work), installs, tunes itself for 1 to 3 minutes and plays you a test voice and a short test video.
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
`INSTALL.cmd` to open the window with your answers filled in; `-NoUi` forces the no-window run).

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

## If a row is red or a message shows an error code

Every message has a code like `NS-E104`, a sentence and a "What to do". In the setup window press **Retry** where this table says "run INSTALL.cmd again". The usual ones:

| Code | What it means | What to do |
|---|---|---|
| NS-E101 | Not 64-bit Windows 10/11 | This PC cannot run Nightshift. Tell the developer. |
| NS-E102 | No NVIDIA graphics card found | Install the NVIDIA driver from nvidia.com/drivers, restart, run INSTALL.cmd again. |
| NS-E103 | NVIDIA driver too old (CUDA below 12.4) | Setup opens the driver page. Install the newest driver, restart, run INSTALL.cmd again. |
| NS-W115 | The driver version could not be read | Nothing: setup uses the CUDA 12.4 build. If a video fails on the graphics card later, install the newest driver. |
| NS-W404 | Captions will be slow: speech recognition runs on the processor, not the graphics card | Nothing blocks you; videos still work. Send the diagnostics zip if the captions step takes too long. |
| NS-E104 | Not enough free disk space | Free 60 GB, or choose another drive when asked for the install folder. |
| NS-E105 | A download site cannot be reached | If the detail says the download site answered a 5xx, 408, 425 or 429 (the setup plan's file downloads wait up to 10 minutes for that, the other downloads do not), it is the site's side (for example GitHub having an outage): nothing on this PC is wrong, run INSTALL.cmd again later (or press Retry; for a model download from the dashboard or `ns models get`, start it again) and it continues where it stopped. Otherwise check the internet, switch off any VPN or proxy, run INSTALL.cmd again. |
| NS-E109 | A downloaded file is damaged | Run INSTALL.cmd again. If it repeats, send the diagnostics zip. |
| NS-E110 | FFmpeg could not be installed | Setup tries winget first and then downloads a fixed FFmpeg build itself. Press Retry once; if it repeats, install FFmpeg yourself (`winget install Gyan.FFmpeg`) and press Retry. Git is not needed. |
| NS-E111 | winget (the Windows app installer) is not available | Nothing to do: setup downloads FFmpeg and the Visual C++ runtime directly. |
| NS-E114 | The Visual C++ runtime is missing (the model runner cannot start without it) | Stage 1 (the Nightshift setup window with text) asks for permission first: press **Yes**. If it was declined, look for a flashing shield icon on the taskbar and press **Yes**, or install it from `https://aka.ms/vs/17/release/vc_redist.x64.exe` and press Retry. |
| NS-E116 | A network port Nightshift needs is already in use | The message names the port and the program that holds it. Close that program (or restart the PC) and press Retry. |
| NS-E117 | The install folder name has characters the model programs cannot open | Use a folder name with plain English letters, digits, spaces, - and _, then press Retry. |
| NS-E115 | The program files could not be copied into the install folder | Make sure the install folder is not the folder you extracted the zip to, close any window that shows it, press Retry. |
| NS-E113 | Antivirus blocked a file | Press **Open Protection history**, allow the Nightshift file there, then press Retry. Adding the install folder as an exclusion stops it happening again. If the message says Smart App Control: it blocks programs it does not know and cannot allow one file; turn it off in Windows Security > App & browser control > Smart App Control settings (it cannot be turned back on without reinstalling Windows), then press Retry. It is also the code when a downloaded program (`llama-server.exe`, `qwen-tts.exe`, `yt-dlp.exe`) is missing after it was unpacked: the scanner removed it. |
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
diagnostics zip. If footage is missing after you moved to another PC, open **Settings > Stock footage keys** in the
dashboard and save the keys again: it writes both places. Running INSTALL.cmd again does not ask for keys when
the install is finished: the window then opens on **Nightshift is ready** (same version) or offers the update
(newer zip).

## Start a video later

In **Create**, the **When** step lets you choose **Pick a time** instead of **Now**. The job waits in the queue
(Queue shows "Scheduled for ...") and starts at that time or as soon as Nightshift is running after it.
You can cancel it any time before. A waiting job does not keep Nightshift awake: if the PC is asleep or Nightshift
was closed at that time, the job starts the next time the Dashboard opens or the nightly run begins. Nothing wakes a closed Nightshift at that time, and **Quit Nightshift** warns you when jobs are waiting.
The nightly run itself is unchanged: **Settings -> Run time**.

## Every night

Nightshift wakes the PC at the time you chose (default 03:00), makes a video, saves it in the Outbox and the PC
may go back to sleep. In the morning open the Dashboard and review. If the PC was off, it runs when you switch
it on. Do not use Nightshift Code while a night run is due: the graphics card can serve only one at a time.
If the graphics card is busy at that time, the run waits up to 2 hours and then skips the night (code `NS-E201`);
it tries again the next night.

When the run ends you get a Windows notification ("1 master + 5 Shorts ready for review", or "run failed
(NS-E203), open Dashboard"). Switch it off in **Settings -> Notifications**. Right after the run Nightshift shuts
its engine down by itself, unless the Dashboard is open.

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
The next nightly run tries again.

### Your own examples and prompts

Put your own example stories in `exemplars\` (a `.md` or `.txt` file per story, or a folder named `horror`,
`mystery`, `revenge` or `wholesome_twist` with the same `meta.yaml` as ours): the writing prompts then use them
instead of ours. To change a prompt, copy its file from `ns_studio\prompts\` to `prompt_overrides\` and edit it
(keep the lines between the `---` marks at the top); a file that does not parse is ignored. Both folders are in
**Export settings**. Upload sheets show the time zone after the publish time.

## If YouTube sends a warning or strike

If YouTube sends a warning, a strike or a monetization notice about a channel, stop that channel first and look at
what it made. A pause stops only new work: the night run skips a paused channel and the Dashboard refuses to start
a story video for it (the Clipper does not check whether the channel is paused). Videos that are already made
stay in the Queue. A story video that is already queued or scheduled (Create > **Pick a time**) still runs after
the pause, because the Dashboard does not look at the channel again when it starts a waiting job: open **Create**
and press **Cancel** on every job of that channel under **Working now**. The same goes for a story video that
stopped with an error: **Retry from where it stopped** (on its card in **Create** and **Queue**) does not refuse a
paused channel and starts that video again, so do not press it on any stopped card of that channel until you
resume the channel.

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
An update also keeps a copy of your settings files (engine settings, setup answers, the nightly time, dashboard
settings, channels and formats) as `<folder>.prev-config.zip`, and `ns update --rollback` puts them back with the old
version, because an older version cannot always read a file a newer one saved. What you changed in those settings
after the update is lost by the rollback, and the rollback names the files it put back and the ones that had
changed; running `ns update --rollback` a second time goes forward again with the settings as they were. Only the
latest update is kept this way (a second update replaces the first one's copy). The copy is made by the version
that does the update, so it exists for an update made through `INSTALL.cmd` from a new zip, and for any update
made by a Nightshift that already has this feature (0.1.15 or later). An update made BY an older version with
`ns update <zip>`, `ns update --github` or Settings > Update (for example 0.1.14 going to 0.1.15) saves no copy,
because the older updater is the one that runs: the rollback then swaps the program as before and says it could
not restore the settings. A copy is replaced only together with the program it belongs to (an update replaces the
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

## Move your settings to another PC

In the Dashboard open **Settings, Your settings**. **Export** saves a zip on your Desktop with your channels, voices, run
time and what Nightshift has learned (never models or videos). On the new PC, install Nightshift, then use **Import**
and pick that zip. What it replaces is kept first in `data/backup/pre-restore-...`. The same can be done from a command
prompt with `ns backup` and `ns restore <zip>`.

## Uninstall

Double-click **UNINSTALL.cmd** (Settings, About, Uninstall copies its full path). A window shows what goes and lets you tick the two things to delete. It removes the nightly task, shortcuts, the notification registration, programs and environments. It asks
separately whether to delete the AI models (15 GB) and your videos and history. The default answer to both is
**No**. At the end it tells you what it kept and where. The window itself still uses a few program files, so the very
last ones are removed by a small hidden task as soon as the window closes; the list says so.

Uninstall does **not** change everything on the PC. These stay, and the closing screen lists them: your settings and
logs (`config` and `logs` in the install folder, so the folder itself stays), your stock footage keys (Windows
Credential Manager), the Visual C++ runtime, FFmpeg and Windows Terminal (other programs use them), and the Python
that setup fetched together with its download cache (several GB, in your user folders).

## Optional models

The voice design model (2 GB) and the second story model (21 GB) are not part of setup. Download them when you need
them: **Settings, Models** in the Dashboard, or `ns models get voicedesign` / `ns models get story_llm` (checked against
the published SHA256, resumes if stopped). In `answers.json`, `"include_voicedesign": true` downloads the voice design
model with setup.
