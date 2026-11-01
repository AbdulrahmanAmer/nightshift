# Change requests to frozen contracts

Format per entry:

## CR-<n>: <short title>
- **From:** WP-<letter> / task
- **Change:** what to add or alter (exact names, types)
- **Why:** the concrete need
- **Status:** open | applied in <commit> | rejected (<reason>)

(none yet)

## CR-1: studio error codes NS-E307 to NS-E313
- **From:** C7 / m1-c7-studio-core Task 2 (also used by the M2 learn, review and cadence tasks)
- **Change:** add to `contracts/errors.json` (message and hint exactly as in the local shim `ns_studio/errors.py`, table `_LOCAL`):
  `NS-E307` The channel profile is invalid. · `NS-E308` Channels are not distinct enough. · `NS-E309` Unknown channel. · `NS-E310` The analytics CSV could not be read. · `NS-E311` Calibration data is missing or incomplete. · `NS-E312` The variation pools are exhausted. · `NS-E313` Output not found or not in a state that allows this action.
- **Why:** spec section 8 (pool minimums, distinctness), 16.5 (calibration), 18.9 (CSV import) and section 10 (review, mark-uploaded) have conditions with no code today; builders may not raise a code that is not in `errors.json`.
- **Workaround until applied:** `ns_studio.errors.StudioError` carries the same codes with a local message table and prefers `errors.json` when the code exists. When this CR is applied, empty `_LOCAL` and nothing else changes. Note: `tests` that scan source for `NS-E` literals will flag these codes until then.
- **Status:** open

## CR-2: `policy_check` prompt, schema and fixture (owner of the files: C5)
- **From:** C7 / m2-c7-studio-core (policy gate)
- **Change:** C5 adds `ns_studio/prompts/policy_check.md` (front-matter `version: 1`, `thinking: true`, `schema: policy_check`), a `policy_check` entry in `PROMPT_SCHEMAS`, and a fixture `tests/fixtures/llm/policy_check.json` that passes every mode. C7 calls it as `call_json(ctx, "policy_check", mode=<mode>, ...)`.
  Template variables per `mode`:
  - `unsafe_topics`: `story` (str), `categories` (list of `{id, label, description, examples}` from `contracts/policy/unsafe_topics.yaml`), `audience` (`general|mature`).
  - `hook_title_honesty`: `story`, `items` (list of `{item, title, hook}`).
  - `standalone_short`: `story`, `moments` (list of `{k, text, hook}`).
  - `tag_relevance`: `story`, `items` (list of `{item, title, tags}`).
  Response schema (all modes, keys not relevant to a mode may be empty): `{"verdict": "pass"|"fail", "reason": str, "flags": [{"category": str, "reason": str}], "intensity": "low"|"medium"|"high", "failed_items": [str]}`. `flags[].category` is an id from `unsafe_topics.yaml`; `failed_items` holds item names (`long`, `s1`, ...) or Short numbers as strings.
- **Why:** spec section 18.8 checks `unsafe_topics`, `hook_title_honesty`, `standalone_short` and the tag relevance half of `metadata_spam` are judged by the LLM (section 16.1 lists `policy_check.md`, thinking on). C7 cannot add files to C5's directories.
- **Workaround until applied:** C7 imports `ns_studio.text.prompts` only through `importlib` inside `ns_studio/policy/judge.py`; with no `policy_check.md` present the CLI leaves the policy gate stages out of the plan (`policy.available()` is false) so `studio run --fake-services` keeps working.
- **Status:** open

## CR-3: nothing new in pyproject (informational)
- **From:** C7
- **Change:** none. Perceptual hashing is implemented with FFmpeg and numpy (both already available), so `imagehash` and Pillow are not needed by C7. spaCy and keyring are only imported lazily from the existing `studio` extra.
- **Status:** informational
