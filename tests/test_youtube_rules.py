# ruff: noqa: E501
"""Card posting-sheet-true: every upload sheet holds what YouTube Studio accepts (rules R1 to R9).

The rules and their sources live in `ns_studio/media/youtube_rules.py`. The tests enter where the product
does: the outbox stages (story and clipper), `build_metadata`, the policy gate's `compute_advice`, the
dashboard's `GET /api/outputs/{id}` and the dashboard's start sweep.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg
from ns_common.config import save_channel
from ns_common.models import Metadata, PolicyAdvice, Seo
from ns_common.paths import Paths
from ns_studio.media import outbox
from ns_studio.media.shotplan import Shot
from tests.helpers import make_channel, make_ctx

try:  # the module is new with this card: a missing module must fail the guard test at its assertion
    yr: Any = importlib.import_module("ns_studio.media.youtube_rules")
except ImportError:
    yr = None
try:  # new with the review of 149f3af: the one reader of sheet files
    sr: Any = importlib.import_module("ns_studio.media.sheet_read")
except ImportError:
    sr = None

REPO = Path(__file__).resolve().parent.parent
CLONE = {"mode": "clone", "name": "owner-clone"}
SEO = {
    "title": "Scary Story: The Doorbell Camera",
    "description": "A camera records someone at 3:12.\nOriginal fiction.",
    "tags": [f"tag{i}" for i in range(10)],
    "hashtags": ["#Shorts", "#horror", "#scarystory"],
    "keywords": ["scary story"],
}


def pasted_bytes(description: str, hashtags: list[str]) -> int:
    """What lands in Studio's description box from the sheet: description, blank line, hashtags; CR LF."""
    text = description + ("\n\n" + " ".join(hashtags) if hashtags else "")
    return len(text.encode("utf-8")) + text.count("\n")


def youtube_tag_chars(tags: list[str]) -> int:
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def _shot() -> Shot:
    return Shot(0, 0.0, 2.0, "fake", "a-1", "x.mp4", 0.0, 1080, 1920, "fake://a/1", "fake", "hallway")


def meta_of(seo: dict[str, Any], advice: PolicyAdvice | None = None) -> Metadata:
    return outbox.build_metadata(
        channel=make_channel(), job_id="after-dark-20261101-ab12", kind="short", k=1, seo=Seo(**seo),
        advice=advice or PolicyAdvice(), shots=[_shot()], combo_id="c", prompt_versions={},
        publish_at="2026-11-02T18:05",
    )


def run_short(ctx, tmp_path: Path, seo: dict[str, Any], duration_s: float = 30.0) -> tuple[dict, str]:
    """The Shorts outbox stage on a fake-services job; returns the `.json` sheet and the `.txt` sheet."""
    video = tmp_path / f"short-{ctx.job_id}.mp4"
    video.write_bytes(b"\x00" * 1024)
    ctx.write_json("render/report.json", {"s1": {"path": str(video), "duration_s": duration_s}})
    ctx.write_json("qc/report.json", {"s1": {"passed": True, "reason": None}})
    ctx.write_json("seo.json", [seo])
    ctx.write_json("clips/shots.json", {"s1": {"total_s": 6.0, "treatment": "t", "shots": [_shot().to_json()]}})
    ctx.db.execute(
        "insert or ignore into clips(source, clip_id, url, license, fetched_at) values (?,?,?,?,?)",
        ("fake", "a-1", "fake://a/1", "fake", "2026-11-01T00:00:00+00:00"),
    )
    outbox.OutboxStage().run(ctx)
    entry = ctx.read_json("outbox/manifest.json")["s1"]
    return json.loads(Path(entry["json"]).read_text(encoding="utf-8")), Path(entry["txt"]).read_text("utf-8")


def run_master(ctx, tmp_path: Path, seo_long: dict[str, Any], chapters: list[dict], total_s: float = 200.0):
    """The master outbox (`run_outbox_master`); returns the master's `.json` and `.txt` sheets."""
    master = tmp_path / f"master-{ctx.job_id}.mp4"
    master.write_bytes(b"x" * 64)
    ctx.write_json("long/chapters.json", {"total_s": total_s, "midroll_suggestions_s": [], "chapters": chapters})
    ctx.write_json("render/long.json", {"path": str(master), "duration_s": total_s})
    ctx.write_json("clips/shots.json", {"long": {"shots": []}})
    ctx.write_json("seo_long.json", seo_long)
    outbox.run_outbox_master(ctx)
    entry = ctx.read_json("outbox/manifest.json")["long"]
    return json.loads(Path(entry["json"]).read_text(encoding="utf-8")), Path(entry["txt"]).read_text("utf-8")


THREE_CHAPTERS = [
    {"timestamp": "00:00", "title": "One", "start_s": 0.0},
    {"timestamp": "01:10", "title": "Two", "start_s": 70.0},
    {"timestamp": "02:20", "title": "Three", "start_s": 140.0},
]


def run_clip(ctx, tmp_path: Path, sheet: dict[str, Any], duration_s: float = 40.0) -> tuple[dict, str]:
    """The clipper's outbox stage with the files its earlier stages leave; returns `.json` and `.txt`."""
    from ns_studio.clipper import stages_clip as sc

    video = tmp_path / f"clip-{ctx.job_id}.mp4"
    video.write_bytes(b"\x00" * 512)
    ctx.write_json("source.json", {"source_id": "src-1", "original_name": "My Stream.mp4", "title": "My Stream"})
    ctx.db.execute(
        "insert or ignore into sources(source_id, channel, path, imported_at, status) values (?,?,?,?,?)",
        ("src-1", ctx.channel.slug, str(video), "2026-11-01T00:00:00+00:00", "rendered"),
    )
    ctx.write_json("clip_layout.json", {"template": "center_crop"})
    ctx.write_json("render/report.json", {"c1": {"path": str(video), "duration_s": duration_s}})
    ctx.write_json("qc/report.json", {"c1": {"passed": True, "reason": None}})
    ctx.write_json("seo_clips.json", [{"k": 1, **sheet}])
    ctx.write_json("policy_text.json", {
        "stage": "text", "passed": True, "checks": {},
        "advice": {"ai_disclosure": "no", "made_for_kids": False, "age_restrict_suggested": False},
    })
    sc.OutboxStage().run(ctx)
    entry = ctx.read_json("outbox/manifest.json")["c1"]
    return json.loads(Path(entry["json"]).read_text(encoding="utf-8")), Path(entry["txt"]).read_text("utf-8")


# ---------------------------------------------------------------------------------------------- R1
def test_angle_brackets_are_removed_from_every_field(tmp_home: Path, tmp_path: Path):
    seo = {**SEO, "title": "The <Last>  Door: a<b>c", "description": "She said <run> twice.\nOriginal fiction.",
           "tags": ["night <bus>", *SEO["tags"]], "hashtags": ["#Shorts", "#<horror>"]}
    meta, txt = run_short(make_ctx(tmp_home), tmp_path, seo)
    assert meta["title"] == "The Last Door: abc"
    assert meta["description"].startswith("She said run twice.")
    assert meta["tags"][0] == "night bus" and meta["hashtags"] == ["#Shorts", "#horror"]
    clip, ctxt = run_clip(make_ctx(tmp_home, fmt="clip"), tmp_path, {
        "title": "Best <play> ever", "description": "A <great> moment.\n\nClip from: My Stream",
        "tags": ["a<b>c", "stream"], "hashtags": ["#Shorts", "#<gg>"], "keywords": [],
    })
    assert clip["title"] == "Best play ever" and clip["tags"][0] == "abc" and "#gg" in clip["hashtags"]
    assert clip["description"].startswith("A great moment.")
    for sheet, text in ((meta, txt), (clip, ctxt)):  # nothing YouTube refuses reaches the sheet or the .txt
        fields = [sheet["title"], sheet["description"], *sheet["tags"], *sheet["hashtags"]]
        assert not any(c in f for f in fields for c in "<>"), fields
        assert "TITLE: " + sheet["title"] in text and re.search(r"^  Made for kids: (Yes|No)$", text, re.M)  # R9


# ---------------------------------------------------------------------------------------------- R2
def test_title_is_1_to_100_characters(tmp_home: Path, tmp_path: Path):
    hundred = ("word " * 20).strip()[:99] + "z"  # 100 characters
    assert meta_of({**SEO, "title": hundred}).title == hundred
    # counted in UTF-16 units, the safe side until the kit's check E1 says how Studio counts: an emoji is 2
    emoji = "\U0001F47B" + "a" * 98  # 99 code points, 100 units
    assert meta_of({**SEO, "title": emoji}).title == emoji
    assert meta_of({**SEO, "title": emoji + "a"}).title == emoji  # 101 units: one letter goes
    tail = meta_of({**SEO, "title": "a" * 99 + "\U0001F47B"}).title  # 101 units: never half of the emoji
    assert tail == "a" * 99 and len(tail.encode("utf-16-le")) // 2 == 99
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    long_title = "The Night Bus That Never Stopped At Harrow Street And What The Driver Told Me At 3 AM, Again"
    long_title += " Tonight."  # 101 characters
    assert len(long_title) == 101
    meta, txt = run_master(ctx, tmp_path, {**SEO, "title": long_title,
                                           "description": "Summary.\n\n[[CHAPTERS]]\n\nOriginal fiction."},
                           THREE_CHAPTERS)
    assert len(meta["title"]) <= 100 and long_title.startswith(meta["title"]) and not meta["title"].endswith(" ")
    assert long_title[len(meta["title"])] == " "  # cut before a word, not inside one
    assert "shortened the title to 100 characters" in txt
    empty = make_ctx(tmp_home)  # empty after cleaning: the story's working title
    empty.write_json("story.json", {"working_title": "The Doorbell", "text": "x"})
    meta, txt = run_short(empty, tmp_path, {**SEO, "title": " <> "})
    assert meta["title"] == "The Doorbell" and "working title" in txt


# ---------------------------------------------------------------------------------------------- R3
def test_description_is_at_most_5000_bytes_after_the_chapters_and_fixed_lines(tmp_home: Path, tmp_path: Path):
    rest = "\nPart 2 of 5.\nNext: part 3.\n\n[[CHAPTERS]]\n\nOriginal fiction."
    summary = ("The bus came back at midnight. " * 170)[: 4990 - len(rest)].strip()
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    seo_long = {**SEO, "hashtags": ["#horror", "#story"], "description": summary + rest}
    assert len(seo_long["description"]) <= 4990  # 4,990 ASCII characters; the chapter list takes it over
    meta, txt = run_master(ctx, tmp_path, seo_long, THREE_CHAPTERS)
    desc = meta["description"]
    assert pasted_bytes(desc, meta["hashtags"]) <= 5000
    assert "\n00:00 One\n01:10 Two\n02:20 Three\n" in desc and desc.endswith("Original fiction.")
    assert "Part 2 of 5.\nNext: part 3." in desc  # the series lines stay
    head = desc.split("\nPart 2 of 5.")[0]
    assert head.endswith("midnight.") and summary.startswith(head)  # shortened at a sentence
    assert "shortened the description to 5,000 bytes" in txt
    # both ends of the number (no hashtags, so the description alone is measured)
    assert meta_of({**SEO, "description": "x" * 5000, "hashtags": []}).description == "x" * 5000
    cut = meta_of({**SEO, "description": "x" * 4999 + "\u00e9", "hashtags": []}).description
    assert pasted_bytes(cut, []) <= 5000 and cut == "x" * 4999
    four = meta_of({**SEO, "description": "\U0001F47B" * 2000, "hashtags": []}).description  # 8,000 bytes
    assert four == "\U0001F47B" * 1250  # cut by bytes, never inside a character
    # a non-Latin channel: three bytes a character, cut at the Japanese full stop
    jp = "\u591c\u306e\u30d0\u30b9\u304c\u623b\u3063\u305f\u3002" * 190  # 1,710 characters, 5,130 bytes
    out = meta_of({**SEO, "description": jp, "hashtags": []}).description
    assert pasted_bytes(out, []) <= 5000 and out.endswith("\u3002") and jp.startswith(out)
    # a description with line breaks is measured as the Windows sheet pastes it: CR LF
    lines = "\n".join(["y" * 99] * 50)  # 4,999 characters, 5,048 bytes with CR LF
    assert pasted_bytes(meta_of({**SEO, "description": lines, "hashtags": []}).description, []) <= 5000


# ---------------------------------------------------------------------------------------------- R4
def test_tags_total_counts_quotes_and_commas_like_youtube(tmp_home: Path):
    from ns_studio.text.seo import normalize_tags

    assert yr is not None and yr.tags_length(["a b", "c"]) == 7  # `"a b",c`
    nine = [f"{i} " + "t" * 48 for i in range(9)]  # 50 characters with a space: 52 each, 9 * 52 + 8 = 476
    exact = [*nine, "u" * 23]  # + 1 + 23 = 500
    assert youtube_tag_chars(exact) == 500 and meta_of({**SEO, "tags": exact}).tags == exact
    over = [*nine, "u" * 24]  # 501: the last tag goes
    assert meta_of({**SEO, "tags": over}).tags == nine
    dirty = meta_of({**SEO, "tags": ["a<b>", "c,d", "   ", "e"]}).tags
    assert dirty == ["ab", "c d", "e"]
    many = [f"{i:02d} word" + "w" * 16 for i in range(20)]  # 24 characters with a space each
    assert youtube_tag_chars(normalize_tags(many, "scary story")) <= 500  # the SEO finaliser counts as YouTube


# ---------------------------------------------------------------------------------------------- R5
def test_hashtags_are_single_words_with_a_hash_and_at_most_five(tmp_home: Path):
    meta = meta_of({**SEO, "hashtags": ["#scary story", "horror", "#Horror", "#a<b>", "#true-crime"]})
    assert meta.hashtags == ["#scarystory", "#horror", "#ab", "#truecrime"]  # a hyphen ends a hashtag
    arabic = meta_of({**SEO, "hashtags": ["#قصة رعب"]}).hashtags  # two words
    assert arabic == ["#قصةرعب"]
    assert yr is not None
    tags, changes = yr.fit_hashtags([f"#t{i}" for i in range(8)])
    assert tags == [f"#t{i}" for i in range(5)] and changes == ["kept the first 5 hashtags"]


# ---------------------------------------------------------------------------------------------- R6
@pytest.mark.parametrize("chapters, why", [
    (THREE_CHAPTERS[:2], "at least 3 chapters"),
    ([THREE_CHAPTERS[0], {"timestamp": "00:05", "title": "Two", "start_s": 5.0}, THREE_CHAPTERS[2]],
     "chapter 1 is shorter than 10 seconds"),
])
def test_chapters_start_at_zero_are_ascending_and_at_least_10_seconds(tmp_home, tmp_path, chapters, why):
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    seo_long = {**SEO, "description": "Summary.\n\n[[CHAPTERS]]\n\nOriginal fiction."}
    meta, txt = run_master(ctx, tmp_path, seo_long, chapters)
    assert not re.search(r"^\d\d:\d\d ", meta["description"], re.M), meta["description"]
    assert meta["description"] == "Summary.\n\nOriginal fiction." and not meta.get("chapters")
    assert "left the chapter list out of the description" in txt and why in txt
    good = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    meta, _ = run_master(good, tmp_path, seo_long, THREE_CHAPTERS)
    assert "00:00 One\n01:10 Two\n02:20 Three" in meta["description"]


# ---------------------------------------------------------------------------------------------- R7
def test_a_short_over_three_minutes_is_flagged(tmp_home: Path, tmp_path: Path):
    _, txt = run_short(make_ctx(tmp_home), tmp_path, SEO, duration_s=180.0)
    assert "WARNING FOR YOUTUBE" not in txt and txt.startswith("FILE: ")
    _, txt = run_short(make_ctx(tmp_home), tmp_path, SEO, duration_s=180.1)
    first = txt.splitlines()[0]
    assert first.startswith("WARNING FOR YOUTUBE: ") and "3:00 long" in first and "3 minutes" in first


# ---------------------------------------------------------------------------------------------- R8
def test_a_cloned_voice_makes_the_disclosure_yes_with_the_reason(tmp_home: Path, tmp_path: Path):
    from ns_studio.policy import final_checks as fc

    def gated(voice: dict) -> str:
        ctx = make_ctx(tmp_home, channel=make_channel(voice=voice))
        ctx.write_json("audio/voice.json", {"mode": voice["mode"]})  # the voice stage's record
        ctx.write_json("story.json", {"text": "A man heard knocking in an old farmhouse."})
        ctx.write_json("policy_notes.json", {"intensity": "low", "real_setting": False})
        return fc.compute_advice(ctx, place_ner=lambda t: []).ai_disclosure

    assert gated(CLONE) == "yes"  # gates on
    assert gated({"mode": "preset", "speaker": "Ryan"}) == "no"  # a preset and a designed voice: unchanged
    assert gated({"mode": "design", "name": "warm-narrator", "description": "warm"}) == "no"
    ctx = make_ctx(tmp_home, channel=make_channel(voice=CLONE))  # policy off: no gate file
    ctx.write_json("audio/voice.json", CLONE)
    assert outbox.load_advice(ctx).ai_disclosure == "yes"
    meta, txt = run_short(ctx, tmp_path, SEO)
    assert meta["ai_disclosure"] == "yes"
    line = "  Altered or synthetic content: Yes (the narration is a voice cloned from a person's recording)"
    assert line in txt.splitlines() and "own voice" in txt  # YouTube's own-voice exception, left to the owner
    preset = make_ctx(tmp_home)  # the job's own record wins over the channel's current voice
    preset.write_json("audio/voice.json", {"mode": "clone", "name": "owner-clone"})
    assert outbox.load_advice(preset).ai_disclosure == "yes"
    narrated = make_ctx(tmp_home)
    narrated.write_json("audio/voice.json", {"mode": "preset", "name": "Ryan"})
    plain, ptxt = run_short(narrated, tmp_path, SEO)
    assert plain["ai_disclosure"] == "no" and "Altered or synthetic content: No (" in ptxt
    # no record of the voice (a job of an earlier release): never the channel's CURRENT voice; the owner
    # decides, with the rule quoted
    unknown = make_ctx(tmp_home, channel=make_channel(voice=CLONE))
    umeta, utxt = run_short(unknown, tmp_path, SEO)
    assert umeta["ai_disclosure"] == "no"  # the .json keeps the gates' answer (a 0.1.14 reader needs yes or no)
    assert "  Altered or synthetic content: You decide (Nightshift did not record which voice narrated" in utxt
    assert "Creators must disclose GenAI content that: Makes a real person appear to say or do something" in utxt
    # the dashboard computes the same answer and its reason where it shows them
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_dashboard.handlers import build_state
    from ns_dashboard.server import create_app

    paths = Paths.from_env()
    save_channel(paths, make_channel(voice=CLONE))
    state = build_state(paths, fake_services=True)
    try:
        client = TestClient(create_app(state=state, start_worker=False, static_dir=tmp_path),
                            base_url="http://127.0.0.1:8100")
        body = client.get(f"/api/outputs/{ctx.job_id}/s1").json()
        assert body["ai_disclosure"] == "yes"
        assert body.get("ai_disclosure_reason") == "the narration is a voice cloned from a person's recording"
        assert "own voice" in (body.get("ai_disclosure_note") or "")
    finally:
        state.db.close()


def test_the_queue_line_follows_the_answer_and_its_reason():
    src = (REPO / "ns_dashboard" / "ui" / "src" / "pages" / "Queue.tsx").read_text(encoding="utf-8")
    assert "The voice is production help" not in src  # the fixed text that said No for a cloned voice
    assert "ai_disclosure_reason" in src and "ai_disclosure_note" in src
    types = (REPO / "ns_dashboard" / "ui" / "src" / "lib" / "types.ts").read_text(encoding="utf-8")
    assert "ai_disclosure_reason?: string | null" in types and "youtube_notes?: string[]" in types
    assert "d.chapters.map" not in src  # the description is copied as the sheet has it, chapters once


# ---------------------------------------------------------------------------------------------- notes
def test_the_sheet_says_what_was_changed_for_youtube(tmp_home: Path, tmp_path: Path):
    body = "Caf\u00e9 noir, minuit pass\u00e9. " * 190  # 4,750 characters, 5,130 bytes
    seo = {**SEO, "title": "The <Last> Door", "description": body.strip() + "\nOriginal fiction."}
    meta, txt = run_short(make_ctx(tmp_home), tmp_path, seo)
    assert "Changed for YouTube: removed < and > from the title; shortened the description to 5,000 bytes" in txt
    assert meta["description"].endswith("\nOriginal fiction.") and pasted_bytes(meta["description"], meta["hashtags"]) <= 5000
    _, clean = run_short(make_ctx(tmp_home), tmp_path, SEO)
    assert "Changed for YouTube" not in clean  # a sheet that needed nothing says nothing


def test_retime_fits_again_and_keeps_what_the_sheet_told_the_owner(tmp_home: Path, tmp_path: Path):
    ctx = make_ctx(tmp_home)
    run_short(ctx, tmp_path, {**SEO, "title": "The <Last> Door"})
    entry = ctx.read_json("outbox/manifest.json")["s1"]
    sheet = json.loads(Path(entry["json"]).read_text(encoding="utf-8"))
    sheet["tags"] = ["a<b", *sheet["tags"]]  # an older writer left a bracket in
    Path(entry["json"]).write_text(json.dumps(sheet), encoding="utf-8")
    meta = outbox.retime_sheet(entry, "2026-11-05T19:00")
    assert meta is not None and meta.tags[0] == "ab" and meta.publish_after == "2026-11-05T19:00"
    txt = Path(entry["txt"]).read_text(encoding="utf-8")
    assert "removed < and > from the title" in txt and "removed < and > from the tags" in txt
    assert "2026-11-05 19:00" in txt


# ---------------------------------------------------------------------------------------------- sweep
def _rc3_sheet(paths: Paths, db, *, slug: str, job_id: str, title: str, status: str) -> Path:
    """An output and its sheets as release 0.1.15-rc3 wrote them: the `.json` as it was (no fitting) and the
    `.txt` in rc3's layout (its altered-content line with no reason, no notes)."""
    folder = paths.outbox / slug / "stories" / job_id / "shorts"
    folder.mkdir(parents=True, exist_ok=True)
    mp4 = folder / f"{job_id} - Short 01.mp4"
    mp4.write_bytes(b"\x00" * 64)
    meta = {"channel": slug, "job_id": job_id, "kind": "short", "k": 1, "title": title,
            "description": "A story.\nOriginal fiction.", "tags": ["night bus", "story"],
            "hashtags": ["#Shorts"], "publish_after": "2026-11-02T18:05", "ai_disclosure": "no",
            "made_for_kids": False, "age_restrict_suggested": False, "clips": [], "combo_id": "c",
            "prompt_versions": {}}
    mp4.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    rc3 = outbox.sheet_text(Metadata.model_validate(meta), mp4.name)  # no notes: rc3's own text
    mp4.with_suffix(".txt").write_text(rc3.replace("\n", "\r\n"), encoding="utf-8", newline="")
    db.execute(
        "INSERT INTO outputs(output_id, job_id, channel, kind, k, title, path, duration_s, combo_id, status,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"{job_id}/s1", job_id, slug, "short", 1, title, str(mp4), 30.0, "c", status, "2026-11-01T00:00:00+00:00"),
    )
    return mp4


def _logs(paths: Paths) -> str:
    return "".join(f.read_text(encoding="utf-8") for f in paths.logs.glob("dashboard-*.jsonl"))


def test_sheets_of_an_earlier_release_are_rewritten_once(tmp_home: Path, tmp_path: Path, monkeypatch):
    from ns_common import __version__
    from ns_dashboard.handlers import build_state
    from ns_dashboard.svc_outputs import OutputsService

    paths = Paths.from_env()
    paths.ensure()
    save_channel(paths, make_channel())
    save_channel(paths, make_channel(slug="cloned", display_name="Cloned", voice=CLONE))
    state = build_state(paths, fake_services=True)
    db = state.db
    try:
        bracket = _rc3_sheet(paths, db, slug="after-dark", job_id="after-dark-20261101-aa01",
                             title="The <Last> Door", status="pending")
        torn = _rc3_sheet(paths, db, slug="after-dark", job_id="after-dark-20261101-aa05", title="Torn",
                          status="pending")
        good_json = torn.with_suffix(".json").read_text(encoding="utf-8")
        torn.with_suffix(".json").write_text("{not json", encoding="utf-8")
        edited = _rc3_sheet(paths, db, slug="after-dark", job_id="after-dark-20261101-aa06", title="Plain",
                            status="pending")
        mine = edited.with_suffix(".txt").read_text(encoding="utf-8").replace("TITLE: Plain", "TITLE: My own <title>")
        edited.with_suffix(".txt").write_text(mine, encoding="utf-8", newline="")  # the owner's Notepad edit
        missing = _rc3_sheet(paths, db, slug="after-dark", job_id="after-dark-20261101-aa08", title="Gone txt",
                             status="pending")
        missing.with_suffix(".txt").unlink()
        unknown = _rc3_sheet(paths, db, slug="cloned", job_id="cloned-20261101-aa02", title="Quiet",
                             status="approved")
        recorded = _rc3_sheet(paths, db, slug="cloned", job_id="cloned-20261101-aa07", title="Heard",
                              status="approved")
        (paths.job_dir("cloned-20261101-aa07") / "audio").mkdir(parents=True)
        (paths.job_dir("cloned-20261101-aa07") / "audio" / "voice.json").write_text('{"mode": "clone"}', "utf-8")
        posted = _rc3_sheet(paths, db, slug="after-dark", job_id="after-dark-20261101-aa03", title="Old <One>",
                            status="uploaded")
        dropped = _rc3_sheet(paths, db, slug="after-dark", job_id="after-dark-20261101-aa04", title="Gone <One>",
                             status="rejected")
        untouched = {p: (p.with_suffix(".json").read_bytes(), p.with_suffix(".txt").read_bytes())
                     for p in (posted, dropped)}

        writes: list[str] = []
        real_write = outbox.write_text_atomic

        def killed_after_one_sheet(path, text, **kw):  # the power goes after the first sheet's two files
            if len(writes) == 2:
                raise RuntimeError("killed")
            writes.append(Path(path).name)
            return real_write(path, text, **kw)

        sweep = getattr(outbox, "sweep_sheets", None)
        assert sweep is not None, "the start sweep's sheet pass (outbox.sweep_sheets)"
        monkeypatch.setattr(outbox, "write_text_atomic", killed_after_one_sheet)
        with pytest.raises(RuntimeError):
            sweep(paths, db, state.log)  # aa01 written, killed at aa05's first write (its rebuilt .txt)
        assert not (paths.data / "sheet-rules.json").exists()  # written last: the next start runs again
        monkeypatch.setattr(outbox, "write_text_atomic", real_write)
        assert writes == [bracket.with_suffix(".txt").name, bracket.with_suffix(".json").name]
        stamp = {p: p.stat().st_mtime_ns for p in (bracket.with_suffix(".txt"), bracket.with_suffix(".json"))}
        warned = _logs(paths).count('"sheet_unreadable"')

        state.worker.start()  # the dashboard's start: recovery, then the sweeps
        state.worker.stop()
        rules = json.loads((paths.data / "sheet-rules.json").read_text(encoding="utf-8"))
        assert rules["rules_version"] == yr.RULES_VERSION and rules["retry"] == {}
        assert all(p.stat().st_mtime_ns == t for p, t in stamp.items())  # the killed run's work is not redone
        meta = json.loads(bracket.with_suffix(".json").read_text(encoding="utf-8"))
        assert meta["title"] == "The Last Door"
        assert "removed < and > from the title" in bracket.with_suffix(".txt").read_text(encoding="utf-8")
        row = db.one("SELECT title FROM outputs WHERE output_id=?", ("after-dark-20261101-aa01/s1",))
        assert row is not None and row["title"] == "The Last Door"
        # the hand-edited sheet: kept as it was, and the new sheet and the Queue name the copy
        kept = edited.with_name(f"{edited.stem}.before-{__version__}.txt")
        assert kept.read_text(encoding="utf-8") == mine
        new = edited.with_suffix(".txt").read_text(encoding="utf-8")
        assert f"YOUR EDITS KEPT: this sheet was rewritten for YouTube's rules; the copy you edited is kept next to it as {kept.name}" in new
        detail = OutputsService(state).detail("after-dark-20261101-aa06/s1")
        assert any(kept.name in n for n in detail["youtube_notes"])
        assert "TITLE: Gone txt" in missing.with_suffix(".txt").read_text(encoding="utf-8")  # a missing .txt is written
        assert not list(missing.parent.glob("*.before-*"))  # and nothing is kept for it
        # no record of the voice: the owner decides (the channel's current clone is not taken as the voice heard)
        assert json.loads(unknown.with_suffix(".json").read_text(encoding="utf-8"))["ai_disclosure"] == "no"
        assert "Altered or synthetic content: You decide (" in unknown.with_suffix(".txt").read_text(encoding="utf-8")
        assert json.loads(recorded.with_suffix(".json").read_text(encoding="utf-8"))["ai_disclosure"] == "yes"
        for p, (j, t) in untouched.items():  # uploaded and rejected left alone
            assert (p.with_suffix(".json").read_bytes(), p.with_suffix(".txt").read_bytes()) == (j, t)
        # a .json that cannot be read is rebuilt from its .txt and the job's records, and says so
        assert _logs(paths).count('"sheet_unreadable"') == warned
        assert "upload sheets brought up to YouTube's rules: 5" in _logs(paths)  # aa05, aa06, aa08, aa02, aa07
        rebuilt, was = json.loads(torn.with_suffix(".json").read_text(encoding="utf-8")), json.loads(good_json)
        assert {k: rebuilt[k] for k in was} == was  # every field the sheet had, from the .txt and the records
        assert outbox.REBUILT in torn.with_suffix(".txt").read_text(encoding="utf-8")
        torn_detail = OutputsService(state).detail("after-dark-20261101-aa05/s1")
        assert not any(n.startswith("WARNING FOR YOUTUBE") for n in torn_detail["youtube_notes"])

        bracket.with_suffix(".json").write_text(json.dumps({**meta, "title": "Back <again>"}), encoding="utf-8")
        before = {p: p.stat().st_mtime_ns for p in paths.outbox.rglob("*.*")}
        state.worker.start()  # run twice: nothing changes and nothing is said
        state.worker.stop()
        assert "<again>" in bracket.with_suffix(".json").read_text(encoding="utf-8")  # done sheets are not redone
        assert {p: p.stat().st_mtime_ns for p in paths.outbox.rglob("*.*")} == before
        assert _logs(paths).count('"sheets_fitted"') == 1
    finally:
        state.worker.stop()
        db.close()


def _dashboard(paths: Paths):
    from ns_dashboard.handlers import build_state
    from ns_dashboard.svc_outputs import OutputsService

    state = build_state(paths, fake_services=True)
    return state, OutputsService(state)


def _warnings(detail: dict) -> list[str]:
    return [n for n in detail["youtube_notes"] if n.startswith("WARNING FOR YOUTUBE")]


class _Obstruction:
    """A sheet file another program holds (a byte-range lock, as Word or Excel take) or that cannot be opened
    for another reason (a folder of its name, the OS error a broken file gives); `clear()` undoes it."""

    def __init__(self, path: Path, how: str) -> None:
        self.path, self.how, self.cleared = path, how, False
        self.saved = path.read_bytes()
        if how == "held":
            import msvcrt

            self.fh = open(path, "r+b")  # noqa: SIM115 (kept open until clear)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, len(self.saved))
        else:
            path.unlink()
            path.mkdir()

    def clear(self) -> None:
        if self.cleared:
            return
        self.cleared = True
        if self.how == "held":
            import msvcrt

            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, len(self.saved))
            self.fh.close()
        else:
            self.path.rmdir()
            self.path.write_bytes(self.saved)


JSON_STATES = ("ok", "utf16", "missing", "torn", "layout", "held", "os_error")
TXT_STATES = ("ok", "utf16", "ansi", "missing", "held", "os_error", "layout")
needs_windows = pytest.mark.skipif(sys.platform != "win32", reason="a share lock and ANSI are Windows")


def _set_json(path: Path, how: str) -> _Obstruction | None:
    if how == "utf16":
        path.write_bytes(path.read_text(encoding="utf-8").encode("utf-16"))
    elif how == "missing":
        path.unlink()
    elif how == "torn":
        path.write_text('{"title": "The Doorb', encoding="utf-8")
    elif how == "layout":
        path.write_text('{"hello": "not a sheet"}', encoding="utf-8")
    elif how in ("held", "os_error"):
        return _Obstruction(path, how)
    return None


def _set_txt(path: Path, how: str) -> _Obstruction | None:
    text = path.read_text(encoding="utf-8").replace("TITLE: Scary Story", "TITLE: Scary Störy")
    if how == "utf16":
        path.write_bytes(text.encode("utf-16"))  # Notepad's "Unicode": UTF-16 with its mark
    elif how == "ansi":
        try:
            path.write_bytes(text.encode(sr.ansi_codec()))  # Notepad's "ANSI" on this PC
        except UnicodeEncodeError:
            pytest.skip("this PC's ANSI code page has no ö")
    elif how == "missing":
        path.unlink()
    elif how == "layout":
        path.write_text("my notes\nTitle idea: The Doorbell\n", encoding="utf-8")
    elif how in ("held", "os_error"):
        return _Obstruction(path, how)
    return None


def _whole(sheet: Path) -> bool:
    j, t = sr.read_sheet_file(sheet, "json"), sr.read_sheet_file(sheet.with_suffix(".txt"), "txt")
    return j.state == sr.OK and t.state == sr.OK


@needs_windows
@pytest.mark.parametrize("txt", TXT_STATES)
@pytest.mark.parametrize("jsn", JSON_STATES)
def test_every_cell_of_the_sheet_table_says_what_is_true_and_following_it_mends_the_sheet(
    tmp_home: Path, tmp_path: Path, jsn: str, txt: str
):
    """Each (.json, .txt) cell of `sheet_read.plan_sheet`'s table: what the Queue does on opening the video,
    the one sentence it shows, and that doing what the sentence says leaves a whole sheet (or, where the
    sheet is gone, that the file it names holds the fields)."""
    paths = Paths.from_env()
    paths.ensure()
    state, queue = _dashboard(paths)
    ctx = make_ctx(tmp_home)
    held: list[_Obstruction] = []
    try:
        run_short(ctx, tmp_path, SEO)
        sheet = Path(ctx.read_json("outbox/manifest.json")["s1"]["json"])
        tfile = sheet.with_suffix(".txt")
        tob = _set_txt(tfile, txt)
        held.extend([tob] if tob else [])
        changed_txt = tfile.read_bytes() if tfile.is_file() and tob is None else None
        job = _set_json(sheet, jsn)
        held.extend([job] if job else [])
        oid = f"{ctx.job_id}/s1"
        said = _warnings(queue.detail(oid))
        joined = " ".join(said)
        assert "reject" not in joined and "--resume" not in joined
        if job is not None:  # the .json is held or cannot be opened: only it is named
            assert len(said) == 1 and sheet.name in said[0] and tfile.name not in said[0]
            assert ("open in another program" if jsn == "held" else "could not be opened") in said[0]
            job.clear()  # done as said, then the video is opened again
            said = _warnings(queue.detail(oid))
            jsn = "ok"  # the next cell is the one of a readable .json
        bad_json = jsn in ("missing", "torn", "layout")
        if tob is not None:  # the .txt is held or cannot be opened
            assert len(said) == 1 and tfile.name in said[0]
            assert ("open in another program" if txt == "held" else "could not be opened") in said[0]
            if not bad_json:
                assert sheet.name not in said[0]  # only the file that failed is named
            tob.clear()
            said = _warnings(queue.detail(oid))
            changed_txt = None
        if bad_json and txt == "missing":  # gone: the job's own record is named, and it holds the fields
            assert len(said) == 1 and str(ctx.file("seo.json")) in said[0] and sr.RULES_TO_CHECK in said[0]
            wrote = json.loads(ctx.file("seo.json").read_text(encoding="utf-8"))[0]
            assert (wrote["title"], wrote["tags"]) == (SEO["title"], SEO["tags"])
            return
        if bad_json and txt == "layout":  # gone: the .txt named holds what the owner wrote
            assert len(said) == 1 and f"from {tfile.name}" in said[0] and sr.RULES_TO_CHECK in said[0]
            assert "The Doorbell" in tfile.read_text(encoding="utf-8")
            return
        assert said == [], said  # mended on opening
        assert _whole(sheet)
        meta = Metadata.model_validate_json(sheet.read_text(encoding="utf-8"))
        new_txt = tfile.read_text(encoding="utf-8")
        if txt in ("utf16", "ansi"):  # decoded, re-saved as UTF-8; the owner's file kept as it was
            assert "re-saved" in new_txt and "as UTF-8" in new_txt
            if bad_json:
                assert meta.title == "Scary Störy: The Doorbell Camera"  # decoded right, not guessed
            kept = list(tfile.parent.glob(f"{tfile.stem}.before-*.txt"))
            assert len(kept) == 1 and kept[0].read_bytes() == changed_txt
            assert "YOUR EDITS KEPT: " in new_txt and kept[0].name in new_txt
        elif txt == "layout":  # the owner's file is kept before the sheet is written again
            kept = list(tfile.parent.glob(f"{tfile.stem}.before-*.txt"))
            assert len(kept) == 1 and "Title idea: The Doorbell" in kept[0].read_text(encoding="utf-8")
        elif txt == "missing" or bad_json:
            assert f"TITLE: {meta.title}" in new_txt
        if bad_json:
            assert outbox.REBUILT in new_txt
        if jsn == "utf16":
            assert "re-saved" in new_txt
    finally:
        for ob in held:
            ob.clear()
        state.db.close()


def test_a_txt_no_encoding_can_read_is_saved_as_utf8_by_the_owner_as_the_sentence_says(tmp_home, tmp_path):
    """The column a single-byte ANSI code page never reaches (bytes no encoding decodes, as on a double-byte
    PC): with the .json readable the owner's file is kept and the sheet written again; with it torn the
    owner is asked to re-save it as UTF-8, and doing so mends the sheet."""
    paths = Paths.from_env()
    paths.ensure()
    state, queue = _dashboard(paths)
    try:
        ctx = make_ctx(tmp_home)
        run_short(ctx, tmp_path, SEO)
        sheet = Path(ctx.read_json("outbox/manifest.json")["s1"]["json"])
        tfile = sheet.with_suffix(".txt")
        good = sr.read_sheet_file(sheet, "json")
        undecoded = sr.SheetRead(tfile, sr.NOT_UTF8, raw=b"\x81\x8d", encoding="unknown")
        plan = sr.plan_sheet(good, undecoded, ctx.file("seo.json"), ctx.job_dir)
        assert (plan.action, plan.keep_txt, plan.sentence) == (sr.FIT, True, None)
        torn = sr.SheetRead(sheet, sr.TORN)
        plan = sr.plan_sheet(torn, undecoded, ctx.file("seo.json"), ctx.job_dir)
        assert plan.action == sr.WAIT and plan.sentence is not None
        assert f"open {tfile.name} in Notepad, choose File > Save as, set Encoding to UTF-8" in plan.sentence
        assert sheet.name in plan.sentence and "reject" not in plan.sentence
        # done as said: the owner re-saves the .txt as UTF-8, then opens the video again
        sheet.write_text("{torn", encoding="utf-8")
        tfile.write_text(tfile.read_text(encoding="utf-8"), encoding="utf-8")
        assert _warnings(queue.detail(f"{ctx.job_id}/s1")) == [] and _whole(sheet)
    finally:
        state.db.close()


@needs_windows
def test_a_sheet_that_stays_unreadable_is_tried_at_five_starts_then_left_with_a_word(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    state, queue = _dashboard(paths)
    try:
        torn = _rc3_sheet(paths, state.db, slug="after-dark", job_id="after-dark-20261101-bb01", title="Torn",
                          status="pending")
        torn.with_suffix(".json").write_text("{not json", encoding="utf-8")
        lock = _Obstruction(torn.with_suffix(".txt"), "held")
        held = [lock]
        for _ in range(6):
            outbox.sweep_sheets(paths, state.db, state.log)
        logs = _logs(paths)
        assert logs.count('"sheet_unreadable"') == 5  # tries 1 to 5, then the sweep is done with it
        assert "(try 4 of 5) and whenever the video is opened in the Queue" in logs
        assert "stopped trying at its starts after 5; opening the video in the Queue tries once more" in logs
        assert "reject" not in logs and "--resume" not in logs
        said = "which is open in another program: close that program, then open this video again"
        assert said in logs
        assert json.loads((paths.data / "sheet-rules.json").read_text(encoding="utf-8"))["retry"] == {}
        assert any(said in w for w in _warnings(queue.detail("after-dark-20261101-bb01/s1")))
        lock.clear()  # done as said
        detail = queue.detail("after-dark-20261101-bb01/s1")
        assert _warnings(detail) == [] and detail["title"] == "Torn" and _whole(torn.with_suffix(".json"))
    finally:
        for ob in locals().get("held", []):
            ob.clear()
        state.db.close()


def test_a_rebuild_whose_repair_changes_the_owners_text_keeps_their_file(tmp_home: Path, tmp_path: Path):
    paths = Paths.from_env()
    paths.ensure()
    state, queue = _dashboard(paths)
    try:
        ctx = make_ctx(tmp_home)
        run_short(ctx, tmp_path, SEO)
        sheet = Path(ctx.read_json("outbox/manifest.json")["s1"]["json"])
        tfile = sheet.with_suffix(".txt")
        mine = tfile.read_text(encoding="utf-8").replace("TITLE: Scary Story", "TITLE: Scary <Story>")
        tfile.write_text(mine, encoding="utf-8")
        sheet.write_text("{torn", encoding="utf-8")
        queue.detail(f"{ctx.job_id}/s1")
        kept = list(tfile.parent.glob(f"{tfile.stem}.before-*.txt"))
        assert len(kept) == 1 and "TITLE: Scary <Story>" in kept[0].read_text(encoding="utf-8")
        new = tfile.read_text(encoding="utf-8")
        assert "TITLE: Scary Story: The Doorbell Camera" in new and kept[0].name in new
        assert "removed < and > from the title" in new
    finally:
        state.db.close()


def test_a_deleted_hashtag_line_does_not_turn_the_description_into_hashtags():
    text = outbox.sheet_text(meta_of(SEO), "s1.mp4")
    gone = text.replace("\n\n#Shorts #horror #scarystory\n", "\n")  # the line and one blank line deleted
    assert "#Shorts" not in gone
    fields = sr.parse_sheet_text(gone)
    assert fields["hashtags"] == [] and fields["description"].endswith("Original fiction.")
    assert sr.parse_sheet_text(text)["hashtags"] == ["#Shorts", "#horror", "#scarystory"]


def test_the_rebuild_keeps_every_field_for_a_master_and_a_clip(tmp_home: Path, tmp_path: Path):
    paths = Paths.from_env()
    paths.ensure()
    state, queue = _dashboard(paths)
    try:
        ctx = make_ctx(tmp_home)
        ctx.write_json("prompt_versions.json", {"seo": 3})
        before, _ = run_short(ctx, tmp_path, SEO)
        sheet = Path(ctx.read_json("outbox/manifest.json")["s1"]["json"])
        sheet.write_text("{torn", encoding="utf-8")
        assert _warnings(queue.detail(f"{ctx.job_id}/s1")) == []
        after = json.loads(sheet.read_text(encoding="utf-8"))
        for key in ("title", "description", "tags", "hashtags", "publish_after", "clips", "combo_id",
                    "prompt_versions", "made_for_kids", "age_restrict_suggested", "ai_disclosure"):
            assert after[key] == before[key], key
        master = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
        mmeta, _ = run_master(master, tmp_path, {**SEO, "description": "Summary.\n\n[[CHAPTERS]]\n\nOriginal fiction."},
                              THREE_CHAPTERS)
        msheet = Path(master.read_json("outbox/manifest.json")["long"]["json"])
        msheet.write_text("", encoding="utf-8")
        assert _warnings(queue.detail(f"{master.job_id}/long")) == []
        assert json.loads(msheet.read_text(encoding="utf-8"))["chapters"] == mmeta["chapters"]
        clip = make_ctx(tmp_home, fmt="clip")
        cmeta, _ = run_clip(clip, tmp_path, {"title": "Best play", "description": "A moment.\n\nClip from: My Stream",
                                              "tags": ["stream"], "hashtags": ["#Shorts"], "keywords": []})
        csheet = Path(clip.read_json("outbox/manifest.json")["c1"]["json"])
        csheet.write_text("{", encoding="utf-8")
        assert _warnings(queue.detail(f"{clip.job_id}/c1")) == []
        rebuilt = json.loads(csheet.read_text(encoding="utf-8"))
        assert (rebuilt["source_video"], rebuilt["template"], rebuilt["title"]) == (
            cmeta["source_video"], cmeta["template"], cmeta["title"])
    finally:
        state.db.close()


_SHEET_PATH = re.compile(r'with_suffix\("\.(json|txt)"\)|\[\s*"(json|txt)"\s*\]')
_READ_CALL = re.compile(r"read_text|read_bytes|\bopen\(|json\.load|model_validate_json|_read_json|_read_text")


#: Files whose `.txt` beside a `.wav` is a narration sidecar, not an upload sheet.
_NOT_SHEETS = {"captions.py", "transcribe.py", "voice.py"}


def test_no_reader_opens_a_sheet_file_outside_the_one_function():
    """Static: no line of product code both names a sheet file and reads it, except `sheet_read.py` (and the
    audio sidecars of `_NOT_SHEETS`, which are not sheets)."""
    offenders = []
    for top in ("ns_studio", "ns_dashboard", "ns_supervisor"):
        for f in (REPO / top).rglob("*.py"):
            if f.name == "sheet_read.py" or f.name in _NOT_SHEETS:
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if _SHEET_PATH.search(line) and _READ_CALL.search(line):
                    offenders.append(f"{f.relative_to(REPO)}:{n}: {line.strip()}")
    assert offenders == [], offenders


def test_every_sheet_read_at_run_time_goes_through_the_one_function(tmp_home: Path, tmp_path: Path, monkeypatch):
    """Run time: the sweep, the Queue, the night's retime and its slot count open the Outbox's .json and
    .txt files only inside `sheet_read.read_sheet_file`."""
    import builtins
    import io
    from types import SimpleNamespace

    from ns_studio import daily

    paths = Paths.from_env()
    paths.ensure()
    state, queue = _dashboard(paths)
    try:
        ctx = make_ctx(tmp_home)
        run_short(ctx, tmp_path, SEO)
        entry = ctx.read_json("outbox/manifest.json")["s1"]
        Path(entry["json"]).write_text("{torn", encoding="utf-8")
        outside: list[str] = []
        real_open = io.open

        def watched(file, mode="r", *a, **kw):
            p = Path(str(file)) if isinstance(file, (str, Path)) else None
            reading = not any(c in str(mode) for c in "wax+")
            if p is not None and reading and p.suffix in (".json", ".txt") and paths.outbox in p.parents:
                frame, inside = sys._getframe(1), False
                while frame is not None:
                    inside = inside or frame.f_code.co_name == "read_sheet_file"
                    frame = frame.f_back
                if not inside:
                    outside.append(str(p))
            return real_open(file, mode, *a, **kw)

        # The watch ends with this block, before the assertion and the clean-up open anything. Only these two
        # patches end there: `monkeypatch.undo()` would also drop the test's home (`tmp_home` sets it through
        # this same fixture) and the session's fakes, which must stay for the whole test.
        with monkeypatch.context() as watching:
            watching.setattr(io, "open", watched)
            watching.setattr(builtins, "open", watched)
            outbox.sweep_sheets(paths, state.db, state.log)
            queue.detail(f"{ctx.job_id}/s1")
            outbox.retime_sheet(entry, "2026-11-05T19:00")
            env: Any = SimpleNamespace(db=state.db)  # the slot count reads only the database and the sheets
            daily.scheduled_shorts(env, "after-dark", datetime(2026, 11, 1).date(), set(), datetime(2026, 11, 2, tzinfo=UTC))
        assert outside == [], outside
    finally:
        state.db.close()


# ---------------------------------------------------------------------------------------------- properties
_ALPHABET = (
    list("abcdefghij XYZ0123456789") + [" ", "  ", "\n", "\n\n", "<", ">", ",", "-", "#", ".", "!", "?", "_"]
    + ["\U0001F47B", "\U0001F600", "\U0001F3F3️‍\U0001F308"]  # emoji, one with a ZWJ sequence
    + ["مرحبا", "بالعالم", "ً"]  # Arabic, a haraka
    + ["夜のバス", "。", "中文"]  # CJK and its full stop
    + ["é", "ä", "́"]  # combining marks, one alone
)


def _text(rng, most: int) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, most)))


def _cases(n: int = 300):
    import random

    rng = random.Random(20261111)
    for _ in range(n):
        yield {
            "title": _text(rng, 160),
            "description": _text(rng, rng.choice([50, 900, 4000])),
            "tags": [_text(rng, 40) for _ in range(rng.randint(0, 25))],
            "hashtags": ["#" * rng.randint(0, 2) + _text(rng, 12) for _ in range(rng.randint(0, 8))],
        }


def test_a_repaired_sheet_passes_the_check():
    assert yr is not None
    for case in _cases():
        f = yr.fit(**case, fallback_title="The working title")
        assert yr.problems(title=f.title, description=f.description, tags=f.tags, hashtags=f.hashtags) == [], case


def test_repairing_twice_equals_repairing_once():
    assert yr is not None
    for case in _cases():
        once = yr.fit(**case, fallback_title="The working title")
        twice = yr.fit(title=once.title, description=once.description, tags=once.tags, hashtags=once.hashtags,
                       fallback_title="The working title")
        assert (twice.title, twice.description, twice.tags, twice.hashtags) == (
            once.title, once.description, once.tags, once.hashtags), case
        assert twice.changes == [] and twice.warnings == [], (case, twice.changes)


# ---------------------------------------------------------------------------------------------- sources
def test_every_rule_names_its_source():
    assert yr is not None, "ns_studio/media/youtube_rules.py holds the rules"
    rules = [v for v in vars(yr).values() if isinstance(v, yr.Rule)]
    assert {r.id for r in rules} == {f"R{i}" for i in range(1, 10)} and set(rules) == set(yr.RULES)
    for r in rules:
        assert re.match(r"^https://(developers\.google\.com|support\.google\.com)/", r.source), r
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", r.checked) and r.quote, r


@pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg required (the voice stage measures loudness)")
def test_the_voice_stage_records_the_voice_it_narrated_with(tmp_home: Path):
    from ns_studio.media.voice import VoiceStage

    ctx = make_ctx(tmp_home, channel=make_channel(voice=CLONE))
    ctx.write_json("parts.json", [{"n": 1, "text": "One two three four five.", "hook": "", "est_seconds": 1.0}])
    VoiceStage().run(ctx)
    assert ctx.read_json("audio/voice.json")["mode"] == "clone"
