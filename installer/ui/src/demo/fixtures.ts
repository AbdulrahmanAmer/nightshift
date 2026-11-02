// Fixtures for the demo transport: one realistic SetupState per screen state (docs/design/installer-ui.md section 5.7).
// Numbers follow the spec wireframes: an RTX 5060 Ti with 16 GB, Windows 11 Pro, 17.3 GB to download.
import type {
  AnswersView, CheckRow, DownloadRow, DownloadsView, DriveView, FailureView, InstallPlan, MachineSummary, Notice, PlanGroup, PlanItem, ResultView,
  SetupState, StepId, StepView, Tier, TierInfo, UninstallView, VerifyView,
} from "../api/types";
import { stepTitle } from "../copy";

export const NOW = () => new Date();
const iso = (offsetS = 0): string => {
  const d = new Date(Date.now() + offsetS * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};
export const isoNow = iso;

/* ------------------------------------------------------------------ machine and checks */

export const DRIVES: DriveView[] = [
  { letter: "C", freeGb: 31, type: "SSD" },
  { letter: "D", freeGb: 412, type: "SSD" },
  { letter: "E", freeGb: 1830, type: "HDD" },
];

export function machine(homeDrive: string): MachineSummary {
  const d = DRIVES.find((x) => x.letter === homeDrive) ?? DRIVES[1]!;
  return {
    windows: "Windows 11 Pro 10.0.26200", gpuName: "NVIDIA GeForce RTX 5060 Ti", vramMb: 16311, driver: "581.57", cuda: "13.0", ramGb: 32,
    profile: "gpu", isAdmin: false, drives: DRIVES,
    meters: { vram: { haveGb: 16, needGb: 16, hardMinGb: 12 }, ram: { haveGb: 32, needGb: 16, hardMinGb: 8 }, disk: { haveGb: d.freeGb, needGb: 60, hardMinGb: 40, path: `${d.letter}:\\Nightshift` } },
  };
}

export function checkRows(homeDrive: string, o: { blocking?: boolean } = {}): CheckRow[] {
  const d = DRIVES.find((x) => x.letter === homeDrive) ?? DRIVES[1]!;
  const diskFail = d.freeGb < 40; const diskWarn = !diskFail && d.freeGb < 60;
  const rows: CheckRow[] = [
    { id: "os", label: "Windows 10/11, 64-bit", status: "ok", blocking: false, value: "Windows 11 Pro 10.0.26200", code: null, title: null, whatToDo: null, fix: null },
    { id: "gpu", label: "Graphics card", status: "ok", blocking: false, value: "RTX 5060 Ti, 16 GB", code: null, title: null, whatToDo: null, fix: null },
    o.blocking
      ? { id: "driver", label: "NVIDIA driver", status: "fail", blocking: true, value: "CUDA 12.1, driver 531.18", code: "NS-E103", title: "NVIDIA driver too old", whatToDo: "Install the newest Game Ready or Studio driver, restart, then press Check again", fix: { kind: "open-url", label: "Open the driver page", url: "https://www.nvidia.com/Download/index.aspx" } }
      : { id: "driver", label: "NVIDIA driver", status: "ok", blocking: false, value: "CUDA 13.0, driver 581.57", code: null, title: null, whatToDo: null, fix: null },
    { id: "ram", label: "Memory", status: "ok", blocking: false, value: "32 GB, all model sizes", code: null, title: null, whatToDo: null, fix: null },
    diskFail
      ? { id: "disk", label: "Free disk space", status: "fail", blocking: true, value: `${d.freeGb} GB free on ${d.letter}:`, code: "NS-E104", title: "Not enough free disk space", whatToDo: "Free 60 GB on this drive, or pick another drive below", fix: { kind: "choose-drive", label: "Pick another drive" } }
      : diskWarn
        ? { id: "disk", label: "Free disk space", status: "warn", blocking: false, value: `${d.freeGb} GB free on ${d.letter}:`, code: "NS-E104", title: "Less free space than recommended", whatToDo: "Setup fits, but leaves little room for videos. 60 GB keeps about a month of them", fix: { kind: "choose-drive", label: "Pick another drive" } }
        : { id: "disk", label: "Free disk space", status: "ok", blocking: false, value: `${d.freeGb} GB free on ${d.letter}:`, code: null, title: null, whatToDo: null, fix: null },
    { id: "disktype", label: "Disk type", status: "ok", blocking: false, value: `${d.letter}: ${d.type}`, code: null, title: null, whatToDo: null, fix: null },
    { id: "network", label: "Internet", status: "ok", blocking: false, value: "all sites reachable", code: null, title: null, whatToDo: null, fix: null },
    { id: "winget", label: "winget (App Installer)", status: "ok", blocking: false, value: "available", code: null, title: null, whatToDo: null, fix: null },
    { id: "longpaths", label: "Long file paths", status: "warn", blocking: false, value: "off", code: "NS-W112", title: "Long file paths are off", whatToDo: "Setup works. Press Fix it to turn them on (Windows asks for permission)", fix: { kind: "elevate-longpaths", label: "Fix it" } },
    { id: "waketimers", label: "Wake timers", status: "ok", blocking: false, value: "enabled", code: null, title: null, whatToDo: null, fix: null },
  ];
  return o.blocking || diskFail
    ? rows
    : rows.map((r) => (r.id === "waketimers" ? { ...r, status: "warn", value: "disabled", code: "NS-W107", title: "The PC cannot wake itself at night", whatToDo: "Open Power Options, Change plan settings, Change advanced power settings, Sleep, Allow wake timers, and pick Enable", fix: null } : r));
}

/* ------------------------------------------------------------------ plan */

const item = (key: string, label: string, file: string | null, bytes: number | null, via: PlanItem["via"] = "download"): PlanItem => ({ key, label, file, bytes, via });

const TIER_LLM: Record<Tier, { label: string; repo: string; file: string; bytes: number }> = {
  small: { label: "Story model (Small)", repo: "Qwen/Qwen3-14B-GGUF", file: "Qwen3-14B-Q4_K_M.gguf", bytes: 9001752960 },
  balanced: { label: "Story model (Balanced)", repo: "mudler/Qwen3.6-35B-A3B-APEX-MTP-GGUF", file: "Qwen3.6-35B-A3B-APEX-MTP-I-Mini.gguf", bytes: 14272220768 },
  best: { label: "Story model (Best)", repo: "unsloth/Qwen3.6-35B-A3B-MTP-GGUF", file: "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf", bytes: 22853663008 },
};

export function planGroups(tier: Tier): PlanGroup[] {
  const llm = TIER_LLM[tier];
  const tools = [item("git", "Git", null, null, "winget"), item("ffmpeg", "FFmpeg", null, null, "winget"), item("terminal", "Windows Terminal", null, null, "winget")];
  const python = [item("setup_env", "Python packages for setup", null, null, "uv")];
  const programs = [
    item("llama_cuda13", "Model runner (llama.cpp)", "llama-b12032-bin-win-cuda-13.4-x64.zip", 380e6),
    item("cudart13", "CUDA runtime", "cudart-llama-bin-win-cuda-13.4-x64.zip", 290e6),
    item("qwentts", "Voice engine", "qwentts-win-x64.zip", 20e6),
    item("opencode", "Nightshift Code", "opencode-windows-x64.zip", 30e6),
    item("yt_dlp", "Video downloader for Clipper", "yt-dlp.exe", 12e6),
  ];
  const models = [
    item("llm", llm.label, llm.file, llm.bytes),
    item("tts_custom", "Voice: preset narrators", "qwen-talker-1.7b-customvoice-Q8_0.gguf", 2.0e9),
    item("tts_base", "Voice: your own voices", "qwen-talker-1.7b-base-Q8_0.gguf", 2.1e9),
    item("whisper", "Caption timing", "faster-distil-whisper-large-v3", 1.5e9),
    item("embed", "Memory index", "bge-small-en-v1.5-q8_0.gguf", 0.5e9),
  ];
  const sum = (xs: PlanItem[]) => xs.reduce((a, x) => a + (x.bytes ?? 0), 0);
  return [
    { id: "tools", label: "Free tools", bytes: null, items: tools },
    { id: "python", label: "Python and packages", bytes: 1.5e9, items: python },
    { id: "programs", label: "Programs", bytes: sum(programs), items: programs },
    { id: "models", label: "AI models", bytes: sum(models), items: models },
  ];
}

/** What a 16 GB card has left for context: 16 GB minus the file minus 0.6 GB of runtime (the same formula as plan.vram_left_gb). */
export const vramLeftGb = (bytes: number): number => Math.round((16 - bytes / 1e9 - 0.6) * 10) / 10;
const BASE_BYTES = 8.3e9; // everything except the story model
const tierInfo = (tier: Tier, needFreeGb: number, needsRamGb: number): TierInfo => {
  const m = TIER_LLM[tier];
  return { repo: m.repo, file: m.file, llmBytes: m.bytes, totalBytes: BASE_BYTES + m.bytes, vramLeftGb: vramLeftGb(m.bytes), recommended: tier === "balanced", needFreeGb, needsRamGb, allowed: true, reason: null };
};
const TIERS: Record<Tier, TierInfo> = { small: tierInfo("small", 60, 16), balanced: tierInfo("balanced", 74, 32), best: tierInfo("best", 81, 32) };

export function estimateLabel(totalBytes: number): { downloadMin: number; installMin: number; label: string } {
  const downloadMin = Math.ceil((totalBytes * 8) / (50e6 * 60));
  const installMin = 15;
  const sum = downloadMin + installMin;
  let label: string;
  if (sum >= 50 && sum <= 75) label = "About 1 hour";
  else if (sum < 50) label = `About ${Math.max(5, Math.round(sum / 5) * 5)} minutes`;
  else { const h = Math.round((sum / 60) * 2) / 2; label = `About ${h} hours`; }
  return { downloadMin, installMin, label };
}

export function plan(tier: Tier = "balanced", ramGb = 32): InstallPlan {
  const groups = planGroups(tier);
  const totalBytes = TIERS[tier].totalBytes;
  const tiers = { ...TIERS };
  if (ramGb < 31) {
    tiers.balanced = { ...tiers.balanced, allowed: false, reason: "Needs 32 GB of memory" };
    tiers.best = { ...tiers.best, allowed: false, reason: "Needs 32 GB of memory" };
  }
  return { tier, profile: "gpu", groups, totalBytes, unknownCount: 4, needFreeGb: TIERS[tier].needFreeGb, estimate: estimateLabel(totalBytes), recommendedTier: ramGb < 31 ? "small" : "balanced", tiers };
}

/* ------------------------------------------------------------------ steps, downloads, verify */

const PHASE_OF: Record<StepId, StepView["phase"]> = {
  b01_tools: "downloading", b02_home: "downloading", b03_setup_env: "downloading", s01_questions: "downloading", s02_download_bins: "downloading", s03_download_models: "downloading",
  s04_venvs: "installing", s05_opencode_prewarm: "installing", s06_tune: "installing", s07_configs: "installing",
  s08_smoke: "verifying", v01_samples: "verifying", s09_shortcuts_scheduler: "verifying", s10_summary: "verifying",
};
export const STEP_ORDER = Object.keys(PHASE_OF) as StepId[];

export function steps(firstOpen: StepId | "all-done", o: { failed?: StepId; running?: boolean; detail?: string | null } = {}): StepView[] {
  const cut = firstOpen === "all-done" ? STEP_ORDER.length : STEP_ORDER.indexOf(firstOpen);
  return STEP_ORDER.map((id, i) => {
    let status: StepView["status"] = i < cut ? "done" : "pending";
    if (i === cut && o.running !== false && !o.failed) status = "running";
    if (o.failed === id) status = "failed";
    const startedAt = status === "pending" ? null : iso(-(STEP_ORDER.length - i) * 55);
    return { id, title: stepTitle[id], phase: PHASE_OF[id], status, detail: status === "running" ? (o.detail ?? null) : status === "failed" ? "A downloaded file is damaged" : null, startedAt, finishedAt: status === "done" ? iso(-(STEP_ORDER.length - i - 1) * 55) : null };
  });
}

export function answers(o: Partial<AnswersView> = {}): AnswersView {
  return {
    ownerName: "Abdo", channel: { displayName: "After Dark Tales", niche: "horror fiction", language: "en" }, homePath: "D:\\Nightshift", llmTier: "balanced",
    voice: { speaker: "Ryan" }, modelsSource: { kind: "internet", path: null }, runTime: "03:00", mastersPerWeek: 1, installCode: true, theme: "dark",
    pexelsKeySet: true, pixabayKeySet: true, ...o,
  };
}

function rowFrom(it: PlanItem, group: PlanGroup["id"]): DownloadRow {
  return { key: it.key, label: it.label, file: it.file, group, via: it.via, bytesTotal: it.bytes, bytesDone: 0, status: "queued", speedBps: null, etaS: null, note: null, error: null };
}
export function downloadRows(groups: PlanGroup[]): DownloadRow[] {
  return groups.flatMap((g) => g.items.map((i) => rowFrom(i, g.id)));
}

/** Overall numbers from the rows. The Python packages row has no size of its own, so it counts its group's estimate. */
export function overall(rows: DownloadRow[], plan0: InstallPlan, speedBps: number | null): DownloadsView["overall"] {
  const py = plan0.groups.find((g) => g.id === "python")?.bytes ?? 0;
  const pyRow = rows.find((r) => r.key === "setup_env");
  const bytesTotal = plan0.totalBytes;
  const bytesDone = rows.reduce((a, r) => a + (r.via === "download" || r.via === "copy" ? r.bytesDone : 0), 0) + (pyRow?.status === "done" ? py : 0);
  const etaS = speedBps ? Math.max(60, Math.ceil((bytesTotal - bytesDone) / speedBps / 60) * 60) : null;
  return { bytesTotal, bytesDone, filesTotal: rows.length, filesDone: rows.filter((r) => r.status === "done" || r.status === "skipped").length, speedBps, etaS };
}

export function downloadsMid(plan0: InstallPlan, paused = false): DownloadsView {
  const rows = downloadRows(plan0.groups).map((r): DownloadRow => {
    const done = (note: string | null): DownloadRow => ({ ...r, status: "done", bytesDone: r.bytesTotal ?? 0, note });
    switch (r.key) {
      case "git": return done("already here");
      case "terminal": return done("already here");
      case "ffmpeg": return { ...r, status: paused ? "paused" : "active", note: "installing with winget, Windows may ask for permission" };
      case "setup_env": return { ...r, status: "queued", note: null };
      case "llama_cuda13": return done(null);
      case "cudart13": return { ...r, status: paused ? "paused" : "active", bytesDone: Math.round(290e6 * 0.62), speedBps: paused ? null : 4.2e6, etaS: paused ? null : 26 };
      case "llm": return { ...r, status: paused ? "paused" : "active", bytesDone: 2.1e9, speedBps: paused ? null : 11.4e6, etaS: paused ? null : 605 };
      case "tts_base": return { ...r, status: "queued", bytesDone: 0.4e9, note: "will continue from 0.4 GB" };
      default: return r;
    }
  });
  const speed = paused ? null : 11.4e6;
  const o = overall(rows, plan0, speed);
  const view = { items: rows, overall: { ...o, bytesDone: 6.2e9, etaS: paused ? null : 2460 }, source: "internet" as const, paused };
  return view;
}

function waveBars(seed: string, n = 28): number[] {
  let s = 0; for (const ch of seed) s = (s * 31 + ch.charCodeAt(0)) % 997;
  return Array.from({ length: n }, (_, i) => Math.round((0.25 + Math.abs(Math.sin(i * 0.9 + s) * 0.6 + Math.sin(i * 2.3 + s * 2) * 0.4) * 0.75) * 100) / 100);
}

export function verifyView(stage: "partial" | "all"): VerifyView {
  const checks: VerifyView["checks"] = [
    { name: "llm", label: "Story model", status: "passed", value: "54 tokens/s", note: null },
    { name: "embed", label: "Memory index", status: "passed", value: "ready", note: null },
    { name: "tts", label: "Voice engine", status: "passed", value: "Ryan, 0.31 RTF", note: null },
    { name: "whisper", label: "Captions", status: "passed", value: "graphics card", note: null },
    { name: "nvenc", label: "Video encoder", status: "passed", value: "NVENC", note: null },
    { name: "mtp", label: "Faster decoding", status: "fallback", value: "off", note: "NS-W302" },
  ];
  return {
    checks: stage === "partial" ? checks : checks,
    voice: { url: "demo-voice.wav", speaker: "Ryan", seconds: 4, text: "This is how your channel will sound at night.", bars: waveBars("Ryan") },
    render: { videoUrl: "", thumbUrl: "demo-frame.svg", seconds: 6, width: 1080, height: 1920, encoder: "h264_nvenc" },
    renderNote: null,
    reportReady: stage === "all" || true,
  };
}

const NOTICE_W302: Notice = { code: "NS-W302", title: "Faster decoding was not faster here", whatToDo: "Nothing to do", step: "s08_smoke" };

export function resultView(): ResultView {
  return {
    home: "D:\\Nightshift", outbox: "D:\\Nightshift\\outbox", appPath: "D:\\NightShift",
    installed: [
      { label: "Story model (Small)", detail: "9.0 GB" }, { label: "Voices: presets and your own", detail: "4.4 GB" }, { label: "Captions, memory, faces", detail: "1.7 GB" },
      { label: "Nightshift Code", detail: "yes" }, { label: "Night run", detail: "03:00" }, { label: "Everything else", detail: "D:\\Nightshift" },
    ],
    shortcuts: ["Nightshift Dashboard", "Nightshift Outbox", "Nightshift Diagnostics"], runTime: "03:00", scheduled: true,
    diagnosticsZip: "C:\\Users\\Abdo\\Desktop\\nightshift-diagnostics-2026-11-02.zip", durationS: 58 * 60 + 12,
    warnings: [{ code: "NS-W112", title: "Long file paths are off", whatToDo: "Setup works. Press Fix it to turn them on (Windows asks for permission)", step: null }],
  };
}

export function failureView(): FailureView {
  return {
    step: "s03_download_models", stepTitle: "Download AI models", code: "NS-E109", title: "A downloaded file is damaged",
    whatToDo: "Press Retry. Setup downloads that file again. If the same error comes back, send the diagnostics zip.",
    detail: [
      "qwen-talker-1.7b-base-Q8_0.gguf: expected 4b9a33a2c1d94e7f8a3b5c6d7e8f9012a3b4c5d6e7f8091a2b3c4d5e6f70812",
      "got 0c1d7f0e92b84c55a1e6d3f4b5a69788c9d0e1f2a3b4c5d6e7f8091a2b3c4d5e",
      "attempt 1: size 2,104,332,288 of 2,104,332,288 bytes, hash differs",
      "attempt 2: size 2,104,332,288 of 2,104,332,288 bytes, hash differs",
      "attempt 3: size 2,104,332,288 of 2,104,332,288 bytes, hash differs",
      "source: https://huggingface.co/Qwen/qwen-talker-1.7b-base-GGUF",
      "file kept as qwen-talker-1.7b-base-Q8_0.gguf.part",
      "free space on D: 388 GB",
    ].join("\n"),
    canSkip: true, attempts: 3, at: iso(-120),
  };
}

/* ------------------------------------------------------------------ whole states */

export function baseState(over: Partial<SetupState> = {}): SetupState {
  const m = machine("D");
  const p = plan("balanced");
  return {
    schema: 1, rev: 1, version: "0.3.0", appPath: "D:\\NightShift", phase: "checks", run: "idle", uiStep: "welcome", resumed: false, prefilledFrom: null,
    startedAt: null, updatedAt: iso(), host: "webview2", machine: m, checks: checkRows("D"), checkedAt: iso(-60), checking: false, blocking: false, plan: p,
    answers: null, answersLocked: false, steps: steps("b01_tools", { running: false }), downloads: { items: downloadRows(p.groups), overall: overall(downloadRows(p.groups), p, null), source: "internet", paused: false },
    verify: null, failure: null, notices: [], result: null, log: [], ...over,
  };
}

const LOGS = [
  { t: iso(-30), level: "info" as const, text: "Checks done: 8 ok, 2 warnings" },
  { t: iso(-20), level: "info" as const, text: "Choices saved for After Dark Tales" },
  { t: iso(-14), level: "info" as const, text: "Git: already here" },
  { t: iso(-8), level: "info" as const, text: "FFmpeg: installing with winget" },
  { t: iso(-2), level: "info" as const, text: "Qwen3-14B-Q4_K_M.gguf  2.1 of 9.0 GB  11.4 MB/s  10 min left" },
];

export const SCREENS = ["welcome", "welcome-resume", "checks", "checks-blocking", "choices", "downloading", "paused", "installing", "verifying", "done", "failed", "uninstall", "uninstall-done", "stop-dialog", "close-dialog"] as const;
export type ScreenName = (typeof SCREENS)[number];

export function uninstallView(over: Partial<UninstallView> = {}): UninstallView {
  return {
    home: "D:\\Nightshift",
    always: [{ label: "The nightly task", bytes: null }, { label: "Desktop shortcuts", bytes: null }, { label: "Programs and Python environments", bytes: 2.3e9 }, { label: "The install folder setting", bytes: null }],
    models: { path: "D:\\Nightshift\\models", bytes: 15.1e9 }, data: { path: "D:\\Nightshift\\data", bytes: 38.4e9 }, running: false, result: null, ...over,
  };
}

export function fixture(name: string): { state: SetupState | null; uninstall: UninstallView | null } {
  const dl = (paused: boolean, run: SetupState["run"]): SetupState => {
    const p = plan("small");
    return baseState({
      phase: "downloading", run, uiStep: null, startedAt: iso(-1500), answers: answers(), answersLocked: true, plan: p, steps: steps("s03_download_models", { detail: "Downloading AI models" }),
      downloads: downloadsMid(p, paused), log: LOGS, checks: checkRows("D"),
    });
  };
  switch (name) {
    case "welcome": return { state: baseState(), uninstall: null };
    case "welcome-resume": {
      const p = plan("small");
      const rows = downloadRows(p.groups).map((r): DownloadRow => (r.key === "git" || r.key === "terminal" || r.key === "ffmpeg" || r.key === "setup_env" || r.key === "llama_cuda13" || r.key === "cudart13" || r.key === "qwentts" || r.key === "opencode" || r.key === "yt_dlp" ? { ...r, status: "done", bytesDone: r.bytesTotal ?? 0, note: "already here" } : r.key === "llm" ? { ...r, bytesDone: 4.7e9, note: "will continue from 4.7 GB" } : r));
      return { state: baseState({ phase: "downloading", run: "idle", uiStep: null, resumed: true, startedAt: iso(-86000), answers: answers(), answersLocked: true, steps: steps("s03_download_models", { running: false }), downloads: { items: rows, overall: { ...overall(rows, p, null), bytesDone: 6.3e9, etaS: null }, source: "internet", paused: false } }), uninstall: null };
    }
    case "checks": return { state: baseState({ uiStep: "checks", checks: checkRows("D") }), uninstall: null };
    case "checks-blocking": {
      const rows = checkRows("C", { blocking: true });
      return { state: baseState({ uiStep: "checks", machine: machine("C"), checks: rows, blocking: true }), uninstall: null };
    }
    case "choices": return { state: baseState({ phase: "choices", uiStep: "choices" }), uninstall: null };
    case "downloading": return { state: dl(false, "running"), uninstall: null };
    case "paused": return { state: dl(true, "paused"), uninstall: null };
    case "stop-dialog": return { state: dl(false, "running"), uninstall: null };
    case "close-dialog": return { state: dl(false, "running"), uninstall: null };
    case "installing": {
      const p = plan("small");
      return {
        state: baseState({
          phase: "installing", run: "running", uiStep: null, startedAt: iso(-3000), answers: answers(), answersLocked: true, plan: p,
          steps: steps("s06_tune", { detail: "Trying the story model at different settings, 54 tokens/s" }).map((s) => (s.id === "s05_opencode_prewarm" ? { ...s, detail: null } : s)),
          downloads: { items: downloadRows(p.groups).map((r) => ({ ...r, status: "done" as const, bytesDone: r.bytesTotal ?? 0 })), overall: { bytesTotal: p.totalBytes, bytesDone: p.totalBytes, filesTotal: 14, filesDone: 14, speedBps: null, etaS: null }, source: "internet", paused: false },
          log: [...LOGS, { t: iso(-1), level: "info", text: "ns tune: llm ctx 16384, 54.2 tokens/s" }],
        }),
        uninstall: null,
      };
    }
    case "verifying": {
      const p = plan("small");
      return {
        state: baseState({
          phase: "verifying", run: "running", uiStep: null, startedAt: iso(-3300), answers: answers(), answersLocked: true, plan: p,
          steps: steps("s09_shortcuts_scheduler"), verify: verifyView("partial"), notices: [NOTICE_W302],
          downloads: { items: downloadRows(p.groups).map((r) => ({ ...r, status: "done" as const, bytesDone: r.bytesTotal ?? 0 })), overall: { bytesTotal: p.totalBytes, bytesDone: p.totalBytes, filesTotal: 14, filesDone: 14, speedBps: null, etaS: null }, source: "internet", paused: false },
          log: LOGS,
        }),
        uninstall: null,
      };
    }
    case "done": {
      const p = plan("small");
      return {
        state: baseState({
          phase: "done", run: "finished", uiStep: null, startedAt: iso(-3500), answers: answers(), answersLocked: true, plan: p, steps: steps("all-done", { running: false }), verify: verifyView("all"),
          notices: [NOTICE_W302], result: resultView(), log: LOGS,
        }),
        uninstall: null,
      };
    }
    case "failed": {
      const p = plan("small");
      return {
        state: baseState({
          phase: "failed", run: "waiting", uiStep: null, startedAt: iso(-1900), answers: answers(), answersLocked: true, plan: p, steps: steps("s03_download_models", { failed: "s03_download_models" }), failure: failureView(), log: LOGS,
        }),
        uninstall: null,
      };
    }
    case "uninstall": return { state: null, uninstall: uninstallView() };
    case "uninstall-done":
      return {
        state: null,
        uninstall: uninstallView({ result: { removed: ["The nightly task", "Desktop shortcuts", "Programs and Python environments"], kept: [{ label: "AI models", path: "D:\\Nightshift\\models" }, { label: "Videos and history", path: "D:\\Nightshift\\data" }] } }),
      };
    default: return { state: baseState(), uninstall: null };
  }
}
