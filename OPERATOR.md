# Nightshift: operator guide

One page. You run the PC with the graphics card. You do not need to know how Nightshift works.
Keep this file next to `INSTALL.cmd`.

## What you need

- A Windows 10 or 11 PC with an NVIDIA RTX graphics card (16 GB) and 32 GB of memory for the recommended
  Balanced model. With less memory setup picks the smaller Small model for you.
- About 75 GB of free disk space (60 GB with the Small model).
- An internet connection: setup downloads about 20 GB with Balanced (about 15 GB with Small), or use a
  `models` folder you were given on Drive.
- Two free keys for stock footage: Pexels and Pixabay. Setup shows you where to get them.
  If you were given an `answers.json` file they may already be filled in.

## Install (about 1 hour, mostly waiting)

1. Download `Nightshift-<version>.zip` from the Google Drive folder. If there is also a `models` folder and an
   `answers.json`, download those too.
2. Right-click the zip, choose **Properties**, tick **Unblock** if you see it, and press OK.
   (Windows marks downloaded files; unblocking stops warning pop-ups later.)
3. Right-click the zip, **Extract All**, and pick a short folder such as `D:\NightShift`.
4. Put `answers.json` in that same folder, next to `INSTALL.cmd`. With it, setup asks almost nothing.
5. Double-click **INSTALL.cmd**. If Windows says "Windows protected your PC", press **More info**, then
   **Run anyway**. Windows may ask for permission to install programs: say **Yes**.
6. A black window with a small crescent appears first. It looks at your PC, gets Python, and then opens the
   **Nightshift setup window**. Keep the black window open: it does the work and is the log. If no setup window
   appears, the black window prints an address (`http://127.0.0.1:...`): paste it into Edge or Chrome.
7. In the setup window: read the PC check (green and yellow rows are fine, red rows say what to do), choose
   your channel, narrator, model and night run time (Balanced is
   preselected; on a PC with less than 32 GB of memory Small is, and the window says why), paste the two stock footage keys, and press **Install**.
   Setup then downloads the AI models (you see every file, the speed and the time left; **Pause** and **Resume**
   work), installs, tunes itself for 1 to 3 minutes and plays you a test voice and a short test video.
8. At the end the window says **Nightshift is ready** and names a **diagnostics zip** on your Desktop. Send it back
   (see below). **Open dashboard** starts the dashboard.
   Setup does not leave the engine running: **Open dashboard** and the Desktop **Nightshift Dashboard** shortcut start it
   (and the dashboard) when needed. The dashboard and the setup window are separate windows, so you can open the
   dashboard while setup is still open.
   Setup also registers Nightshift for Windows notifications (the `ns notify` command and the nightly task use it). If you
   never see one, check Windows **Settings -> System -> Notifications** for "Nightshift".

With an `answers.json` next to `INSTALL.cmd` setup runs without a window, as before (add `-Ui` after
`INSTALL.cmd` to open the window with your answers filled in; `-NoUi` forces the no-window run).

You can close the setup window at any time: setup keeps running in the black window, and running **INSTALL.cmd**
again opens the window again. If you close the black window, setup stops; run **INSTALL.cmd** again and it carries on
where it stopped. Do not turn the PC off while a download is moving; if you do, just run INSTALL.cmd again.

The model files can also come from a folder on this PC or from the zip files in the Google Drive models folder:
choose that under **Model files come from** in the window.

## If a row is red or a message shows an error code

Every message has a code like `NS-E104`, a sentence and a "What to do". In the setup window press **Retry** where this table says "run INSTALL.cmd again". The usual ones:

| Code | What it means | What to do |
|---|---|---|
| NS-E101 | Not 64-bit Windows 10/11 | This PC cannot run Nightshift. Tell the developer. |
| NS-E102 | No NVIDIA graphics card found | Install the NVIDIA driver from nvidia.com/drivers, restart, run INSTALL.cmd again. |
| NS-E103 | NVIDIA driver too old | Setup opens the driver page. Install the newest driver, restart, run INSTALL.cmd again. |
| NS-E104 | Not enough free disk space | Free 60 GB, or choose another drive when asked for the install folder. |
| NS-E105 | A download site cannot be reached | Check the internet, switch off any VPN or proxy, run INSTALL.cmd again. |
| NS-E109 | A downloaded file is damaged | Run INSTALL.cmd again. If it repeats, send the diagnostics zip. |
| NS-E110 / NS-E111 | A tool or winget (App Installer) is missing | Install "App Installer" from the Microsoft Store, then press Check again (or Retry). |
| NS-E113 | Antivirus blocked a file | Open Windows Security, Protection history, allow the Nightshift file and press Retry. |
| NS-E501 / NS-E502 | Pexels or Pixabay key rejected | Copy the key again from the website (no spaces) and paste it. |
| NS-E108 / NS-E112 | Graphics card libraries not found | Send the diagnostics zip. Nothing else to do. |

Yellow rows (codes starting `NS-W`) are warnings. `NS-W403` means the setup test video could not be made: setup is complete, make your first video from the dashboard. Setup carries on. Examples: the install folder is on a hard
disk (slow), the PC cannot wake itself at night (turn on "Allow wake timers" in Power Options), long Windows
paths are off (run setup once as administrator), or a fallback was used for the voice or video encoder.

If the same red error comes back after one retry, do not guess: send the diagnostics zip.

## Send diagnostics back

1. At the end of setup a file called `nightshift-diagnostics-<date>.zip` is saved on your **Desktop**.
   Any time later you can create a new one with the **Nightshift - Diagnostics** shortcut.
2. Upload that file to the **diagnostics** folder in the Google Drive folder you were given.
3. It holds logs, settings and test results. It never contains your keys, videos or the models.
4. In the Dashboard you can do the same from **Settings, Updates, Make report**: it saves the zip on your Desktop and
   shows a **Download** button.

## Make your first video (about 20 minutes)

1. Open **Nightshift Dashboard** on your Desktop. A small "Nightshift Engine" window appears and the dashboard opens
   in its own Nightshift window (no browser tabs; it opens once, a second click brings it to the front). If that window
   cannot start on this PC, Nightshift opens a browser app window instead by itself and you do nothing.
2. Press **Run now**, then **shorts_only** (fastest) or **master_first** (a full long video plus Shorts).
   The bar at the top shows the stages: loading, writing, voicing, captions, clips, rendering, checking, done.
3. Open **Review**. Watch the video, read the notes, press **Approve** or **Reject** (add a reason).
4. Press **Open Outbox**, drag the approved MP4 into YouTube Studio, copy the title, description and tags from
   the sheet, set the answers it shows for AI disclosure and "made for kids", and publish.
5. Paste the YouTube link into **Mark uploaded**.

Until the Dashboard is part of your version (it arrives in a later release) the developer will tell you what to
run instead.

## Make a narrator voice

In the Dashboard open **Library**, then **Voices**. Two ways to make a new voice:

- **Design**: type how the voice should sound ("a warm, slow baritone"), a name, and a sentence to hear it say.
  The first time, Nightshift needs the 2 GB voice-design model; if it says the model is missing, run INSTALL.cmd
  again and tick the voice design option.
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

## Start a video later

In **Create**, the **When** step lets you choose a start time instead of "now". The job waits in the queue
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

About once a week the engine also asks GitHub whether a newer version exists and tells you with a notification
(same switch). It sends nothing else. To check yourself: `ns update --check`; to install the newest version:
`ns update --github` (the download is checked against its published SHA256 before anything is replaced, and the old
version is put back if a step fails).

## Update to a new version

1. Download the new `Nightshift-<version>.zip` and its `.sha256` file from Drive.
2. Extract it into the **same folder** as before and choose **Replace files**. Your models, settings, videos and
   history are stored elsewhere and are not touched.
3. Double-click **Nightshift - Update** on the Desktop and pick the zip (from version 0.3 on). For versions
   before that, double-click **INSTALL.cmd** again: it keeps everything that is done and refreshes the rest.
4. Send a new diagnostics zip so the developer can see the test table is green.
5. In the Dashboard, **Settings, Updates, Check now** asks GitHub for a newer version. In version 0.1.0 this part is not
   in the engine yet: the page then says so, and you use the Update shortcut with the zip as above.

Notes (from 0.1.1): the update steps out of the Nightshift folder before it swaps it, so it works from the Desktop
shortcut and from a command prompt opened inside the folder. The old version is kept next to it as `<folder>.prev`;
run `ns update --rollback` to go back. If a step fails, the old version is put back and the message names the step.

## Move your settings to another PC

In the Dashboard open **Settings, Your settings**. **Export** saves a zip on your Desktop with your channels, voices, run
time and what Nightshift has learned (never models or videos). On the new PC, install Nightshift, then use **Import**
and pick that zip. What it replaces is kept first in `data/backup/pre-restore-...`. The same can be done from a command
prompt with `ns backup` and `ns restore <zip>`.

## Uninstall

Double-click **UNINSTALL.cmd** (Settings, About, Uninstall copies its full path). A window shows what goes and lets you tick the two things to delete. It removes the nightly task, shortcuts, the notification registration, programs and environments. It asks
separately whether to delete the AI models (15 GB) and your videos and history. The default answer to both is
**No**. At the end it tells you what it kept and where.
