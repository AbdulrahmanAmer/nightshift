# Nightshift: operator guide

One page. You run the PC with the graphics card. You do not need to know how Nightshift works.
Keep this file next to `INSTALL.cmd`.

## What you need

- A Windows 10 or 11 PC with an NVIDIA RTX graphics card (16 GB) and about 60 GB of free disk space.
- An internet connection (setup downloads about 15 GB), or a `models` folder you were given on Drive.
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
6. Watch the window. First you see a table of green and red rows (your PC check), then setup installs tools,
   asks a few questions (skipped when `answers.json` is there), downloads the AI models with progress bars,
   tunes itself for 1 to 3 minutes and runs a test table.
7. At the end you see a summary and the name of a **diagnostics zip** on your Desktop. Send it back (see below).

You can close the window at any time. Run **INSTALL.cmd** again and it carries on where it stopped.
Do not turn the PC off while a download bar is moving; if you do, just run INSTALL.cmd again.

## If a row is red or a message shows an error code

Every message has a code like `NS-E104`, a sentence and a "What to do". The usual ones:

| Code | What it means | What to do |
|---|---|---|
| NS-E101 | Not 64-bit Windows 10/11 | This PC cannot run Nightshift. Tell the developer. |
| NS-E102 | No NVIDIA graphics card found | Install the NVIDIA driver from nvidia.com/drivers, restart, run INSTALL.cmd again. |
| NS-E103 | NVIDIA driver too old | Setup opens the driver page. Install the newest driver, restart, run INSTALL.cmd again. |
| NS-E104 | Not enough free disk space | Free 60 GB, or choose another drive when asked for the install folder. |
| NS-E105 | A download site cannot be reached | Check the internet, switch off any VPN or proxy, run INSTALL.cmd again. |
| NS-E109 | A downloaded file is damaged | Run INSTALL.cmd again. If it repeats, send the diagnostics zip. |
| NS-E110 / NS-E111 | A tool or winget (App Installer) is missing | Install "App Installer" from the Microsoft Store, then run INSTALL.cmd again. |
| NS-E501 / NS-E502 | Pexels or Pixabay key rejected | Copy the key again from the website (no spaces) and paste it. |
| NS-E108 / NS-E112 | Graphics card libraries not found | Send the diagnostics zip. Nothing else to do. |

Yellow rows (codes starting `NS-W`) are warnings. Setup carries on. Examples: the install folder is on a hard
disk (slow), the PC cannot wake itself at night (turn on "Allow wake timers" in Power Options), long Windows
paths are off (run setup once as administrator), or a fallback was used for the voice or video encoder.

If the same red error comes back after one retry, do not guess: send the diagnostics zip.

## Send diagnostics back

1. At the end of setup a file called `nightshift-diagnostics-<date>.zip` is saved on your **Desktop**.
   Any time later you can create a new one with the **Nightshift - Diagnostics** shortcut.
2. Upload that file to the **diagnostics** folder in the Google Drive folder you were given.
3. It holds logs, settings and test results. It never contains your keys, videos or the models.

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

## Every night

Nightshift wakes the PC at the time you chose (default 03:00), makes a video, saves it in the Outbox and the PC
may go back to sleep. In the morning open the Dashboard and review. If the PC was off, it runs when you switch
it on. Do not use Nightshift Code while a night run is due: the graphics card can serve only one at a time.

## Update to a new version

1. Download the new `Nightshift-<version>.zip` and its `.sha256` file from Drive.
2. Extract it into the **same folder** as before and choose **Replace files**. Your models, settings, videos and
   history are stored elsewhere and are not touched.
3. Double-click **Nightshift - Update** on the Desktop and pick the zip (from version 0.3 on). For versions
   before that, double-click **INSTALL.cmd** again: it keeps everything that is done and refreshes the rest.
4. Send a new diagnostics zip so the developer can see the test table is green.

## Uninstall

Double-click **UNINSTALL.cmd**. It removes the nightly task, shortcuts, programs and environments. It asks
separately whether to delete the AI models (15 GB) and your videos and history. The default answer to both is
**No**. At the end it tells you what it kept and where.
