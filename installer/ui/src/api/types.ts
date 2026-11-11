// Data shapes of the setup server (docs/design/installer-ui.md section 5.6). Mirrored by installer/stage2/web/model.py.
// HTTP JSON keys are camelCase. Do not edit one side without the other.

export type Phase = "checks" | "choices" | "downloading" | "installing" | "verifying" | "done" | "failed";
export type RunState = "idle" | "running" | "pausing" | "paused" | "waiting" | "stopping" | "stopped" | "finished";
export type Tier = "small" | "balanced" | "best";
export type PresetVoice = "Ryan" | "Aiden" | "Serena" | "Vivian";
export type UiStep = "welcome" | "checks" | "choices";
export type StepId =
  | "b01_tools" | "b02_home" | "b03_setup_env"
  | "s01_questions" | "s02_download_bins" | "s03_download_models" | "s04_venvs" | "s05_opencode_prewarm"
  | "s06_tune" | "s07_configs" | "s08_smoke" | "v01_samples" | "s09_shortcuts_scheduler" | "s10_summary";
/** Windows pages the server may open by name (model.WINDOWS_PAGES). */
export type WindowsPage = "power" | "protection-history";
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
  devMode: boolean;                  // INSTALL.cmd --dev: stub models, fake engines (docs/DEV_MODE.md)
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
  support: SupportView;              // who the help file goes to (installer/support.json)
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
  | { kind: "open-windows-page"; label: string; page: WindowsPage }
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
export interface TierInfo {
  repo: string | null;               // Hugging Face repository of the model (manifest.json)
  file: string | null;               // GGUF file name
  llmBytes: number;                  // download size of the model file
  totalBytes: number;                // whole download for this tier
  vramLeftGb: number | null;         // video memory a 16 GB card has left for context: 16 GB - file - 0.6 GB (negative = larger than the card)
  recommended: boolean;              // Balanced
  needFreeGb: number;
  needsRamGb: number;
  allowed: boolean;
  reason: string | null;
}
export interface InstallPlan {
  tier: Tier;
  profile: "gpu" | "cpu-dev";
  groups: PlanGroup[];
  totalBytes: number;                // known sizes only
  unknownCount: number;              // items whose size is learned when the download starts
  needFreeGb: number;
  estimate: { downloadMin: number; installMin: number; label: string };   // label: "About 1 hour"
  recommendedTier: Tier;             // balanced from 32 GB of memory, else small (the fallback)
  tiers: Record<Tier, TierInfo>;
}

/** A story model the owner named: size is what the server resolved with a HEAD request. */
export interface LlmCustom { repo: string; file: string; size: number }
export interface ModelCheck { ok: boolean; size: number | null; message: string | null; code: string | null }

export interface ChoicesDraft {
  ownerName: string | null;
  channel: { displayName: string; niche: string; language: string };   // language defaults to "en"
  homePath: string;
  llmTier: Tier;
  llmCustom: LlmCustom | null;       // a Hugging Face GGUF the owner named; replaces the tier's model when set (tier is then "small")
  voice: { speaker: PresetVoice };
  modelsSource: { kind: "internet" | "folder" | "drive-zip"; path: string | null };
  runTime: string;                   // "03:00"
  nightly: boolean;                  // the switch "Run Nightshift by itself every night"; off on a new install
  nightlySeen: string;               // what Automatic runs were when the page showed the switch: "new", "off" or "on@HH:MM"; sent back unchanged
  shortcutsHere: boolean;            // the switch "Make the Desktop shortcuts and the ns and studio commands open this install": only shown while one of them is another Nightshift folder's; off keeps them
  mastersPerWeek: 1 | 2 | 3;
  installCode: boolean;
  includeVoicedesign: boolean;       // download the voice design model now (about 2 GB); otherwise it comes with the first design
  theme: "dark" | "light";
  pexelsKey: string | null;          // null keeps the stored key (resume)
  pixabayKey: string | null;
}
/** Whose the Desktop shortcuts are, seen from one install folder. `mine`: this install's, or nobody's (no question). */
export interface ShortcutsNow { state: "mine" | "other" | "unreachable"; path: string | null; line: string | null }
export interface AnswersView extends Omit<ChoicesDraft, "pexelsKey" | "pixabayKey"> {
  shortcutsNow?: ShortcutsNow;       // for the folder in homePath; the folder check brings it again for every folder picked
  pexelsKeySet: boolean;
  pixabayKeySet: boolean;
}
export type ChoicesField =
  | "ownerName" | "channel.displayName" | "channel.niche" | "channel.language" | "homePath" | "llmTier" | "llmCustom"
  | "voice.speaker" | "modelsSource.path" | "runTime" | "mastersPerWeek" | "pexelsKey" | "pixabayKey";
export interface FieldError { field: ChoicesField; message: string; code: string | null }
export type AnswersResult = { ok: true; answers: AnswersView; plan: InstallPlan } | { ok: false; errors: FieldError[] };

export interface KeyCheck { ok: boolean; code: "NS-E501" | "NS-E502" | "NS-E503" | "NS-E504" | "NS-E505" | null; message: string | null }
/** The choices besides the folder and the tier that change the space an install needs: they go with every folder check, so the picker line, the folder error and the refusal on Install use one number (Install checks the draft). */
export type DiskDraft = Pick<ChoicesDraft, "llmCustom" | "installCode" | "includeVoicedesign" | "modelsSource">;
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
  nightlySeen?: string;              // Automatic runs in THIS folder ("new", "off", "on@HH:MM"); install folder checks only
  shortcutsNow?: ShortcutsNow;       // whose the Desktop shortcuts are, seen from THIS folder; install folder checks only
}

/** POST /api/pick-folder: the native folder dialog, opened by the server. `check` is the DiskCheck of the picked path. */
export interface PickFolderResult {
  ok: boolean;
  path: string | null;               // null when cancelled or when the dialog could not open
  cancelled: boolean;
  message: string | null;            // set when ok is false: plain words
  check: DiskCheck | null;
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
  runTime: string;                   // the time Automatic runs have when they are on, else the chosen time
  scheduled: boolean;                // Automatic runs are on (the record of the Windows task says so)
  nightly?: "on" | "off" | "failed" | "unrecorded"; // off is a choice; failed: setup tried to switch them on and Windows refused; unrecorded: Windows runs the task and Nightshift could not record it (NS-W208)
  nightlyLine?: string | null;       // the sentence the end of setup says about Automatic runs
  shortcutsLine?: string | null;     // the sentence about whose the Desktop shortcuts are (NS-W119 when another install's were kept)
  homeVarLine?: string | null;       // the sentence about NIGHTSHIFT_HOME when it names, or named, another Nightshift folder
  commandsLine?: string | null;      // the sentence about the ns and studio commands: on the PATH, another install's (NS-W120), or why not
  diagnosticsZip: string | null;
  stockKeyMissing: boolean;          // neither a Pexels nor a Pixabay key is stored
  durationS: number;
  warnings: Notice[];
  devMode?: boolean;                 // a dev install: the Done screen says so in its list
}
/** installer/support.json: both null until the owner fills it in. */
export interface SupportView { name: string | null; driveUrl: string | null }
export interface LogLine { t: string; level: "info" | "warn" | "error"; text: string }

export type ActionName =
  | "start" | "pause" | "resume" | "retry" | "skip" | "cancel" | "recheck" | "open-dashboard"
  | "diagnostics" | "reveal" | "elevate-longpaths" | "close-setup" | "copy-status" | "open-windows-page";
export interface ActionBody { path?: string }
export interface ActionResult { ok: boolean; message: string | null; state: SetupState }

export interface UninstallView {
  home: string | null;
  always: { label: string; bytes: number | null }[];
  models: { path: string; bytes: number };
  data: { path: string; bytes: number };
  running: boolean;
  /** `keys`: the uninstall program's own lines about the stock-footage keys (forgotten, left for another
   *  Nightshift folder, or what could not be removed); absent from a server that predates them. */
  result: { removed: string[]; kept: { label: string; path: string }[]; stays: string[]; keys?: string[] } | null;
}
