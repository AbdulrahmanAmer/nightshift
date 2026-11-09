# Operator command line reference

Two commands exist: `studio` (content: channels, jobs, review, learning, clips, series) and `ns` (runtime:
engine, health, updates, backups). Both print a short help with `--help` on any level. This list is read from
the command definitions in `ns_studio/cli.py`, `ns_studio/clipper/cli.py`, `ns_studio/series/cli.py`,
`ns_dashboard/assistant/cli.py` and `ns_supervisor/cli.py`.

`--fake-services[=list]` runs a command against in-process fakes (no GPU, no network). A bare flag fakes
everything; `=llm,tts,whisper,embed,clips` picks some. In `studio series` and `studio ask` the flag is a plain
on/off switch.

## studio

| Command | What it does |
|---|---|
| `studio` | Print the version. |
| `studio run` | Create and run jobs through the stage pipeline. Options: `--daily` (cadence-planned nightly run), `--channel SLUG`, `--count N`, `--format master_first\|shorts_only\|series_compilation`, `--dry-run` (print the stage plan), `--resume JOB_ID`, `--script FILE` with `--title TEXT` (one video from a story you wrote, 200 to 30000 characters, narrated exactly as written; a Short must come to at most about 2:45, a long video to 9:00 to 14:30 at the channel's pace, otherwise it is refused before anything runs; `--count` must stay 1), `--policy/--no-policy`, `--fake-services`. |
| `studio channel add` | Add a channel (`--slug --name --niche ...` or `--from-yaml FILE`); pool minimums and distinctness are enforced. |
| `studio channel list` | List configured channels with status, format and pool size. |
| `studio channel edit SLUG --set key=value` | Change channel fields; the result is re-validated before it is saved. |
| `studio channel validate [SLUG]` | Check pool minimums and cross-channel distinctness. |
| `studio voice list` | List the voices known to the TTS service. |
| `studio voice preview NAME` | Synthesize a short sample (`--text`, `--out`, `--speed`) into a WAV file. |
| `studio voice design NAME --description "..."` | Design a voice from a description (`--language`); needs the VoiceDesign model. Stored in `models\tts\voices\NAME\`. |
| `studio voice clone NAME --audio WAV --text "..." --rights-confirmed` | Clone a voice from a 3-30 s recording and the words said in it; `NS-E313` without `--rights-confirmed`, `NS-E312` for a bad recording. |
| `studio voice apply-pending` | Create the voice chosen in setup (queued in `config\pending_voice.json`). A real `studio run` does this first by itself. |
| `studio review [--channel SLUG]` | List outputs waiting for review. |
| `studio review show OUTPUT_ID` | Show story text, policy notes and disclosure advice (`--play` opens the video). |
| `studio review approve OUTPUT_ID` | Approve a pending output (`--note`). |
| `studio review reject OUTPUT_ID` | Reject an output and move its files to `rejected/` (`--reason`). |
| `studio mark-uploaded JOB_ID ITEM URL` | Record the public URL of an approved output (ITEM is `long` or `s<k>`). |
| `studio pause SLUG` | Pause a channel after a YouTube warning, strike or monetization notice (`--reason`). |
| `studio resume SLUG` | Resume a paused channel after reviewing its last 10 outputs (`--yes` skips the prompt). |
| `studio status` | Per-channel status, performers, rejections and incidents (`--channel`, `--milestone` for the week-8 go/no-go). |
| `studio learn --import CSV` | Import a YouTube Studio analytics CSV, match rows to outputs and reweight the pools (`--date`, `--map field=column`). |
| `studio calibrate --channel SLUG` | Generate stories for human scoring (`--n`); with `--score FILE` correlate the scores and set `critic_bar`. |
| `studio clip VIDEO --channel SLUG` | Clip a video you own into vertical Shorts (`--template`, `--max-clips`, `--min-s`, `--max-s`, `--confirm-rights`, `--reselect JOB_ID`). |
| `studio ask "TEXT"` | Ask the Assistant, the same tool loop as the dashboard (`--yes` accepts confirm cards, `-c` continues a conversation). Needs the dashboard package and fastapi; otherwise it prints a message and exits 1. |

### studio series (Series Workshop)

| Command | What it does |
|---|---|
| `studio series start CHANNEL` | Create a story folder with a concept skeleton and print its slug (`--premise`). |
| `studio series concepts SLUG` | Write concept drafts `concept-A.md`, `concept-B.md`, ... (`--n`, 1 to 6). |
| `studio series regenerate SLUG LETTER` | Rewrite one concept draft with your notes (`--feedback`). |
| `studio series choose SLUG LETTER` | Promote a draft to `concept.md`. |
| `studio series map SLUG` | Write `series.md`, the part map (`--parts`, `--minutes`). |
| `studio series write SLUG N` | Write `script.md`, the Shorts files and `notes.md` of part N. |
| `studio series revise SLUG N` | Apply your notes to a part's script and nothing else (`--feedback`). |
| `studio series approve SLUG [PART\|all]` | Approve written parts for production. |
| `studio series design SLUG` | Save `design.yaml`; with no option it lists the allowed choices (`--format`, `--visual-treatment`, `--caption-style`, `--voice`, `--thumbnail-style`, `--cadence-days`). |
| `studio series produce SLUG [PART\|all]` | Queue one master-first job per approved part. |
| `studio series status SLUG` | Show the story status, parts and the suggested next step. |
| `studio series list` | List every workshop story. |

## Nightly run behaviour

`studio run --daily` (what the task "Nightshift Daily" starts) acquires its services through the engine
(`ns_supervisor`, port 8099) and starts the engine when it is down; so does any real `studio run` (fake
services keep the in-process fakes and never touch the engine). Stages that embed text hold the `embed` service while
they run; llama, tts and the speech recognizer are GPU tenants, one at a time. A busy GPU is waited for up to
2 hours, then the run stops with `NS-E201` and the remaining jobs of the night are listed as skipped in
`logs\nightly-<date>.json`. At the end it shows a toast (Settings -> Notifications) and tells the engine
(`POST /run_finished`), which exits 60 seconds later unless the dashboard is running or other work started.

## Dashboard actions without a command

Some actions exist in the Dashboard and for the Assistant (`studio ask`) but have no `studio` or `ns` command:

| Action | Where | Notes |
|---|---|---|
| Design a voice from a description | Library -> Voices -> Design, `POST /api/voices/design`, Assistant tool `design_voice`; also `studio voice design` | Needs the VoiceDesign model; stores the voice in `models\tts\voices\<name>\` and writes `preview.wav`. `studio voice list` shows it afterwards. |
| Clone a voice from a recording | Library -> Voices -> Clone, `POST /api/voices/clone`, Assistant tool `clone_voice`; also `studio voice clone` | Needs `consent: true` (the Yes on the Assistant's card counts). A 3-30 s clean single-speaker recording; otherwise `NS-E312`. Without consent the answer is `NS-E313`. |
| Start a job at a chosen time | Create -> When, `POST /api/jobs/run` and `POST /api/jobs/clip` with `schedule_at`, Assistant tool `start_story_job` | `null` or a past time starts now; a time without a zone is PC local time; at most 31 days ahead. The job stays `queued` with its `schedule_at` until due. `ns schedule` (the nightly task) is unchanged. |

## ns

| Command | What it does |
|---|---|
| `ns` | Print the version. |
| `ns stop` | Stop the engine and every model process (what "Nightshift - Stop All" runs). |
| `ns doctor` | Check the install; `--bundle` collects logs and reports into a diagnostics zip. |
| `ns verify` | Check every service and benchmark them; writes `logs\verify-report.json` (`--quick` for health, one completion, one voice). |
| `ns tune` | Find the fastest llama-server flags for this PC and save them to `runtime.json` (`--dry-run` prints the sweep matrix). |
| `ns update ZIP` | Apply a release zip (verified, with automatic rollback); `ns update --rollback` goes back one version and puts back the settings files the update copied (it names them). |
| `ns update --check` | Ask GitHub whether a newer release exists; prints a sentence and a JSON line (`update_available`, `current`, `latest`). Changes nothing. The engine does this once a week and shows a notification. |
| `ns update --github` | Download `Nightshift-Update-<version>.zip` (releases up to v0.1.1: `nightshift-app-<version>.zip`, still accepted) of the newest release, verify its SHA256 (GitHub's digest, else the attached `.sha256`; nothing is installed without one) and apply it like `ns update ZIP`. |
| `ns backup` | Back up settings, voices, the database and exemplars (`--out` sets the target zip). |
| `ns restore ZIP` | Restore a backup; what it overwrites is kept in `data\backup\pre-restore-*` (`--all` also restores `runtime.json`). |
| `ns schedule install` | Register the nightly task "Nightshift Daily" (`--time HH:MM`; default: the time the task already has, else 03:00). A task that already exists keeps its state: one you switched off in Task Scheduler is registered switched off again (`config\schedule.json` mode `switched_off`) unless you add `--enable`. `ns update` and the Settings run-time change do the same. The one exception is a task you registered with `--disabled` (mode `disabled`): `ns update` leaves it alone and off, while `ns schedule install` and the Settings run-time change register it enabled (add `--disabled` again to keep it off). |
| `ns schedule remove` | Unregister the nightly task. |
| `ns schedule show` | Show the task state and next run. |
| `ns notify TITLE MESSAGE` | Show a Windows toast (does nothing where toasts are unavailable). |
