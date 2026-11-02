export type EngineState = "idle" | "starting" | "loading" | "ready" | "working" | "error";
export interface Engine { state: EngineState; detail?: string; elapsed_s?: number | null; vram_owner?: string | null; service?: string | null; progress?: number | null; reachable?: boolean; draining?: boolean; step?: number; steps?: number; vram_gb?: number }
export interface ChannelSummary { slug: string; display_name: string; status: string; niche?: string; voice?: any; masters_per_week?: number; publish_window?: string; format?: string; pending?: number; tonight?: boolean | string }
export interface GpuStatus { name: string; load_pct?: number | null; vram_used_mb?: number | null; vram_total_mb?: number | null; temp_c?: number | null }
export interface Incident { channel: string; ts: string; kind: string; note: string | null }
export interface Status { version?: string; engine: Engine; next_run?: string | null; next_run_in_s?: number | null; schedule_time?: string; outbox?: { pending: number; approved: number; rejected: number; uploaded?: number }; channels: ChannelSummary[]; services?: Record<string, { state: string; vram_mb?: number | null }>; disk?: { free_gb: number | null; total_gb: number | null; home: string }; job?: Job | null; queue?: { queued: number; running: number; scheduled: number }; last_run?: Run | null; incidents?: Incident[]; last_error?: { job_id: string; message: string; kind: string } | null; gpu?: GpuStatus | null }
export type Kind = "long" | "short" | "clip";
/** One video: GET /outputs items. `reason` is the reject note, `url` the YouTube link once posted, `path` the video file. */
export interface Output { output_id: string; job_id: string; kind: Kind; k: number | null; title: string; channel: string; duration_s: number | null; created_at: string; status: string; review_note: string | null; reason: string | null; url: string | null; path: string; source_id: string | null; combo_id: string | null; media_url: string; thumb_url: string }
export interface OutputDetail extends Output { description?: string; tags?: string[]; hashtags?: string[]; publish_after?: string | null; ai_disclosure?: string | null; made_for_kids?: boolean | null; age_restrict_suggested?: boolean | null; story_excerpt?: string | null; policy_notes?: string[]; chapters?: { title: string; timestamp?: string; start_s: number }[] | null; related_video_instruction?: string | null; thumbnails?: string[] | null; sheet?: any; sheet_text?: string | null; policy?: any; critic_avg?: number | null; words?: number | null; [k: string]: any }
/** A dashboard job (GET /jobs, /jobs/{id}, the SSE `dashboard_job` frames). `progress` is 0-100; `step`/`steps` come from the `name (i/n)` message. */
export interface Job { job_id: string; kind: string; channel: string | null; status: string; schedule_at: string | null; progress: number; stage: string | null; step: number | null; steps: number | null; message: string; created_at: string; started_at: string | null; finished_at: string | null; params: { title?: string; [k: string]: any }; result: { output_ids?: string[]; error?: { code?: string; hint?: string | null }; [k: string]: any } | null; cancel_requested: boolean; title?: string }
export interface Template { id: string; label: string; description: string; when?: string; regions?: any[]; caption_y_pct?: number; title_card?: boolean }
export interface Voice { name: string; mode: string; source?: string; description?: string }
/** POST /voices/design and /voices/clone answer (contracts/schemas/voice_make_response). */
export interface VoiceMade { voice_id: string; preview_wav_path: string }
export interface Pools { hooks: string[]; structures: string[]; caption_styles: string[]; visual_treatments: string[] }
/** The channel as the editor holds it: a profile (ChannelProfile) in the fields it edits. */
export interface Channel extends ChannelSummary { language?: string; audience?: string; voice_notes?: string; pools?: Pools; avoid_topics?: string[]; fiction_label?: boolean; long_minutes?: number[]; shorts_per_master?: number[] }
/** GET /settings. `flags` are the engine settings as flat dotted keys with plain values ("llama.flags.ctx": 16384). */
export interface Settings { schedule_time: string; home: string; profile: string | null; flags: Record<string, string | number | boolean>; keys: Record<string, boolean>; notifications: boolean; runtime_locked: boolean; install: { version: string | null; app_path: string | null; profile: string | null }; owner_name: string | null; ui_theme: string | null; next_run: string | null; schedule_result?: { applied: boolean; note: string }; version?: string }
export interface LearnRow { title: string; views: number; viewed_pct: number; kind?: Kind }
/** GET /learn/summary: the last import, the best and worst videos, 14 days of views and the week-8 check. */
export interface Learn { imported_at: string | null; matched: number | null; unmatched: number | null; summary: string[]; changes: { pool: string; option: string; delta: number }[]; top: LearnRow[]; bottom: LearnRow[]; daily: { date: string; shorts: number; long: number }[]; milestone: { label: string; rule: string; current: number; target: number; weeks: number; weeks_needed: number } | null; pools: Record<string, any>; weights: any[]; last_import: any; unmatched_uploads: string[] }
export interface Idea { idea_id: number; channel: string; title: string; premise: string; twist: string; status: string; source: string; job_id: string | null; created_at: string }
export interface StoryOutput { output_id: string; kind: Kind; k: number | null; status: string; video_url: string | null }
/** GET /library/stories. `title`, `words` and `status` are derived (`status` from the outputs: pending, approved, posted, rejected or none). */
export interface Story { job_id: string; channel: string; format: string; working_title: string | null; opening_line: string; word_count: number; critic_avg: number | null; revisions: number; created_at: string; outputs: StoryOutput[]; title: string; words: number; status: string }
export interface Footage { source: string; clip_id: string; url: string; license: string; width: number | null; height: number | null; duration: number | null; local_path: string | null; keywords: string[]; fetched_at: string; last_used: string | null; uses: number }
export interface Format { id: string; name: string; kind: string | null; canvas: { w: number; h: number; fps: number } | null; channels: string[]; source: "shipped" | "edited" | "yours"; valid: boolean; errors: string[]; definition: any }
export interface LogLine { ts: string; level: string; component: string; event: string; msg?: string; data?: any }
export type ChatEvent =
  | { kind: "conversation"; payload: { conversation_id: string } }
  | { kind: "message"; payload: { text: string; partial?: boolean } }
  | { kind: "confirm"; payload: { action_id: string; tool: string; args?: any; text: string } }
  | { kind: "tool_call"; payload: { tool: string; args?: any } }
  | { kind: "tool_result"; payload: { tool: string; ok: boolean; result?: any; error?: string } }
  | { kind: "busy"; payload: { message: string } }
  | { kind: "error"; payload: { code?: string; message: string; hint?: string } }
  | { kind: "done"; payload: { conversation_id?: string } };
/** One part of a workshop story (GET /library/series). */
export interface SeriesPart { n: number; title: string; minutes: number; status: "planned" | "written" | "approved" | "producing" | "published" | string; job_id?: string | null; words: number; shorts: number }
/** A workshop story with its part table (GET /library/series reads the story folders under stories/). */
export interface SeriesStory { slug: string; channel: string; title: string; status: string; next_step: string; has_design: boolean; chosen?: string | null; parts: SeriesPart[]; [k: string]: any }
/** One row of GET /runs: a nightly (daily) or manual run with the report the studio wrote. */
export interface Run { run_id: string; started_at: string; finished_at?: string | null; mode: string; duration_s?: number | null; summary?: { jobs?: { job_id: string; channel: string; stages?: number }[]; failures?: { job_id: string; channel: string; code?: string; error?: string }[] } }
/** GET /channels: one card per channel (the list on the Channels page). */
export interface ChannelCard { slug: string; display_name: string; status: string; niche: string; language: string; format: string; voice: string; pending: number; approved: number; last_output_at: string | null; stats: ChannelStats | null; masters_per_week: number; publish_window: string; caption_styles: string[]; formats_allowed: string[] }
export interface ChannelStats { week?: number; watched_pct?: number; spark?: number[]; qc_pass_rate?: number | null; qc_pass_target?: number }
/** The channel profile file (ns_common.models.Channel): what the editor loads from GET /channels/{slug} and saves with PUT. */
export interface ChannelProfile { slug: string; display_name: string; status: "active" | "paused"; language: string; niche: string; seed_keywords: string[]; voice_notes: string; voice: { mode: "preset" | "design" | "clone"; speaker: string | null; description: string | null; name: string | null; speed: number }; format: "master_first" | "shorts_only" | "series_compilation"; masters_per_week: number; long_minutes: number[]; shorts_per_master: number[]; shorts_per_day_max: number; length_buckets: number[]; publish_window: string; audience: "general" | "mature"; made_for_kids: boolean; fiction_label: boolean; creator_note: string; critic_bar: number; qc_pass_target: number; pools: ChannelPools; avoid_topics: string[]; formats_allowed: string[]; captions: { mask_profanity: boolean }; created_at: string | null }
export interface ChannelPools { hooks: string[]; structures: string[]; pov_tones: string[]; pacings: string[]; caption_styles: string[]; visual_treatments: string[]; title_formulas: string[] }
/** GET/PUT/POST /channels...: the profile plus the validation warnings. */
export interface ChannelDetail { profile: ChannelProfile; warnings: string[]; warning_fields: { field: string; message: string }[] }
/** GET /update/state: what the detached updater writes to run/update_state.json (idle when it never ran). */
export interface UpdateState { state: "idle" | "running" | "done" | "failed"; old: string | null; new: string | null; message: string | null; updated_at: string | null }
