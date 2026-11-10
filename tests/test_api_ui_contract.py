# ruff: noqa: E501
"""API <-> UI contract: what the routes answer is what `ui/src/lib/types.ts` says (review rows 2, 5, 6, 8, 17, 21, 22, 24).

The routes return plain dicts, so there is no response model to compare. Instead this test seeds a dashboard
with fake adapters and real services (channels, outputs in every state, jobs, ideas, stories, footage,
formats, series, runs, learn), calls each route the UI calls and checks the JSON field for field against the
interface the UI declares for it:

- every key of the answer is declared in the interface (the UI can read it by that name),
- every field the interface requires (no `?`) is present, and
- every value has the type the interface says (`null` only where the type says `| null`).

`ui/src/lib/types.ts` is parsed here with a small tokenizer (interfaces, `extends`, type aliases, unions,
arrays, inline objects, `Record`). A mismatch such as `word_count` against `words`, `idea_id` against `id`
or a nullable `critic_avg` that the page formats with `.toFixed` fails with the route, the field and the
reason. `KNOWN_MISMATCHES` lists the ones that are not fixed yet; the test is strict both ways (an entry that
no longer happens fails), so fixing a row means deleting its line.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ns_dashboard.fakes import seed_channel, seed_output
from tests.test_dashboard_api import make_env

pytest.importorskip("fastapi")

TYPES_TS = Path(__file__).resolve().parents[1] / "ns_dashboard" / "ui" / "src" / "lib" / "types.ts"

#: "<route key>: <problem>" -> the review row that fixes it. Fixing a row deletes its entries.
KNOWN_MISMATCHES: dict[str, str] = {}


# ------------------------------------------------------------------ a small TypeScript type reader


@dataclass
class Member:
    """One member of an interface or inline object type."""

    name: str
    optional: bool
    type: str


@dataclass
class Iface:
    """An `interface` (members, the interface it extends, and whether it has an index signature)."""

    name: str
    extends: str | None
    members: dict[str, Member] = field(default_factory=dict)
    index: str | None = None


def strip_comments(text: str) -> str:
    """Remove `/* */` and `//` comments."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def split_top(text: str, sep: str) -> list[str]:
    """Split on `sep` outside any (), [], {}, <> and string literal."""
    parts: list[str] = []
    depth = 0
    quote = ""
    cur: list[str] = []
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def match_brace(text: str, start: int) -> int:
    """Index of the `}` that closes the `{` at `start`."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced braces in types.ts")


MEMBER_RE = re.compile(r"^(?:readonly\s+)?(?P<name>\w+|\[\s*\w+\s*:\s*\w+\s*\])(?P<opt>\?)?\s*:\s*(?P<type>.+)$", re.S)


def parse_members(body: str) -> tuple[dict[str, Member], str | None]:
    """Members of an object type body (`a: string; b?: number`), plus the index-signature type if any."""
    members: dict[str, Member] = {}
    index: str | None = None
    for chunk in split_top(body.replace("\n", " "), ";"):
        for piece in split_top(chunk, ","):  # inline object types may use commas
            if not piece:
                continue
            m = MEMBER_RE.match(piece)
            if m is None:
                raise ValueError(f"cannot read member {piece!r}")
            name, typ = m["name"], m["type"].strip()
            if name.startswith("["):
                index = typ
            else:
                members[name] = Member(name, bool(m["opt"]), typ)
    return members, index


def parse_types(text: str) -> tuple[dict[str, Iface], dict[str, str]]:
    """(interfaces, type aliases) of a TypeScript module that only declares types."""
    src = strip_comments(text)
    ifaces: dict[str, Iface] = {}
    aliases: dict[str, str] = {}
    for m in re.finditer(r"export interface (\w+)(?:\s+extends\s+(\w+))?\s*\{", src):
        end = match_brace(src, m.end() - 1)
        members, index = parse_members(src[m.end() : end])
        ifaces[m[1]] = Iface(m[1], m[2], members, index)
    for m in re.finditer(r"export type (\w+)\s*=\s*", src):
        start = m.end()
        # an alias ends at the first top-level `;` or at the next `export` on a new line
        depth = 0
        end = len(src)
        for i in range(start, len(src)):
            c = src[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == ";" and depth == 0:
                end = i
                break
            elif src.startswith("\nexport ", i) and depth == 0:
                end = i
                break
        aliases[m[1]] = " ".join(src[start:end].split())
    return ifaces, aliases


TYPES = parse_types(TYPES_TS.read_text(encoding="utf-8"))


def all_members(ifaces: dict[str, Iface], name: str) -> tuple[dict[str, Member], str | None]:
    """Members of `name` including what it extends."""
    iface = ifaces[name]
    members: dict[str, Member] = {}
    index = iface.index
    if iface.extends:
        base, base_index = all_members(ifaces, iface.extends)
        members.update(base)
        index = index or base_index
    members.update(iface.members)
    return members, index


def is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def check_value(typ: str, value: Any, where: str, out: list[str]) -> None:
    """Append a problem to `out` when `value` is not a `typ`."""
    ifaces, aliases = TYPES
    typ = typ.strip()
    alts = split_top(typ, "|")
    if len(alts) > 1:
        trial: list[list[str]] = []
        for alt in alts:
            sub: list[str] = []
            check_value(alt, value, where, sub)
            if not sub:
                return
            trial.append(sub)
        out.append(f"{where}: {json.dumps(value, default=str)[:60]} is none of `{typ}`")
        return
    if typ in ("any", "unknown"):
        return
    if typ in ("null", "undefined"):
        if value is not None:
            out.append(f"{where}: expected null, got {type(value).__name__}")
        return
    if typ == "string":
        if not isinstance(value, str):
            out.append(f"{where}: expected string, got {describe(value)}")
        return
    if typ == "number":
        if not is_number(value):
            out.append(f"{where}: expected number, got {describe(value)}")
        return
    if typ == "boolean":
        if not isinstance(value, bool):
            out.append(f"{where}: expected boolean, got {describe(value)}")
        return
    if (typ.startswith('"') and typ.endswith('"')) or (typ.startswith("'") and typ.endswith("'")):
        if value != typ[1:-1]:
            out.append(f"{where}: expected {typ}, got {json.dumps(value, default=str)[:40]}")
        return
    if typ.endswith("[]"):
        if not isinstance(value, list):
            out.append(f"{where}: expected an array, got {describe(value)}")
            return
        inner = typ[:-2].strip()
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1]
        for i, item in enumerate(value[:20]):
            check_value(inner, item, f"{where}[{i}]", out)
        return
    if typ.startswith("Record<"):
        if not isinstance(value, dict):
            out.append(f"{where}: expected an object, got {describe(value)}")
            return
        inner = split_top(typ[len("Record<") : -1], ",")[1]
        for k, item in value.items():
            check_value(inner, item, f"{where}.{k}", out)
        return
    if typ.startswith("{") and typ.endswith("}"):
        members, index = parse_members(typ[1:-1])
        check_object(members, index, value, where, out)
        return
    if typ in aliases:
        check_value(aliases[typ], value, where, out)
        return
    if typ in ifaces:
        members, index = all_members(ifaces, typ)
        check_object(members, index, value, where, out)
        return
    out.append(f"{where}: types.ts mentions `{typ}`, which this test cannot read")


def describe(value: Any) -> str:
    if value is None:
        return "null"
    return f"{type(value).__name__} {json.dumps(value, default=str)[:40]}"


def check_object(members: dict[str, Member], index: str | None, value: Any, where: str, out: list[str]) -> None:
    """Keys and values of one object against its members."""
    if not isinstance(value, dict):
        out.append(f"{where}: expected an object, got {describe(value)}")
        return
    for name, m in members.items():
        if name not in value:
            if not m.optional:
                out.append(f"{where}.{name}: required by the UI type but missing from the answer")
            continue
        check_value(m.type, value[name], f"{where}.{name}", out)
    for key, item in value.items():
        if key in members:
            continue
        if index is not None:
            check_value(index, item, f"{where}.{key}", out)
        else:
            out.append(f"{where}.{key}: in the answer but not declared in the UI type")


def check_payload(type_name: str, payload: Any, many: bool, where: str) -> list[str]:
    """Problems of `payload` (a list when `many`) against the interface `type_name`."""
    out: list[str] = []
    if many:
        if not isinstance(payload, list):
            return [f"{where}: expected a list, got {describe(payload)}"]
        if not payload:
            return [f"{where}: the seeded answer is empty, so nothing was checked"]
        for i, item in enumerate(payload):
            check_value(type_name, item, f"{where}[{i}]", out)
    else:
        check_value(type_name, payload, where, out)
    return dedupe(out)


def dedupe(problems: list[str]) -> list[str]:
    """Same problem on every row of a list is reported once, with the index removed."""
    seen: dict[str, None] = {}
    for p in problems:
        seen.setdefault(re.sub(r"\[\d+\]", "[]", p), None)
    return list(seen)


# ------------------------------------------------------------------ a seeded dashboard


@dataclass
class Case:
    """One route the UI calls and the interface it declares for the answer."""

    path: str
    type: str
    many: bool = False
    method: str = "GET"


def seed(tmp_home: Path) -> Any:
    """A dashboard with a channel, outputs in every state, jobs, ideas, stories, footage, runs and logs."""
    from datetime import UTC, datetime

    from ns_dashboard.fakes import FakeSeriesAdapter

    client, state = make_env(tmp_home)
    now = datetime.now(UTC).isoformat()
    seed_channel(state.db, state.paths, "after-dark", format="master_first")
    seed_channel(state.db, state.paths, "quiet-hours", status="paused")
    ready = seed_output(state.db, state.paths, kind="long", k=1, title="The Lift")
    seed_output(state.db, state.paths, kind="short", k=1, title="Floor 13", job_id=ready.split("/")[0])
    approved = seed_output(state.db, state.paths, kind="short", k=2, status="approved", title="Approved one")
    rejected = seed_output(state.db, state.paths, kind="short", k=3, title="Rejected one")
    uploaded = seed_output(state.db, state.paths, kind="short", k=4, title="Posted one")
    state.db.execute(
        "INSERT INTO sources(source_id, channel, path, imported_at) VALUES ('src-20261101-aaaa', 'after-dark', 'x.mp4', ?)", (now,)
    )
    seed_output(state.db, state.paths, kind="clip", k=1, title="A clip", source_id="src-20261101-aaaa")
    assert client.post(f"/api/outputs/{rejected}/reject", json={"reason": "bad audio"}).status_code == 200
    assert client.post(f"/api/outputs/{approved}/approve").status_code == 200
    assert client.post(f"/api/outputs/{uploaded}/approve").status_code == 200
    r = client.post(f"/api/outputs/{uploaded}/uploaded", json={"url": "https://youtu.be/abc"})
    assert r.status_code == 200, r.text
    # jobs: done (with outputs), failed, running, queued and scheduled
    store = state.store
    done = store.create("run", "after-dark", {"channel": "after-dark", "format": "shorts_only", "count": 1})
    store.mark_running(done.job_id)
    store.update_progress(done.job_id, 0.5, "captions (3/6)")
    store.finish(done.job_id, "done", "Done", {"output_ids": [ready]})
    bad = store.create("clip", "after-dark", {"source_id": "src-20261101-aaaa"})
    store.mark_running(bad.job_id)
    store.finish(bad.job_id, "failed", "NS-E202: whisper failed", {"error": {"code": "NS-E202", "hint": None}})
    verify = store.create("verify", None, {"quick": True})
    store.mark_running(verify.job_id)
    store.finish(verify.job_id, "done", "Done", {"passed": True})
    run = store.create("run", "after-dark", {"channel": "after-dark", "format": "master_first", "count": 1})
    store.mark_running(run.job_id)
    store.update_progress(run.job_id, 0.25, "voice (2/8)")
    store.create("clip", "after-dark", {"source_id": "src-20261101-bbbb", "schedule_at": "2999-01-01T02:00:00+00:00"})
    # ideas, a story with outputs, footage with a use, a run row, log lines
    client.post("/api/library/ideas", json={"channel": "after-dark", "premise": "A doorbell camera films tomorrow night.", "twist": "It is me."})
    state.db.execute(
        "INSERT INTO stories(job_id, channel, format, working_title, text, word_count, opening_line, critic_avg, revisions, created_at)"
        " VALUES (?, 'after-dark', 'master_first', 'The Lift', 'text', 2300, 'It began.', 8.4, 1, ?)",
        (ready.split("/")[0], now),
    )
    state.db.execute(
        "INSERT INTO stories(job_id, channel, format, working_title, text, word_count, opening_line, critic_avg, revisions, created_at)"
        " VALUES ('after-dark-20261102-ffff', 'after-dark', 'shorts_only', NULL, 't', 120, 'x', NULL, 0, ?)", (now,)
    )
    state.db.execute(
        "INSERT INTO clips(source, clip_id, url, license, width, height, duration, local_path, fetched_at, keywords)"
        " VALUES ('pexels', '123', 'https://x', 'Pexels', 1920, 1080, 9.5, 'c.mp4', ?, '[\"night\", \"city\"]')", (now,)
    )
    state.db.execute(
        "INSERT INTO clip_usage(source, clip_id, channel, job_id, item, used_at) VALUES ('pexels', '123', 'after-dark', 'j1', 'long', ?)", (now,)
    )
    state.db.execute(
        "INSERT INTO runs(run_id, started_at, finished_at, mode, summary_json) VALUES ('r1', ?, ?, 'daily', ?)",
        (now, now, json.dumps({
            "jobs": [{"job_id": "j1", "channel": "after-dark", "stages": 12}], "failures": [],
            # P47: what `daily._hold_for_pause` writes for a channel paused during the night
            "paused": [{"channel": "after-dark", "jobs": 1, "reason": "its channel was paused during the night", "job_id": "j2"}],
            # night-fits: what `daily` writes for work left for time (S5, S6, S3) and the night's time
            "out_of_time": [
                {"channel": "after-dark", "fmt": "master_first", "jobs": 1, "need_s": 11400, "left_s": 3120, "basis": "history"},
                {"channel": "after-dark", "job_id": "j3", "stage": "voice", "jobs": 1, "need_s": 900, "left_s": 600, "basis": "default"},
                {"channel": "after-dark", "job_id": "j4", "stage": None, "jobs": 1, "resumed": True, "need_s": None, "left_s": None, "basis": "default"},
            ],
            "timing": {"task_start": now, "start_basis": "stub", "limit_s": 21600, "deadline": now, "used_s": 20400,
                       "left_s": 600, "stages_s": 18000, "gpu_s": 1200, "wait_s": None, "load_s": None},
        })),
    )
    (state.paths.home / "run").mkdir(exist_ok=True)
    (state.paths.home / "run" / "morning_note.json").write_text(
        json.dumps({"notes": [{"at": now, "title": "Nightshift", "message": "2 videos are ready"}]}), encoding="utf-8"
    )
    state.log.info("job_start", "hello", job_id="x")
    series = state.adapters.series
    assert isinstance(series, FakeSeriesAdapter)
    series.stories = [
        {
            "slug": "the-lift", "channel": "after-dark", "status": "producing", "title": "The Lift", "next_step": "produce",
            "has_design": True, "chosen": "A", "drafts": [],
            "parts": [{"n": 1, "title": "Floor 13", "minutes": 15, "status": "approved", "job_id": None, "words": 2250, "shorts": 3}],
        }
    ]
    return client, state, {"ready": ready, "approved": approved, "done": done.job_id}


CASES = [
    Case("/api/status", "Status"),
    Case("/api/outputs", "Output", many=True),
    Case("/api/outputs?status=pending", "Output", many=True),
    Case("/api/outputs/{ready}", "OutputDetail"),
    Case("/api/jobs", "Job", many=True),
    Case("/api/jobs/{done}", "Job"),
    Case("/api/channels", "ChannelCard", many=True),
    Case("/api/channels/after-dark", "ChannelDetail"),
    Case("/api/templates", "Template", many=True),
    Case("/api/voices", "Voice", many=True),
    Case("/api/settings", "Settings"),
    Case("/api/learn/summary", "Learn"),
    Case("/api/library/ideas", "Idea", many=True),
    Case("/api/library/stories", "Story", many=True),
    Case("/api/library/footage", "Footage", many=True),
    Case("/api/library/formats", "Format", many=True),
    Case("/api/library/series", "SeriesStory", many=True),
    Case("/api/runs", "Run", many=True),
    Case("/api/logs/tail", "LogLine", many=True),
    Case("/api/update/state", "UpdateState"),
    Case("/api/notes", "MorningNote", many=True),
    Case("/api/assistant/tools", "ToolInfo", many=True),
]


@pytest.fixture
def world(tmp_home: Path) -> Any:
    return seed(tmp_home)


def problems_of(client: Any, ids: dict[str, str], case: Case) -> list[str]:
    path = case.path.format(**ids)
    r = client.request(case.method, path)
    assert r.status_code == 200, f"{case.method} {path}: {r.status_code} {r.text[:200]}"
    return check_payload(case.type, r.json(), case.many, case.path.split("?")[0])


def test_types_ts_is_readable():
    ifaces, aliases = TYPES
    for case in CASES:
        assert case.type in ifaces or case.type in aliases, f"types.ts has no `{case.type}` (route {case.path})"
    members, index = all_members(ifaces, "Settings")
    assert "schedule_time" in members and index is None  # Settings is closed: every key is declared


def test_the_reader_flags_a_renamed_field_and_a_null():
    # the two classic mismatches: `word_count` against `words`, and a null where the page calls .toFixed
    ifaces, aliases = parse_types(
        "export interface Story { title: string; words: number; critic_avg: number; status?: string }"
    )
    global TYPES
    saved = TYPES
    TYPES = (ifaces, aliases)
    try:
        found = check_payload("Story", {"title": "T", "word_count": 5, "critic_avg": None}, False, "/x")
    finally:
        TYPES = saved
    assert any("words: required by the UI type but missing" in p for p in found)
    assert any("word_count: in the answer but not declared" in p for p in found)
    assert any("critic_avg: expected number, got null" in p for p in found)


def test_every_route_the_ui_calls_answers_what_types_ts_declares(world: Any):
    client, _state, ids = world
    found: dict[str, str] = {}
    for case in CASES:
        for problem in problems_of(client, ids, case):
            found[problem] = case.path
    unexpected = sorted(p for p in found if p not in KNOWN_MISMATCHES)
    fixed = sorted(k for k in KNOWN_MISMATCHES if k not in found)
    assert not unexpected, "API and UI disagree:\n  " + "\n  ".join(unexpected)
    assert not fixed, "these mismatches are gone, delete them from KNOWN_MISMATCHES:\n  " + "\n  ".join(fixed)


def test_error_codes_the_ui_names_exist_in_the_error_table():
    """The Library's voice states (NS-E310 to NS-E316) and every other `NS-E...` the pages name are real codes."""
    from ns_common.errors import load_errors

    known = load_errors()
    src = TYPES_TS.parents[1]
    named: set[str] = set()
    for f in src.rglob("*.tsx"):
        named |= set(re.findall(r'"(NS-[EW]\d{3})"', f.read_text(encoding="utf-8")))
    assert {"NS-E310", "NS-E311", "NS-E312", "NS-E314", "NS-E315", "NS-E316"} <= named
    assert not sorted(c for c in named if c not in known), "codes in the pages that contracts/errors.json lacks"
