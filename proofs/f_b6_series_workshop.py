"""F-B6: the Series workshop end to end.

concepts -> choose -> map -> write -> approve -> (produce refused before a design) -> design ->
`POST /api/library/series/{slug}/produce` -> the `series_part` job reaches done -> the Library shows the part
`produced`. The workshop steps go through the install's own `studio series` CLI, the production through the
dashboard route the Library page calls, and each claim is read where it lands: the story folder, the job
folder, the `outputs` rows, the outbox file and the captions of the produced video.

Ported from runs/qa/fb6.ps1 and fb6b.py (evidence log 2026-11-09, 0.1.14). The dev fakes write canned text, so
telling a series part from a standalone story by wording needs a real model (0.1.15 engine round); the
series identity is read from the job's source (the approved part script), `series_context.json` and the
titles.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from proofs._lib import db_rows, pauses_proof_channels, proof_channel, short, wait_job

PROOF = {
    "id": "f-b6-series-workshop",
    "boxes": ["F-B6"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 2700,
    "fakes": [],
}

CHANNEL = "proof-series"
PREMISE = "A lighthouse keeper keeps hearing a radio voice that arrives a day early"
PREFER = {"format": "long_16x9", "visual_treatment": "dark_slowzoom", "caption_style": "clean_white"}
MUST_RUN = ("script_import", "voice", "render_master", "shorts_derive", "qc", "outbox")
NOT_WRITTEN = ("ideas", "long_plan", "chapters_draft", "chapters_critic", "continuity")


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _series(ctx: Any, *args: str, timeout: float = 300) -> Any:
    """`studio series <args>`: exit 0 is checked; returns the printed text."""
    done = ctx.cli(["studio", "series", *args], timeout=timeout)
    ctx.check(
        done.returncode == 0,
        f"studio series {args[0]} exits 0",
        exit_code=done.returncode,
        stdout=short(done.stdout),
        stderr=short(done.stderr),
    )
    return (done.stdout or "").strip()


def _story_status(ctx: Any, slug: str) -> dict[str, Any]:
    out = _series(ctx, "status", slug)
    return json.loads(out)


def _library_story(ctx: Any, slug: str) -> dict[str, Any] | None:
    status, listing = ctx.api("GET", "/api/library/series")
    ctx.check(status == 200 and isinstance(listing, list), "the Library lists the series", status=status)
    return next((s for s in listing if isinstance(s, dict) and s.get("slug") == slug), None)


def _series_jobs(ctx: Any, slug: str) -> int:
    like = f'%"story_slug": "{slug}"%'
    sql = "select count(*) from dashboard_jobs where kind='series_part' and params_json like ?"
    rows = db_rows(ctx, sql, (like,))
    return int(rows[0][0])


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w:']+", text.lower())


def _script_tokens(script_md: Path) -> list[str]:
    """The words of a part script: its body without the front matter, headings and sentence markers."""
    text = script_md.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    body = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.DOTALL)
    body = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    return _tokens(body)


def _caption_cues(ass: Path) -> list[list[str]]:
    cues = []
    for line in ass.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("Dialogue:"):
            cues.append(_tokens(re.sub(r"\{[^}]*\}", "", line.split(",", 9)[-1])))
    return [c for c in cues if c]


def _probe(ctx: Any, video: Path) -> dict[str, Any]:
    """ffprobe of `video` through a recorded helper process (its output goes to a log in the scratch)."""
    exe = shutil.which("ffprobe")
    if exe is None:
        ctx.skip("ffprobe is not on PATH: the produced file's streams could not be read")
    proc = ctx.spawn(
        [
            exe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(video),
        ],
        name="ffprobe-long",
    )
    code = proc.wait(timeout=120)
    text = (ctx.scratch / "ffprobe-long.log").read_text(encoding="utf-8", errors="replace")
    ctx.check(code == 0, "ffprobe reads the produced file", exit_code=code, output=short(text))
    return json.loads(text)


@pauses_proof_channels
def run(ctx: Any) -> None:
    proof_channel(ctx, CHANNEL)

    # the workshop, by the studio CLI
    slug = _series(ctx, "start", CHANNEL, "--premise", PREMISE).splitlines()[-1].strip()
    story_dir = ctx.home / "stories" / slug
    ctx.check(
        re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug) is not None and (story_dir / "status.yaml").is_file(),
        "series start prints the story slug and makes the story folder",
        slug=slug,
        folder=str(story_dir),
    )
    _series(ctx, "concepts", slug, "--n", "3")
    drafts = sorted(p.name for p in story_dir.glob("concept-*.md"))
    ctx.check(
        drafts == ["concept-A.md", "concept-B.md", "concept-C.md"],
        "concepts wrote three drafts",
        drafts=drafts,
    )
    _series(ctx, "choose", slug, "A")
    ctx.check((story_dir / "concept.md").is_file(), "choose A promoted a draft to concept.md")
    _series(ctx, "map", slug, "--parts", "1", "--minutes", "12")
    ctx.check((story_dir / "series.md").is_file(), "map wrote series.md (1 part, 12 minutes)")
    _series(ctx, "write", slug, "1")
    script_md = story_dir / "parts" / "part-01" / "full" / "script.md"
    ctx.check(script_md.is_file(), "write 1 wrote the part script", file=str(script_md))
    _series(ctx, "approve", slug, "all")
    state = _story_status(ctx, slug)
    series_title = state["title"]
    ctx.check(
        [p["status"] for p in state["parts"]] == ["approved"] and state["has_design"] is False,
        "part 1 is approved and the series has no design yet",
        parts=state["parts"],
        has_design=state["has_design"],
        title=series_title,
    )
    listed = _library_story(ctx, slug)
    ctx.check(
        listed is not None and listed.get("next_step") == "design" and listed.get("has_design") is False,
        "the Library lists the story and its next step is the design",
        listed=short(listed),
    )

    # produce before a design is saved: refused, nothing queued
    status, answer = ctx.api("POST", f"/api/library/series/{slug}/produce", {})
    err = answer.get("error") if isinstance(answer, dict) else None
    ctx.check(
        status == 409
        and isinstance(err, dict)
        and err.get("code") == "cannot_produce"
        and "no design yet" in str(err.get("message")),
        "produce before a design answers 409 cannot_produce 'no design yet'",
        status=status,
        answer=short(answer),
    )
    ctx.check(
        _series_jobs(ctx, slug) == 0
        and [p["status"] for p in _story_status(ctx, slug)["parts"]] == ["approved"],
        "the refused produce queued no job and left the part approved",
        series_part_jobs=_series_jobs(ctx, slug),
    )

    # the design, from the choices the product offers
    choices = json.loads(_series(ctx, "design", slug))
    pick = {
        key: PREFER[key] if PREFER[key] in choices[key] else choices[key][0]
        for key in ("format", "visual_treatment", "caption_style")
    }
    ctx.step("design choices offered", choices={k: choices[k] for k in pick}, picked=pick)
    _series(
        ctx,
        "design",
        slug,
        "--format",
        pick["format"],
        "--visual-treatment",
        pick["visual_treatment"],
        "--caption-style",
        pick["caption_style"],
        "--cadence-days",
        "7",
    )
    design_file = story_dir / "design.yaml"
    ctx.check(design_file.is_file(), "design.yaml is saved", file=str(design_file))
    listed = _library_story(ctx, slug)
    ctx.check(
        listed is not None and listed.get("has_design") is True and listed.get("next_step") == "produce",
        "the Library now shows the design and 'produce' as the next step",
        listed=short(listed),
    )

    # produce through the Library route
    status, answer = ctx.api("POST", f"/api/library/series/{slug}/produce", {})
    queued = answer.get("queued") if isinstance(answer, dict) else None
    title = f"{series_title} - Part 01"
    ctx.check(
        status == 201
        and isinstance(queued, list)
        and len(queued) == 1
        and queued[0].get("part") == 1
        and queued[0].get("title") == title,
        "POST /api/library/series/{slug}/produce answers 201 and queues part 1",
        status=status,
        answer=short(answer),
    )
    job_id = str(queued[0]["job_id"])
    rows = db_rows(ctx, "select kind, channel from dashboard_jobs where job_id=?", (job_id,))
    ctx.check(
        rows == [("series_part", CHANNEL)],
        "the queued job is a series_part job on the proof channel",
        rows=rows,
    )
    job = wait_job(ctx, job_id, timeout=2400, every=10)
    ctx.check(
        job.get("status") == "done",
        "the series_part job is done",
        status=job.get("status"),
        message=job.get("message"),
        result=short(job.get("result")),
    )

    # the Library shows the part produced
    ctx.wait(
        lambda: [p["status"] for p in (_library_story(ctx, slug) or {}).get("parts", [])] == ["produced"],
        180,
        every=5,
        what="the Library to show part 1 as produced",
    )
    state = _story_status(ctx, slug)
    ctx.check(
        [(p["n"], p["status"], p["job_id"]) for p in state["parts"]] == [(1, "produced", job_id)],
        "the Library and studio series status show part 1 produced by that job",
        parts=state["parts"],
    )

    # series identity: the job's source is the approved part script
    job_dir = ctx.home / "data" / "jobs" / job_id
    source = ctx.read_json(job_dir / "script_source.json")
    ctx.check(
        source.get("kind") == "script_import" and _same(source.get("script", ""), script_md),
        "the job's source is the workshop's approved part script (script_import)",
        kind=source.get("kind"),
        script=source.get("script"),
        part_script=str(script_md),
    )
    context = ctx.read_json(job_dir / "series_context.json")
    ctx.check(
        context.get("series_title") == series_title
        and context.get("part_n") == 1
        and context.get("title") == title
        and bool(context.get("short_titles"))
        and all(t.startswith(f"{title} - Short ") for t in context["short_titles"]),
        "series_context.json names the series, part 1 and the Short titles",
        series_title=context.get("series_title"),
        title=context.get("title"),
        short_titles=context.get("short_titles"),
    )
    stages = ctx.read_json(job_dir / "stage_state.json")
    ctx.check(
        all(stages.get(name, {}).get("status") == "done" for name in MUST_RUN)
        and next(iter(stages)) == "script_import"
        and not set(stages) & set(NOT_WRITTEN),
        "script_import ran first and the story was imported, not written (no ideas, plan or draft stage)",
        stages=list(stages),
    )

    # the outputs and the files
    outs = [
        dict(r)
        for r in ctx.db(
            "select output_id, kind, path from outputs where job_id=? order by output_id", (job_id,)
        )
    ]
    longs = [r for r in outs if r["kind"] == "long"]
    shorts = [r for r in outs if r["kind"] == "short"]
    ctx.check(
        len(longs) == 1 and len(shorts) >= 1 and len(longs) + len(shorts) == len(outs),
        "outputs rows: one full video and its Shorts",
        outputs=outs,
    )
    full = Path(longs[0]["path"])
    series_slug = context.get("series_slug", "")
    ctx.check(
        full.is_file()
        and full.stat().st_size > 0
        and full.suffix == ".mp4"
        and full.parent.parts[-2:] == ("part-01", "full")
        and "series" in full.parts
        and any(part.startswith(series_slug) for part in full.parts)
        and full.stem.startswith(title),
        "the full video is under outbox/<channel>/series/<series>/part-01/full and named for the part",
        file=str(full),
        bytes=full.stat().st_size if full.is_file() else None,
    )
    ctx.check(
        all(
            Path(r["path"]).is_file()
            and Path(r["path"]).parent.parts[-2:] == ("part-01", "shorts")
            and Path(r["path"]).stem.startswith(f"{title} - Short ")
            for r in shorts
        ),
        "each Short is under part-01/shorts and titled 'Part 01 - Short NN'",
        shorts=[Path(r["path"]).name for r in shorts],
    )

    # the video opens and closes with the approved script's first and last lines
    words = _script_tokens(script_md)
    cues = _caption_cues(job_dir / "captions" / "long.ass")
    ctx.check(
        bool(words)
        and bool(cues)
        and words[: len(cues[0])] == cues[0]
        and words[-len(cues[-1]) :] == cues[-1],
        "the video's captions begin and end with the approved part script's first and last words",
        script_first=words[:8],
        caption_first=cues[0] if cues else None,
        script_last=words[-8:],
        caption_last=cues[-1] if cues else None,
        cues=len(cues),
    )

    # last: the file itself (skips here, after everything else is shown, when ffprobe is missing)
    probe = _probe(ctx, full)
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration = float(probe.get("format", {}).get("duration", 0))
    ctx.check(
        video.get("codec_name") == "h264"
        and audio.get("codec_name") == "aac"
        and video.get("width", 0) * 9 == video.get("height", 0) * 16
        and duration > 60,
        "the full video is h264 16:9 with aac audio and a real length",
        video=video,
        audio=audio,
        duration_s=duration,
    )
