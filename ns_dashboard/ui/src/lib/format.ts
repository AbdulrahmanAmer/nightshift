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
  ideas: "Finding an idea", plan: "Planning the story", draft: "Writing the story", critic_revise: "Editing the story", split: "Cutting into parts",
  hooks: "Writing the opening line", seo: "Writing title and tags", visual_plan: "Planning footage", policy_text: "Safety check", voice: "Recording narration",
  duration_check: "Checking length", captions: "Adding captions", shorts_timing: "Picking the best moments", clips: "Finding footage", render: "Rendering",
  render_master: "Rendering the full video", thumbnails: "Making thumbnails", shorts_derive: "Cutting the Shorts", qc: "Checking quality", policy_final: "Final check",
  outbox: "Saving", import: "Importing the video", audio: "Extracting audio", transcribe: "Transcribing", select: "Choosing moments", detect: "Finding faces",
  seo_clip: "Writing titles", queued: "Waiting for the engine",
};
export const stageWords = (k?: string | null) => (k && STAGE_WORDS[k]) || (k ? k.replace(/_/g, " ") : "Working");
export const RUN_STAGES = ["ideas", "plan", "draft", "critic_revise", "hooks", "seo", "visual_plan", "voice", "captions", "clips", "render", "qc", "outbox"];
export const CLIP_STAGES = ["import", "audio", "transcribe", "select", "detect", "captions", "render", "qc", "outbox"];

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
