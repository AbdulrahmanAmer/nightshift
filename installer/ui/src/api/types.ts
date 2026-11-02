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
  recommendedTier: Tier;             // balanced from 32 GB of memory, else small (the fallback)
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
