/* Nightshift Dashboard — core: API layer with demo fallback, state, router, shared UI, Today + Review pages.
   Page modules for Make videos, Make clips, Channels, Learn and Settings live in app-pages.js and register
   themselves on window.NS.pages. No frameworks, no network beyond the local API. */
(function () {
  "use strict";

  const NS = (window.NS = { pages: {}, state: {}, util: {} });

  // ------------------------------------------------------------------ utilities
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtDur = (s) => { s = Math.round(Number(s) || 0); const m = Math.floor(s / 60), r = s % 60; return `${m}:${String(r).padStart(2, "0")}`; };
  const fmtWhen = (iso) => { if (!iso) return ""; const d = new Date(iso); if (isNaN(d)) return String(iso); return d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  Object.assign(NS.util, { $, $$, esc, fmtDur, fmtWhen, sleep });

  const STAGE_WORDS = {
    ideas: "Finding an idea", plan: "Planning the story", draft: "Writing the story", critic_revise: "Editing the story",
    split: "Cutting into parts", hooks: "Writing the opening line", seo: "Writing title and tags", visual_plan: "Planning footage",
    policy_text: "Safety check", voice: "Recording narration", duration_check: "Checking length", captions: "Adding captions",
    shorts_timing: "Picking the best moments", clips: "Finding footage", render: "Rendering", render_master: "Rendering the full video",
    thumbnails: "Making thumbnails", shorts_derive: "Cutting the Shorts", qc: "Checking quality", policy_final: "Final check",
    outbox: "Saving to the Outbox", import: "Importing the video", audio: "Extracting audio", transcribe: "Transcribing",
    select: "Choosing moments", detect: "Finding faces", seo_clip: "Writing titles", queued: "Waiting for the engine",
  };
  const stageWords = (k) => STAGE_WORDS[k] || (k ? String(k).replace(/_/g, " ") : "Working");
  NS.util.stageWords = stageWords;

  // ------------------------------------------------------------------ toasts, modal
  function toast(msg, kind) {
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }
  function modal({ title, body, okLabel = "OK", cancelLabel = "Cancel", danger = false, input = null }) {
    return new Promise((resolve) => {
      const root = $("#modal");
      root.hidden = false;
      root.innerHTML = `
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
          <h2 id="modal-title">${esc(title)}</h2>
          ${body ? `<p class="muted">${esc(body)}</p>` : ""}
          ${input ? `<div class="field"><label for="modal-input">${esc(input.label)}</label><input id="modal-input" class="input" placeholder="${esc(input.placeholder || "")}"></div>` : ""}
          <div class="btn-row">
            <button class="btn btn-ghost" data-act="cancel" type="button">${esc(cancelLabel)}</button>
            <button class="btn ${danger ? "btn-danger-outline" : "btn-primary"}" data-act="ok" type="button">${esc(okLabel)}</button>
          </div>
        </div>`;
      const close = (val) => { root.hidden = true; root.innerHTML = ""; resolve(val); };
      root.onclick = (e) => {
        const act = e.target.closest("[data-act]")?.dataset.act;
        if (act === "cancel" || e.target === root) close(null);
        if (act === "ok") close(input ? ($("#modal-input").value || "") : true);
      };
      root.onkeydown = (e) => { if (e.key === "Escape") close(null); if (e.key === "Enter" && input) close($("#modal-input").value || ""); };
      (input ? $("#modal-input") : $('[data-act="ok"]', root)).focus();
    });
  }
  async function copyText(text, label) {
    try { await navigator.clipboard.writeText(text); toast(`${label || "Text"} copied`, "ok"); }
    catch { const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); toast(`${label || "Text"} copied`, "ok"); } catch { toast("Select the text and press Ctrl+C", "warn"); } ta.remove(); }
  }
  Object.assign(NS.util, { toast, modal, copyText });

  // ------------------------------------------------------------------ demo data (used only when no engine answers)
  const DEMO = {
    status: {
      engine: { state: "idle", detail: "Services idle", elapsed_s: null, vram_owner: null },
      next_run: new Date(Date.now() + 9 * 3600e3).toISOString(),
      outbox: { pending: 3, approved: 14, rejected: 2 },
      channels: [
        { slug: "after-dark", display_name: "After Dark Stories", status: "active", niche: "original short horror fiction set in everyday places", voice: "Ryan", masters_per_week: 1, publish_window: "18:00-21:00" },
        { slug: "tiny-mysteries", display_name: "Tiny Mysteries", status: "paused", niche: "small-town mysteries with a twist", voice: "Aiden", masters_per_week: 2, publish_window: "17:00-20:00" },
      ],
      services: { llama: { state: "stopped" }, embed: { state: "stopped" }, tts: { state: "stopped" }, dashboard: { state: "ready" } },
      disk: { free_gb: 412, total_gb: 931, home: "D:\\Nightshift" },
    },
    outputs: [
      { output_id: "after-dark-20261101-a1f2/long", kind: "long", title: "The Doorbell Camera That Recorded Tomorrow", channel: "after-dark", duration_s: 731, created_at: "2026-11-01T03:41:00", status: "pending" },
      { output_id: "after-dark-20261101-a1f2/s1", kind: "short", k: 1, title: "My doorbell camera records someone every night at 3:12", channel: "after-dark", duration_s: 52, created_at: "2026-11-01T03:52:00", status: "pending" },
      { output_id: "clip-7f3a/c2", kind: "clip", k: 2, title: "Why the first ten minutes of a stream decide everything", channel: "tiny-mysteries", duration_s: 38, created_at: "2026-11-01T11:12:00", status: "pending" },
    ],
    detail: {
      description: "A night-shift nurse stops checking her doorbell clips. Tonight the notification arrives while she is standing on the porch.\n\nOriginal fiction.",
      tags: ["horror story", "scary story", "doorbell camera", "short horror", "creepy story", "narrated story", "fiction", "suspense", "night shift", "porch"],
      hashtags: ["#horrorstory", "#scarystories", "#fiction"],
      publish_after: new Date(Date.now() + 30 * 3600e3).toISOString(),
      ai_disclosure: "no", made_for_kids: false, age_restrict_suggested: false,
      story_excerpt: "The doorbell camera pings at 3:12 every night. I stopped checking the clips a week ago. Tonight my phone buzzed while I was standing on the porch, and the clip was already playing…",
      policy_notes: ["No real people, places or brands named", "Title matches what happens in the story", "Marked as original fiction in the description"],
      chapters: [{ title: "The notification", start_s: 0 }, { title: "A week of clips", start_s: 118 }, { title: "The porch", start_s: 265 }, { title: "Tomorrow", start_s: 470 }],
    },
    jobs: [],
    templates: [
      { id: "auto", label: "Auto", description: "Nightshift looks at the video and picks the layout." },
      { id: "gaming_facecam", label: "Gaming with facecam", description: "Your camera on top, the game below." },
      { id: "podcast_stacked", label: "Podcast, two speakers", description: "Both speakers stacked, the one talking is highlighted." },
      { id: "podcast_single", label: "Podcast, one speaker", description: "Follows one face, full frame." },
      { id: "talking_head", label: "Talking head", description: "Follows your face through the clip." },
      { id: "center_crop", label: "Center crop", description: "Middle of the frame, blurred edges. For screens and slides." },
      { id: "screen_plus_cam", label: "Screen + camera", description: "Small camera on top, the whole screen below." },
    ],
    voices: [{ name: "Ryan", mode: "preset" }, { name: "Aiden", mode: "preset" }, { name: "Serena", mode: "preset" }, { name: "Vivian", mode: "preset" }, { name: "Velvet", mode: "design" }],
    settings: { schedule_time: "03:00", home: "D:\\Nightshift", profile: "gpu", flags: { ctx: 16384, kv_type: "f16", n_gpu_layers: 99, encoder: "h264_nvenc", tts_engine: "qwentts" }, keys: { pexels: true, pixabay: true }, notifications: true },
    learn: { imported_at: "2026-10-28T09:12:00", summary: ["Shorts that open mid-scene are watched 23% longer than ones that open with a question.", "The 50-second length keeps more viewers than 75 seconds on After Dark Stories.", "Yellow captions and clean white captions perform the same; both stay in rotation."], changes: [{ pool: "hooks", option: "cold_open", delta: 0.08 }, { pool: "hooks", option: "question", delta: -0.05 }, { pool: "length_buckets", option: "50", delta: 0.06 }, { pool: "length_buckets", option: "75", delta: -0.04 }] },
    logs: [{ ts: "03:12:04", level: "INFO", component: "studio", event: "stage_start", msg: "voice after-dark-20261101-a1f2" }, { ts: "03:14:40", level: "INFO", component: "studio", event: "stage_end", msg: "voice 156.2 s" }, { ts: "03:14:41", level: "WARN", component: "media", event: "clip_pool", msg: "no portrait clip for 'porch light flicker', using mood search" }],
  };

  // ------------------------------------------------------------------ API layer
  const api = {
    base: "/api", demo: false,
    async request(method, path, body, opts = {}) {
      if (this.demo) return demoHandle(method, path, body);
      const init = { method, headers: {} };
      if (body instanceof FormData) init.body = body;
      else if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
      const res = await fetch(this.base + path, init);
      if (!res.ok) { let msg = res.statusText; try { msg = (await res.json()).detail || msg; } catch {} throw new Error(msg || `HTTP ${res.status}`); }
      if (opts.raw) return res;
      const ct = res.headers.get("content-type") || "";
      return ct.includes("json") ? res.json() : res.text();
    },
    get(p, o) { return this.request("GET", p, undefined, o); },
    post(p, b, o) { return this.request("POST", p, b, o); },
    put(p, b) { return this.request("PUT", p, b); },
    mediaUrl(id) { return this.demo ? "" : `${this.base}/media/${encodeURIComponent(id)}`; },
  };
  NS.api = api;

  // Demo handler: mutates DEMO in memory so the mockup behaves like the real thing.
  async function demoHandle(method, path, body) {
    await sleep(120);
    const [p, q] = path.split("?");
    const parts = p.split("/").filter(Boolean);
    const sp = new URLSearchParams(q || "");
    if (p === "/status") return structuredClone(DEMO.status);
    if (p === "/outputs") { const st = sp.get("status"); return DEMO.outputs.filter((o) => !st || o.status === st).map((o) => ({ ...o })); }
    if (parts[0] === "outputs" && parts.length >= 2) {
      const id = decodeURIComponent(parts.slice(1, parts.length - (["approve", "reject", "uploaded"].includes(parts.at(-1)) ? 1 : 0)).join("/"));
      const out = DEMO.outputs.find((o) => o.output_id === id);
      if (!out) throw new Error("Not found");
      const act = parts.at(-1);
      if (method === "POST" && ["approve", "reject", "uploaded"].includes(act)) {
        out.status = act === "approve" ? "approved" : act === "reject" ? "rejected" : "uploaded";
        if (act === "approve") DEMO.status.outbox.approved++; if (act === "reject") DEMO.status.outbox.rejected++;
        DEMO.status.outbox.pending = DEMO.outputs.filter((o) => o.status === "pending").length;
        return { ok: true };
      }
      return { ...out, ...DEMO.detail };
    }
    if (p === "/jobs" && method === "GET") return DEMO.jobs.map((j) => ({ ...j }));
    if ((p === "/jobs/run" || p === "/jobs/clip") && method === "POST") {
      const kind = p.endsWith("run") ? "run" : "clip";
      const job = { job_id: "j" + Math.random().toString(16).slice(2, 8), kind, channel: (body?.get?.("channel") || body?.channel || "after-dark"), status: "queued", progress: 0, stage: "queued", message: "Waiting for the engine", created_at: new Date().toISOString() };
      DEMO.jobs.unshift(job); simulateJob(job); return { job_id: job.job_id };
    }
    if (parts[0] === "jobs" && parts.length === 2) { const j = DEMO.jobs.find((x) => x.job_id === parts[1]); if (!j) throw new Error("Not found"); return { ...j }; }
    if (parts[0] === "jobs" && parts[2] === "cancel") { const j = DEMO.jobs.find((x) => x.job_id === parts[1]); if (j) { j.status = "cancelled"; j.message = "Cancelled"; } return { ok: true }; }
    if (p === "/channels" && method === "GET") return DEMO.status.channels.map((c) => ({ ...c, pools: DEMO_POOLS, language: "en", long_minutes: [10, 14], shorts_per_master: [4, 6], audience: "general", fiction_label: true }));
    if (parts[0] === "channels" && method === "POST" && parts.length === 1) { DEMO.status.channels.push({ ...body, status: "active" }); return { ok: true }; }
    if (parts[0] === "channels" && parts.length >= 2) {
      const ch = DEMO.status.channels.find((c) => c.slug === parts[1]);
      if (parts[2] === "pause" && ch) ch.status = "paused"; if (parts[2] === "resume" && ch) ch.status = "active";
      if (parts[2] === "validate") return { ok: true, warnings: [] };
      if (method === "PUT" && ch) Object.assign(ch, body);
      return { ok: true };
    }
    if (p === "/templates") return DEMO.templates;
    if (p === "/voices") return DEMO.voices;
    if (p === "/voices/preview") return null;
    if (p === "/settings") { if (method === "PUT") Object.assign(DEMO.settings, body); return structuredClone(DEMO.settings); }
    if (p === "/settings/keys") { DEMO.settings.keys[body.name] = true; return { ok: true }; }
    if (p === "/diagnostics") return { path: "C:\\Users\\you\\Desktop\\nightshift-diagnostics-2026-11-01.zip" };
    if (p === "/engine/warmup") { DEMO.status.engine = { state: "loading", detail: "Loading LLM (9 GB)", elapsed_s: 0, vram_owner: "warmup" }; setTimeout(() => (DEMO.status.engine = { state: "ready", detail: "Ready", elapsed_s: null, vram_owner: "idle" }), 6000); return { ok: true }; }
    if (p === "/engine/shutdown") { DEMO.status.engine = { state: "idle", detail: "Engine stopped", elapsed_s: null, vram_owner: null }; return { ok: true }; }
    if (p === "/learn/summary") return structuredClone(DEMO.learn);
    if (p === "/learn/import") { DEMO.learn.imported_at = new Date().toISOString(); return { matched: 41, unmatched: 3, ...DEMO.learn }; }
    if (p.startsWith("/logs/tail")) return DEMO.logs;
    throw new Error(`Demo: ${method} ${path} not available`);
  }
  const DEMO_POOLS = { hooks: ["cold_open", "question", "confession", "warning"], structures: ["twist", "reverse_reveal", "slow_burn", "unreliable_narrator"], caption_styles: ["punch_yellow", "clean_white"], visual_treatments: ["dark_slowzoom", "handheld_grit"] };
  function simulateJob(job) {
    const stages = job.kind === "clip" ? ["import", "audio", "transcribe", "select", "detect", "captions", "render", "qc", "outbox"]
      : ["ideas", "plan", "draft", "critic_revise", "hooks", "seo", "visual_plan", "voice", "captions", "clips", "render", "qc", "outbox"];
    let i = 0;
    DEMO.status.engine = { state: "working", detail: stageWords(stages[0]), elapsed_s: 0, vram_owner: "studio" };
    const tick = setInterval(() => {
      if (job.status === "cancelled") { clearInterval(tick); DEMO.status.engine = { state: "ready", detail: "Ready", elapsed_s: null, vram_owner: "idle" }; return; }
      job.status = "running"; job.stage = stages[i]; job.message = stageWords(stages[i]); job.progress = Math.round(((i + 1) / stages.length) * 100);
      DEMO.status.engine.detail = job.message;
      i++;
      if (i >= stages.length) {
        clearInterval(tick); job.status = "done"; job.progress = 100; job.message = "Done";
        DEMO.status.engine = { state: "ready", detail: "Ready", elapsed_s: null, vram_owner: "idle" };
        const n = job.kind === "clip" ? 3 : 1;
        const ids = [];
        for (let k = 1; k <= n; k++) {
          const id = `${job.kind}-${job.job_id}/${job.kind === "clip" ? "c" + k : "s" + k}`;
          DEMO.outputs.unshift({ output_id: id, kind: job.kind === "clip" ? "clip" : "short", k, title: job.kind === "clip" ? `Clip ${k}: the moment everyone replays` : "The night the hallway light answered back", channel: job.channel, duration_s: 40 + k * 7, created_at: new Date().toISOString(), status: "pending" });
          ids.push(id);
        }
        job.result = { output_ids: ids };
        DEMO.status.outbox.pending = DEMO.outputs.filter((o) => o.status === "pending").length;
      }
    }, 1400);
  }

  // ------------------------------------------------------------------ state + status strip
  const state = NS.state;
  state.status = null; state.route = "today"; state.selectedOutput = null;

  async function refreshStatus() {
    try {
      if (!api.demo) {
        const res = await fetch(api.base + "/status");
        if (!res.ok) throw new Error("status " + res.status);
        state.status = await res.json();
      } else state.status = await api.get("/status");
    } catch {
      if (!api.demo) { api.demo = true; $("#demo-flag").hidden = false; state.status = await api.get("/status"); }
    }
    renderStrip();
  }
  function renderStrip() {
    const s = state.status; if (!s) return;
    const e = s.engine || {};
    const pill = $("#engine-pill");
    pill.dataset.state = e.state || "idle";
    let text = e.detail || { idle: "Engine idle", starting: "Engine starting", loading: "Loading model", ready: "Ready", working: "Working", error: "Engine problem" }[e.state] || "Engine";
    if (e.state === "loading" && e.elapsed_s != null) text += ` · ${e.elapsed_s}s`;
    $("#engine-text").textContent = text;
    $("#next-run").textContent = s.next_run ? `Next automatic run: ${fmtWhen(s.next_run)}` : "No automatic run scheduled";
    const pending = s.outbox?.pending ?? 0;
    const c = $("#nav-pending"); c.textContent = String(pending); c.hidden = pending === 0;
  }
  function connectEvents() {
    if (api.demo || !("EventSource" in window)) return;
    try {
      const es = new EventSource(api.base + "/events");
      es.onmessage = (ev) => { try { const m = JSON.parse(ev.data); if (m.kind === "status") { state.status = m.payload; renderStrip(); } if (m.kind === "job") document.dispatchEvent(new CustomEvent("ns:job", { detail: m.payload })); } catch {} };
      es.onerror = () => { es.close(); setTimeout(connectEvents, 5000); };
    } catch {}
  }

  // ------------------------------------------------------------------ router
  const ROUTES = { "": "today", today: "today", review: "review", make: "make", clips: "clips", channels: "channels", learn: "learn", settings: "settings" };
  let pageCleanup = null;
  async function render() {
    const hash = location.hash.replace(/^#\/?/, "");
    const [name, query] = hash.split("?");
    const route = ROUTES[name] ?? "today";
    state.route = route; state.query = new URLSearchParams(query || "");
    $$(".nav-list a").forEach((a) => { if (a.dataset.route === route) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    if (pageCleanup) { try { pageCleanup(); } catch {} pageCleanup = null; }
    const main = $("#main");
    main.innerHTML = `<div class="page"><p class="muted">Loading…</p></div>`;
    try {
      const page = NS.pages[route];
      if (!page) { main.innerHTML = `<div class="page"><h1>Not here yet</h1><p class="lead">This screen is coming in a later update.</p></div>`; return; }
      pageCleanup = (await page(main)) || null;
    } catch (err) {
      main.innerHTML = `<div class="page"><h1>Something went wrong</h1><p class="lead">${esc(err.message)}</p><div class="btn-row"><button class="btn" onclick="location.reload()">Reload</button></div></div>`;
    }
    main.focus({ preventScroll: true });
  }

  // ------------------------------------------------------------------ shared page helpers
  function header(title, lead, actionsHtml) {
    return `<div class="page-h"><div><h1>${esc(title)}</h1>${lead ? `<p class="lead">${esc(lead)}</p>` : ""}</div>${actionsHtml ? `<div class="btn-row">${actionsHtml}</div>` : ""}</div>`;
  }
  function outputRow(o, selected) {
    return `<div class="row selectable" role="button" tabindex="0" data-id="${esc(o.output_id)}" aria-selected="${selected ? "true" : "false"}">
      <span class="badge ${esc(o.kind)}">${o.kind === "long" ? "Full video" : o.kind === "short" ? "Short" : "Clip"}</span>
      <div class="min0"><div class="row-title">${esc(o.title)}</div><div class="row-sub">${esc(channelName(o.channel))} · ${fmtWhen(o.created_at)}</div></div>
      <span class="num muted">${fmtDur(o.duration_s)}</span></div>`;
  }
  function channelName(slug) { return state.status?.channels?.find((c) => c.slug === slug)?.display_name || slug || ""; }
  function progressBlock(job) {
    const cls = job.status === "done" ? "done" : job.status === "failed" || job.status === "cancelled" ? "bad" : "";
    const label = job.status === "done" ? "Done" : job.status === "failed" ? `Stopped: ${job.message || "failed"}` : job.status === "cancelled" ? "Cancelled" : stageWords(job.stage || job.status);
    return `<div class="card" data-job="${esc(job.job_id)}">
      <div class="stage-line"><span class="stage">${esc(label)}</span><span class="muted num">${job.progress ?? 0}%</span></div>
      <div class="progress ${cls}" style="margin-top:10px"><span style="width:${job.progress ?? 0}%"></span></div>
      <div class="btn-row" style="margin-top:14px">
        ${job.status === "done" ? `<a class="btn btn-primary" href="#/review">Review it</a>` : ""}
        ${job.status === "running" || job.status === "queued" ? `<button class="btn btn-ghost" data-cancel="${esc(job.job_id)}" type="button">Cancel</button>` : ""}
      </div></div>`;
  }
  // Polls one job until it finishes; calls onUpdate(job) each time. Returns a stop function.
  function watchJob(jobId, onUpdate) {
    let stopped = false;
    (async () => {
      while (!stopped) {
        try { const j = await api.get(`/jobs/${encodeURIComponent(jobId)}`); onUpdate(j); if (["done", "failed", "cancelled"].includes(j.status)) { refreshStatus(); break; } } catch (e) { onUpdate({ job_id: jobId, status: "failed", message: e.message, progress: 0 }); break; }
        await sleep(1500);
      }
    })();
    return () => { stopped = true; };
  }
  Object.assign(NS.util, { header, outputRow, channelName, progressBlock, watchJob, refreshStatus });

  // ------------------------------------------------------------------ page: Today
  NS.pages.today = async function (main) {
    await refreshStatus();
    const s = state.status; const pending = s.outbox?.pending ?? 0;
    const chans = s.channels || [];
    main.innerHTML = `<div class="page">
      ${header(pending ? `${pending} video${pending === 1 ? "" : "s"} ready to review` : "All caught up", pending ? "Watch each one, approve it, then post it to YouTube with the copied details." : "Nothing is waiting for you. Make something, or let tonight's run do it.")}
      <div class="tiles">
        <a class="tile" href="#/review"><span class="tile-title">Review videos${pending ? ` (${pending})` : ""}</span><span class="tile-desc">Approve or reject what Nightshift made, then copy the title, description and tags for YouTube.</span><span class="tile-cta">Open review →</span></a>
        <a class="tile" href="#/make"><span class="tile-title">Make videos now</span><span class="tile-desc">Write a new story and turn it into a full video plus Shorts, without waiting for tonight.</span><span class="tile-cta">Start →</span></a>
        <a class="tile" href="#/clips"><span class="tile-title">Make clips from a video</span><span class="tile-desc">Drop in a stream or podcast you own and get the best moments as vertical clips.</span><span class="tile-cta">Choose a video →</span></a>
      </div>
      <div class="card">
        <div class="kv"><div class="k">Tonight<small>${esc(s.next_run ? "Runs automatically while you sleep." : "Automatic runs are off; set a time in Settings.")}</small></div><div class="num">${esc(s.next_run ? fmtWhen(s.next_run) : "—")}</div></div>
        <div class="kv"><div class="k">Channels<small>Paused channels are skipped tonight.</small></div>
          <div class="chips">${chans.map((c) => `<label class="chip ${c.status === "paused" ? "paused" : "on"}"><input type="checkbox" data-chan="${esc(c.slug)}" ${c.status === "active" ? "checked" : ""}> ${esc(c.display_name)} <span class="small muted">${c.status === "paused" ? "paused" : "on"}</span></label>`).join("") || `<a class="btn" href="#/channels">Add a channel</a>`}</div></div>
        <div class="kv"><div class="k">Outbox<small>Approved videos wait in the Outbox folder until you post them.</small></div><div class="num"><span class="state warn">${s.outbox?.pending ?? 0} pending</span> · <span class="state ok">${s.outbox?.approved ?? 0} approved</span> · <span class="state muted">${s.outbox?.rejected ?? 0} rejected</span></div></div>
      </div>
      <details class="section"><summary>Advanced: engine and storage</summary><div class="section-body">
        <div class="kv"><div class="k">Services</div><div>${Object.entries(s.services || {}).map(([n, v]) => `<span class="pill" style="margin-left:6px" data-state="${v.state === "ready" ? "ready" : v.state === "error" ? "error" : "idle"}"><span class="dot"></span>${esc(n)} ${esc(v.state)}</span>`).join("")}</div></div>
        <div class="kv"><div class="k">Disk</div><div class="num">${esc(s.disk?.free_gb ?? "?")} GB free of ${esc(s.disk?.total_gb ?? "?")} GB · ${esc(s.disk?.home || "")}</div></div>
        <div class="kv"><div class="k">Memory owner</div><div>${esc(s.engine?.vram_owner || "nobody")}</div></div>
      </div></details>
    </div>`;
    main.onchange = async (e) => {
      const slug = e.target.dataset?.chan; if (!slug) return;
      const on = e.target.checked;
      try { await api.post(`/channels/${encodeURIComponent(slug)}/${on ? "resume" : "pause"}`); toast(`${channelName(slug)} ${on ? "will run tonight" : "paused"}`, "ok"); await refreshStatus(); NS.pages.today(main); }
      catch (err) { toast(err.message, "bad"); e.target.checked = !on; }
    };
  };

  // ------------------------------------------------------------------ page: Review
  NS.pages.review = async function (main) {
    await refreshStatus();
    let items = await api.get("/outputs?status=pending");
    let current = state.query.get("id") && items.find((o) => o.output_id === state.query.get("id")) ? state.query.get("id") : items[0]?.output_id || null;

    const draw = async () => {
      if (!items.length) {
        main.innerHTML = `<div class="page">${header("Review", "Nothing is waiting for review.")}<div class="empty"><strong>All caught up</strong>New videos appear here after tonight's run, or when you make one now.</div><div class="btn-row"><a class="btn btn-primary" href="#/make">Make videos now</a><a class="btn" href="#/clips">Make clips</a></div></div>`;
        return;
      }
      const o = items.find((x) => x.output_id === current) || items[0]; current = o.output_id;
      const d = await api.get(`/outputs/${encodeURIComponent(o.output_id)}`);
      const sheet = [
        ["Title", o.title], ["Description", d.description || ""], ["Tags", (d.tags || []).join(", ")], ["Hashtags", (d.hashtags || []).join(" ")],
      ];
      const everything = `TITLE\n${o.title}\n\nDESCRIPTION\n${d.description || ""}\n\nTAGS\n${(d.tags || []).join(", ")}\n\nHASHTAGS\n${(d.hashtags || []).join(" ")}`;
      const asks = [
        ["Altered or synthetic content", d.ai_disclosure === "yes" ? "Yes" : "No"], ["Made for kids", d.made_for_kids ? "Yes" : "No"],
        ["Age restriction", d.age_restrict_suggested ? "Consider restricting (intense content)" : "Not needed"], ["Publish after", d.publish_after ? fmtWhen(d.publish_after) : "Any time"],
        ...(d.related_video_instruction ? [["Related video", d.related_video_instruction]] : []),
      ];
      const idx = items.indexOf(o);
      main.innerHTML = `<div class="page">
        ${header("Review", `${items.length} waiting · ${idx + 1} of ${items.length}`, `<span class="muted small">Keys: <span class="kbd">A</span> approve · <span class="kbd">R</span> reject · <span class="kbd">→</span> skip</span>`)}
        <div class="review">
          <div>
            <div class="player ${o.kind === "long" ? "landscape" : ""}">
              ${api.demo ? `<div class="muted" style="padding:20px;text-align:center">Video preview<br><span class="small">(demo: no engine connected)</span></div>` : `<video controls preload="metadata" src="${api.mediaUrl(o.output_id)}" poster="${api.mediaUrl(o.output_id)}/thumb.jpg"></video>`}
            </div>
            <div class="filmstrip" style="margin-top:14px">${items.map((it) => `<button type="button" data-pick="${esc(it.output_id)}" aria-current="${it.output_id === current}"><span class="badge ${esc(it.kind)}">${it.kind === "long" ? "Full" : it.kind}</span><span class="ft">${esc(it.title)}</span><span class="small muted num">${fmtDur(it.duration_s)}</span></button>`).join("")}</div>
          </div>
          <aside class="review-side">
            <div class="review-title"><span class="badge ${esc(o.kind)}">${o.kind === "long" ? "Full video" : o.kind === "short" ? "Short" : "Clip"}</span> ${esc(o.title)}</div>
            <div class="muted">${esc(channelName(o.channel))} · ${fmtDur(o.duration_s)} · made ${fmtWhen(o.created_at)}</div>
            <div class="review-actions">
              <button class="btn btn-ok btn-lg" data-act="approve" type="button">Approve</button>
              <button class="btn btn-danger-outline btn-lg" data-act="reject" type="button">Reject</button>
              <button class="btn btn-lg" data-act="skip" type="button">Skip</button>
            </div>
            <button class="btn btn-primary btn-block" data-copy-all type="button">Copy everything for YouTube</button>
            <div class="card">
              ${sheet.map(([k, v]) => `<div class="sheet-field" style="margin-bottom:12px"><div><h3>${esc(k)}</h3><pre>${esc(v)}</pre></div><button class="btn btn-ghost" data-copy="${esc(k)}" type="button">Copy</button></div>`).join("")}
            </div>
            <details class="section" open><summary>What YouTube will ask</summary><div class="section-body">${asks.map(([k, v]) => `<div class="kv"><div class="k">${esc(k)}</div><div>${esc(v)}</div></div>`).join("")}</div></details>
            ${d.chapters?.length ? `<details class="section"><summary>Chapters (paste into the description)</summary><div class="section-body"><pre class="small" style="white-space:pre-wrap;margin:0">${esc(d.chapters.map((c) => `${fmtDur(c.start_s)} ${c.title}`).join("\n"))}</pre><button class="btn btn-ghost" data-copy="Chapters" type="button">Copy chapters</button></div></details>` : ""}
            <details class="section"><summary>Why this passed</summary><div class="section-body"><ul style="margin:0;padding-left:18px">${(d.policy_notes || []).map((n) => `<li>${esc(n)}</li>`).join("") || "<li>No notes</li>"}</ul>${d.story_excerpt ? `<p class="muted small" style="margin:8px 0 0">${esc(d.story_excerpt)}</p>` : ""}</div></details>
            <button class="btn btn-ghost" data-act="uploaded" type="button">I posted it: save the YouTube link</button>
          </aside>
        </div></div>`;

      const act = async (what) => {
        if (what === "skip") { current = items[(idx + 1) % items.length]?.output_id || null; return draw(); }
        if (what === "approve") { await api.post(`/outputs/${encodeURIComponent(o.output_id)}/approve`); toast("Approved. It is in the Outbox folder.", "ok"); }
        if (what === "reject") { const reason = await modal({ title: "Reject this video?", body: "A short reason helps Nightshift do better next time.", okLabel: "Reject", danger: true, input: { label: "Reason", placeholder: "e.g. the ending made no sense" } }); if (reason === null) return; await api.post(`/outputs/${encodeURIComponent(o.output_id)}/reject`, { reason }); toast("Rejected", "warn"); }
        if (what === "uploaded") { const url = await modal({ title: "Save the YouTube link", body: "Paste the video URL so analytics can be matched later.", okLabel: "Save", input: { label: "YouTube URL", placeholder: "https://youtu.be/…" } }); if (!url) return; await api.post(`/outputs/${encodeURIComponent(o.output_id)}/uploaded`, { url }); toast("Link saved", "ok"); }
        items = await api.get("/outputs?status=pending"); await refreshStatus(); current = items[Math.min(idx, items.length - 1)]?.output_id || null; draw();
      };
      main.onclick = (e) => {
        const b = e.target.closest("button"); if (!b) return;
        if (b.dataset.act) return act(b.dataset.act).catch((err) => toast(err.message, "bad"));
        if (b.dataset.pick) { current = b.dataset.pick; return draw(); }
        if (b.hasAttribute("data-copy-all")) return copyText(everything, "Everything");
        if (b.dataset.copy) { const k = b.dataset.copy; const v = k === "Chapters" ? d.chapters.map((c) => `${fmtDur(c.start_s)} ${c.title}`).join("\n") : sheet.find((s) => s[0] === k)?.[1] || ""; return copyText(v, k); }
      };
      onKey = (e) => { if (e.target.closest("input,textarea,[contenteditable]")) return; const k = e.key.toLowerCase(); if (k === "a") act("approve"); else if (k === "r") act("reject"); else if (e.key === "ArrowRight") act("skip"); };
    };
    let onKey = null;
    const keyProxy = (e) => onKey && onKey(e);
    document.addEventListener("keydown", keyProxy);
    await draw();
    return () => document.removeEventListener("keydown", keyProxy);
  };

  // ------------------------------------------------------------------ strip buttons, theme, boot
  $("#warmup-btn").onclick = async () => { try { await api.post("/engine/warmup", { service: "llama" }); toast("Warming up the engine", "ok"); refreshStatus(); } catch (e) { toast(e.message, "bad"); } };
  $("#quit-btn").onclick = async () => {
    const ok = await modal({ title: "Quit Nightshift?", body: "Stops the engine and frees the graphics card. Anything in progress stops within 30 seconds; tonight's automatic run still happens.", okLabel: "Quit", danger: true });
    if (!ok) return;
    try { await api.post("/engine/shutdown", { grace_s: 30 }); toast("Nightshift is shutting down. You can close this tab.", "ok"); refreshStatus(); } catch (e) { toast(e.message, "bad"); }
  };
  $("#theme-toggle").onclick = () => {
    const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("ns-theme", next); } catch {}
  };
  try { const t = localStorage.getItem("ns-theme"); if (t) document.documentElement.dataset.theme = t; } catch {}

  window.addEventListener("hashchange", render);
  document.addEventListener("DOMContentLoaded", async () => { await refreshStatus(); connectEvents(); render(); setInterval(() => { if (document.visibilityState === "visible") refreshStatus(); }, api.demo ? 2000 : 10000); });
})();
