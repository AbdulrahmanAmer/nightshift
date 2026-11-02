export const fmtDur = (s: number | null | undefined) => {
  const n = Math.round(Number(s) || 0); const h = Math.floor(n / 3600); const m = Math.floor((n % 3600) / 60); const sec = String(n % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
};
export const fmtClock = (iso?: string | null) => { if (!iso) return ""; const d = new Date(iso); return isNaN(+d) ? "" : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }); };
const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString();
export const fmtDay = (iso?: string | null) => {
  if (!iso) return ""; const d = new Date(iso); if (isNaN(+d)) return String(iso);
  const now = new Date();
  if (sameDay(d, now)) return "Today";
  if (sameDay(d, new Date(Date.now() - 864e5))) return "Yesterday";
  if (sameDay(d, new Date(Date.now() + 864e5))) return "Tomorrow";
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
};
/** Day words for use inside a sentence: "today", "tomorrow", otherwise "Sat 31 Oct". */
export const dayInline = (iso?: string | null) => { const d = fmtDay(iso); return ["Today", "Yesterday", "Tomorrow"].includes(d) ? d.toLowerCase() : d; };
export const fmtWhen = (iso?: string | null) => (iso ? `${dayInline(iso)} ${fmtClock(iso)}` : "");
export const fmtNum = (n: number) => Math.round(n).toLocaleString("en-US");
export const fmtCompact = (n: number) => (n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? Math.round(n / 1e3) + "k" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(Math.round(n)));
export const kindLabel = (k: string) => (({ long: "Full video", short: "Short", clip: "Clip" }) as Record<string, string>)[k] || k;
export const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;

export const STAGE_WORDS: Record<string, string> = {
  // one-Short stories
  ideas: "Finding an idea", plan: "Planning the story", draft: "Writing the story", critic_revise: "Editing the story", split: "Cutting into parts",
  hooks: "Writing the opening line", seo: "Writing title and tags", visual_plan: "Planning footage",
  // full videos with Shorts, and series parts
  long_plan: "Planning the full story", chapters_draft: "Writing the chapters", chapters_critic: "Editing the chapters", continuity: "Checking the story holds together",
  seo_long: "Writing the video title and tags", shorts_select: "Choosing the Shorts", visual_plan_long: "Planning footage", seo_shorts: "Writing the Short titles",
  script_import: "Reading the approved script", short_combos: "Choosing the looks", duration_check: "Checking length", shorts_timing: "Picking the best moments",
  render_master: "Rendering the full video", thumbnails: "Making thumbnails", shorts_derive: "Cutting the Shorts",
  // shared
  policy_text: "Safety check", policy_final: "Final check", voice: "Recording narration", captions: "Adding captions", clips: "Finding footage", render: "Rendering",
  qc: "Checking quality", outbox: "Saving",
  // clip jobs
  clip_import: "Importing the video", clip_audio: "Extracting audio", clip_analyze: "Looking at the video", clip_transcribe: "Transcribing", clip_select: "Choosing moments",
  clip_layout: "Framing the clips", clip_captions: "Adding captions", clip_render: "Rendering the clips", clip_seo: "Writing titles", clip_qc: "Checking quality", clip_outbox: "Saving the clips",
  download: "Downloading the video", import: "Importing the video", audio: "Extracting audio", transcribe: "Transcribing", select: "Choosing moments", detect: "Finding faces",
  queued: "Waiting for the engine",
};
export const stageWords = (k?: string | null) => (k && STAGE_WORDS[k]) || (k ? k.replace(/_/g, " ") : "Working");
/** "Stopped at voice", or just "Stopped" when the failure did not say which stage. */
export const stoppedAt = (k?: string | null) => (k ? `Stopped at ${stageWords(k).toLowerCase()}` : "Stopped");
/** Stages in order, per kind of job (ns_studio.orchestrator.FORMAT_STAGES and the clip stages). */
export const SHORTS_STAGES = ["ideas", "plan", "draft", "critic_revise", "split", "hooks", "seo", "visual_plan", "voice", "captions", "clips", "render", "qc", "outbox"];
export const MASTER_STAGES = ["ideas", "long_plan", "chapters_draft", "chapters_critic", "continuity", "seo_long", "shorts_select", "visual_plan_long", "seo_shorts", "voice", "duration_check", "captions", "shorts_timing", "clips", "render_master", "thumbnails", "shorts_derive", "qc", "outbox"];
export const SERIES_STAGES = ["script_import", "visual_plan_long", "seo_long", "seo_shorts", "voice", "duration_check", "captions", "shorts_timing", "clips", "render_master", "thumbnails", "shorts_derive", "qc", "outbox"];
export const CLIP_STAGES = ["clip_import", "clip_audio", "clip_analyze", "clip_transcribe", "clip_select", "clip_layout", "clip_captions", "clip_render", "clip_seo", "clip_qc", "clip_outbox"];
/** The stage list of a job: clips, a series part, a one-Short story or a full video (the default). */
export const stagesOf = (j: { kind: string; params?: { format?: string; [k: string]: any } }) => (j.kind === "clip" ? CLIP_STAGES : j.kind === "series_part" ? SERIES_STAGES : j.params?.format === "shorts_only" ? SHORTS_STAGES : MASTER_STAGES);

export const store = {
  get<T>(k: string, fb: T): T { try { const v = localStorage.getItem(k); return v == null ? fb : (JSON.parse(v) as T); } catch { return fb; } },
  set(k: string, v: unknown) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage blocked: per-viewer convenience only */ } },
};

export const POOL_LABELS: Record<string, { label: string; opts: Record<string, string> }> = {
  hooks: { label: "How stories open", opts: { cold_open: "Drop into the moment", question: "A real question", confession: "A confession", shocking_fact: "A concrete detail", in_medias_res: "Mid-action", warning: "A warning" } },
  structures: { label: "How stories are built", opts: { twist: "Twist ending", reverse_reveal: "Reverse reveal", slow_burn: "Slow burn", unreliable_narrator: "Unreliable narrator", countdown: "Countdown", two_timelines: "Two timelines" } },
  caption_styles: { label: "Caption look", opts: { punch_yellow: "Bold yellow", clean_white: "Clean white", boxed_dark: "Boxed dark", minimal_lower: "Minimal, lower third" } },
  visual_treatments: { label: "Footage look", opts: { dark_slowzoom: "Dark, slow zoom", handheld_grit: "Handheld grit", clean_bright: "Clean and bright", cinematic_teal: "Cinematic teal" } },
};

/** Job kinds whose result is video. A finished self-check or analytics import has no videos to count. */
export const MAKES_VIDEO = ["run", "clip", "series_part"];
/** "Done", with the number of videos only for the kinds that make video. */
export const doneWords = (j: { kind: string; result?: { output_ids?: string[] } | null }) => {
  if (!MAKES_VIDEO.includes(j.kind)) return "Done";
  const n = j.result?.output_ids?.length ?? 0;
  return n ? `Done · ${plural(n, "video")}` : "Done · no videos";
};
