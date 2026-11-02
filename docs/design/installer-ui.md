# Installer UI: design and build spec

Owner: C1 (installer). C9 owns the shared component additions in `ns_dashboard/ui`.
Status: design spec, ready to build. Nothing here has been run on the authoring laptop. Every check
runs in CI with fakes or on the partner PC.

The setup window is a small local web app. It uses the dashboard's design system (ref12 tokens and
components) and runs inside a frameless native window. `INSTALL.cmd` still starts everything, and the
console stays open as the log. The existing wizard steps (`installer/stage2/steps/s00`–`s10`) keep
doing the work. This spec gives them a face, and a way to answer and steer them, without forking the
install logic.

Read with: `BUILD_SPEC.md` §5, §13, §21, §22 (J1–J3), `OPERATOR.md`, `docs/design/ref12-map.md`.

---

## 0. Decisions

| # | Decision | Why |
|---|---|---|
| U1 | `stage1.ps1` becomes a splash. It probes the PC, gets uv and Python 3.12, starts the setup server and waits on it | Python is the first moment a web UI can exist. Everything after that point moves to Python, so each step has one implementation |
| U2 | The setup server is `installer/stage2/web/`, **standard library only** (`http.server.ThreadingHTTPServer`, `json`, `subprocess`, `threading`, `urllib`, `wave`, `zipfile`, `winreg`, `ctypes`). It runs on the bare uv-managed Python, before any venv exists | It has to start before `venvs\studio` exists, because the install folder is chosen in the UI and the venv lives in that folder |
| U3 | The server runs the three bootstrap steps that used to live in stage1 (tools, install folder, setup environment). It then starts the existing wizard as a **child process** in `venvs\studio` with `--ui-bridge stdio` | The wizard needs pydantic, httpx, keyring and `ns_common`. The server must not. A pipe between the two keeps both simple and testable |
| U4 | Wizard and server talk over the wizard's stdin/stdout as JSON lines (§1.7). In bridge mode its stdout carries nothing else | No second port, no polled files, and the server owns the child's lifetime |
| U5 | The UI reads state with `GET /api/state?since=<rev>` (long poll, 15 s) and writes with `POST` | Survives the window closing and reopening. A stdlib server needs no SSE or WebSocket code |
| U6 | The window is a frameless WPF window hosting WebView2 (`launchers/nightshift-host.ps1`, §6). Fallbacks: Edge or Chrome `--app=`, then the default browser. The console always prints the address | A native-looking window on the partner PC. Nothing breaks without WebView2 |
| U7 | `answers.json` next to `INSTALL.cmd` keeps today's behaviour: **headless, unattended, no window**. `INSTALL.cmd -Ui` opens the window with the answers prefilled instead | The brief says the unattended path keeps working without a UI, and CI depends on it |
| U8 | The UI is its own Vite project, `installer/ui/`, built into `installer/ui/static/` and committed, like `ns_dashboard/static/` | The bundle is a `git archive`, so the build has to be in the tree. The partner PC never runs npm |
| U9 | The installer defaults to the dark theme. The partner picks a theme in Choices, and it is saved for the dashboard | Brief |

---

## 1. Architecture

### 1.1 Processes

```
INSTALL.cmd (console window, title "Nightshift setup")
 └─ powershell stage1.ps1 ........ splash: probe, uv, Python, start server, wait for it
     └─ python -m installer.stage2.web ...... P1, setup server, bare Python 3.12, stdlib only
         ├─ powershell -STA nightshift-host.ps1   window (WebView2), or msedge --app=, or browser
         ├─ winget / uv / powershell (bootstrap steps b01–b03, re-probe, elevate)
         └─ venvs\studio\Scripts\python.exe -m installer.stage2.wizard --ui-bridge stdio   P2, engine
             └─ ns verify, ns tune, studio voice preview, ... (unchanged)
```

- P1 is a foreground child of the console. Closing the **console** ends P1 and P2. Everything is
  resumable through `install_state.json`, `.part` files and the verified markers, so running
  `INSTALL.cmd` again carries on.
- Closing the **window** stops nothing. Running `INSTALL.cmd` again while P1 is alive only reopens the
  window (§1.9).
- P2 lives only while install steps run. Pause keeps it alive. Stop ends it cleanly, and Resume starts
  a fresh P2 that picks up from `install_state.json`.

### 1.2 What stage1 prints before Python exists

There can be no UI until there is Python. Stage1 fills that gap with a splash of exactly this shape.
It uses ASCII only, because Windows PowerShell 5.1 consoles use the OEM code page:

```

      _.._
    .' .-'`     N I G H T S H I F T
   /  /         setup 0.3.0
   |  |
   \  '.___.;   This window is the setup log. Keep it open until setup says it is done.
    '._  _.'
       ``

  [ ok ] This PC        Windows 11 Pro, NVIDIA GeForce RTX 5060 Ti (16 GB), 32 GB memory
  [ ok ] uv             0.13.6, already here
  [ .. ] Python         3.12, downloading (about 30 MB)
  [    ] Setup window

```

The version comes from the `VERSION` file. Each of the four status lines updates in place with
`[Console]::SetCursorPosition`. When the console has no cursor control (redirected output), each update
is printed as a new line instead. Tags: `[    ]` waiting, `[ .. ]` working, `[ ok ]` done, `[warn]` done
with a warning, `[FAIL]` stopped. Colours: ok green, warn yellow, FAIL red, everything else default.

| Line | Text when done | Stops here when |
|---|---|---|
| This PC | `<OS caption>, <GPU name> (<VRAM> GB), <RAM> GB memory`, or `[warn]` with `<OS caption>, no NVIDIA graphics card found, <RAM> GB memory` | `NS-E101` (not 64-bit Windows): print `NS-E101  This PC cannot run Nightshift. Tell the developer.` and exit 10. Every other check result is shown in the UI |
| uv | `<version>, already here` or `<version>, downloaded` | uv cannot be downloaded or installed with winget: the code (`NS-E105` or `NS-E110`) and its OPERATOR.md "What to do" sentence, exit 13 |
| Python | `3.12, already here` or `3.12, downloaded` | `uv python install` fails: `NS-E105` and its sentence, exit 13 |
| Setup window | `opened`, `opened in Edge`, `opened in Chrome` or `opened in your browser`. If none worked: `[warn] open the address below in Edge or Chrome` | P1 does not answer `GET /api/ping` within 20 s: `[FAIL] Setup window: the setup program did not start`, then the last 20 lines of P1's stderr, then exit 14 |

Directly under the fourth line, always, whichever window opened:

```
         Setup address: http://127.0.0.1:53817/?t=3f9c2a7e
         If no window appeared, paste that address into Edge or Chrome.
```

After that the console prints one line per server log event as `HH:MM:SS  <text>`, with `warn  ` or
`error ` in front of warnings and errors:

```
21:04:12  Checks done: 8 ok, 2 warnings
21:05:40  Choices saved for After Dark Tales
21:05:41  Git: already here
21:06:58  FFmpeg: installed with winget
21:09:30  Qwen3-14B-Q4_K_M.gguf  2.1 of 9.0 GB  11.4 MB/s  10 min left
```

Progress lines are printed at most once every 10 s per file, so the log stays readable.

### 1.3 Stage1 changes (exact)

Keep: the parameters, constants, every probe function, `Get-PreflightRows`, `Get-FakeProbe`, `-DryRun`,
`-FakeProbe`, `-Answers`, the TLS line, and the manifest and errors.json loading.

Add:

| Switch | Behaviour |
|---|---|
| `-ProbeOnly -Out <file> [-HomePath <dir>]` | Runs the probe and `Get-PreflightRows`, writes the hand-over JSON (today's shape plus `is_admin`, `os_caption`, `os_version`) to `<file>` and exits 0. No splash, nothing installed. P1 calls this for "Check again" |
| `-Ui` | Opens the window even when `answers.json` exists. The answers prefill the Choices screen |
| `-NoUi` | Headless, even without `answers.json`. Same as `NS_NONINTERACTIVE=1` |

Change:

- `$script:NetworkHosts` adds `https://pypi.org` and `https://files.pythonhosted.org`, because the
  setup environment and the venvs install from PyPI.
- uv goes to `%LOCALAPPDATA%\Nightshift\bootstrap\uv\uv.exe` instead of `<home>\bin\uv\`, because the
  install folder is not known yet. `Find-Uv` looks in PATH, then in that folder, then in
  `<home>\bin\uv\` (for existing installs).
- Remove from the main flow: the `Show-Rows` fix-and-recheck loop, `Resolve-NsHome`'s console prompt,
  the `Install-WingetTool` calls, the home tree, the `NIGHTSHIFT_HOME` variable and the studio venv.
  They move to P1 as bootstrap steps b01–b03 (§1.6). `Get-PreflightRows` still runs, against the
  default home from `Select-DefaultHome` (or `home_path` from answers.json). The UI re-checks the disk
  row once a folder is chosen.
- Find Python with `uv python find 3.12`, then start P1:
  `<python> -X utf8 -m installer.stage2.web --app-path <App> --preflight <tmp json> [--answers <path>] [--headless]`,
  with `PYTHONPATH=<App>` and `<App>` as the working directory. Stage1 waits for P1 and returns its
  exit code. `INSTALL.cmd` keeps its `pause`.
- `-DryRun` prints `[dry-run] start setup server: <command line>` and exits 0 before starting P1, so
  the CI dry run keeps working. Update `tests/test_installer_stage1.py` for the moved steps.

Headless (U7): stage1 passes `--headless` when `answers.json` was found and `-Ui` was not given, when
`-NoUi` was given, or when `NS_NONINTERACTIVE=1`. P1 then runs the same state machine without HTTP or
a window. Blocking check rows print as today's `Show-Rows` table and exit 10. P2 runs without
`--ui-bridge`, so its rich console output and its retry-and-backoff behaviour are unchanged.

### 1.4 Server (P1)

`python -m installer.stage2.web`:

- Binds `ThreadingHTTPServer(("127.0.0.1", 0), Handler)` (a random free port) with
  `daemon_threads = True`.
- Generates a token with `secrets.token_urlsafe(16)` and writes
  `%LOCALAPPDATA%\Nightshift\setup-server.json`:
  `{"pid": 1234, "port": 53817, "token": "...", "started": "2026-11-02T21:04:01", "app_path": "D:\\NightShift"}`.
  The file is deleted on a clean exit. The address also goes to
  `%LOCALAPPDATA%\Nightshift\setup-address.txt`.
- Serves `installer/ui/static/` at `/` (`index.html`, `app.js`, `app.css`, `assets/*`), with
  `Cache-Control: no-store` on `index.html`.
- Protection:
  - Every request needs `Host: 127.0.0.1:<port>`, otherwise 421.
  - `GET /?t=<token>` with the right token sets `ns_setup=<token>; HttpOnly; SameSite=Strict; Path=/`
    and redirects to `/`.
  - Every `/api/*` request needs that cookie, otherwise 401.
  - `POST` bodies must be `application/json` and at most 64 KB.
  - No CORS headers.
  - Every response carries
    `Content-Security-Policy: default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'`.
- Logging goes to `<home>\logs\installer-ui-<date>.jsonl` once the home exists. Before that it goes to
  `%LOCALAPPDATA%\Nightshift\bootstrap\setup-ui.jsonl`, and b02 moves that file into the home.

Endpoints (all JSON unless noted):

| Method and path | Body or query | Returns |
|---|---|---|
| `GET /api/ping` | | `{ok: true, pid, version}` |
| `GET /api/state` | `?since=<rev>` (optional) | `SetupState`. With `since`, waits up to 15 s for `rev > since`, then returns the state either way |
| `POST /api/ui-step` | `{uiStep: "welcome" \| "checks" \| "choices"}` | `ActionResult`. Moves between the three screens before Start. The server holds the value, so a reopened window lands on the same screen |
| `POST /api/answers` | `ChoicesDraft` | `AnswersResult` |
| `POST /api/check-key` | `{service: "pexels" \| "pixabay", key}` | `KeyCheck`. A live request through `urllib`, using the same queries as `s01_questions.validate_*` |
| `GET /api/disk` | `?path=D:\Nightshift&tier=small[&source=1]` | `DiskCheck` |
| `POST /api/action/{name}` | `ActionBody` | `ActionResult` |
| `GET /api/file/{name}` | | File bytes. Allowlist: `voice-sample.wav`, `render-test.mp4`, `render-test.jpg`, `verify-report.json`. Anything else is 404 |
| `GET /api/uninstall` | | `UninstallView` (uninstall mode only, §2.10) |
| `POST /api/uninstall` | `{deleteModels: boolean, deleteData: boolean}` | `ActionResult` |
| `POST /api/internal/open-window` | header `X-NS-Token: <token>`, no cookie | `{ok}`. Used by a second `INSTALL.cmd` to reopen the window (§1.9) |

Action names: the six in the brief (`start`, `pause`, `resume`, `retry`, `cancel`, `open-dashboard`),
plus:

- `skip`: skip an optional step that failed
- `recheck`: run the probe again
- `elevate-longpaths`
- `diagnostics`: write the bundle now
- `reveal`: open Explorer at a path listed in the state
- `close-setup`: end P1 once nothing is running

The preconditions for each action are in §5.6.

### 1.5 State machine

```
            recheck
           ┌──────┐
           ▼      │
 ┌────────────┐ Continue ┌─────────┐ Install ┌─────────────┐     ┌────────────┐     ┌───────────┐     ┌──────┐
 │   checks   │─────────▶│ choices │────────▶│ downloading │────▶│ installing │────▶│ verifying │────▶│ done │
 └────────────┘ (no      └─────────┘ (start) └─────────────┘     └────────────┘     └───────────┘     └──────┘
        │        blocking row)                    │  ▲                │  ▲              │  ▲
        │ blocking row                            ▼  │ retry          ▼  │ retry        ▼  │ retry
        ▼                                      ┌────────┐          ┌────────┐        ┌────────┐
   stays in checks                             │ failed │          │ failed │        │ failed │
   (Continue disabled)                         └────────┘          └────────┘        └────────┘
```

`phase` is always derived, never stored on its own:

| Situation | Phase |
|---|---|
| Answers not accepted, `uiStep` is `welcome` or `checks` | `checks` |
| Answers not accepted, `uiStep` is `choices`; or answers accepted and `start` not pressed | `choices` |
| First unfinished step is `b01_tools`, `b02_home`, `b03_setup_env`, `s01_questions`, `s02_download_bins` or `s03_download_models` | `downloading` |
| First unfinished step is `s04_venvs`, `s05_opencode_prewarm`, `s06_tune` or `s07_configs` | `installing` |
| First unfinished step is `s08_smoke`, `v01_samples`, `s09_shortcuts_scheduler` or `s10_summary` | `verifying` |
| Every step is done or skipped | `done` |
| A step is `failed` and P2 waits for a decision, or P2 exited non-zero | `failed`. The Failed screen also names the phase the step belongs to |

"First unfinished" means the first step, in run order, whose status is neither `done` nor `skipped`.

`run` tracks the processes and is separate from `phase`:

| `run` | Meaning |
|---|---|
| `idle` | Nothing is running: checks, choices, or a resumed install not yet started |
| `running` | A bootstrap step or P2 is working |
| `pausing`, `paused` | Downloads are held at the next 1 MB chunk |
| `waiting` | P2 reported a failure and is waiting for a decision |
| `stopping`, `stopped` | Stop was pressed. P2 finished its chunk and exited 130. Everything can be resumed |
| `finished` | `done` was reached |

Resume after a restart: on start-up, P1 finds the home from the `NIGHTSHIFT_HOME` user variable, or
else from `answers.json` `home_path`. It reads `<home>\config\install_state.json` and
`<home>\config\answers.json`. If any step is done, the state has `resumed: true`. The Welcome screen
then shows its resume variant (§2.1), the answers are locked, and Start carries on at the first
unfinished step. Before P2 starts, download rows already show the bytes in each `<file>.part`
(`os.stat`).

Pause holds every active transfer before its next 1 MB chunk. The `.part` files stay. Stop does the
same, then P2 exits with code 130 and marks the running step `pending`, never `failed`.

### 1.6 Bootstrap steps in P1 (moved from stage1)

`installer/stage2/web/bootstrap.py` uses the standard library only. Each step has `check()`, `run()` and
`verify()`, like `Step`. Results go into the same `steps` map in `<home>\config\install_state.json`
under the ids `b01_tools`, `b02_home` and `b03_setup_env`. They run after `start` and before P2.

| Id | Title in the UI | What it does |
|---|---|---|
| `b01_tools` | Free tools | Handles Git (`Git.Git`, required), FFmpeg (`Gyan.FFmpeg`, required) and Windows Terminal (`Microsoft.WindowsTerminal`, optional). It refreshes PATH from the registry and skips any tool already found. Otherwise it runs `winget install --id <id> -e --silent --accept-package-agreements --accept-source-agreements`. A required tool still missing afterwards raises `NS-E110`. winget itself missing while a required tool is missing raises `NS-E111`. Download rows for these use `via: "winget"` with no size |
| `b02_home` | Install folder | Creates `<home>` and its top folders (`bin models venvs config data cache imports outbox rejected logs`). Sets the user variable `NIGHTSHIFT_HOME` through `winreg` (`HKCU\Environment`), then broadcasts `WM_SETTINGCHANGE` with `ctypes` `SendMessageTimeoutW`. Copies `uv.exe` into `<home>\bin\uv\`, moves the bootstrap log, and writes `<home>\config\answers.json` without keys |
| `b03_setup_env` | Setup environment | Runs `uv venv --python 3.12 <home>\venvs\studio` (when missing) and `uv pip install --python <py> -e "<App>[studio]"`, exactly as stage1 did. Its download row is "Python packages for setup", `via: "uv"`, with no size. Failure raises `NS-E105`, with uv's last 20 lines as the detail |

Later, `s04_venvs` finds `venvs\studio` already there and only adds what it adds today.

P1 then starts P2:

```
<home>\venvs\studio\Scripts\python.exe -X utf8 -m installer.stage2.wizard --app-path <App> --home <home>
    --answers <home>\config\answers.json --preflight <tmp json> --ui-bridge stdio
```

It runs with `PYTHONPATH=<App>`, and with stdin, stdout and stderr all set to `PIPE`. stderr goes to
the log and the console. The first line on stdin carries the keys (§1.7). P1 never writes the keys to
disk.

### 1.7 Engine bridge (P2 ↔ P1, JSON lines)

In bridge mode, `wizard.py` builds a `BridgeUi(Ui)` instead of `Ui`. It writes one JSON object per line
to stdout (UTF-8, `flush=True`) and reads commands from stdin on a daemon thread.

Events from P2 to P1:

```ts
type EngineEvent =
  | { type: "hello"; pid: number; version: string; resumed: boolean }
  | { type: "step"; id: StepId; status: "running" | "done" | "skipped" | "failed" | "warning"; title: string; detail: string | null }
  | { type: "say"; text: string; style: string | null }                  // Ui.say, rule, markdown, panel
  | { type: "table"; title: string; columns: string[]; rows: string[][] }
  | { type: "plan"; items: { key: string; file: string; bytes: number | null; via: "download" | "copy" }[]; totalBytes: number }
  | { type: "file"; key: string; event: "start"; total: number | null }
  | { type: "file"; key: string; event: "progress"; done: number; total: number | null }   // at most 4 per second per file
  | { type: "file"; key: string; event: "finish"; outcome: "downloaded" | "copied" | "skipped" | "missing" | "failed" }
  | { type: "warn"; code: string; detail: string }
  | { type: "failure"; step: StepId; code: string; message: string; hint: string; detail: string; optional: boolean; attempt: number }
  | { type: "verify"; checks: { name: string; passed: boolean; value: string | null; limit: string | null }[] }
  | { type: "sample"; kind: "voice" | "render" | "thumb"; path: string; seconds: number | null }
  | { type: "bundle"; path: string | null }
  | { type: "bye"; exitCode: number };
```

Commands from P1 to P2:

```ts
type EngineCommand =
  | { cmd: "keys"; pexels_api_key: string | null; pixabay_api_key: string | null }   // always the first line
  | { cmd: "decision"; action: "retry" | "skip" | "quit" }
  | { cmd: "pause" } | { cmd: "resume" } | { cmd: "stop" };
```

Wizard changes (all additive):

- **New flag.** `parse_args` gains `--ui-bridge {stdio}`. With it, `build_context` uses `BridgeUi` and
  sets `unattended=False`, even when stdin is not a TTY.
- **`step_status` hook.** `Ui` gains `step_status(step_id, status, title, detail)`, a no-op in plain
  `Ui`. `Wizard._step` calls it with `running` right after `ui.rule(...)`, and again after every
  `ctx.state.mark(...)`.
- **`decide_failure` hook.** `Ui` also gains `decide_failure(step_id, failure, optional, attempt) -> str | None`,
  which `_on_failure` calls first. `None` falls back to today's menu. `BridgeUi.decide_failure` emits
  `failure`, then blocks until a `decision` command arrives.
- **Prompts never block.** `BridgeUi.ask`, `confirm` and `choose` return the default and emit
  `{type: "warn", code: "prompt", detail: <prompt>}`. In bridge mode the answers are complete, so a
  prompt means a bug.
- **Download reporter.** `BridgeUi.reporter(total)` returns a `BridgeReporter`. It emits `file` events
  and calls `ctx.gate.wait()` inside `update()`. `ctx.gate` is a `threading.Event`, cleared by `pause`
  and set by `resume`.
- **Pause-safe retries.** `downloader.download()` gains `gate: threading.Event | None = None` and waits
  on it before each attempt. An attempt that failed while the gate was cleared does not count, so a
  long pause that drops the connection cannot use up the three retries. `StepContext.download` passes
  `ctx.gate`.
- **Stop.** The `stop` command sets `ctx.stop_requested`, and `BridgeReporter.update()` raises
  `SetupStopped` on its next call. `Wizard.run` catches `SetupStopped` the way it catches
  `KeyboardInterrupt`: it marks the running step `pending` and exits 130.
- **Keys.** `S01Questions._keys` reads the keys from the `keys` command. They are held only in memory,
  in `ctx.raw_answers`. It validates them and stores them in keyring, as today.

### 1.8 Unattended and CI

- Headless P1 (`--headless`) runs the checks, treats `answers.json` as the accepted choices, runs
  b01–b03, then starts P2 **without** `--ui-bridge`. Output and exit codes match today's: 3 for an
  unattended failure, 10 for a blocking preflight.
- `--simulate` on P1 passes `--simulate-downloads` to P2. b01–b03 use
  `installer/stage2/web/fakes.py`, which records commands and creates the venv folder with a stub
  `python.exe` copied from `sys.executable`.

### 1.9 Window and reconnect

1. P1 starts serving.
2. P1 opens the window, trying in this order:
   - the WebView2 host (§6)
   - Edge `--app=`, from `%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe`, then
     `%ProgramFiles%\Microsoft\Edge\Application\msedge.exe`
   - Chrome `--app=`, from `%ProgramFiles%\Google\Chrome\Application\chrome.exe`, then
     `%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe`
   - `os.startfile(url)`

   App mode uses `--app=<url> --window-size=1366,768 --user-data-dir=%LOCALAPPDATA%\Nightshift\browser`.
   The state records which one worked in `host`.
3. If the window closes while setup is running, nothing stops. When the host exits with code 0 and
   `run` is `running`, the console prints
   `21:20:03  The setup window was closed. Setup keeps running. Run INSTALL.cmd to open it again.`
4. Running `INSTALL.cmd` again: before the splash, stage1 reads `setup-server.json`. If that pid is
   alive and `GET /api/ping` answers within 2 s, stage1 prints `Setup is already running. Opening its
   window.`, calls `POST /api/internal/open-window` and exits 0. A stale file is deleted.
5. The UI reconnects on its own. The long poll retries every 2 s while it fails, and the connection
   banner (§3.5) appears after 6 s without an answer.

---

## 2. Screens

### 2.0 Shell shared by every screen

The window is 1366 × 768: the WebView2 client area, or the browser viewport.

```
┌────────────────────────────────────────────────────────────────────────────────────────────────────────────┐ 0
│ canvas: swirl image under a canvas-colour veil (layer 0)                                                   │
│  ╭──────────────────────────────────────────────────────────────────────────────────────────────────────╮  │ 16
│  │ ☾ Nightshift setup · Checking this PC                                                    —   ▢   ✕  │  │ 16–56  TitleBar
│  │╭──╮                                                                                                  │  │
│  ││  │   Screen title (Greeting size lg)                                         [secondary action]     │  │ 64–160 header
│  ││☾ │   One line that says what happens here                                                           │  │
│  ││● │  ┌────────┬────────┬────────┬────────┬────────┬────────┐                                         │  │ 176
│  ││○ │  │  col 1 │  col 2 │  col 3 │  col 4 │  col 5 │  col 6 │   6 columns, 20 px gap,                  │  │
│  ││○ │  │        │        │        │        │        │        │   content width 1166 px                  │  │
│  ││○ │  │        │        │        │        │        │        │                                          │  │
│  │╰──╯  └────────┴────────┴────────┴────────┴────────┴────────┘                                         │  │ 664
│  │      status line (muted, tabular figures)                               [secondary]  [primary]     │  │ 680–720 footer
│  ╰──────────────────────────────────────────────────────────────────────────────────────────────────────╯  │ 752
└────────────────────────────────────────────────────────────────────────────────────────────────────────────┘ 768
  x: 16 AppWindow edge · 16–120 StepRail (104) · 152–1318 content · 1350 AppWindow edge
```

| Region | Box (px) | Component and props |
|---|---|---|
| Canvas | 0, 0, 1366, 768 | `SetupBackdrop` (installer component, §3.2) |
| App frame | 16, 16, 1334, 736 | `AppWindow className="absolute inset-4 overflow-hidden"` |
| Title bar | frame top, height 40 | `TitleBar title="Nightshift setup" subtitle={phaseLabel} maximized hostControls={host.available()} onCloseRequest` (new in ref12, §6.4) |
| Step rail | x 16, frame height, width 104 | `StepRail steps={RAIL_STEPS} label="Setup steps"` (new in ref12, §5.3) |
| Header | 152, 64, 1166, 96 | `Greeting as="h1" size="lg" className="[&_p]:mt-2 [&_p]:max-w-[72ch]"` with per-screen `salutation`, `question` and `description`. The class override keeps the description to one line, under the 34ch default. An empty `question=""` renders a zero-height line. The right side holds an optional header `Button` |
| Body | 152, 176, 1166, 488 | CSS grid `grid grid-cols-6 gap-5`, with grid areas per screen |
| Footer | 152, 680, 1166, 40 | Left: `<p className="text-ds-xs text-ds-muted ds-num">`. Right: `Button size="lg"`s with `gap-3`. Secondary is `variant="soft"` or `"ghost"`, primary is `variant="primary"` |

`RAIL_STEPS` (icons from the extended registry, §5.3):

| id | Tooltip label | icon | State |
|---|---|---|---|
| `welcome` | Welcome | `moon` | `done` once the partner left Welcome |
| `checks` | Check this PC | `cpu` | `done` once Continue was pressed with no blocking row |
| `choices` | Your choices | `edit` | `done` once answers are accepted |
| `downloading` | Download | `download` | from `phase` |
| `installing` | Install | `package` | from `phase` |
| `verifying` | Test | `check` | from `phase` |
| `done` | Done | `film` | from `phase` |

The rail shows progress. It is not navigation. Before Start, the finished steps `welcome`, `checks` and
`choices` get `onSelect`, which posts `/api/ui-step` to go back. After Start nothing on the rail is
clickable. A failure marks the current step `failed`.

Phase labels for `TitleBar.subtitle`: Welcome, Checking this PC, Your choices, Downloading, Installing,
Testing, Ready, Stopped with an error, Uninstall.

### 2.1 Welcome

Purpose: say what Nightshift is, what will be installed, how big it is and how long it takes. Then
continue, or open Advanced.

```
╭─ ☾ Nightshift setup · Welcome ──────────────────────────────────────────────────────────── — ▢ ✕ ─╮
│▌  Set up Nightshift                                                                                 │
│▌  on this PC                                                                                        │
│▌  Nightshift writes a story, narrates it and edits a long video and Shorts while you sleep.         │
│▌  ┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────────────────┐ │
│▌  │ [notebook art]│ │ [film strip]  │ │ [microphone]  │ │               │ │ What setup installs   │ │
│▌  │               │ │               │ │               │ │               │ │ AI models     15.1 GB │ │
│▌  │ A long video  │ │ Shorts cut    │ │ A narrator    │ │               │ │ Python and    ~1.5 GB │ │
│▌  │ every week    │ │ from it       │ │ voice         │ │               │ │  packages             │ │
│▌  │ 10 to 14 min, │ │ 4 to 6 clips, │ │ Runs on your  │ │               │ │ Programs       0.7 GB │ │
│▌  │ with chapters │ │ own hooks     │ │ graphics card │ │               │ │ Free tools Git, FFmpeg│ │
│▌  └───────────────┘ └───────────────┘ └───────────────┘ │               │ │ ───────────────────── │ │
│▌  ┌───────────────────────────────────────────────────┐ │               │ │ To download   17.3 GB │ │
│▌  │ ◷ About 1 hour   46 min downloading at 50 Mbit/s, │ │               │ │ Free space     60 GB  │ │
│▌  │   15 min installing and testing. You can close    │ │               │ │ Time      about 1 h   │ │
│▌  │   this window and run INSTALL.cmd again any time  │ │               │ └───────────────────────┘ │
│▌  └───────────────────────────────────────────────────┘ └───────────────┘                           │
│▌  Version 0.3.0 · D:\NightShift                                         [Advanced]  [Start setup]   │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Grid areas (6 columns): `f1 f2 f3 . plan plan` / `time time time . plan plan`. The `plan` card spans
both rows. Column 4 is left empty on purpose: it gives the three tiles air and keeps the plan card
from sitting against them.

| Region | Component and props |
|---|---|
| Header | `Greeting salutation="Set up Nightshift" question="on this PC" description="Nightshift writes a story, narrates it and edits a long video and Shorts while you sleep."` |
| f1 | `FeatureCard illustration={<StoryIllustration />} title="A long video every week" description="10 to 14 minutes, with chapters and ad-break points"`. No `href` or `onClick`, so it renders a static `article` |
| f2 | `FeatureCard illustration={<ClipsIllustration />} title="Shorts cut from it" description="4 to 6 vertical clips, each with its own hook"` |
| f3 | `FeatureCard illustration={<VoiceIllustration />} title="A narrator voice" description="Runs on your graphics card. Nothing leaves this PC"` |
| plan | `Card elevation={1} padding="lg"` with `CardHeader title="What setup installs" level={2}`. Then one row per `plan.groups[]`: the label left in `text-ds-sm`, the size right in `text-ds-sm ds-num`, and unknown sizes shown as `size shown when the download starts` in `text-ds-muted`. Then a `DottedRule`, then three total rows: `To download`, `Free space needed`, `Time` |
| time | `Card elevation={0} tone="sunken" padding="md"` with `MetaItem icon="clock"` "About 1 hour" in `text-ds-md font-semibold`, and the explanation in `text-ds-xs text-ds-text-2` |
| Footer left | `Version {state.version} · {state.appPath}` |
| Footer right | `Button variant="soft"` "Advanced" (opens the drawer), then `Button variant="primary" trailingIcon="chevronRight"` "Start setup" (`POST /api/ui-step {uiStep: "checks"}`). Nothing is installed until Install on Choices |

The Advanced drawer is a `Card elevation={3} radius="2xl" padding="lg"` that slides in over columns 4–6
(`translateX`, 200 ms, `ease-ds-out`). It closes with Escape or a click outside, and it contains:

- **Use answers from a file.** `TextField label="answers.json path"` and `Button variant="soft"`
  "Load". The server reads the file, posts it through the same validation as `POST /api/answers` and
  shows field errors inline.
- **Open the setup log folder.** `Button variant="ghost" leadingIcon="folder"`, which calls `reveal`
  with the log folder.
- **Run without this window next time.** Static text: "Start INSTALL.cmd with -NoUi, or put
  answers.json next to it."

Time estimate:

- `downloadMin = ceil(plan.totalBytes * 8 / (50e6 * 60))`, the same 50 Mbit/s assumption as
  `downloader.eta_text`.
- `installMin = 15`.
- The label is "About 1 hour" when the sum is 50–75 min. Below 50 it is "About N minutes", rounded to
  5. Above 75 it is "About N hours", rounded to the half hour.

Resume variant (`resumed: true`):

- Salutation "Setup is halfway", question "on this PC".
- Description "It stopped during {stepTitle}. Everything finished so far is kept".
- The plan card title becomes "Still to do" and lists only unfinished groups, with the bytes left.
- The primary button reads "Continue setup". It sends `resume` and goes straight to the Download,
  Installing or Verify screen.
- Checks run again in the background (`recheck`) and only interrupt with the Checks screen when a row
  blocks.

Prefilled variant (`-Ui` with `answers.json`): the footer left reads `Answers loaded from
{prefilledFrom}`.

### 2.2 Hardware check

Purpose: show every check as a plain row, block only on what really blocks, and say exactly what to do.

```
╭─ ☾ Nightshift setup · Checking this PC ─────────────────────────────────────────────────── — ▢ ✕ ─╮
│▌  This PC                                                                     [Check again]         │
│▌  8 of 10 checks passed. 2 warnings, nothing blocks setup.                                          │
│▌  ┌──────────────────────────────────────────────────────────────┐ ┌──────────────────────────────┐ │
│▌  │ ● Windows 10/11, 64-bit   Windows 11 Pro 10.0.26200       OK │ │ Against what Nightshift needs│ │
│▌  │ ● Graphics card           RTX 5060 Ti, 16 GB              OK │ │  100%     100%      71%      │ │
│▌  │ ● NVIDIA driver           CUDA 13.0, driver 581.57        OK │ │  ▐█▌      ▐█▌       ▐▆▌      │ │
│▌  │ ● Memory                  32 GB, all model sizes          OK │ │  ▐█▌      ▐█▌       ▐▆▌      │ │
│▌  │ ● Free disk space         43 GB free on D:           Warning │ │  VRAM     Memory    Disk     │ │
│▌  │ ● Disk type               D: SSD                          OK │ │  16/16    32/16     43/60 GB │ │
│▌  │ ● Internet                all sites reachable             OK │ └──────────────────────────────┘ │
│▌  │ ● winget (App Installer)  available                       OK │ ┌──────────────────────────────┐ │
│▌  │ ● Long file paths         off                        Warning │ │ ⚠ NS-W112 Long file paths off│ │
│▌  │ ● Wake timers             enabled                         OK │ │ Setup works. Press Fix it to │ │
│▌  └──────────────────────────────────────────────────────────────┘ │ turn them on                 │ │
│▌                                                                   │ [Fix it]                     │ │
│▌                                                                   └──────────────────────────────┘ │
│▌  Checked 21:04                                                           [Back]  [Continue]        │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Grid areas: `rows rows rows rows meters meters` / `rows rows rows rows cards cards`. The `cards` area
scrolls when it holds more than two cards. Blocking cards come first.

| Region | Component and props |
|---|---|
| Header | `Greeting salutation="This PC" question="" description={summary}`. Right: `Button variant="soft" size="md" leadingIcon="retry"` "Check again", which calls `recheck` |
| rows | `Card elevation={1} padding="md"` containing a `<ul>` with one `CheckRowItem` per `state.checks[]` |
| `CheckRowItem` (installer component) | Row height 40, gap 4, hover `bg-ds-hover`. Contents, left to right: `StatusDot tone={ok: "success", warn: "attention", fail: "danger"} size={8}`; the label in `text-ds-sm font-medium` (a fixed 200 px column); the value in `text-ds-sm text-ds-text-2 ds-num` (flexes); `Badge tone={ok: "success", warn: "neutral", fail: "danger"} variant="soft" size="sm"` reading `OK`, `Warning` or `Blocks setup`. Clicking a row that is not ok scrolls its card into view and focuses its button |
| meters | `MeterGroup title="Against what Nightshift needs" meters={[{label: "VRAM", value, tone}, {label: "Memory", value, tone}, {label: "Disk", value, tone}]}` (ported, §5.2). `value = min(100, round(haveGb / needGb * 100))` from `machine.meters`. `tone` is `"accent"` at 100, `"danger"` below `hardMinGb`, and `"highlight"` in between. Under each meter is a caption in `text-ds-2xs text-ds-muted ds-num`: `16/16 GB`, `32/16 GB`, `43/60 GB` |
| cards | One `IssueCard` per row that is not ok |
| `IssueCard` (installer component) | `Card elevation={2} padding="md" radius="lg"`. Title row: `Icon name={fail ? "error" : "warning"} size={18}`, then `Badge tone={fail ? "danger" : "neutral"} size="sm"` with the code, then the title in `text-ds-md font-semibold`. Body: `whatToDo` in `text-ds-sm text-ds-text-2`, max 44ch. Action: `row.fix` as a `Button` (`variant={fail ? "primary" : "soft"} size="md"`) |

Rows and their copy. The blocking rules are `Get-PreflightRows`, unchanged except the network row:

| Row | Blocks | Card title | What to do (shown) | Fix button |
|---|---|---|---|---|
| os, `NS-E101` | yes | This PC cannot run Nightshift | Nightshift needs 64-bit Windows 10 or 11. Tell the developer | none. Continue stays disabled |
| gpu, `NS-E102` | yes (a warning with `allow_cpu`) | No NVIDIA graphics card found | Install the NVIDIA driver from nvidia.com/drivers, restart the PC, then press Check again | "Open the driver page" (`open-url`, `https://www.nvidia.com/Download/index.aspx`) |
| gpu, VRAM under 15 000 MB | no (warn) | The graphics card has less than 16 GB | Setup can continue, but only the Small model fits and videos take longer | none |
| driver, `NS-E103` | yes under CUDA 12.4, warn under 12.8 | NVIDIA driver too old | Install the newest Game Ready or Studio driver, restart, then press Check again | "Open the driver page" |
| ram | no (warn under 16 GB) | Less memory than recommended | Setup continues with the Small model only | none |
| disk, `NS-E104` | yes under 40 GB | Not enough free disk space | Free 60 GB on this drive, or pick another drive below | "Pick another drive" |
| disk, under 60 GB | no (warn) | Less free space than recommended | Setup fits, but leaves little room for videos. 60 GB keeps about a month of them | "Pick another drive" |
| disktype, `NS-W106` | no (warn) | The install drive is a hard disk | Models load slowly from a hard disk. Pick an SSD drive if you have one | "Pick another drive" |
| network, `NS-E105` | yes when github.com, pypi.org or files.pythonhosted.org is unreachable. Warn when only huggingface.co is | A download site cannot be reached | Check the internet, switch off any VPN or proxy, then press Check again. If only Hugging Face is blocked: In the next step, choose "A folder on this PC" or "A zip from Google Drive" for the model files | "Check again" |
| winget, `NS-E111` | yes when Git or FFmpeg is missing | App Installer is missing | Install "App Installer" from the Microsoft Store, then press Check again | "Open the Microsoft Store" (`open-url`, `ms-windows-store://pdp/?ProductId=9NBLGGH4NNS1`) |
| longpaths, `NS-W112` | no (warn) | Long file paths are off | Setup works. Press Fix it to turn them on (Windows asks for permission) | "Fix it" (`elevate-longpaths`) |
| waketimers, `NS-W107` | no (warn) | The PC cannot wake itself at night | Open Power Options, Change plan settings, Change advanced power settings, Sleep, Allow wake timers, and pick Enable | none |

"Pick another drive" expands, inside the card, a
`SegmentedControl label="Install drive" variant="plain" options={drives where freeGb >= need}`, with
labels like `E: 412 GB free`. Picking a drive posts a draft with `homePath = "<letter>:\\Nightshift"`
and P1 re-evaluates the disk rows. When no drive has room, the control is replaced by the line "No
drive has 40 GB free. Free some space, then press Check again".

The `description` is "`{ok} of {n}` checks passed." followed by one of:

- "Nothing blocks setup."
- "`{w}` warnings, nothing blocks setup."
- "`{f}` items block setup. Fix them, then press Check again."

Footer:

- Left: `Checked {HH:MM}`. With a blocking row it reads `Fix the red items to continue`, in
  `text-ds-danger`.
- Right: `Button variant="soft"` "Back" and `Button variant="primary" disabled={state.blocking}`
  "Continue" (`/api/ui-step choices`).

While a re-check runs (5–20 s, `checking: true`), "Check again" reads "Checking" and is disabled. The
rows keep their last values at 60% opacity, and nothing moves.

### 2.3 Choices

Purpose: everything `s01_questions` asks, on one screen, next to the sizes that depend on it.

```
╭─ ☾ Nightshift setup · Your choices ─────────────────────────────────────────────────────── — ▢ ✕ ─╮
│▌  Your choices                                                                                      │
│▌  You can change the channel, voice and time later in the dashboard.                                │
│▌  ┌───────────────────────────────┐ ┌───────────────────────────────┐ ┌───────────────────────────┐ │
│▌  │ Channel                       │ │ Model size                    │ │ Night run                 │ │
│▌  │ Channel name [After Dark Tales│ │ [ Small | Balanced | Best ]   │ │ ( 03 h )( 00 m )          │ │
│▌  │ About        [horror fiction ]│ │ 9.0 GB · fits 16 GB of video  │ │ [01:00|03:00|05:00|Other] │ │
│▌  │ Your name    [Abdo           ]│ │ Fastest. Good stories, Shorts │ │ The PC wakes at this time │ │
│▌  └───────────────────────────────┘ │ Download 17.3 GB · 60 GB free │ └───────────────────────────┘ │
│▌  ┌───────────────────────────────┐ └───────────────────────────────┘ ┌───────────────────────────┐ │
│▌  │ Narrator                      │ ┌───────────────────────────────┐ │ Stock footage keys        │ │
│▌  │ (Ryan)(Aiden)(Serena)(Vivian) │ │ Model files come from         │ │ Pexels  [••••••••]  Works │ │
│▌  │ Calm, low male voice          │ │ (•) The internet              │ │ Pixabay [••••••••]  Works │ │
│▌  │ You hear it in the test step  │ │ ( ) A folder on this PC       │ │ Get free keys ↗           │ │
│▌  └───────────────────────────────┘ │ ( ) A zip from Google Drive   │ └───────────────────────────┘ │
│▌                                    └───────────────────────────────┘                               │
│▌  Install folder D:\Nightshift · 412 GB free  [More options]                [Back]  [Install]      │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Grid areas: `chan chan tier tier run run` / `voice voice src src keys keys`.

| Region | Component and props |
|---|---|
| Header | `Greeting salutation="Your choices" question="" description="You can change the channel, voice and time later in the dashboard."` |
| chan | `Card padding="lg"` with `CardHeader title="Channel" level={2}` and three fields: `TextField label="Channel name" maxLength={60} required`; `TextField label="What it is about" placeholder="horror fiction" required`; `TextField label="Your name" placeholder="Shown on the dashboard" maxLength={40}` |
| tier | `Card padding="lg"`, `CardHeader title="Model size"`. `SegmentedControl label="Model size" options={[{value: "small", label: "Small"}, {value: "balanced", label: "Balanced"}, {value: "best", label: "Best"}]} variant="filled" size="md"`. Under it, three lines for the selected tier in `ds-num`: size and fit, a description, then `Download {totalGb} GB · needs {needGb} GB free`. A tier with `allowed: false` stays selectable. Selecting it shows `Badge tone="danger" size="sm"` with its `reason` and disables Install |
| run | `Card padding="lg"`, `CardHeader title="Night run"`. `<p className="flex gap-2">` with `TimeChip value={hh} unit="h"` and `TimeChip value={mm} unit="m"` (ported, §5.2). Then `SegmentedControl label="Run time" options={["01:00", "03:00", "05:00", "other"]}`. "Other" reveals `TextField label="Time, 24 hour clock" inputMode="numeric" pattern="([01]\d\|2[0-3]):[0-5]\d"`. Caption in `text-ds-xs text-ds-muted`: "The PC wakes itself at this time, makes a video and goes back to sleep" |
| voice | `Card padding="lg"` with `CardHeader title="Narrator"`, then `SegmentedControl label="Narrator voice" variant="plain" options={PRESET_VOICES.map(v => ({value: v, label: v}))}`, then the voice line for the selection, then the caption "You hear it in the test step. Designed and cloned voices are made later in the dashboard" |
| src | `Card padding="lg"`, `CardHeader title="Model files come from"`, and a radio group of three `ChoiceRow`s. `ChoiceRow` (installer component) is a native `<input type="radio">` (or `checkbox`) styled as a 40 px row, `rounded-ds-md`, `bg-ds-sunken` when checked. The folder and Drive zip options reveal `TextField label="Folder"`, followed by the `DiskCheck` (`source=1`) result line: `Found 7 of 9 model files, 13.4 GB`, or the error |
| keys | `Card padding="lg"` with `CardHeader title="Stock footage keys"`. Two fields, `TextField type="password" autoComplete="off" label="Pexels key"` and the same for `"Pixabay key"`, each checked on blur through `/api/check-key`. After each field: `Icon name="check"` with "Works" in `text-ds-success-ink`, or `Icon name="error"` with the message in `text-ds-danger`. Then `Button variant="ghost" size="sm" trailingIcon="external"` "Get free keys", which opens a two-line popover linking https://www.pexels.com/api/ and https://pixabay.com/api/docs/ (`open-url`) |
| Footer left | `Install folder {homePath} · {freeGb} GB free`, then `Button variant="ghost" size="sm"` "More options" |
| Footer right | `Button variant="soft"` "Back" and `Button variant="primary" disabled={!valid}` "Install" |

Voice lines. The presets are `s01_questions.PRESET_VOICES`. These lines are UI copy only:

| Voice | Line |
|---|---|
| Ryan | Calm, low male voice |
| Aiden | Bright, younger male voice |
| Serena | Warm female voice |
| Vivian | Clear, steady female voice |

Tier readouts. Sizes come from `manifest.json` at runtime. The values below are what the 0.1.0
manifest gives:

| Tier | Line 1 | Line 2 | Allowed when |
|---|---|---|---|
| Small | `9.0 GB · fits 16 GB of video memory` | Fastest. Good stories and Shorts | always |
| Balanced | `14.3 GB · needs 32 GB of memory` | Better long stories. A little slower | `ramGb >= 31` |
| Best | `22.9 GB · needs 32 GB of memory` | Best writing. Slowest, largest download | `ramGb >= 31` |

**Default tier (owner decision 2026-11-02).** Balanced is the recommended default, so `answers.json` ships with `"llm_tier": "balanced"` and the Choices screen starts on Balanced (`plan.recommendedTier`). On a PC with less than 32 GB of memory (`ramGb < 31`) the screen starts on Small instead and says why in one line under the selector: "This PC has 16 GB of memory, so Small is selected. Balanced needs 32 GB". An unattended `answers.json` that asks for Balanced or Best on such a PC still fails with "llm_tier 'balanced' needs 32 GB of RAM". The Welcome screen's size, free-space and time figures follow the selected tier (default download about 20 GB with Balanced, about 15 GB with Small), and its plan card names the tier.

"More options" opens a `Card elevation={3} padding="lg"` popover, anchored above the footer, with:

- **Install folder.** `TextField label="Install folder"`, followed by the `/api/disk` result (debounced
  400 ms). For example `412 GB free on D: (SSD)` in muted, or
  `Only 31 GB free on C:. Nightshift needs 60 GB` in danger. A web page cannot open a native folder
  picker, so the path is typed. Under it,
  `SegmentedControl label="Drive" variant="plain" size="sm"` lists the drives from the preflight, and
  picking one fills `<letter>:\Nightshift`.
- **Long videos per week.** `SegmentedControl label="Long videos per week" options={["1", "2", "3"]}`.
  Default 1, with the caption "Start with 1 for a new channel".
- **Nightshift Code.** A `ChoiceRow` checkbox: "Also install Nightshift Code, an offline coding
  assistant". Default on.
- **Theme.**
  `SegmentedControl label="Theme" options={[{value: "dark", label: "Dark", icon: "moon"}, {value: "light", label: "Light", icon: "sun"}]}`.
  It switches `document.documentElement.dataset.theme` at once, and the choice is saved for the
  dashboard.

Validation happens twice. P1 checks the fields (`answers_view.validate`, standard library), and P2
checks them again with pydantic:

| Field | Rule | Message |
|---|---|---|
| `channel.displayName` | 1–60 characters after trimming | Give the channel a name |
| `channel.niche` | 2–60 characters | Say in a few words what the channel is about |
| `ownerName` | 0–40 characters | Use 40 characters or fewer |
| `homePath` | absolute path on a fixed local drive, no `%`, 80 characters at most, at least `required_free_gb(tier)` free | Pick a folder on a drive with {need} GB free. This one has {free} GB |
| `llmTier` | allowed on this PC | {Tier} needs 32 GB of memory. This PC has {ram} GB |
| `modelsSource.path` | an existing folder when kind is `folder` or `drive-zip` | That folder was not found |
| `runTime` | `^([01]\d\|2[0-3]):[0-5]\d$` | Use the 24 hour clock, for example 03:00 |
| `pexelsKey`, `pixabayKey` | not empty, unless a key is already stored (resume) | Paste the {Pexels} key. Setup needs it to find stock footage |

Install posts the draft to `POST /api/answers` if it changed since the last accepted version, then
calls `start`.

**Drive zip source.** The folder holds one or more `*.zip` files downloaded from the Google Drive models
folder. Drive splits big folders into several zips. Before `s03`, P1 extracts every zip, streamed, into
`<home>\cache\models_incoming\` and passes that folder to P2 as `models_source`. While they unpack, the
disk check also counts the zips' total size. After `s03` verifies, `models_incoming` is deleted. The
zips themselves are never touched.

**New answers fields** (contract change CR-C1-UI-1, §5.5):

- `owner_name: string | null`
- `ui_theme: "dark" | "light"`, default `"dark"`
- `models_source_kind: "internet" | "folder" | "drive-zip"`, default `"internet"`. With `"drive-zip"`,
  `models_source` points at the folder holding the zips.

### 2.4 Download

Purpose: one row per file, honest speed and time left, pause and resume, and it survives restarts.

```
╭─ ☾ Nightshift setup · Downloading ──────────────────────────────────────────────────────── — ▢ ✕ ─╮
│▌  Downloading 17.3 GB                                                         [Pause]              │
│▌  You can close this window. Keep the black setup window open, it is doing the work.                │
│▌  ┌───────────────────────────────────────────────────────────────────────────┐ ┌─────────────────┐ │
│▌  │ Free tools                                                                │ │ Time left       │ │
│▌  │ ● Git                      already here                             done  │ │ ( 00 h )( 41 m )│ │
│▌  │ ○ FFmpeg                   installing with winget                         │ │ 6.2 of 17.3 GB  │ │
│▌  │ Programs                                                                  │ │ 11.4 MB/s       │ │
│▌  │ ● Model runner (llama.cpp) llama-b12032-bin-win-cuda-13.4-x64.zip   100%  │ └─────────────────┘ │
│▌  │ ● CUDA runtime             cudart-llama-bin-win-cuda-13.4-x64.zip  62% ▬▬ │ ┌─────────────────┐ │
│▌  │ AI models                                                                 │ │ From the        │ │
│▌  │ ● Story model (Small)      Qwen3-14B-Q4_K_M.gguf     2.1/9.0 GB  23% ▬    │ │ internet        │ │
│▌  │                            11.4 MB/s · 10 min left                        │ │ Hugging Face and│ │
│▌  │ ○ Voice: preset narrators  qwen-talker-1.7b-customvoice-Q8_0  2.0 GB      │ │ GitHub. Every   │ │
│▌  │ ○ Voice: your own voices   qwen-talker-1.7b-base-Q8_0.gguf    2.1 GB      │ │ file is checked │ │
│▌  │ ○ Caption timing           faster-distil-whisper-large-v3     1.5 GB      │ │ after download  │ │
│▌  └───────────────────────────────────────────────────────────────────────────┘ └─────────────────┘ │
│▌  3 of 14 files · 6.2 of 17.3 GB                                       [Stop setup]  [Pause]       │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Grid areas: `list list list list list eta` / `list list list list list src`. The list scrolls inside its
card. Within each group, active rows are pinned to the top.

| Region | Component and props |
|---|---|
| Header | `Greeting salutation={`Downloading ${totalGb} GB`} question="" description="You can close this window. Keep the black setup window open, it is doing the work."`. Right: `Button variant="soft" leadingIcon={paused ? "play" : "pause"}`, reading "Pause" or "Resume" |
| list | `Card padding="md"` with a heading per group, `CardHeader level={3}`: Free tools, Python packages, Programs, AI models. One `DownloadRow` per `downloads.items[]` |
| `DownloadRow` (installer component) | 48 px tall, 64 px while active (it gains a second line). Contents, left to right: <br>• Status mark: `StatusDot` tone `online` while active, `success` when done, `danger` when failed. Other states show an empty 8 px ring in `border-ds-default`. <br>• Label in `text-ds-sm font-medium`, a 220 px column. <br>• File name in `text-ds-xs text-ds-muted font-mono truncate` (flexes). <br>• Size in `text-ds-xs ds-num`, like `2.1/9.0 GB`. <br>• `ProgressBar value={pct} label={`${label} progress`} maxTrack={120} tone={failed ? "danger" : "accent"}`. Rows with `via` `winget` or `uv` have no size and show the text "installing" instead of a bar. <br>Second line while active: `{speed} · {eta} left`, in `text-ds-2xs text-ds-text-2 ds-num`. Second line when failed: `{code} {message}` in `text-ds-danger`, with `Button variant="ghost" size="sm" leadingIcon="retry"` "Retry" |
| eta | `CountdownCard title="Time left" target={new Date(Date.now() + overall.etaS * 1000)} units={["h", "m"]}` (ported with the new `units` prop, §5.2), followed by two lines in `text-ds-sm ds-num`: `{done} of {total} GB` and `{speed}/s`. While `etaS` is null (the first 10 s, or while paused) it is a plain `Card padding="md"` titled "Time left", with "Measuring speed" or "Paused" in `text-ds-text-2` |
| src | `Card tone="sunken" elevation={0} padding="md"`. Title and body depend on the source: <br>• "From the internet": "Hugging Face and GitHub. Every file is checked after download" <br>• "From your folder": "Files that match are copied from {path}. The rest is downloaded" <br>• "From the Google Drive zip": "Unpacking {n} zips into the install folder, then copying" |
| Footer left | `{filesDone} of {filesTotal} files · {doneGb} of {totalGb} GB` |
| Footer right | `Button variant="ghost"` "Stop setup" (opens the dialog in §2.9), then `Button variant="primary"` "Pause" or "Resume" |

Friendly labels (`installer/stage2/web/labels.py`, by plan key):

| Key | Label |
|---|---|
| `git`, `ffmpeg`, `terminal` | Git, FFmpeg, Windows Terminal |
| `setup_env` | Python packages for setup |
| `llama_cuda`, `llama_cuda13`, `llama_cpu` | Model runner (llama.cpp) |
| `cudart`, `cudart13` | CUDA runtime |
| `qwentts` | Voice engine |
| `opencode` | Nightshift Code |
| `yt_dlp` | Video downloader for Clipper |
| `llm`, `llm_balanced`, `llm_best` | Story model (Small), (Balanced), (Best) |
| `embed` | Memory index |
| TTS CustomVoice, Base, tokenizer | Voice: preset narrators, Voice: your own voices, Voice codec |
| `whisper` | Caption timing |
| `yunet` | Face finder for Clipper |
| `spacy` | Language rules |
| PyTorch fallback files | Backup voice engine files |

Speed and time left are computed in P1, not in the UI:

- Per file: an exponential moving average of bytes per second over 5 s windows, with `alpha = 0.3`.
- Overall speed: the sum over active files.
- Overall `etaS = (bytesTotal - bytesDone) / overallSpeed`. It is shown only after 10 s of data, and
  rounded up to the whole minute.
- Rows without a size add no bytes and do not hold up the estimate.

**Resume after a restart.** A file with a `.part` starts as `queued`, with `bytesDone` from the file
size and the note "will continue from {x} GB". A file with a verified `.ok` marker shows "already here"
and `done`.

**Pause.** The header and footer buttons switch to Resume. Active rows show `paused` and keep their
bars, and the CountdownCard shows "Paused". Winget and uv installs cannot pause. While one runs, the
button reads "Pause after this tool" and takes effect when the file downloads begin.

### 2.5 Installing

Purpose: show the steps that come after the downloads as a short list with a count. No invented
percentages.

```
╭─ ☾ Nightshift setup · Installing ───────────────────────────────────────────────────────── — ▢ ✕ ─╮
│▌  Installing                                                                                        │
│▌  Setting things up on this PC. This takes 5 to 15 minutes.                                         │
│▌  ┌─────────────────────────────┐ ┌─────────────────────────────────────────────────────────────┐   │
│▌  │ Steps                       │ │ ● Python environments            done · 3 min 12 s          │   │
│▌  │                             │ │ ● Nightshift Code first start    done · 41 s                │   │
│▌  │        ┌──────────────┐     │ │ ● Tune for this graphics card    running · 1 min 05 s       │   │
│▌  │        │▓▓▓▓▓▓▓░░░░░░ │     │ │   Trying the story model at different settings, 54 tokens/s │   │
│▌  │        │      ·  ·    │     │ │ ○ Settings and keys              waiting                    │   │
│▌  │        └──────────────┘     │ └─────────────────────────────────────────────────────────────┘   │
│▌  │            2/4              │ ┌─────────────────────────────────────────────────────────────┐   │
│▌  └─────────────────────────────┘ │ Latest  21:41:07  ns tune: llm ctx 16384, 54.2 tokens/s     │   │
│▌                                  └─────────────────────────────────────────────────────────────┘   │
│▌  Step 3 of 4                                                                  [Stop setup]        │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Grid areas: `stepper stepper list list list list` / `stepper stepper log log log log`.

| Region | Component and props |
|---|---|
| Header | `Greeting salutation="Installing" question="" description="Setting things up on this PC. This takes 5 to 15 minutes."` |
| stepper | `IntakeStepper title="Steps" value={doneCount} max={stepCount} unit="step" readOnly vessel="drive"` (ported with `readOnly` and `vessel`, §5.2). The drive fills as steps finish. `readOnly` hides the −/+ buttons |
| list | `Card padding="md"` with one `StepItem` per step |
| `StepItem` (installer component) | 44 px row. Mark: `StatusDot` `success` when done, `online` while running, `danger` when failed, an empty ring while waiting. Title in `text-ds-sm font-medium`. Status in `text-ds-xs text-ds-text-2 ds-num`: `done · 3 min 12 s`, `running · 1 min 05 s` (ticks every second), `waiting`, or `skipped, not chosen`. A running row adds a second line with P2's latest `say` text for that step |
| log | `Card tone="sunken" elevation={0} padding="sm"` holding one line in `text-ds-xs font-mono`: "Latest", then the newest log line, updated in place |
| Footer | Left: `Step {n} of {stepCount}`. Right: `Button variant="ghost"` "Stop setup" |

Steps and their plain titles:

- `s04_venvs`: Python environments
- `s05_opencode_prewarm`: Nightshift Code first start (skipped when not chosen; `stepCount` stays 4)
- `s06_tune`: Tune for this graphics card
- `s07_configs`: Settings and keys

The brief names "venv, CUDA runtime, binaries, first-run config". The binaries and the CUDA runtime are
downloaded and unpacked by `s02`, so they appear on the Download screen under Programs. Installing
shows what actually runs after the downloads.

### 2.6 Verify

Purpose: prove the PC works, with real output the partner can see and hear. This screen carries the
installer's one rich moment.

```
╭─ ☾ Nightshift setup · Testing ──────────────────────────────────────────────────────────── — ▢ ✕ ─╮
│▌  Testing                                                                                           │
│▌  Nightshift makes a short test video and reads a sentence in your narrator's voice.                │
│▌  ┌─────────────────────────────────────┐ ┌──────────────────────────┐ ┌───────────────────────────┐ │
│▌  │ Checks                              │ │ ┌────────────┐           │ │ Ryan                      │ │
│▌  │ ● Story model     54 tokens/s    OK │ │ │            │           │ │ (▶) ▂▅▇▆▃▅▇▆▄▂▅▆▇▅▃▂  0:04│ │
│▌  │ ● Memory index    ready          OK │ │ │ test frame │           │ │ "This is how your channel │ │
│▌  │ ● Voice engine    Ryan, 0.31 RTF OK │ │ │  9:16      │           │ │ will sound at night."     │ │
│▌  │ ● Captions        graphics card  OK │ │ │            │           │ └───────────────────────────┘ │
│▌  │ ● Video encoder   NVENC          OK │ │ │            │           │ ┌───────────────────────────┐ │
│▌  │ ● Faster decoding off       Warning │ │ └────────────┘           │ │ NS-W302 Faster decoding   │ │
│▌  │ ● Shortcuts and nightly task     ...│ │ Test video · 0:06 · NVENC│ │ was not faster here       │ │
│▌  │ ○ Diagnostics zip                   │ │ [Play]                   │ │ Nothing to do             │ │
│▌  └─────────────────────────────────────┘ └──────────────────────────┘ └───────────────────────────┘ │
│▌  6 of 8 done                                                            [Copy test report]       │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Grid areas: `checks checks render render voice voice` / `checks checks render render notes notes`.

| Region | Component and props |
|---|---|
| Header | `Greeting salutation="Testing" question="" description="Nightshift makes a short test video and reads a sentence in your narrator's voice."` |
| checks | `Card padding="md"` with `CardHeader title="Checks"`, one `VerifyRowItem` per `verify.checks[]` (laid out like `CheckRowItem`, value in `ds-num`), and then two more rows, "Shortcuts and nightly task" and "Diagnostics zip", from the `s09` and `s10` step states |
| render | `Card padding="md"` holding a 9:16 frame, 180 × 320, `rounded-ds-lg overflow-hidden bg-ds-sunken`, showing `<img src="/api/file/render-test.jpg" alt="First frame of the test video">`. "Play" (`Button variant="soft" size="sm" leadingIcon="play"`) swaps it for `<video src="/api/file/render-test.mp4" controls autoPlay>`. Caption in `text-ds-xs text-ds-text-2 ds-num`: `Test video · 0:06 · {encoder}`. While pending, the empty frame shows `Icon name="film" size={22}` in muted and the line "Rendering a 6 second test" |
| voice | `Card padding="md"` with `CardHeader title={speaker}`. Then a row: `IconButton icon={playing ? "pause" : "play"} label={playing ? "Pause sample" : "Play sample"} variant="accent" round size="md"`, then `Waveform bars={voice.bars} progress={currentTime / voice.seconds}` (ported), then the length (`0:04`). Under it, the sentence in `text-ds-sm text-ds-text-2`. Audio plays through `new Audio("/api/file/voice-sample.wav")` |
| notes | One `IssueCard` per notice raised during verify (`NS-W301`, `NS-W302`, `NS-W401`, `NS-W403`), with the copy from §3.6 |
| Footer | Left: `{done} of {total} done`. Right: `Button variant="soft" leadingIcon="copy"` "Copy test report", enabled once `verify.reportReady` |

Check labels map from the names in `verify-report.json`. A name not listed here shows as it is:

| Name prefix | Label | Value shown |
|---|---|---|
| `llm` | Story model | `{value} tokens/s` |
| `embed` | Memory index | `ready` |
| `tts` | Voice engine | `{speaker}, {value} RTF` |
| `whisper` | Captions | `graphics card` or `processor` |
| `nvenc` | Video encoder | `NVENC`, or `libx264 (slower)` after the fallback |
| `mtp` | Faster decoding | `on`, or `off` |
| `bench.*` | not listed; kept in the report | |

**Samples step `v01_samples`.** A new optional step after `s08_smoke`. It never blocks:

1. **Voice.** Runs `studio voice preview <speaker> --out <home>\cache\setup-test\voice-sample.wav`. The
   command already exists in `ns_studio/cli.py`, with the default sentence "This is how your channel
   will sound at night." P1 serves the WAV and computes `bars`: 28 RMS buckets read with `wave` and
   `array('h')`, scaled to 0.25–1.
2. **Test render.** Runs `studio run --fake-services --format shorts_only --count 1` with
   `NIGHTSHIFT_HOME` set, for that child process only, to a scratch home at
   `<home>\cache\setup-test\home`. The scratch home is seeded with copies of `config\runtime.json` and
   `config\channels\<slug>.yaml`, so the real outbox and database stay clean. This is the same
   fake-services render that the slice gates use, here running on this PC's FFmpeg and encoder.
   Afterwards the step copies the MP4 to `<home>\cache\setup-test\render-test.mp4` and grabs a frame
   with `ffmpeg -ss 2 -i render-test.mp4 -frames:v 1 -q:v 3 render-test.jpg`.
3. **Failures.** If either part fails, the step emits `warn` with `NS-W403` (CR-C1-UI-2). The render
   card shows the §3.6 text, and the voice card hides when there is no sample.

**Copy test report.** `fetch("/api/file/verify-report.json")`, then `navigator.clipboard.writeText`,
then the toast "Test report copied". The page is served from `127.0.0.1`, which browsers treat as
secure, so the clipboard API is available. If the clipboard is refused, a dialog shows the JSON in a
read-only `<textarea>` with a "Select all" button.

### 2.7 Done

```
╭─ ☾ Nightshift setup · Ready ────────────────────────────────────────────────────────────── — ▢ ✕ ─╮
│▌  Nightshift is ready                                                                               │
│▌  Your first video is one click away. Tonight at 03:00 the PC makes one on its own.                 │
│▌  ┌───────────────────────────────────────┐ ┌───────────────────────────┐ ┌───────────────────────┐ │
│▌  │ Installed                             │ │ Your videos land here     │ │ Send this back        │ │
│▌  │ Story model (Small)          9.0 GB   │ │ D:\Nightshift\outbox      │ │ nightshift-diagnostics│ │
│▌  │ Voices: presets and your own 4.4 GB   │ │ [Open folder]             │ │ -2026-11-02.zip       │ │
│▌  │ Captions, memory, faces      1.7 GB   │ │ ......................... │ │ is on your Desktop.   │ │
│▌  │ Nightshift Code              yes      │ │ Desktop shortcuts         │ │ Upload it to the      │ │
│▌  │ Night run                    03:00    │ │ Nightshift Dashboard      │ │ diagnostics folder on │ │
│▌  │ Everything else    D:\Nightshift      │ │ Nightshift Outbox         │ │ Google Drive.         │ │
│▌  │ · Long file paths are off             │ │ Nightshift Diagnostics    │ │ [Show file]           │ │
│▌  └───────────────────────────────────────┘ └───────────────────────────┘ └───────────────────────┘ │
│▌  Setup took 58 min                                    [Copy test report]  [Open dashboard]        │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Grid areas: `inst inst out out send send`.

| Region | Component and props |
|---|---|
| Header | `Greeting salutation="Nightshift is ready" question="" description={`Your first video is one click away. Tonight at ${runTime} the PC makes one on its own.`}`. When `scheduled` is false: "Your first video is one click away. The nightly task could not be set up, so start videos from the dashboard" |
| inst | `Card padding="lg"` with `CardHeader title="Installed"`. Rows from `result.installed[]`: label in `text-ds-sm`, detail on the right in `ds-num`. Then each warning as a `text-ds-xs text-ds-text-2` line led by `StatusDot tone="attention" size={5}` |
| out | `Card padding="lg"` with `CardHeader title="Your videos land here"`. The path in `text-ds-sm font-mono break-all`. `Button variant="soft" size="md" leadingIcon="folder"` "Open folder" (`reveal` with `result.outbox`). A `DottedRule`, then the "Desktop shortcuts" list |
| send | `Card padding="lg"` with `CardHeader title="Send this back"`. The zip name in `font-mono text-ds-sm`, then "is on your Desktop. Upload it to the diagnostics folder on Google Drive." and `Button variant="soft" size="md" leadingIcon="folder"` "Show file" (`reveal` with the zip). When `diagnosticsZip` is null, the text is instead "The diagnostics zip could not be written. Use the Nightshift Diagnostics shortcut on your Desktop.", with `Button variant="soft"` "Try again" (`diagnostics`) |
| Footer left | `Setup took {duration}` |
| Footer right | `Button variant="soft" leadingIcon="copy"` "Copy test report" (secondary) and `Button variant="primary"` "Open dashboard" (primary). Open dashboard calls `open-dashboard`, which starts the "Nightshift Dashboard" desktop shortcut (`launchers\nightshift-dashboard.ps1`) detached, then shows "Opening the dashboard" in the footer for 3 s |

The brief's "diagnostics bundle" is the `send` card. `s10_summary` writes the bundle exactly as it does
today (`ns doctor --bundle`).

After Open dashboard the window stays open. Its close button no longer asks for confirmation. Closing it
with `run = finished` calls `close-setup`, so P1 exits 0 and the console prints "Setup is done. You can
close this window." before `INSTALL.cmd`'s `pause`.

### 2.8 Failed

Shown over any phase when `failure` is set and `run` is `waiting`, or after P2 exited non-zero.

```
╭─ ☾ Nightshift setup · Stopped with an error ────────────────────────────────────────────── — ▢ ✕ ─╮
│▌  Setup stopped at Download AI models                                                               │
│▌  Everything finished so far is kept.                                                               │
│▌  ┌───────────────────────────────────────────────────────────────────┐ ┌─────────────────────────┐ │
│▌  │ ✕  NS-E109   A downloaded file is damaged                         │ │ Steps                   │ │
│▌  │                                                                   │ │ ● Free tools            │ │
│▌  │ What to do                                                        │ │ ● Install folder        │ │
│▌  │ Press Retry. Setup downloads that file again. If the same error   │ │ ● Setup environment     │ │
│▌  │ comes back, send the diagnostics zip.                             │ │ ● Questions             │ │
│▌  │                                                                   │ │ ● Download programs     │ │
│▌  │ Details                                                           │ │ ✕ Download AI models    │ │
│▌  │ qwen-talker-1.7b-base-Q8_0.gguf: expected 4b9a33a2..., got 0c1d...│ │ ○ Python environments   │ │
│▌  │ Tried 3 times                                                     │ │ ○ Tune for this card    │ │
│▌  │ [Copy diagnostics]                                                │ │ ○ ...                   │ │
│▌  └───────────────────────────────────────────────────────────────────┘ └─────────────────────────┘ │
│▌  Error at 21:52                                      [Stop setup]  [Skip this step]  [Retry]      │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Grid areas: `err err err err steps steps`.

| Region | Component and props |
|---|---|
| Header | `Greeting salutation={`Setup stopped at ${failure.stepTitle}`} question="" description="Everything finished so far is kept."` |
| err | `Card elevation={2} padding="lg"`, in four parts: <br>• Title row: `Icon name="error" size={22} className="text-ds-danger"`, then `Badge tone="danger" variant="soft" size="md"` with `failure.code`, then `failure.title` in `text-ds-xl font-semibold`. <br>• "What to do" as `CardHeader level={3}`, followed by `failure.whatToDo` in `text-ds-md`, max 60ch. <br>• "Details", followed by `failure.detail` in `text-ds-xs font-mono text-ds-text-2`. It is clamped to 6 lines, with a "Show all" ghost button. Then `Tried {attempts} times`. <br>• `Button variant="soft" leadingIcon="copy"` "Copy diagnostics" |
| steps | `Card padding="md"` with `CardHeader title="Steps"` and every step as a compact `StepItem` (32 px rows) |
| Footer left | `Error at {HH:MM}` |
| Footer right | `Button variant="ghost"` "Stop setup"; `Button variant="soft"` "Skip this step", shown only when `failure.canSkip`; `Button variant="primary" leadingIcon="retry"` "Retry" |

**Copy diagnostics** calls the `diagnostics` action:

- When the studio venv exists, P1 runs `ns doctor --bundle`.
- Otherwise it zips `<home>\logs`, `install_state.json` and the preflight JSON into
  `Desktop\nightshift-diagnostics-<date>-setup.zip` with `zipfile`.

It then copies the zip's path to the clipboard and shows the toast "Diagnostics zip saved on your
Desktop. Its name is copied".

`whatToDo` comes from §3.6. `wizard.describe_failure` can also return the codes `answers.json` and
`unexpected`. Both show:

- title: "Something unexpected stopped setup"
- what to do: "Press Retry once. If it stops again, press Copy diagnostics and send the zip"

### 2.9 Stop confirmation

`ConfirmDialog` is an installer component: a native `<dialog>` holding a
`Card elevation={3} radius="2xl" padding="lg"`, 440 px wide, with focus trapped inside and Escape to
cancel.

- Title: "Stop setup?"
- Body: "Everything finished so far is kept. Run INSTALL.cmd again to carry on from here."
- Buttons: `Button variant="soft"` "Keep going" (autofocus) and `Button variant="primary"` "Stop setup",
  which calls `cancel`.

### 2.10 Uninstall

`UNINSTALL.cmd` passes `-Ui` to `uninstall.ps1`. The script then looks for the uv Python
(`uv python find 3.12`, with uv from PATH, `%LOCALAPPDATA%\Nightshift\bootstrap\uv\` or
`<home>\bin\uv\`).

- If Python is found, the script starts `python -m installer.stage2.web --uninstall --home <home>`,
  which opens the same host window.
- Otherwise it falls back to today's console questions.

`uninstall.ps1` without `-Ui` is unchanged, so CI and Settings → Uninstall keep working.

```
╭─ ☾ Nightshift setup · Uninstall ────────────────────────────────────────────────────────── — ▢ ✕ ─╮
│▌  Remove Nightshift                                                                                 │
│▌  from this PC                                                                                      │
│▌  ┌─────────────────────────────────────────────┐ ┌──────────────────────────────────────────────┐  │
│▌  │ Always removed                              │ │ You decide                                   │  │
│▌  │ The nightly task                            │ │ [ ] Delete the AI models            15.1 GB  │  │
│▌  │ Desktop shortcuts                           │ │     Keep them to reinstall without downloading│ │
│▌  │ Programs and Python environments   2.3 GB   │ │ [ ] Delete videos and history       38.4 GB  │  │
│▌  │ The install folder setting                  │ │     Your outbox, rejected videos and history │  │
│▌  └─────────────────────────────────────────────┘ └──────────────────────────────────────────────┘  │
│▌  Kept unless ticked: D:\Nightshift\models, outbox, data             [Cancel]  [Remove Nightshift] │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Grid areas: `gone gone gone keep keep keep`.

| Region | Component and props |
|---|---|
| Rail | `StepRail` with the single step `{id: "uninstall", label: "Uninstall", icon: "trash", state: "current"}` |
| Header | `Greeting salutation="Remove Nightshift" question="from this PC"` |
| gone | `Card padding="lg"` with `CardHeader title="Always removed"` and four rows, sizes from `UninstallView.always` |
| keep | `Card padding="lg"` with `CardHeader title="You decide"` and two `ChoiceRow` checkboxes, **unchecked by default**. Each shows its size and a muted line under it |
| Footer right | `Button variant="soft"` "Cancel" (`close-setup`), then `Button variant="primary"` "Remove Nightshift". It opens a `ConfirmDialog` titled "Remove Nightshift?", whose body lists what will go, then calls `POST /api/uninstall` |

P1 runs `powershell -NoProfile -ExecutionPolicy Bypass -File installer\uninstall.ps1 -HomeDir <home>`,
adding `-DeleteModels` and `-DeleteData` when they are ticked. It sets `NS_NONINTERACTIVE=1` and binds
both switches every time (`-DeleteModels:$false` when unticked), so the script never asks. Output lines
become log lines.

The result screen reuses the Done layout:

- Salutation "Nightshift is removed".
- One card, "Kept", listing each kept folder with its path and a `reveal` button.
- The primary button "Close" (`close-setup`).

P1 runs on the uv Python, which the uninstaller never removes. `uninstall.ps1` gains one additive
change: it also removes `%LOCALAPPDATA%\Nightshift\bootstrap`, `\browser` and `\webview2`.

---

## 3. Visual rules

### 3.1 Direction (unslop)

- **Product.** Gets a partner's GPU PC ready to make story videos overnight, offline.
- **Character.** Calm, exact, a little nocturnal.
- **World.** A workshop on the night shift: a machine being readied, drive lights, the moon on the rail.
- **Moments.** Two, and only two: the Welcome feature tiles (the existing Nightshift illustrations) and
  the Verify screen's real test frame with the narrator's waveform. Everything else stays quiet.
- **Signature.** Figures in Geist Mono with tabular numbers (sizes, speeds, times, codes), and a live
  `StatusDot` only on the row that is working right now.

The unslop rules this spec applies:

- one accent, on the primary button and the selection only
- no gradients, glows or coloured shadows beyond what the ref12 tokens already define
- no emoji
- sentence case, with no uppercase tracked labels
- no exclamation marks, and no trailing periods on labels and buttons
- no greeting headline
- the 4 px grid, with equal rows sized equally
- one icon set at 18 px and stroke 1.75
- weights 400–600
- nested radii
- no looping motion
- tabular numbers in one format
- every error says what to do next

### 3.2 Tokens and depth

The installer uses the dashboard tokens exactly and adds **no colour values**.
`installer/ui/src/main.css`:

```css
@import "tailwindcss";
@import "@dash/styles.css";                               /* --ds-* tokens, Tailwind theme, .sidetab rail CSS */
@source "../../../ns_dashboard/ui/src/components/ref12";
@source "../../../ns_dashboard/ui/src/shell";
@source "./";
```

`index.html` sets `<html data-theme="dark">`, and the theme control in Choices flips it.

Depth layers from ref12-map §5, applied to the installer:

| z | Layer | Installer element |
|---|---|---|
| 0 | canvas | `SetupBackdrop`: `<img src="./canvas-swirl.jpg" alt="">` with `object-fit: cover`, under a `bg-ds-canvas` veil at 78% opacity (`opacity-[.78]`). The swirl shows only in the 16 px margin around the frame and at its rounded corners, as a dim texture. It never sits behind content. If the image fails to load, `PageBackdrop` from ref12 is used |
| 1 | `AppWindow` | the frame: `bg-ds-app`, radius `2xl` (32), `shadow-ds-frame` |
| 2 | `StepRail` | the SideTab wave (`--rail-bg`, `--rail-shadow`) |
| 3 | `Card elevation={1}` | every panel. Sunken tiles use `tone="sunken" elevation={0}` |
| 4 | `Card elevation={2}` | `IssueCard` and the Failed error card |
| 5 | `Card elevation={3}` | the Advanced drawer, the More options popover, `ConfirmDialog` |
| 6 | accent glow | the active `SegmentedControl` segment and the primary button, nothing else |

**Image licence.** `D:\NightShift-refs\background-swirl.jpg` (390 KB, shipped as is) carries a faint
stock-site watermark in its centre. The owner must confirm the licence, or supply a clean file, before
it goes into the public repo as `installer/ui/public/canvas-swirl.jpg`. Until then, `SetupBackdrop`
renders `PageBackdrop`, which is the same fallback path, and the image stays out of the commit.

### 3.3 Grid, type and spacing

- **Grid.** 6 columns with 20 px gaps (`gap-5`). Content is 1166 px wide in a 1366 px window. A
  container query, as in `.ds-dash`, switches to 4 columns between 600 and 1019 px of content width
  (windows 1024–1180 px wide). There, `a a a a b b` becomes `a a a a` above `b b b b`, and the body
  scrolls. The window minimum is 1024 × 640. Below 700 px of height, the body scrolls and the footer
  sticks to the bottom.
- **Type.** ref12 tokens only:

  | Element | Size and weight |
  |---|---|
  | Screen title | `Greeting size="lg"`, 32 / 600 |
  | Card title | `xl` 20 / 600 |
  | Section heading | `lg` 16 / 600 |
  | Body | `md` 14 |
  | Rows | `sm` 13 |
  | Meta | `xs` 12 |
  | Captions | `2xs` 11 |

  Weights are 400, 500 and 600. 700 appears only where ref12 components already use it (ring values).
  Figures use `ds-num` (tabular). File names, paths, codes and times use `font-mono` (Geist Mono).
- **Spacing.** Everything sits on the 4 px grid. Content cards get 24 px padding (`lg`) and list cards
  16/20 (`md`). Rows are 4 px apart, sections 20 px, footer buttons 12 px.
- **Radii.** They nest: `AppWindow` 32, cards 24, rows and tiles 16, controls 10, badges 6.
- **Icons.** 18 px, stroke 1.75, in `text-ds-text-2`, from the extended ref12 registry only.

### 3.4 Copy rules

- Write plain words for a non-technical partner. Sentences say "graphics card", not GPU; the meter
  label may say VRAM, because that is what the card's box says. Say "install folder", not
  NIGHTSHIFT_HOME.
- Every error and warning has a next step the partner can take, taken from OPERATOR.md or §3.6. A code
  never appears on its own.
- Use sentence case everywhere and no uppercase eyebrows. That rules out `StatCard`, whose eyebrow is
  uppercase.
- No exclamation marks.
- No greeting headline. The `Greeting` component's `salutation` names the screen.
- Labels, buttons and captions have no trailing period. Paragraphs of several sentences keep theirs.
- Numbers:
  - sizes in GB with one decimal (`9.0 GB`)
  - speeds in MB/s with one decimal
  - time left as `10 min left`
  - clock times as `HH:MM`, 24-hour
- Buttons name their action: "Start setup", "Install", "Retry", "Open dashboard". Never "OK" or "Next".

### 3.5 Motion and states

- **Entrance.** Each screen has one: the body fades in and rises 4 px over 200 ms (`ease-ds-out`), with
  a 30 ms stagger per grid area. Screens never slide sideways.
- **Progress.** Bars tween `transform`, as `ProgressBar` already does. `VerticalMeter` fills tween
  `transform: scaleY` (§5.2).
- **No loops.** Nothing loops anywhere. The live `StatusDot` is static: its colour says "live", not a
  pulse.
- **Reduced motion.** `prefers-reduced-motion` and the `.ds-root` rules in `styles.css` turn
  transitions off.
- **Focus and keyboard.** Every control shows the `ds-focus` ring. Tab order follows reading order,
  with the footer last. Enter triggers a screen's primary button, unless focus is in a text field.
- **Screen readers.** One `aria-live="polite"` region announces phase changes and failures. Download
  progress is announced at most every 30 s, for example "23 percent, 10 minutes left".
- **Connection banner.** After 6 s without an answer from the state endpoint, a
  `Card elevation={3}` strip appears under the title bar: "Lost contact with setup. If the black setup
  window is closed, run INSTALL.cmd again". It hides on the next answer.

### 3.6 Plain text for every code the installer can show

`installer/stage2/web/operator_copy.json` maps each code to `{title, whatToDo}`. P1 uses it to fill
`CheckRow`, `Notice` and `FailureView`, falling back to the errors.json `message` and `hint`. The rows
that exist in OPERATOR.md follow its wording. Where OPERATOR.md says "run INSTALL.cmd again", the
in-window text says "press Retry", because the window replaces the rerun.

| Code | Title | What to do |
|---|---|---|
| NS-E101 | This PC cannot run Nightshift | Nightshift needs 64-bit Windows 10 or 11. Tell the developer |
| NS-E102 | No NVIDIA graphics card found | Install the NVIDIA driver from nvidia.com/drivers, restart, then press Check again |
| NS-E103 | NVIDIA driver too old | Install the newest driver from the page that opened, restart, then press Check again |
| NS-E104 | Not enough free disk space | Free 60 GB, or pick another drive |
| NS-E105 | A download site cannot be reached | Check the internet, switch off any VPN or proxy, then press Retry |
| NS-E108 | The graphics card libraries did not load | Press Copy diagnostics and send the zip. Nothing else to do |
| NS-E109 | A downloaded file is damaged | Press Retry. Setup downloads that file again. If the same error comes back, send the diagnostics zip |
| NS-E110 | A free tool did not install | Install "App Installer" from the Microsoft Store, then press Retry |
| NS-E111 | App Installer is missing | Install "App Installer" from the Microsoft Store, then press Check again |
| NS-E112 | Caption timing cannot use the graphics card | Press Copy diagnostics and send the zip. Nothing else to do |
| NS-E113 | Windows Security blocked a file | Open Windows Security, then Protection history, allow the Nightshift file and press Retry. Adding the install folder as an exclusion stops it happening again (new code, CR-C1-UI-2) |
| NS-E202 | A Nightshift service did not start | Press Retry once. If it stops again, send the diagnostics zip |
| NS-E203 | A model took too long to start | Press Retry once. If it stops again, send the diagnostics zip |
| NS-E204 | The nightly task could not be set up | Setup still works. Start videos from the dashboard and send the diagnostics zip |
| NS-E501 | The Pexels key was not accepted | Copy the key again from the website (no spaces) and paste it |
| NS-E502 | The Pixabay key was not accepted | Copy the key again from the website (no spaces) and paste it |
| NS-E503 | The internet dropped | Check the connection, then press Retry |
| NS-W106 | The install drive is a hard disk | Models load slowly from a hard disk. Pick an SSD drive if you have one |
| NS-W107 | The PC cannot wake itself at night | Turn on "Allow wake timers" in Power Options, under Sleep |
| NS-W112 | Long file paths are off | Setup works. Press Fix it to turn them on (Windows asks for permission) |
| NS-W301 | Using the backup voice engine | Voices sound the same and take a little longer. Nothing to do |
| NS-W302 | Faster decoding was not faster here | Nothing to do |
| NS-W401 | Using the slower video encoder | Update the NVIDIA driver when you can. Videos still work |
| NS-W403 | The setup test video could not be made | Setup is complete. Make your first video from the dashboard. If that also fails, send the diagnostics zip (new code, CR-C1-UI-2) |

---

## 4. Edge cases

| Case | What happens |
|---|---|
| No GPU | Stage1's first line is `[warn]`. On the Hardware check, the gpu row fails with `NS-E102` (blocking) and the driver row fails. With `allow_cpu: true` in answers.json both become warnings, the plan uses the `cpu-dev` profile, and the Welcome plan shows the 2.5 GB dev set |
| Wrong GPU (not RTX, or under 16 GB) | Under 15 000 MB of VRAM: the warning row "The graphics card has less than 16 GB", the VRAM meter in `highlight` tone, and Balanced and Best disabled with the reason "Needs a 16 GB graphics card". CUDA under 12.4 (an old card or old driver) blocks with `NS-E103` |
| Low disk | Under 40 GB on the default drive, `NS-E104` blocks, and the card offers the drive picker. Choices checks the folder against `required_free_gb(tier)` as it changes, and Install stays disabled until it fits. During downloads, P1 checks free space every 30 s. Under 2 GB free, it pauses all transfers and raises `NS-E104` as a failure with "Free some space, then press Retry" |
| Offline with models_source | Hugging Face unreachable but GitHub and PyPI reachable: a warning, and the source in Choices defaults to "A folder on this PC". Files missing from the folder still need the internet. Their rows read "not in your folder, downloading", and if that fails they get `NS-E105` with "Put this file in your models folder, or connect to the internet, then press Retry". Fully offline (GitHub or PyPI unreachable) blocks, because Python packages and programs cannot come from a folder in this version |
| Partial download resume | `.part` files and `.ok` markers survive. On restart, the Welcome resume variant shows "Still to do" with the bytes left, and rows read "will continue from 2.1 GB". HTTP `Range` resume is the downloader's existing behaviour. A server that ignores `Range` restarts the file (existing code), and the row says "restarted, the server did not allow resume" once |
| Admin rights | A normal run may get a UAC prompt for each winget tool, so those rows read "installing, Windows may ask for permission". Long paths: `elevate-longpaths` runs `Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-Command','Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem LongPathsEnabled 1 -Type DWord'`. If the partner declines UAC, the card reads "Windows did not give permission. Setup works without it". If the partner ran setup as administrator, it still works, and Done adds the note "Setup ran as administrator. Shortcuts were made for {USERNAME}" |
| Antivirus blocking | The signs: a zip item's `expect` file missing right after extraction; `PermissionError`, or `OSError` with winerror 225, when starting a downloaded `.exe`; uv or Python disappearing between runs. P1 and P2 map all of these to `NS-E113`, with the file path as the detail. Retry extracts again. If the WebView2 host itself is blocked, the fallback chain opens the browser |
| Browser or window not opening | The fallback order is in §1.9. In every case the console prints the address (§1.2) and `setup-address.txt` holds it. If every launcher fails, stage1 shows `[warn] open the address below in Edge or Chrome`, and setup waits on the Welcome screen |
| Window closed mid-install | The install continues, since the console owns P1 and P2. `INSTALL.cmd` reopens the window (§1.9, step 4), and `/api/state` puts the UI back on the current screen |
| Console closed mid-install | P1 and P2 end. `install_state.json` keeps the running step as `pending`, and the next `INSTALL.cmd` resumes. This is why the Download screen says to keep the black setup window open |
| Two INSTALL.cmd at once | The second one finds a live `setup-server.json` and only reopens the window |
| Sleep or reboot during download | Connections drop, and the downloader retries with `Range` (counted attempts). After a reboot, `INSTALL.cmd` resumes |
| Keys rejected | Shown inline on Choices, and Install stays disabled until both keys work. A rate-limited key (HTTP 429) counts as working, as in `s01_questions` |
| answers.json invalid | Headless: exits with the field error, as today. With `-Ui`: Choices opens with the bad fields marked and the footer line "answers.json had problems. Fix them here" |
| Port in use or firewall | Binding to port 0 always gets a free port. Windows Firewall does not filter loopback traffic |
| DPI scaling 125–150% | WebView2 and Chromium scale CSS pixels. The host sizes its window in device-independent pixels (WPF), so 1366 × 768 becomes 1708 × 960 physical pixels at 125%. When that is larger than the work area, the host starts maximized |

---

## 5. Build plan

### 5.1 Branch order (scope guard, `dev/check_scope.py`)

1. `orchestrator/installer-ui-contracts`: the contract changes in §5.5. They touch the answers schema
   and model, errors.json and the manifest's `bins.webview2`.
2. `feat/c9-ref12-installer-widgets`: §5.2 and §5.3 in `ns_dashboard/ui`, covering the ported widgets,
   Icon names, `TitleBar`, `StepRail`, the SideTab export and the token aliases. Rebuild
   `ns_dashboard/static`.
3. `feat/c1-installer-ui`: everything under `installer/`, plus `launchers/nightshift-host.ps1`,
   `INSTALL.cmd`, `UNINSTALL.cmd`, the tests, the CI change and the bundle change.

### 5.2 Ported planify widgets (into `ns_dashboard/ui/src/components/ref12/`)

Port these from `D:\NightShift-tools\planify-ui\src\components\widgets\`:

- Rewrite the imports to `./Card`, `./Icon` and `../../ui/kit` (for `cx`).
- Convert each `.css` file either to Tailwind `ds-*` utilities inside the TSX, or to a co-located
  `.css` file imported by the TSX that reads only `--ds-*` variables.
- Export them from `index.ts` under `// widgets`.

| File | Exports | Changes from planify |
|---|---|---|
| `Meters.tsx` | `VerticalMeter`, `MeterGroup`, `MeterDatum` | `tone` becomes `"accent" \| "highlight" \| "danger"`, with `danger` mapped to the existing danger fill (`bg-ds-danger`). The fill animates `transform: scaleY(pct / 100)` with `transform-origin: bottom`, not `height` |
| `Readouts.tsx` | `TimeChip`, `CountdownCard` (not `WeatherCard` or `DayChips`) | `CountdownCard` gains `units?: ("d" \| "h" \| "m")[]`, default `["d", "h", "m"]`. `pad` and `useNow` move to `ref12/time.ts` |
| `Controls.tsx` | `IntakeStepper` (not `AppTile`, `KnobSwitch` or `Smiley`) | Adds `readOnly?: boolean`: no −/+ buttons, and `onChange` becomes optional. Adds `vessel?: "cup" \| "drive"`, default `"cup"`. `"drive"` draws a drive bay in the same 64-unit viewBox: the outline and clip path `M8 14h48a4 4 0 0 1 4 4v28a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4V18a4 4 0 0 1 4-4Z`, plus two 3 px LED dots at (48, 42) and (54, 42) in `--ds-text-muted`. The fill rises from y 50 to y 14 |
| `Media.tsx` | `Waveform`, `sampleWaveform` (not `TransportControls` or `MusicPlayer`) | none |

Token aliases, appended to both the `:root, [data-theme="light"]` block and the `[data-theme="dark"]`
block in `styles.css`. Each one points at an existing token, so no colour value is new:

```css
--ds-bg-selected: var(--ds-bg-accent);
--ds-text-on-selected: var(--ds-text-on-accent);
--ds-bg-highlight: var(--ds-bg-accent-soft);
--ds-shadow-inset: none;
--ds-shadow-inset-sm: none;
--ds-shadow-control: var(--ds-shadow-1);
--ds-shadow-selected: var(--ds-shadow-accent);
--ds-weight-medium: 500;
--ds-weight-semibold: 600;
--ds-font-display: var(--ds-font-sans);
```

### 5.3 Other ref12 additions

**Icon registry.** `Icon.tsx` adds these names (lucide-react 0.460):

| Name | lucide icon | Name | lucide icon |
|---|---|---|---|
| `check` | `Check` | `memory` | `MemoryStick` |
| `warning` | `TriangleAlert` | `disk` | `HardDrive` |
| `error` | `CircleX` | `network` | `Wifi` |
| `pause` | `Pause` | `offline` | `WifiOff` |
| `play` | `Play` | `shield` | `ShieldAlert` |
| `retry` | `RotateCcw` | `package` | `Package` |
| `folder` | `FolderOpen` | `minus`, `minimize` | `Minus` |
| `copy` | `Copy` | `maximize` | `Square` |
| `cpu` | `Cpu` | `restore` | `Copy` |
| `close` | `X` | `external` | `ExternalLink` |
| `terminal` | `SquareTerminal` | | |

**`StepRail.tsx` (new).** Props:

```ts
{
  steps: {
    id: string;
    label: string;
    icon: IconName;
    state: "done" | "current" | "upcoming" | "failed";
    onSelect?: () => void;
  }[];
  label: string;
  className?: string;
}
```

- It renders the SideTab markup and classes: `sidetab`, `sidetab-shape`, `sidetab-fill`,
  `sidetab-list`, `sidetab-items`, `sidetab-item` (with `is-on` for `current`), `sidetab-tip`, and
  `sidetab-dot` (with `data-tone="success"` for `done` and `data-tone="attention"` for `failed`).
- It sets the `--cap-start`, `--cap-end`, `--h`, `--gap` and `--i` variables.
- It imports `CAP_PATH`, `RAIL_W` and `layoutFor` from `shell/SideTab.tsx`. `useRailLayout` gets
  exported there, which is an additive change.
- A step with `onSelect` renders as a `<button>`. The current step renders as
  `<span aria-current="step">`.
- The brand slot shows `Mark size={28} tone="white"`. It has no theme or quit tools.

**`TitleBar.tsx` (new).** Specified in §6.4.

### 5.4 Installer files

```
installer/ui/
  package.json            name "nightshift-setup-ui"; the same versions as ns_dashboard/ui for react, react-dom,
                          lucide-react, @fontsource-variable/geist, @fontsource-variable/geist-mono, vite,
                          @vitejs/plugin-react, tailwindcss, @tailwindcss/vite, typescript, @types/react, @types/react-dom
  package-lock.json
  index.html              <html lang="en" data-theme="dark">, <div id="root">, <script type="module" src="/src/main.tsx">
  vite.config.ts          below
  tsconfig.json           as ns_dashboard/ui, plus "baseUrl", "paths" (below) and "include": ["src",
                          "../../ns_dashboard/ui/src/components/ref12", "../../ns_dashboard/ui/src/ui",
                          "../../ns_dashboard/ui/src/shell/SideTab.tsx"]
  public/canvas-swirl.jpg only after the licence check (§3.2)
  src/main.tsx            fonts, main.css, <App/>
  src/main.css            §3.2
  src/api/types.ts        §5.6, exactly
  src/api/client.ts       getState(since), uiStep, postAnswers, action(name, body), checkKey, disk; long-poll loop with backoff
  src/api/useSetup.ts     React hook: state, connection status, in-flight flags
  src/host.ts             WebView2 bridge (§6.3)
  src/App.tsx             shell (§2.0) and the screen switch on (phase, run, failure, uiStep)
  src/screens/            Welcome.tsx, Checks.tsx, Choices.tsx, Download.tsx, Installing.tsx, Verify.tsx, Done.tsx,
                          Failed.tsx, Uninstall.tsx
  src/parts/              SetupBackdrop.tsx, CheckRowItem.tsx, VerifyRowItem.tsx, IssueCard.tsx, DownloadRow.tsx,
                          StepItem.tsx, ChoiceRow.tsx, ConfirmDialog.tsx, Footer.tsx, Toast.tsx
  src/copy.ts             every visible string in this spec, by screen
  src/format.ts           gb(), mbps(), minutesLeft(), hhmm(), duration()
  static/                 build output, committed (index.html, app.js, app.css, assets/*)
installer/stage2/web/
  __init__.py
  __main__.py             argparse: --app-path --preflight --answers --headless --no-window --uninstall --home
                          --simulate --fixture --port; exit codes as in §1.3
  server.py               ThreadingHTTPServer, Handler, routing, token and Host checks, static files, long poll
  store.py                SetupStore: the SetupState dict, rev counter, threading.Condition, snapshot()
  machine.py              phase derivation, allowed actions, transitions (pure functions, unit-tested)
  model.py                TypedDicts mirroring §5.6
  bootstrap.py            b01_tools, b02_home, b03_setup_env (stdlib)
  engine.py               EngineProcess: spawn P2, read events, write commands, map events into the store
  preflight.py            load the hand-over JSON, rows to CheckRow, re-probe through stage1 -ProbeOnly, disk check
  plan.py                 manifest.json to InstallPlan per tier (a stdlib mirror of downloader.plan_models + plan_bins)
  answers_view.py         ChoicesDraft validation, AnswersView, answers.json read and write (camelCase to snake_case)
  keys.py                 urllib checks for Pexels and Pixabay
  samples.py              helpers for v01_samples: WAV bars, scratch-home seeding, ffmpeg frame grab
  launch.py               the window launch chain (§1.9), setup-server.json, setup-address.txt
  uninstall.py            UninstallView, running uninstall.ps1
  labels.py               friendly labels (§2.4) and check labels (§2.6)
  operator_copy.json      §3.6
  fakes.py                FakeRunner for bootstrap and engine tests
  fixtures/state-*.json   one SetupState per screen, for review (§5.7)
installer/stage2/bridge.py              BridgeUi, BridgeReporter, SetupStopped (runs in P2; may import rich and ns_common)
installer/stage2/steps/v01_samples.py   the samples step (§2.6), optional
launchers/nightshift-host.ps1           §6
```

`vite.config.ts`:

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

const dash = fileURLToPath(new URL("../../ns_dashboard/ui/src", import.meta.url));
const nm = (p: string) => fileURLToPath(new URL(`./node_modules/${p}`, import.meta.url));

// Builds into ./static (served by installer/stage2/web/server.py). Fixed names (app.js, app.css), no inline
// scripts (CSP script-src 'self').
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "./",
  resolve: {
    alias: {
      "@ref12": `${dash}/components/ref12`,
      "@dash": dash,
      // The shared components live outside this project: force their bare imports to this project's node_modules.
      react: nm("react"),
      "react-dom": nm("react-dom"),
      "lucide-react": nm("lucide-react"),
    },
    dedupe: ["react", "react-dom", "lucide-react"],
  },
  server: { port: 5179, fs: { allow: [".", dash] }, proxy: { "/api": "http://127.0.0.1:8765" } },
  build: {
    outDir: "static",
    emptyOutDir: true,
    assetsDir: "assets",
    modulePreload: { polyfill: false },
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: (i) => ((i.names?.[0] ?? i.name ?? "").endsWith(".css") ? "app.css" : "assets/[name]-[hash][extname]"),
      },
    },
  },
});
```

`tsconfig.json` adds `"baseUrl": "."` and these `paths`:

```json
{
  "@ref12": ["../../ns_dashboard/ui/src/components/ref12/index.ts"],
  "@ref12/*": ["../../ns_dashboard/ui/src/components/ref12/*"],
  "@dash/*": ["../../ns_dashboard/ui/src/*"],
  "react": ["./node_modules/@types/react"],
  "lucide-react": ["./node_modules/lucide-react"]
}
```

For local UI work, run P1 as `python -m installer.stage2.web --port 8765 --no-window --simulate`, add
`--fixture <name>` to freeze a screen, and run `npm run dev` in `installer/ui`.

**CI.** In `.github/workflows/ci.yml`, the `ui-build` job lists both lockfiles in
`cache-dependency-path` and gains these steps after the dashboard ones:

```yaml
      - name: npm ci (installer UI)
        working-directory: installer/ui
        run: npm ci --no-audit --no-fund
      - name: npm run build (installer UI)
        working-directory: installer/ui
        run: npm run build
      - name: Committed installer build equals the sources
        shell: bash
        run: |
          changes="$(git status --porcelain installer/ui/static)"
          if [ -n "$changes" ]; then
            echo "::error::installer/ui/static differs from a fresh build of installer/ui; run npm ci and npm run build in installer/ui and commit the result"
            echo "$changes"
            exit 1
          fi
```

`powershell-parse` already parses every tracked `.ps1`, so it covers `nightshift-host.ps1`. Add one
step to it: `powershell -NoProfile -STA -ExecutionPolicy Bypass -File launchers/nightshift-host.ps1 -CheckOnly`,
which must print one JSON line and exit 0.

**Bundle (`dev/make_bundle.ps1`).** The zip is a `git archive`, so the committed `installer/ui/static/`
is included automatically.

- The verifier requires `installer/ui/static/index.html` and `installer/ui/static/app.js` in the zip,
  and rejects `installer/ui/node_modules/`.
- New parameter `-WebView2Dir <dir>`, passed by the release job. It copies exactly three files into
  `bin/webview2/` in the zip: `Microsoft.Web.WebView2.Core.dll`, `Microsoft.Web.WebView2.Wpf.dll` and
  `WebView2Loader.dll`. Each SHA256 is first checked against `manifest.json` `bins.webview2.files`.
- The "no binaries" check allowlists exactly those three paths. They add about 1 MB, so the 25 MB
  limit still holds.

`release.yml`:

1. Downloads the pinned NuGet package from
   `https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/<version>`.
2. Checks its SHA256.
3. Unzips `lib/net462/Microsoft.Web.WebView2.Core.dll`, `lib/net462/Microsoft.Web.WebView2.Wpf.dll` and
   `runtimes/win-x64/native/WebView2Loader.dll` into a temp folder.
4. Passes that folder as `-WebView2Dir`.

Dev and CI zips built without `-WebView2Dir` still work, because the window falls back to the browser.

### 5.5 Contract changes (orchestrator branch, recorded in `contracts/CHANGE_REQUESTS.md`)

- **CR-C1-UI-1, answers.**
  - `contracts/schemas/answers.schema.json` and `ns_common.models.Answers` gain:
    - `owner_name: str | None = None` (max 40)
    - `ui_theme: Literal["dark", "light"] = "dark"`
    - `models_source_kind: Literal["internet", "folder", "drive-zip"] = "internet"`
  - `installer/answers.example.json` gains
    `"owner_name": null, "ui_theme": "dark", "models_source_kind": "internet"`.
  - `s07_configs` writes `owner_name` and `ui_theme` to wherever C9's dashboard reads the greeting
    name and the default theme. That is the `ui` block of `config\runtime.json`, added in the same CR
    if it does not exist yet.
- **CR-C1-UI-2, errors.json.** Adds two codes:
  - `NS-E113`. Message: "A file was blocked or removed by antivirus software." Hint: "Allow the file
    in Windows Security, Protection history, or add the install folder as an exclusion, then re-run
    setup."
  - `NS-W403`. Message: "The setup test video could not be made." Hint: "Setup is complete; make the
    first video from the dashboard and send the diagnostics if it fails."
- **CR-C1-UI-3, manifest.**
  - Adds `bins.webview2 = {version, url, sha256, files}`. `url` points at the nupkg, and `files` maps
    `Microsoft.Web.WebView2.Core.dll`, `Microsoft.Web.WebView2.Wpf.dll` and `WebView2Loader.dll` to
    their SHA256s.
  - `tools/resolve_manifest.py` resolves it.
  - Pin a 1.0.x release that ships `lib/net462`, because Windows PowerShell 5.1 runs on .NET Framework
    4.8.
- **install_state.json.** New step ids `b01_tools`, `b02_home`, `b03_setup_env` and `v01_samples`. The
  model already accepts any step id. List them in CONTRACTS.md.

### 5.6 Data shapes

The TypeScript lives in `installer/ui/src/api/types.ts`, mirrored by TypedDicts in
`installer/stage2/web/model.py`. HTTP JSON keys are camelCase. `answers.json` stays snake_case, and
`answers_view.py` maps between the two.

```ts
export type Phase = "checks" | "choices" | "downloading" | "installing" | "verifying" | "done" | "failed";
export type RunState = "idle" | "running" | "pausing" | "paused" | "waiting" | "stopping" | "stopped" | "finished";
export type Tier = "small" | "balanced" | "best";
export type PresetVoice = "Ryan" | "Aiden" | "Serena" | "Vivian";
export type UiStep = "welcome" | "checks" | "choices";
export type StepId =
  | "b01_tools" | "b02_home" | "b03_setup_env"
  | "s01_questions" | "s02_download_bins" | "s03_download_models" | "s04_venvs" | "s05_opencode_prewarm"
  | "s06_tune" | "s07_configs" | "s08_smoke" | "v01_samples" | "s09_shortcuts_scheduler" | "s10_summary";
export type CheckId = "os" | "gpu" | "driver" | "ram" | "disk" | "disktype" | "network" | "winget" | "longpaths" | "waketimers";

export interface SetupState {
  schema: 1;
  rev: number;                       // +1 on every change; the long-poll cursor
  version: string;                   // VERSION file
  appPath: string;
  phase: Phase;
  run: RunState;
  uiStep: UiStep | null;             // pre-start screen; null after start
  resumed: boolean;
  prefilledFrom: string | null;      // answers.json path when -Ui loaded one
  startedAt: string | null;          // ISO 8601, local time
  updatedAt: string;
  host: "webview2" | "browser-app" | "browser" | "none";
  machine: MachineSummary;
  checks: CheckRow[];
  checkedAt: string | null;
  checking: boolean;
  blocking: boolean;                 // any CheckRow.blocking
  plan: InstallPlan;
  answers: AnswersView | null;       // null until accepted (or loaded on resume)
  answersLocked: boolean;            // true once s01_questions is done
  steps: StepView[];                 // every StepId, in run order
  downloads: DownloadsView;
  verify: VerifyView | null;
  failure: FailureView | null;
  notices: Notice[];                 // NS-W* raised so far
  result: ResultView | null;         // set at done
  log: LogLine[];                    // newest last, at most 50
}

export interface MachineSummary {
  windows: string;                   // "Windows 11 Pro 10.0.26200"
  gpuName: string | null;
  vramMb: number | null;
  driver: string | null;
  cuda: string | null;               // "13.0"
  ramGb: number;
  profile: "gpu" | "cpu-dev";
  isAdmin: boolean;
  drives: DriveView[];
  meters: { vram: MeterValue; ram: MeterValue; disk: MeterValue & { path: string } };
}
export interface DriveView { letter: string; freeGb: number; type: "SSD" | "HDD" | "Unknown" }
export interface MeterValue { haveGb: number; needGb: number; hardMinGb: number }

export type FixAction =
  | { kind: "open-url"; label: string; url: string }
  | { kind: "elevate-longpaths"; label: string }
  | { kind: "choose-drive"; label: string }
  | { kind: "recheck"; label: string };

export interface CheckRow {
  id: CheckId;
  label: string;                     // "NVIDIA driver"
  status: "ok" | "warn" | "fail";
  blocking: boolean;
  value: string;                     // "CUDA 13.0, driver 581.57"
  code: string | null;               // "NS-E103"
  title: string | null;              // plain title (§3.6)
  whatToDo: string | null;
  fix: FixAction | null;
}

export interface PlanItem { key: string; label: string; file: string | null; bytes: number | null; via: "download" | "winget" | "uv" }
export interface PlanGroup { id: "tools" | "python" | "programs" | "models"; label: string; bytes: number | null; items: PlanItem[] }
export interface TierInfo { llmBytes: number; totalBytes: number; needFreeGb: number; needsRamGb: number; allowed: boolean; reason: string | null }
export interface InstallPlan {
  tier: Tier;
  profile: "gpu" | "cpu-dev";
  groups: PlanGroup[];
  totalBytes: number;                // known sizes only
  unknownCount: number;              // items whose size is learned when the download starts
  needFreeGb: number;
  estimate: { downloadMin: number; installMin: number; label: string };   // label: "About 1 hour"
  tiers: Record<Tier, TierInfo>;
}

export interface ChoicesDraft {
  ownerName: string | null;
  channel: { displayName: string; niche: string; language: string };   // language defaults to "en"
  homePath: string;
  llmTier: Tier;
  voice: { speaker: PresetVoice };
  modelsSource: { kind: "internet" | "folder" | "drive-zip"; path: string | null };
  runTime: string;                   // "03:00"
  mastersPerWeek: 1 | 2 | 3;
  installCode: boolean;
  theme: "dark" | "light";
  pexelsKey: string | null;          // null keeps the stored key (resume)
  pixabayKey: string | null;
}
export interface AnswersView extends Omit<ChoicesDraft, "pexelsKey" | "pixabayKey"> {
  pexelsKeySet: boolean;
  pixabayKeySet: boolean;
}
export type ChoicesField =
  | "ownerName" | "channel.displayName" | "channel.niche" | "channel.language" | "homePath" | "llmTier"
  | "voice.speaker" | "modelsSource.path" | "runTime" | "mastersPerWeek" | "pexelsKey" | "pixabayKey";
export interface FieldError { field: ChoicesField; message: string; code: string | null }
export type AnswersResult = { ok: true; answers: AnswersView; plan: InstallPlan } | { ok: false; errors: FieldError[] };

export interface KeyCheck { ok: boolean; code: "NS-E501" | "NS-E502" | "NS-E503" | null; message: string | null }
export interface DiskCheck {
  path: string;
  exists: boolean;
  drive: string | null;
  freeGb: number | null;
  type: "SSD" | "HDD" | "Unknown";
  needGb: number;
  ok: boolean;
  message: string | null;
  modelsFound: { files: number; of: number; bytes: number } | null;   // with source=1 only
}

export interface StepView {
  id: StepId;
  title: string;                     // plain: "Download AI models"
  phase: "downloading" | "installing" | "verifying";
  status: "pending" | "running" | "done" | "skipped" | "failed" | "warning";
  detail: string | null;             // latest say() while running, the skip reason, or the error
  startedAt: string | null;
  finishedAt: string | null;
}

export interface DownloadRow {
  key: string;                       // plan key, unique
  label: string;
  file: string | null;
  group: PlanGroup["id"];
  via: "download" | "copy" | "winget" | "uv" | "present";
  bytesTotal: number | null;
  bytesDone: number;
  status: "queued" | "active" | "copying" | "verifying" | "extracting" | "paused" | "done" | "skipped" | "failed";
  speedBps: number | null;
  etaS: number | null;
  note: string | null;               // "will continue from 2.1 GB", "not in your folder, downloading"
  error: { code: string; message: string } | null;
}
export interface DownloadsView {
  items: DownloadRow[];
  overall: { bytesTotal: number; bytesDone: number; filesTotal: number; filesDone: number; speedBps: number | null; etaS: number | null };
  source: "internet" | "folder" | "drive-zip";
  paused: boolean;
}

export interface VerifyRow { name: string; label: string; status: "pending" | "running" | "passed" | "failed" | "fallback"; value: string | null; note: string | null }
export interface VoiceSample { url: string; speaker: PresetVoice; seconds: number; text: string; bars: number[] }
export interface TestRender { videoUrl: string; thumbUrl: string; seconds: number; width: number; height: number; encoder: "h264_nvenc" | "libx264" }
export interface VerifyView { checks: VerifyRow[]; voice: VoiceSample | null; render: TestRender | null; renderNote: string | null; reportReady: boolean }

export interface FailureView {
  step: StepId;
  stepTitle: string;
  code: string;
  title: string;
  whatToDo: string;
  detail: string;
  canSkip: boolean;
  attempts: number;
  at: string;
}
export interface Notice { code: string; title: string; whatToDo: string; step: StepId | null }
export interface ResultView {
  home: string;
  outbox: string;
  appPath: string;
  installed: { label: string; detail: string }[];
  shortcuts: string[];               // "Nightshift Dashboard", ...
  runTime: string;
  scheduled: boolean;
  diagnosticsZip: string | null;
  durationS: number;
  warnings: Notice[];
}
export interface LogLine { t: string; level: "info" | "warn" | "error"; text: string }

export type ActionName =
  | "start" | "pause" | "resume" | "retry" | "skip" | "cancel" | "recheck" | "open-dashboard"
  | "diagnostics" | "reveal" | "elevate-longpaths" | "close-setup";
export interface ActionBody { path?: string }
export interface ActionResult { ok: boolean; message: string | null; state: SetupState }

export interface UninstallView {
  home: string | null;
  always: { label: string; bytes: number | null }[];
  models: { path: string; bytes: number };
  data: { path: string; bytes: number };
  running: boolean;
  result: { removed: string[]; kept: { label: string; path: string }[] } | null;
}
```

Action preconditions. When one is not met, P1 answers HTTP 409 with `{ok: false, message}`:

| Action | Allowed when |
|---|---|
| `start` | answers accepted, `!blocking`, and `run` is `idle` or `stopped` |
| `pause` | `run = running` and phase is `downloading` |
| `resume` | `run` is `paused` or `stopped` |
| `retry` | `run = waiting` |
| `skip` | `run = waiting` and `failure.canSkip` |
| `cancel` | `run` is `running`, `paused` or `waiting`. It moves `run` to `stopping` |
| `recheck` | `run` is `idle` or `stopped`, and `checking` is false |
| `open-dashboard` | `phase = done` |
| `reveal` | `path` is one of `result.outbox`, `result.home`, `result.diagnosticsZip`, the logs folder, or a kept path in `UninstallView` |
| `elevate-longpaths` | the `longpaths` row is `warn` |
| `diagnostics` | always |
| `close-setup` | `run` is `idle`, `stopped` or `finished` |

### 5.7 Tests (CI, fakes, no downloads)

Python tests live in `tests/` and run in the `test` job:

| File | Covers |
|---|---|
| `test_installer_web_machine.py` | Phase derivation for every step id and `uiStep`. The action preconditions table. `run` transitions, including stop then resume, and failure then retry or skip |
| `test_installer_web_server.py` | Starts the server on port 0 with `--simulate --no-window` in a thread. Checks: 401 without the cookie; 421 with a wrong Host; the token cookie flow; the long poll returning on a `rev` change and after its timeout; static `index.html` served with the CSP; the `/api/file` allowlist; the 64 KB body limit |
| `test_installer_web_answers.py` | Every validation rule in §2.3. The camelCase/snake_case round trip against `installer/answers.example.json`. Keys never written to disk. `ns_common.models.Answers` accepts what P1 writes |
| `test_installer_web_plan.py` | `plan.py` byte totals per tier equal `downloader.plan_models` plus `plan_bins` on the real `manifest.json`, for both the gpu and cpu-dev profiles |
| `test_installer_web_preflight.py` | Hand-over JSON fixtures (good, no GPU, old driver, low disk, HDD, only Hugging Face offline, fully offline) map to the right `CheckRow`s, `blocking`, `fix` and `operator_copy.json` text |
| `test_installer_bridge.py` | Runs a real P2 in simulate mode through `EngineProcess`: `python -m installer.stage2.wizard --simulate-downloads --ui-bridge stdio --halt-after s03_download_models`. Checks: events parse; `file` progress maps to rows; `pause` holds progress (no new bytes for 1 s); `resume` continues; `stop` exits 130 and leaves the step `pending`; an injected `NS-E109` produces `failure`, and `decision retry` then completes |
| `test_installer_web_headless.py` | `python -m installer.stage2.web --headless --simulate --answers <example>` gives the same exit code and the same final `install_state.json` as the old path |
| `test_installer_web_samples.py` | WAV bars from a generated 1 s sine wave: 28 values in [0.25, 1]. Scratch-home seeding copies only the two config files |
| `test_installer_web_launch.py` | A fake host that exits 3 makes `launch.py` try app mode next. With Edge missing too, it falls to the default browser. With everything failing, `host = "none"` and `setup-address.txt` is written |
| `test_installer_stage1.py` (update) | Dry-run lines for the new flow. `-ProbeOnly` writes valid JSON from the fake probe. `-NoUi`, and answers.json, both select `--headless`. A fake probe with `NS-E101` exits 10 with the splash line |
| `test_installer_files.py` (update) | `installer/ui/static/index.html` and `app.js` exist. `operator_copy.json` covers every code that `Get-PreflightRows` and the steps can raise. An AST walk proves `installer/stage2/web/**` imports only the standard library and `installer.stage2.web.*` |

UI checks are part of the build, since vitest is not in the stack. `tsc -b` must pass under `strict`.
`src/api/types.check.ts` asserts at compile time that `copy.ts` has an entry for every `StepId`,
`CheckId` and `Phase`.

Review fixtures: `installer/stage2/web/fixtures/state-<name>.json` holds one per screen state:
`welcome`, `welcome-resume`, `checks-blocking`, `checks-ok`, `choices`, `downloading`, `paused`,
`installing`, `verifying`, `done`, `failed` and `uninstall`. `--simulate --fixture <name>` serves one
unchanged. The owner reviews them through the dev server. CI does not screenshot them.

### 5.8 Acceptance checklist

CI must pass on the PR:

- [ ] `ui-build` builds `installer/ui`, and the committed `installer/ui/static` matches.
- [ ] `powershell-parse` parses `stage1.ps1`, `uninstall.ps1` and `nightshift-host.ps1`, and
      `nightshift-host.ps1 -CheckOnly` prints JSON and exits 0.
- [ ] Every test in §5.7 passes, and the existing installer tests pass with their updated expectations.
- [ ] `stage1.ps1 -DryRun` passes in CI as before.
- [ ] `make_bundle.ps1 -DryRun` lists `installer/ui/static/index.html` and stays under 25 MB.

Design review by the owner, on the dev server with the fixtures, at 1366 × 768 and 1024 × 640, in dark
and light:

- [ ] Every screen matches its wireframe regions and uses the listed components with the listed props.
- [ ] No colour value outside `--ds-*`.
- [ ] No uppercase labels, no exclamation marks, no trailing periods on labels.
- [ ] Every error and warning card has a "What to do" line.
- [ ] Figures are tabular, and file names and paths are mono.
- [ ] Only the active download row has a live dot, and nothing loops.

On the partner PC, reported back through the diagnostics zip and a phone photo of the window:

- [ ] `INSTALL.cmd` shows the splash, and the window opens within 30 s of Python being ready.
- [ ] The hardware screen shows the RTX 5060 Ti with 16 GB, and the meters.
- [ ] Choices accepts the keys and shows the tier sizes.
- [ ] Download shows per-file progress. Pause and Resume work. After closing the window, running
      `INSTALL.cmd` again reopens it on the Download screen.
- [ ] Verify plays the narrator sample and shows the test frame.
- [ ] Done opens the dashboard, and the diagnostics zip is on the Desktop.
- [ ] The title bar works: dragging moves the window, double-click maximizes, minimize and
      maximize/restore work, and close asks first while installing.

---

## 6. App host (`launchers/nightshift-host.ps1`)

A frameless native window around WebView2. The setup wizard uses it now, and the dashboard can move to
it later (`nightshift-dashboard.ps1` switches over in a separate change).

### 6.1 Runtime and assemblies

- **Assemblies**, shipped in the bundle under `<App>\bin\webview2\` (§5.4, CR-C1-UI-3):
  - `Microsoft.Web.WebView2.Core.dll` and `Microsoft.Web.WebView2.Wpf.dll`, from the NuGet package's
    `lib\net462\`
  - `WebView2Loader.dll`, from `runtimes\win-x64\native\`
- **Runtime.** The Evergreen WebView2 Runtime, already present on Windows 11 and on current Windows 10.
  Detection reads the `pv` value of the client key `{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`, which must
  exist and not be `0.0.0.0`. Locations, checked in order:
  1. `HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`
  2. `HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`
  3. `HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`
- **Fallback.** The script exits with code 3 and prints the reason when the runtime is missing, an
  assembly is missing, or WebView2 fails to initialise. P1's `launch.py` then moves on to Edge or Chrome
  `--app=`, then to the default browser (§1.9).
- **User data folder.** `%LOCALAPPDATA%\Nightshift\webview2`.

### 6.2 Script outline

```powershell
<#
.SYNOPSIS
    Frameless Nightshift window around WebView2.
    nightshift-host.ps1 -Url <http://127.0.0.1:port/?t=...> [-Title 'Nightshift setup'] [-Width 1366] [-Height 768]
                        [-AppPath <dir>] [-CheckOnly]
    Exit codes: 0 window closed, 3 WebView2 unavailable (caller falls back), 4 bad arguments.
    Runs in an STA thread: started as  powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File ...
#>
[CmdletBinding()]
param(
    [string]$Url = '',
    [string]$Title = 'Nightshift',
    [int]$Width = 1366,
    [int]$Height = 768,
    [string]$AppPath = '',
    [switch]$CheckOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $AppPath) { $AppPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }

$ClientKey = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
function Get-WebView2Version {
    $roots = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$ClientKey",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$ClientKey",
        "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$ClientKey"
    )
    foreach ($r in $roots) {
        $pv = (Get-ItemProperty -LiteralPath $r -Name pv -ErrorAction SilentlyContinue).pv
        if ($pv -and $pv -ne '0.0.0.0') { return [string]$pv }
    }
    return $null
}

$dir = Join-Path $AppPath 'bin\webview2'
$dlls = @('Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.Wpf.dll', 'WebView2Loader.dll')
$missing = @($dlls | Where-Object { -not (Test-Path -LiteralPath (Join-Path $dir $_)) })
$runtime = Get-WebView2Version
if ($CheckOnly) {
    [pscustomobject]@{ webview2 = ($null -ne $runtime -and $missing.Count -eq 0); runtime = $runtime; missing = $missing } |
        ConvertTo-Json -Compress
    exit 0
}
if (-not $Url) { exit 4 }
if ($null -eq $runtime -or $missing.Count -gt 0) {
    Write-Host "WebView2 unavailable: runtime=$runtime missing=$($missing -join ',')"
    exit 3
}

Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase
$env:PATH = "$dir;$env:PATH"                                  # WebView2Loader.dll is found next to the Core assembly
Add-Type -Path (Join-Path $dir 'Microsoft.Web.WebView2.Core.dll')
Add-Type -Path (Join-Path $dir 'Microsoft.Web.WebView2.Wpf.dll')
Add-Type -Namespace NsHost -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ReleaseCapture();
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern System.IntPtr SendMessage(System.IntPtr h, int msg, System.IntPtr w, System.IntPtr l);
'@

$win = New-Object System.Windows.Window
$win.Title = $Title
$win.Width = $Width; $win.Height = $Height; $win.MinWidth = 1024; $win.MinHeight = 640
$win.WindowStyle = 'None'
$win.AllowsTransparency = $false
$win.ResizeMode = 'CanResize'
$win.WindowStartupLocation = 'CenterScreen'
$win.Background = (New-Object System.Windows.Media.BrushConverter).ConvertFrom('#0b0b1c')   # dark canvas token: no white flash
$chrome = New-Object System.Windows.Shell.WindowChrome
$chrome.CaptionHeight = 0
$chrome.ResizeBorderThickness = New-Object System.Windows.Thickness 6
$chrome.GlassFrameThickness = New-Object System.Windows.Thickness 0
$chrome.UseAeroCaptionButtons = $false
[System.Windows.Shell.WindowChrome]::SetWindowChrome($win, $chrome)
$wa = [System.Windows.SystemParameters]::WorkArea
if ($Width -gt $wa.Width -or $Height -gt $wa.Height) { $win.WindowState = 'Maximized' }

$wv = New-Object Microsoft.Web.WebView2.Wpf.WebView2
$props = New-Object Microsoft.Web.WebView2.Wpf.CoreWebView2CreationProperties
$props.UserDataFolder = Join-Path $env:LOCALAPPDATA 'Nightshift\webview2'
$wv.CreationProperties = $props
$win.Content = $wv

$script:ready = $false
$script:confirmedClose = $false
$script:uiAcked = $false
$script:exitCode = 0

function Send-Web([hashtable]$Msg) {
    if ($script:ready) { $wv.CoreWebView2.PostWebMessageAsJson(($Msg | ConvertTo-Json -Compress)) }
}
function Set-MaxBounds {
    # Frameless WPF windows cover the taskbar when maximized; cap them to the work area.
    $a = [System.Windows.SystemParameters]::WorkArea
    $win.MaxWidth = $a.Width + 12
    $win.MaxHeight = $a.Height + 12
}
function Get-StateName { "$($win.WindowState)".ToLowerInvariant() }

$wv.add_CoreWebView2InitializationCompleted({
    param($s, $e)
    if (-not $e.IsSuccess) {
        Write-Host "WebView2 init failed: $($e.InitializationException.Message)"
        $script:exitCode = 3; $script:confirmedClose = $true; $win.Close(); return
    }
    $script:ready = $true
    $core = $wv.CoreWebView2
    $core.Settings.AreDefaultContextMenusEnabled = $false
    $core.Settings.AreDevToolsEnabled = ($env:NS_DEVTOOLS -eq '1')
    $core.Settings.IsStatusBarEnabled = $false
    $core.Settings.IsZoomControlEnabled = $false
    $core.add_NewWindowRequested({ param($s2, $a) $a.Handled = $true; Start-Process $a.Uri })   # external links: default browser
    $core.add_WebMessageReceived({
        param($s2, $a)
        try { $m = $a.WebMessageAsJson | ConvertFrom-Json } catch { return }
        switch ([string]$m.cmd) {
            'minimize'        { $win.WindowState = 'Minimized' }
            'toggle-maximize' {
                if ($win.WindowState -eq 'Maximized') { $win.WindowState = 'Normal' }
                else { Set-MaxBounds; $win.WindowState = 'Maximized' }
            }
            'drag-start'      {
                if ($win.WindowState -ne 'Maximized') {
                    $h = (New-Object System.Windows.Interop.WindowInteropHelper $win).Handle
                    [void][NsHost.Native]::ReleaseCapture()
                    [void][NsHost.Native]::SendMessage($h, 0xA1, [IntPtr]2, [IntPtr]::Zero)   # WM_NCLBUTTONDOWN, HTCAPTION
                }
            }
            'close-ack'       { $script:uiAcked = $true }
            'close'           { $script:confirmedClose = $true; $win.Close() }
            'ready'           { Send-Web @{ event = 'window-state'; state = (Get-StateName) } }
        }
    })
})

$win.add_StateChanged({ Send-Web @{ event = 'window-state'; state = (Get-StateName) } })
$win.add_Closing({
    param($s, $e)
    if ($script:confirmedClose -or -not $script:ready) { return }
    $e.Cancel = $true
    $script:uiAcked = $false
    Send-Web @{ event = 'close-requested' }                    # the UI confirms, then posts {cmd: "close"}
    # No GetNewClosure here: inside a closure $script: would point at the closure's own scope.
    $script:closeTimer = New-Object System.Windows.Threading.DispatcherTimer
    $script:closeTimer.Interval = [TimeSpan]::FromSeconds(3)
    $script:closeTimer.add_Tick({
        $script:closeTimer.Stop()
        if (-not $script:uiAcked) { $script:confirmedClose = $true; $win.Close() }   # page not answering: close anyway
    })
    $script:closeTimer.Start()
})

$wv.Source = [Uri]$Url
[void]$win.ShowDialog()
exit $script:exitCode
```

P1 starts the host with
`powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File launchers\nightshift-host.ps1 -Url <url> -Title "Nightshift setup" -AppPath <App>`.
It then waits up to 8 s for one of two things. Exit code 3 means fall back to the next launcher. The
window's first `GET /` request means success, and P1 sets `host: "webview2"`.

### 6.3 Message protocol

```ts
// installer/ui/src/host.ts (moves to ns_dashboard later, unchanged)
export type HostCommand =
  | { cmd: "minimize" }
  | { cmd: "toggle-maximize" }
  | { cmd: "drag-start" }          // pointerdown (primary button) inside the drag region
  | { cmd: "close" }               // only after the UI has decided (§6.5)
  | { cmd: "close-ack" }           // the UI is showing its close dialog; the host must not force-close
  | { cmd: "ready" };              // once after the app mounts; the host answers with window-state

export type HostEvent =
  | { event: "window-state"; state: "normal" | "maximized" | "minimized" }
  | { event: "close-requested" };  // Alt+F4, taskbar close, system menu

interface WebView2Bridge {
  postMessage(message: HostCommand): void;
  addEventListener(type: "message", listener: (e: MessageEvent<HostEvent>) => void): void;
  removeEventListener(type: "message", listener: (e: MessageEvent<HostEvent>) => void): void;
}
declare global {
  interface Window { chrome?: { webview?: WebView2Bridge } }
}

export const host = {
  available: (): boolean => !!window.chrome?.webview,
  send: (c: HostCommand): void => window.chrome?.webview?.postMessage(c),
  on: (fn: (e: HostEvent) => void): (() => void) => {
    const w = window.chrome?.webview;
    if (!w) return () => {};
    const l = (e: MessageEvent<HostEvent>) => fn(e.data);
    w.addEventListener("message", l);
    return () => w.removeEventListener("message", l);
  },
};
```

An object passed to `postMessage` reaches PowerShell as JSON through `WebMessageAsJson`. A message sent
with `PostWebMessageAsJson` reaches the page as `e.data`, already parsed.

### 6.4 `TitleBar` (ref12 component)

`ns_dashboard/ui/src/components/ref12/TitleBar.tsx`:

```ts
export function TitleBar(props: {
  title: string;                     // "Nightshift setup"
  subtitle?: string;                 // phase label
  maximized: boolean;                // from window-state events
  hostControls: boolean;             // host.available()
  onCloseRequest: () => void;        // the UI decides: confirm, or close at once
  onCommand?: (c: "minimize" | "toggle-maximize" | "drag-start") => void;   // defaults to host.send
  className?: string;
}): JSX.Element
```

- **Layout.** 40 px tall, at the top of `AppWindow`, with `px-4` and a transparent background. On the
  left: `Mark size={18}`, then `title` in `text-ds-sm font-semibold`, then ` · ` and `subtitle` in
  `text-ds-sm text-ds-muted`.
- **Drag region.** The whole bar except the buttons, with `user-select: none`. A primary-button
  `onPointerDown` that does not start on a button sends `drag-start`. `onDoubleClick` sends
  `toggle-maximize`.
- **Buttons.** Shown only when `hostControls` is true. Three `IconButton variant="ghost" size="sm"`
  with a gap of 4:
  - `icon="minimize" label="Minimize"`
  - `icon={maximized ? "restore" : "maximize"} label={maximized ? "Restore" : "Maximize"}`
  - `icon="close" label="Close"`, which calls `onCloseRequest`. Its hover is
    `hover:bg-ds-danger hover:text-ds-on-accent`, using existing tokens. It is the only red surface in
    the chrome.

  Each 24 px button gets padding that makes its hit area 32 × 32.
- **Without WebView2.** When `window.chrome?.webview` is absent (the browser fallback), the buttons are
  not rendered and the drag handlers do nothing. The bar stays, as a title line.
- **Keyboard.** The buttons follow the page content in tab order. Alt+F4 is handled by the host
  (`close-requested`).

### 6.5 Close behaviour

What the UI does when the TitleBar close button is pressed or `close-requested` arrives:

| Situation | Response |
|---|---|
| `run` is `idle`, `stopped` or `finished` | Close straight away. When `run` is `finished`, or `idle` before Start, send `close-setup` first, so P1 exits and the console prints its closing line. Then `host.send({cmd: "close"})` |
| `run` is `running`, `paused` or `waiting` | Send `host.send({cmd: "close-ack"})`, then show a `ConfirmDialog` (below) |
| Uninstall running | The same dialog, without "Stop setup and close". Its body reads "Removal finishes in the black window." |

The `ConfirmDialog` while setup is running:

- Title: "Close the setup window?"
- Body: "Setup keeps running in the black setup window. Run INSTALL.cmd to open this window again."
- Buttons:
  - `Button variant="ghost"` "Stop setup and close": calls `cancel`, waits for `run = stopped`, then
    sends `close`
  - `Button variant="soft"` "Keep this open" (autofocus)
  - `Button variant="primary"` "Close window": sends `close` only, and the install continues

### 6.6 Host acceptance

CI:

- [ ] `powershell-parse` parses the script under Windows PowerShell 5.1.
- [ ] Without `bin\webview2`, `nightshift-host.ps1 -CheckOnly` prints
      `{"webview2":false,"runtime":<pv or null>,"missing":[the three names]}` and exits 0.
- [ ] `test_installer_web_launch.py` passes (§5.7).

On the partner PC (manual, listed in the first-run note to the partner):

- [ ] The window opens with no Windows title bar, shows rounded content on the canvas, and has no white
      flash before the page.
- [ ] Dragging the title bar moves the window. Double-clicking maximizes and restores, and the maximized
      window does not cover the taskbar. The edges resize, stopping at 1024 × 640.
- [ ] Minimize, maximize/restore and close work. Closing while downloading asks first, and "Close
      window" leaves the console running and the download going.
- [ ] Alt+F4 asks the same question.
- [ ] Links such as "Get free keys" open in the default browser, not inside the window.
- [ ] With WebView2 removed or blocked, setup opens in Edge app mode instead, and the console shows the
      address.
