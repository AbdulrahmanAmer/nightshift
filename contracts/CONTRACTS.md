# Nightshift contracts (frozen per slice)

Source of truth for every shared name. Code in `ns_common/` implements these; `contracts/schemas/*.schema.json`
are exported from `ns_common/models.py` and must match (test `test_every_schema_file_matches_models`).

| Area | Where |
|---|---|
| Names, paths, ports | spec §15.1–15.3; `ns_common/paths.py`, `ns_common/models.py::Runtime` |
| Supervisor API | spec §15.3 (implemented in Slice 4) |
| TTS API | spec §15.4; client `ns_common/tts_client.py` |
| Embed API | spec §15.5; client `ns_common/embed_client.py` |
| JSON schemas | `contracts/schemas/`; models in `ns_common/models.py` |
| SQLite | `contracts/schema.sql`; `ns_common/db.py` |
| Stage interface | spec §15.8 (implemented in Slice 1 `ns_studio/stages.py`) |
| CLI | spec §15.9 |
| Logging | spec §15.11; `ns_common/logging.py` |
| Error codes | `contracts/errors.json`; `ns_common/errors.py` |
| Policy lists | `contracts/policy/` |
| Prompt front-matter | spec §15.13 |
| Pinned downloads | `manifest.json` (profiles `gpu`, `cpu-dev`); `tools/resolve_manifest.py` |

## Freeze tags
- `contracts-m0` — everything above as of milestone M0 completion (component C3).

## Rules
Only the main session edits `contracts/` and `ns_common/`. Workers file requests in `CHANGE_REQUESTS.md`.
