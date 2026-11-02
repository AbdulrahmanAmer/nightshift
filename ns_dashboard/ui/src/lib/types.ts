export type EngineState = "idle" | "starting" | "loading" | "ready" | "working" | "error";
export interface Engine { state: EngineState; detail?: string; elapsed_s?: number | null; vram_owner?: string | null; service?: string | null; progress?: number | null; step?: number; steps?: number; vram_gb?: number }
export interface ChannelSummary { slug: string; display_name: string; status: string; niche?: string; voice?: any; masters_per_week?: number; publish_window?: string; format?: string; pending?: number; tonight?: boolean | string }
export interface GpuStatus { name: string; load_pct?: number | null; vram_used_mb?: number | null; vram_total_mb?: number | null; temp_c?: number | null }
export interface Status { version?: string; engine: Engine; next_run?: string | null; schedule_time?: string; outbox?: { pending: number; approved: number; rejected: number; uploaded?: number }; channels: ChannelSummary[]; services?: Record<string, { state: string; vram_mb?: number }>; disk?: { free_gb: number; total_gb: number; home: string }; last_error?: { job_id: string; message: string; kind: string } | null; gpu?: GpuStatus | null }
export type Kind = "long" | "short" | "clip";
export interface Output { output_id: string; kind: Kind; k?: number; title: string; channel: string; duration_s: number; created_at: string; status: string; path?: string; url?: string; reason?: string }
export interface OutputDetail extends Output { description?: string; tags?: string[]; hashtags?: string[]; publish_after?: string; ai_disclosure?: string; made_for_kids?: boolean; story_excerpt?: string; policy_notes?: string[]; chapters?: { title: string; start_s: number }[]; related_video_instruction?: string; critic_avg?: number; words?: number }
export interface Job { job_id: string; kind: string; channel?: string; status: string; /** ISO time a scheduled job is held until; null/absent = started now. */ schedule_at?: string | null; progress?: number; stage?: string; step?: number; steps?: number; message?: string; created_at?: string; title?: string; result?: { output_ids?: string[] } }
export interface Template { id: string; label: string; description: string }
export interface Voice { name: string; mode: string; description?: string }
/** POST /voices/design and /voices/clone answer (contracts/schemas/voice_make_response). */
export interface VoiceMade { voice_id: string; preview_wav_path: string }
export interface Pools { hooks: string[]; structures: string[]; caption_styles: string[]; visual_treatments: string[] }
export interface Channel extends ChannelSummary { language?: string; audience?: string; voice_notes?: string; pools?: Pools; avoid_topics?: string[]; fiction_label?: boolean; long_minutes?: number[]; shorts_per_master?: number[]; stats?: { week?: number; watched_pct?: number; spark?: number[]; qc_pass_rate?: number | null; qc_pass_target?: number } }
export interface Settings { schedule_time?: string; home?: string; profile?: string; flags?: Record<string, string | number>; keys?: Record<string, boolean>; notifications?: boolean; version?: string; [k: string]: any }
export interface LearnRow { title: string; views: number; viewed_pct: number; kind?: Kind }
export interface Learn { imported_at?: string; matched?: number; unmatched?: number; summary?: string[]; changes?: { pool: string; option: string; delta: number }[]; top?: LearnRow[]; bottom?: LearnRow[]; daily?: { date: string; shorts: number; long: number }[]; milestone?: { label: string; rule: string; current: number; target: number; weeks: number; weeks_needed: number } }
export interface Idea { id: number | string; channel: string; title: string; premise: string; status: string; source?: string; created_at?: string }
export interface Story { job_id: string; title: string; channel: string; format: string; words: number; critic_avg: number; created_at: string; outputs: number; status: string }
export interface Footage { source: string; id: string; keywords: string[]; license: string; duration: number; last_used: string; uses: number }
export interface Format { id: string; name?: string; kind: string; canvas?: { w: number; h: number; fps: number }; channels?: string[]; source?: "shipped" | "edited" | "yours"; valid?: boolean; errors?: string[]; definition?: any }
export interface LogLine { ts: string; level: string; component: string; event: string; msg?: string }
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
export interface SeriesStory { slug: string; channel: string; title: string; status: string; next_step: string; has_design: boolean; chosen?: string | null; parts: SeriesPart[] }
