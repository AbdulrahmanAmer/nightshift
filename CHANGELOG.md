# Changelog

All notable changes to Nightshift are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Milestones map to minors: 0.1.0 is M0, 0.2.0 is M1, and so on
(`docs/ENGINEERING.md` section 7).

## [Unreleased]

## [0.1.15] - 2026-11-12

This is release candidate 0.1.15-rc8, published for testing. It is a prerelease: it is not offered as an update to a
PC on 0.1.14 or on an earlier candidate, and you install it by hand (`Nightshift-Setup-0.1.15-rc8.zip` for a first
install, or `Nightshift-Update-0.1.15-rc8.zip` through Settings > Updates > From a file). 0.1.15-rc8 is the first candidate that fits the story model beside a normal Windows desktop
on a 16 GB card (rc6 and rc7 could not install on the GPU PC: their own model check refused the model by 28 to 202
MB, and rc5 let it spill into the PC's memory): llama.cpp now places the model itself, the model file no longer
holds 13 GB of the PC's memory, and the PC was measured at every step before a line was written. It also lets you
remove what you do not want, keeps the Assistant's stories as files, backs your database up every day, and closing
Nightshift Code gives the card back at once. 0.1.15-rc7 adds what the first real Short on the GPU PC found (it was refused for black
frames, its redo died on one slow answer of Pixabay, and it downloaded 1.15 GB of footage): footage downloads are
tried again and a dead clip is replaced, a redo keeps the footage it has, the story model is no longer loaded for
steps a redo skips, and an update no longer calls a program it ended "could not be ended". 0.1.15-rc6 was the
first candidate written from a real graphics card. 0.1.15-rc5 was installed on the GPU PC on 2026-11-12 (an RTX 5060 Ti with 16 GB):
setup ran through in one go, every model answered once, a voice was designed and one was cloned, and the Assistant
answered; the GPU kit stopped at its third step and the first long video stopped at its fourth, both for faults of
Nightshift and not of the PC. Compared with 0.1.15-rc5 this candidate fixes what that day found: the model test
(`ns verify`) measures the voice's speaking and the video memory Nightshift itself left, tells a missed target from
a part that did not work, and no longer leaves its sample video on your channel; the kit goes on after a missed
target; a story job repairs a chapter that came back too short or too long instead of ending, never replaces an idea
you approved without telling you, and has one retry for each kind of trouble; a program that has ended no longer
keeps the graphics card from the next job; a download of hours survives an internet outage of minutes; the PC
check reads the newest NVIDIA driver's banner; and six items merged earlier in the block, which had no entry, are
written below (signed update packages, a damaged database replaced from a good backup, a damaged model file
fetched again by Repair, the uv an update brings, a command typed for another version's install refused, and the
Head's own fixes). No whole video has been made on a real PC yet: that is what this
candidate is for. 0.1.15-rc5 added the
items of block E, "the first real nights": a nightly run never begins work that cannot end inside the time Windows
allows the task, and says in the morning what it left and what stopped a night that died; other programs holding the
graphics card's memory are waited for and named; the upload sheet holds what YouTube accepts; every answer of Pexels
and Pixabay is told for what it is and a wrong key is found before a job starts; a simulated graphics card lets
a PC without one run the code that hands the card over; and a one-command kit, `GPU-KIT.cmd`, proves the product on
the GPU PC and packs what it found into one zip. 0.1.15-rc4 added the items of block D, "install and update
safety": an update or rollback typed in a terminal hands over to its own window, a second Nightshift on the same PC
takes nothing of the first without your yes, setup asks about the nightly run and keeps it off unless you say yes,
packages are installed from a lock, the local servers refuse requests made by web pages, and `ns` and `studio` work
in any new terminal. 0.1.15-rc3 added the items of block B (the Code console gives the graphics card back when it is
closed and Nightshift Code fits a 16K context and keeps its edit tools, pausing a channel stops its queued jobs,
Automatic runs is a switch, work whose lock file cannot be opened is no longer held for ever, assembled audio keeps
full precision, a model an install cannot use is refused up front, an updater test source, the clip captions with a
hesitation, a stage table no job rewrites, a true hint for NS-E301, and the tests that measure every join of a video
made in parts). Below is everything merged since 0.1.14; each "Not covered" line is a limit the change shipped with.
"Shown by tests" means the change is covered by the test suite and no more is claimed.

### Setup and install

- The story model fits a 16 GB card beside your desktop, and uses the PC's memory only on purpose (rc8; found on the
  GPU PC, where the model was forced wholly onto the card, did not fit beside Windows and the open programs, and the
  graphics driver spilled the rest into the PC's memory until 0.2 GB was free; rc6's stricter check then refused it
  and its update could not install). Nightshift no longer forces the model's layers onto the card: llama.cpp's own
  fitting places it, keeping 1 GB of the card free. Before each load the engine picks the fastest setting that fits
  what is free at that moment: the whole model with its speed-up draft (measured: 145.8 tokens/s beside Firefox),
  then the draft off (119.7), then part of the model in the PC's memory. The model is no longer memory-mapped, so it
  takes about 1.2 GB of the PC's memory instead of 13.7 GB; its prompt cache is capped at 2 GB. Every load is read
  back from the card; a load that spilled is stopped and loaded once more on a lighter setting. A load is refused
  only when the card and the PC's memory together cannot hold it (Windows' page file may grow, and is counted so).
  Context 16K and a compact cache are the new defaults on a GPU PC; your settings are moved once, and stay readable by
  rc5 if you roll back. `ns tune` and the model check (which an update runs) follow the same rule: the card settles
  before each try, speeds are the middle of three runs, and only the context, cache and draft are tried. Not covered:
  measured by hand on the GPU PC, not yet through the product; the Assistant's reply limit at 16K is 1,200 tokens
  (long stories go to draft files, below); the optional NVIDIA "Prefer No Sysmem Fallback" setting is not checked.
- Sizes are shown in Windows units everywhere (one formatter: 1 GB = 1024^3 bytes, as Explorer shows them; speeds in
  MB/s), the GPU kit reads the NVIDIA driver from `nvidia-smi` and no longer masks ordinary words that happen to be
  the Windows user name, and it says you may open the Dashboard and watch.

- The model test measures what it claims (verify-limits; found on the GPU PC, where `ns verify` failed a healthy
  install). "The voice speaks" is its own row (`tts.preset` and the designed and cloned voice's rows) and carries no
  speed limit. The speed is a new row, `tts.rate`: six sentences sent as one request, timed from the first sentence's
  arrival to the last, so it is the speaking that is measured and not the voice program's start; the start is its
  own row, `tts.start_s`, with a soft target that never fails the test. Before, one sentence was timed with the
  program's start and the load of its model in it, and a card that speaks at a sixth of real time was shown as more
  than twice too slow. The idle row, `vram.idle`, counts the video memory in use after the models are unloaded MINUS
  what other programs held before the test loaded anything (read once the card stopped falling); before, it compared
  the whole card with 300 MB, which no PC with a desktop open can meet. The table's last line says whether a part
  did not work or a target was missed, with the row, its value and its limit. The voice program's own lines (which
  device it chose, its timings) are kept in the log after every run. `ns verify --quick`, which setup and an update
  run, has the same rows and exit code as before, except that its idle row now also counts only Nightshift's share.
  Not covered: the speed row rests on the voice program handing over each sentence as it finishes; if it holds
  them back the row falls back to the whole request and says so, and which of the two the real program does is not
  yet seen. Shown by tests with stand-in programs.
- The model test's sample video no longer lands on your channel (verify-job-isolated; found on the GPU PC, where
  the owner's first look at the Dashboard showed a finished Short he had not made). The full `ns verify` proves the
  studio's pipeline with one sample job made from a built-in story and stand-in footage. That job now runs in a
  folder of its own, `run\verify-job` in the install, with the install's programs and its own data, Outbox, ledger
  and cache, and the folder is removed when the check ends, passed or failed, and at the next run's start if a kill
  left one. Nothing of it appears in the Queue, the Outbox, the Library, the statistics or the time estimates.
  Before, it wrote a Short with upload sheets into the Outbox of every active channel, a story, an idea and 22
  stand-in clips into the Library, and its timings into the ledger the estimates are made from. The clean-up removes
  that one folder and nothing else: a link is never entered, a folder of that name it did not make is left alone
  (and the row says to move it away), and a run whose state it cannot tell is treated as still running. Not
  covered: a folder swapped for a link in the instant between the clean-up's check and its entry, which only a
  program of your own user account that can already write the install could do; the sample job no longer loads the
  install's spaCy model (its text check uses the fallback); a sample Short an earlier candidate left is not found or
  removed for you: reject it in the Outbox. Shown by tests; the job itself with a real FFmpeg is not yet seen.
- The GPU kit's step 3 reads the model test's rows (found on the GPU PC, where it stopped a healthy install with "a
  model did not answer"). When a part did not work the kit stops and names the rows. When only targets were missed
  the step is still a FAIL, its line says that every model answered and names each row with its value and limit,
  and the kit goes on to the next step. Shown by tests; not yet run on a real PC.
- Setup's downloads survive an internet outage of minutes (download-outage). A file is given up for network trouble
  only after its tries are used AND the trouble has lasted ten minutes without progress; once one file has used
  that time up, the files after it get one try each instead of ten minutes each; when the other files are done,
  the files the outage interrupted are tried again, as often as a pass still gets one of them further. Every wait,
  every file set aside for now and every new pass is said on the screen and written to the log. Without a terminal
  (a window that is not a console, a session of a program) setup prints plain lines instead of live bars: one when
  a file starts and ends, and at most once a minute how much of the total is down and how fast. Not covered: seen
  on stand-in servers only; the real unattended route on a real line is not yet seen.
- The PC check reads the banner of NVIDIA driver 621.01 ("CUDA UMD Version"), and no longer warns that
  `download.visualstudio.microsoft.com` is unreachable on a PC that already has the Visual C++ runtime (that site
  serves the runtime and nothing else). Not covered: a translated banner is still left to the driver number.
- Setting up again after "uninstall, keep my data" now follows the answers you give the second time. The answers
  from `--answers`, from the file next to INSTALL.cmd or from the setup window win for every choice they name
  (choices they leave out stay as before), and setup lists each difference on screen. A tier named without
  `llm_custom` drops the earlier custom model. A new language model is switched in only after its file is downloaded
  and checked: until then `config/runtime.json` keeps naming the old model, so a nightly run in between still finds
  one. Ports, encoder and whisper device in `runtime.json` are kept, and setup says so. Only the channel fields your
  answers changed are rewritten (a new channel name makes a new channel and the old one keeps running), and a
  Pexels or Pixabay key in the answers is checked and stored in the Windows keyring before it is removed from the
  file. Not covered: the Choices screen is not pre-filled from the kept answers, so a kept designed or cloned
  voice is replaced by the first preset if you press Install unchanged; `install_code` and a new voice recording
  under the same voice name are not compared; a run time changed on an install finished before 0.1.15 is picked
  up only after an uninstall.
- Install folder: Browse to the folder an earlier "keep my data" uninstall left (or to an existing Nightshift
  folder) uses it as picked and no longer adds `Nightshift` inside it. A folder that already holds other files is
  no longer the default home (`D:/NightShift` and `D:/Nightshift` are one folder on NTFS); setup offers the next
  free name or drive. A typed folder with other files is still accepted, with a warning that it already has other
  files in it. Not covered: the setup window does not yet ask you to confirm before Install.
- When a download site has an outage (HTTP 5xx, 408, 425 or 429), setup waits it out for up to 10 minutes of
  continuous trouble instead of giving up after about 2.5 minutes, retrying after 2, 4, 8 ... 60 seconds (or the
  site's `Retry-After`, at most 300 s), and shows the wait ("github.com answers 504 (their side). Trying again in
  40 s, 7 min left") in the console, the setup window and the log. `ns models get` and the dashboard model download
  wait the same way. After the 10 minutes the error is NS-E105 naming the host and saying it is the download
  site's side, not this PC, and that what is downloaded is kept; any other 4xx (401, 403, 410) still stops at once.
  Not covered: downloads outside the setup plan (uv, FFmpeg, the Visual C++ runtime, `ns update`) keep their old
  short retries.
- Setup accepts only the uv version the manifest pins (0.13.6). Before, a uv found on PATH (winget's 0.13.7 was
  one) could end up in `<home>/bin/uv` and stay there on a rerun. Now a uv in the install folder is used only if
  `uv --version` run from there reports the pin; a wrong copy is replaced by stage 1's uv or the pinned download
  (SHA256 checked). A download of another version, or a uv that cannot be replaced because it is in use, ends with
  NS-E105. Stage 1 asks winget for the pinned version and stops with NS-E105 if no uv reports it. Not covered:
  `ns update`, `ns doctor` and the console wizard still use whatever uv they find first.
- One rule for the name of a designed or cloned voice, enforced where the name is typed: 1 to 40 letters, digits,
  `-` or `_`, starting with a letter or digit. Setup refuses a name such as "Night Narrator" and offers one
  ("Night-Narrator") instead of writing it and failing every first run with NS-E310; the dashboard channel routes,
  `studio channel add/edit`, the Assistant voice tools and the voice service use the same rule. Channels and
  answers already on disk with an old name still load. A channel profile that cannot be read is skipped, not
  fatal: `studio channel list` marks it UNREADABLE, `studio channel validate` prints PROBLEM and exits 1,
  `studio run --daily` prints "channel X NOT RUN, its profile cannot be read" and the nightly toast adds "N channel
  skipped, profile cannot be read". Not covered: a saved voice whose old name the rule refuses is kept, and
  creating it still fails with NS-E310; nothing renames it for you (an install that hit this must rename the voice
  in its answers file).
- The welcome text of the console (headless) setup, `OPERATOR.md` and `README.md` now say what the installer
  already checked: at least 60 GB of free disk space (they said about 40 GB), and stock-footage keys are optional.
  Text only; no installer behaviour changed. Not covered: the per-tier figures (74 GB Balanced, 81 GB Best) these
  documents gave were later replaced by the computed need described in the next entry.
- Setup works out how much disk space this install needs (`ns_common/diskneed.py`, pull request 189, already in
  rc1) instead of using fixed figures: the files it still has to download or copy (none for a file already in
  place with a good `.ok` mark), the unpacked size of the programs it still has to make, and a working reserve for
  videos (38 GB on a first install). The PC check row, the Choices check and step `s01` use that one number, and
  a drive with less free space is refused with NS-E104. A drive whose free space cannot be read never blocks.
  The welcome text now says 60 to 72 GB for a first install and that setup shows the exact number. Not covered:
  the reserve is a stated estimate (twelve weeks of videos at the default pace), not a measurement of your use.
- Getting a model this install cannot use is refused at once, with its own code (D-36). On an install whose profile
  has no voice design (or story) model, Settings > Models > Get, Library > Voices > Get voice design model and `ns
  models get voicedesign` now say NS-E122 ("This model is not made for this install's profile.") with the model and
  the profile named; before, the page said "Downloading" and then failed with NS-E109, whose hint is about a bad
  checksum. The refusal comes before anything is downloaded and writes no model state. NS-E109 keeps its meaning (a
  failed SHA256). A profile that has the files still plans them, and a `cpu-dev` install on the `reference` voice
  engine is not refused, because it can get voice design. Not covered: shown on a dev install through the route and
  the command; the toast in the window was not looked at; a `runtime.json` or `manifest.json` that cannot be read
  decides nothing here (Get goes on as before, D-35); the hint says the voice design and story models need the GPU
  profile, which is broader than the rule; the Get button is not hidden on such an install.

- Setup asks about the nightly run once, and the answer is off unless you say yes (nightly-optional, part b). On a
  first install the setup window has the switch "Run Nightshift by itself every night", the console setup asks the
  same question, and an answers file says `"nightly": true`; with no yes (an unattended install with no answer is a
  no) no task is registered, and the run time you chose waits for the switch in Settings > Schedule > Automatic
  runs. A Repair or a reinstall that keeps your data no longer changes Automatic runs: it does not switch them on
  (the rc3 entry on the Automatic runs switch said it did; that was true of rc3 only). A task that is on is
  registered again at its own time, a task you switched off stays off, and a new run time in the answers moves the
  task only if you did not change the time in Settings since the last setup. The setup window's switch is used
  only while Automatic runs still are what the window showed under it ("Now: ..."); if they changed meanwhile,
  setup changes nothing and says so. "Uninstall, keep my data" removes the task, so the setup after it asks again.
  If Windows accepts a task but Nightshift cannot record it, setup undoes the task or says plainly that Windows
  runs it at that time and Nightshift could not record it (NS-W208), and the Done screen says the same. Not
  covered: a task that was edited by hand so that it names no Nightshift folder counts as on and is left, and
  setup says it could not tell which Nightshift it belongs to; a Repair window always answers, so an untouched
  switch is worded "as you chose in setup" where "kept" would be truer. Shown by tests.
- Setup says what it does with a switched-off nightly task that is not this install's (D-41). When the PC's
  "Nightshift Daily" task is switched off, setup reads whose it is. On a first install without a yes, a task left by
  this folder or by a Nightshift folder that has been deleted is removed, its run time kept for the switch, and
  setup says so; a task of another Nightshift that still exists, or on a drive that is not there, or one that names
  no folder, is left, and setup names it and says how to move it here (switch Automatic runs on in this
  Nightshift's Settings: the PC has one nightly task, so it then runs this Nightshift, and the other one's Settings
  show Automatic runs off). A Repair removes only a deleted Nightshift's task and leaves this folder's switched-off
  task as you left it. Deleted, "drive not there" and "access refused" are told apart: only a folder proven absent
  counts as deleted, so a task whose folder Windows would not let setup look into is no longer removed. Not
  covered: `UNINSTALL.cmd` still reads a folder it may not look into as deleted, so it can remove a task that
  setup now leaves alone; when you give another install's switched-off task back, it is registered with today's
  settings, not its old ones. Shown by tests.
- A setup run with an answers file no longer asks the remaining questions in the console (D-40). `INSTALL.cmd -Answers
  <file>`, an `answers.json` next to `INSTALL.cmd`, `-NoUi` or setup input that is not a console now runs to the
  end without waiting: a key the file names (an empty `""` means no key) is never asked for, a choice the file
  leaves out takes its stated default, and a missing channel name or niche, a missing clone recording or a run time
  that does not parse stops setup with the reason. The setup window is not affected, and `INSTALL.cmd -Ui` opens it
  with your answers filled in when you want to be asked. `INSTALL.cmd -NoUi` and `-Answers` also end without
  waiting at "Press any key"; a double-click still waits so the message can be read. A failed step is still
  retried three times (after 5, 15 and 45 seconds), then setup writes the help bundle and exits with 3. Not
  covered: the "Your choices" table on screen lists every value and `config\answers.json` keeps them, but
  `install-report.json` does not mark which values were defaults; the Visual C++ runtime fallback can still show a
  Windows permission prompt in an unattended run and waits up to 10 minutes before it stops with NS-E114. Shown by
  tests.
- Setup takes the pinned programs from a `files` folder when GitHub cannot be reached (D-17). A folder called `files`
  beside `INSTALL.cmd`, made beforehand on any PC with internet by `tools\make-files-kit.ps1`, supplies uv, the
  Python build uv installs, the Visual C++ runtime, FFmpeg, llama.cpp, the voice engine, OpenCode, yt-dlp, the window
  files and the spaCy model; each file is checked against its SHA256 before it is used, and setup says per program
  where it came from. If github.com gives no answer at all and the folder holds every file this PC needs, the
  Internet row is a warning and setup goes on (before, it stopped at the Checks screen however full the folder
  was); with an incomplete folder the row stays red and names what is missing. A file that is missing, has the
  wrong bytes or is another version under the same name is downloaded instead, and when that fails too the error is
  NS-E105 naming the file. Without a `files` folder nothing changes. Not covered: PyPI (the packages) and Hugging
  Face (the models, which keep their own "Model files come from" folder) still have to answer, and PyPI silent is
  red whatever is in the folder; a kit made without OpenCode counts as incomplete unless the answers switch
  Nightshift Code off; `files\python` is matched by name only, so a stale 3.12 archive passes the look and uv then
  refuses it. Shown by tests.
- A second Nightshift on the same PC takes nothing of the first without your yes (second-install-takeover). The PC
  has one Desktop, one nightly task, one notification registration, one `NIGHTSHIFT_HOME` and one PATH. Installing,
  repairing or uninstalling a second Nightshift now changes them only where they belong to that install or to none,
  or where you said yes in that run. Setup says on its last lines and on the Done screen which install the Desktop
  shortcuts open (NS-W119); every install keeps its own full set of shortcuts in the folder `Shortcuts` inside its
  install folder; the switch "Make the Desktop shortcuts and the ns and studio commands open this install" (the
  console asks, an answers file says `"shortcuts_here": true`) moves the shortcuts, the commands and the PC's
  default `NIGHTSHIFT_HOME` together, once. Shortcuts of a Nightshift folder that was deleted are taken without a
  question, with a line that names it. `NIGHTSHIFT_HOME` is now only the PC's default: the shortcuts, the nightly
  task and `INSTALL.cmd` or `UNINSTALL.cmd` started from an install's own `app` folder work on the install they
  are in; `ns` or `studio` run from an install's own program works on that install when the variable is only the
  default, and says so in one line; a value you set in that window yourself (`$env:NIGHTSHIFT_HOME = 'D:/Other'`)
  is followed, also with one line. Uninstalling one install removes only what is its own: its task, the shortcuts
  that open it, the variable when it names it, its own commands and PATH entry (the rest of the PATH stays exactly
  as it was); what another install uses is left, each such line says "Left" and for which folder, and the
  notification registration and the stored stock-footage keys stay while another install is found. The list of
  installs (`installs.txt` in `%LOCALAPPDATA%\Nightshift`) is written whole, so another setup never reads half of
  it. Not covered: an install made before this version is not in that list until its setup runs once; a terminal
  opened before the default moved keeps the old value, so a `ns` run there works on the old install (it says so
  first) and you open a new terminal; a Nightshift folder deleted by hand keeps the Desktop shortcuts, the task and
  the variable until you say yes; uninstalling one install while another holds none of the shared things leaves
  the other's notifications unregistered until its Repair; shortcuts pinned to the taskbar or copied to the Start
  menu are not rewritten. Shown by tests.
- `ns` and `studio` work in any new terminal (FN-3). Setup puts two small command files, `ns.cmd` and `studio.cmd`,
  in the folder `cmd` inside the install folder and adds that folder to your user PATH, and its last lines and the
  Done screen say so. After that, `ns doctor --bundle` or `studio resume <slug>` typed in any PowerShell or Command
  Prompt window opened from then on runs against that install, whatever `NIGHTSHIFT_HOME` says in the window;
  `ns doctor` has a row "commands" that says which install they open. A window that was open before setup does not
  have them. The commands always open the install whose `cmd` folder they are in, so a PC with two installs has them
  from the first `cmd` folder on the PATH: setup adds this install's folder when no other existing install's comes
  first, otherwise it leaves your PATH alone and says which install the commands open and gives this install's
  files by full path (NS-W120); the same yes as for the shortcuts moves them. Setup also says so when another
  program's `ns` or `studio` comes first (the NativeScript `ns`, an Android Studio or JetBrains `studio`). While a
  plain `ns` does not reach this install, the messages, notes and pages of this install give the command file by its
  full path instead of the plain word, decided when the text is shown. A PATH that setup would make longer than
  2047 characters is left as it is and setup says so; setup never cuts your PATH. A dev install never changes the
  PATH of the PC it runs on. Not covered: another Windows administrator account has its own PATH (type the full
  path there); a path with a space is shown with its drive outside the quotes, `D:"\My Videos\Nightshift\cmd\ns.cmd"
  doctor`, which a Command Prompt and PowerShell both run; in Windows PowerShell 5.1 an argument with `&`, `|`, `<`,
  `>` or `^` and no space must be quoted twice (`'"a&b"'`), and an argument with an odd number of double quotes takes
  the rest of the line with it; 0.1.14's uninstaller does not know the commands, so after uninstalling 0.1.14 you
  remove the `cmd` folder's PATH entry by hand; uninstalling the install that had the plain commands does not give
  them to the other one until its Repair. Shown by tests.
- A damaged `config\runtime.json` no longer sticks setup, and every command names it (setup-torn-runtime, D-35, and
  one settings reader for every route). A `runtime.json` that cannot be read is set aside by setup as
  `runtime.json.bad` in the config folder, a new one is built from your answers (settings that were only in the old
  file are not carried over), the tuning runs again, and the console and the log say what happened; Retry works. A
  file saved by a newer Nightshift, or edited by hand, with settings this version does not know keeps a copy as
  `runtime.json.newer`, loses only those settings and keeps the rest; a file saved with a byte order mark or as
  UTF-16 (Windows PowerShell 5.1 writes a mark with `Set-Content -Encoding UTF8`) is written again as plain UTF-8
  with the same settings; going between a real install and a dev install keeps the old file as `runtime.json.other`
  and builds the new kind from your answers. `ns doctor` names a missing or unusable file in its `runtime.json` row
  as NS-E125 with the way out (run setup again) and no longer ends in a traceback; `ns models get`, `ns verify`,
  `ns tune`, `studio status`, `studio run`, the voice commands and the engine read the file through the same one
  reader and say NS-E125 with the file's path. Before, `studio run` and the engine said NS-E203 ("Service health
  check timed out") for a missing file and an uncoded error for a damaged one, and `ns models get` with an
  unreadable file started the 2 GB GPU voice-model download. Not covered: a file held open past the retries is
  NS-E125 and Retry works once it is released; the numbers a newer version tuned are checked for type and range
  only, which is why such a file is tuned again; `ns doctor` still moves a damaged `install_state.json` aside as
  `.bad`, as every start does, and `studio status` still makes an empty job database when there is none. Shown by
  tests.
- `ns` and `studio` with no install found refuse with NS-E123 (D-35). In a console without `NIGHTSHIFT_HOME`, on a PC
  whose default folder holds no install, any command that needs an install prints NS-E123 naming the folder it
  looked in and the install the program belongs to, and writes and downloads nothing; before, `ns models get
  voicedesign` there started the GPU voice-model download into the default folder on C:. Setup's own tools, the
  update worker, the launchers and the nightly task name their home and are not refused, and `--help` still
  answers. Not covered: a `NIGHTSHIFT_HOME` that names a deleted folder is taken as given; the dashboard and the
  engine are not checked (they are started with their home set). Shown by tests.
- Setup's voice check speaks every installed voice kind, and a voice that cannot speak no longer stops setup (P30a).
  `ns verify --quick`, which setup step s08 runs, speaks one sentence with each installed talker: the preset, the
  Base talker for a cloned or designed voice, and VoiceDesign; only the speed benchmark is left out of the quick
  run. A preset that does not speak still fails the check and switches the engine (NS-W301). A cloned or designed
  voice that does not speak is a warning row with the voice's name and the way out (Library > Voices: make the voice
  again under another name; or Channels > Edit channel > Narrator: choose another voice), setup goes on,
  `ns verify` leaves one morning note per sentence, and the update's result lists it; before, one broken optional
  voice could stop a Repair or make an update roll back. Not covered: the setup window's Voice row shows the warning
  text with no way out on screen (the morning note has it), and the Settings self-check leaves no note; a later
  talker has up to 180 seconds, which is thinnest on the reference engine with a hard disk. Shown by tests.
- `ns doctor` on a processor install does not ask for an NVIDIA driver, and the tune picks a context that fits (D-44).
  On an install whose profile is `cpu-dev` the nvidia-smi row says "not needed (this install runs on the processor:
  profile cpu-dev)" and the doctor is clean when everything works; a GPU install without a driver still fails there.
  `ns tune` picks 16384 tokens of context on both profiles, the smallest the night's jobs and Nightshift Code work
  with (every prompt is checked at its largest input and its answer limit against it); before, a 7.8 GB processor PC
  was tuned to 32768, the language server took 4.4 to 4.6 GB, and a story job timed out. An install tuned earlier
  keeps its context until `ns tune` and a restart. A clip window longer than four minutes whose answer is cut off
  (NS-E302) is asked again once, as two halves that overlap by two minutes. Not covered: the budget counts English
  text, so a channel in a script that takes more tokens (Cyrillic takes about 2.3 times as many) can send a prompt
  over 16384 on a processor install; a clip moment longer than 120 seconds that crosses the middle of a split window
  fits neither half; a story title with an emoji still becomes the outbox folder name. Shown by tests.
- `GPU-KIT.cmd` proves the product on the GPU PC and packs what it found into one zip (gpu-kit). It is in the Setup
  zip beside `INSTALL.cmd`: after you install the candidate, double-click it. It finds the install (`-Home`, your
  `NIGHTSHIFT_HOME`, the install it sits in, or it asks once for the folder), says which one it is using, prints
  its plan, and refuses an install that is not a GPU install, with the way out. It tells every step in one line
  that ends PASS, FAIL, WAIT or NOT RUN, and the steps are: (1) start, which checks this is a GPU install and that
  no other install's Nightshift is running; (2) record this PC: the graphics card, driver and video memory, the
  disk, the sleep settings, `ns doctor`, and the check of the model files (`ns verify --downloads`, skipped when
  the kit ran it less than a day ago); (3) test each model once with `ns verify` and run the proofs that fit a
  real install (`prove.py --all --strict`); (4) try each stored stock-footage key, Pixabay and Pexels, with one
  real search and one real download; (5) make one story video with the real models on the kit's own channel
  (about 3 hours); (6) cut 3 minutes from that video and make one clip from them in Clips; (7) make a voice
  designed from a description and a voice cloned from the video's narration, each speaking one sentence; (8) open
  Nightshift Code on the graphics card, send it one prompt, close it and check the card is free within 5 seconds;
  (9) ask the Assistant one question; (10) switch Automatic runs on for the next two nights if they are off, say
  the two times, and stop until you run it again after the second morning; (11) and (12) read night 1 and night
  2; (13) collect everything, put back what the kit changed and write the zip. After the steps it asks a few
  questions only you can answer (the posting sheet pasted into YouTube Studio, the video watched, the morning
  note, the two hand checks in `dev/kit/night-task-end.txt`); "later" keeps a question for the next run.
  `GPU-KIT.cmd -Unattended` is the same run for a program or an AI session that starts it without a person at
  the window: it never reads the keyboard and never waits for a key, leaves those questions open and lists them
  for the owner (a later double-click asks exactly the open ones), ends with a fixed exit code (0 done, 1 a step
  failed, 2 refused, 3 waiting: run again later, 4 another kit run holds the lock, 5 blocked, 130 interrupted)
  and prints one last line of JSON saying the step, the state, when to run again, the open questions and the
  zip; every prompt of the attended run ends by time whatever the input is. `docs/GPU_PC_SESSION.md` is the
  instruction file for such a session: the first real install on the GPU PC, the kit, and the report back. Step 5
  does not start when the video (the estimate and 30 minutes to spare) would not end before the nightly run: it
  says WAIT with the hours and the time to run it again. Resumable: close the window whenever you like, run
  `GPU-KIT.cmd` again and it goes on from the step it reached (its state is `kit\kit-state.json` in the install);
  a video job the kit started that was cut off is continued, not made again, and there is one kit channel, one
  story video, one settings change and one zip however often it runs. What it changes on the PC and puts back:
  its own channel `gpu-kit` (made from the first active channel's language and voice, active only for its video
  and the two nights, paused again at the end and on every way out); Automatic runs, switched on for exactly two
  nights only when they were off (the old value is written first) and switched off again in step 13 or by the
  night after the two, unless you switched them yourself in between (the next entry); two test voices
  (`gpukit-design-<date>` and `gpukit-clone-<date>` in the voice library), removed again unless a channel names
  one as its voice; and one stock clip per key, removed from the footage cache again unless a video used it. Left
  on the PC on purpose, and listed in the zip's README: the kit's story video, the two night videos and the clip
  in the Queue on the kit's channel, the clip's 3-minute input in Library > Sources, one Assistant conversation
  and one OpenCode session. Each of the two armed nights makes one more full story video on the kit's channel,
  besides what your channels have due (step 10 says what each has due). A night is read from its own run record
  and is a PASS only when the run's summary lists the kit's video and the night ledger has it ended "done" in
  that run, inside a known time limit, or when the night's own time rule says it did not begin or finish the
  video (that is recorded as the finding); a night that did not run, made nothing for the kit, knew no limit or
  ran past it is a FAIL with the reason. The stock keys are tried before the long video: a provider with no
  stored key is NOT RUN for that provider, and a stored key that fails stops the kit with Nightshift's own
  sentence for that answer, before any video is made. A step is NOT RUN, and nothing is started or ended, when
  someone else is using Nightshift or the graphics card: an update that is unfinished gives NS-W210 and its way
  out; Nightshift Code, a job, a waiting job or a model loading holds the card; and step 3, which stops and
  reloads every model, runs only while nobody else uses Nightshift at all. The kit ends only what it started: it
  starts Nightshift's engine when it is down and asks it to shut down at the end only when its own start record
  is still the engine that runs and nothing else is using it (otherwise it leaves it running and says why; the
  engine ends by itself after 30 minutes without use); it closes only its own Nightshift Code window by the
  process it recorded; it never sends Stop All; and a closed window stops nothing and says so. The zip is one file,
  `kit\nightshift-gpu-kit-<date>.zip`, written whole and atomically, also after a failed step, with a
  `MISSING.txt` that names each part that is not there and why; the last lines say "Send this one file" or why
  not yet. It holds the machine record, the model and proof results, the stock results, the story job's stage
  times, path, length, QC result, frame grid and posting sheet (not the video itself), the clip, voice, Code and
  Assistant results, both nights' ledgers, logs, morning notes and Windows' power and restart events, the
  diagnostics zip, your answers and the kit's state. Masked in every text file, and listed in its README: the
  stock-footage keys and tokens stored on this PC (every value, in every spelling it can take), the usual key
  shapes and any value named key, token, secret, password or authorization, Hugging Face, GitHub and Bearer
  tokens, the Windows user name and profile folder, the install's home path, the PC's name and the account's
  security id; pictures and audio are copied as they are. The kit sends nothing anywhere: it asks only this PC's
  own Nightshift. A run on a test install with the simulated graphics card (`-DryRun`, which a real install
  refuses, as it refuses an install that is not a GPU install without it) is marked SIMULATED on every result
  line and on the first line of the zip's README, and ends by saying that its zip is a test, not the GPU PC's
  proof. Not covered: nothing in the kit has run on a real graphics card or with real models; on this laptop its
  dry run gave NOT RUN for the stock keys, the clip, the cloned voice and Nightshift Code (no FFmpeg, OpenCode or
  key there), so those steps are shown by tests with stand-ins only; `GPU-KIT.cmd` itself has not been run from a
  real install yet, because block E's install proof has not run. Step 3's model test stops and reloads every
  model, so a video or Nightshift Code started during it loses its model (the kit says so first and starts it
  only while nobody else uses Nightshift); ending the engine is a check followed by a request, so something
  started in the instant between them is not seen; the stage times, the 3-hour estimate and what a night makes
  beside your own work are unknown until the GPU PC's nights have written the ledger. Shown by tests with
  stand-ins for the engine, the clock, FFmpeg, OpenCode, the models and the Windows readers, and by a dry run on a
  test install with the simulated card, not on a real one.
- A damaged model file is found, named, and downloaded again by Repair (model-file-damaged; box F-E23). Before, a
  model file damaged in place kept its size and date, so Repair skipped it; only `ns verify --downloads` hashed the
  files, with no progress shown; and no message named the file. Now `ns doctor` checks that every model file is
  there and has its size (story model, voices, tokenizer, whisper) and says when the files were last checked by
  hash. `ns verify --downloads` shows each file's progress, speed and time left, writes `logs\model-check.json`, and
  Ctrl+C changes nothing. When a model fails to load, its file is checked by hash, and one that does not match is
  named (NS-E127) in the error, the Queue's failure card and the morning note, with "run INSTALL.cmd again and
  choose Repair". Repair (INSTALL.cmd over the install, in the window or without it) now hashes every pinned model,
  says first how much and how long, sets a damaged file aside as `.bad`, downloads it again and deletes `.bad` only
  after the new file checks. A file gone together with its marker is found too. A file a program holds open is
  refused (NS-E119): Stop All, then Repair. The nightly pre-check stops a night only for files that night's jobs
  load; for another damaged file it leaves a note. Not covered: unpacked programs (llama.cpp, the voice engine) and
  a custom story model have no hash to check; the PyTorch fallback voice's weights (`models\tts\hf`) are read by no
  check but Repair (follow-up card `tts-fallback-weights-checked`); a file damaged in place with nothing recorded
  shows Done in the setup window until `ns verify --downloads` has recorded it; the nightly set is by service, not
  by tonight's jobs; a Stop All during a long hash is not worked out. Shown by tests; the card's proof (one byte
  changed in a model file of an engine-test install) has not run on a real install.

### Updates, files and safety

- A failed update names the check that failed and its message, and the update window ends with one short result line
  instead of the whole release notes (rc8). Every Dashboard start writes `logs\launch-<date>.log`, and Nightshift
  Code writes `logs\code-<date>.log`, so a start that stops says where. Closing Nightshift Code in any way (its
  window, its program ended, Release) unloads the story model at once instead of after ten idle minutes; a model that
  stops under Code is logged with the PC's free memory and commit, and loaded again. Not covered: why the Dashboard
  shortcut once failed to start the engine (17:07 on the GPU PC) and why Code's model stopped once at 16:26 are not
  known; both now leave a log.
- Your database is copied every day (the first time Nightshift opens it that day, or the first job), checked, and the
  last 14 copies are kept; a failed copy never stops work. `ns doctor` has a database row: health, size, newest copy,
  and a FAIL when no copy exists after the first day.
- You can remove what you do not want: a voice you made (refused while a channel uses it), an idea, a story or a
  footage clip, from the Library (Remove, after a confirm) or with `studio voice remove` and `studio remove idea |
  story | footage`. Nothing is deleted: it is moved to `removed\` in your Nightshift folder with a record. Reject no
  longer leaves an empty folder in the Outbox, and rejected videos no longer count in the channel's pass rate.

- An update or setup no longer refuses with NS-E706 "could not be ended" for a Nightshift program it had ended
  (rc6's last fix, found by CI under load): the stop sequence asks Windows whether the program still runs, not a
  library that called an ended program running while another program still held it. Not covered: a real update
  on a busy PC (shown by a test that holds the ended program open).
- The release page of a version with long notes is published (rc7; the rc6 release run failed with "body is too
  long (maximum is 125000 characters)"): over 100,000 characters the page names CHANGELOG.md instead of copying it.

- After an update the dashboard window shows the new interface, not the old one kept in its cache. The page,
  `app.js` and `app.css` are sent with `Cache-Control: no-cache`; only content-hashed files under `/assets/` are
  cached, and the page names its two files with a version token (`app.js?v=...`). The window host clears the
  WebView2 HTTP cache once per app version (stamp `ns-cache-version.txt` in `state/webview2`), keeping cookies
  and settings. Not covered: never tried in a real WebView2 window; if another window holds the profile open the
  clearing is skipped and retried at the next start, so that first start can still show the old page.
- An update no longer switches the nightly task back on. Before it re-registers "Nightshift Daily" (`ns update`,
  Settings > Updates, a run-time change in Settings, `ns schedule install`) Nightshift reads the task as Windows
  has it and puts back its enabled state and run time. A task you switched off in Task Scheduler stays off (the
  update notes say "scheduled task re-registered (still switched off, daily at HH:MM)") and the top bar shows
  "Automatic runs off"; `ns schedule install --enable` switches it on. If the task cannot be read nothing is
  replaced. Not covered: only the enabled flag and the first trigger's time are kept (extra or weekly triggers
  are replaced); the Settings row still says the PC wakes at that time for a task kept off; the first update
  after this release still runs the old re-registration; not tried on a real Task Scheduler.
- Automatic runs are a switch (nightly-optional, part a). Settings > Schedule > Automatic runs adds the Windows task
  "Nightshift Daily" at the run time beside it (on) or removes the task and its record (off); the time is kept either
  way. The top bar, the Queue's countdown card and the Assistant's tonight plan say what Windows has, not what you
  clicked: "Next run HH:MM", "Runs at HH:MM only while you are signed in" (NS-W205: Windows refused the unattended
  mode; "Try again" asks for it again) or "Automatic runs off"; the Queue's countdown shows a time only while it is
  on. A task you delete or disable in Task Scheduler is read as off within a minute, with one note (NS-W207), and left
  as you set it. If Windows refuses a change, the switch stays where it was (NS-E204). An update, `ns verify --quick`,
  a rollback and a backup restore never switch it on or off: the update and `--quick` settle the record before an
  older updater could re-enable a disabled task, and a rollback and a restore leave `config\schedule.json` alone. Two
  windows changing it at once wait for each other (10 seconds, then NS-E204). A dev install cannot switch it on
  ("Automatic runs cannot be switched on in a dev install."). Not covered in rc3 (changed in rc4: see "Setup asks about the
  nightly run once" below): setup registered the nightly task, so a first install started with Automatic runs on,
  and a Repair or a reinstall that kept your data switched them back on. Shown on a dev install only
  up to the dev-mode refusal (as installed: off; "on at 04:30" answered 409 and changed nothing; a time change while
  off touched no task; `ns verify --quick` passed with the task unchanged); switching on, deleting the task by hand,
  an update and a rollback were not run, because they need an engine-test install, and nothing was tried on a real
  Task Scheduler with the switch. When Windows accepts a change but `config\schedule.json` cannot be written, every
  page says off (or the old time) while Windows runs the new one, until the file can be written again; there is no
  browser test of the Settings switch;
  0.1.14's own updater still re-enables a disabled task if the one `ns verify --quick` look it calls fails.
- An update saves a copy of the settings files it may rewrite (engine settings, setup answers, install state, the
  nightly time, dashboard settings, channels, formats) as `<folder>.prev-config.zip`, and `ns update --rollback`
  puts them back with the old program. Settings changed after the update are lost on rollback and the result
  names the files; a second rollback goes forward with them. A copy that cannot be saved or read stops the update
  (NS-E702, NS-E115 from INSTALL.cmd). Not covered: the hop 0.1.14 to 0.1.15 by `ns update`, `--github` or Settings
  > Updates saves no copy at update time (the old updater runs; the first start of the new version saves it, see
  the next entry); `secrets.dpapi` is never copied.
- First start after an update made by an older updater takes the settings copy itself (F-10c, pull request 206;
  `ns_common/first_start.py`). 0.1.14's updater saves no copy, so the first time 0.1.15 is about to write a
  settings file (engine settings, setup answers, install state, channels, formats, schedule, dashboard settings) and
  finds a program backup (`<folder>.prev`) with no copy of its own, it saves the settings as the older version left
  them, then goes on; once per process, with no file access afterwards. If the copy cannot be saved (a locked file,
  a full disk, a copy of another version in its place) Nightshift still starts, writes one line to
  `logs\first_start-<date>.jsonl` and records the failure in `.ns-first-start.json`, so a later start does not
  save settings the new version has already changed as the older version's. A setting a newer version added is
  written to its file only when you use it (`ns_common/config_compat.py`, `NEW_FIELDS`), and `ns update --rollback`
  with no usable copy takes out of the settings every such setting added since 0.1.14 and names each one. Not
  covered: a file the rollback cannot change (open, read-only) keeps its new settings and is named; going back to a
  version older than 0.1.14 checks only the settings added since 0.1.14; a tiny-story job's `plan.json` (engine-test
  installs only) cannot be resumed by 0.1.14 after a rollback; the end-to-end rollback on a real install is still
  to be shown by QA.
- `ns update --rollback` is safe to interrupt (pull request 206, `ns_common/rollback_intent.py`). It first writes
  `<folder>.rollback-pending.json`, then makes the settings files ones the older version reads, then swaps the
  programs. Whichever version is in `<folder>` afterwards loads its settings; the next start of the newer version
  undoes or finishes the rollback (and says so in its log), and `ns update`, `ns update --rollback` and
  `INSTALL.cmd` refuse, changing nothing, while the settings of an interrupted rollback are not back. Not covered:
  a power cut between the two folder renames leaves no `<folder>`, and in rc3 nothing could start until you renamed
  `<folder>.swap` back (rc4's INSTALL.cmd puts it back: see the entry on an interrupted rollback below); 0.1.14 itself cannot finish an interrupted rollback;
  a window that is already open when a rollback runs saves at once, into the settings the rollback is changing
  (only a window whose first save falls in the rollback waits, up to 60 seconds, once).
- Your API keys on a drive without file permissions (FAT or exFAT; pull request 193). Windows cannot lock
  `config\secrets.dpapi` there, so rc1's save failed with NS-E120. Now the keys file is saved without a lock and
  you get the warning NS-W116 once per install (setup, Settings > Stock footage keys, or a morning note at engine
  start; `config\keys_unprotected.json` records that it was given). A restore skips only the keys file when it
  cannot be locked there, and says so; `ns backup` to such a drive exits 0 with the keys file left out and
  prints a warning, `backup-manifest.json` records `left_out`, and Settings > Export shows the note. `ns backup`
  prints the code, message and hint of NS-E120 instead of a traceback when a lock fails on a drive that does keep
  permissions. If Windows cannot say whether the drive keeps permissions, it is treated as keeping them, so a
  failing lock is still NS-E120. Not covered: the warning shown under the keys is kept in the dashboard's memory
  and is gone after a restart (the marker file stops it repeating); a notice that fails to show is not given
  again; the file is still encrypted for this PC but any account on the PC can read it.
- A job or night whose lock file cannot be opened right now (an antivirus or backup program holds it) is no
  longer treated as finished (pull request 193, `ns_studio/ownership.py`). It stays "running" until three probes in
  a row have failed and the process that started it is gone; a record with no process number counts as dead an
  hour after it was announced; a process that is still alive keeps it held. While it is held for that reason you
  get one morning note with NS-W117 and `studio status` shows a `held:` line. Not covered: nothing is closed or
  restarted for you while it stays held; a job or night whose owner record is missing or cannot be read no longer
  stays held for ever (see the next entry).
- Work whose lock file cannot be opened is held for at most an hour, not for ever (lock-bound,
  `ns_studio/ownership.py`). It stays "running" while its process is running and is not shown to be another
  program: a process that started after the work was recorded is not its owner, a process number Windows reports as
  holding the lock file counts as the owner whatever the clocks say (the Restart Manager answer is kept for a minute),
  and a running process whose start time cannot be read is held too, without proof: its NS-W117 note says the process
  may be another program and does not tell you to end it. In every other case the hold ends: three failed looks in a row
  once the process is gone, or an hour after the work was last known to start (a record with no start time counts from
  when its record or lock file was last written). You get one morning note with NS-W118 ("Nightshift cannot tell
  whether this work is still running, and no process is known to be running it"), and after the hour the work counts
  as interrupted: the next start of Nightshift or the next nightly run closes it, and its Queue card says how to
  continue it (usually Retry). Not covered: work whose own process is still alive stays held for as long as it lives,
  and Release in the Queue does not reach it (it frees the graphics card only; end that process or restart the PC);
  shown on an install by making a lock file unreadable with `icacls`, not with a real antivirus or backup program.
- A file Nightshift saves is no longer written over in place while another program reads it: the diagnostics
  zips, the backup zip and restored files, the config and state files, the job `stage_state.json` and the outbox
  videos and sheets are written beside the file and swapped in whole, and readers retry while Windows refuses an
  open. If a player, sync client or virus scan keeps the file open for 15 s, the save fails with NS-E119 and the
  old file stays whole. In 0.1.14 these were written in place, with no swap at
  all: the zips (diagnostics, backup), restored files, the outbox videos and sheets and the job
  `stage_state.json`, so a reader or a power cut could meet a half-written file. Not covered:
  some readers (`svc_outputs`, formats, the clip layout, the Settings preview) and writers (`ns_supervisor/tune.py`,
  `policy/regen.py`, `clip_sources.py`, the outbox folder marker) are not converted; stale `~ns*.tmp` files are
  swept only in the outbox, diagnostics, `config/`, `run/`, `%TEMP%` and the setup data folder (at engine and
  setup-server start, and when those folders are written to), nowhere else; text files written through the
  default call now get LF line endings where Windows wrote CRLF (the job, stage, schedule and state JSON files), while the outbox
  sheets and sidecars, the clip sheets, the workshop files, the calibrate story texts and report and the `.sha256`
  files keep the platform's CRLF; the pull request notes that a job resumed across the update may therefore run a
  later stage once more.
- The launchers and the setup's hand-over files are swapped in whole too, so a reader never meets an empty file:
  the window host's run file in `run/`, `config/window.json`, `config/opencode.json`, the WebView2 cache stamp,
  the setup preflight and folder-dialog files and the calibrate outputs (`stories.csv`, `scores.csv`, story texts,
  `calibration_report.json`). Stop-all no longer deletes the run file of a live window, and an interrupted second
  `studio calibrate` no longer truncates `stories.csv`. Leftover `~ns*.tmp` files older than an hour are swept from
  `run/`, `config/`, `%TEMP%` and the setup data folder at engine and setup-server start. Not covered: the
  WebView2 profile folder is not swept; after the retries (about 0.1 s) a refused open still reads as "no window";
  `studio calibrate` replaces its files one by one, not as a set.
- The file that holds your stock-footage keys for the night run (`config/secrets.dpapi`) now really stays
  readable by you and SYSTEM only. Any account on the PC can decrypt that file, so its permissions are its only
  lock; 0.1.14 asked icacls for "<USERNAME>:F", ignored the answer, and on an account icacls could not map (an
  Entra ID account, for example) left the file open to Users with no message. Now your account's SID is granted,
  the empty temp file is locked before any key goes in, the result is read back, and a lock that fails stops the
  save with NS-E120 (old file untouched; the key stays in Credential Manager). At engine start a `secrets.dpapi`
  an older version left open is locked. Setup shows a failed lock as a notice and in its summary, and Settings >
  Stock footage keys shows the code and hint under the keys. `ns backup` and Settings > Export lock the zip when
  it holds the keys file (a failing lock raises NS-E120 and leaves no zip), and a restore writes the keys file
  through the same lock (NS-E703 names NS-E120 and the restore rolls back). Not covered: on a drive that takes no
  permissions (FAT, exFAT) or where AppLocker blocks icacls, every key save and any restore of a backup holding the
  keys file now fails, and `ns backup` fails with NS-E120, with no switch to leave the keys out (pull request 193,
  in rc2, changes that; see the entry on drives without permissions); the warning under the keys is kept in the
  dashboard's memory and is gone after a restart.
- An update zip is checked before it is put under its name. `ns update --github` and the dashboard download the
  zip to a temp file, hash it against the release's SHA256 and move it to `data/updates` only when it matches; a
  mismatch is NS-E701 and nothing is saved. Settings > Updates > From a file does the same: an empty `.sha256`,
  one that is not 64 hex digits and an empty zip are refused, and a zip that does not match is refused with
  "NS-E701: SHA256 mismatch for <zip>: expected ..., got ...; the file was not saved." The older pair of that
  name is left as it was. Not covered: if a program holds the old zip open, it and its checksum stay (NS-E119).
- Release candidates: a tag like `v0.1.15-rc1` is published on GitHub as a prerelease and never as "latest", and
  the updater refuses any release whose data says prerelease or draft (NS-E701), so no PC is offered a candidate
  as an update; `rc2` sorts before `rc10`. The release workflow accepts only `vX.Y.Z` or `vX.Y.Z-rcN` (N from 1).
  Not covered: a candidate install fetches the voice engine from the previous final release, and "What is new"
  finds no notes for a candidate (it looks for a heading with the candidate's own number).
- A nightly killed by a power cut or crash is closed as interrupted, resumed, and no longer silences its channel.
  Runs and jobs hold a lock file in `data/owners`; engine start, `studio run` and the nightly close any whose
  owner is gone. The half-made job shows in the Queue as a failed card with NS-E328 (Retry continues it), the next
  nightly continues it before planning, a morning note says what became of it, and the "Last night" card shows
  "Interrupted". A lock file that cannot be made (5 tries in about 1 s) refuses the
  night with NS-E327 ("Something stopped" on the card) and a note. Not covered: a 0.1.14 nightly job that failed
  before the kill is resumed once; the refusal has no code of its own.
- Closing the Nightshift Code console any way (its X, the process ended, signing out) gives the graphics card back
  within one engine monitor pass, so that night's run starts instead of waiting two hours and giving up with NS-E201
  (F-H47). The launcher sends its own process number with its request for the card; the engine records it with the
  process's start time and frees the card when that process is gone (a process number reused by another program is not
  the old one). A console that is open but idle keeps the card as before, a console that was closed while it waited
  for the card no longer takes it when its turn comes, and the Studio and Assistant holds, and Release in the Queue,
  work as before. Not covered: not shown on an install: on a dev install the language model is not a graphics-card
  service, so the card is never recorded as held and no process number is watched (the closing of a real console is
  for QA on a GPU install; the tests use a stand-in process); the process watched is the launcher's PowerShell, so
  ending `powershell.exe` alone frees the card under a still-running OpenCode; two Nightshift Code consoles share the
  owner name `opencode`, so closing either frees the card and the other loses its model.
- Nightshift Code's first question fits a 16K context (D-34). Setup now writes `config\opencode.json` with the context
  and output limits of the install (16384 and 4096), the to-do, web fetch, task and skill tools off, a short
  Nightshift prompt in place of OpenCode's long one, and the model named after the installed file; the read, edit and
  write tools stay on. Measured with the pinned OpenCode 1.19.5 against a recording stand-in for the language model:
  the first request is 14,579 characters, about 4,165 tokens at 3.5 characters per token (7,125 at the 0.1.14 ratio of
  2.046), against a budget of 8,192; 0.1.14's was 15,317 tokens counted by llama.cpp. Every start of the launcher
  brings an older file up to this (keeping every key you added, and removing the `"patch": false` an earlier build of
  this release wrote, because in this OpenCode it also switches the edit and write tools off), and takes the context
  from the number the running engine uses. With a context under 16384 Nightshift Code does not start: it says
  "Nightshift Code needs 16384 tokens of context; this install has N" before it takes the graphics card; a test
  install at 8192 is refused, and the nightly run uses that same context, so a bigger one needs more memory for every
  job. To make Code read-only set `"edit": false` under `"tools"`. Not covered: the token numbers are estimates from
  characters, and no real model has answered a question under this setup; the install proof ran the pinned OpenCode
  and the refusal on a dev install (which writes no `opencode.json` of its own, so the file came from setup's own
  function) and found that the first version of this change had removed the edit and write tools; that is fixed here
  and measured again with the figures above, but not repeated on an install; setup run again writes the file new and
  drops keys you added.
- An interrupted job is described truthfully in every process and for every way it was started. `studio status` and
  the Dashboard's recovery named the stage after the one the job stopped in (a job that stopped in `policy_text` was
  shown as "stopped at stage voice") because a process that only reads progress loaded no stage modules; the stage is
  now named from the job's own complete plan wherever it is asked, with no set-up call. A job is called "the night's
  run" only when the nightly started it: a job you started yourself (with `studio run`, Run now or a dashboard
  request) is described as "A job you started yourself was interrupted" in the morning note and on its card, and no
  nightly run is promised for it; before a Retry card exists, `studio status` names the command that works now
  (`studio run --resume <id>`) and says when the card will come, and a card settled by `studio run --resume` says that
  instead of crediting the nightly run. Where a card, the night's note or `OPERATOR.md` promises that the next nightly
  run continues a job, it now says "while Automatic runs are on" and names the Automatic runs switch. Not covered:
  cards already on screen keep the words they were written with; the install proof met the old wording and the wrong
  stage, and these fixes are shown by tests (one per origin: the nightly, `studio run`, Run now, a dashboard request,
  a series part, a clip) and were not run on an install again.

- Stopping Nightshift for an update, a rollback or a Repair always ends (D-46, D-48). The install proof of this
  version found a rollback refused because Nightshift's own engine hung while exiting, and Nightshift was left
  down. Now the engine exits within 20 seconds of being asked; the update waits for Nightshift's own programs,
  showing "Waiting for Nightshift to stop: ...", ends what belongs to THIS install and is still there, and refuses
  (NS-E706) naming only a program that is not Nightshift's. A refusal starts Nightshift again and says so; when
  Nightshift's own program cannot be ended it says to restart the PC. Setup's Repair does the same over 0.1.14
  and the earlier candidates. An interrupted update that was finished or repaired now reads as finished
  everywhere, and nothing starts a job or the engine while one is unfinished (NS-W210), whichever way the program
  was started. A rollback that had to end an engine which no longer answered starts Nightshift again and its
  last line says so (D-51). Shown on an install of this candidate's code before its last two fixes (D-50, D-51):
  12 terminal rollbacks of 12, an engine hung in its own exit, and a foreign program holding a file; the D-51
  case itself is shown by tests only.
- An update or rollback typed in a terminal now hands over to a separate window (D-39). `ns update <zip>`,
  `ns update --github`, `ns update --git` and `ns update --rollback` return at once and open a window, "Nightshift
  update", that shows each step and the result; closing the terminal or that window stops nothing. The command
  exits (with 0) before the result is known: the result is the last line of that window and of the log
  (`logs\update-<time>.log`), and the entry of `run\update_state.json` whose `log` is the one the command printed.
  With `--detach` no window opens and the command prints a JSON line with the worker's `pid` and a `follow` script:
  run it with `powershell -NoProfile -ExecutionPolicy Bypass -File <follow>` to watch the progress; it exits 0 when
  the update is done and 1 otherwise. A script that needs the result runs `--detach` and then `follow`. `--wait`
  is refused (exit 2) when `ns` runs from Nightshift's own Python environments, because it would hold the files the
  update replaces; before, a terminal `ns update` held its own `ns.exe`, and a rollback could fail with "rolled
  back" while the disk held something else. Now only one update, rollback or repair runs at a time (NS-E705), and
  nothing starts while a program still runs from the Python environments (a Studio window, a terminal running
  `ns` or `studio`, Nightshift Code, a Python prompt): after up to a minute NS-E706 names each program and nothing
  is changed. If the environments cannot be refreshed after the program was changed, the message says which
  program is in place, which environment is left and how to finish, and the update does not start Nightshift: the
  progress window opens on every route (also from Settings > Updates), a note waits on the Queue page, and Settings
  > Updates shows the failed update or rollback until the next one. The commands such a message names are the
  `--detach` forms, because after a rollback the `ns` you type is the older version's; for a version that has no
  `--detach` it names INSTALL.cmd. Before it changes the program, an update or rollback writes `run\venv-repair.json`
  (the uv commands that fill the environments) and deletes it after the last one; if it stops in between (a program
  held a file, the packages were unreachable, the power went off), the next start from a Desktop shortcut or the
  nightly task runs those commands first and starts Nightshift only when they worked, otherwise it says why
  (NS-W210) and starts nothing. Nothing starts from a shortcut while an update runs, and the nightly task waits up
  to 30 minutes for one. INSTALL.cmd and Repair take the same lock and refuse while a program runs from the
  environments (an unattended setup exits with 15 when an update, rollback or repair holds the lock and with 14
  when programs still run or cannot be listed). When a program holds the record open and Windows refuses to delete
  it for 15 seconds, `run\venv-repair.done` marks it finished instead; only when neither can be written does the
  update stop before its check and name Repair, keeping the program it put in place. The Diagnostics shortcut still
  works after a failed repair: when `ns doctor --bundle` cannot run, it makes a smaller zip without Python (logs,
  install state, version, the repair record and update state; no settings or keys) and says which zip it made.
  Not covered: from 0.1.14 and the 0.1.15 candidates up to rc3 their own terminal `ns update` still fails on its
  own `ns.exe` and cannot be changed, so update or go back with Settings > Updates, the Update shortcut or
  `ns update ... --detach`; an environment an older updater left on another version has no record and is only named
  (by `ns doctor`, row "python environments", and at each start), with Repair as the way out; a program the holder
  check does not see (a path given as `--opt=<path>`, an 8.3 name, a junction home, an elevated process) ends at
  the retries and the "not finished" message; a record kept read-only or refused by an access rule makes the next
  update stop with NS-E702 and no way out named (an outside program has to cause it); a start in the few renames
  between the record and the swap is let through. Shown by tests.
- `studio run`, the voice commands, `studio dashboard` and `ns verify` read the repair record before they ask the
  engine (D-39b with default-port-tests, D-48). While a record waits, `studio run` and the voice commands refuse
  with NS-W210 whether or not the engine answers; `ns verify` and `studio dashboard` refuse when the engine is down
  and go on when it answers. All of them say that an update or rollback is running now when one holds
  `run\update.lock`, and go on when nothing waits; with
  nothing waiting the check is a file look and no connection, so the footage-key and no-channel refusals still come
  before any job, with their codes and messages unchanged. Not covered: a record held open by another program is
  waited for a moment, and `install_state.json` is read when the engine is up; the engine's own start refuses
  before it takes its lock, so a start with a record waiting while an engine already runs would say "not started"
  (the updater writes a record only after stopping the engine, so no route is known). Shown by tests.
- A rollback or update that is interrupted at any point leaves a program folder that can be put back (206-carried,
  with launcher-reach below). A stale record of a failed restore now marks the settings copy as not restored, so a
  later update keeps the copy; a leftover `app.swap` is settled and said (put back as `app`, kept as `app.prev`, or
  renamed `app.swap-left-<date>` and never deleted, with `ns doctor` row "program folders" naming it and the way
  out); a `VERSION` that is missing, empty or garbage counts as older than every release, so the rollback takes out
  every setting added since 0.1.14 the same way each time and says the version "could not be read"; a setting whose
  accepted values grew since 0.1.14 cannot ship without a rule that writes its fallback when you go back; and
  INSTALL.cmd run after a power cut between the two folder renames (no `app` folder, `app.swap` holding the newer
  program) puts the program folder back, tells you to start Nightshift once so the rollback is undone from its
  record, and copies nothing. This replaces the rc3 limit that nothing could start until you renamed `app.swap`
  back. Not covered: INSTALL.cmd of 0.1.14 or rc2 does not settle `app.swap`; with no `app`, no `app.swap` (moved
  away by hand) and a record, INSTALL.cmd makes a fresh program and the next start calls the record stale, so the
  settings from before the rollback are lost (the log says so); a swap folder held open is left and named, and
  only tried once per start. Shown by tests.
- Every Desktop shortcut and the nightly task start Nightshift through `<home>\nightshift-launch.ps1`, however the
  install reached this version (launcher-reach). Setup writes that script, and the first start of this version
  copies it and rewrites the six Desktop shortcuts and the task action of an install updated by 0.1.14's own updater,
  which writes none. When `app` is missing the script says so every time, at 03:00 too: a file `Nightshift did not
  run.txt` on the Desktop, a Windows notification, a log line (NS-W209), and after the repair a morning note that
  names the missed nights; the note on the Desktop and the missed-night record are cleared at the next start of
  the engine, not only by the next shortcut or night. Only shortcuts that belong to this install are rewritten, in place, keeping their name and notification
  id; another install's, or one on a drive that is not there, are left (shortcuts of a deleted Nightshift are taken
  only by setup, as described above); the task keeps its logon, time and on or off state. A home path with an apostrophe,
  brackets or non-ASCII letters is covered by tests. The engine's PowerShell helpers open no windows when it runs without
  a console. `ns doctor` has a row "launch script" that fails when a shortcut or the task could not be pointed at
  the script; Repair fixes it. Not covered: taskbar pins and Start menu copies are not rewritten, and a deleted
  shortcut is not made again; Windows may refuse changing the task of an install when the engine is not
  elevated (the task then stays as it was, the row names it, and the next start tries again); a shortcut change
  shows a `~nslnk-<pid>.tmp` file on the Desktop for a moment. Shown by tests.
- Packages are installed from a lock (P29a). A committed `uv.lock` records the packages (all from PyPI, with
  SHA256) that this version was tested with; setup, a Repair (INSTALL.cmd again) and every update from a locked
  version install exactly those versions, each download checked against its hash, instead of the newest ones in the
  allowed ranges on that day. The lists are kept as `logs\pins-<environment>.txt`. A package whose lock is missing
  or does not belong to it is refused with NS-E124 before anything is changed; a hash or index failure is NS-E105
  with uv's lines. `ns update --rollback` to a version from before the lock installs that version its own way.
  Torch, the CUDA wheels and the spaCy model stay on the manifest's own hash pins and are not in the lock. Not
  covered: an install updated by 0.1.14's own updater to this version gets floating packages (that updater knows no
  lock), and stays unlocked until its next update or a Repair; the lock pins no Python build; a mirror (`UV_DEFAULT_INDEX`) works only when it serves PyPI's exact bytes.
  Shown by tests.
- The local servers refuse requests made by web pages (FN-1). The engine (port 8099), the voice service (8091) and
  the dashboard answer only requests addressed to 127.0.0.1 or localhost; the engine and the voice service answer no
  web page at all, and the dashboard answers a browser only from its own page: the same scheme, name and port the
  request was addressed to (D-45). A page served by another program on this PC, on another port or under the other
  name, is refused for reading as well as for changing anything. The dashboard also answers 415 to a body that is
  not JSON on its JSON routes. Before, a page you visited could send a plain form POST to the engine and stop the
  night's services. A refusal is NS-E207 and leaves one morning note naming the page (at most three pages and one
  "more" a day, counted across the servers); a page opened from a file is named as such. Nightshift's own programs,
  the dashboard page and its uploads work as before. A script of yours must address 127.0.0.1 or localhost and send no
  Origin, Referer or Sec-Fetch-* header, with a JSON body or none; a tool that must post a form to the dashboard
  sends an Origin equal to exactly the address it called (scheme, name and port). Not covered: the setup
  server keeps its own check; llama-server and the embedding service are not guarded by this; update
  packages are still checked for integrity only (the SHA256 beside the zip proves it arrived whole, not who made
  it), not signed; a browser too old to mark its own requests (Firefox before 70, Edge's IE mode) is taken for a
  program, and can send a request with no body to the routes that take none (stop everything, start a check); the
  refusal note counts "at least" what it had seen when it was written (D-50); the fake services of fake installs
  have no guard. Shown in a real browser (Edge) on an install of this candidate's code before its last two fixes:
  every request from a page on another port and from a file page was refused and nothing ran.
- Morning notes have one owner, and Mark all read removes only what you saw (FN-2). Every note any part of Nightshift
  leaves goes through one module under one lock (the launch script takes the same lock), so two notes written at the
  same moment are both kept, and Mark all read removes exactly the notes the bell showed; a note that arrives
  while the page is open waits for the next look. Notes waiting to be written are kept as one small file each in a
  spool folder (at most 50) until the next writer moves them in. At 200 waiting notes the oldest are dropped and one
  note says how many were removed unread, so the newest note that matters is never the one lost; a routine note
  (such as "ready for review", which the Queue and Library already list) is kept out instead of pushing out a note
  that matters, and the count includes it. Not covered: 0.1.14 reads this file, but its first write keeps only 20
  notes; "oldest" is the place in the file, so a note moved in from the spool ranks as new; a note can be delivered
  twice when its spool file cannot be removed and you dismiss it meanwhile (rare); on a full disk only a log line
  is left; Mark all read answers "busy" when the file stays held for 5 seconds. Shown by tests.
- Five owner texts that were false in some state are now true in each (FN-4). A busy graphics card (NS-E201) no
  longer promises a nightly run that will not come: the Queue, the toast, the paused-channel text and the Assistant
  say "while Automatic runs are on in Settings", NS-E201 says the card was in use by another task or another model of
  the same task, and the way out for a job stopped at night is to open the Nightshift Studio shortcut and type
  `run --resume <id>` (Retry on its card where it has one). A failed switch-off is NS-E206 and says how to switch
  the task off in Task Scheduler yourself, NS-E204's hint opens with Settings > Schedule > Automatic runs, and the
  Settings key dialog says a key is kept in both Windows Credential Manager and `config\secrets.dpapi` on this PC. The
  uninstaller says what happened to the stock-footage keys and the secrets file, per case with a way out by
  hand, and leaves Credential Manager while another Nightshift is found. After an update, "the nightly task was
  switched off or removed in Task Scheduler" is said only when that is what happened; when another install holds the
  task or its folder is gone, the text names whose it is. Not covered: the case where Credential Manager fails and
  the file succeeds still gives no warning; eight owner hints longer than 400 characters are not shortened; an
  install the uninstaller cannot see (set up before the install list existed and holding no task, shortcut or
  variable) loses its Credential Manager keys when another install is uninstalled and keeps its own
  `secrets.dpapi`. Shown by tests.
- A nightly run plans inside the time Windows allows it and never begins work that cannot end inside it (night-fits,
  part a). Windows stops the nightly task 6 hours after it started it, whatever it is doing. A run the task started
  now works to a deadline 10 minutes before that (kept for the report, the publish times, the backup and the morning
  note), counted from the task's own start, which the launch script hands to it. Before each new video, each
  half-made video it would continue, each wait for the graphics card and each stage, it asks one question with four
  answers: begin it; stop here for tonight (the finished stages are kept, and the next nightly run goes back to the
  video before it starts new ones); do not start it tonight (the channel stays due and is planned again, first); or
  give it up for the nightly run, because one of its stages cannot fit a night on this PC. The times it plans with
  are this PC's own: every stage, every wait for the graphics card and every model load is written with its seconds
  into a night ledger (`data\ledger\ledger-<yyyymm>.jsonl`, in the diagnostics zip), a stage is planned at the
  longest of its last 5 passes, and until a type of video has been timed here Nightshift plans 3 hours for a full
  video, 2.5 hours for a part of a series and 1 hour for a Short or any other job, so a first night makes less
  rather than being stopped half way. A video that needs more than the night has left can still be begun as the
  night's first work and is then made over several nights. The wait for the graphics card is no longer 2 hours
  whatever the hour: it ends early enough for the next stage to fit. A run you start yourself (`studio run`, Create,
  Retry) has no deadline and is never refused for time. What you read: when work was left, the morning note says how
  long the night took ("Last night's run used 5 h 40 min of the 6 hours Windows allows it"), then one sentence for
  each video that was left, begun over several nights or given up, with its numbers and its way on, and ends with
  NS-W211; the Queue's Last night card shows the same sentences, and a video stopped for time has its sentence on
  its Queue card. "Cannot fit" needs evidence that survives one odd night: 2 of the stage's last 5 passes took
  longer than a whole fresh night has here, the newest among them (one such pass alone is tried once more, and only
  as a night's first work), or a half-made video was the night's first work on two nights in a row and each time had
  too little time left for its next stage. The half-made video is then left for Retry on its Queue card, new videos
  of that type are not started at night, the note says so once and the Last night card every night, with the ways
  out (make them from Create; for full videos also Default type > Shorts only in Channels), and it heals without a
  setting: one pass of that stage inside a night's time, from Retry or from a video made in Create, lets the nightly
  run make them again. A half-made video that no night has moved forward for more than 3 days is left for Retry too.
  `ns doctor` has a row "nightly task limit" that warns when the task registered in Windows stops earlier than the 6
  hours each night is planned inside, and says how to register it again. Not covered: shown by tests on a clock the
  tests move, not on an install and not with real stage times: the 3, 2.5 and 1 hour figures are guesses until the
  GPU PC's first nights have written the ledger; an estimate can be too short, and the stage under way then runs
  past the deadline (the next entry says what follows); the ledger does not record how long a story is, so the
  longest of the last five passes stands for every length; a type of video refused again on later nights is said on
  the Last night card, not again in the note; `NS_TEST_NIGHT_LIMIT_S` (60 to 21600 seconds) shortens the limit for a
  proof, on a dev or engine-test install only.
- A night that died says what stopped it (night-fits, part b). When a nightly run is found dead (when the Dashboard
  starts, when `studio` starts, or by the next night), the morning note and the job's Queue card no longer say
  "power cut, crash or restart" for every case. Nightshift reads what it recorded itself (the night ledger, a mark
  it writes 2 minutes before Windows' limit while the run still works, and a beat file it rewrites every 30 seconds
  while the run lives) and, read only, Windows' System event log around the last beat (`wevtutil`, events 1074,
  6006, 6008 and 41, at most 20 events and 5 seconds), and says one of these: "Windows stopped the night's run at
  <hh:mm>, at the 6-hour limit it allows the nightly task"; "Windows restarted the PC at <hh:mm> to finish
  installing updates, during the night's run" (the second such night within 14 days adds, once, where to set Active
  hours in Windows Update: NS-W212); "The PC lost power or stopped unexpectedly at about <hh:mm>, during the night's
  run"; "The PC was shut down or restarted at <hh:mm> (asked by <program>), during the night's run"; or, when the
  evidence explains nothing, when the run was last known alive and exactly what is missing (the PC did not restart,
  so the run's own process was ended; the event log could not be read; it names nothing at that time; or it holds
  more events than Nightshift reads). A cause is said only on its evidence: without a beat no Windows event is tied
  to the run, and a night of an older version, which left no evidence, keeps the old words. Each sentence goes on
  with the video and the stage it was at, and how it continues (the next nightly run while Automatic runs are on, or
  Retry). Sleep: when the PC slept or hibernated during the night's run (Windows' own count of the time asleep, read
  at every beat), the ledger gets a `slept` line, and a stage that fails within 2 minutes of the wake is not a
  failure: the video is interrupted and continued by the next nightly run, and the note says when the PC went to
  sleep and woke, and that the run keeps the PC from falling asleep when idle but not from Sleep chosen in Start,
  the power button, a closed lid or a low battery. A night whose stage under way ran past Windows' limit and that
  then ended by itself says so in the morning note ("worked 6 h 35 min, 35 min past the 6 hours Windows allows the
  nightly task"). Not covered: it is NOT known whether Windows ends the whole process tree of the nightly task at
  the limit, or only the program it started (`cmd.exe`, which started PowerShell, which started `studio`):
  Microsoft's documentation does not say, the tests show both answers with stand-in processes, and only a real task
  on the GPU PC can show which one Windows gives (`dev/kit/night-task-end.txt` is the check to run there). If only
  `cmd.exe` is ended, the night runs on past the limit, by at most the stage under way, because no stage or wait is
  begun that would end after the deadline. Not known either: whether modern standby counts as sleep (the same file
  has a step for it). An FFmpeg that a stopped night left running is not found and ended by this change; a night
  that hangs keeps writing its beat and is not called dead; a restart you chose yourself with "Update and restart"
  also counts as a restart for updates. Shown by tests with a fake event log made from a capture of a real one and
  with process trees ended by their recorded pids, not on an install: no sentence here has yet been produced by a
  real stop.
- Other programs holding the graphics card's memory are found before a model loads (vram-busy). Right before every
  start of a model on the card (the nightly run, `studio run`, a dashboard job, Retry, the Assistant, Nightshift
  Code, a voice preview or a new voice, `ns verify`, the warm-up) the engine reads the card's memory and compares
  what is free with what that model needs plus 512 MB. What a model needs is measured, not guessed: after each load
  the engine records how much the card's used memory grew (`run\vram-need.json`; the largest of the last three loads
  of the same model, context and flags), and a model that was never measured on this PC is always tried, and
  measured. When a measured model does not fit because of memory that other programs hold, a start you made yourself
  (`studio run`, the Dashboard, the Assistant, Nightshift Code, a voice preview) is refused at once with NS-E208,
  and the nightly run waits: it looks again every 2 minutes for at most 30 minutes, never longer than the night's
  own time decision gave it, and the Queue says "Waiting for the graphics card's memory: other programs are using
  9.1 GB of the graphics card's 15.9 GB (game.exe, chrome.exe; Windows does not say how much each uses), and the
  story model needs 10.4 GB (measured on its last load). Close them and the run goes on." When the memory does not
  come free, the night keeps its videos for the next night (the job stays interrupted, not failed, and the rest of
  the night is skipped), and one morning note with NS-E208 says what held the memory, how long Nightshift waited and
  what to close. What is named: the programs Windows lists on the card, by process name only (never a path or a
  window title), five at most; Nightshift's own processes and Windows' own are never named. What cannot be named or
  counted: under WDDM, the driver mode of a Windows desktop PC, Windows does not say how much memory each program
  uses, so the sentence gives the total held by others, names the programs without numbers and says so; when Windows
  lists no program it says "Windows did not say which program". A model that needs more than the card holds even
  when idle is never blamed on another program: it is tried, and if it does not start the error is NS-E203 with the
  numbers, and NS-E203's hint no longer sends you to `ns tune` first. Nightshift never closes another program and
  never loads a smaller model instead. Where the card cannot be read (no nvidia-smi, or an install on the processor
  without the simulated card) nothing is checked and a load goes ahead as before. Not covered: shown by tests with
  scripted readings and on the simulated card, never on a real one, so what the story model really needs, what an
  idle card of the GPU PC keeps in use (1,024 MB is assumed until three quiet readings agree) and whether memory
  comes back within the 10 seconds the engine allows after it stops one of its own models are not measured; the
  first load of a model is never refused, so a card that is full on the first night still fails the old way (NS-E202
  or NS-E203); a game open at the very first reading can hide in the idle figure until a quieter reading lowers it;
  `NS_TEST_VRAM_WAIT_S` (60 to 1800 seconds) shortens the wait for a proof, on a dev or engine-test install only;
  `proofs/vram_busy.py` is the proof for an engine-test install and has not been run on one.
- Every night now looks for the GPU kit's record before it plans, even if you never run the kit (gpu-kit). The kit
  writes `run\gpu-kit-nights.json` before it changes anything for its two nights: each night's window (a day long,
  from 30 minutes before the run time), what each makes (one full story video on the kit's channel), what
  Automatic runs were before the kit, the run time it armed and the end of night 2's window. One reader
  (`ns_supervisor/kitnights.py`) answers for the nightly run, the planner, the three routes to Automatic runs and
  the kit's last step, and it never raises. No record, which is every PC that never ran the kit: nothing changes,
  and the planner pays one missing-file check. A valid record: the night inside a window plans the kit's video
  (below). An expired record, a night after the end of night 2's window: the nightly run first puts back what the
  kit changed, then runs exactly as it would without a record. It pauses the kit's channel before the night, so
  the night neither plans nor continues the kit's job; it switches Automatic runs off only when they were off
  before the kit, nobody switched them since, and the task is still at the time the kit armed, and then leaves a
  morning note and that night makes nothing, because you never asked for it; it removes the record. When the runs
  are yours, that night runs as usual. An unreadable record (torn, not JSON, a time without a time zone, written by
  a newer version, a file that cannot be opened): the night runs as if there were none, the log says why, and one
  morning note names the file and says to switch Automatic runs off in Settings if you did not switch them on
  yourself, or to run `GPU-KIT.cmd` again, which puts back what it changed; the file is left as it is, and no
  night switches Automatic runs off while it cannot be read. The planner's one exception: it never plans a
  channel named `gpu-kit`, except on an armed night, when it adds one full story video (`master_first`) on it,
  last in the night's plan, once a night (not when a kit job was made or worked on in that night's window, so a
  night-1 job continued by night 2 is that night's one), never after the record's end, and the video goes through
  the night's time rule like any other, so it can be left for time. If you switch Automatic runs yourself after
  the kit armed them, by any of the three routes (a request to the switch in Settings, `ns schedule install`
  without `--disabled`, or yes to setup's nightly question), the record notes it once with the route and the time,
  and from then neither a night nor the kit switches them off: the switch is yours. A request the kit itself makes
  is not counted as yours. Not covered: shown by tests with stand-in nights and clocks and by the kit's dry run on
  a test install, where the nights ran through the install's own stub with a 20-minute test limit, never started
  by the Windows task and never on a real graphics card or with real models; nothing in the kit has run on a real
  graphics card, and `GPU-KIT.cmd` itself has not been run from a real install yet (block E's install proof has
  not run). When the kit switched Automatic runs on and its record is then unreadable, they stay on after the two
  nights until you or the kit's last step switch them off. Shown by tests.
- Update packages are signed and checked before anything changes (update-signing; box F-E26). Each release now has
  a `.sig` file made with the owner's release key. A Nightshift that knows that key installs an update only when
  the `.sig` fits the file's exact name, version and content. The check comes first on every route (Update now,
  `ns update <zip>`, `--github`, the Update shortcut, Settings > Updates > From a file, which now takes three files:
  the zip, its `.sha256` and its `.sig`), before anything is stopped or changed. A package that was changed,
  renamed, signed by another key or has no `.sig` is refused (NS-E707); Nightshift keeps running as it was, and the
  sentence says which case it is and what to download. Before, the `.sha256` came from the same place as the zip,
  so it showed that the file arrived whole, not who made it. The check needs no extra package. An update from
  0.1.14 and a rollback to it still work. Not covered: the owner has not yet run `dev\release_keys.ps1 -Create`, so
  no key is built in. Until then `TRUSTED` is empty: the program accepts a package on its SHA256 alone and says once
  per update "This version of Nightshift does not check who made an update yet", candidates are published unsigned
  with a warning in the release run, and a FINAL tag fails the release run. The first signed update needs one
  bootstrap from an unsigned version, which is accepted on its SHA256. A rollback, setup's INSTALL.cmd and
  `ns update --git` check no `.sig`: verify a Setup zip with its `.sha256`. An older signed package is accepted
  (there is no downgrade rule), and a refused zip stays in `data\updates`. Shown by tests; no update has been signed
  or installed on a real PC, and the card's proof with a throwaway key is written and has not run.
- A damaged database is found at start and replaced by the newest good backup (db-integrity; box F-E25). Before, a
  database damaged by a crash, a full disk or a bad sector made every job fail with a raw error until you found
  `ns restore`. Now each start checks `studio.db` quickly. A damaged one is set aside in `data\damaged`, the newest
  nightly copy that passes a full check is put back, the videos made since that copy are put back in the Queue as
  "to review" from their job folders, and a morning note (NS-W213) says which day it went back to and what is not in
  that copy. With no good copy a new database is built from the job folders: the history is gone, the videos are
  not. The Assistant's `assistant.db` is set aside and starts empty. The nightly copy is written under a temporary
  name, checked, and only then does it replace the day's file and let old copies go. A night whose database breaks
  half way stops with one note (NS-E209), and the next start does the restore. A copy that cannot be read right now
  (a backup tool or antivirus holds it) is never taken as damaged: the start stops and changes nothing (NS-E209).
  A nightly copy that fails is counted and said (NS-W214), and `ns doctor` has a row "database". Not covered: only
  the videos are put back. Your ratings, the incident notes, the clip use and the nights' record are not in an older
  copy, and an idea used since then comes back unused. Damage that only an index carries is answered by the older
  copy too. `data\damaged` is never emptied for you. A `studio` command typed by hand shows the raw error and the
  next night then stops and marks it. Shown by tests and by SQLite probes on this laptop; no damaged database has
  been restored on a real PC.
- An update brings the uv its program pins before it reads the program's lock (uv-pin-on-update; box F-E28).
  Before, an update used whatever uv was in the install folder, so the day a release moved to a newer uv or lock
  format every update would fail and roll back. Now every update route, and a rollback, first put the uv named in
  the program's `manifest.json` in place: from a `files` folder beside the zip, then the install's cache, then
  GitHub, each checked by its SHA256 and run once. The old uv is kept in `bin\uv.prev`, and a rollback puts it
  back. When no copy can be had the update is refused before any change (NS-E708), naming the version and the file
  (for a PC without internet, put the uv zip in `files`; for From a file and Update now that is
  `data\updates\files`). A failed update puts the old uv back; if that fails too, the sentence says so and to
  choose Repair. The same pin changes nothing. `ns doctor` has a row "uv" that compares it with the pin. Not
  covered: an install updated by 0.1.14's updater has packages that were not installed from the lock until its next
  update or Repair; a rollback that cannot get the older uv still goes back and says in its result line that uv
  stays at the newer version. Shown by tests with a stand-in uv; no real uv, kill or held file was seen.
- A program no longer rewrites the settings of an install of another version by accident (version-mismatch-writes;
  box F-E29). Before, `ns tune` or `ns schedule install` typed in an old PowerShell window that still named another
  Nightshift install rewrote that install's settings with this version's fields, which the older version then
  refused at its next start. Now a command that would change something there (settings, folders, the database, the
  nightly task, starting work) stops before any change with NS-E128, naming both installs and the command that works
  for each (`<install>\cmd\ns.cmd`). It applies only when the install's version differs and the window's
  NIGHTSHIFT_HOME is what points there. Commands that only read still work (doctor, status, backup,
  `ns schedule show`, `ns tune --dry-run`, `ns update --check`), open the database read-only and write no log line
  in that install. The check runs once at the start of `ns`, `studio`, the engine, the Dashboard, the voice server
  and the job children, and a command in no list counts as writing. Never refused: an install's own program and
  shortcuts, a home named on purpose (`ns update --home`, setup, the proofs, the GPU kit), an update or rollback,
  and a variable that is only the PC's default. Not covered: an older program has no such check and can still write
  a newer install; a window with no variable and another install's program typed by its full path; an install
  whose version changes while a command runs; a dev install as the target is refused (its version cannot be read).
  Shown by tests; never run with two real installs.

### Videos and stories

- A long video's footage gets the same black-clip check and redo rules as a Short; a clip download cut part-way goes
  on from where it stopped when the server allows it, instead of starting again on your data; a check that cannot run
  stops with NS-E401 instead of throwing clips away; the Dashboard shows the footage step's progress ("12 of 85
  clips, 40 MB"); the error box shows the full path of the failed run's logs (rc8).
- You can make a long video from your own story text: Create, "Use my own story text" (rc8).

- Footage no longer ends a video, and costs less data (footage-robust, rc7; found on the GPU PC, where the first
  real Short was refused by QC for two black intervals, its redo downloaded a new set of clips and then ended with
  NS-E503 on one slow answer of Pixabay, and the run used 1.15 GB of the owner's data). A clip download that times
  out, is cut or gets a server error is tried again (after 2, 5 and 15 s, each try in the job's log); a clip that
  still cannot be fetched is replaced by the next candidate, and the job stops only when three candidates in a row
  fail or none is left. Each shot's clip is checked for black before the render, and a black stretch is moved or the
  clip replaced. A redo after QC keeps the footage it has: a redo for black frames replaces only the clips under the
  black, a redo for the hook keeps them all. A step that a redo skips no longer starts its service (the story model
  was loaded for 21 s for nothing). The footage step logs its progress and the MB it downloaded
  (`clips/footage.json`), and OPERATOR.md says what footage costs: about 0.4 GB a minute of Short, about 1.3 to 1.6
  GB for an 11-minute video. Not covered: whether the black check before the render matches QC on real footage (it
  looks at a small copy without the zoom), why the first Short's picture went black (very dark footage is the likely
  cause, unconfirmed), the long video's footage gets the retries but not the black check, a retried clip starts again
  from its first byte, and the Dashboard does not show the footage progress yet (the log does).

- A story job no longer ends because a rewrite came back too short, and never replaces your idea in silence
  (story-robust; found on the GPU PC, where the owner's first long video ended after three minutes with NS-E303
  "length: 202 words, allowed 247-371" while the studio had already put a story of its own in the place of the idea
  he had approved). Four changes. (1) When the critic asks for a rewrite, the rewrite is told the length it must
  keep. A chapter (or a Short's story) that is outside its length window is repaired where it happened, lengthened
  or trimmed by the missing words, at most twice, and scored by the critic again; only when that does not hold
  does the job see NS-E303, and the message names every rule the text still breaks. (2) An idea you approved is
  checked before it is marked used. If the safety check refuses it, it goes back to Library > Ideas as new: a
  video you started yourself ends at once with the reason (NS-E601) and nothing is written in its place; a nightly
  run goes on with your next approved idea, or with one of its own when none is left, and says so in the log. (3)
  When the idea is yours and the written story is refused later (by the text check or by the critic), the retry
  writes the story again from the same idea; it never draws another. (4) A job has one retry for each kind of
  trouble (the idea, the text, the video and sound) instead of one for all of them; before, a refused idea spent
  the retry that a short chapter needed. The hints of NS-E303 and NS-E601 say what happened and what you can do.
  Not covered: whether the real story model keeps to the length it is told is not yet seen (one rewrite on a real
  PC is all the evidence); the safety check's wrong refusal of that idea is not fixed here, it is only made
  visible and survivable; there is no "use my idea anyway" choice yet; a nightly run's message in the Queue does
  not yet name the idea it set aside, and the error box still shows the failed run's folder as a short path.
  Shown by tests with a scripted stand-in model.
- A Short or a master chapter with many clips no longer builds an FFmpeg command that grows with the clip count.
  More than 12 clips are rendered in parts of at most 12 (video only, in a `<video>.parts` folder removed after a
  good render), then one more call joins the parts and adds the audio. The longest command stays at about 1,100
  to 1,200 characters (200-character home) for 16, 200 and 1,000 clips (53,587 before at 1,000), so a clip list
  cannot reach the Windows limit behind NS-E327. Not covered: the grain of the dark_slowzoom, handheld_grit and
  cinematic_teal looks restarts in every part and each seam is a separate encode, so such a video is not
  frame-for-frame what 0.1.14 made; peak disk use is about twice the video while parts and output coexist.
- The repeat check NS-E605 compares a story only with videos that were really made: a job that failed (at the
  voice stage, for example) or a video you rejected no longer blocks the same script. A script still too close
  (0.85 on the same channel, 0.80 on another) to a finished video fails with "This script is too close to the
  finished video of job <id>. Change the script, or use that video instead of making a second one."; the hint
  reaches the dashboard job and the command line. A job you resume meets the repeat check again, and fails with
  NS-E605 if the same story was finished meanwhile. Not covered: the hint does not name the stage the old job
  stopped at; the opening-line repeat check still reads every story, finished or not.
- The policy gates close six gaps. Words the duration check adds to a chapter are now checked (NS-E602, NS-E603,
  and the unsafe-topic classifier NS-E601); the chosen or banked idea is classified before planning, and the
  finished script and metadata get a final unsafe-topic re-check. The altered-content advice is "yes" for any
  place spaCy recognises, a real-setting answer or an unclear one, and an intense story on a mature channel sets
  "Age restriction suggested". A Short cut from a master that does not open with its own hook fails NS-E611 and is voiced again.
  Tonal audio in quiet passages is only measured and warned (`qc/audio_flatness.json`, `audio_flatness_warning`
  in the job log), never failed; the -55 dBFS noise floor still fails. Not covered: the flatness limit is not yet
  measured on real qwen-tts narration (the partner's first night is asked for its values); `intensity` is kept in
  `policy_notes.json`, not on the shared policy result; almost every story gets "yes" for altered content; the
  classifier now runs 3 to 4 times per job, so a false positive can cost a full render; the NS-E611 message still
  reads "render too similar to a previous video" for a missing hook; the words the duration check adds are
  scanned against `misleading_claims.txt`, a list written for titles and tags, so ordinary story prose ("happened
  to me", "real life") can fail the video with NS-E603, and the regeneration that follows redoes only the metadata,
  which cannot change the prose.
- Story length follows the voice. The word budget uses the pace each voice really spoke in its last jobs
  (`config/voice_wpm.json`, mean of the last 12 jobs; a job under 120 words is not counted) instead of a fixed
  table. A story under the 9:00 floor has its shortest chapter lengthened up to three times, then fails NS-E303
  (the NS-E305 hint still says "twice"). The voice keeps its own seed on the first pass; only a regeneration gets a
  new seed. Not covered: the series workshop still plans with the table pace; the figure is keyed by voice name
  only; the 35 s Short word window is narrower (61 to 70 words), so more drafts may miss it and be redone.
- Every Short ends with a spoken, captioned closing line of at most 15 words ("Part 2 is up next." when another
  part follows). A fallback hook is cut to 12 words and not voiced twice, and the Short's length budget leaves
  room for hook and closing so QC (NS-W406) drops fewer. A long-video-first job picks the idea with chapter
  potential. Not covered: owner-script Shorts get no closing line; a single-part loop-back story gets a closing
  that breaks the loop; the policy gate does not scan the closing line.
- A story that misses the critic's bar (NS-E304 from the critic) is no longer a dead end: it is moved to
  `rejected/<channel>/<date>/<job>/attempt<N>` with a `reason.json`, and the job restarts at the ideas stage with
  another idea; three ideas at most (counted in `ideas_failed.json`, so a resume does not get three more), then
  NS-E304. A job that fails NS-E303, NS-E304 (ideas), NS-E305, NS-E402, NS-E403 or NS-E404 gets one regeneration,
  and it really redraws: chapter drafts and the voice take are keyed to the regeneration, so the second try writes
  new chapters (the earlier NS-E602 and NS-E612 regeneration reused the saved drafts and changed nothing; fixed
  too). A plain resume still reuses its drafts. Not covered: an owner script, a series part, a clipper NS-E404 and
  an NS-E303 before the job exists fail at once; policy and these regenerations share one count; a Short outside
  the length limit restarts at `shorts_select` and voices the whole master again.
- A Short that QC drops (NS-W406) is kept for review: its video, `qc.json` and a `reason.json` are copied to
  `rejected/<channel>/<date>/<job>/dropped/<item>`; the master and the other Shorts are delivered as before. A
  language-model call that fails with NS-E301 on a server error (5xx), a refused or reset connection or an unusable
  answer is tried once more after 3 seconds; a timeout or a 4xx is not retried. Not covered: a single dropped Short
  is not regenerated, the NS-W406 hint still points only to `qc/dropped.json`, and the Dashboard assistant raises
  NS-E301 without a retry.
- Shorts and Shorts cut from a full video draw from their format in Library > Formats: frame size, caption
  position and words on screen, shot lengths, frame rate and encoder preset, so a duplicated or edited Short
  format changes the video. The format comes from the series design, else the first allowed one of that kind in
  the channel's `formats_allowed`, else the default; the job records it in `formats.json` and the render report.
  The first job a studio process starts after the update leaves one morning note naming the edited or added
  formats that differ from the shipped ones (when there are any). The Formats editor now refuses at save what the
  studio refuses (shot range reversed, region off the canvas, odd canvas size, unknown caption style), and a
  hand-edited format file that is broken is flagged as not valid in the formats list and left out while the others
  still load. Owner copies of `short_9x16` or `square_1x1` made before this update hold the old values
  (`anchor_y_pct` 72, `words_on_screen` 3, fps 30, preset p4); those values now apply, and the morning note does
  not mention them. Not covered: full videos, the square master,
  clip layouts and the Anton font still ignore formats; a series design format that is skipped is reported only in
  the log and the render report; a channel whose `formats_allowed` lists `square_1x1` but no Short format now
  gets 1080x1080 Shorts where it got 9:16; the editor has no fields for region, caption style or canvas.
- A new job takes its id by making its folder, so two jobs never share one (pull request 207, D-ids,
  `ns_common/db.py` `claim_job_id`). Before, an id was 16 random bits per name and day and nothing checked that
  it was free: two videos for one channel on one day, or two uploads or two linked videos on one day (their ids
  are shared by all channels), could write into the same folder, one over the other (about 1 in 300 days at 20 jobs a day). The folder is made in one step the file
  system settles, the id format is unchanged, and after 64 refusals in a row the job stops with NS-E205. Used by
  new videos, clip jobs, the series workshop's produce, uploads and linked videos; the nightly's run id is checked
  against the runs table and the owner files. Not covered: the nightly run id is checked, not claimed, so two
  nights started in the same moment that draw the same id are refused by the runs table (an error, before any
  job is made); a clip job from a local file keeps a drawn label that names no folder.
- The Queue keeps the posting checklist reachable after Approve (pull request 210, P14, `Queue.tsx`). The Ready
  tab asks for pending and approved videos and keeps the ones that still need you (to review, or approved and not
  posted; an approved test output is left out), so an approved video stays on screen with its checklist until you
  mark it posted, and "Finish posting" on the posted list opens it again. Before, the video left the screen about
  ten seconds after Approve together with its checklist. Not covered: the Ready tab's number still counts only
  videos to review; there is no browser test of this page in CI.
- Pausing a channel holds its story jobs on every route (P47, pull request 203 and its follow-ups). A video of a
  paused channel that is queued, scheduled (Create > Pick a time) or waiting for the engine does not start, also when
  its time comes; it stays under Working now with "Paused: <channel> is paused since <time>; resume it to release this
  job" and starts by itself, in its turn, after Resume. A video being made stops before its next step (the step under
  way is finished first; Cancel stops at once), keeps what it made and continues from that step after the resume; a
  Create request for several videos keeps the ones still owed (the status says, for example, "(3 videos still to make,
  the first one half made)") and makes none twice. Retry changed for every Create request for several videos, paused
  or not: Retry on a failed request now also makes the videos of that request that had not started (a request for
  five that failed in the second gives four videos of work, not one), once; a second Retry of the same request is
  refused, and the message after pressing Retry says how many videos it will make. Retry and the Assistant's
  resume are refused for a paused channel
  ("'<channel>' is paused. Resume it first."). `studio run --channel <it>` and `studio run --resume <its job>` refuse
  with the new NS-E330 ("The channel is paused.") and start nothing; a run of every channel leaves it out; a pause
  typed while a `studio run` works stops it before its next step, recorded failed with NS-E330, and the terminal
  prints `studio run --resume <job id>`. A video the night was making is continued by the first night after the resume
  if it started no more than three days before. The Queue's Last night card says how many jobs a pause held ("1 job
  not started: <channel> is paused"), the Assistant's tonight plan says a held job starts "when <channel> is resumed",
  an engine start no longer wakes the Dashboard for an overdue job of a paused channel, a profile that cannot be read
  is not a pause (one warning, the night goes on), and a series part gets its title back after the resume instead of
  "Queued". Not covered: shown on a fake-services install (two channels, a Short scheduled two minutes ahead, a run of
  two Shorts: both stayed held past the scheduled time and were made after the resume, one Short per job); the Last
  night card needs a nightly run and was not exercised; the Create page says "Waiting for the engine" or "Scheduled
  for <time>" for a held job, not the pause; with `--count`, the videos a paused `studio run` had not started are not
  made (type the run again); a resume during the same night does not continue the night's job that night, and a job
  older than three days waits for Retry; Clipper jobs are still accepted for a paused channel; Cancel of a waiting
  request leaves the half-made job's files in its folder.
- A job runs the policy gates only when it asked for them, and no job rewrites the stage table (D-stages). Before, a
  job's plan added `policy_text` to the module-wide table of its format at run time, so in a long-running process (the
  Dashboard, the nightly) one gated job changed the plan of every later job. The gates and Short combos are now added
  once when the stage modules load, and a job with the policy off drops them from its own plan: a `--no-policy` job
  after a gated one has none, and the progress card, the orchestrator and the idea check read the same plan. The job's
  choice is recorded in `job.json` (`policy`), a resume without a flag keeps it, an explicit `--policy` or
  `--no-policy` on a resume wins and is recorded, and a folder made by 0.1.14 resumes with the gates when they are
  available. A resumed clip job no longer gets the story gates (the Clipper has its own metadata check), and
  calibration has no gates (as in a fresh `studio calibrate` before). Not covered: shown on a dev install in one
  process (the table the same after every call and after a fake job, `--resume --dry-run` listing no gate); nothing
  here changes what the gates check.
- Assembled audio keeps its full sample precision (audio-fmt). `assemble_audio` joined each voice piece to a silence
  in a filter graph that named the rate and channel layout but not the sample format, and FFmpeg settled the join on a
  coarse one: every sample of `audio_raw.wav` was a multiple of 256 and a pure test tone through it left a steady
  residual of 0.0184 of its size (rms; 0.0475 after loudness normalisation), which could add hiss or grit to quiet
  passages. Every chain now names a float sample format: the residual is 0.00009 (0.00024 after loudness
  normalisation) and most samples are no longer multiples of 256. The master, the Shorts cut from it and the Shorts
  rendered directly all go through that function, also when it assembles in parts. Not covered: not shown on the
  fake-services install, where the fake voice does not exercise the coarse join and the job's `audio_raw.wav` came out
  byte-identical to a 0.1.14 job's; the proof is the tone test, which needs FFmpeg, and no real narration was
  compared.
- The NS-E301 hint says only the retry the product makes (F-H43, D-37). It promised "The job will retry once" although
  a timeout and a refused request never were. It now reads: a story job tries once more after a few seconds when the
  model sent a server error, the connection dropped or was refused, or the answer was empty; a timeout or a request
  the model rejects (a 4xx answer) is not tried again, so run the job again once the model answers. The retry itself
  did not change. Shown on the install through the Assistant's `explain_error` and the error registry. Not covered:
  the Dashboard assistant, `ns verify` and `ns tune`'s benchmark raise NS-E301 with no retry at all (the hint names
  the story job); the retry is shown by tests, not on an install.

- A designed or cloned voice queued by an older setup under a name the voice service refuses is created after all
  (D-24; the rule for such names is in the rc3 entry above). If 0.1.13 or 0.1.14 setup queued "Night Narrator", the
  next run, Library > Voices > Create it now or `studio voice apply-pending` makes the voice as "Night-Narrator",
  points the channel at it and says so in a morning note once; the same run's job already speaks with it. If the
  suggested name exists, or the channel's profile cannot be read or saved, the request is kept with a way out and
  tried again, and a request whose channel now names another voice is dropped and said. The ways out the text names
  exist: Channels > Edit channel > Narrator > Save channel, or `studio channel edit <slug> --set
  voice.name=Night-Narrator`. Not covered: the job of that channel stops early with NS-E310 only in the one state
  where the voice must still be chosen, and the screen shows NS-E310's general line above the name text; a voice you
  made yourself under the suggested name between a kill and the next run is adopted; Library has no rename or
  delete for voices. Shown by tests.
- The upload sheet holds what YouTube Studio accepts (posting-sheet-true). Every sheet (the `.txt`, its `.json` and
  the Queue's copy buttons) now passes one check of YouTube's rules, each kept in one file with the page it comes
  from and the day that page was read (2026-11-11): `<` and `>` are removed from the title, the description and the
  tags (YouTube refuses them); the title is 1 to 100 characters, cut at a word, an emoji counted as two (the safe
  side), and an empty title becomes the story's working title; the description with its hashtag line is at most
  5,000 bytes, shortened in its summary at a sentence and never in the chapter list or the fixed lines; the tags
  come to at most 500 characters counted YouTube's way (the commas, and two quotes for a tag with a space); hashtags
  are single words after a `#`, five at most; a chapter list is given only when there are at least three chapters,
  the first at 00:00, ascending and each at least 10 seconds, and is left out otherwise with the reason; a vertical
  video longer than 3 minutes gets a warning that YouTube will post it as a regular video, not a Short; and made for
  kids is answered on every sheet. A line "Changed for YouTube: ..." says what was changed and "WARNING FOR YOUTUBE:
  ..." what could not be repaired. Sheets written by earlier versions that still wait to be posted are brought up to
  the rules once, when the Dashboard starts. A sheet you edited by hand is not lost: before it is rewritten your
  text is kept beside it as `<name>.before-<version>.txt`, and a line "YOUR EDITS KEPT: ..." on the sheet names that
  file; a `.txt` saved as UTF-16 or ANSI is re-saved as UTF-8 with its original bytes kept the same way; a sheet
  whose `.json` cannot be read is rebuilt from its `.txt` and the job's records, and one that cannot be read at all
  gets a line in the Queue that names the file and says what to do. The altered or synthetic content advice now
  follows the voice the video was narrated with: a cloned voice is always "Yes (the narration is a voice cloned from
  a person's recording)", with YouTube's own-voice example quoted below it; a preset or designed voice keeps the
  answer of the policy check, with its reason; a video whose narrating voice was not recorded (made before this
  version) says "You decide", with YouTube's rule quoted; the fixed "Answer No. The voice is production help" is
  gone from the Queue. A sheet whose clips came from Pexels or Pixabay also offers one line, "OPTIONAL CREDIT (not
  required; paste it at the end of the description or leave it out): Stock footage from Pixabay.": neither site
  requires a credit in the published video, and when the pasted line would take the description past 5,000 bytes the
  sheet says the credit was left out instead of offering it. Not covered: nothing has been pasted into YouTube
  Studio yet, so that Studio accepts each field as given, and how it counts an emoji in a title, are not shown
  (`dev/kit/K8_PASTE_CHECK.md` is that check, for the GPU PC session); the rules are YouTube's pages as read on
  2026-11-11, and the sentence quoted for the altered-content rule should be compared with the page once more before
  the final release; a change you made to the altered-content line or to Nightshift's own note lines is not kept as
  an edit; "You decide" is stored as "no" in the `.json`; a UTF-8 `.txt` to which a few bytes in another encoding
  were appended is read wholly as ANSI, and when its `.json` is torn as well those wrong characters reach the
  rebuilt sheet (the change line says the file was read as ANSI); the credit names the site only, never a
  photographer. Shown by tests, not on an install.
- Every answer of Pexels and Pixabay is told for what it is, and a wrong key is found before a job starts
  (stock-truth). One function now reads what a stock-footage site answered, for every caller (setup's key question,
  Settings > Stock footage keys, the check before a job, `ns doctor`, the clip search and the clip download), and
  each kind of answer has its own sentence, way out and code: the site did not accept the key (Pexels answers 401,
  Pixabay 400 with "Invalid or missing API key") is NS-E501 for Pexels and NS-E502 for Pixabay, and only this kind
  ever names the key; the site's request limit was reached (429) is NS-E504, which says the key is fine, and
  Nightshift waits as long as the site asks; the site is failing (5xx) or no answer came is NS-E503; an answer
  Nightshift does not read (any other status, or a page that is not the documented list) is NS-E505, which says it
  does not mean the key is wrong. Before, a busy or failing site could be reported as a rejected key. Setup and
  Settings still refuse a key the site refused, but a key they could not check now is kept and said so (NS-W501,
  NS-W502), and a saved key is no longer deleted because the site was down. A wrong or revoked key no longer shows
  only 30 to 90 minutes into a job, at the clips stage: before a job that will still fetch stock footage begins (the
  nightly run, `studio run` and `--resume`, Create, Retry) each site with a stored key is asked once, for at most 15
  seconds, and the answer is kept for 12 hours (30 minutes when the site could not be asked). When every stored key
  was refused the job does not start and no model is loaded; with one key refused and one accepted the job runs on
  the other site and a note says so; a busy, failing or unreachable site never stops a job. `ns doctor` has a row
  per site, `stock key pexels` and `stock key pixabay`: accepted, refused (with the sentence and the way out), could
  not be checked now, or no key stored. Pexels is asked at its documented address
  (`https://api.pexels.com/v1/videos/search`; Pexels says the old one will be deprecated). The key's text goes into
  no log, error, note or file (a test searches every file of the home for it), and "Stock footage keys are still
  written in an answers file" has its own code now, NS-E126. Shown live with a real Pixabay key on 2026-11-11, on
  the request code as it was before this change: one video search (80 hits, every field the product reads present)
  and one download through the product's own code, and setup's check accepting that key and refusing a wrong one.
  What the sites answered then is what this change was built on, and its tests replay those answers; the changed
  code itself has made no request to either site. Pexels is shown only up to authentication, and at its old
  addresses: a wrong key answers 401. No real Pexels key was used, so no Pexels search or download has run, and the
  address the product asks now has not been requested live. Not covered: a key revoked inside the 12 hours is found
  at the clips stage of the first job and stops the next job at once; the clip feature (your own footage) is not
  checked; whether the Footage page shows the link to Pexels that Pexels asks for was not checked, and a
  photographer's name is not kept; box F-B14 (rate limits against a real site) is unticked and must be proven again,
  because its evidence quoted the old code.
- The Head's own fixes of block E (head-own; D-52, D-53, D-57, NS-E331 and two more, from the install proof and the
  final review). Reject moves a video's files together or not at all (D-57): before, a Reject while a Queue page had
  the video open failed with WinError 32, left a whole copy in `rejected` and the row unchanged, because the preview
  kept the file open for as long as the page showed the player. Now the preview opens the file per block, the files
  move in one step (a copy only across drives), a held file is tried 5 times, and on failure everything already
  moved is put back and NS-E331 names the held file; the row stays as it was, and Reject again works. `ns tune` on a
  PC with no graphics card decides the context on warm, repeated measurements (D-52): before, two runs saved 32768
  and then 16384 because the cold first load stood for the floor; the graphics-card choice is unchanged. An update
  to a candidate shows "What is new" from its release's notes (D-53): before, it showed none, because the notes
  were looked up under the candidate's own version (the updater that runs is the installed one, so an update from
  0.1.15-rc5 to this candidate still shows none; the next one does). The morning note says a video is "made over several nights"
  only of a job the night really left half made, not one that failed, finished or never started. A browser that
  sends nothing identifying (Edge's Internet Explorer mode, a Firefox older than 90) can no longer change anything
  with an empty request: a request that is not a GET with no Sec-Fetch, Origin or Referer, from a User-Agent that
  names Trident or Firefox, is refused (NS-E207), with one note a day. One test no longer fails on a loaded PC.
  Not covered: what the Dashboard page shows for the refused Reject was not looked at; the old-browser rule goes by
  the User-Agent, so a program that names neither and sends nothing is not caught; `ns tune` has not run on a real
  PC. Shown by tests.

### Clips and the Assistant

- A story the Assistant writes is a file you can open and edit: `drafts\<id>.md` in your Nightshift folder, written
  one part at a time and shown in the chat with Open; Yes starts the video from the file as it is then. A reply cut by
  the reply limit no longer ends in NS-E301: the chat says the story was too long for one reply and names its draft;
  a tool's refusal is shown in its own words (rc8; found on the GPU PC, where the owner's first planned long video was
  lost inside a cut reply). Not covered: the Create page has no draft picker yet; drafts are not in the settings
  backup.

- Clips from a long video: the clip-picking prompt (v4) also asks the model for the moment's spoken text with
  capitals, commas, full stops and question marks put back; that text is laid over the recogniser's words, so the
  number of words and every timing stay as they were (written to `clip_caption_words.json` beside
  `clip_plan.json`). A word the model dropped, added or reworded, or a symbol nobody said ($20, 20%, #1), keeps the
  recogniser's spelling, emoji are stripped, and a clip the model text covers less than 80% of stays as
  recognised. The prompt is now told the kind of video (stream with facecam, podcast, talking head, screen
  recording or slides) instead of "unknown", and a clip's title is cut at a word boundary to 70 characters (it was
  100). Not covered: "like" is not removed (the filler setting `captions.remove_fillers` handles um/uh/er only); no
  real model has run the v4 prompt; jobs made with fake services never show cleaned captions.
- The Assistant checks a changing action's arguments before it shows the confirm card, with the checks the real
  action runs. A call that would fail (an unreadable or over-31-day `schedule_at`, a paused channel, a missing
  footage key, a story step the story's state does not allow, warm-up or shut-down with the engine offline,
  diagnostics with the doctor missing) gets no card; the model is told what is wrong, so it can ask or correct
  itself. When a turn ends with no answer after such a refusal you read "I could not set that up, so there is
  nothing to confirm." Not covered: `clone_voice` checks that the recording exists, not its length;
  `import_analytics` checks the file name and size, not the CSV columns; the `start_story_job` card still prints
  the raw format id (`shorts_only`).
- With "remove fillers" on, a clip whose speaker says "um" or "uh" gets its captions, instead of failing at captions
  (F-H95, D-38). The setting drops `um`, `uh`, `uhm`, `umm`, `er`, `erm` and `hmm` and keeps "like"; the captions kept
  their words but were given the speaker list of all the words, so the stage stopped with "speaker_of must have one
  entry per word". Both lists now come from the same kept words, so each caption keeps its own speaker's colour and
  the break between speakers lands on the right word, a clip that is only hesitations ends with no dialogue and no
  failure, and a cleaned spelling still lines up with its word. The setting is off by default. Not covered: shown on a
  fake-services install with a prepared transcript (clip 1 covered three "um" and three "like": `c1.ass` has no "um"
  and keeps "like"); no real recording or recogniser was run, so F-H95 and F-H127 stay open for QA's run.

### Engine-test installs and developer switches

- Engine-test installs (`fake_voice: true`): the fake voice speaks the real text with the Windows speech voice
  (SAPI) instead of tone bursts, so speech recognition can align it; before, story, master and script jobs stopped
  at captions with NS-E403. One Windows voice stands in for every voice and language; the voice service's
  `GET /health` shows `voice_mode`. With no Windows voice it falls back to tone bursts and warns with NS-W303 ("The
  fake voice made tone bursts, which speech recognition cannot align."; in `/health`, the job log and
  `audio/voice_engine.json`), and the NS-E403 detail then says it is the test voice. Dev installs and CI keep tone
  bursts; `NS_TTS_FAKE_SPEECH=sapi` or `tones` forces either. A developer switch `NS_TTS_FAKE_DELAY_S` (default 0,
  at most 120) makes the fake voice engine wait before answering, so a Cancel or the voice lease can be tested.
  Not covered: that real whisper aligns this voice is still QA's proof.
- Engine-test switch `tiny_story` (an answer in `answers.json`, copied to `runtime.json`) for an install with a 1
  to 2 GB language model, so such a model can finish a story. Setup refuses it without `allow_cpu`. It scales
  length only: a master is planned at 4 minutes in 5 chapters and accepted from 2:00 to 8:00 (QC window 110 s to
  15:00), a Short may have 0.35 to 1.6 times its words; every validator and the policy gates still run. A video
  made under it is marked `profile: tiny_story`, its sheet opens with "TEST OUTPUT", the Queue shows a "Test
  output" badge, and marking it posted is refused with NS-E329. Not covered: never run end to end with the real
  1.7B model; a tiny master can still stop at NS-E305 or a Short at NS-E404 if the model writes far less than the
  target; cadence and notifications still count test outputs; a `runtime.json` saved by this version cannot be loaded
  by 0.1.14 (every one carries the `tiny_story` field), and the hop from 0.1.14 saves no settings copy; the dashboard cannot set the switch (it appears only in the
  settings data).
- A dev or engine-test install can check for and take its update from a fake GitHub (H-15.9). The environment variable
  `NS_RELEASE_SOURCE` names an `http` address on this PC (127.0.0.1, localhost or ::1), and every request of the
  updater (`ns update --check` and `--github`, the Dashboard's check and Update button, the weekly check) then goes
  there, and the zip and its checksum must come from the same host and port. It is read in one place and honoured only
  on a dev install or an engine-test install (`tiny_story`, `fake_footage` or the fake voice engine). On any other
  install, or with an address that is not on this PC, or with a `runtime.json` that cannot be read, it is refused with
  NS-E704 (its detail says which reason and the way out) and no server is asked; the weekly check tells you once, by
  notification or a Dashboard note, and again when the reason changes. A prerelease from the test source is still
  refused (NS-E701), and without the variable nothing changes. `docs/DEV_MODE.md` describes it and
  `proofs/_fakes/github.py` is the fake server. Not covered: the install proof ran the checks only, against that fake
  serving the block's update zip (unset: GitHub is asked and 0.1.14 is the latest; set: one request to the fake; a
  stand-in file named rc3: "Version 0.1.15-rc3 is available"; an address that is not on this PC, and a copy of the
  config with every test switch off: NS-E704, no request); applying an update from the fake and rolling back were not
  run, because they need the previous candidate installed first; a redirect to another host is followed once before it
  is refused, and its answer is discarded.
- A simulated graphics card for a PC that has none (H-10, test switch `fake_gpu`). On a PC without an NVIDIA card
  nothing ever takes the graphics card, so who holds it, the queue behind it, the busy answer and the release could
  not be shown there. With `"fake_gpu": true` in the answers of a dev or engine-test install, setup writes the
  switch into `config\runtime.json` (Repair keeps it) and the engine treats the language model, the voice service
  and whisper as users of the one graphics card, as they are on the GPU PC. The lock, the queue, the busy answer
  (NS-E201), the lease and its expiry and every release are then decided by the same code as on a real card; nothing
  of that is faked. Only nvidia-smi is: every question the engine asks it is answered from `run\fake_gpu.json` (the
  card's name and size, other programs holding memory, a missing, hanging or failing nvidia-smi) in the text the
  real tool prints, and the real tool is not run. Every number measured on the simulated card is marked "simulated
  (fake_gpu)" wherever it is shown or kept: `/status` and the Dashboard header ("Simulated card"), the Queue's
  meters, `ns doctor` and the diagnostics zip, `ns verify` (which gives "n/a" instead of an idle-memory figure), the
  engine log, the night ledger and the times in the morning note, and the memory a model was measured to need, which
  is kept apart from a real card's. `NS_FAKE_GPU_LEASE_S` (30 to 600 seconds) shortens the lease for a proof. A real
  install ignores the switch: set by hand there it changes nothing, the engine warns with NS-W121 at every start and
  leaves one morning note, `ns doctor` shows the row `fake_gpu` as FAIL, setup says NS-W121 and leaves it off, and
  the dashboard can never set it. Not covered: it proves nothing about a real card: the driver giving memory back
  when a process ends, the values nvidia-smi prints on the GPU PC (only their shape is copied, from one recording),
  real load times, a real out-of-memory error and NVENC all need the GPU PC; a dev install's studio runs its fakes
  in its own process and takes no lease, so the night's lease shows only on an engine-test install; that closing the
  real Nightshift Code window ends its process in time (F-H47) stays open for the GPU PC;
  `proofs/f_h47_closed_code_window.py` is the proof for an engine-test install (a process that holds the card ends,
  and the card is free within 5 seconds) and has not been run on one. Shown by tests.

### Behind the scenes

- A program that has ended no longer keeps the graphics card (found by a test on a busy PC). The engine asks
  whether the program that holds the card still runs; the library it asked (psutil 7.2.2) can call a program that
  has just ended "running" for as long as Windows still lists its number, and on a busy PC that happened in a
  third to a half of the tries, so the next job waited for a holder that was gone. The engine now asks Windows
  itself for the program's exit code and start time and falls back to the library only when Windows gives no
  answer. Not covered: a holder started with administrator rights was not probed (the fallback answers there).
  Two tests that rested on a time (three seconds for an engine to stop, twenty for a stand-in to start) now wait
  for the event instead.
- Tests and CI: a docs-only pull request no longer runs the whole suite and the suite runs in shards weighed
  from real timings (six here, eight since rc4); CI's FFmpeg install cannot fail silently; a push to `main` is never cancelled by a newer
  one; a test fails on stray control characters in tracked text files; the dashboard and setup window now each
  load one script file (`app.js`), so the versioned entry cannot load twice.
- Tests and CI, added since rc1: the test-shard timeout in `ci.yml` and `release.yml` is 30 minutes (it was 25),
  so a test file with no measured weight no longer turns every open pull request red, and a test fails while a
  shard has room for fewer than 8 such files (pull request 208). Not covered: the slowest test files are not made
  cheaper yet, and the weights are not refreshed by a schedule.
- Proofs as code (pull requests 195 and 196): `dev/prove.py` runs the scripts in `proofs/` one at a time against an
  install, writes an evidence file per box under the runs folder, and `dev/finalization_status.py --check` accepts a
  tick citing `proof:<id>` only when a script declares that id for that box. The first scripts cover F-B18 (`studio
  status` only), F-C1 (status and the first events frame), F-B7 (reject, approve, mark posted) and F-B13 and F-B14
  (stock-footage rate limits against a local fake). Not covered: no script has yet been run on an install, and the
  evidence file is not read by the check.
- Instructions: the repository's `CLAUDE.md` and `docs/ENGINEERING.md` section 5 now follow the block process
  (builders commit locally; the block's one pull request carries the CI run); 17 boxes of section H of
  `docs/FEATURE_STATUS.md` were ticked with evidence checked against main, and six boxes for the launch gaps were
  added. `OPERATOR.md` no longer says every open window waits for a rollback. No product behaviour.
- Documents: the pipeline, setup-window, runtime and installer specs and the operator documents (the incident
  procedure and first-run wording in `OPERATOR.md`) and the Clipper spec text were brought in line with the code;
  change requests that were applied elsewhere were marked closed, stale ones rejected, and CR-H106-1 filed; the finalization lists carry the boxes ticked on the 0.1.14
  installs. No product behaviour.
- Test only (H-5b): a new test renders a Short, a chapter and a whole master in parts with the real FFmpeg and
  measures every join (each frame once and in order, an even clock, no gap or click in the sound, card frames, a
  caption across a join); on the install proof the 695.6-second master (20,869 frames) and its four Shorts have every
  frame step at exactly 1/30 s. Its sound measurement is what found the audio precision problem fixed above. No
  product code changed.
- Test only (B-flaky): the two uv resolver tests no longer see the stage-1 uv of the PC they run on, so
  `tests/test_uv_resolver.py` gives the same answer on a PC with Nightshift's bootstrap uv as on the CI runner; the
  `seed_output` id flake was confirmed gone (200 passes of the one test). No product code changed.

- Tests and CI, block D: the suite runs in eight shards (it was six; with block D's tests six shards were predicted
  at 758 seconds against the 720-second limit, eight at 569), the shard weights list block D's 31 new test files and
  seven that grew, and the 720-second limit, the room rule and every timeout are unchanged. The stalled-transfer
  test no longer counts requests: it checks that every request after the first resumes at the stall offset and
  follows a counted, waited stall, so a slow runner cannot fail it. No product code changed.
- Test only (machine-state-guards): a test run cannot change a scheduled task, write the registry or the user's
  environment, use the real keyring, write to the real Desktop, Start menu or taskbar, show a notification, open a
  window, elevate, or use the real home under `%LOCALAPPDATA%`, even when a test removes its fakes
  (`monkeypatch.undo()`), deletes a variable or starts a child with an empty environment; the guards are inactive in
  a normal process, and the `monkeypatch.undo()` calls in the tests were replaced by scoped ones. Not covered: a test that
  starts PowerShell itself outside the product's two runners, COM and `-EncodedCommand` spellings the text match
  does not know, and `Start-Process explorer` inside a PowerShell script are outside it. No product behaviour.
- Test only (default-port-tests): no test can connect to the default engine, voice or language-model ports (8099,
  8100, 8080), so a test run no longer reaches a real Nightshift on the same PC; such a connection fails that test by
  name. Not covered: processes a test starts are outside it. No product behaviour.
- Test only (D-43): every proof script in `proofs/` now runs in the test suite against a fake-services home made by
  the product's own code, with a second case that must fail for each proof, and a test fails when a proof has none;
  `python dev/prove.py --all --strict` exits 3 when a selected proof was skipped. This found two faults in the proofs
  themselves (F-B7 looked for the job id in the outbox path, F-B13 unpacked a list wrongly), now fixed. No product
  behaviour.
- Test only (FN-5): every place that builds a single-quoted PowerShell string from a path or a name uses one quoting
  function that also handles the typographic quotes U+2018, U+2019, U+201A, U+201B (and for the double-quote form
  U+201C to U+201E), and a test fails on a new hand-made literal of the shapes it knows. A Windows user
  name with a typographic apostrophe gets working shortcuts, toasts, nightly task, uninstaller and update windows. Not covered: the guard does not see a literal built in a loop, a join or a call it does not follow, and
  the cmd.exe action of the nightly task still holds the home path as typed (a `%` or, with delayed expansion
  switched on in the registry, a `!` in a hand-set home is read by cmd).
- Checklist: `docs/FINALIZATION.md` and `docs/FEATURE_STATUS.md` carry what QA showed on 0.1.15-rc4: three boxes
  ticked (F-E17, F-G1, F-G4), nine given rc4's evidence with the older evidence kept beside it (F-B21 to F-B25,
  F-B27, F-B28, F-H38, F-H143), and two unticked as stale (F-B26: its code changed after its proof; F-B14: its
  evidence quotes NS-E501 for a rate limit, which is NS-E504 since the stock-footage change above). No product
  behaviour.

## [0.1.14] - 2026-11-09

The defects the first proofs on the 0.1.12 and 0.1.13 installs found, fixed by builder agents and reviewed
before merge, plus the footage-reuse gate and a render fix for long install paths. Send
`Nightshift-Setup-0.1.14.zip`.

- Stock-footage reuse is now checked for real. A clip used twice in one video, or used by another video of the
  same channel in the last 14 days (30 days for another channel), fails the check NS-E613 before the video
  reaches the outbox; until now the check read data that did not exist yet and always passed. When the fresh
  clips run out the picker first searches wider (the parts of multi-word keywords, the other scenes'
  keywords, the channel's seed keywords, similar moods) and only then reuses a clip; a video that had to reuse
  one still passes, with the warning NS-W405, and the video's review card in the Queue shows that warning in plain words
  ("Warning: clip reuse - ...") above the OK lines. Where the spaCy model is installed, keywords that differ
  only in form (mice and mouse) are searched once; clips tagged or titled as watermark, logo or text overlay are never picked.
- A Short with many stock clips no longer fails to render with NS-E327 when the install folder path is long:
  FFmpeg now reads its filter graph from a file next to the video and names its inputs relative to their folder
  (Windows refuses a command line over 32,767 characters, and one Short reached about 36,000). Paths inside the graph are made
  absolute, so a render also works when `NIGHTSHIFT_HOME` is a relative path.
- Settings > Updates shows "What is new in <version>" with the release notes after an update, also after the
  page reloads on the new version. The dashboard now shows two toasts it never showed: "Version X is available
  (you have Y). Open Settings, Updates" when the weekly check finds a newer release, and "The nightly run has
  finished. See the Queue".
- Quit Nightshift answers before the engine stops, so the page no longer sees a dropped connection when you
  quit.
- Approving a video you had rejected moves its files (video, sheets, long-video thumbnails) back from
  `rejected` to the outbox, where you post from; if the files are gone or a different file is already there,
  you get NS-E326 and the video stays rejected.
- Cancelling a clip download from a link leaves no `source.mp4.part` and no folder behind; leftovers from a
  download that was killed are removed when the dashboard starts.
- A stock-footage host that is down (HTTP 5xx, timeout, reset) is retried three times with a pause and then
  reported as NS-E503 "... is not answering right now", no longer as a wrong key; the retries apply to searches,
  a failing download stops at once with NS-E503, and a source that answered NS-E503 is not asked again in that
  job. A rejected key (401, 403, 400) still stops at once.
- A full disk is reported as NS-E104 naming the full folder, for a footage download and for FFmpeg renders
  (video, long-form segments and master, audio), and the half-written file is removed instead of left behind.
- Setup step 4 (the Python environments) is done only when every environment can load the product, the progress flags in `wizard_flags.json` belong to the install that
  wrote them, and the uninstaller removes that file. A setup killed during the package install and run again,
  also after an uninstall that kept your data, redoes the packages and the smoke test instead of finishing
  with an engine that cannot start. A finished install also needs `opencode.exe` before the Nightshift Code
  step counts as done. Other steps (the configuration files, the scheduled task) are still counted done on
  what they wrote last time; this change covers step 4, the flags, the uninstaller and `opencode.exe`.
- The setup window's local server no longer resets the connection (WinError 10053) when it refuses a request
  early; it reads the rest of the request after answering, so the browser or the health ping gets the answer
  (WinError 10053 or 10054).
- `studio calibrate` leaves stories that could not be made (a dedupe stop NS-E304, an LLM timeout, a failed
  critic call) out of the refusal rate, prints them on their own line ("1 story could not be made: NS-E304 x1"),
  and no longer advises two-model mode because of them. This holds for stories made on 0.1.14: an earlier
  `stories.csv` still counts its errors as refusals until `studio calibrate` is run again.
- The Windows toast after the nightly run depends only on Settings > Notifications, not on whether services are faked, so a dev or
  engine-test install shows it too; with notifications off the log says why it was skipped.
- Cutting a clip job again when it failed or was cancelled now says that only a finished clip job can be cut
  again and points to the Create page, instead of telling you to wait for a job that had already stopped.
- The setup window's "skip the host window" switch (`NS_DASHBOARD_NO_HOST`) no longer reaches the engine or the
  helper programs it starts.
- Dev mode: the fake language model accepts `tool: <name> {json args}` as the whole message and then calls
  exactly that Assistant tool, so every tool can be chosen on a dev install.

## [0.1.13] - 2026-11-09

The first round of the finalization lists (`docs/FINALIZATION.md`, `docs/FEATURE_STATUS.md`): fourteen code
items from the plumbing map (eleven builder pull requests and three orchestrator fixes), the script-to-video
path, the engine test with fake footage, and the GPU install path proven without a card. Send
`Nightshift-Setup-0.1.13.zip`.

- Script to video: a story you wrote becomes a video. `POST /api/jobs/run` takes `script` and `script_title`,
  `studio run --script <file>` does the same, and the Assistant has `start_script_job` ("make a Short from this
  script: …"). The text is spoken word for word (no model rewrite), the Short's length comes from the script
  at the channel's pace and voice speed, a long script needs 9 to 14:30 minutes of narration, and anything the
  QC would later reject (too short, too long, banned phrases, a stray heading) is refused before the voice is
  made. NS-E305 now reads "The long video's narration is under 9 minutes."
- Engine test with placeholder footage: `answers.json` `fake_footage: true` installs the real language model,
  embeddings and speech recognition with the fake clip source (and `fake_voice` for the voice), so the whole
  pipeline can be proven on a PC without a graphics card or a stock-footage key; setup says so, and the
  Settings payload shows `dev_mode` and `fake_footage`.
- GPU path proven without a card: every CUDA pin downloaded and hash-checked, both llama.cpp builds start and
  stop only at the driver, the whisper DLL set is complete. In the setup window's PC check, a PC with no
  graphics card no longer shows the driver line as "The driver is older than RTX 50-series cards need" (the
  line now says the driver was not checked, and the red graphics card row carries the fix), and an RTX
  50-series card whose driver version cannot be read (`NS-W115`) is told it gets the CUDA 13 build, which is
  what step 2 downloads. `BUILD_SPEC.md` 1.2 and 21 state the real driver rule (CUDA 12.4 for older cards,
  CUDA 13 / driver 580 for RTX 50-series), and the stage 1 and supervisor tests gain an `nvidia-smi` fixture
  for an RTX 5060 Ti.
- Nightshift Code follows the language model's address: the launcher takes the endpoint the engine hands it
  (or the port in runtime.json) and rewrites `opencode.json` before OpenCode starts.
- Supervisor events keep their kind through the dashboard relay (`update` and `run` arrive as themselves).
- `NS_DASHBOARD_NO_HOST` never reaches the engine's children.
- Loudness: the limiter reaches -14 LUFS in two measurement passes and narration is limited once, at the
  voice stage.
- Clips: the clip-selection prompt asks for emphasis words and the captions use them; the dashboard's layout
  list comes from the clip format presets (your edited formats included; a format you add yourself is listed,
  but the clipper cannot cut with it yet).
- A studio child that stops on an uncoded exception reports NS-E327 with a hint instead of `code: null`.
- An update carries the new version's release notes: the result line of `ns update` and
  `update_state.json` hold them as `release_notes`. The Settings card does not show them yet.
- `studio dashboard [--port] [--open]` starts the engine and opens the dashboard.
- `schedule_registered` means a usable task; `schedule_mode` in Settings names `registered`,
  `interactive_only`, `disabled` or `none`.
- Cancel and failure free every lease owner a job took, not only `studio:<job id>`.
- Series workshop: the missing-story message points to Library > Series instead of a console command.
- Assistant queue: a claim exactly as old as the stale limit can be taken over (a fast machine made the
  takeover test flaky).
- Setup tests describe the PC through the fake probe only (the user-level home variable of an existing install
  no longer leaks into them).

## [0.1.12] - 2026-11-09

From the verification loop of 2026-11-05 (seven defects on the dev install) and the plumbing pass of
2026-11-08 (`docs/PLUMBING.md`; a real-engine install on a PC without a graphics card: real language model,
embeddings and speech recognition with the fake voice, report `D:\NightShift-refs\test-loop-2026-11-08.md`).
Send `Nightshift-Setup-0.1.12.zip`; an install started with 0.1.6 to 0.1.11 resumes under it.

- Clipper: a quiet clip with high peaks (a phone recording, a podcast) now reaches -14 LUFS instead of failing
  QC with NS-E404 at about -17 LUFS. The clip is lifted and its peaks held down before the loudness pass, and
  `render/report.json` says which path each clip took (`loudness.mode` is `linear` or `limited`, with the
  measured values). Narration and Shorts audio that is quiet with high peaks gets the same treatment, so a
  Short no longer misses -14 LUFS for that reason.
- A format edited or added in Library > Formats is now used by the studio: it wrote `config\formats\<id>.yaml`
  and the studio read only the shipped presets, so no edit ever reached a render (found by the plumbing map,
  `docs/PLUMBING.md`).
- Home layout: `exemplars` and `prompt_overrides` (the two folders the studio reads for the owner's own stories
  and prompt edits) are created with the rest; the never-written `stories` folder and the unused clips index
  path are gone. Uninstall also removes `app.failed` and `app.swap` left by an interrupted update.
- A real install on a PC without an NVIDIA card (profile `cpu-dev`) pointed its voice service at the PyTorch
  venv although setup had downloaded the qwentts.cpp binary (the venv exists only on the fallback): the voice
  service could not start. `runtime.json` now says `qwentts` whenever the binary was found, on both profiles.
- `ns verify` on that profile encodes its test clip with libx264 and skips the video-memory measurement
  instead of failing both rows for want of `nvidia-smi`.
- Engine test: the `answers.json` setting `fake_voice: true` installs the real language model, embeddings and
  speech recognition with the fake voice engine (no voice model, no qwentts.cpp download; `tts.engine: fake`).
  It is how the orchestrator proved the real-engine path on a laptop without a graphics card (docs/DEV_MODE.md).
- Variation: when a job's pinned combo fields leave no unused combination in the 30-day ledger, the draw
  repeats one with a warning (`NS-W325`) instead of stopping the job with NS-E325. A series pins hook,
  structure, pacing, caption style and visual treatment, which left its Shorts nine combinations: the first
  series part on the dev install failed at `short_combos`, and in production the second part would have.
- Dev mode: the fake model's series map fits the part count the workshop asked for (a two-part series
  stopped with NS-E302 "returned 3 parts, expected 2"); the workshop's "choose a concept first" and "no
  design yet" messages no longer tell a dashboard user to run a `studio series` command.
- Setup after an uninstall installs again instead of showing Done over an empty folder: the uninstall now
  removes `config\install_state.json` (answers and channels stay), and a setup window that finds the
  program folder gone runs the install steps again, with the Done screen's duration counted from that run
  (found by the windowed dev install on 2026-11-05: it said 30,265 s).
- Clipper import: a video that already sits in its hash folder with its sidecar is recorded without copying the
  sidecar onto itself (WinError 32 on Windows; it stopped every dev clip job from an upload at 1 s).
- Dev mode: a transcript sidecar dropped beside an upload moves with the video into its hash folder (it was
  deleted with the upload folder when the clip job started).
- Dev mode: a clip job from an upload finds a transcript sidecar dropped next to the stored file after the
  upload (it was only read when recorded at import, so every dev clip-from-upload stopped with NS-E406).
- `ns restore` refuses while Nightshift is running ("stop it first, nothing was changed") instead of failing
  halfway on the locked database and putting the files back (found on the 0.1.11 dev install with the engine up).
- A job interrupted by a restart (the engine stopped or the PC restarted mid-job) keeps the stage it was at and
  the studio job it had started, so the card says where it stopped (`NS-E328`) and Retry continues it. Found by
  killing the engine at the voice stage on the 0.1.11 dev install: the card lost the stage and Retry answered
  "stopped before it made anything to continue from".

## [0.1.11] - 2026-11-05

From the clean-teardown test loop on the 0.1.10 dev install (one test at a time, each torn down and the
process list checked; report `D:\NightShift-refs\test-loop-2026-11-05.md`). Send `Nightshift-Setup-0.1.11.zip`;
an install started with 0.1.6 to 0.1.10 resumes under it (INSTALL.cmd, Continue).

- Stop-all now closes the Nightshift window too (it stopped the engine and the dashboard and left the WebView2
  window on a dead page, 150 MB, in the 0.1.10 teardown test); the window also closes itself when its server
  stops answering for 15 s (the nightly task, an update, uninstall).
- Create wizard: the Review step keeps its Back button (it had only Start; the way back was the step rail).
- `studio run --daily` says "nothing due tonight" on the console when the planner has nothing to make (it printed only the backup path).

## [0.1.10] - 2026-11-04

From the browser-driven user journey on the 0.1.9 dev install (docs/DEV_MODE.md; every page and flow
clicked through in Chrome, the Pixabay key tested live). Send `Nightshift-Setup-0.1.10.zip`; an install
started with 0.1.6 to 0.1.9 resumes under it (INSTALL.cmd, Continue).

- Dev mode: the fake language model now turns plain requests in the Assistant into tool calls ("fetch two
  clips of a dark hallway", "what is the status", "pause Dev Night Tales", "where is the footage cache",
  "make a Short for ..."), answers in text after the tool ran (the paths it returned, or the Yes/No card it
  raised), and the Library's footage fetch uses the synthetic clip source when no key is stored. The
  Assistant's whole path (confirm card, tool run, result) can now be tried on a dev install.
- Create page: a finished job card (done, stopped or cancelled) has a Hide button; the job and its videos stay,
  the card leaves the Earlier list (`POST /api/jobs/{id}/dismiss`). Library > Footage shows each clip's file
  path and has a "Fetch 2 clips" button for the search words (the Assistant's `fetch_footage`, by hand).
- Settings > Stock footage keys: a saved key is checked with the service right away (the same check as the
  installer, with the product User-Agent) and the toast says "Pixabay accepted the key"; a key the service
  rejects is not stored (the working key, if any, is kept) and the dialog says to paste it again; when the
  service cannot be reached the key is stored and the toast says it could not be checked.
- Channel card: "This week N of P made" counts the jobs that made a full video (the plan is full videos a
  week); a Shorts-only channel counts every job. The dev channel showed "5 of 1 made" after four one-Short
  jobs and a clip job.
- Assistant: after a Yes the card now says what happened, not only "Done": the file paths a tool made (fetch
  footage), the job id it started, or its note. The model gets no turn after a confirmed action, so "fetch
  two clips and show me where they are" used to end with the paths only in the transcript.

- Dev mode: a clip job on any video now runs through selection, cutting and rendering: the fake model's
  `clip_select` answer is fitted to the window in the prompt when the recorded fixture's moments (62-262 s of
  a test recording) fall outside it (the first dev-mode clip job failed with NS-E407). The clipper's dev-mode
  transcript sidecar is `<video stem>.transcript.json` next to the video (docs/DEV_MODE.md).
- Clips stage: when every candidate for a scene is ruled out by the reuse rules (14 days on the channel, 30
  across channels) the scene reuses footage from an earlier video with a warning (`NS-W405`) instead of
  failing the whole master after the writing and the voice (the first dev-mode nightly run died with
  NS-E402 "24 candidates for 2 slots"); when the pool is smaller than the video (a master plus its Shorts on
  a niche with few clips) a clip may repeat inside the video, again with the warning.
- Nightly planner: a master job that failed no longer counts as the week's master (its combo row did, so the
  channel went silent for the whole spacing, a week at one master a week); finished long videos count, and a
  long job started in the last 12 hours counts as still running.
- Quality check: a derived Short that fails QC (the first dev-mode master died with "s5: duration", 61 s
  for a 50 s bucket, after the master and four Shorts were rendered) is left out with `NS-W406` and listed
  in `qc\\dropped.json`; the master and the other Shorts are published. The master's own QC failure, or
  every Short failing, still stops the job.

## [0.1.9] - 2026-11-04

From the clean dev-mode install of the official 0.1.8 zip on the orchestrator's laptop. Send
`Nightshift-Setup-0.1.9.zip`; an install started with 0.1.6 to 0.1.8 resumes under it (INSTALL.cmd, Continue).

- A stock-footage key saved by an earlier setup (Credential Manager or `config\\secrets.dpapi`) that the
  service rejects no longer stops setup: it is forgotten with a notice, and Settings takes a new one (the
  first clean dev-mode install of 0.1.8 died with NS-E502 on a placeholder key left behind). A key given in
  `answers.json` that is rejected still stops setup. Settings clears a key when it is saved empty, and the
  uninstall forgets both copies of the keys.

## [0.1.8] - 2026-11-04

The first release that ran end to end on a real PC before shipping: a dev-mode install on the orchestrator's
laptop (`INSTALL.cmd --dev`, docs/DEV_MODE.md) exercised stage 1, the setup server and window (WebView2 host),
every step, the shortcuts, the engine, the dashboard, a job with a real FFmpeg render, an update over the
install, and the uninstall. Also two review rounds (installer, studio) and the assistant fit work. Send
`Nightshift-Setup-0.1.8.zip`; an install started with 0.1.6 or 0.1.7 resumes under it (INSTALL.cmd, Continue).

- First dev-mode run on a laptop (`INSTALL.cmd --dev`, unattended): the welcome step waited for Enter and
  died with `EOFError` when standard input was not a console; the console UI now treats end-of-input as
  "not interactive" and takes the defaults, and an unattended run never waits for Enter. A setup that fails
  before the engine venv exists could not write the diagnostics zip (`ns doctor` is not installed yet); the
  wizard now writes a plain zip of the logs, install state, answers and bootstrap files itself
  (`installer/stage2/diagzip.py`).
- Launchers (`launchers/_common.ps1` `Get-NsHome`): the install home is found from the launcher's own folder
  (`<home>\app\launchers`) when `NIGHTSHIFT_HOME` is unset or points at a folder without `config\runtime.json`;
  the first dev-mode dashboard launch from a process started before setup had set the variable looked under
  `%LOCALAPPDATA%\Nightshift` and stopped with "Supervisor venv not found".
- Dashboard: an `/api/health` route for the supervisor's readiness probe (`ServiceDef.health_path`); without it the
  dashboard service stayed "starting" and the engine pill said "Loading dashboard..." for ever (first dev-mode
  launch). Dev mode no longer needs a stock-footage key to start a job (its clip source is a fake).
- Renders are bitrate-capped (`-maxrate 12M -bufsize 24M`, NVENC and libx264, masters, Shorts and clips) and
  the CPU encoder never uses `ultrafast`: the first dev-mode Short came out at 134 Mbit/s (900 MB for 54 s)
  with `libx264 -preset ultrafast -crf 21` under the film-grain filter.
- Story validators: the repeated-opening check (`opening_similarity`) uses cosine >= 0.9 instead of 0.8 (nomic
  similarities between two openings of the same niche sit around 0.8, a false rejection fails the job after
  the writing), and dev mode skips it (the fake LLM tells the same story every run: the second dev job failed
  with NS-E303 `cosine 1.00`).
- Unattended and console setup runs write `logs\\install-report.json` too (built from `install_state.json`,
  answers without keys, the last error), so the owner gets the same file whichever way setup ran.
- The setup window opened over an install finished by the console or unattended path (which has no samples
  step) showed "Setup is halfway" instead of Done; `v01_samples` is now recorded as skipped for such an
  install and the window opens on Done with Open dashboard.
- The setup window wrote naive local timestamps into `install_state.json` while the console wizard wrote UTC
  ones; subtracting the two for the Done-screen duration raised `TypeError` and the window never started
  over a console-finished install (exit 14). Both now write UTC stamps and the arithmetic tolerates a mix.
- `install_state.json` carries the version of the program that ran setup last: after an update through
  INSTALL.cmd the Settings page showed the old install version.
- Dashboard Queue page: the review list reloads when the outbox counts change (it said "Nothing to review"
  under a "Ready 2" tab after the first dev job), and the "Next automatic run" countdown and "Tonight's run
  starts at" line only show when the nightly task is registered (the pill said "No nightly task" beside a
  13 h countdown).
- Uninstall also removes the setup environment and the run/state/tmp folders under `%LOCALAPPDATA%\Nightshift`,
  the install folder's `state` and `tmp`, and (with "delete my videos") `stories`; the first dev-mode uninstall left
  them behind. The "kept" line lists `stories` when it stays.

### Installer dev mode (`orchestrator/installer-dev-mode`)

`INSTALL.cmd --dev` (or `NIGHTSHIFT_DEV_MODE=1`) runs the real installer end to end on a PC with no NVIDIA card and
without downloading any model or PyTorch, so stage 1, the setup server and window, steps s01 to s10, the shortcuts, the
first dashboard launch, the supervisor and a "Create it now" job can be exercised on a laptop. It is not `--simulate`:
the steps run for real; only what a laptop cannot do is replaced. Read `docs/DEV_MODE.md` for what it proves and what it
does not.

- **Stage 1** (`-Dev`): the graphics card, driver and video memory rows are information rows, memory warns under 4 GB,
  disk needs 10 GB; the profile is always `cpu-dev`; the hand-over has `dev_mode: true` and the setup server gets `--dev`.
- **Setup window**: the plan lists the CPU build and the models as "(stub, dev)"; `state.devMode` shows
  "DEV MODE: fake engines, no models" in the footer of every screen, and the Done screen lists stubs and fakes.
- **Steps**: s02 downloads the CPU llama.cpp build, yt-dlp and the window files and skips cudart, OpenCode and qwentts.cpp;
  s03 writes 4 KB stubs with the manifest names (`.dev-stub` marker, hash `dev-stub`, never accepted by a real install);
  s04 installs the new `dev-install` extra (studio without faster-whisper, PyAV and CTranslate2; no cuBLAS/cuDNN, no
  `tts` venv); s05 and s06 are skipped; s08 verifies against the fakes; s09 registers the night task disabled
  (`ns schedule install --disabled`, `schedule.json` mode `disabled`). Installing for real over a dev install redoes the
  later steps and replaces the stubs.
- **Fake engines** (`runtime.json` `dev_mode`): the supervisor starts `ns_supervisor.devfake` (llama, embed) and
  `ns_tts.server --engine fake`; `ns verify` passes against them (libx264 replaces NVENC, Whisper and VRAM are dev-mode
  passes) and its report says `dev_mode`; studio and dashboard jobs run on `FAKEABLE` with a real FFmpeg render.
- **Contracts** (CR-DEVMODE-1): `Runtime.dev_mode`, `InstallState.dev_mode`, `GateReport.dev_mode` and their schemas.
  `ns tune` and `ns doctor` know the mode; the Settings page hides the flag.

### Installer round 3 (`orchestrator/installer-round3`, from the round-3 installer gap report)

- **INSTALL.cmd over a finished install no longer opens an empty window (blocker B1, a 0.1.7 regression).** On
  start the setup server compares the installed program (`<home>\app`, the appcopy fingerprint: VERSION plus hashes
  of `manifest.json` and `pyproject.toml`) with the extracted one. A different program: b02, b03, s04, s08, s09
  and s10 go back to pending (in the window and in `install_state.json`), the window shows the Welcome resume
  screen, and Continue copies the new program (the old one stays as `app.prev`), refreshes the environments, runs
  the final test, remakes the shortcuts and the night task, and ends on Done. The same program: Done opens at once
  with the result a finished run builds (home, outbox, night run time, diagnostics zip) and **Open dashboard**
  works. An install that stopped half way is left alone. This restores the documented paths "update by running
  INSTALL.cmd from the new zip" and "open Nightshift by double-clicking INSTALL.cmd".
- Resume on a nearly full drive (L1): the free-disk row of a locked resume is a warning, not a blocker, when free
  space plus what setup already holds under the install folder reaches the 40 GB limit ("N GB free on D:; setup
  already holds X GB here"). The 2 GB guard during downloads and `NS-E104` from the steps still protect the run.
- Final test (L2): `s08` runs `ns stop` before the port check, so a detached engine left by a closed console is
  stopped instead of ending as `NS-E116` against Nightshift's own python.exe. `NS-E116` no longer says "change
  the port in Settings" (there is no such setting): "Close the program using that port (the message names it), or
  restart the PC, then press Retry".
- The unattended run (L3, `answers.json` next to INSTALL.cmd) holds `SetThreadExecutionState` like the window does,
  and takes a lock (`<data>\bootstrap\setup.lock`, released by Windows if the process dies): a second unattended
  setup says "Setup is already running on this PC" and exits with code 15 (A1-#14).
- Stage 1 (P2, P3, A1-#25, A1-#26): it uses the pinned uv (`manifest.json` `bins.uv`, 0.13.6) and downloads it when
  the copy it finds, including one on PATH, is another version; it asks uv for the managed Python 3.12 only
  (`UV_PYTHON_PREFERENCE=only-managed`, also in `ns_common.uv.uv_env` for b03, s04 and `ns update`) and never takes
  a Python from a `WindowsApps` folder (Microsoft Store alias); it runs `Unblock-File` on the extracted programs
  (`.dll .exe .ps1 .cmd .pyd`) so the WebView2 helpers load and the setup window no longer falls back to Edge on a
  browser-downloaded zip; the setup server gets 60 s to answer (was 20) and the splash says it is still waiting
  after 15 s.
- **Open dashboard (P1, A1-#20):** the engine the Done screen starts no longer inherits stage 1's `PYTHONPATH`
  (the extracted folder) or `NIGHTSHIFT_BOOTSTRAP_UV`, so it and its dashboard import the program from
  `<home>\app` and keep working when the extracted folder is deleted. The same variables are cleared in
  `Start-NsSupervisor`. The engine started by a shortcut and the 03:00 task now run with the install home as their
  working directory, not the program folder, so an update is no longer blocked while either runs.
- Windows Security (P4): `NS-E113` for Smart App Control (WinError 4551, "Application Control policy") says that it
  blocks programs it does not know and cannot allow one file: "Turn it off in Windows Security > App & browser
  control > Smart App Control settings (this cannot be turned back on without reinstalling Windows), then press
  Retry". Antivirus blocks keep "Protection history, allow the file". `contracts/errors.json`, the operator copy,
  the window's fallback copy and OPERATOR.md say both.
- Browser windows (P6, P7): the Edge or Chrome fallback of the Dashboard launcher gets `--no-first-run
  --no-default-browser-check`. The Dashboard launcher starts the WebView2 window as its own hidden PowerShell
  process (`-STA`, working directory = install home) and ends once the window reports ready, instead of keeping
  the window in its own process: no "Windows PowerShell" console is left behind the dashboard when Windows Terminal
  is the default console, and closing it no longer closes the dashboard. A host that exits 3 (WebView2 missing) or
  fails still falls back to the browser app window.
- One engine per install (P8): `python -m ns_supervisor.app` takes `run\supervisor.lock` (a lock Windows drops with
  the process) before anything else; a second start prints "The Nightshift engine is already running" and exits 0
  without touching `run\supervisor.json`, which the failed second start used to delete on its way out.
- Keys (P9): `s01` already puts the stock footage keys in Credential Manager and `config\secrets.dpapi` as soon as it
  runs, so a restart after s01 keeps them; the server's in-memory copy only covers the minutes between Start and
  s01. A resumed window now reports what is really stored (`pexelsKeySet` / `pixabayKeySet` from `secrets.dpapi`,
  not "set because the questions step is done") and, when neither key is there, adds the notice "No stock footage
  key is stored. Add a Pixabay key (free) in Settings > Stock footage keys before the first video".
- A2-#13: `dev/make_bundle.ps1` already rewrites `manifest.json` `version` from VERSION; `tests/test_installer_rerun.py`
  now proves the chain (bundle manifest, `<home>\app` copy, wizard manifest) makes `s04` and `s08` run again after
  an update through INSTALL.cmd. OPERATOR.md step 4 says that an `answers.json` makes setup unattended (no window;
  `INSTALL.cmd -Ui` for the window), and its update and key paragraphs describe the new INSTALL.cmd behaviour.
- Deliberately left (listed in the PR): system proxy and TLS-scanning antivirus (A1-#15 / A2-#12), a retry with the
  CUDA 12.4 pair after `NS-E203` (A1-#17), the PyTorch fallback limits (A1-#23, A1-#24), a constraints file or
  `uv.lock` (A1-#12), and a key field on the Welcome resume screen (P9 only reports a missing key).

### Studio round 3: engine stays up through a job, keep-awake at night, no-key refusal up front (`orchestrator/studio-round3`)

From the third studio review of 0.1.7 (`gaps-round3-studio`). Nothing here has run on the GPU PC yet.

- **The engine no longer leaves in the middle of a job (L1).** After `captions` every stage holds no GPU service, and
  the engine's 30-minute idle clock only saw acquires and releases, so a long download-and-render stretch made it exit
  (the next job then failed with NS-E202 "supervisor unreachable", every night after the first video). `POST /heartbeat`
  now restarts the idle clock for a `studio:` run or the dashboard job worker (`dashboard:job`) even when it holds
  nothing. `StudioAcquirer` beats from its first `acquire` until `run_finished` (with the run's owner while nothing is
  held), the dashboard's job worker pulses the engine every minute while a job runs, and an `acquire` that meets "supervisor
  unreachable" connects again once (the nightly run starts a new engine) before it fails.
- **The PC stays awake (L2).** `ns_common/keepawake.py` (`ES_CONTINUOUS | ES_SYSTEM_REQUIRED`, per thread, released in
  `finally`) is held around `run_daily`, around `studio run`, and for the length of every job on the dashboard's worker
  thread. The installer keeps its own identical copy (its web package may import only the standard library); a test
  keeps the two equal.
- **No stock-footage key means no story video, said up front (L3).** The nightly planner (per job, before a combo is drawn),
  `studio run` and the dashboard's Run now / Retry refuse with NS-E502 "No stock footage key. Add a Pixabay key (free)
  in Settings > Stock footage keys" before any GPU work. When both sources fail, the Pixabay error is raised first
  (Pexels gives out no keys). The setup text now says videos cannot be made until a key is added.
- **The voice stage's time limit follows this PC's speed (L4).** `ns verify` stores the slowest voice mode's real-time
  factor in `config/tts_speed.json`; the batch limit is 120 s plus `max(3, 6 x rtf x 1.5)` s per line (unchanged without
  a measurement), and a batch that times out is retried once at twice the limit, resuming from the items the service had
  finished.
- **Embed server off the card (P1)**: started with `CUDA_VISIBLE_DEVICES=-1`. **llama-server always `-np 1` (P2).**
- **Pending setup voice card (P3)** says a video cannot be voiced until the voice exists (NS-E311) and points at
  "Create it now".
- **The Assistant's GPU hold is leased (P4)**: it beats every 30 s during a turn and the engine reclaims a silent hold
  after 10 minutes, like a studio lease.
- **Retry from where it stopped (P5).** A failed story job on the Queue / Create pages has a button that continues the
  same studio job from its failed stage (`POST /api/jobs/{id}/resume`, assistant tool `resume_job` with a confirm card).
- **Updater (P7).** Settings > Update from a file now installs `Nightshift-Update-<version>.zip` together with its
  `.sha256` (`POST /api/update/file`, then `ns update <zip>`); the release notes say the checksum is required. A GitHub
  error status (403/429 rate limit, 404, 5xx) is NS-E118 and says so instead of "check the internet connection"; the
  Settings page shows the `error` line of `ns`, not the `hint:` line after it.
- **Reject moves the files (P8)**: the dashboard's Reject takes the video, its sheet and thumbnails out of the Outbox
  (same code as `studio review reject`, into the `rejected` folder of the Nightshift home, under channel, day and job). **Whisper failures are NS-E411**
  (was NS-E202).

### Installer: first-run support (`orchestrator/installer-first-run`)

What the owner needs to help a partner who cannot describe what went wrong, and the copy a non-technical partner reads.
This branch edits `contracts/errors.json` (two hints), so it is an `orchestrator/` branch.

- **Copy status** (every screen after Start: Welcome resume, Downloading, Installing, Testing, Failed, Done). One click
  puts a line like `Nightshift setup 0.1.7 · step 6/14 Download AI models · 41% · 12:03 elapsed · last error NS-E105 (A
  download site cannot be reached)` on the clipboard (new action `copy-status`; `installer/stage2/web/report.py`). The
  last error survives a Retry.
- **`install-report.json`** next to `verify-report.json` (the `logs` folder of the install; the setup data folder before the install
  folder exists). Written at every failure, stop and finish: version, outcome, failed step, code, attempts, last error,
  machine (Windows, graphics card, memory, driver, CUDA, free space), choices, engines (voice engine, captions device,
  encoder, fallbacks, schedule mode), warning codes, every step with its status and seconds, the answers without keys.
- **One diagnostics zip with the setup evidence.** `ns doctor --bundle` and the setup window's fallback now both hold
  `install-report.json`, `setup/wizard_flags.json`, `setup/answers.json` (keys removed), `setup/preflight.json` (the
  hardware check), `setup/setup-server.err` and `setup/bootstrap/*`. The name carries the minute
  (`nightshift-diagnostics-2026-11-04-1203.zip`), so two zips on one day do not overwrite each other. The zip the wizard
  makes before the last step gets the final report added when setup ends.
- **Failed screen.** Buttons Save a help file (was Copy diagnostics), Open log folder and, for NS-E113, Open Protection
  history; one sentence says where the file is and who gets it ("Send this file to <name>", from the new
  `installer/support.json`, or "the person who sent you Nightshift" until the owner fills it in). Wake timers (NS-W107)
  get an Open power settings button. New action `open-windows-page` (`power`, `protection-history`; names only).
- **Done screen.** Three numbered next steps (Open dashboard; Create it now once with the graphics card idle; tomorrow
  morning's note and the schedule), the "Add a Pixabay key in Settings before the first video" reminder when no key is
  stored (`result.stockKeyMissing`), warnings now show their instruction next to the title, "What was installed" is a
  fold-out.
- **Copy fixes** (`installer/ui/src/copy.ts`, `operator_copy.json`, `contracts/errors.json`, `OPERATOR.md`, Settings):
  no assumed download speed or "black window"; Choices says which choices are final; model cards without "context";
  step and check names a partner understands ("Program parts", "Story memory", "graphics card video"); "Copy full
  test report (for the developer)"; uninstall no longer says "Nothing else was changed on this PC" and lists what stays
  (settings and logs, keys, Visual C++ runtime, FFmpeg, Windows Terminal, the Python download cache); Settings names the
  `Nightshift-Update` zip and the folder UNINSTALL.cmd is in. The UI's code table is now `operator_copy.json` itself, so
  the two can no longer disagree.

### Assistant slice 1: make it fit (`feat/assistant-fit`)

The Assistant overflowed the local model's 16,384-token context in long chats (about 9-10k tokens were used before any
history: the prompt, a 1,700-token system map, all 59 tools, and tool results of up to 6,000 characters).

- **Prompt budgets** (`assistant/prompt.py`). Every request is planned against the context from `runtime.json`
  (`llama.flags.ctx`, 16,384 when unknown): 16k = reply 1,200, system 1,300, tools 2,500, context block 2,500, margin
  1,000, history the rest (7,884); 32k = 1,500 / 1,300 / 3,000 / 4,500 / 1,500 / 20,968. One estimator everywhere
  (`approx_tokens` plus 10 %). The reply limit (`max_tokens`) now follows the plan.
- **Stable system message, live facts on the question.** The system message is the same bytes for the whole run of the
  app (no live state), so llama-server keeps its cache; engine state, GPU owner, videos waiting, jobs, next run,
  channels, the page and a reminder are attached to the latest user message once per turn, labelled as data. They are
  never stored in the transcript.
- **History compaction.** Tool results of earlier turns become one-line digests; this turn's results are kept up to
  1,500 tokens and shrunk by re-building the JSON (summary fields kept, lists and strings shortened), never cut through
  the middle of a string. Over budget, the oldest whole turn goes first, so a tool call is never split from its result.
  Stored results are no longer truncated at 6,000 characters (they are rebuilt smaller above 20,000).
- **Topic router and trimmed tools** (`assistant/router.py`). No model call: the page, keyword phrases, ids, `NS-` codes,
  channel slugs and the previous turn's topics pick at most two topics. The model sees 8 core tools (`status`, `find`,
  `where_is`, `open_path`, `search_docs`, `diagnose`, `explain_error`, `list_tools`) plus at most 10 tools of those
  topics: at most 18 instead of 59. Calling `list_tools(topic)` adds that topic's tools for the next step of the turn.
  `fetch_footage` is in the library topic; the 12 workshop tools appear only with the series topic.
- **Task-shaped read tools** (`assistant/composite.py`): `diagnose` (job, failed stage, plain meaning of the code, log
  lines, policy retries, next step), `review_brief` (policy notes, disclosure, earlier rejections on the same options;
  the owner still decides), `what_worked` (best options per pool with the number of videos and a confidence word),
  `tonight_plan`, `video_stats`, `get_story` and `open_path` (opens a folder inside the Nightshift home only).
- **Model alias `code`** instead of `story` for the Assistant: in two-model mode `story` is the 21 GB writing model.
- **Docs scope.** `search_docs` and `find` serve `README.md`, `OPERATOR.md`, the error catalogue and a generated list of
  what the Assistant can and cannot do. `BUILD_SPEC.md` and `docs/ENGINEERING.md` are no longer indexed (they describe a
  build, not this install). The system prompt is version 2.
- **Pasted keys are blanked** (`assistant/redact.py`) before they are stored in `assistant.db` or sent to the model;
  the next turn tells the model to point to Settings > Stock footage keys.
- **Offline evaluation** (`assistant/eval.py`, `tests/assistant_eval/`): the 20 partner questions on the scripted fake
  model assert the topics and tools offered, the retrieval scope, the token budget of every request (also after a
  30-turn history) and the confirm gate. No database schema change.

## [0.1.7] - 2026-11-04

Hotfix after the partner's first run of 0.1.6 and two fresh audits (install blockers from the Start button
onward, and installer functionality). Do NOT send 0.1.5 or 0.1.6 to a new PC; send `Nightshift-Setup-0.1.7.zip`.
An install started with 0.1.6 resumes under 0.1.7: run INSTALL.cmd from the new zip and press Continue.

### 0.1.7 install-path fixes (`orchestrator/hotfix-0.1.7-install`, from the second install audit)
- faster-whisper and PyAV (blocker): the `studio` extra now pins `faster-whisper>=1.0,<1.3` and
  `av>=11,<19`. faster-whisper 1.2.1 calls `av.open(metadata_errors=...)`, which PyAV 19.0 removed, so on a
  fresh install `uv` resolved av 19.0.2 and every transcription raised `TypeError`. The partner would have
  reached the end of the roughly 20 GB download, then seen "speech recognition failed on the processor"
  (`NS-E202`) in the final test, and every caption and Clipper job would have failed the same way. The caps
  reach the one venv that runs faster-whisper through `s04`, the setup venv (`b03`) and `ns update`, which all
  install `nightshift[studio,...]` from `pyproject.toml`; `tests/test_install_caps.py` fails if either cap goes.
- The PC no longer sleeps during setup: the setup server holds `SetThreadExecutionState(ES_CONTINUOUS |
  ES_SYSTEM_REQUIRED)` on the thread that runs the steps and releases it when the run finishes, fails or is
  stopped. The partner would have found the download stopped after a 1 to 2 hour wait. A downloader that gets a
  network error now probes the site's host and waits (2, 4, 8 ... up to two minutes) for the network to come
  back before it counts the attempt, so a PC that woke from sleep no longer burns its three tries in seconds
  and shows `NS-E105` ("a required download site is unreachable"). The row reads "No internet right now,
  waiting for it to come back".
- Visual C++ runtime: stage 1 (the black window, in front) now installs it when it is missing: it downloads
  the pinned `bins.vcredist`, checks the SHA256, says "Windows will ask for permission to install the
  Microsoft Visual C++ runtime; press Yes", runs it with `Start-Process -Verb RunAs -Wait` and reports the
  result. The partner would have seen the old prompt only as a flashing taskbar shield behind the setup
  window, a row reading "installing" for up to 50 minutes and then `NS-E114`. A declined prompt prints the fix
  and setup goes on; the setup window keeps its own path as a fallback with a 10 minute wait, and `NS-E114`
  now says "Look for a flashing shield icon on the taskbar and press Yes, or install it from
  aka.ms/vs/17/release/vc_redist.x64.exe and press Retry".
- Install folders with characters outside printable ASCII (an Arabic or accented folder name) are refused in
  the setup window (instantly, and by the server) and in the console question (`NS-E117`; the question asks
  again) with "Use a folder name with plain English letters, digits, spaces, - and _ (the model programs
  cannot open other paths)". llama.cpp, qwentts.cpp and CTranslate2 open model paths through the ANSI code
  page, so such a home made `ns tune` fall back to defaults and the story model fail its test (`NS-E203`).
- A program Windows refuses to start (Smart App Control `WinError 4551`, an Application Control policy
  `1260`, antivirus `225`/`226`, "the file contains a virus") is `NS-E113` with "Windows blocked <file> when
  it was started ... Allow it in Windows Security > App & browser control, then press Retry", from the engine's
  service start, the installer's command runner and the failed checks of the final test. The partner would
  have seen "a service failed" (`NS-E202`), "the story model did not pass" (`NS-E203`) or a quiet switch to the
  7 GB PyTorch voice engine.
- uv failures show uv's last 15 lines on the Failed screen instead of a bare "A required download site is
  unreachable"; a full disk is `NS-E104` and a blocked program `NS-E113`. Every uv call (setup venv, `s04`,
  the PyTorch fallback, `ns update`) now uses `UV_CACHE_DIR=<home>/cache/uv`, so the roughly 3 GB uv cache
  fills the install drive, not C:. If that folder cannot be made, `s04` checks that C: has 8 GB free first
  (`NS-E104`). uv's own managed Python (about 30 MB) stays where uv puts it.
- Busy ports: before the final test (`s08`), before an engine start and before a service start, the ports
  (story model 8080, memory index 8081, voice 8091, engine 8099, dashboard 8100) are bind-tested. A taken one
  is the new `NS-E116` naming the port and the program that holds it (psutil) and saying "Close that program
  or change the port in Settings". The partner would have seen "the story model did not pass" with no port.
- Final test: a stale `logs/verify-report.json` is deleted before each `ns verify`, so a Retry whose verify
  died before writing can no longer pass on an old report. The supervisor's start timeout (a fixed 120 s, too
  short to load the 14 GB story model from a hard disk, which would have ended as `NS-E203`) is now read from
  `config/supervisor.json`: setup writes 600 s when the install drive is not an SSD (or is unknown) and 300 s
  otherwise, and the nightly run uses it too.
- Not in this change: certificates and proxies, pinning Hugging Face file revisions and the qwen-tts temp
  folder (`orchestrator/pins-0.1.7`), re-syncing venvs when INSTALL.cmd is run over an existing install.


- Installer: running INSTALL.cmd again after the questions step (window closed, PC slept or rebooted, power
  cut, update over an install) showed a greyed-out Choices screen whose Install button re-sent the locked
  answers and stopped; the setup window now opens on the "Setup is halfway" screen and Continue resumes the
  run from the first unfinished step (`machine.derive_phase` follows the steps when the answers are locked).
### Assistant: pull stock footage now (`feat/assistant-fetch-footage`)

- **`fetch_footage` tool and `POST /api/library/footage/fetch`.** The Assistant (after the owner's Yes) can search
  Pixabay and Pexels for a scene and download up to 5 clips into `cache\clips\<source>\`, the same folder and `clips`
  table the nightly jobs use, so later jobs reuse them and Library > Footage lists them. The answer starts with the
  absolute file paths, then source, clip id, page link, licence, size, duration and keywords. Clips already in the cache
  are not downloaded again; a dead download is skipped as long as another succeeds. It is a plain network download:
  no GPU lease and no studio job. Keys are read from Credential Manager or the DPAPI file; with none stored the answer is
  `NS-E502` "No stock footage key. Add a Pixabay key (free) in Settings > Stock footage keys" (keys are entered only in
  Settings, never in chat).
- **`where_is` answers "footage cache", "stock footage", "stock clips" and "clip cache"** with the cache folder.
- Confirm cards show a tool's schema defaults (count 1, landscape, auto) instead of "(default)".
- `ClipCache.record` is the one place that writes a `clips` row (used by `ensure` and by the new tool).

### Pins and voice-engine temp files (`orchestrator/pins-0.1.7`, from install audit 2, findings 3 and 9)
- Every Hugging Face file in `manifest.json` (all LLM tiers, embed, voice GGUFs, whisper) now names the commit
  it is fetched from (`revision`, a 40-character sha), so a re-upload of a file under the same name on `main`
  can no longer fail every download with NS-E109. Each file was checked at that commit: it exists, its size
  and the LFS sha256 equal the manifest. The PyTorch fallback repos (and the VoiceDesign weights) are pinned
  in `tts_fallback_revisions`. `HfFile.revision` defaults to `main` for an entry that names none, and the
  custom model chosen in setup still comes from `main`. A test fails if the shipped manifest holds `main`.
- `bins.yunet` points at the opencv_zoo commit that holds the file instead of `raw/main`; its sha256 is
  unchanged (re-hashed from the commit URL).
- Voice engine: `qwen-tts.exe` and `qwen-codec.exe` never convert argv to UTF-8, so a path with Arabic (or
  other non-ASCII) letters, such as a `%TEMP%` under that Windows user name, made the voice test fail with
  "WAV write failed" and setup fell back to the 10 GB PyTorch engine (NS-W301). The engine now keeps every
  scratch file handed to the tools under `<home>\cache	ts-tmp` (`Paths.cache_tts_tmp`), one folder per call,
  removed afterwards (leftovers older than a day are swept). A voice whose files sit on a non-ASCII path is
  copied there before the tool reads it. The test stand-in now rejects non-ASCII path arguments like the real
  tools do.

## [0.1.6] - 2026-11-04

### Hotfix (`orchestrator/hotfix-0.1.6-key-check`, from the partner's first run of 0.1.5)
- Installer, API keys screen: the live check of the Pexels and Pixabay keys sent Python's default
  `User-Agent` (`Python-urllib/3.12`). Cloudflare in front of both services answers HTTP 403 (error 1010)
  to that signature, so every valid key was reported as `NS-E501`/`NS-E502` ("not accepted, copy the key
  again, no spaces"). The check now sends `Nightshift-Setup/<version>`; verified live against both
  services. The studio's own stock-footage client (httpx) was never affected.
- Both stock-footage keys are optional, so setup never blocks on them: Pexels has paused new API keys, and a
  Pixabay key can be added later in Settings > Stock footage keys. The setup screen marks both fields optional
  with a hint, a pasted key is still checked live, the console wizard lets Enter skip a key and prints a notice
  when none is given, `s01`/`s07` no longer require a stored key, and the studio builds only the clip sources
  that have a key (both when none, so the first video's clips stage fails with `NS-E502`, whose hint now says
  to add the key in Settings).
- Pixabay video renditions: the studio takes the smallest rendition that still covers a 1080p master (Pixabay
  now serves `large` at 3840x2160, about 35 MB per clip; `small` is 1920x1080 at about 10 MB). The request
  (`/api/videos/`, `video_type=film`, `safesearch=true`, 24 h response cache, 90 requests/min) was checked
  live against the current Pixabay API docs and response.
- Do NOT send v0.1.5: its key screen rejects every key. Send `Nightshift-Setup-0.1.6.zip`.

## [0.1.5] - 2026-11-03

### Final pre-tag fixes (`feat/release-0.1.5-pretag`, from the last review before tagging)

- **Shortcuts and the nightly task follow the program folder.** Running `INSTALL.cmd` over an install made with an
  older version moved the program to `<home>\app`, but the Desktop shortcuts and the "Nightshift Daily" task kept the
  folder the zip had been extracted to (and Task Scheduler could not start the 03:00 task once that folder was
  deleted, as `OPERATOR.md` suggests). s09 now records `shortcuts_app` and runs again when it differs from the
  program folder, which rewrites every shortcut and re-registers the task with the new command and working directory.
- **`ns update` marks the program folder as finished.** It writes `.ns-app.done` (the same text setup writes) into the
  new tree, and `ns update --rollback` into the restored one. A later `INSTALL.cmd` run used to find no marker, call
  `<home>\app` "an unfinished folder from an earlier try" and delete it without stopping the engine; it now swaps it
  with `app.prev` like any update, with the engine stopped first.
- **The CPU fallback for captions is not sticky.** With a graphics card, s08 probes whisper on CUDA on every run. A
  passing probe puts `runtime.json` back on `cuda`; only a failing probe keeps or sets the CPU, with `NS-W404` each
  time. A driver update or a restored DLL no longer leaves captions slow for good.
- **RTX 50 card with an unreadable driver version.** Stage 1 hands over `cuda13` (as s02 chooses), so the setup
  window lists the llama.cpp and CUDA runtime rows that are really downloaded, and the total matches.
- **Zip extracted to a drive root.** `D:\Nightshift` counted as "inside" `D:\`, so the preselected install folder was
  refused. A drive root holds every folder of its drive: only the root itself is refused now, in stage 1 and in the
  window. Copying from a root takes only the bundle's own top-level entries, never the whole drive.
- **Uninstall leaves nothing behind.** It takes the FFmpeg fallback's folder (`<home>\bin\ffmpeg`) out of the user
  PATH, and the console uninstall (used when no Python is found) removes `app`, `app.new` and `app.prev` too.
- **`UNINSTALL.cmd` no longer keeps `<home>\app` alive.** Explorer starts the console in that folder and it waited at
  `pause`, so the emptied folder was left after the window closed. The script changes to `%TEMP%` first, and the
  deferred cleanup keeps trying for two minutes per folder (was 90 seconds).

### Downloads resume, verify and recover (`feat/release-download-hardening`, #61)

- **Real-network resume tests (CI only).** `tests/test_downloader_network.py` (marker `network`, skipped unless
  `NS_NETWORK_TESTS` is set; the CI `test` job sets it and runs them in their own step, failing if any is skipped)
  downloads the pinned `yt-dlp.exe` from the GitHub release and a real file through the Hugging Face redirect with the
  real `download()`, pauses after 2 MiB (1 MiB), checks the `.part` size, resumes and asserts exactly one `Range` request
  from that size, the pinned/straight-download hash, and that the `.ok` marker is written after the hash. A `.part`
  cut short by hand resumes from the cut. The evidence lines go to the job log and summary.
- **Resume edge cases.** A `.part` longer than the server's file, a server that ignores `Range`, and a `416` all restart
  the file from zero without using up a retry (a `416` that says the part is the whole file goes straight to the hash).
  A wrong hash deletes the file and fetches it once more from the start, then `NS-E109` (was three tries).
- **Re-run after a crash.** A finished file whose `.ok` marker was lost (killed between the rename and the marker) is
  hashed on the disk and adopted instead of downloaded again; a marker whose file was deleted, resized or touched is
  ignored; verified files cost two stat calls and no request.
- **No handle held across a pause.** A Pause landing between the downloader's gate check and its progress report used
  to park inside the open `.part` with the connection open (the bridge reporter blocked at the gate). The downloader now
  reports through a non-blocking `update_nowait`, so the file and the connection are always closed before it waits.
  Stop keeps the `.part`; a stop in the middle of a `models_source` copy removes the half copy, and copies use a
  `.copy` file so they can never overwrite or delete a resumable `.part`.
- **Disk full is `NS-E104`**, with the free space at the destination, not a traceback or three network retries.
- **Retries are capped and shown.** At most five tries per file (three by default), waits of 2, 4, 8 ... capped at
  30 s; the row note says "(try 2 of 3)".
- **Smoother progress.** The downloader reports every 64 KiB or 250 ms, whichever comes first (it now reads blocks as
  they arrive instead of waiting to fill 64 KiB). The overall time left rises by at most 20 % from one poll to the
  next unless a file restarted.
- **`ns verify --downloads`** (`installer/stage2/verify_downloads.py`): re-hashes every model and program of this
  install against `manifest.json` and prints ok / missing / bad hash with the fix for each; exit 1 when something needs
  fixing. OPERATOR.md has a new "If a download stops" section.


### Install audit fixes (`feat/release-install-audit`, findings #1 to #8 and #10 to #12 of `install-audit.md`)

- **Visual C++ runtime.** llama.cpp and qwentts.cpp are built against the dynamic MSVC runtime and do not start on a
  clean Windows 11 without it (the symptom used to show only at the smoke test, after the 20 GB download). The PC
  check has a new row "Visual C++ runtime" (`msvcp140.dll`, `vcruntime140.dll`, `vcruntime140_1.dll` in System32).
  Step b01 installs it when missing: winget `Microsoft.VCRedist.2015+.x64` first, then the pinned `bins.vcredist`
  (`VC_redist.x64.exe` 14.44.35211.0, SHA256 in `manifest.json`) run elevated with `/install /quiet /norestart`. The
  smoke test checks the DLLs before it starts llama-server. New `NS-E114` (declined permission prompt, failed
  installer, missing DLLs).
- **The program lives in `<home>\app`.** Setup copies the extracted folder into `<home>\app` (without `venvs`, `models`
  or any `bin` folder but the bundle's `bin\webview2`), records that path as `app_path`, and the venvs, the shortcuts, the
  nightly task, the launchers and `ns update` all use it. The extracted folder can be deleted. The install folder may
  not be the extracted folder (NTFS ignores case: `D:\NightShift` and `D:\Nightshift` were the same folder, so the first
  update tried to rename the folder that held the venvs and models), inside it, or in Downloads or OneDrive; the window
  says so and stage 1 picks `<drive>:\Nightshift-home` as the default when the usual name is taken. Running
  `INSTALL.cmd` from a newer zip over an existing install updates it with the `ns update` swap (`app.prev` is the
  rollback target, `.git`, `config` and `answers.json` are carried over). New `NS-E115`. The setup environment and
  the venvs also refresh when only the version or the program folder changed.
- **Git is not required.** Setup no longer installs it (its machine-wide installer was an extra administrator prompt
  and a way to stop setup). `ns update --git` without Git says "Install Git for this" and names the Git-free ways.
- **FFmpeg fallback.** When winget is missing or refuses, b01 downloads the pinned static build
  (`bins.ffmpeg`: BtbN `autobuild-2026-10-31-12-59`, n8.1.3, SHA256 in `manifest.json`), unpacks `ffmpeg.exe` and
  `ffprobe.exe` into `<home>\bin\ffmpeg` and adds that folder to the user's PATH. `ns_common.ffmpeg` also looks there.
  winget is called with `--source winget`. A PC without a working winget is a yellow row, not a blocker.
- **Re-run over a half venv.** `uv venv` gets `--clear` when the folder exists but has no `python.exe`
  (uv 0.10 and later refuse otherwise); applied once, never in a loop.
- **Antivirus.** WinError 225, a downloaded program that disappears between write and verify, a zip member removed
  right after unpacking, and a `llama-server.exe` or `qwen-tts.exe` that is gone before the smoke test are `NS-E113`
  with the allow-list hint (were `NS-E105`, an unexplained exception, or `NS-E203` and a silent 7 GB detour to the
  PyTorch engine).
- **`manifest.version`** is written from `VERSION` by `dev/make_bundle.ps1` (and checked in the finished zip), which
  is what the re-run refresh of the venvs and the smoke test compares.
- **Morning use.** `/status` reports the Windows session of the engine and whether a run is using it. The Dashboard
  shortcut restarts an idle engine that the 03:00 task started in session 0 (no desktop: Open Outbox and the folder
  pickers did nothing there) and never interrupts a busy one.
- **Wake timers.** "Important wake timers only" (power setting 2) now counts as not enough for the 03:00 task, and
  the row says which setting to change.
- **Voice engine pin.** `bins.qwentts` is the v0.1.4 `Nightshift-VoiceEngine-win-cuda-x64.zip` asset; the unpinned
  fallback URL uses the new asset name first and the old one second.

### Installer: whisper CUDA libraries and a CPU fallback for captions (CR-C1-4 a, b, c):

- **cuBLAS and cuDNN are pinned and installed.** faster-whisper runs on CTranslate2, whose Windows build needs the
  CUDA 12 cuBLAS DLL (and ships a cuDNN 9 loader); a fresh PC has only the NVIDIA driver, so the first video's
  captions ran on the CPU or failed with `NS-E408`. `manifest.json` has a new `python.whisper_cuda` list
  (`nvidia-cublas-cu12==12.8.4.1`, `nvidia-cudnn-cu12==9.10.2.21`, each `--hash=sha256:` of its win_amd64 wheel) and
  s04 installs it into `venvs\studio` in hash-checking mode. Before it used the ranges `>=12.8,<13` and `>=9,<10`.
  The studio extra now also says `ctranslate2>=4.6.3,<4.9` (CUDA 12.8 Windows builds). The DLL folders reach PATH
  through `ns_common.gpu_dlls.ensure_on_path()` (verify) and the transcriber's own `ensure_gpu_dlls_on_path()`
  before `faster_whisper` is imported, so no shell setting is involved.
- **Setup tests a real transcription and falls back to the CPU.** `ns verify --quick` only loads the whisper model,
  and CTranslate2 loads cuBLAS lazily. s08 now transcribes a generated 3 second WAV in the studio venv on
  `device=cuda`; when that (or the model load) fails it writes `device=cpu, compute_type=int8` to `runtime.json`,
  warns `NS-W404` ("Whisper runs on the CPU: captions will be slow") and tests the CPU too. Only a CPU that cannot
  transcribe stops setup (`NS-E202`); the old hard stop `NS-E112` on a GPU failure is gone. `OPERATOR.md` and the
  setup window's copy know `NS-W404`.
- **spaCy model wheel and the PyTorch fallback repos come from the manifest.** `python.spacy_wheel`
  (`en_core_web_md` 3.8.0 and its sha256) replaces the hard-coded URL, installed with `--require-hashes`;
  `tts_fallback_repos` replaces the constant in `installer/stage2/fallback.py`. `Manifest` gained `python`
  (`PythonPins`) and `tts_fallback_repos`, both defaulted; `contracts/schemas/manifest.schema.json` follows.
- `tools/resolve_manifest.py --check` also validates the `python` pins; CI tests compare the pinned cuBLAS/cuDNN
  hashes with PyPI and the spaCy wheel hash with the real download.
- Still open: CR-C1-4 (e), a CUDA 12.8 llama.cpp build for drivers between CUDA 12.4 and 12.7.

### RTX 50-series cards stop early on a CUDA 12 driver
- The only llama.cpp builds Nightshift ships are CUDA 12.4 and CUDA 13.x, and the 12.4 build has no code for
  RTX 50-series (Blackwell) cards. Setup used to warn and go on, then fail the smoke test after the full
  download. The "This PC" check and step s02 now stop with `NS-E103` ("RTX 50-series cards need a driver with
  CUDA 13, driver 580 or newer") before anything is downloaded; an RTX 50 card whose driver version cannot be
  read gets the CUDA 13 build. Other cards keep the 12.4 rule.

## [0.1.4] - 2026-11-03

### Same content as 0.1.3, released
- The `v0.1.3` tag's release build failed on one new test whose expectation was wrong (70 MB at 1 MB/s is 70 s,
  not 70 min), so no 0.1.3 bundle was published. The test is corrected and 0.1.4 ships everything listed under 0.1.3.

## [0.1.3] - 2026-11-03

### Download time left is measured, never assumed
- The console no longer prints "about N hours at 50 Mbit/s" before the downloads; its progress lines now carry
  the measured file speed and the whole download's time left, the same numbers the setup window shows.
- The overall time left divides the remaining bytes by the whole download's speed over the last 30 s
  (`SpeedMeter.total_rate`), which is steadier than adding up per-file averages; per-file averages stand in
  only while that window is still filling.
### First-run fixes from the partner PC (`feat/release-0.1.3-hotfix`)
- **The first setup window is the Nightshift window.** The bundle now carries the three WebView2 DLLs in `bin/webview2/`
  (`dev/make_bundle.ps1 -WebView2Dir`; the release workflow downloads the pinned nupkg, checks its SHA256 and each DLL
  against `installer/webview2_files.json`, and packs exactly those three files; nothing else is allowed under `bin/` in
  the zip). The window launcher looks in the install folder and in `<app>/bin/webview2`, and step s02 copies matching
  DLLs into the install folder instead of downloading the nupkg. Before, the DLLs arrived after the first window opened,
  so the first run showed an Edge app window with Edge's title bar and icon.
- **A current NVIDIA driver is no longer reported as too old.** Stage 1 finds `nvidia-smi.exe` in the DCH driver store
  (`System32/DriverStore/FileRepository/nv*`) too, reads the CUDA version from the banner, then from the driver number
  (one table, 580 = 13.0, 570 = 12.8, 560 = 12.6, 550 = 12.4, 545 = 12.3), then from Windows' own display adapter record.
  The driver row is labelled "NVIDIA driver (CUDA 12.4 or newer; 12.8+ for RTX 50-series)". `NS-E103` (fail) only when a
  version was read and is below 12.4. A version that could not be read at all is the new warning `NS-W115` and setup
  uses the CUDA 12.4 build. A driver between 12.4 and 12.8 warns, without a code, only for RTX 50-series (Blackwell) cards.
- **Browse buttons for the install folder and the models folder.** `POST /api/pick-folder` shows the Windows folder
  dialog (`installer/pick-folder.ps1`, in front of the setup window) and returns the path with its disk check. The install
  folder is validated when typed, on blur and on pick: absolute path on a local fixed drive (no network paths, no
  removable drives), at most 120 characters (was 80), no characters Windows forbids, spaces and non-ASCII letters
  allowed. A picked drive root or a folder that already holds files gets a `Nightshift` folder inside it. The folder is
  created when Install starts, not before.

## [0.1.2] - 2026-11-03

### Hotfix: the setup window never started on a stock PC
- `INSTALL.cmd` stopped with "[FAIL] Setup window: the setup program did not start" (exit code 14) on the first
  real install, although the setup server was up and had opened Edge. Stage 1's health ping passed the session
  cookie through `-Headers`, and Windows PowerShell 5.1 silently drops that restricted header, so the server
  answered 401 and the console killed it. `Invoke-ServerPing` now sends the cookie through a `WebRequestSession`
  and also as an `X-NS-Token` header, which the server accepts alongside the cookie.
- New CI test runs the real `Invoke-ServerPing` under `powershell.exe` (5.1) against a live setup server, and pins
  that the old form fails there, so the gap that let v0.1.1 ship cannot reopen.

### Release asset names
- Clearer release assets from the next tag on: `Nightshift-Setup-<ver>.zip` (the installer bundle, was `Nightshift-<ver>.zip`),
  `Nightshift-Update-<ver>.zip` (update-from-file package, was `nightshift-app-<ver>.zip`) and
  `Nightshift-VoiceEngine-win-cuda-x64.zip` (was `qwentts-win-cuda-x64.zip`), each with a `.sha256`.
- `ns update --github` and the dashboard updater accept both the new and the old update-package name (new first), so
  v0.1.0 and v0.1.1 releases keep working. `manifest.json` still pins the v0.1.0 voice-engine asset by its old name.
- The release body now starts with a "Which file do I download?" table (`dev/release_notes.md`) followed by the changelog entries.

## [0.1.1] - 2026-11-03

Second code-complete release. It carries the clean-clone review fixes, the re-review rounds (pull requests #48 to #51),
the pre-tag polish (this release's last pull request) and the `bins.qwentts` pin to the v0.1.0 release asset.

### Pre-tag polish (`feat/release-0.1.1-pretag`; `docs/design/system-gaps.md` and the final engine and dashboard checks)
- **Voice stage timeout** (gap 1): the studio's wait for a whole job's narration is 120 s + 3 s per line, at most 2 h
  (`ns_common.tts_timeout`, one formula for `ns_studio/media/voice.py`, `TtsClient` and the TTS service, which never stops a
  `qwen-tts` process before the client gives up). When the client times out it asks `POST /v1/audio/speech/cancel` to kill
  the process, so the GPU is free before the retry; whole items the service had finished stay on disk and the retry
  (`resume`) skips them. Was a fixed 30 minutes against a service that kept the GPU busy for up to 3.6 hours.
- **Queue, Release the graphics card** (gap 2): the supervisor refuses while the holder sent a heartbeat in the last 90 s
  (a nightly run that overran shows nothing in the Queue); otherwise a Nightshift holder is stopped through the jobs worker,
  and for a foreign holder such as `opencode` it only releases and says the other program may still be using the GPU. The
  confirm text says so.
- **Nightly task that runs only while you are signed in** (gap 3, NS-W205): `Status.schedule_mode` (`registered`,
  `interactive_only`, `none`; contract change `CR-PRETAG-C9-1`). The top bar and Settings say "Runs at 03:00 only while you
  are signed in" in a warning style; the next-run countdown stays.
- **Setup voice** (gaps 4, 5, 6): it is made under the job's own GPU owner, so Cancel frees it at once; Library, Voices,
  Create it now is a job (`apply_voice`) with progress and Cancel in the card and in the Queue; the setup window no longer
  promises an automatic voice design download (Settings, Models, Get voice design model).
- **Series parts stay produced**: a finished `series_part` job moves its part to `produced`, and the start sweep resets a
  part to `approved` only when its job is missing, failed or cancelled (it also settles parts 0.1.0 left on `producing`).
  Before, reopening the dashboard offered Part 1 again and made the video twice.
- **Update page**: the 10-minute "did not come back" wait starts when `update_state.json` leaves `running`, not at the
  click; during a long install the page keeps saying it is installing.
- **Uninstall removes `<home>\run`** (the update worker's private venv copy, 150-300 MB) and lists it in its report; the
  update worker deletes its own copy after a relaunch that worked.
- **CI**: the test job may take 40 minutes (`ci.yml`, `release.yml`); the duplicate 70 s child smoke run is gone.

### Fixed (re-review round, pull requests #48 to #51)
- **Loose ends (#48)**: the embed service gets `-c/-b/-ub` from `EmbedCfg`; link-download Cancel reaches the Clipper
  `CancelToken` (flag file, 3 s grace, then the process tree); the drift gate is described as it works (median offset
  between caption and ASR word starts, 120 ms); the setup window has an "Also download the voice design model" checkbox
  (a designed voice implies it; `NS-W114` when ticked off); `OPERATOR.md` explains the PyTorch fallback (`NS-W301`), Get
  voice design model and the DPAPI key file.
- **Runtime (#49)**: the update worker starts the engine (and the dashboard window, if one was open) again after the update
  or a rollback and writes `done` or `failed` only after `/status` answers; the rollback retries its renames and waits for
  the verify engine; browser presence uses `psutil.net_connections`; a uv shim is replaced by the real exe; Get voice design
  follows `runtime.tts.engine` (PyTorch gets the Hugging Face weights); `update_state.json` is written atomically and a
  dead worker is shown as failed; the deferred uninstall waits for the dashboard window and lists leftovers in
  `%TEMP%\Nightshift-uninstall-leftovers.txt`; `schedule.json` is not written when Windows refused the task (NS-W206); the
  worker runs from a private copy of the supervisor venv so uv can rewrite `venvs\supervisor`.
- **Dashboard (#50)**: Cancel releases the GPU lease (flag file, 10 s grace, kill, `POST /release_owner`); `studio:` leases
  are renewed by a 30 s heartbeat and a silent lease is reclaimed after 10 minutes; the Queue shows "GPU held by ..." with a
  Release button; Settings follows the update across the restart and reloads on the new version; saved Assistant requests
  are claimed in one statement and answered when a job ends; series parts go back to `approved` when cancelled or failed;
  caption look "Automatic"; failed jobs keep their stage and studio job ids; stock-footage keys also go to the DPAPI file;
  Library, Voices, "Pending voice from setup" with Create it now; a real `studio_job` child process is run in CI.
- **Studio and TTS (#51)**: embed windows are token-aware per script; CJK captions are joined without spaces and aligned per
  character; channel languages reach `qwen-tts --lang`; series parts keep their pacing; publish slots use the PC's local date
  and count Shorts already scheduled; master and Short budgets count the pauses; the regenerate-once table has the master
  names; **one `qwen-tts --stream-by-line -o -` process per batch** instead of one per sentence; pending voices are created
  by every job (`apply_pending_voices`); `NS-E311` for an unknown voice; the clip Outbox with nothing in it is `NS-E404`;
  `load_keys` falls back to the DPAPI file on any keyring error.

### Fixed (studio, from the clean-clone review `D:\NightShift-refs\review-studio.md` and `review-clipper-tts-common.md`)
- **First run**: a channel that exists only as YAML (what setup writes) gets its `channels` row before the first insert
  (`make_job`, `load_job`, clips, ledger); the policy gate and every story embed use a 1,500-character opening window and
  `FakeEmbed` refuses over 512 tokens like `llama-server`; caption drift is the offset between caption and ASR word starts
  (speech rhythm no longer fails NS-E403); the stock-footage keys are read under the names setup and Settings store
  (`ns_common.secrets_names`).
- **Wrong output**: `pacing` sets the TTS speed and Short word bands use the QC +-15 % (masters are capped at 14 minutes);
  each Short is written, titled, captioned and recorded in its own combo; publish slotting follows
  `outbox/manifest.json` and rewrites both sheets; `studio calibrate` takes the embed lease and only advises two-model
  mode when a story model exists; `tag_relevance` really asks the model (policy_check v2).
- **Degrades**: uncoded exceptions become `NS-E327` for their job only; whisper is unloaded when its tenant is released;
  a regenerated job changes seed, temperature and clips; `--resume` no longer redoes LLM stages after the voice stage or a
  chapter expansion; prompt versions are saved per job; words, sentences and captions work in any script;
  apostrophes in FFmpeg paths are escaped, not deleted; Outbox paths stay under 200 characters; the visual_plan prompt asks
  for caption emphasis; master chapters get the caption contrast fallback.
- **Series**: parts use their design (voice, caption style, visual treatment, title, "Part N of M", end card, publish delay)
  and master QC allows 1.15x the part's minutes.
- **Clipper**: clip SEO uses clip rules (not the fiction lists) and a failing clip is skipped, not the job; phone videos
  are probed with their display rotation; one whisper pass; `import_url` can be cancelled (`CancelToken`) and kills yt-dlp;
  rights not ticked is `NS-E410`; the caption style picked on Create is honoured; `captions.remove_fillers` channel setting.
- **New**: your own `exemplars\` and `prompt_overrides\` are read; upload sheets name the time zone; the creator note closes
  every description.

### Fixed (installer and runtime, clean-clone review)
One commit per row of `D:\NightShift-refs\review-installer-runtime.md` (see the pull request), each with a test that fails
without it.
- **Setup could not find uv** (row 1): stage 1 hands the uv it downloaded over in `NIGHTSHIFT_BOOTSTRAP_UV`; the one
  resolver (`ns_common.uv.find_uv`) checks the install folder, that hand-over, the bootstrap folder, then PATH; b02 copies
  uv and verifies the copy; b03 runs the copy.
- **Non-ASCII user or Desktop path** (row 2): PowerShell values are read as base64 UTF-8, so the Desktop path and
  NIGHTSHIFT_HOME survive any console code page.
- **Update swap** (rows 3, 4): shortcuts start in the install home, the Update/Dashboard/Studio launchers also change the
  Win32 working directory, the swap retries a busy rename; `ns update` runs as a detached worker outside the engine's Job
  Object (`CREATE_BREAKAWAY_FROM_JOB`, WMI fallback) and writes `run/update_state.json`.
- **Idle exit with the Dashboard open** (row 5): an open window host or a browser connected to the Dashboard keeps the engine.
- **Update re-sync** (row 6, gap 18): setup and `ns update` read one table of extras (`ns_common.venv_extras`).
- **Uninstall** (row 7): the last deletion (`bin\webview2`) runs from a detached PowerShell after the window exits, and the
  result lists what really went.
- **Setup window** (rows 8, 12, P4): the host starts only when its DLLs exist, has 20 s to show a window, else Edge or
  Chrome opens with `--no-first-run --no-default-browser-check`; Stop and a dying host are explained on the console.
- **install_state.json** (row 9): atomic write, `.bak` copy, tolerant read.
- **Open dashboard** (row 10): the launcher runs with its output captured and its error reaches the window.
- **Stage 1 without winget** (row 11): NS-E105/NS-E110 instead of a raw PowerShell error.
- **Nightly task** (P1, P2, P3): S4U first, interactive logon when Windows refuses (NS-W205), never blocks setup
  (NS-W206); API keys also in a DPAPI file the nightly run can read; a message without a desktop becomes a morning note.
- **Gaps 10, 14, 19, 27, 28, 37, 38, 39**: `ns models get voicedesign|story_llm`, `ns update --git --channel`, launchers use
  `/health`, whisper `int8`, `ns gate` removed, NS-W301 in `ns verify`, the `local/code` check runs after s07.

Fixes from the v0.1.0 code review (installer, launchers, supervisor), found by reading the paths no test had run.

### Fixed (dashboard side of `docs/design/system-gaps.md`)
Calls from the dashboard into the studio, supervisor and Series Workshop that pointed at functions which do not exist,
and routes and buttons that nothing used. A table-driven test (`tests/test_dashboard_wiring.py`) now resolves every
`ns_studio`, `ns_supervisor`, `ns_tts` and `ns_common` name the dashboard takes, so an unwired call fails CI.
- **Run now** (gap 2) calls `ns_studio.jobs` and `policy.regen.run_job_guarded`; progress and Cancel come from the stage
  runner's log events (`ns_dashboard/studio_bridge.py`).
- **Clipper** (gap 3) builds the clip job with `create_clip_job` and runs it with `run_job`; links download through
  `clipper.source.import_url`. **Re-run with another template** (gap 18, `POST /api/jobs/clip/rerun`) resumes the job
  with `resume_clip_job`, keeping the transcript.
- **Series parts** (gap 6): the jobs worker runs `series_part` rows that `Workshop.produce` writes; a failed part goes back
  to approved. The **Series Workshop tools** (gap 7) are in the Assistant (topic series) with Yes/No cards, and
  **Library, Series** (gap 8) lists stories and parts from the workshop files with a "Produce next part" button.
- **Settings, Update** (gap 9) shows "not available in this version yet" instead of an error while `ns update --check` /
  `--github` do not exist. **Diagnostics** (gap 10) calls `doctor.build_bundle` and has a Download button (gap 22).
  **Self-check** (gaps 11, 27) calls `run_verify(paths, rt, ctl, quick=, deps=)`; Settings also has Warm up and Stop all.
  **Results, import CSV** (gap 12) reads the file with `learn.importer` and updates what the pools learned.
  **Run time** (gap 13) re-registers the Windows task with `schedule.install`.
- **Settings** has Export settings, Import settings (gap 17, `ns backup` / `ns restore`) and the real path of
  `UNINSTALL.cmd`; the log viewer filters by component (gap 28).
- **Assistant** (gap 21) lists past conversations and answers requests saved while the engine was busy. **Library,
  Formats** (gap 23) has an edit form (and Duplicate works again). **Queue** (gap 26) shows a "Last night" card.
- **First start** (gap 30) greets the owner by the name chosen in setup and starts in the chosen theme.

### Fixed (dashboard, from the clean-clone review)
- **Jobs run in the studio venv.** The dashboard service lives in the supervisor venv, which has no faster-whisper, GPU DLL
  wheels, spaCy or OpenCV, so Run now, Clipper jobs, series parts and Library, Voices, Clone failed at the first whisper or
  spaCy use. Each of them now starts `<home>\venvs\studio\Scripts\python.exe -m ns_dashboard.studio_job` (cwd = the app
  folder, `NIGHTSHIFT_HOME`, UTF-8; `sys.executable` when that venv does not exist) and reads its progress as JSON lines
  (`ns_dashboard/studio_runner.py`). Cancel kills the process tree with `ns_supervisor.procs.kill_tree`: a link download
  (yt-dlp) no longer outlives a cancelled job and its half-file folder is deleted.
- **API and UI agree.** `tests/test_api_ui_contract.py` seeds a dashboard, calls every route the UI calls and compares the
  JSON field for field with `ui/src/lib/types.ts`. That found and fixed: Library > Stories (white screen: `words`,
  `critic_avg`), Ideas (Approve/Drop/Restore sent `/ideas/undefined`), Footage cards, the Queue's Approved and posted tab
  (`status=approved,uploaded` was a 422; rejected videos had no reason, posted ones no link or path), the Channels editor
  (it sent the card, so every save was a 422; it now loads and saves the whole profile), Settings > Models
  (`[object Object]`; engine settings are flat dotted keys now), Results after a reload, formats' channels, job step/steps.
- **Every page has an error boundary**; a crash resets the remembered Library tab. Demo data appears only when the very
  first status call fails; later the page shows "Engine not answering" and retries with backoff. The 2 s heartbeat no
  longer re-renders every page. The Queue's job bars follow the running job.
- **Settings > Updates > Install** follows the detached updater through `run/update_state.json` across the dashboard's own
  restart. **Import settings** closes the database for the restore and restarts the worker (refused while a job runs).
- **Saved Assistant requests are answered** when a job ends and show in the open chat. Cancel at job start no longer
  loses the race. An interrupted series part goes back to approved. Clip length limits equal `ClipParams`. The Caption
  look picked in Create reaches the clip job. Big uploads use the chunked `/api/uploads` and spool under `<home>/tmp`.
- Also: failed jobs show a red card with code, hint and a Diagnostics button; morning notes (a toast that could not show) in
  the bell and Last night; the three thumbnails of a long video; footage delete; Make a Short / Reuse as series on a
  story; clip results say which layout was used; the run time is the registered task's; `#/design` pages are dev-only;
  Settings > Models can fetch the voice design and story models (`ns models get`).


### Fixed (voice engine, `feat/release-review-tts`: review rows 1-4, 12, 13)
The four stacked blockers that left the partner PC with no working voice (qwentts failed, the PyTorch fallback failed
too, install stopped at `NS-E108`), plus pitch and error codes. Contract and exact command lines: top of `ns_tts/engine.py`.
- **qwentts runs the programs the release zip contains.** `qwentts.exe` does not exist; the engine now starts
  `bin\qwentts\qwen-tts.exe` (text on stdin, `--model --codec --speaker|--ref-spk --ref-rvq --ref-text --lang --seed -o`,
  lowercase speaker names) and `qwen-codec.exe` (`--model --talker -i`) for new voices, using the flags of the pinned
  qwentts.cpp `--help`. The old guessed flags (`--text`, `--out`, `--tokenizer`, `--language`, `--ref-audio`,
  `--save-speaker-embedding`, `--batch-file`) are gone. `tts-server.exe` is shipped but not used (one talker per process).
- **Voice format.** qwentts voices are `speaker.spk` + `speaker.rvq` + `ref.txt` in the voice folder; `voice.json` gains
  `files` (additive, the dashboard still reads `name` and `mode`). Voices with only `speaker.bin` (PyTorch engine, older
  fakes) still load. `GET /v1/voices/{name}/embedding` returns a zip when a voice has several files.
- **PyTorch fallback.** The supervisor starts the voice service with the `tts` venv's Python when `runtime.tts.engine` is
  `reference` (torch lives only there), and the engine reads the models where the installer puts them: `<model_dir>\hf`
  (`ns_common.paths.TTS_HF_SUBDIR`, `Paths.models_tts_hf`).
- **Speed no longer changes pitch.** Narration speed (0.7 to 1.3) is an FFmpeg `atempo` chain per chunk instead of
  resampling; neither qwentts tool has a speed control.
- **Error codes.** Synthesis and TTS HTTP failures are `NS-E310`, a stored voice that is missing files or belongs to the
  other engine is `NS-E311`, a missing VoiceDesign model is the new `NS-E314` ("run Settings > Models > Get voice design"), a voice that
  cannot be built is the new `NS-E315`, and embed-service failures are the new `NS-E316`. `NS-E301` stays the language model's code; `NS-E202` is used only while the voice service starts.
- **`/health` of the voice service** now reports `engine_kind` (`qwentts`, `reference` or `fake`) so `ns verify` can warn
  `NS-W301` when the PyTorch fallback is in use.
### Removed
- `GET /api/templates/{id}/preview.png` and the Pillow preview drawing: nothing called them (gap 28).
- `Paths.outbox_dir`, the old date/job Outbox layout, which no code used (gap 24).

### Fixed
- **`ns update` could never swap folders** (`NS-E702` every time): the shortcuts start `ns.exe` with the app folder as
  working directory, and Windows refuses to rename a folder that is any process's working directory. `ns update` (and
  `--rollback`) now steps out of the app folder first, and `Nightshift - Update` does a `Set-Location` before calling it.
- **uv was only looked up on PATH**, but the installer keeps it at `bin\uv\uv.exe` and never adds it to PATH, so
  `ns update` could not re-sync the environments and `ns doctor --bundle` had no `pip freeze`. One resolver
  (`ns_common.uv.find_uv`: install folder first, then PATH) now serves update, doctor, the installer and the setup window.
- **The dashboard could open as a dead page after setup**: `ns verify --quick` left a headless engine running that never
  starts the dashboard service. The Dashboard shortcut (and the setup window's Open dashboard, which runs it) now always
  asks the engine to start the dashboard, waits for port 8100 and never opens a dead address. `ns verify` stops an engine
  it started itself, and the setup summary runs `ns stop`.
- **The smoke test checked check names that do not exist** (`llm`, `mtp`; `ns verify` reports `llama.start`,
  `llama.health`, `llama.json_completion` and has no MTP check) and the fake runner faked the same wrong names, so a
  failing story model was never caught. The names now live in `ns_supervisor.check_names` and are used by `ns verify`, the
  smoke step, the fake and the setup window labels. The dead MTP branch is gone (`ns tune` decides MTP). Setup rows group
  the real checks per area.
- **Paths with spaces broke headless setup** (`C:\Users\John Doe\...`): stage 1 wrapped the server arguments in literal
  quotes and splatted them, which Windows PowerShell 5.1 quotes again. It now passes plain strings (and quotes correctly
  for the window-mode `Start-Process`).
- **Shutdown race in the engine**: a GPU waiter granted the lock while `shutdown()` ran started a new model process after
  the stop and re-created `run\supervisor.json`. The grant is re-checked (`NS-E202`), `warmup` refuses during shutdown and
  `write_state()` does nothing once shutting down.
- **`terminate()` killed services at once**: it ran `taskkill /T /F` first, so `term_grace_s` was never used. It now asks
  politely (`taskkill /T`), waits the grace period when Windows accepted the request, and only then force-kills the tree.
- **A hidden console stayed hidden**: the dashboard launcher returned on any host exit code other than 3 before showing
  the console it had hidden. It now restores it in a `finally` and falls back to the browser window on any exit but 0.
- **The setup and dashboard windows shared one single-instance lock**, so opening the dashboard while setup was open only
  raised the setup window. `nightshift-host.ps1 -Role setup|dashboard` (default dashboard) scopes the mutex and
  `run\apphost-<role>.json`; the setup window passes `-Role setup`.
- **Toasts never showed**: nothing registered the AppUserModelID `Nightshift`. Setup now writes
  `HKCU\Software\Classes\AppUserModelId\Nightshift` (DisplayName, IconUri = the brand icon) and stamps the ID on the
  Desktop dashboard shortcut through IPropertyStore (best effort; the registry key alone is what makes toasts show).
  `UNINSTALL.cmd` removes the key.

### Gaps closed (engine side, `docs/design/system-gaps.md`)
- **The nightly run now starts the services it needs** (gap 1): `studio run` (and `--daily`) used `DevAcquirer`, which
  only hands out port numbers, so at 03:00 the first LLM call hit a closed port. `StudioAcquirer` asks the engine
  (`ns_supervisor`) for llama, embed, tts and whisper, starting the engine if it is down, and waits up to 2 hours for a
  busy GPU (J7). After that the run stops with `NS-E201`, lists the remaining jobs as skipped and tries again the next
  night. Fakes keep `DevAcquirer`.
- **`embed` is started before idea dedupe and the text policy check** (gap 4): stages that embed text declare
  `uses_embed` and hold the service while they run; a test fails when a new stage calls `ctx.services.embed` without it.
- **A designed or cloned voice chosen in setup is created** (gap 5): `studio voice design|clone|apply-pending` use the
  same service calls as the Library. Setup queues `config\pending_voice.json` with a clear message (the TTS engine is
  only settled by the smoke test after it, and the other engine refuses the voice, `NS-E311`); the first real
  `studio run` creates it.
- **End-of-run toast** (gap 14): "1 master + 5 Shorts ready for review" / "run failed (NS-E203), open Dashboard" from the
  nightly path, honouring Settings > Notifications (`ns_common.config.notifications_enabled`).
- **Cliffhanger pauses** (gap 15): chapters send their cliff line to TTS, so the pause before it is 1,200 ms.
- **`ns update --check` and `--github`** (gaps 9 and 16): read the latest GitHub release, download
  `nightshift-app-<version>.zip` and verify its SHA256 (GitHub's digest, else the attached `.sha256`; no checksum, no
  install) before the normal verified update with rollback. The engine runs the check once a week and shows a
  notification when a newer release exists.
- **The engine leaves after the nightly run** (gap 25): the run tells it (`POST /run_finished`) and it exits 60 s later
  unless the dashboard is running or other work started, instead of staying up 30 idle minutes.
- **Error codes**: a whisper failure is `NS-E408` (hint: the audio track) and a stored voice from the other TTS engine is
  `NS-E311` (hint: re-create it), also through `TtsClient`.
- **`series_compilation` is hidden from Run now** (gap 29) in the API and the Assistant until it has stages.
- **Whisper code default is `distil-large-v3` / `int8`** (gap 31), like setup and the spec; the installer still writes
  `float16` explicitly.
- `tests/test_wiring.py` imports every `ns_*` module and checks that every dotted target named in
  `ns_dashboard/adapters.py` and `ns_dashboard/assistant/tools.py` exists.

## [0.1.0] - 2026-11-02

First release (milestone M0, code complete). Highlights since the pipeline landed:

- **Dashboard UI rebuilt** on the ref12 card language: side tab rail, marbled canvas, three themes (light, dark, soft),
  top bar with the inline Assistant, redesigned pages, GPU and VRAM meters, brand assets and a new app icon.
- **Host window:** the dashboard opens in a frameless WebView2 window (single instance, remembered size), with the
  Edge/Chrome app window as the fallback.
- **Voice tools:** design a voice from a description and clone one from a recording (with a consent check, `NS-E313`),
  both previewed in the Library.
- **Scheduling:** Create can hold a story or clip job until a chosen time (`schedule_at`).
- **QC pass rate:** each channel shows how many videos passed QC against a target line (`Channel.qc_pass_target`).
- **Installer web wizard:** `INSTALL.cmd` opens a setup window (stage 1 splash with `-Ui`, `-NoUi` and `-ProbeOnly`; a
  stdlib web server and state machine with resume). Screens: Welcome, This PC, Choices, Download, Installing, Verify, Done,
  Failed and Uninstall; `UNINSTALL.cmd -Ui` opens the Uninstall screen. The console wizard still runs with an
  `answers.json`. The window is built with `npm run build` in `ns_dashboard/ui` (`vite.installer.config.ts`) into the
  committed `installer/ui/static`.

### Added
- Foundation and contracts (C3): shared pydantic models in `ns_common.models` with generated JSON schemas,
  `contracts/errors.json`, `contracts/schema.sql`, config/paths/db/logging helpers, the `contracts-m0` and
  `contracts-m1` freeze tags.
- Studio core (C7): resumable stage runner, orchestrator with the canonical `FORMAT_STAGES`, the `studio` CLI,
  variation pools, policy gates, learning from analytics, cadence and review.
- Text pipeline (C5): shorts-only and master-first story stages, 20 prompts, 12 exemplars.
- Media pipeline (C6): voice, captions, clips, render, QC and the content-organized Outbox; caption placement,
  emphasis, speaker colours and profanity masking; format presets as YAML in `ns_studio/presets/formats/`
  with a generic layout engine.
- TTS service (C4): qwentts.cpp wrapper with a PyTorch reference fallback and voice embeddings.
- Runtime (C2): supervisor with a Job Object, service lifecycle, the `ns` tools and the Windows launchers.
- Installer (C1): `INSTALL.cmd`, `UNINSTALL.cmd`, stage 1, the stage 2 wizard with resumable verified
  downloads, `OPERATOR.md`.
- Clipper (C8): analysis-first clip selection, model-chosen layouts, a per-clip judge, and clips from a link
  through a pinned yt-dlp (`NS-E409`).
- Dashboard (C9): local FastAPI backend (jobs worker, Library, update endpoints, Assistant with a tool
  registry, URL clip jobs) and the React + TypeScript front-end in `ns_dashboard/ui`, built to
  `ns_dashboard/static`.
- Series Workshop (C11) and the integration pass (CLI mounts for `studio clip|series|ask`, idea bank feeding
  the `ideas` stage).
- CI split into parallel jobs: `lint`, `types`, `test`, `ui-build`, `powershell-parse`, `bundle` and (pull
  requests only) `scope-check`, with a per-branch concurrency group and pip caching. `ui-build` rebuilds the
  dashboard with node 20 and fails when the committed `ns_dashboard/static` differs.
- Release workflow: a `v*` tag runs the tests, builds the UI, builds the bundle and publishes a GitHub Release
  with `Nightshift-<ver>.zip`, its `.sha256` and the `nightshift-app-<ver>.zip` update alias.
- `dev/make_bundle.ps1` (git archive of HEAD with the dashboard build, content verification, `-DryRun`).
- `dev/check_scope.py`: pull-request guard that keeps each `feat/c<K>-*` branch inside its component paths.
- `tests/test_hygiene.py`: lazy heavy imports, no `shell=True`, no `print` outside CLI modules, PowerShell
  prelude, error-code registry and schema drift checks.
- `build-qwentts` workflow compiling qwentts.cpp for Windows x64 with CUDA.
- `VERSION` file, checked against `ns_common.__version__` and `pyproject.toml`.
- `docs/REPO_SETTINGS.md`: branch protection, repository settings and how the manifest binary pins are made.
- `manifest.json` pins `bins.yt_dlp`; installer step s02 downloads it to `bin\yt-dlp\yt-dlp.exe`.
- Contracts: `NS-E409` (download failed); `Channel.formats_allowed` and `Channel.captions.mask_profanity`;
  optional `emphasis` on `VisualBeat` and `ClipPlanItem`; the Assistant tables (`conversations`, `messages`,
  `actions`, `queued_messages`) in `contracts/schema.sql`.
- Packaging: `ns_dashboard` is part of the package list and pyright scope; package-data ships
  `ns_dashboard/static`, `ns_dashboard/prompts` and `ns_studio/presets/formats`; a `dashboard` extra
  (`python-multipart`, `Pillow`, `keyring`) is installed into the supervisor venv; `opencv-python-headless`
  joins the `studio` extra.
- UI contracts (C3): installer answers `owner_name`, `ui_theme`, `models_source_kind`; codes `NS-E113`, `NS-W403`, `NS-E312`;
  the `TBD-release` rule for optional manifest pins; `Status` (with optional `gpu`), `JobRunRequest` (`schedule_at`),
  `ChannelStats` (`qc_pass_rate`) and the voice design/clone models (CR-C1-UI-1..3, CR-C3-UI-4..7).
- `launchers/nightshift-host.ps1`: single-instance frameless WPF window around WebView2 (title bar protocol, size and
  position kept in `config/window.json`); `nightshift-dashboard.ps1` tries it first and falls back to the
  Edge/Chrome app window, then the default browser. `bins.webview2` (change request CR-C2-host) pins the Microsoft.Web.WebView2 NuGet package;
  the installer unpacks only its three DLLs to `bin/webview2`.

- Release 0.1.0 prep: `NS-E313` (voice cloning without consent; `POST /api/voices/clone` now answers it instead of
  `consent_required`), optional `Channel.qc_pass_target` (default 0.9, read by the channel stats), `JobClipRequest` for
  `POST /api/jobs/clip` (with `schedule_at`) and its schema.
- Voice-sample uploads accept `.wav`, `.mp3`, `.m4a`, `.flac`, `.ogg` and `.webm`; the dashboard no longer renames
  recordings to `recording.webm`.
- The brand icon (`ns_dashboard/static/brand/nightshift.ico`) is the host window icon and the icon of the shortcuts the
  installer creates; `dev/make_bundle.ps1` requires it in the bundle.

### Changed
- ruff bans `import subprocess` outside `ns_common/ffmpeg.py`, `ns_supervisor/`, `installer/`, `tools/`,
  `dev/` and `tests/` (rule `TID251`), with per-file exceptions for `ns_dashboard/updater.py`, `ns_tts/engine.py`
  and `ns_studio/clipper/source.py`.
- `dev` extra gains `fastapi`, `psutil`, `python-multipart`, `opencv-python-headless`, `Pillow`, `imagehash`.
- `nightshift-dashboard.ps1` opens the dashboard as an Edge or Chrome app window (`--app=`), falling back to the
  default browser.
- The Assistant store no longer defines its own DDL; `Db.migrate()` creates its tables from the shared schema.
