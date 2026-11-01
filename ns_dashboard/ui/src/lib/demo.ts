/* Sample data shown when no engine answers /api/status. Same shapes as the real API.
   Ported from the vanilla dashboard's demo layer and filled out so every page has content. */
import type { ChatEvent, Job } from "./types";
import { CLIP_STAGES, RUN_STAGES, stageWords } from "./format";

const iso = (hoursFromNow: number) => new Date(Date.now() + hoursFromNow * 3600e3).toISOString();
const at = (daysAgo: number, hh: number, mm: number) => { const d = new Date(); d.setDate(d.getDate() - daysAgo); d.setHours(hh, mm, 0, 0); return d.toISOString(); };
const tonight = () => { const d = new Date(); d.setHours(3, 0, 0, 0); if (d.getTime() < Date.now()) d.setDate(d.getDate() + 1); return d.toISOString(); };

const POOLS = { hooks: ["cold_open", "question", "confession", "warning"], structures: ["twist", "reverse_reveal", "slow_burn", "unreliable_narrator"], caption_styles: ["punch_yellow", "clean_white"], visual_treatments: ["dark_slowzoom", "handheld_grit"] };

export const DEMO: any = {
  status: {
    version: "0.9.2",
    engine: { state: "idle", detail: "Idle", elapsed_s: null, vram_owner: null },
    next_run: tonight(), schedule_time: "03:00",
    outbox: { pending: 0, approved: 0, rejected: 0 },
    channels: [
      { slug: "after-dark", display_name: "After Dark Stories", status: "active", niche: "Original short horror fiction set in everyday places", voice: "Ryan", masters_per_week: 2, publish_window: "18:00-21:00", format: "master_first" },
      { slug: "tiny-mysteries", display_name: "Tiny Mysteries", status: "active", niche: "Small-town mysteries with a quiet twist", voice: "Serena", masters_per_week: 1, publish_window: "17:00-20:00", format: "master_first" },
      { slug: "night-desk", display_name: "The Night Desk", status: "paused", niche: "Clips from my late-night streams and podcast", voice: "Aiden", masters_per_week: 1, publish_window: "20:00-22:00", format: "shorts_only" },
    ],
    services: { llama: { state: "stopped" }, embed: { state: "stopped" }, tts: { state: "stopped" }, dashboard: { state: "ready" } },
    disk: { free_gb: 412, total_gb: 931, home: "D:\\Nightshift" },
  },
  outputs: [
    { output_id: "after-dark-20261102-a1f2/long", kind: "long", title: "The Doorbell Camera That Recorded Tomorrow", channel: "after-dark", duration_s: 731, created_at: at(0, 3, 41), status: "pending", path: "D:\\Nightshift\\outbox\\after-dark\\2026-11-02\\after-dark-20261102-a1f2\\long\\master.mp4" },
    { output_id: "after-dark-20261102-a1f2/s1", kind: "short", k: 1, title: "My doorbell camera records someone every night at 3:12", channel: "after-dark", duration_s: 52, created_at: at(0, 3, 52), status: "pending", path: "D:\\Nightshift\\outbox\\after-dark\\2026-11-02\\after-dark-20261102-a1f2\\shorts\\s1.mp4" },
    { output_id: "after-dark-20261102-a1f2/s2", kind: "short", k: 2, title: "The clip was timestamped for tomorrow night", channel: "after-dark", duration_s: 47, created_at: at(0, 3, 54), status: "pending", path: "D:\\Nightshift\\outbox\\after-dark\\2026-11-02\\after-dark-20261102-a1f2\\shorts\\s2.mp4" },
    { output_id: "tiny-mysteries-20261102-77c1/long", kind: "long", title: "The Bakery That Prints Two Receipts", channel: "tiny-mysteries", duration_s: 846, created_at: at(0, 4, 58), status: "pending", path: "D:\\Nightshift\\outbox\\tiny-mysteries\\2026-11-02\\tiny-mysteries-20261102-77c1\\long\\master.mp4" },
    { output_id: "clip-7f3a/c2", kind: "clip", k: 2, title: "Why the first ten minutes of a stream decide everything", channel: "night-desk", duration_s: 38, created_at: at(1, 23, 12), status: "pending", path: "D:\\Nightshift\\outbox\\night-desk\\2026-11-01\\clip-7f3a\\c2.mp4" },
    { output_id: "after-dark-20261031-9c0d/long", kind: "long", title: "The Laundromat That Only Opens at Night", channel: "after-dark", duration_s: 702, created_at: at(2, 3, 40), status: "uploaded", url: "https://youtu.be/demo1" },
    { output_id: "after-dark-20261031-9c0d/s1", kind: "short", k: 1, title: "The dryer was still warm when I got there", channel: "after-dark", duration_s: 49, created_at: at(2, 3, 50), status: "approved" },
    { output_id: "tiny-mysteries-20261030-2b8e/long", kind: "long", title: "Nobody Ordered the Thirteenth Pizza", channel: "tiny-mysteries", duration_s: 779, created_at: at(3, 4, 12), status: "uploaded", url: "https://youtu.be/demo2" },
    { output_id: "after-dark-20261030-7b1e/s3", kind: "short", k: 3, title: "The night bus that skips my stop", channel: "after-dark", duration_s: 61, created_at: at(3, 3, 55), status: "rejected", reason: "The ending made no sense" },
  ],
  detail: {
    description: "A night-shift nurse stops checking her doorbell clips. Tonight the notification arrives while she is standing on the porch.\n\nOriginal fiction.",
    tags: ["horror story", "scary story", "doorbell camera", "short horror", "creepy story", "narrated story", "fiction", "suspense", "night shift", "porch"],
    hashtags: ["#horrorstory", "#scarystories", "#fiction"], publish_after: iso(30), ai_disclosure: "no", made_for_kids: false,
    story_excerpt: "The doorbell camera pings at 3:12 every night. I stopped checking the clips a week ago. Tonight my phone buzzed while I was standing on the porch, and the clip showed me opening the door.",
    policy_notes: ["No real people, places or brands named", "Title matches what happens in the story", "Marked as original fiction", "Footage is licensed stock, logged with its licence"],
    critic_avg: 8.4, words: 1652,
    chapters: [{ title: "The notification", start_s: 0 }, { title: "A week of clips", start_s: 118 }, { title: "The porch", start_s: 265 }, { title: "Tomorrow", start_s: 470 }],
    related_video_instruction: "Set Related video to The Doorbell Camera That Recorded Tomorrow",
  },
  jobs: [
    { job_id: "j9a41c", kind: "clip", channel: "night-desk", status: "done", progress: 100, stage: "outbox", message: "Done", created_at: at(1, 22, 40), title: "Stream, 31 October", result: { output_ids: ["clip-7f3a/c1", "clip-7f3a/c2", "clip-7f3a/c3", "clip-7f3a/c4"] } },
    { job_id: "j77b02", kind: "run", channel: "tiny-mysteries", status: "failed", progress: 62, stage: "clips", message: "No footage matched three scenes. Add a Pexels key in Settings or widen the footage look.", created_at: at(2, 3, 0) },
    { job_id: "j51e9d", kind: "run", channel: "after-dark", status: "done", progress: 100, stage: "outbox", message: "Done", created_at: at(2, 3, 0), title: "The Laundromat That Only Opens at Night" },
  ] as Job[],
  templates: [
    { id: "auto", label: "Auto", description: "Nightshift looks at the video and picks the layout" },
    { id: "gaming_facecam", label: "Gaming with facecam", description: "Your camera on top, the game below" },
    { id: "podcast_stacked", label: "Podcast, two speakers", description: "Both speakers stacked, the one talking is highlighted" },
    { id: "podcast_single", label: "Podcast, one speaker", description: "Follows one face, full frame" },
    { id: "talking_head", label: "Talking head", description: "Follows your face" },
    { id: "center_crop", label: "Center crop", description: "Middle of the frame with blurred edges" },
    { id: "screen_plus_cam", label: "Screen + camera", description: "Small camera on top, the whole screen below" },
  ],
  voices: [
    { name: "Ryan", mode: "preset", description: "Low, unhurried, close to the mic" },
    { name: "Aiden", mode: "preset", description: "Bright, conversational" },
    { name: "Serena", mode: "preset", description: "Warm, measured, a little dry" },
    { name: "Vivian", mode: "preset", description: "Clear, quick, good for Shorts" },
    { name: "Hollow Porch", mode: "design", description: "Deep, calm, slightly raspy older man" },
  ],
  settings: { schedule_time: "03:00", home: "D:\\Nightshift", profile: "gpu", version: "0.9.2", flags: { ctx: 16384, kv_type: "f16", n_gpu_layers: 99, encoder: "h264_nvenc", tts_engine: "qwentts" }, keys: { pexels: true, pixabay: false }, notifications: true, wake_pc: true },
  learn: {
    imported_at: at(5, 9, 12), matched: 41, unmatched: 3,
    summary: ["Shorts that open mid-scene are watched 23% longer than ones that open with a question", "50-second Shorts keep more viewers than 75-second ones on After Dark Stories", "Yellow and clean white captions perform the same, so both stay in rotation"],
    changes: [{ pool: "hooks", option: "cold_open", delta: 0.08 }, { pool: "length_buckets", option: "50", delta: 0.06 }, { pool: "hooks", option: "question", delta: -0.05 }, { pool: "length_buckets", option: "75", delta: -0.04 }],
    top: [{ title: "The Laundromat That Only Opens at Night", views: 18400, viewed_pct: 71, kind: "long" }, { title: "The dryer was still warm when I got there", views: 9200, viewed_pct: 66, kind: "short" }, { title: "Nobody Ordered the Thirteenth Pizza", views: 6100, viewed_pct: 58, kind: "long" }, { title: "The receipt said Thursday", views: 4800, viewed_pct: 54, kind: "short" }],
    bottom: [{ title: "The night bus that skips my stop", views: 410, viewed_pct: 38, kind: "short" }],
    daily: Array.from({ length: 14 }, (_, i) => ({ date: new Date(Date.now() - (13 - i) * 864e5).toISOString().slice(0, 10), shorts: [820, 1240, 990, 1560, 2210, 1980, 2640, 3010, 2880, 3420, 3950, 3610, 4280, 4720][i], long: [140, 180, 220, 260, 310, 290, 380, 420, 460, 510, 540, 600, 650, 720][i] })),
    milestone: { label: "Two full videos a week on Tiny Mysteries", rule: "Average watched stays at 55% or more for 3 weeks in a row", current: 58, target: 55, weeks: 2, weeks_needed: 3 },
  },
  logs: [
    { ts: "03:00:02", level: "INFO", component: "supervisor", event: "run_start", msg: "nightly run, 2 channels" },
    { ts: "03:12:04", level: "INFO", component: "studio", event: "stage_start", msg: "voice after-dark-20261102-a1f2" },
    { ts: "03:14:40", level: "INFO", component: "studio", event: "stage_end", msg: "voice 156.2 s" },
    { ts: "03:14:41", level: "WARN", component: "media", event: "clip_pool", msg: "no portrait clip for 'porch light flicker', using mood search" },
    { ts: "03:41:10", level: "INFO", component: "studio", event: "outbox", msg: "after-dark-20261102-a1f2 long + 2 shorts" },
    { ts: "04:58:31", level: "INFO", component: "studio", event: "outbox", msg: "tiny-mysteries-20261102-77c1 long" },
  ],
  ideas: [
    { id: 1, channel: "after-dark", title: "The Laundromat That Only Opens at Night", premise: "A student finds a laundromat that is open only between 2 and 4 a.m. and never has the same machines twice.", status: "used", source: "Generated", created_at: at(4, 3, 0) },
    { id: 2, channel: "after-dark", title: "The Substitute", premise: "A substitute teacher takes over a class whose real teacher has been dead for a year, according to the kids.", status: "approved", source: "You", created_at: at(3, 19, 10) },
    { id: 3, channel: "after-dark", title: "Floor 13 Exists", premise: "An elevator technician keeps being called to a floor the building's plans say was never built.", status: "new", source: "Assistant", created_at: at(1, 10, 0) },
    { id: 4, channel: "tiny-mysteries", title: "The Bakery's Second Receipt", premise: "Every receipt from the town bakery has a faint second receipt printed underneath, from a day that has not happened yet.", status: "used", source: "You", created_at: at(5, 11, 0) },
    { id: 5, channel: "tiny-mysteries", title: "Lost Dog, Found Twice", premise: "The same dog is returned to the same family by two different neighbours on the same afternoon.", status: "new", source: "Generated", created_at: at(0, 3, 2) },
    { id: 6, channel: "after-dark", title: "The Hotel Wake-Up Call", premise: "A guest keeps getting wake-up calls for a room that was bricked up in 1974.", status: "rejected", source: "Generated", created_at: at(6, 3, 1) },
  ],
  stories: [
    { job_id: "after-dark-20261102-a1f2", title: "The Doorbell Camera That Recorded Tomorrow", channel: "after-dark", format: "master_first", words: 1652, critic_avg: 8.4, created_at: at(0, 3, 41), outputs: 3, status: "pending" },
    { job_id: "tiny-mysteries-20261102-77c1", title: "The Bakery That Prints Two Receipts", channel: "tiny-mysteries", format: "master_first", words: 1890, critic_avg: 8.1, created_at: at(0, 4, 58), outputs: 1, status: "pending" },
    { job_id: "after-dark-20261031-9c0d", title: "The Laundromat That Only Opens at Night", channel: "after-dark", format: "master_first", words: 1598, critic_avg: 8.1, created_at: at(2, 3, 40), outputs: 5, status: "posted" },
    { job_id: "tiny-mysteries-20261030-2b8e", title: "Nobody Ordered the Thirteenth Pizza", channel: "tiny-mysteries", format: "master_first", words: 1744, critic_avg: 7.8, created_at: at(3, 4, 12), outputs: 4, status: "posted" },
    { job_id: "after-dark-20261030-7b1e", title: "The Night Bus That Skips My Stop", channel: "after-dark", format: "shorts_only", words: 121, critic_avg: 7.2, created_at: at(3, 3, 55), outputs: 1, status: "rejected" },
  ],
  footage: [
    { source: "pexels", id: "8451234", keywords: ["empty hallway", "night"], license: "Pexels License", duration: 12.4, last_used: at(0, 3, 20), uses: 2 },
    { source: "pexels", id: "7765001", keywords: ["porch light", "front door"], license: "Pexels License", duration: 15.0, last_used: at(0, 3, 21), uses: 1 },
    { source: "pixabay", id: "22318", keywords: ["kitchen window", "rain"], license: "Pixabay Content License", duration: 9.1, last_used: at(2, 3, 30), uses: 1 },
    { source: "pexels", id: "9120440", keywords: ["laundromat", "neon sign"], license: "Pexels License", duration: 18.2, last_used: at(2, 3, 31), uses: 3 },
    { source: "pixabay", id: "40417", keywords: ["bakery", "early morning"], license: "Pixabay Content License", duration: 11.6, last_used: at(0, 4, 40), uses: 1 },
    { source: "pexels", id: "6603318", keywords: ["street lamp", "fog"], license: "Pexels License", duration: 14.3, last_used: at(3, 4, 0), uses: 4 },
  ],
  formats: [
    { id: "long_16x9", name: "Full video", kind: "long", canvas: { w: 1920, h: 1080, fps: 30 }, channels: ["after-dark", "tiny-mysteries"] },
    { id: "short_9x16", name: "Short", kind: "short", canvas: { w: 1080, h: 1920, fps: 30 }, channels: ["after-dark", "tiny-mysteries"] },
    { id: "clip_gaming_facecam", name: "Gaming with facecam", kind: "clip", canvas: { w: 1080, h: 1920, fps: 30 }, channels: ["night-desk"] },
    { id: "clip_podcast_stacked", name: "Podcast, two speakers", kind: "clip", canvas: { w: 1080, h: 1920, fps: 30 }, channels: ["night-desk"] },
    { id: "clip_screen_plus_cam", name: "Screen + camera", kind: "clip", canvas: { w: 1080, h: 1920, fps: 30 }, channels: [] },
  ],
  channelStats: {
    "after-dark": { week: 2, watched_pct: 64, spark: [38, 44, 41, 52, 58, 55, 63, 61, 66, 70, 68, 72, 74, 71] },
    "tiny-mysteries": { week: 1, watched_pct: 58, spark: [30, 33, 31, 40, 42, 39, 47, 46, 50, 52, 51, 55, 57, 58] },
    "night-desk": { week: 0, watched_pct: 49, spark: [22, 25, 30, 28, 34, 33, 31, 36, 40, 38, 41, 44, 43, 45] },
  },
  pending: {} as Record<string, () => any>,
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const recount = () => {
  const o = DEMO.outputs as any[];
  DEMO.status.outbox = { pending: o.filter((x) => x.status === "pending").length, approved: o.filter((x) => ["approved", "uploaded"].includes(x.status)).length, rejected: o.filter((x) => x.status === "rejected").length };
  DEMO.status.channels.forEach((c: any) => (c.pending = o.filter((x) => x.status === "pending" && x.channel === c.slug).length));
};
const fdGet = (b: any, k: string) => (b instanceof FormData ? b.get(k) : b?.[k]);

export function simulateJob(job: Job) {
  const stages = job.kind === "clip" ? CLIP_STAGES : RUN_STAGES;
  let i = 0;
  DEMO.status.engine = { state: "loading", detail: "Loading model", elapsed_s: 0, vram_gb: 9 };
  const tick = setInterval(() => {
    if (job.status === "cancelled") { clearInterval(tick); DEMO.status.engine = { state: "ready", detail: "Ready" }; return; }
    job.status = "running"; job.stage = stages[i]; job.step = i + 1; job.steps = stages.length; job.message = stageWords(stages[i]); job.progress = Math.round(((i + 1) / stages.length) * 100);
    DEMO.status.engine = { state: "working", detail: job.message, step: i + 1, steps: stages.length };
    i++;
    if (i >= stages.length) {
      clearInterval(tick); job.status = "done"; job.progress = 100; job.message = "Done"; DEMO.status.engine = { state: "ready", detail: "Ready" };
      const n = job.kind === "clip" ? 3 : 1; const ids: string[] = [];
      for (let k = 1; k <= n; k++) {
        const id = `${job.kind}-${job.job_id}/${job.kind === "clip" ? "c" + k : "s" + k}`;
        DEMO.outputs.unshift({ output_id: id, kind: job.kind === "clip" ? "clip" : "short", k, title: job.kind === "clip" ? ["The moment chat lost it", "Why I never skip the intro", "The one question I always ask"][k - 1] : "The night the hallway light answered back", channel: job.channel, duration_s: 40 + k * 7, created_at: new Date().toISOString(), status: "pending", path: `D:\\Nightshift\\outbox\\${job.channel}\\today\\${id.replace("/", "\\")}.mp4` });
        ids.push(id);
      }
      job.result = { output_ids: ids };
    }
  }, 1400);
}

export async function demoHandle(method: string, path: string, body?: any): Promise<any> {
  await sleep(80);
  const [p, q] = path.split("?"); const parts = p.split("/").filter(Boolean); const sp = new URLSearchParams(q || "");
  if (p === "/status") { recount(); return structuredClone(DEMO.status); }
  if (p === "/outputs") { const st = (sp.get("status") || "").split(",").filter(Boolean); return DEMO.outputs.filter((o: any) => !st.length || st.includes(o.status)).map((o: any) => ({ ...o })); }
  if (parts[0] === "outputs" && parts.length >= 2) {
    const isAct = ["approve", "reject", "uploaded", "open-folder"].includes(parts[parts.length - 1]);
    const id = decodeURIComponent(parts.slice(1, parts.length - (isAct ? 1 : 0)).join("/"));
    const out = DEMO.outputs.find((o: any) => o.output_id === id); if (!out) throw new Error("Not found");
    if (method === "POST" && isAct) {
      const a = parts[parts.length - 1];
      if (a === "approve") out.status = "approved";
      if (a === "reject") { out.status = "rejected"; out.reason = body?.reason; }
      if (a === "uploaded") { out.status = "uploaded"; out.url = body?.url; }
      if (a === "open-folder") throw new Error("Opening folders needs the engine");
      recount(); return { ok: true };
    }
    const ch = out.channel === "tiny-mysteries";
    return { ...out, ...DEMO.detail, ...(ch ? { description: "Every receipt from the town bakery has a faint second receipt underneath, dated next Thursday.\n\nOriginal fiction.", story_excerpt: "Mrs. Penhallow kept every receipt in a biscuit tin. It was her grandson who noticed the second line of print, fainter than the first, and the date on it.", related_video_instruction: "Set Related video to The Bakery That Prints Two Receipts" } : {}), ...(out.kind === "clip" ? { description: "From my 31 October stream.", chapters: [], story_excerpt: "Cut from your own stream at 1:12:40. The moment was picked because chat activity tripled and you laughed.", policy_notes: ["Your own video, rights confirmed when the job started", "No music from the stream kept in the clip"] } : {}) };
  }
  if (p === "/jobs" && method === "GET") return DEMO.jobs.map((j: Job) => ({ ...j }));
  if ((p === "/jobs/run" || p === "/jobs/clip") && method === "POST") {
    const kind = p.endsWith("run") ? "run" : "clip";
    const job: Job = { job_id: "j" + Math.random().toString(16).slice(2, 8), kind, channel: fdGet(body, "channel") || "after-dark", status: "queued", progress: 0, stage: "queued", message: "Waiting for the engine", created_at: new Date().toISOString(), title: kind === "clip" ? (fdGet(body, "file")?.name || fdGet(body, "url") || "Your video") : undefined };
    DEMO.jobs.unshift(job); simulateJob(job); return { job_id: job.job_id };
  }
  if (parts[0] === "jobs" && parts.length === 2) { const j = DEMO.jobs.find((x: Job) => x.job_id === parts[1]); if (!j) throw new Error("Not found"); return { ...j }; }
  if (parts[0] === "jobs" && parts[2] === "cancel") { const j = DEMO.jobs.find((x: Job) => x.job_id === parts[1]); if (j) { j.status = "cancelled"; j.message = "Cancelled"; } return { ok: true }; }
  if (p === "/channels" && method === "GET") return DEMO.status.channels.map((c: any) => ({ ...c, pools: structuredClone(POOLS), language: "en", long_minutes: [10, 14], shorts_per_master: [4, 6], audience: "general", fiction_label: true, avoid_topics: ["real crimes", "self-harm"], voice: { mode: "preset", speaker: c.voice, speed: 1 }, stats: DEMO.channelStats[c.slug] }));
  if (p === "/channels" && method === "POST") { DEMO.status.channels.push({ ...body, voice: body.voice?.speaker, status: "active" }); return { ok: true }; }
  if (parts[0] === "channels" && parts.length >= 2) {
    const ch = DEMO.status.channels.find((c: any) => c.slug === parts[1]);
    if (parts[2] === "pause" && ch) ch.status = "paused";
    if (parts[2] === "resume" && ch) ch.status = "active";
    if (parts[2] === "validate") return { ok: true, warnings: [] };
    if (method === "PUT" && ch) Object.assign(ch, body, { voice: body.voice?.speaker || ch.voice });
    return { ok: true };
  }
  if (p === "/templates") return DEMO.templates;
  if (p === "/voices") return DEMO.voices;
  if (p === "/voices/preview") throw new Error("Voice samples need the engine running");
  if (p === "/settings") { if (method === "PUT") Object.assign(DEMO.settings, body); return structuredClone(DEMO.settings); }
  if (p === "/settings/keys") { DEMO.settings.keys[body.name] = true; return { ok: true }; }
  if (p === "/diagnostics") return { path: "C:\\Users\\you\\Desktop\\nightshift-diagnostics-2026-11-02.zip" };
  if (p === "/engine/shutdown") { DEMO.status.engine = { state: "idle", detail: "Engine stopped" }; return { ok: true }; }
  if (p === "/learn/summary") return structuredClone(DEMO.learn);
  if (p === "/learn/import") { DEMO.learn.imported_at = new Date().toISOString(); return structuredClone(DEMO.learn); }
  if (p.startsWith("/logs/tail")) { const lvl = sp.get("level"); return DEMO.logs.filter((l: any) => !lvl || l.level === lvl); }
  if (p === "/library/ideas" && method === "GET") return DEMO.ideas.map((i: any) => ({ ...i }));
  if (p === "/library/ideas" && method === "POST") { const idea = { id: Date.now(), status: "new", source: "You", created_at: new Date().toISOString(), ...body }; DEMO.ideas.unshift(idea); return idea; }
  if (parts[0] === "library" && parts[1] === "ideas" && method === "PUT") { const i = DEMO.ideas.find((x: any) => String(x.id) === parts[2]); if (i) Object.assign(i, body); return i; }
  if (p === "/library/stories") { const qq = (sp.get("q") || "").toLowerCase(); return DEMO.stories.filter((s: any) => !qq || s.title.toLowerCase().includes(qq)); }
  if (p === "/library/footage") { const qq = (sp.get("q") || "").toLowerCase(); return DEMO.footage.filter((f: any) => !qq || f.keywords.join(" ").includes(qq)); }
  if (p === "/library/formats" && method === "GET") return DEMO.formats.map((f: any) => ({ ...f }));
  if (p === "/library/formats" && method === "POST") { DEMO.formats.push({ ...body }); return body; }
  if (p === "/update/check") return { current: "0.9.2", latest: "0.9.2", up_to_date: true, notes: "" };
  if (p === "/update/apply") throw new Error("Updating needs the engine running");
  if (p === "/assistant/confirm") { const run = DEMO.pending[body.action_id]; if (!run) throw new Error("Nothing to confirm"); delete DEMO.pending[body.action_id]; return run(); }
  if (p === "/assistant/dismiss") { delete DEMO.pending[body.action_id]; return { ok: true }; }
  throw new Error(`Not available with demo data: ${method} ${path}`);
}

/* Scripted assistant: recognises a few intents and yields the same event kinds as the real
   /api/assistant/chat stream (conversation, message, confirm, done). */
export async function* demoChat(message: string): AsyncGenerator<ChatEvent> {
  const last = message.toLowerCase();
  const pend = (text: string, tool: string, run: () => any): ChatEvent => { const id = "a" + Math.random().toString(16).slice(2, 8); DEMO.pending[id] = run; return { kind: "confirm", payload: { action_id: id, tool, text } }; };
  const startJob = (kind: string, channel: string) => () => { const job: Job = { job_id: "j" + Math.random().toString(16).slice(2, 8), kind, channel, status: "queued", progress: 0, stage: "queued", message: "Waiting", created_at: new Date().toISOString() }; DEMO.jobs.unshift(job); simulateJob(job); return { message: "Started. Progress is on the Create page, and the result lands in the Queue." }; };
  yield { kind: "conversation", payload: { conversation_id: "demo" } };
  await sleep(700);
  if (/draft|series|concept/.test(last)) {
    yield { kind: "message", payload: { text: "Three concepts for an After Dark Stories series. Keep one, or ask me to redo any of them.\n\nA. The Night Shift Nurse. Her doorbell camera records tomorrow. Six parts, each a new night.\nB. Floor 13 Exists. An elevator technician keeps getting called to a floor that was never built.\nC. The Substitute. A class whose real teacher has been dead for a year, according to the kids." } };
    yield pend("Save these three concepts to Library, Ideas as new ideas", "save_ideas", () => { DEMO.ideas.unshift({ id: Date.now(), channel: "after-dark", title: "Series: The Night Shift Nurse", premise: "Her doorbell camera records tomorrow. Six parts, each a new night.", status: "new", source: "Assistant", created_at: new Date().toISOString() }); return { message: "Saved to Library, Ideas." }; });
  } else if (/regenerate|redo/.test(last)) {
    yield { kind: "message", payload: { text: "New take on B. Floor 13 Exists becomes The Service Lift: a night porter's lift stops between floors, and the intercom answers in his own voice from tomorrow." } };
  } else if (/pause|resume/.test(last)) {
    const ch = DEMO.status.channels.find((c: any) => last.includes(c.display_name.toLowerCase()) || last.includes(c.slug)) || DEMO.status.channels[0];
    const resume = /resume/.test(last);
    yield { kind: "message", payload: { text: resume ? `${ch.display_name} will run again from tonight.` : `${ch.display_name} will be skipped from tonight until you resume it. Videos already made stay in the Queue.` } };
    yield pend(`${resume ? "Resume" : "Pause"} ${ch.display_name}`, resume ? "resume_channel" : "pause_channel", () => { ch.status = resume ? "active" : "paused"; return { message: `${ch.display_name} is ${resume ? "running again" : "paused"}.` }; });
  } else if (/clip|youtu|http/.test(last)) {
    const url = message.match(/https?:\/\/\S+/)?.[0];
    if (url) { yield { kind: "message", payload: { text: "I can cut clips from that link for The Night Desk with the Auto layout. It downloads the video first, then transcribes it and picks the moments." } }; yield pend(`Make up to 6 clips from ${url} for The Night Desk`, "start_clips", startJob("clip", "night-desk")); }
    else yield { kind: "message", payload: { text: "Paste the link to your video, or drop the file on the Create page, and I will make clips from it." } };
  } else if (/where|folder|file|find/.test(last)) {
    yield { kind: "message", payload: { text: "Videos waiting for you are in D:\\Nightshift\\outbox\\<channel>\\<date>\\. The newest is The Doorbell Camera That Recorded Tomorrow, in after-dark\\2026-11-02\\after-dark-20261102-a1f2\\long\\master.mp4." } };
  } else if (/status|doing|engine|tonight/.test(last)) {
    yield { kind: "message", payload: { text: `The engine is ${String(DEMO.status.engine.detail || "idle").toLowerCase()}. ${DEMO.status.outbox.pending} videos are waiting for review. Tonight's run starts at 03:00 and makes one full video for After Dark Stories and one for Tiny Mysteries.` } };
  } else if (/why|explain|fail/.test(last)) {
    yield { kind: "message", payload: { text: "The Tiny Mysteries run on Saturday stopped at Finding footage. Three scenes had no matching stock clip because only the Pixabay key is missing and the footage look was set to Clean and bright. Adding a Pixabay key in Settings fixes it." } };
  } else if (/make|create|new video|story/.test(last)) {
    yield { kind: "message", payload: { text: "I can start a new story video for After Dark Stories now: a full video plus Shorts, about an hour of work." } };
    yield pend("Start a full video and Shorts for After Dark Stories", "start_run", startJob("run", "after-dark"));
  } else {
    yield { kind: "message", payload: { text: "I can start videos or clips, draft story concepts, pause or resume channels, find files and explain results. With demo data my answers are scripted. On your PC I run on the local model." } };
  }
  yield { kind: "done", payload: { conversation_id: "demo" } };
}
