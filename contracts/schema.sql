-- Nightshift studio.db (spec §15.7). All statements idempotent.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS channels (
  slug TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  mode TEXT NOT NULL,               -- daily | manual | gate
  summary_json TEXT
);

CREATE TABLE IF NOT EXISTS ideas (
  idea_id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL REFERENCES channels(slug),
  job_id TEXT,
  premise TEXT NOT NULL,
  twist TEXT NOT NULL,
  embedding BLOB,                    -- float32 little-endian vector
  created_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new',   -- new | approved | used | rejected (idea bank, spec 20.3a)
  title TEXT,
  source TEXT                        -- owner | assistant | generated
);

CREATE TABLE IF NOT EXISTS stories (
  job_id TEXT PRIMARY KEY,
  channel TEXT NOT NULL REFERENCES channels(slug),
  format TEXT NOT NULL,              -- master_first | shorts_only | series_compilation
  working_title TEXT,
  text TEXT NOT NULL,
  word_count INTEGER NOT NULL,
  opening_line TEXT NOT NULL,
  embedding BLOB,
  critic_avg REAL,
  revisions INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parts (
  job_id TEXT NOT NULL REFERENCES stories(job_id),
  n INTEGER NOT NULL,
  text TEXT NOT NULL,
  hook TEXT,
  word_count INTEGER NOT NULL,
  est_seconds REAL NOT NULL,
  PRIMARY KEY (job_id, n)
);

CREATE TABLE IF NOT EXISTS clips (
  source TEXT NOT NULL,              -- pexels | pixabay | fake
  clip_id TEXT NOT NULL,
  url TEXT NOT NULL,
  license TEXT NOT NULL,
  width INTEGER, height INTEGER, duration REAL,
  local_path TEXT,
  fetched_at TEXT NOT NULL,
  keywords TEXT,                     -- JSON array
  PRIMARY KEY (source, clip_id)
);

CREATE TABLE IF NOT EXISTS clip_usage (
  source TEXT NOT NULL,
  clip_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  job_id TEXT NOT NULL,
  item TEXT NOT NULL,                -- long | s<k>
  used_at TEXT NOT NULL,
  FOREIGN KEY (source, clip_id) REFERENCES clips(source, clip_id)
);
CREATE INDEX IF NOT EXISTS idx_clip_usage_clip ON clip_usage(source, clip_id, used_at);
CREATE INDEX IF NOT EXISTS idx_clip_usage_channel ON clip_usage(channel, used_at);

CREATE TABLE IF NOT EXISTS combos (
  combo_id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  job_id TEXT NOT NULL,
  item TEXT NOT NULL,
  hook TEXT, structure TEXT, pov_tone TEXT, pacing TEXT,
  caption_style TEXT, visual_treatment TEXT, title_formula TEXT, length_bucket INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_combos_channel ON combos(channel, created_at);

CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  channel TEXT,
  path TEXT NOT NULL,
  duration_s REAL,
  sha256 TEXT,
  imported_at TEXT NOT NULL,
  template TEXT,
  status TEXT NOT NULL DEFAULT 'imported',
  rights_confirmed INTEGER NOT NULL DEFAULT 0,   -- owner confirmed rights to the video (spec 19.1)
  title TEXT
);

CREATE TABLE IF NOT EXISTS outputs (
  output_id TEXT PRIMARY KEY,        -- "<job_id>/long" | "<job_id>/s<k>"
  job_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  kind TEXT NOT NULL,                -- long | short | clip
  k INTEGER,
  title TEXT NOT NULL,
  path TEXT NOT NULL,
  phash TEXT,
  duration_s REAL,
  combo_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | uploaded
  review_note TEXT,
  source_id TEXT REFERENCES sources(source_id),   -- set for clips (spec 15.7)
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outputs_channel ON outputs(channel, created_at);

CREATE TABLE IF NOT EXISTS uploads (
  output_id TEXT PRIMARY KEY REFERENCES outputs(output_id),
  video_url TEXT NOT NULL,
  published_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analytics (
  output_id TEXT NOT NULL REFERENCES outputs(output_id),
  date TEXT NOT NULL,
  views INTEGER, viewed_pct REAL, avg_pct_viewed REAL, avg_view_duration_s REAL,
  likes INTEGER, comments INTEGER, shares INTEGER, subs_gained INTEGER,
  PRIMARY KEY (output_id, date)
);

CREATE TABLE IF NOT EXISTS pool_weights (
  channel TEXT NOT NULL,
  pool TEXT NOT NULL,
  option TEXT NOT NULL,
  weight REAL NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (channel, pool, option)
);

CREATE TABLE IF NOT EXISTS calibration (
  channel TEXT NOT NULL,
  story_id TEXT NOT NULL,
  critic_json TEXT NOT NULL,
  human_json TEXT,
  PRIMARY KEY (channel, story_id)
);

CREATE TABLE IF NOT EXISTS incidents (
  incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,                -- pause | resume | warning | strike
  note TEXT
);

CREATE TABLE IF NOT EXISTS dashboard_jobs (
  job_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  channel TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  progress REAL NOT NULL DEFAULT 0,
  message TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  params_json TEXT
);

-- Dashboard Assistant transcript (spec 20.3a). ns_dashboard may also open it from data/assistant.db.
CREATE TABLE IF NOT EXISTS assistant_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  role TEXT NOT NULL,                -- user | assistant | tool | system
  content TEXT NOT NULL,
  tool_calls_json TEXT
);

-- Dashboard Assistant conversations, pending confirm cards and the busy queue (CR-C9-9). Same file as
-- assistant_messages (data/assistant.db); ns_dashboard/assistant/store.py no longer defines them itself.
CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY,
  title TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  role TEXT NOT NULL,                 -- system | user | assistant | tool
  content TEXT,
  tool_calls_json TEXT,
  tool_call_id TEXT,
  name TEXT,
  context_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, msg_id);
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL,
  confirm_text TEXT NOT NULL,
  context_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed | dismissed
  result_json TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS queued_messages (
  queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
  text TEXT NOT NULL,
  context_json TEXT,
  status TEXT NOT NULL DEFAULT 'queued',    -- queued | done
  created_at TEXT NOT NULL
);
