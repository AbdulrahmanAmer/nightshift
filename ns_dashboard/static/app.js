/* Nightshift Dashboard — core (API with demo fallback, state, router, shared UI) and the Queue screen.
   Create, Channels, Results and Settings live in app-pages.js and register on window.NS.pages. */
(function () {
  "use strict";
  const NS = (window.NS = { pages: {}, state: {}, util: {} });

  // ---------------------------------------------------------------- utilities
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtDur = (s) => { s = Math.round(Number(s) || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const fmtWhen = (iso) => { if (!iso) return ""; const d = new Date(iso); return isNaN(d) ? String(iso) : d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const STAGE_WORDS = { ideas: "Finding an idea", plan: "Planning the story", draft: "Writing the story", critic_revise: "Editing the story", split: "Cutting into parts", hooks: "Writing the opening line", seo: "Writing title and tags", visual_plan: "Planning footage", policy_text: "Safety check", voice: "Recording narration", duration_check: "Checking length", captions: "Adding captions", shorts_timing: "Picking the best moments", clips: "Finding footage", render: "Rendering", render_master: "Rendering the full video", thumbnails: "Making thumbnails", shorts_derive: "Cutting the Shorts", qc: "Checking quality", policy_final: "Final check", outbox: "Saving", import: "Importing the video", audio: "Extracting audio", transcribe: "Transcribing", select: "Choosing moments", detect: "Finding faces", seo_clip: "Writing titles", queued: "Waiting for the engine" };
  const stageWords = (k) => STAGE_WORDS[k] || (k ? String(k).replace(/_/g, " ") : "Working");
  const kindLabel = (k) => ({ long: "Full video", short: "Short", clip: "Clip" }[k] || k);
  const store = { get(k) { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} } };
  Object.assign(NS.util, { $, $$, esc, fmtDur, fmtWhen, sleep, stageWords, kindLabel, store });

  // ---------------------------------------------------------------- toasts, modal, copy
  function toast(msg, kind) { const el = document.createElement("div"); el.className = "toast" + (kind ? " " + kind : ""); el.textContent = msg; $("#toasts").appendChild(el); setTimeout(() => el.remove(), 4200); }
  function modal({ title, body, okLabel = "OK", cancelLabel = "Cancel", danger = false, input = null }) {
    return new Promise((resolve) => {
      const root = $("#modal"); root.hidden = false;
      root.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><h2 id="modal-title">${esc(title)}</h2>${body ? `<p class="muted">${esc(body)}</p>` : ""}${input ? `<div class="field"><label for="modal-input">${esc(input.label)}</label><input id="modal-input" class="input" placeholder="${esc(input.placeholder || "")}"></div>` : ""}<div class="btn-row"><button class="btn btn-ghost" data-act="cancel" type="button">${esc(cancelLabel)}</button><button class="btn ${danger ? "btn-danger-outline" : "btn-primary"}" data-act="ok" type="button">${esc(okLabel)}</button></div></div>`;
      const close = (v) => { root.hidden = true; root.innerHTML = ""; resolve(v); };
      root.onclick = (e) => { const a = e.target.closest("[data-act]")?.dataset.act; if (a === "cancel" || e.target === root) close(null); if (a === "ok") close(input ? ($("#modal-input").value || "") : true); };
      root.onkeydown = (e) => { if (e.key === "Escape") close(null); if (e.key === "Enter" && input) close($("#modal-input").value || ""); };
      (input ? $("#modal-input") : $('[data-act="ok"]', root)).focus();
    });
  }
  async function copyText(text, label) {
    try { await navigator.clipboard.writeText(text); toast(`${label || "Text"} copied`, "ok"); return true; }
    catch { const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); let ok = false; try { ok = document.execCommand("copy"); } catch {} ta.remove(); toast(ok ? `${label || "Text"} copied` : "Select the text and press Ctrl+C", ok ? "ok" : "warn"); return ok; }
  }
  Object.assign(NS.util, { toast, modal, copyText });

  // ---------------------------------------------------------------- demo data (only when no engine answers)
  const DEMO = {
    status: { engine: { state: "idle", detail: "Idle", elapsed_s: null, vram_owner: null }, next_run: new Date(Date.now() + 9 * 3600e3).toISOString(), outbox: { pending: 3, approved: 2, rejected: 1 },
      channels: [{ slug: "after-dark", display_name: "After Dark Stories", status: "active", niche: "original short horror fiction set in everyday places", voice: "Ryan", masters_per_week: 1, publish_window: "18:00-21:00" }, { slug: "tiny-mysteries", display_name: "Tiny Mysteries", status: "paused", niche: "small-town mysteries with a twist", voice: "Aiden", masters_per_week: 2, publish_window: "17:00-20:00" }],
      services: { llama: { state: "stopped" }, embed: { state: "stopped" }, tts: { state: "stopped" }, dashboard: { state: "ready" } }, disk: { free_gb: 412, total_gb: 931, home: "D:\\Nightshift" } },
    outputs: [
      { output_id: "after-dark-20261101-a1f2/long", kind: "long", title: "The Doorbell Camera That Recorded Tomorrow", channel: "after-dark", duration_s: 731, created_at: "2026-11-01T03:41:00", status: "pending", path: "D:\\Nightshift\\outbox\\after-dark\\2026-11-01\\after-dark-20261101-a1f2\\long\\master.mp4" },
      { output_id: "after-dark-20261101-a1f2/s1", kind: "short", k: 1, title: "My doorbell camera records someone every night at 3:12", channel: "after-dark", duration_s: 52, created_at: "2026-11-01T03:52:00", status: "pending", path: "D:\\Nightshift\\outbox\\after-dark\\2026-11-01\\after-dark-20261101-a1f2\\shorts\\s1.mp4" },
      { output_id: "clip-7f3a/c2", kind: "clip", k: 2, title: "Why the first ten minutes of a stream decide everything", channel: "tiny-mysteries", duration_s: 38, created_at: "2026-11-01T11:12:00", status: "pending", path: "D:\\Nightshift\\outbox\\tiny-mysteries\\2026-11-01\\clip-7f3a\\c2.mp4" },
      { output_id: "after-dark-20261031-9c0d/long", kind: "long", title: "The Laundromat That Only Opens at Night", channel: "after-dark", duration_s: 702, created_at: "2026-10-31T03:40:00", status: "uploaded", url: "https://youtu.be/demo1" },
      { output_id: "after-dark-20261031-9c0d/s1", kind: "short", k: 1, title: "The dryer was still warm when I got there", channel: "after-dark", duration_s: 49, created_at: "2026-10-31T03:50:00", status: "approved" },
      { output_id: "after-dark-20261030-7b1e/s3", kind: "short", k: 3, title: "The night bus that skips my stop", channel: "after-dark", duration_s: 61, created_at: "2026-10-30T03:55:00", status: "rejected", reason: "ending made no sense" },
    ],
    detail: { description: "A night-shift nurse stops checking her doorbell clips. Tonight the notification arrives while she is standing on the porch.\n\nOriginal fiction.", tags: ["horror story", "scary story", "doorbell camera", "short horror", "creepy story", "narrated story", "fiction", "suspense", "night shift", "porch"], hashtags: ["#horrorstory", "#scarystories", "#fiction"], publish_after: new Date(Date.now() + 30 * 3600e3).toISOString(), ai_disclosure: "no", made_for_kids: false, age_restrict_suggested: false, story_excerpt: "The doorbell camera pings at 3:12 every night. I stopped checking the clips a week ago. Tonight my phone buzzed while I was standing on the porch…", policy_notes: ["No real people, places or brands named", "Title matches what happens in the story", "Marked as original fiction"], chapters: [{ title: "The notification", start_s: 0 }, { title: "A week of clips", start_s: 118 }, { title: "The porch", start_s: 265 }, { title: "Tomorrow", start_s: 470 }], related_video_instruction: "Set Related video → The Doorbell Camera That Recorded Tomorrow" },
    jobs: [], templates: [{ id: "auto", label: "Auto", description: "Nightshift looks at the video and picks the layout." }, { id: "gaming_facecam", label: "Gaming with facecam", description: "Your camera on top, the game below." }, { id: "podcast_stacked", label: "Podcast, two speakers", description: "Both speakers stacked; the one talking is highlighted." }, { id: "podcast_single", label: "Podcast, one speaker", description: "Follows one face, full frame." }, { id: "talking_head", label: "Talking head", description: "Follows your face." }, { id: "center_crop", label: "Center crop", description: "Middle of the frame, blurred edges. For screens and slides." }, { id: "screen_plus_cam", label: "Screen + camera", description: "Small camera on top, the whole screen below." }],
    voices: [{ name: "Ryan", mode: "preset" }, { name: "Aiden", mode: "preset" }, { name: "Serena", mode: "preset" }, { name: "Vivian", mode: "preset" }],
    settings: { schedule_time: "03:00", home: "D:\\Nightshift", profile: "gpu", flags: { ctx: 16384, kv_type: "f16", n_gpu_layers: 99, encoder: "h264_nvenc", tts_engine: "qwentts" }, keys: { pexels: true, pixabay: true }, notifications: true },
    learn: { imported_at: "2026-10-28T09:12:00", matched: 41, unmatched: 3, summary: ["Shorts that open mid-scene are watched 23% longer than ones that open with a question.", "50-second Shorts keep more viewers than 75-second ones on After Dark Stories.", "Yellow and clean white captions perform the same; both stay in rotation."], changes: [{ pool: "hooks", option: "cold_open", delta: 0.08 }, { pool: "hooks", option: "question", delta: -0.05 }, { pool: "length_buckets", option: "50", delta: 0.06 }, { pool: "length_buckets", option: "75", delta: -0.04 }], top: [{ title: "The Laundromat That Only Opens at Night", views: 18400, viewed_pct: 71 }, { title: "The dryer was still warm when I got there", views: 9200, viewed_pct: 66 }], bottom: [{ title: "The night bus that skips my stop", views: 410, viewed_pct: 38 }] },
    logs: [{ ts: "03:12:04", level: "INFO", component: "studio", event: "stage_start", msg: "voice after-dark-20261101-a1f2" }, { ts: "03:14:40", level: "INFO", component: "studio", event: "stage_end", msg: "voice 156.2 s" }, { ts: "03:14:41", level: "WARN", component: "media", event: "clip_pool", msg: "no portrait clip for 'porch light flicker', using mood search" }],
  };
  const DEMO_POOLS = { hooks: ["cold_open", "question", "confession", "warning"], structures: ["twist", "reverse_reveal", "slow_burn", "unreliable_narrator"], caption_styles: ["punch_yellow", "clean_white"], visual_treatments: ["dark_slowzoom", "handheld_grit"] };

  // ---------------------------------------------------------------- API layer
  const api = {
    base: "/api", demo: false,
    async request(method, path, body, opts = {}) {
      if (this.demo) return demoHandle(method, path, body);
      const init = { method, headers: {} };
      if (body instanceof FormData) init.body = body; else if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
      const res = await fetch(this.base + path, init);
      if (!res.ok) { let msg = res.statusText; try { msg = (await res.json()).detail || msg; } catch {} throw new Error(msg || `HTTP ${res.status}`); }
      if (opts.raw) return res;
      return (res.headers.get("content-type") || "").includes("json") ? res.json() : res.text();
    },
    get(p, o) { return this.request("GET", p, undefined, o); }, post(p, b, o) { return this.request("POST", p, b, o); }, put(p, b) { return this.request("PUT", p, b); },
    mediaUrl(id) { return this.demo ? "" : `${this.base}/media/${encodeURIComponent(id)}`; },
  };
  NS.api = api;

  async function demoHandle(method, path, body) {
    await sleep(100);
    const [p, q] = path.split("?"); const parts = p.split("/").filter(Boolean); const sp = new URLSearchParams(q || "");
    const recount = () => { DEMO.status.outbox = { pending: DEMO.outputs.filter((o) => o.status === "pending").length, approved: DEMO.outputs.filter((o) => ["approved", "uploaded"].includes(o.status)).length, rejected: DEMO.outputs.filter((o) => o.status === "rejected").length }; };
    if (p === "/status") { recount(); return structuredClone(DEMO.status); }
    if (p === "/outputs") { const st = (sp.get("status") || "").split(",").filter(Boolean); return DEMO.outputs.filter((o) => !st.length || st.includes(o.status)).map((o) => ({ ...o })); }
    if (parts[0] === "outputs" && parts.length >= 2) {
      const isAct = ["approve", "reject", "uploaded", "open-folder"].includes(parts.at(-1));
      const id = decodeURIComponent(parts.slice(1, parts.length - (isAct ? 1 : 0)).join("/"));
      const out = DEMO.outputs.find((o) => o.output_id === id); if (!out) throw new Error("Not found");
      if (method === "POST" && isAct) { const a = parts.at(-1); if (a === "approve") out.status = "approved"; if (a === "reject") { out.status = "rejected"; out.reason = body?.reason; } if (a === "uploaded") { out.status = "uploaded"; out.url = body?.url; } recount(); return { ok: true }; }
      return { ...out, ...DEMO.detail };
    }
    if (p === "/jobs" && method === "GET") return DEMO.jobs.map((j) => ({ ...j }));
    if ((p === "/jobs/run" || p === "/jobs/clip") && method === "POST") { const kind = p.endsWith("run") ? "run" : "clip"; const job = { job_id: "j" + Math.random().toString(16).slice(2, 8), kind, channel: body?.get?.("channel") || body?.channel || "after-dark", status: "queued", progress: 0, stage: "queued", message: "Waiting for the engine", created_at: new Date().toISOString() }; DEMO.jobs.unshift(job); simulateJob(job); return { job_id: job.job_id }; }
    if (parts[0] === "jobs" && parts.length === 2) { const j = DEMO.jobs.find((x) => x.job_id === parts[1]); if (!j) throw new Error("Not found"); return { ...j }; }
    if (parts[0] === "jobs" && parts[2] === "cancel") { const j = DEMO.jobs.find((x) => x.job_id === parts[1]); if (j) { j.status = "cancelled"; j.message = "Cancelled"; } return { ok: true }; }
    if (p === "/channels" && method === "GET") return DEMO.status.channels.map((c) => ({ ...c, pools: DEMO_POOLS, language: "en", long_minutes: [10, 14], shorts_per_master: [4, 6], audience: "general", fiction_label: true }));
    if (p === "/channels" && method === "POST") { DEMO.status.channels.push({ ...body, voice: body.voice?.speaker, status: "active" }); return { ok: true }; }
    if (parts[0] === "channels" && parts.length >= 2) { const ch = DEMO.status.channels.find((c) => c.slug === parts[1]); if (parts[2] === "pause" && ch) ch.status = "paused"; if (parts[2] === "resume" && ch) ch.status = "active"; if (parts[2] === "validate") return { ok: true, warnings: [] }; if (method === "PUT" && ch) Object.assign(ch, body, { voice: body.voice?.speaker || ch.voice }); return { ok: true }; }
    if (p === "/templates") return DEMO.templates; if (p === "/voices") return DEMO.voices; if (p === "/voices/preview") return null;
    if (p === "/settings") { if (method === "PUT") Object.assign(DEMO.settings, body); return structuredClone(DEMO.settings); }
    if (p === "/settings/keys") { DEMO.settings.keys[body.name] = true; return { ok: true }; }
    if (p === "/diagnostics") return { path: "C:\\Users\\you\\Desktop\\nightshift-diagnostics-2026-11-01.zip" };
    if (p === "/engine/shutdown") { DEMO.status.engine = { state: "idle", detail: "Engine stopped", elapsed_s: null, vram_owner: null }; return { ok: true }; }
    if (p === "/learn/summary") return structuredClone(DEMO.learn);
    if (p === "/learn/import") { DEMO.learn.imported_at = new Date().toISOString(); return structuredClone(DEMO.learn); }
    if (p.startsWith("/logs/tail")) return DEMO.logs;
    throw new Error(`Not available in demo: ${method} ${path}`);
  }
  function simulateJob(job) {
    const stages = job.kind === "clip" ? ["import", "audio", "transcribe", "select", "detect", "captions", "render", "qc", "outbox"] : ["ideas", "plan", "draft", "critic_revise", "hooks", "seo", "visual_plan", "voice", "captions", "clips", "render", "qc", "outbox"];
    let i = 0; DEMO.status.engine = { state: "working", detail: `Working: ${stageWords(stages[0])} (1 of ${stages.length})`, elapsed_s: 0, vram_owner: "studio" };
    const tick = setInterval(() => {
      if (job.status === "cancelled") { clearInterval(tick); DEMO.status.engine = { state: "ready", detail: "Ready", elapsed_s: null, vram_owner: null }; return; }
      job.status = "running"; job.stage = stages[i]; job.step = i + 1; job.steps = stages.length; job.message = stageWords(stages[i]); job.progress = Math.round(((i + 1) / stages.length) * 100);
      DEMO.status.engine.detail = `Working: ${job.message} (${i + 1} of ${stages.length})`; i++;
      if (i >= stages.length) {
        clearInterval(tick); job.status = "done"; job.progress = 100; job.message = "Done"; DEMO.status.engine = { state: "ready", detail: "Ready", elapsed_s: null, vram_owner: null };
        const n = job.kind === "clip" ? 3 : 1; const ids = [];
        for (let k = 1; k <= n; k++) { const id = `${job.kind}-${job.job_id}/${job.kind === "clip" ? "c" + k : "s" + k}`; DEMO.outputs.unshift({ output_id: id, kind: job.kind === "clip" ? "clip" : "short", k, title: job.kind === "clip" ? `Clip ${k}: the moment everyone replays` : "The night the hallway light answered back", channel: job.channel, duration_s: 40 + k * 7, created_at: new Date().toISOString(), status: "pending", path: `D:\\Nightshift\\outbox\\${job.channel}\\today\\${id.replace("/", "\\")}.mp4` }); ids.push(id); }
        job.result = { output_ids: ids };
      }
    }, 1300);
  }

  // ---------------------------------------------------------------- state + strip
  const state = NS.state; state.status = null; state.route = "queue";
  async function refreshStatus() {
    try { if (!api.demo) { const res = await fetch(api.base + "/status"); if (!res.ok) throw new Error(); state.status = await res.json(); } else state.status = await api.get("/status"); }
    catch { if (!api.demo) { api.demo = true; $("#demo-flag").hidden = false; state.status = await api.get("/status"); } }
    renderStrip();
  }
  function renderStrip() {
    const s = state.status; if (!s) return; const e = s.engine || {};
    const pill = $("#engine-pill"); pill.dataset.state = e.state || "idle";
    let text = e.detail || ({ idle: "Idle", starting: "Starting", loading: "Loading model", ready: "Ready", working: "Working", error: "Engine problem" }[e.state] || "Engine");
    if (e.state === "loading" && e.elapsed_s != null) text += ` · ${e.elapsed_s}s`;
    $("#engine-text").textContent = text;
    $("#next-run").textContent = s.next_run ? `Next run ${fmtWhen(s.next_run)}` : "Automatic runs off";
    const pending = s.outbox?.pending ?? 0; const c = $("#nav-pending"); c.textContent = String(pending); c.hidden = pending === 0;
  }
  function connectEvents() {
    if (api.demo || !("EventSource" in window)) return;
    try { const es = new EventSource(api.base + "/events"); es.onmessage = (ev) => { try { const m = JSON.parse(ev.data); if (m.kind === "status") { state.status = m.payload; renderStrip(); } } catch {} }; es.onerror = () => { es.close(); setTimeout(connectEvents, 5000); }; } catch {}
  }

  // ---------------------------------------------------------------- router
  const ROUTES = { "": "queue", queue: "queue", review: "queue", create: "create", channels: "channels", results: "results", settings: "settings" };
  let pageCleanup = null;
  async function render() {
    const [name, query] = location.hash.replace(/^#\/?/, "").split("?");
    const route = ROUTES[name] ?? "queue"; state.route = route; state.query = new URLSearchParams(query || "");
    $$(".nav-list a").forEach((a) => { if (a.dataset.route === route) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    if (pageCleanup) { try { pageCleanup(); } catch {} pageCleanup = null; }
    const main = $("#main"); main.innerHTML = `<div class="page"><p class="muted">Loading…</p></div>`;
    try { const page = NS.pages[route]; pageCleanup = (await page(main)) || null; }
    catch (err) { main.innerHTML = `<div class="page"><h1>Something went wrong</h1><p class="lead">${esc(err.message)}</p><div class="btn-row"><button class="btn" id="reload-btn" type="button">Reload</button></div></div>`; $("#reload-btn", main).onclick = () => location.reload(); }
    main.focus({ preventScroll: true });
  }

  // ---------------------------------------------------------------- shared helpers
  const header = (title, lead, actions) => `<div class="page-h"><div><h1>${esc(title)}</h1>${lead ? `<p class="lead">${esc(lead)}</p>` : ""}</div>${actions ? `<div class="btn-row">${actions}</div>` : ""}</div>`;
  const channelName = (slug) => state.status?.channels?.find((c) => c.slug === slug)?.display_name || slug || "";
  function progressBlock(job) {
    const cls = job.status === "done" ? "done" : ["failed", "cancelled"].includes(job.status) ? "bad" : "";
    const label = job.status === "done" ? "Done" : job.status === "failed" ? `Stopped: ${job.message || "something failed"}` : job.status === "cancelled" ? "Cancelled" : stageWords(job.stage || job.status);
    const step = job.step && job.steps ? ` · step ${job.step} of ${job.steps}` : "";
    return `<div class="card" data-job="${esc(job.job_id)}"><div class="stage-line"><span class="stage">${esc(label)}</span><span class="muted num">${job.kind === "clip" ? "Clips" : "Story video"}${step}</span></div><div class="progress ${cls}" style="margin-top:10px"><span style="width:${job.progress ?? 0}%"></span></div><div class="btn-row" style="margin-top:14px">${job.status === "done" ? `<a class="btn btn-primary" href="#/">Review it</a>` : ""}${["running", "queued"].includes(job.status) ? `<button class="btn btn-ghost" data-cancel="${esc(job.job_id)}" type="button">Cancel</button>` : ""}</div></div>`;
  }
  function watchJob(jobId, onUpdate) {
    let stopped = false;
    (async () => { while (!stopped) { try { const j = await api.get(`/jobs/${encodeURIComponent(jobId)}`); onUpdate(j); if (["done", "failed", "cancelled"].includes(j.status)) { refreshStatus(); break; } } catch (e) { onUpdate({ job_id: jobId, status: "failed", message: e.message, progress: 0 }); break; } await sleep(1500); } })();
    return () => { stopped = true; };
  }
  Object.assign(NS.util, { header, channelName, progressBlock, watchJob, refreshStatus });

  // ---------------------------------------------------------------- Queue: watch → approve → post, in YouTube's order
  NS.pages.queue = async function (main) {
    await refreshStatus();
    let tab = state.query.get("tab") || "ready";
    const STATUS = { ready: "pending", posted: "approved,uploaded", rejected: "rejected" };
    let items = await api.get(`/outputs?status=${STATUS[tab]}`);
    let current = state.query.get("id") && items.find((o) => o.output_id === state.query.get("id")) ? state.query.get("id") : items[0]?.output_id || null;
    let onKey = null;

    const tabsHtml = () => `<div class="tabs" role="tablist">${[["ready", "Ready", state.status.outbox?.pending], ["posted", "Approved & posted", state.status.outbox?.approved], ["rejected", "Rejected", state.status.outbox?.rejected]].map(([k, l, n]) => `<button role="tab" data-tab="${k}" aria-selected="${tab === k}" type="button">${l}${n != null ? ` (${n})` : ""}</button>`).join("")}</div>`;

    const drawList = () => {
      main.innerHTML = `<div class="page">${header(tab === "posted" ? "Approved and posted" : "Rejected", tab === "posted" ? "Approved videos wait in the Outbox folder until you post them. Posted ones have a YouTube link." : "Rejected videos are kept for 7 days in the rejected folder, then deleted.")}${tabsHtml()}
        ${items.length ? `<div class="list">${items.map((o) => `<div class="row"><span class="badge ${esc(o.kind)}">${kindLabel(o.kind)}</span><div><div class="row-title">${esc(o.title)}</div><div class="row-sub">${esc(channelName(o.channel))} · ${fmtWhen(o.created_at)}${o.status === "uploaded" && o.url ? ` · <a href="${esc(o.url)}" target="_blank" rel="noopener">on YouTube</a>` : o.status === "approved" ? " · not posted yet" : ""}${o.reason ? ` · reason: ${esc(o.reason)}` : ""}</div></div>${o.status === "approved" ? `<button class="btn" data-resume="${esc(o.output_id)}" type="button">Finish posting</button>` : `<span class="num muted">${fmtDur(o.duration_s)}</span>`}</div>`).join("")}</div>` : `<div class="empty"><strong>Nothing here</strong></div>`}</div>`;
    };

    const drawReady = async () => {
      if (!items.length) {
        main.innerHTML = `<div class="page">${header("Nothing to review", "Everything made so far is approved or posted. New videos appear here after tonight's run.")}${tabsHtml()}<div class="empty"><strong>Make something now?</strong><a class="btn btn-primary btn-lg" href="#/create">Create</a></div></div>`; return;
      }
      const o = items.find((x) => x.output_id === current) || items[0]; current = o.output_id;
      const d = await api.get(`/outputs/${encodeURIComponent(o.output_id)}`);
      const prog = store.get("ns-post-" + o.output_id) || {};
      const approved = o.status !== "pending" || prog.approved;
      const chapters = d.chapters?.length ? "\n\n" + d.chapters.map((c) => `${fmtDur(c.start_s)} ${c.title}`).join("\n") : "";
      const description = (d.description || "") + chapters + (d.hashtags?.length ? "\n\n" + d.hashtags.join(" ") : "");
      const idx = items.indexOf(o);
      const step = (n, title, sub, actionHtml, key) => `<div class="check ${prog[key] ? "done" : ""}" data-step="${key}"><span class="cn">${prog[key] ? "✓" : n}</span><div><div class="ct">${esc(title)}</div>${sub ? `<div class="cs">${esc(sub)}</div>` : ""}</div>${actionHtml || ""}</div>`;

      main.innerHTML = `<div class="page">
        ${header(`${items.length} ready to post`, `${idx + 1} of ${items.length} · ${kindLabel(o.kind)} for ${channelName(o.channel)}`)}
        ${tabsHtml()}
        <div class="queue">
          <div class="player-wrap">
            <div class="big-title">${esc(o.title)}</div>
            <div class="player ${o.kind === "long" ? "landscape" : ""}">${api.demo ? `<div class="muted" style="padding:20px;text-align:center">Video plays here<br><span class="small">(demo: no engine connected)</span></div>` : `<video controls preload="metadata" src="${api.mediaUrl(o.output_id)}" poster="${api.mediaUrl(o.output_id)}/thumb.jpg"></video>`}</div>
            <div class="player-meta"><span class="num muted">${fmtDur(o.duration_s)}</span><span class="muted">made ${fmtWhen(o.created_at)}</span><span class="spacer"></span><button class="btn btn-ghost" data-act="skip" type="button">Next video →</button></div>
            <details class="section"><summary>The story and why it passed checks</summary><div class="section-body"><p style="margin:0">${esc(d.story_excerpt || "")}</p><ul style="margin:0;padding-left:18px">${(d.policy_notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div></details>
            <div class="filmstrip">${items.map((it) => `<button type="button" data-pick="${esc(it.output_id)}" aria-current="${it.output_id === current}"><span class="badge ${esc(it.kind)}">${kindLabel(it.kind)}</span><span class="ft">${esc(it.title)}</span><span class="small muted num">${fmtDur(it.duration_s)}</span></button>`).join("")}</div>
          </div>
          <aside class="checklist">
            ${approved
              ? step(1, "Approved", "Ready to post.", `<button class="btn btn-ghost" data-act="unapprove" type="button" title="Mark as not reviewed">Undo</button>`, "approved")
              : `<div class="check wide"><span class="cn">1</span><div class="cbody"><div class="ct">Watch it, then decide</div><div class="btn-row"><button class="btn btn-ok btn-lg" data-act="approve" type="button">Approve</button><button class="btn btn-danger-outline" data-act="reject" type="button">Reject</button></div><div class="cs">Keys: A approve · R reject · → next</div></div></div>`}
            ${step(2, "Open the video file", o.path ? o.path : "In the Outbox folder.", `<button class="btn" data-act="folder" type="button">Open folder</button>`, "folder")}
            ${step(3, "Open YouTube Studio and start an upload", "Drag the file in.", `<a class="btn" href="https://studio.youtube.com/" target="_blank" rel="noopener" data-mark="studio">Open</a>`, "studio")}
            ${step(4, "Title", o.title, `<button class="btn" data-copy="title" type="button">Copy</button>`, "title")}
            ${step(5, "Description", d.chapters?.length ? "Includes chapters and hashtags." : "Includes hashtags.", `<button class="btn" data-copy="description" type="button">Copy</button>`, "description")}
            ${o.kind === "long" ? step(6, "Thumbnail", "Pick one of the three made for this video.", api.demo ? `<button class="btn" disabled type="button">Open</button>` : `<a class="btn" href="${api.mediaUrl(o.output_id)}/thumb.jpg" target="_blank" rel="noopener" data-mark="thumb">Open</a>`, "thumb") : ""}
            ${step(o.kind === "long" ? 7 : 6, "Audience", d.made_for_kids ? "Yes, made for kids" : "No, it's not made for kids", `<button class="btn btn-ghost" data-mark="audience" type="button">Done</button>`, "audience")}
            ${step(o.kind === "long" ? 8 : 7, "Altered content", d.ai_disclosure === "yes" ? "Answer Yes" : "Answer No (voice is production assistance; footage is real stock)", `<button class="btn btn-ghost" data-mark="altered" type="button">Done</button>`, "altered")}
            ${step(o.kind === "long" ? 9 : 8, "Tags", `${(d.tags || []).length} tags`, `<button class="btn" data-copy="tags" type="button">Copy</button>`, "tags")}
            ${step(o.kind === "long" ? 10 : 9, o.kind === "long" ? "Schedule" : "Schedule and related video", (d.publish_after ? `Publish ${fmtWhen(d.publish_after)}` : "Any time") + (d.related_video_instruction && o.kind !== "long" ? ` · ${d.related_video_instruction}` : ""), `<button class="btn btn-ghost" data-mark="schedule" type="button">Done</button>`, "schedule")}
            <div class="check wide"><span class="cn">${o.kind === "long" ? 11 : 10}</span><div class="cbody"><div class="ct">Paste the YouTube link</div><div class="btn-row"><input class="input" id="yt-url" placeholder="https://youtu.be/…" style="flex:1;min-width:180px"><button class="btn btn-primary" data-act="done" type="button" ${approved ? "" : "disabled"}>Done</button></div><div class="cs">Saving the link lets Nightshift learn from this video's results.</div></div></div>
          </aside>
        </div></div>`;

      const mark = (key, val = true) => { prog[key] = val; store.set("ns-post-" + o.output_id, prog); const el = $(`[data-step="${key}"]`, main); if (el) { el.classList.toggle("done", !!val); el.querySelector(".cn").textContent = val ? "✓" : el.querySelector(".cn").textContent; } };
      const act = async (what) => {
        if (what === "skip") { current = items[(idx + 1) % items.length]?.output_id || null; return drawReady(); }
        if (what === "approve") { await api.post(`/outputs/${encodeURIComponent(o.output_id)}/approve`); mark("approved"); toast("Approved", "ok"); await refreshStatus(); return drawReady(); }
        if (what === "unapprove") { mark("approved", false); return drawReady(); }
        if (what === "reject") { const reason = await modal({ title: "Reject this video?", body: "A short reason helps Nightshift do better next time.", okLabel: "Reject", danger: true, input: { label: "Reason", placeholder: "e.g. the ending made no sense" } }); if (reason === null) return; await api.post(`/outputs/${encodeURIComponent(o.output_id)}/reject`, { reason }); toast("Rejected", "warn"); items = await api.get(`/outputs?status=pending`); await refreshStatus(); current = items[Math.min(idx, items.length - 1)]?.output_id || null; return drawReady(); }
        if (what === "folder") { try { await api.post(`/outputs/${encodeURIComponent(o.output_id)}/open-folder`); } catch { if (o.path) await copyText(o.path, "File path"); } mark("folder"); return; }
        if (what === "done") { const url = $("#yt-url", main).value.trim(); if (!/^https?:\/\//.test(url)) return toast("Paste the full YouTube link first", "warn"); await api.post(`/outputs/${encodeURIComponent(o.output_id)}/uploaded`, { url }); toast("Posted and saved", "ok"); items = await api.get(`/outputs?status=pending`); await refreshStatus(); current = items[Math.min(idx, items.length - 1)]?.output_id || null; return drawReady(); }
      };
      main.onclick = (e) => {
        const t = e.target.closest("button, a[data-mark]"); if (!t) return;
        if (t.dataset.tab) { tab = t.dataset.tab; return load(); }
        if (t.dataset.act) return act(t.dataset.act).catch((err) => toast(err.message, "bad"));
        if (t.dataset.pick) { current = t.dataset.pick; return drawReady(); }
        if (t.dataset.mark) { mark(t.dataset.mark); return; }
        if (t.dataset.copy) { const v = t.dataset.copy === "title" ? o.title : t.dataset.copy === "description" ? description : (d.tags || []).join(", "); copyText(v, t.dataset.copy[0].toUpperCase() + t.dataset.copy.slice(1)).then((ok) => ok && mark(t.dataset.copy)); }
      };
      onKey = (e) => { if (e.target.closest("input,textarea")) return; const k = e.key.toLowerCase(); if (k === "a" && !approved) act("approve"); else if (k === "r" && !approved) act("reject"); else if (e.key === "ArrowRight") act("skip"); };
    };

    const load = async () => { items = await api.get(`/outputs?status=${STATUS[tab]}`); current = items[0]?.output_id || null; if (tab === "ready") await drawReady(); else { drawList(); main.onclick = (e) => { const t = e.target.closest("button"); if (!t) return; if (t.dataset.tab) { tab = t.dataset.tab; load(); } if (t.dataset.resume) { tab = "ready"; location.hash = `#/?id=${encodeURIComponent(t.dataset.resume)}`; } }; onKey = null; } };
    const keyProxy = (e) => onKey && onKey(e);
    document.addEventListener("keydown", keyProxy);
    if (tab === "ready") await drawReady(); else await load();
    return () => document.removeEventListener("keydown", keyProxy);
  };

  // ---------------------------------------------------------------- strip, theme, boot
  $("#quit-btn").onclick = async () => { const ok = await modal({ title: "Quit Nightshift?", body: "Stops the engine and frees the graphics card. Anything in progress stops within 30 seconds. Tonight's automatic run still happens.", okLabel: "Quit", danger: true }); if (!ok) return; try { await api.post("/engine/shutdown", { grace_s: 30 }); toast("Shutting down. You can close this tab.", "ok"); refreshStatus(); } catch (e) { toast(e.message, "bad"); } };
  $("#theme-toggle").onclick = () => { const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"); const next = cur === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; store.set("ns-theme", next); };
  const savedTheme = store.get("ns-theme"); if (savedTheme) document.documentElement.dataset.theme = savedTheme;
  window.addEventListener("hashchange", render);
  document.addEventListener("DOMContentLoaded", async () => { await refreshStatus(); connectEvents(); render(); setInterval(() => { if (document.visibilityState === "visible") refreshStatus(); }, api.demo ? 2000 : 10000); });
})();
