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

Ported from the F-C12 entries of runs/qa (evidence log 2026-11-09, 0.1.14). The `script_import` stage of a
series part is shown by the F-B6 proof. `build-qwentts.yml` is shown by its effect on the install's own
release: GitHub's release for the install's version (no token) lists the voice engine zip (more than 500 MB)
with its .sha256 beside it. When GitHub cannot be reached, the proof ends as skipped for that part, after all
other checks.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from proofs._lib import db_rows, proof_channel, short

PROOF = {
    "id": "f-c12-status-script-import",
    "boxes": ["F-C12"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 900,
    "fakes": [],
}

CHANNEL = "proof-script"
TITLE = "Proof Last Ferry"
SCRIPT = Path(__file__).parent / "_fixtures" / "f_c12_script" / "script-ferry.txt"
RUN_SCRIPT = ["studio", "run", "--script", str(SCRIPT), "--channel", CHANNEL, "--title", TITLE, "--format"]
WRITING_STAGES = ("ideas", "plan", "draft", "critic_revise", "split", "hooks")


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

    # 5. build-qwentts.yml: its effect is the voice engine asset on the release of this install's version
    _voice_engine_asset(ctx)
