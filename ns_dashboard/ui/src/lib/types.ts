export type EngineState = "idle" | "starting" | "loading" | "ready" | "working" | "error";
export interface Engine { state: EngineState; detail?: string; elapsed_s?: number | null; vram_owner?: string | null; service?: string | null; progress?: number | null; reachable?: boolean; draining?: boolean; step?: number; steps?: number; vram_gb?: number }
export interface ChannelSummary { slug: string; display_name: string; status: string; niche?: string; voice?: any; masters_per_week?: number; publish_window?: string; format?: string; pending?: number; tonight?: boolean | string }
export interface GpuStatus { name: string; load_pct?: number | null; vram_used_mb?: number | null; vram_total_mb?: number | null; temp_c?: number | null }
export interface Incident { channel: string; ts: string; kind: string; note: string | null }
export type ScheduleMode = "registered" | "interactive_only" | "none";
export interface Status { version?: string; schedule_registered?: boolean; schedule_mode?: ScheduleMode; engine: Engine; next_run?: string | null; next_run_in_s?: number | null; schedule_time?: string; outbox?: { pending: number; approved: number; rejected: number; uploaded?: number }; channels: ChannelSummary[]; services?: Record<string, { state: string; vram_mb?: number | null }>; disk?: { free_gb: number | null; total_gb: number | null; home: string }; job?: Job | null; queue?: { queued: number; running: number; scheduled: number }; last_run?: Run | null; incidents?: Incident[]; last_error?: { job_id: string; message: string; kind: string } | null; gpu?: GpuStatus | null }
export type Kind = "long" | "short" | "clip";
/** One video: GET /outputs items. `reason` is the reject note, `url` the YouTube link once posted, `path` the video file. */
export interface Output { output_id: string; job_id: string; kind: Kind; k: number | null; title: string; channel: string; duration_s: number | null; created_at: string; status: string; review_note: string | null; reason: string | null; url: string | null; path: string; source_id: string | null; combo_id: string | null; profile?: string | null; media_url: string; thumb_url: string }
export interface OutputDetail extends Output { description?: string; tags?: string[]; hashtags?: string[]; publish_after?: string | null; ai_disclosure?: string | null; made_for_kids?: boolean | null; age_restrict_suggested?: boolean | null; story_excerpt?: string | null; policy_notes?: string[]; chapters?: { title: string; timestamp?: string; start_s: number }[] | null; related_video_instruction?: string | null; thumbnails?: string[] | null; sheet?: any; sheet_text?: string | null; policy?: any; critic_avg?: number | null; words?: number | null; [k: string]: any }
/** A dashboard job (GET /jobs, /jobs/{id}, the SSE `dashboard_job` frames). `progress` is 0-100; `step`/`steps` come from the `name (i/n)` message. */
export interface Job { job_id: string; kind: string; channel: string | null; status: string; schedule_at: string | null; progress: number; stage: string | null; step: number | null; steps: number | null; message: string; created_at: string; started_at: string | null; finished_at: string | null; params: { title?: string; [k: string]: any }; result: { output_ids?: string[]; layout?: { template: string; per_clip: Record<string, string>; notes: Record<string, string>; no_facecam: boolean } | null; error?: { code?: string; hint?: string | null; stage?: string | null }; [k: string]: any } | null; cancel_requested: boolean; dismissed_at?: string | null; title?: string }
export interface Template { id: string; label: string; description: string; when?: string; regions?: any[]; caption_y_pct?: number; title_card?: boolean }
export interface Voice { name: string; mode: string; source?: string; description?: string }
/** POST /voices/design and /voices/clone answer (contracts/schemas/voice_make_response). */
export interface VoiceMade { voice_id: string; preview_wav_path: string }
export interface Pools { hooks: string[]; structures: string[]; caption_styles: string[]; visual_treatments: string[] }
/** The channel as the editor holds it: a profile (ChannelProfile) in the fields it edits. */
export interface Channel extends ChannelSummary { language?: string; audience?: string; voice_notes?: string; pools?: Pools; avoid_topics?: string[]; fiction_label?: boolean; long_minutes?: number[]; shorts_per_master?: number[] }
/** GET /settings. `flags` are the engine settings as flat dotted keys with plain values ("llama.flags.ctx": 16384). */
export interface Settings { schedule_time: string; schedule_registered?: boolean; schedule_mode?: ScheduleMode; home: string; profile: string | null; flags: Record<string, string | number | boolean>; keys: Record<string, boolean>; key_warnings?: Record<string, { code: string; message: string; hint: string; detail: string }>; notifications: boolean; runtime_locked: boolean; install: { version: string | null; app_path: string | null; profile: string | null }; owner_name: string | null; ui_theme: string | null; next_run: string | null; schedule_result?: { applied: boolean; note: string }; version?: string; dev_mode?: boolean; fake_footage?: boolean; tiny_story?: boolean }
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
  | { kind: "busy"; payload: { message: string; owner?: string; queued?: boolean } }
  | { kind: "error"; payload: { code?: string; message: string; hint?: string } }
  | { kind: "done"; payload: { conversation_id?: string } };
/** One part of a workshop story (GET /library/series). */
export interface SeriesPart { n: number; title: string; minutes: number; status: "planned" | "written" | "approved" | "producing" | "produced" | "published" | string; job_id?: string | null; words: number; shorts: number }
/** A workshop story with its part table (GET /library/series reads the story folders under stories/). */
export interface SeriesStory { slug: string; channel: string; title: string; status: string; next_step: string; has_design: boolean; chosen?: string | null; parts: SeriesPart[]; [k: string]: any }
/** One row of GET /runs: a nightly (daily) or manual run with the report the studio wrote. */
/** `state` is `running` only while a live process owns the run; a run with no finish time and no owner is `interrupted` (killed by a power cut or a crash), and `note` says so. */
/** What became of a job a night left half made: open (needs Retry), resumed (continued and finished), failed_again (continued, stopped again) or waiting (the GPU was busy). */
export type InterruptedState = "open" | "resumed" | "failed_again" | "waiting";
export interface InterruptedJob { job_id: string; channel: string; stage?: string | null; state?: InterruptedState; resumed?: boolean; has_card?: boolean }
export interface Run { run_id: string; started_at: string; finished_at?: string | null; mode: string; duration_s?: number | null; state?: "running" | "finished" | "failed" | "interrupted"; interrupted?: boolean; note?: string; summary?: { jobs?: { job_id: string; channel: string; stages?: number }[]; interrupted_jobs?: InterruptedJob[]; failures?: { job_id: string; channel: string; code?: string; error?: string }[]; skipped?: { channel: string; jobs: number }[] } }
/** GET /channels: one card per channel (the list on the Channels page). */
export interface ChannelCard { slug: string; display_name: string; status: string; niche: string; language: string; format: string; voice: string; pending: number; approved: number; last_output_at: string | null; stats: ChannelStats | null; masters_per_week: number; publish_window: string; caption_styles: string[]; formats_allowed: string[] }
export interface ChannelStats { week: number | null; watched_pct: number | null; spark: number[]; qc_pass_rate: number | null; qc_pass_target: number }
/** The channel profile file (ns_common.models.Channel): what the editor loads from GET /channels/{slug} and saves with PUT. */
export interface ChannelProfile { slug: string; display_name: string; status: "active" | "paused"; language: string; niche: string; seed_keywords: string[]; voice_notes: string; voice: { mode: "preset" | "design" | "clone"; speaker: string | null; description: string | null; name: string | null; speed: number }; format: "master_first" | "shorts_only" | "series_compilation"; masters_per_week: number; long_minutes: number[]; shorts_per_master: number[]; shorts_per_day_max: number; length_buckets: number[]; publish_window: string; audience: "general" | "mature"; made_for_kids: boolean; fiction_label: boolean; creator_note: string; critic_bar: number; qc_pass_target: number; pools: ChannelPools; avoid_topics: string[]; formats_allowed: string[]; captions: { mask_profanity: boolean; remove_fillers?: boolean }; created_at: string | null }
export interface ChannelPools { hooks: string[]; structures: string[]; pov_tones: string[]; pacings: string[]; caption_styles: string[]; visual_treatments: string[]; title_formulas: string[] }
/** GET/PUT/POST /channels...: the profile plus the validation warnings. */
export interface ChannelDetail { profile: ChannelProfile; warnings: string[]; warning_fields: { field: string; message: string }[]; formats_in_use?: Record<string, { id: string | null; name?: string; why?: string; source?: string; applied: boolean; problem?: string }> }
/** GET /notes: what the nightly run could not show as a toast (run/morning_note.json). */
export interface MorningNote { at: string; title: string; message: string }
/** GET /update/state: what the detached updater writes to run/update_state.json (idle when it never ran). */
export interface UpdateState { state: "idle" | "running" | "done" | "failed"; old: string | null; new: string | null; message: string | null; updated_at: string | null; /** The updater process is gone though the file still said running. */ stale?: boolean;
  /** After an update: the new version's section of CHANGELOG.md (the `## [x.y.z] - date` heading line included). */ release_notes?: string }
/** One entry of GET /assistant/tools: the registry the Assistant works from. */
export interface ToolInfo { name: string; description: string; side_effect: boolean; topic: string }

/* ---- answers the pages read from POST/PUT calls and from the lists that had no declared type (checked by tests/test_api_ui_contract.py) ---- */
/** GET /models/state: model downloads started in this session. */
export type ModelsState = Record<string, { state: string; message: string }>;
/** GET /settings/uninstall. */
export interface UninstallInfo { path: string; exists: boolean }
/** GET /update/check and POST /update/apply (the parsed `ns update` result). */
export interface UpdateCheck { state: "up_to_date" | "available" | "unavailable" | "error" | "unknown" | "started" | "updated"; up_to_date: boolean; message: string; current?: string | null; latest?: string | null; [k: string]: any }
/** GET /assistant/conversations. */
export interface AssistantConversation { conversation_id: string; title: string | null; created_at: string; updated_at: string; queued: number }
/** GET /assistant/history. */
export interface AssistantMessage { role: string; content: string; created_at: string; tool_calls?: any; tool_call_id?: string; name?: string }
export interface AssistantHistory { conversation_id: string; messages: AssistantMessage[]; pending_actions: { action_id: string; tool: string; args: any; confirm_text: string }[] }
/** POST /assistant/confirm and /assistant/dismiss. */
export interface ActionAnswer { action_id: string; status: "done" | "failed" | "dismissed"; conversation_id: string; tool?: string; message?: string; result?: any }
/** POST /channels/{slug}/validate. */
export interface ChannelValidation { ok: boolean; errors: string[]; warnings: string[]; error_fields: { field: string; message: string }[]; warning_fields: { field: string; message: string }[] }
/** POST /uploads, PUT /uploads/{id} and POST /uploads/{id}/complete. */
export interface UploadState { upload_id: string; source_id: string; filename: string; size: number; offset: number; status: "uploading" | "complete"; path: string | null; sha256: string | null; chunk_size?: number }
/** POST /diagnostics and POST /settings/export: a zip that was written. */
export interface FileMade { path: string; name: string; size: number | null; download_url: string; /** What `ns backup` left out on purpose (the keys file, on a drive without permissions, NS-W116), and the sentence that says why. */ left_out?: string[]; note?: string }
/** POST /settings/import. */
export interface SettingsImported { restored: boolean; file: string; lines: string[]; note: string; /** Lines about the keys file when a restore left it as it was (NS-W116 / NS-E120). */ left_out?: string[] }
/** POST /library/series/{slug}/produce. */
export interface SeriesQueued { slug: string; queued: { part: number; job_id: string; title: string }[] }
/** POST /learn/import. */
export interface LearnImported { matched: number; unmatched: number; summary: string[]; changes: { pool: string; option: string; delta: number }[]; unmatched_rows?: string[]; job_id?: string }
/** GET /voices/pending and POST /voices/apply-pending: the voice that setup queued and could not make yet. */
export interface PendingVoiceInfo { name: string; mode: "design" | "clone"; description: string; channel: string | null }
export interface PendingVoice { pending: PendingVoiceInfo | null }
export interface VoiceApplied { created: string | null; pending: PendingVoiceInfo | null; /** The `apply_voice` job that makes it (null when nothing was queued by setup). */ job_id?: string | null }
