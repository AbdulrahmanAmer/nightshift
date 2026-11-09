"""F-C12: `studio status` and the `script_import` stage (a script imported instead of written).

On the proof's own channel, through the install's own `studio` CLI:

1. `studio status` and `studio status --channel` print the channel, its cadence and its outputs, and the
   outputs line agrees with the `outputs` table.
2. `studio run --script <file> --format master_first` is refused with the coded NS-E303 (a 135-word script is
   far too short for a long video) and makes no job.
3. `studio run --script <file> --format shorts_only` makes a Short: the plan starts at `script_import_short`,
   no stage writes the story (no ideas, plan, draft, critic or split stage), the owner's text is kept word for
   word in `story.json`, and the video reaches the outbox as an `outputs` row with its file.
4. `studio status` after the job shows the new output.

5. The `script_import` stage of a series part: a minimal series (one part) goes through `studio series start,
   concepts, choose, map, write, approve, design`, then `POST /api/library/series/{slug}/produce` queues the
   part. When its `series_part` job is done, the job folder shows `script_import` as the first stage in
   `stage_state.json` (no ideas, plan or draft stage) and `script_source.json` names the part's own approved
   script.

Ported from the F-C12 entries of runs/qa (evidence log 2026-11-09, 0.1.14). `build-qwentts.yml` is shown by
its effect on the install's own release: GitHub's release for the install's version (no token) lists the
voice engine zip (more than 500 MB) with its .sha256 beside it. When GitHub cannot be reached, the proof
ends as skipped for that part, after all other checks.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from proofs._lib import FINAL, api_no_deadline, db_rows, pauses_proof_channels, proof_channel, short, wait_job

PROOF = {
    "id": "f-c12-status-script-import",
    "boxes": ["F-C12"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 3600,
    "fakes": [],
}

CHANNEL = "proof-script"
TITLE = "Proof Last Ferry"
SCRIPT = Path(__file__).parent / "_fixtures" / "f_c12_script" / "script-ferry.txt"
RUN_SCRIPT = ["studio", "run", "--script", str(SCRIPT), "--channel", CHANNEL, "--title", TITLE, "--format"]
WRITING_STAGES = ("ideas", "plan", "draft", "critic_revise", "split", "hooks")
PREMISE = "A ferry pilot keeps finding the same unsigned note in her coat on the last crossing of the night"
PREFER = {"format": "long_16x9", "visual_treatment": "dark_slowzoom", "caption_style": "clean_white"}
SERIES_NOT_WRITTEN = ("ideas", "long_plan", "chapters_draft", "chapters_critic", "continuity")


def _words(text: str) -> list[str]:
    return text.split()


def _counts_line(ctx: Any) -> str:
    """The `outputs:` text `studio status` should print for the proof channel, built from the table."""
    rows = db_rows(ctx, "select status, count(*) from outputs where channel=? group by status", (CHANNEL,))
    return ", ".join(f"{r[0]}={r[1]}" for r in sorted(rows, key=lambda r: r[0])) or "no outputs yet"


def _status_line(out: str, label: str) -> str | None:
    match = re.search(rf"^\s*{label}:\s*(.*)$", out, re.MULTILINE)
    return match.group(1).strip() if match else None


def _job_dirs(ctx: Any) -> list[str]:
    root = ctx.home / "data" / "jobs"
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name.startswith(f"{CHANNEL}-"))


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _series(ctx: Any, *args: str, timeout: float = 300) -> str:
    """`studio series <args>` through the install's own CLI: exit 0 is checked; returns the printed text."""
    done = ctx.cli(["studio", "series", *args], timeout=timeout)
    ctx.check(
        done.returncode == 0,
        f"studio series {args[0]} exits 0",
        exit_code=done.returncode,
        stdout=short(done.stdout),
        stderr=short(done.stderr),
    )
    return (done.stdout or "").strip()


def _cancel_if_running(ctx: Any, job_id: str) -> None:
    """After a timeout the job must not go on making a video on the proof channel: no-deadline calls."""
    try:
        _, now = api_no_deadline(ctx, "GET", f"/api/jobs/{job_id}")
        if isinstance(now, dict) and now.get("status") in FINAL:
            return
        code, said = api_no_deadline(ctx, "POST", f"/api/jobs/{job_id}/cancel")
        ctx.step(
            "cleanup: the series_part job was still going and is cancelled", status=code, answer=short(said)
        )
    except Exception as exc:  # noqa: BLE001  (a cleanup error must not hide the proof's own result)
        ctx.step("cleanup: the series_part job could not be checked", job_id=job_id, error=str(exc))


def _series_part(ctx: Any) -> None:
    """The box's series `script_import` stage: a one-part series on the proof's channel, produced through the
    Library's route; the job folder shows the stage ran first on the part's own approved script."""
    slug = _series(ctx, "start", CHANNEL, "--premise", PREMISE).splitlines()[-1].strip()
    story_dir = ctx.home / "stories" / slug
    ctx.check(
        re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug) is not None and (story_dir / "status.yaml").is_file(),
        "series start prints the story slug and makes the story folder (the proof's own series)",
        slug=slug,
        channel=CHANNEL,
        folder=str(story_dir),
    )
    _series(ctx, "concepts", slug, "--n", "3")
    _series(ctx, "choose", slug, "A")
    ctx.check((story_dir / "concept.md").is_file(), "choose A promoted a draft to concept.md")
    _series(ctx, "map", slug, "--parts", "1", "--minutes", "12")
    ctx.check((story_dir / "series.md").is_file(), "map wrote series.md (1 part, 12 minutes)")
    _series(ctx, "write", slug, "1")
    script_md = story_dir / "parts" / "part-01" / "full" / "script.md"
    ctx.check(script_md.is_file(), "write 1 wrote the part script", file=str(script_md))
    _series(ctx, "approve", slug, "all")
    state = json.loads(_series(ctx, "status", slug))
    ctx.check(
        [p["status"] for p in state["parts"]] == ["approved"], "part 1 is approved", parts=state["parts"]
    )
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

    status, answer = ctx.api("POST", f"/api/library/series/{slug}/produce", {})
    queued = answer.get("queued") if isinstance(answer, dict) else None
    ctx.check(
        status == 201 and isinstance(queued, list) and len(queued) == 1 and queued[0].get("part") == 1,
        "POST /api/library/series/{slug}/produce answers 201 and queues part 1",
        status=status,
        answer=short(answer),
    )
    assert isinstance(queued, list)
    job_id = str(queued[0]["job_id"])
    rows = db_rows(ctx, "select kind, channel from dashboard_jobs where job_id=?", (job_id,))
    ctx.check(
        rows == [("series_part", CHANNEL)],
        "the queued job is a series_part job on the proof channel",
        rows=rows,
    )
    try:
        job = wait_job(ctx, job_id, timeout=2400, every=10)
    finally:
        _cancel_if_running(ctx, job_id)
    ctx.check(
        job.get("status") == "done",
        "the series_part job is done",
        status=job.get("status"),
        message=job.get("message"),
        result=short(job.get("result")),
    )

    job_dir = ctx.home / "data" / "jobs" / job_id
    stages = ctx.read_json(job_dir / "stage_state.json")
    ctx.check(
        next(iter(stages)) == "script_import"
        and stages["script_import"].get("status") == "done"
        and not set(stages) & set(SERIES_NOT_WRITTEN),
        "script_import ran first and is done; no ideas, plan or draft stage wrote the story",
        stages={k: v.get("status") for k, v in stages.items()},
    )
    source = ctx.read_json(job_dir / "script_source.json")
    ctx.check(
        source.get("kind") == "script_import" and _same(str(source.get("script", "")), script_md),
        "script_source.json names the part's own approved script (script_import)",
        kind=source.get("kind"),
        script=source.get("script"),
        part_script=str(script_md),
    )
    state = json.loads(_series(ctx, "status", slug))
    ctx.check(
        [(p["n"], p["status"], p["job_id"]) for p in state["parts"]] == [(1, "produced", job_id)],
        "studio series status shows part 1 produced by that job",
        parts=state["parts"],
    )


def _voice_engine_asset(ctx: Any) -> None:
    """build-qwentts.yml attaches the zip and its .sha256 to the release of the tag; read them from GitHub."""
    tag = "v" + str(ctx.version).lstrip("v")
    url = f"https://api.github.com/repos/AbdulrahmanAmer/nightshift/releases/tags/{tag}"
    request = urllib.request.Request(  # noqa: S310 (a fixed https URL)
        url, headers={"User-Agent": "Nightshift-proof", "Accept": "application/vnd.github+json"}
    )
    status, raw = 0, b""
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310
            status, raw = resp.status, resp.read(8_000_000)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, b""
    except (urllib.error.URLError, OSError) as exc:
        ctx.step("GitHub could not be reached", url=url, error=str(exc))
        ctx.skip(
            f"F-C12's build-qwentts.yml release asset: GitHub's release API ({url}) did not answer ({exc}); "
            "the runner needs internet access to api.github.com"
        )
    if status in (403, 429) or status >= 500:
        ctx.step("GitHub did not serve the release", url=url, status=status)
        ctx.skip(
            f"F-C12's build-qwentts.yml release asset: api.github.com answered {status} for {tag} (a rate "
            "limit or an outage); the runner needs a quiet address or a later retry"
        )
    ctx.check(status == 200, f"GitHub has a release for {tag}", url=url, status=status)
    release = json.loads(raw.decode("utf-8"))
    assets = {a.get("name"): a for a in release.get("assets") or [] if isinstance(a, dict)}
    zip_name = "Nightshift-VoiceEngine-win-cuda-x64.zip"
    zip_asset = assets.get(zip_name) or {}
    sha_asset = assets.get(zip_name + ".sha256") or {}
    ctx.check(
        int(zip_asset.get("size") or 0) > 500_000_000 and int(sha_asset.get("size") or 0) > 0,
        f"release {tag} holds {zip_name} (more than 500,000,000 bytes) and its .sha256 beside it",
        tag=tag,
        zip_bytes=zip_asset.get("size"),
        sha256_bytes=sha_asset.get("size"),
        assets=sorted(str(n) for n in assets),
    )


@pauses_proof_channels
def run(ctx: Any) -> None:
    proof_channel(ctx, CHANNEL)
    text = SCRIPT.read_text(encoding="utf-8-sig")
    ctx.step("script fixture", file=SCRIPT.name, words=len(_words(text)))

    # 1. studio status
    before = _counts_line(ctx)
    done = ctx.cli(["studio", "status", "--channel", CHANNEL])
    ctx.check(
        done.returncode == 0 and f"channel {CHANNEL} [" in done.stdout,
        "studio status --channel: exit 0 and the proof channel is named",
        exit_code=done.returncode,
        first_line=done.stdout.splitlines()[0] if done.stdout else None,
    )
    ctx.check(
        _status_line(done.stdout, "cadence") is not None,
        "studio status prints the channel's cadence",
        cadence=_status_line(done.stdout, "cadence"),
    )
    ctx.check(
        _status_line(done.stdout, "outputs") == before,
        "studio status prints the outputs the table holds",
        printed=_status_line(done.stdout, "outputs"),
        table=before,
    )
    everyone = ctx.cli(["studio", "status"])
    ctx.check(
        everyone.returncode == 0 and f"channel {CHANNEL} [" in everyone.stdout,
        "studio status with no channel lists the proof channel too",
        exit_code=everyone.returncode,
        channels=re.findall(r"^channel (\S+)", everyone.stdout, re.MULTILINE),
    )

    # 2. a long video from a 135-word script is refused
    jobs_before = _job_dirs(ctx)
    refused = ctx.cli(
        [*RUN_SCRIPT, "master_first"],
        timeout=300,
    )
    said = (refused.stdout or "") + (refused.stderr or "")
    ctx.check(
        refused.returncode != 0 and "NS-E303" in said and "long video" in said,
        "a script too short for a long video is refused with NS-E303 and the reason",
        exit_code=refused.returncode,
        said=short(said),
    )
    ctx.check(
        _job_dirs(ctx) == jobs_before,
        "the refused script made no job",
        jobs=len(_job_dirs(ctx)),
    )

    # 3. the same script as a Short
    made = ctx.cli(
        [*RUN_SCRIPT, "shorts_only"],
        timeout=840,
    )
    match = re.search(r"job (\S+) done: (\d+) stages", made.stdout or "")
    ctx.check(
        made.returncode == 0 and match is not None,
        "studio run --script --format shorts_only finishes: exit 0 and 'job <id> done'",
        exit_code=made.returncode,
        stdout=short(made.stdout),
        stderr=short(made.stderr),
    )
    assert match is not None
    job_id, stage_count = match.group(1), int(match.group(2))
    job_dir = ctx.home / "data" / "jobs" / job_id
    state = ctx.read_json(job_dir / "stage_state.json")
    stages = list(state)
    ctx.check(
        stages[0] == "script_import_short" and stages[-1] == "outbox" and len(stages) == stage_count,
        "the plan starts at script_import_short and ends at the outbox",
        stages=stages,
    )
    ctx.check(
        all(v.get("status") == "done" for v in state.values()),
        "every stage of the job is done",
        statuses={k: v.get("status") for k, v in state.items()},
    )
    ctx.check(
        not set(stages) & set(WRITING_STAGES),
        "no stage wrote the story: no ideas, plan, draft, critic or split stage",
        writing_stages_found=sorted(set(stages) & set(WRITING_STAGES)),
    )
    source = ctx.read_json(job_dir / "script_source.json")
    ctx.check(
        source.get("kind") == "script_import" and source.get("owner_script") is True,
        "script_source.json names the owner's script",
        source=source,
    )
    script_md = job_dir / str(source.get("script"))
    ctx.check(script_md.is_file(), "script.md is written to the job folder", file=str(script_md))
    story = ctx.read_json(job_dir / "story.json")
    ctx.check(
        _words(story.get("text", "")) == _words(text),
        "story.json holds the owner's text word for word",
        story_words=len(_words(story.get("text", ""))),
        script_words=len(_words(text)),
    )

    # the effect: the Short is in the outbox
    rows = [
        dict(r)
        for r in ctx.db("select output_id, kind, status, path, title from outputs where job_id=?", (job_id,))
    ]
    ctx.check(
        len(rows) == 1 and rows[0]["kind"] == "short" and rows[0]["status"] == "pending",
        "one pending Short output row for the job",
        rows=rows,
    )
    video = Path(rows[0]["path"])
    ctx.check(
        video.is_file() and video.stat().st_size > 0 and ctx.home / "outbox" in video.parents,
        "the Short's file is in the outbox and not empty",
        file=str(video),
        bytes=video.stat().st_size if video.is_file() else None,
    )

    # 4. studio status after the job
    after = ctx.cli(["studio", "status", "--channel", CHANNEL])
    printed = _status_line(after.stdout, "outputs")
    ctx.check(
        after.returncode == 0 and printed == _counts_line(ctx) and printed != "no outputs yet",
        "studio status now counts the new output, as the table does",
        printed=printed,
        table=_counts_line(ctx),
    )

    # 5. the script_import stage of a series part
    _series_part(ctx)

    # 6. build-qwentts.yml: its effect is the voice engine asset on the release of this install's version
    _voice_engine_asset(ctx)
