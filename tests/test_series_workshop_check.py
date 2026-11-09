# ruff: noqa: E501
"""`Workshop.check` against the real steps (D-27 break-it round).

The Assistant calls `Workshop.check(tool, args)` before it shows a confirm card. The card and the Yes behind it
must never disagree, so every rule lives once: each step starts with the same `_pre_*` helper that `check`
runs. This file proves it by walking the nine changing tools through every state of a story (nothing yet, drafts,
a chosen concept, a part map, a written part, a design, an approved part, a part in production) with arguments
that are good and bad: where `check` refuses, the real step refuses with the same words; where `check` lets it
pass, the real step does not refuse. A step whose rule is copied instead of shared fails here the day one copy
changes. It also proves `check` writes nothing.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from ns_studio.series.store import SeriesDoc, StoryDir, WorkshopError, stories_dir
from ns_studio.series.tools import TOOLS, call_tool
from ns_studio.series.workshop import Workshop
from tests.test_series_workshop import PREMISE, Recorder, make_workshop

CHANGING = [t["name"] for t in TOOLS if t["side_effect"]]
VOLATILE = (".db", ".db-wal", ".db-shm", ".log", ".jsonl", ".lock")


def argument_sets(slug: str) -> list[tuple[str, dict[str, Any]]]:
    """Good and bad arguments of every changing tool, for the story `slug` and for one that does not exist."""
    out: list[tuple[str, dict[str, Any]]] = [
        ("workshop_start", {"channel": "after-dark"}),
        ("workshop_start", {"channel": "no-such-channel"}),
        ("workshop_start", {"channel": "after-dark", "premise": "A ferry that arrives before it leaves"}),
    ]
    for s in (slug, "no-such-story"):
        out += [("workshop_concepts", {"slug": s, **extra}) for extra in ({}, {"n": 0}, {"n": 7}, {"n": 2})]
        out += [
            ("workshop_regenerate", {"slug": s, "letter": letter, "feedback": fb})
            for letter, fb in (("A", "darker"), ("b", "darker"), ("Z", "darker"), ("A", "   "))
        ]
        out += [("workshop_choose", {"slug": s, "letter": letter}) for letter in ("A", "c", "Z")]
        out += [
            ("workshop_map", {"slug": s, **extra})
            for extra in (
                {"parts": 3},
                {"parts": 0},
                {"parts": 41},
                {"parts": 3, "minutes_per_part": 12},
                {"parts": 3, "minutes_per_part": 5},
            )
        ]
        out += [("workshop_write_part", {"slug": s, "n": n}) for n in (1, 2, 9)]
        out += [
            ("workshop_revise_part", {"slug": s, "n": n, "feedback": fb})
            for n, fb in ((1, "tighten"), (1, "  "), (2, "tighten"), (9, "tighten"))
        ]
        out += [
            ("workshop_approve", {"slug": s, **extra})
            for extra in ({}, {"part": 1}, {"part": 2}, {"part": 9}, {"part": "all"}, {"part": "x"})
        ]
        out += [
            ("workshop_design", {"slug": s, **extra})
            for extra in (
                {},
                {"format": "no_such_format"},
                {"visual_treatment": "cinematic_teal"},
                {"caption_style": "no_such_style"},
                {"voice": "Aiden", "publish_cadence_days": 5},
            )
        ]
        out += [
            ("workshop_produce", {"slug": s, **extra})
            for extra in ({}, {"part": 1}, {"part": 2}, {"part": 9})
        ]
    return out


def tree(root: Path) -> dict[str, str]:
    """Every file under `root` (databases and logs aside) with a hash of its bytes."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.endswith(VOLATILE) and "logs" not in p.relative_to(root).parts
    }


def outcome(fn: Any, *args: Any) -> str | None:
    """None when `fn(*args)` returns, else what it raised in words."""
    try:
        fn(*args)
    except WorkshopError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001 - anything else is a finding too
        return f"{type(e).__name__}: {e}"
    return None


def restore(ws: Workshop, saved: Path) -> None:
    """Put the story folders and the job folders back as they were before a real step ran."""
    for src, dst in ((saved / "stories", stories_dir(ws.paths)), (saved / "jobs", ws.paths.jobs_dir)):
        shutil.rmtree(dst, ignore_errors=True)
        if src.exists():
            shutil.copytree(src, dst)


def keep(ws: Workshop, saved: Path) -> None:
    shutil.rmtree(saved, ignore_errors=True)
    for src, dst in ((stories_dir(ws.paths), saved / "stories"), (ws.paths.jobs_dir, saved / "jobs")):
        if src.exists():
            shutil.copytree(src, dst)


def walk(ws: Workshop, slug: str, state: str, saved: Path, problems: list[str]) -> int:
    """Compare `check` with the real step for every argument set in the story's current state."""
    seen = 0
    keep(ws, saved)
    for tool, args in argument_sets(slug):
        before = tree(ws.paths.home)
        refused = outcome(ws.check, tool, args)
        if tree(ws.paths.home) != before:
            problems.append(f"[{state}] {tool} {args}: check wrote something")
        real = outcome(call_tool, ws, tool, args)
        restore(ws, saved)
        seen += 1
        if refused is not None and real != refused:
            problems.append(f"[{state}] {tool} {args}: check says {refused!r}, the step says {real!r}")
        elif refused is None and real is not None:
            problems.append(f"[{state}] {tool} {args}: check lets it pass, the step refuses: {real!r}")
    return seen


def test_check_and_the_real_step_agree_in_every_state_of_a_story(tmp_home: Path):
    ws = make_workshop(tmp_home, enqueue=Recorder())
    saved = tmp_home.parent / "workshop-check-saved"
    problems: list[str] = []
    slug = ws.start("after-dark", PREMISE)
    steps = (
        ("fresh", lambda: None),
        ("drafted", lambda: ws.concepts(slug)),
        ("chosen", lambda: ws.choose(slug, "A")),
        ("mapped", lambda: ws.map_parts(slug, 3, 12)),
        ("written", lambda: ws.write_part(slug, 1)),
        ("designed", lambda: ws.design(slug)),
        ("approved", lambda: ws.approve(slug, 1)),
        ("producing", lambda: ws.produce(slug, 1)),
    )
    total = 0
    for state, advance in steps:
        advance()
        total += walk(ws, slug, state, saved, problems)
    assert not problems, "\n" + "\n".join(problems)
    assert total == len(steps) * len(argument_sets(slug))  # every state was walked with the whole argument table


def test_the_walk_sees_both_answers(tmp_home: Path):
    """The comparison is not vacuous: in a written story some calls pass and some are refused, with reasons."""
    ws = make_workshop(tmp_home, enqueue=Recorder())
    slug = ws.start("after-dark", PREMISE)
    ws.concepts(slug)
    ws.choose(slug, "A")
    ws.map_parts(slug, 3, 12)
    ws.write_part(slug, 1)
    answers = {
        (t, a.get("part", a.get("n", a.get("letter")))): outcome(ws.check, t, a)
        for t, a in argument_sets(slug)
        if a.get("slug") == slug
    }
    assert answers[("workshop_write_part", 1)] is None  # a written part can be rewritten
    assert answers[("workshop_write_part", 2)] is None
    assert "series has no part 9" in str(answers[("workshop_write_part", 9)])
    assert "choose" in str(answers[("workshop_approve", 2)]) or "cannot approve" in str(
        answers[("workshop_approve", 2)]
    )
    assert "no design yet" in str(answers[("workshop_produce", 1)])
    refused = [v for v in answers.values() if v is not None]
    assert len(refused) >= 10 and len(refused) < len(answers)


def test_revise_part_does_not_need_the_series_row(tmp_home: Path):
    """The review found one drift: `check` read the part's row of series.md, the real revise never does.

    With the row gone (a hand-edited series.md) a revise that works must not be refused before its card.
    """
    ws = make_workshop(tmp_home)
    slug = ws.start("after-dark", PREMISE)
    ws.concepts(slug)
    ws.choose(slug, "A")
    ws.map_parts(slug, 3, 12)
    ws.write_part(slug, 1)
    sd = StoryDir.open(ws.paths, slug)
    series = sd.read_series()
    sd.write_series(
        SeriesDoc.model_validate(
            {**series.model_dump(), "parts": [p for p in series.model_dump()["parts"] if p["n"] != 1]}
        )
    )
    args = {"slug": slug, "n": 1, "feedback": "tighten the ending"}
    assert outcome(ws.check, "workshop_revise_part", args) is None
    assert outcome(call_tool, ws, "workshop_revise_part", args) is None


def test_an_unknown_workshop_tool_is_refused_like_call_tool_refuses_it(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = ws.start("after-dark", PREMISE)
    assert outcome(ws.check, "workshop_nope", {"slug": slug}) == outcome(
        call_tool, ws, "workshop_nope", {"slug": slug}
    )


@pytest.mark.parametrize("name", CHANGING)
def test_every_changing_workshop_tool_has_a_branch_in_check(tmp_home: Path, name: str):
    """A new changing tool must get a branch in `Workshop.check`, or this fails (it would raise 'unknown workshop tool')."""
    ws = make_workshop(tmp_home)
    slug = ws.start("after-dark", PREMISE)
    tool = next(t for t in TOOLS if t["name"] == name)
    args: dict[str, Any] = {
        "slug": slug,
        "channel": "after-dark",
        "letter": "A",
        "feedback": "x",
        "n": 1,
        "parts": 3,
    }
    args = {k: v for k, v in args.items() if k in tool["args_schema"]["properties"]}
    message = outcome(ws.check, name, args)
    assert message is None or "unknown workshop tool" not in message
