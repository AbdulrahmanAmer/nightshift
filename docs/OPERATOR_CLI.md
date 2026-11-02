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
| `studio run` | Create and run jobs through the stage pipeline. Options: `--daily` (cadence-planned nightly run), `--channel SLUG`, `--count N`, `--format master_first\|shorts_only\|series_compilation`, `--dry-run` (print the stage plan), `--resume JOB_ID`, `--policy/--no-policy`, `--fake-services`. |
| `studio channel add` | Add a channel (`--slug --name --niche ...` or `--from-yaml FILE`); pool minimums and distinctness are enforced. |
| `studio channel list` | List configured channels with status, format and pool size. |
| `studio channel edit SLUG --set key=value` | Change channel fields; the result is re-validated before it is saved. |
| `studio channel validate [SLUG]` | Check pool minimums and cross-channel distinctness. |
| `studio voice list` | List the voices known to the TTS service. |
| `studio voice preview NAME` | Synthesize a short sample (`--text`, `--out`, `--speed`) into a WAV file. |
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

## Dashboard actions without a command

Some actions exist in the Dashboard and for the Assistant (`studio ask`) but have no `studio` or `ns` command:

| Action | Where | Notes |
|---|---|---|
| Design a voice from a description | Library -> Voices -> Design, `POST /api/voices/design`, Assistant tool `design_voice` | Needs the VoiceDesign model; stores the voice in `models\tts\voices\<name>\` and writes `preview.wav`. `studio voice list` shows it afterwards. |
| Clone a voice from a recording | Library -> Voices -> Clone, `POST /api/voices/clone`, Assistant tool `clone_voice` | Needs `consent: true` (the Yes on the Assistant's card counts). A 3-30 s clean single-speaker recording; otherwise `NS-E312`. |
| Start a job at a chosen time | Create -> When, `POST /api/jobs/run` and `POST /api/jobs/clip` with `schedule_at`, Assistant tool `start_story_job` | `null` or a past time starts now; a time without a zone is PC local time; at most 31 days ahead. The job stays `queued` with its `schedule_at` until due. `ns schedule` (the nightly task) is unchanged. |

## ns

| Command | What it does |
|---|---|
| `ns` | Print the version. |
| `ns stop` | Stop the engine and every model process (what "Nightshift - Stop All" runs). |
| `ns doctor` | Check the install; `--bundle` collects logs and reports into a diagnostics zip. |
| `ns verify` | Check every service and benchmark them; writes `logs\verify-report.json` (`--quick` for health, one completion, one voice). |
| `ns tune` | Find the fastest llama-server flags for this PC and save them to `runtime.json` (`--dry-run` prints the sweep matrix). |
| `ns gate MILESTONE` | Run `dev/gate_<milestone>.py` of the installed app (`m0`, `m1`, ...). |
| `ns update ZIP` | Apply a release zip (verified, with automatic rollback); `ns update --rollback` goes back one version. |
| `ns backup` | Back up settings, voices, the database and exemplars (`--out` sets the target zip). |
| `ns restore ZIP` | Restore a backup; what it overwrites is kept in `data\backup\pre-restore-*` (`--all` also restores `runtime.json`). |
| `ns schedule install` | Register the nightly task "Nightshift Daily" (`--time HH:MM`, default 03:00). |
| `ns schedule remove` | Unregister the nightly task. |
| `ns schedule show` | Show the task state and next run. |
| `ns notify TITLE MESSAGE` | Show a Windows toast (does nothing where toasts are unavailable). |
