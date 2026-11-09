// The demo transport: an in-memory stand-in for the setup server, so the UI can be looked at and clicked through without
// Python. It implements the same Api interface as the real client. ?screen=<name> freezes one fixture; without it the
// demo starts at Welcome and plays a scripted sequence (progress advances on a timer) when the buttons are pressed.
import type { Api } from "../api/client";
import type {
  ActionBody, ActionName, ActionResult, AnswersResult, ChoicesDraft, DiskCheck, DownloadRow, KeyCheck, ModelCheck, PickFolderResult, SetupState, StepId, UiStep, UninstallView, VerifyRow,
} from "../api/types";
import { checkRows, DRIVES, demoNeedGb, downloadRows, downloadsMid, fixture, isoNow, machine, overall, plan as makePlan, resultView, steps as makeSteps, verifyView, answers as answersView } from "./fixtures";

const TICK_MS = 400;
const SPEED = 11.4e6;
const SCALE = 60; // the demo downloads about 60 times faster than the numbers it shows

const sleep = (ms: number) => new Promise<void>((r) => window.setTimeout(r, ms));

export function createDemoApi({ screen }: { screen: string | null }): Api {
  const f = fixture(screen ?? "welcome");
  let st: SetupState | null = f.state;
  let un: UninstallView | null = f.uninstall;
  const waiters = new Set<() => void>();
  let timer: number | null = null;
  const eng = { i: 0, c: 0, winget: new Map<string, number>() };

  const notify = () => { for (const w of [...waiters]) w(); };
  const bump = (patch: Partial<SetupState>) => {
    if (!st) return;
    st = { ...st, ...patch, rev: st.rev + 1, updatedAt: isoNow() };
    notify();
  };
  const cur = (): SetupState => st as SetupState;
  const ok = (message: string | null = null): ActionResult => ({ ok: true, message, state: cur() });
  const no = (message: string): ActionResult => ({ ok: false, message, state: cur() });

  /* ------------------------------------------------------------ engine */

  const startEngine = () => { if (timer == null) timer = window.setInterval(tick, TICK_MS); };
  const stopEngine = () => { if (timer != null) { window.clearInterval(timer); timer = null; } };

  const INSTALL_STEPS: StepId[] = ["s04_venvs", "s05_opencode_prewarm", "s06_tune", "s07_configs"];
  const VERIFY_STEPS: StepId[] = ["s08_smoke", "v01_samples", "s09_shortcuts_scheduler", "s10_summary"];
  const TICKS: Partial<Record<StepId, number>> = { s04_venvs: 9, s05_opencode_prewarm: 5, s06_tune: 11, s07_configs: 4, s08_smoke: 8, v01_samples: 8, s09_shortcuts_scheduler: 4, s10_summary: 4 };
  const DETAIL: Partial<Record<StepId, string>> = {
    s04_venvs: "Installing Python packages for the story, voice and video tools",
    s05_opencode_prewarm: "Starting Nightshift Code once so it opens quickly later",
    s06_tune: "Trying the story model at different settings, 54 tokens/s",
    s07_configs: "Writing your settings and keys",
    s08_smoke: "Loading each model once",
    v01_samples: "Making a test video and a voice sample",
  };
  const LOG: Partial<Record<StepId, string>> = {
    s04_venvs: "uv pip install: 41 packages",
    s05_opencode_prewarm: "opencode: first start done",
    s06_tune: "ns tune: llm ctx 16384, 54.2 tokens/s",
    s07_configs: "config\\runtime.json written",
    s08_smoke: "ns verify: 5 of 6 passed",
    v01_samples: "voice sample 4 s, test render 6 s",
    s09_shortcuts_scheduler: "Nightly task set for 03:00",
    s10_summary: "Diagnostics zip written to the Desktop",
  };

  function pushLog(text: string) {
    const log = [...cur().log, { t: isoNow(), level: "info" as const, text }].slice(-50);
    bump({ log });
  }

  /** Move a list of steps along one tick. Returns true when the last one finished. */
  function runSteps(ids: StepId[]): boolean {
    const s = cur();
    const id = ids[eng.i];
    if (!id) return true;
    eng.c++;
    const need = TICKS[id] ?? 6;
    const finished = eng.c >= need;
    const nextSteps = s.steps.map((x) => {
      const idx = ids.indexOf(x.id);
      if (idx < 0) return x;
      if (idx < eng.i || (idx === eng.i && finished)) return { ...x, status: "done" as const, detail: null, startedAt: x.startedAt ?? isoNow(), finishedAt: x.finishedAt ?? isoNow() };
      if (idx === eng.i) return { ...x, status: "running" as const, detail: DETAIL[x.id] ?? null, startedAt: x.startedAt ?? isoNow() };
      return x;
    });
    let verify = s.verify;
    if (id === "s08_smoke" || id === "v01_samples") {
      const all = verifyView("all");
      const upto = id === "s08_smoke" ? Math.min(all.checks.length, Math.floor((eng.c / need) * all.checks.length) + 1) : all.checks.length;
      const checks: VerifyRow[] = all.checks.map((c, i) => (i < upto - (id === "s08_smoke" && !finished ? 1 : 0) ? c : { ...c, status: i === upto - 1 && id === "s08_smoke" ? "running" : "pending", value: null, note: null }));
      verify = { ...all, checks, voice: id === "v01_samples" && finished ? all.voice : null, render: id === "v01_samples" && finished ? all.render : null, reportReady: false };
    }
    bump({ steps: nextSteps, verify, notices: id === "s08_smoke" && finished ? [{ code: "NS-W302", title: "The speed boost did not help here", whatToDo: "Nothing to do", step: id }] : cur().notices });
    if (finished) { pushLog(LOG[id] ?? "done"); eng.i++; eng.c = 0; }
    return finished && eng.i >= ids.length;
  }

  function tick() {
    const s = cur();
    if (s.run !== "running") return;
    if (s.phase === "downloading") return tickDownload();
    if (s.phase === "installing") {
      if (runSteps(INSTALL_STEPS)) { eng.i = 0; eng.c = 0; bump({ phase: "verifying" }); }
      return;
    }
    if (s.phase === "verifying") {
      if (runSteps(VERIFY_STEPS)) {
        const v = verifyView("all");
        bump({ phase: "done", run: "finished", verify: { ...v, reportReady: true }, result: resultView() });
        stopEngine();
      } else if (cur().steps.find((x) => x.id === "s10_summary")?.status === "done") {
        bump({ verify: { ...(cur().verify ?? verifyView("all")), reportReady: true } });
      }
    }
  }

  function tickDownload() {
    const s = cur();
    const rows: DownloadRow[] = s.downloads.items.map((r) => ({ ...r }));
    const active = () => rows.filter((r) => r.status === "active" || r.status === "paused");
    // promote queued rows: winget/uv rows one at a time, up to two file downloads
    for (const r of rows) {
      if (r.status === "paused") { r.status = "active"; }
    }
    const files = () => active().filter((r) => r.via === "download");
    const tools = () => active().filter((r) => r.via === "winget" || r.via === "uv");
    for (const r of rows) {
      if (r.status !== "queued") continue;
      if ((r.via === "winget" || r.via === "uv") && tools().length < 1) { r.status = "active"; r.note = r.via === "winget" ? "installing with winget, Windows may ask for permission" : null; eng.winget.set(r.key, 0); }
      else if (r.via === "download" && files().length < 2) { r.status = "active"; r.speedBps = SPEED / 2; }
    }
    for (const r of tools()) {
      const n = (eng.winget.get(r.key) ?? 0) + 1;
      eng.winget.set(r.key, n);
      if (n >= 4) { r.status = "done"; r.note = r.via === "winget" ? "installed with winget" : null; }
    }
    const fs = files();
    for (const r of fs) {
      const share = (SPEED * (TICK_MS / 1000) * SCALE) / fs.length;
      r.bytesDone = Math.min(r.bytesTotal ?? 0, r.bytesDone + share);
      r.speedBps = SPEED / fs.length;
      r.etaS = Math.max(60, Math.ceil(((r.bytesTotal ?? 0) - r.bytesDone) / (SPEED / fs.length) / 60) * 60);
      r.note = null;
      if (r.bytesDone >= (r.bytesTotal ?? 0)) { r.status = "done"; r.speedBps = null; r.etaS = null; }
    }
    const o = overall(rows, s.plan, fs.length ? SPEED : null);
    const allDone = rows.every((r) => r.status === "done" || r.status === "skipped");
    const nextSteps = s.steps.map((x) => (x.id === "s03_download_models" ? { ...x, status: allDone ? ("done" as const) : x.status } : x));
    if (allDone) {
      eng.i = 0; eng.c = 0;
      bump({ downloads: { ...s.downloads, items: rows, overall: { ...o, speedBps: null, etaS: null } }, steps: nextSteps, phase: "installing" });
      pushLog("All downloads checked");
    } else {
      bump({ downloads: { ...s.downloads, items: rows, overall: o, paused: false }, steps: nextSteps });
    }
  }

  /* ------------------------------------------------------------ helpers */

  function driveOf(path: string) {
    const letter = (/^([A-Za-z]):[\\/]/.exec(path)?.[1] ?? "").toUpperCase();
    return { letter, drive: DRIVES.find((d) => d.letter === letter) ?? null };
  }

  function applyHome(path: string) {
    const { letter } = driveOf(path);
    if (!letter || !DRIVES.some((d) => d.letter === letter)) return;
    const rows = checkRows(letter);
    const blocking = rows.some((r) => r.blocking);
    bump({ machine: machine(letter), checks: cur().checks.map((c) => rows.find((r) => r.id === c.id && (c.id === "disk" || c.id === "disktype")) ?? c), blocking: cur().checks.some((c) => c.blocking && c.id !== "disk") || blocking });
  }

  /* ------------------------------------------------------------ Api */

  let pickCount = 0;
  const api: Api = {
    demo: true,

    async getState(since, signal) {
      if (!st) throw new Error("no state in this demo screen");
      if (st.rev > since) return st;
      await new Promise<void>((resolve) => {
        const done = () => { waiters.delete(done); window.clearTimeout(t); resolve(); };
        const t = window.setTimeout(done, 15000);
        waiters.add(done);
        signal?.addEventListener("abort", done, { once: true });
      });
      return cur();
    },

    async uiStep(step: UiStep) {
      bump({ uiStep: step, phase: step === "choices" ? "choices" : "checks", checks: cur().checks });
      return ok();
    },

    async postAnswers(draft: ChoicesDraft): Promise<AnswersResult> {
      await sleep(150);
      const p = makePlan(draft.llmTier, cur().machine.ramGb, draft.llmCustom);
      const { pexelsKey, pixabayKey, ...rest } = draft;
      const a = answersView({ ...rest, pexelsKeySet: !!pexelsKey || (cur().answers?.pexelsKeySet ?? false), pixabayKeySet: !!pixabayKey || (cur().answers?.pixabayKeySet ?? false) });
      bump({ answers: a, plan: p, downloads: { ...cur().downloads, items: downloadRows(p.groups), overall: overall(downloadRows(p.groups), p, null), source: draft.modelsSource.kind } });
      applyHome(draft.homePath);
      return { ok: true, answers: a, plan: p };
    },

    async loadAnswersFile(path: string): Promise<AnswersResult> {
      await sleep(250);
      if (!/answers\.json$/i.test(path.trim())) return { ok: false, errors: [{ field: "homePath", message: "That file was not found", code: null }] };
      const a = answersView();
      return { ok: true, answers: a, plan: makePlan(a.llmTier, 32, a.llmCustom) };
    },

    async checkKey(service, key): Promise<KeyCheck> {
      await sleep(550);
      const who = service === "pexels" ? "Pexels" : "Pixabay";
      if (key.trim().length < 8 || /bad/i.test(key)) return { ok: false, code: service === "pexels" ? "NS-E501" : "NS-E502", message: `The ${who} key was not accepted. Copy it again from the website (no spaces)` };
      return { ok: true, code: null, message: null };
    },

    async checkModel(repo, file): Promise<ModelCheck> {
      // The same answers as installer/stage2/web/modelcheck.py with its simulate head: the size comes from the quantization in the name.
      await sleep(600);
      const r = repo.trim(); const f = file.trim();
      const no = (message: string, size: number | null = null, code: string | null = null): ModelCheck => ({ ok: false, size, message, code });
      if (!/^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*$/.test(r)) return no("Type the repository as owner/name, for example Qwen/Qwen3-14B-GGUF");
      if (!/^[A-Za-z0-9][A-Za-z0-9._+/-]*\.gguf$/i.test(f) || f.includes("..")) return no("Type the GGUF file name, for example Qwen3-14B-Q4_K_M.gguf");
      if (/-\d{5}-of-\d{5}\.gguf$/i.test(f)) return no("Split files (-00001-of-00003) are not supported. Pick a single-file version");
      const low = f.toLowerCase();
      if (low.includes("missing")) return no("That file was not found in that repository. Check the spelling of both");
      if (low.includes("private")) return no("That repository needs a login. Nightshift only downloads public files");
      const sizes: [string, number][] = [["70b", 42.5e9], ["f16", 28e9], ["q8", 14.6e9], ["q6", 12.3e9], ["q5", 10.7e9], ["q4", 9e9], ["q3", 7e9]];
      const size = sizes.find(([k]) => low.includes(k))?.[1] ?? 8e9;
      if (size > 15.5e9) return no(`That file is ${(size / 1e9).toFixed(1)} GB. A 16 GB graphics card holds files up to 15.5 GB. Pick a smaller version, for example Q4_K_M`, size);
      return { ok: true, size, message: null, code: null };
    },

    // The demo has no dialog: it "picks" the next folder of a short list and then starts over (the real one is the Windows dialog).
    async pickFolder(target, _start, tier, custom): Promise<PickFolderResult> {
      await sleep(350);
      const folders = target === "models" ? ["D:\\Models", "E:\\Downloads\\Model files"] : ["E:\\Nightshift", "D:\\My Studio\\Nightshift"];
      const path = folders[pickCount++ % folders.length];
      return { ok: true, path, cancelled: false, message: null, check: await api.disk(path, tier, target === "models", custom) };
    },

    async disk(path, tier, source, custom): Promise<DiskCheck> {
      await sleep(180);
      const need = demoNeedGb(tier, custom ?? null);
      if (source) {
        const exists = !/missing|none/i.test(path) && path.trim().length > 2;
        return { path, exists, drive: null, freeGb: null, type: "Unknown", needGb: need, ok: exists, message: exists ? null : "That folder was not found", modelsFound: exists ? { files: 7, of: 9, bytes: 13.4e9 } : null };
      }
      const { letter, drive } = driveOf(path);
      if (!letter || !drive) return { path, exists: false, drive: null, freeGb: null, type: "Unknown", needGb: need, ok: false, message: "Type a full path on a local drive, for example D:\\Nightshift", modelsFound: null };
      const fit = drive.freeGb >= need;
      return { path, exists: true, drive: letter, freeGb: drive.freeGb, type: drive.type, needGb: need, ok: fit, message: fit ? `${drive.freeGb} GB free on ${letter}: (${drive.type})` : `Only ${drive.freeGb} GB free on ${letter}:. This install needs about ${need} GB`, modelsFound: null };
    },

    async action(name: ActionName, body?: ActionBody): Promise<ActionResult> {
      const s = st;
      if (!s) return no("Not available here");
      switch (name) {
        case "start": {
          if (s.blocking) return no("Fix the red items first");
          if (!s.answers) return no("Save your choices first");
          const p = s.plan;
          const items = s.resumed ? s.downloads.items : downloadRows(p.groups);
          eng.i = 0; eng.c = 0; eng.winget.clear();
          bump({ phase: "downloading", run: "running", uiStep: null, startedAt: isoNow(), answersLocked: true, steps: makeSteps("s03_download_models", { detail: "Downloading AI models" }), downloads: { ...s.downloads, items, overall: overall(items, p, null), paused: false }, failure: null });
          startEngine();
          return ok();
        }
        case "pause":
          bump({ run: "paused", downloads: { ...s.downloads, paused: true, overall: { ...s.downloads.overall, speedBps: null, etaS: null }, items: s.downloads.items.map((r) => (r.status === "active" ? { ...r, status: "paused" as const, speedBps: null, etaS: null } : r)) } });
          return ok();
        case "resume":
          bump({ run: "running", downloads: { ...s.downloads, paused: false, items: s.downloads.items.map((r) => (r.status === "paused" ? { ...r, status: "active" as const } : r)) } });
          startEngine();
          return ok();
        case "cancel":
          bump({ run: "stopping" });
          stopEngine();
          window.setTimeout(() => bump({ run: "stopped", downloads: { ...cur().downloads, items: cur().downloads.items.map((r) => (r.status === "active" ? { ...r, status: "queued" as const, speedBps: null, etaS: null } : r)), overall: { ...cur().downloads.overall, speedBps: null, etaS: null } } }), 700);
          return ok();
        case "retry": case "skip": {
          const p = s.plan;
          const mid = downloadsMid(p, false);
          eng.i = 0; eng.c = 0; eng.winget.clear();
          bump({ phase: "downloading", run: "running", failure: null, downloads: mid, steps: makeSteps("s03_download_models", { detail: "Downloading AI models" }) });
          startEngine();
          return ok();
        }
        case "recheck":
          bump({ checking: true });
          window.setTimeout(() => bump({ checking: false, checkedAt: isoNow() }), 2000);
          return ok();
        case "elevate-longpaths":
          await sleep(900);
          bump({ checks: cur().checks.map((c) => (c.id === "longpaths" ? { ...c, status: "ok" as const, value: "on", code: null, title: null, whatToDo: null, fix: null } : c)) });
          return ok();
        case "open-dashboard": return ok("Opening the dashboard");
        case "diagnostics": return ok("C:\\Users\\Abdo\\Desktop\\nightshift-diagnostics-2026-11-04-1203.zip");
        case "copy-status": return ok("Nightshift setup 0.3.0 · step 6/14 Download AI models · 41% · 12:03 elapsed · last error NS-E105 (A download site cannot be reached)");
        case "open-windows-page": return ok();
        case "reveal": return ok(body?.path ?? null);
        case "close-setup": return ok();
      }
    },

    async getUninstall() { return un; },

    async uninstall(): Promise<ActionResult> {
      if (!un) return no("Not available here");
      un = { ...un, running: true };
      window.setTimeout(() => { un = un && { ...un, running: false, result: { removed: ["The nightly task", "Desktop shortcuts", "Programs and Python environments"], kept: [{ label: "AI models", path: un.models.path }, { label: "Videos and history", path: un.data.path }], stays: ["Your settings and logs", "Your stock footage keys, in Windows Credential Manager", "The Visual C++ runtime, FFmpeg and Windows Terminal, which other programs use", "The Python that setup fetched and its download cache (several GB), in your user folders"] } }; }, 2200);
      return { ok: true, message: null, state: undefined as unknown as SetupState };
    },

    fileUrl: (name) => (name === "render-test.jpg" ? "demo-frame.svg" : name === "voice-sample.wav" ? "demo-voice.wav" : `demo-${name}`),
    async fetchText() {
      return JSON.stringify({ version: "0.3.0", checks: verifyView("all").checks.map((c) => ({ name: c.name, passed: c.status !== "failed", value: c.value })) }, null, 2);
    },
  };

  return api;
}

