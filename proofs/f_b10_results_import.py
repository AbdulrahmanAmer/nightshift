"""F-B10: the Results page renders the numbers of an imported YouTube Studio CSV.

Two Shorts (one per proof channel) are marked uploaded with 11-character video ids. A CSV in YouTube Studio's
layout (a Total row and one row per video) is dropped on the Results page's own drop zone. The proof
then reads the effect at three levels that must agree: the `analytics` rows and `pool_weights`,
GET /api/learn/summary (what the page loads) and the text Playwright reads off the rendered page (views,
mix, weighted retention, per-video, the 7-day spark, the weight changes). Ported from runs/qa/fb10.py and
pw_shot.py (evidence log 2026-11-09, 0.1.12 and 0.1.14).
"""

from __future__ import annotations

import csv
import math
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from proofs._lib import db_rows, pauses_proof_channels, proof_channel, run_short, short

PROOF = {
    "id": "f-b10-results-import",
    "boxes": ["F-B10"],
    "install": "dev",
    "window": True,
    "needs": ["engine"],
    "timeout_s": 2400,
    "fakes": [],
}

# one channel per Short: a fresh channel's idea history cannot refuse the Short (NS-E304); the niche differs
# so the channels pass the cross-channel distinctness check of POST /api/channels
CHANNELS = (("proof-results-a", "proof results alpha"), ("proof-results-b", "proof results beta"))
VIDEO_IDS = ("PrfB10vidA1", "PrfB10vidB2")
CSV_NAME = "proof-fb10-yt.csv"
LIST_SIZE = 5  # videos in the summary's top and in its bottom (ns_dashboard/learn_view.py)
TARGET = 55  # the page's retention target, Results.tsx
HEADER = [
    "Content",
    "Video title",
    "Video publish time",
    "Duration",
    "Views",
    "Stayed to watch (%)",
    "Average percentage viewed (%)",
    "Average view duration",
    "Likes",
    "Comments added",
    "Shares",
    "Subscribers",
]
# per video: views, stayed to watch %, average % viewed, average view duration (h:mm:ss) and its seconds,
# likes, comments, shares, subscribers (the layout and numbers of QA's yt-fb10.csv)
METRICS = (
    (2400, 71.5, 88.2, "0:00:48", 48.0, 180, 22, 15, 31),
    (750, 48.0, 52.4, "0:00:28", 28.0, 20, 3, 1, 2),
)


def _js_round(x: float) -> int:
    """Math.round of the page's script (halves go up)."""
    return math.floor(x + 0.5)


def _compact(n: int) -> str:
    """fmtCompact of ui/src/lib/format.ts (the label above a spark well)."""
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e4:
        return f"{_js_round(n / 1e3)}k"
    if n >= 1e3:
        return f"{n / 1e3:.1f}k"
    return str(n)


def _body(page: Any) -> str:
    try:
        return str(page.inner_text("body"))
    except Exception:  # the page is between two renders
        return ""


def _output_of(ctx: Any, channel: str) -> tuple[str, str]:
    """(output_id, title) of the proof channel's Short: the one a former run left, else a new Short job."""
    sql = (
        "select output_id, title from outputs where channel=? and profile is null and kind='short'"
        " and status != 'rejected' order by created_at desc"
    )
    rows = db_rows(ctx, sql, (channel,))
    if rows:
        ctx.step("a Short of the proof channel is already there", channel=channel, output=rows[0][0])
        return str(rows[0][0]), str(rows[0][1])
    job = run_short(ctx, channel)
    ctx.check(
        job.get("status") == "done",
        "the Short job is done",
        channel=channel,
        job_id=job.get("job_id"),
        status=job.get("status"),
        message=job.get("message"),
    )
    rows = db_rows(ctx, sql, (channel,))
    ctx.check(bool(rows), "the job left a Short on the proof channel", channel=channel)
    return str(rows[0][0]), str(rows[0][1])


def _mark_uploaded(ctx: Any, oid: str, video_id: str) -> None:
    url = f"https://youtu.be/{video_id}"
    status, answer = ctx.api("POST", f"/api/outputs/{oid}/uploaded", {"url": url})
    ctx.check(
        status == 200,
        "marked uploaded with an 11-character video id",
        output=oid,
        status=status,
        answer=short(answer),
    )
    ups = db_rows(ctx, "select video_url from uploads where output_id=?", (oid,))
    ctx.check(ups == [(url,)], "uploads row holds the URL", output=oid, uploads=ups)


def _learn_job_ids(ctx: Any) -> set[str]:
    status, jobs = ctx.api("GET", "/api/jobs?kind=learn&limit=100")
    ctx.check(status == 200 and isinstance(jobs, list), "learn jobs listed", status=status)
    return {str(j.get("job_id")) for j in jobs}


@pauses_proof_channels
def run(ctx: Any) -> None:
    base = f"http://127.0.0.1:{ctx.ports['dashboard']}"

    # ---- 1. two uploaded Shorts with known video ids (the proof's own channels and ids)
    outputs: list[tuple[str, str]] = []
    for (slug, niche), video_id in zip(CHANNELS, VIDEO_IDS, strict=True):
        proof_channel(ctx, slug, niche=niche)
        oid, title = _output_of(ctx, slug)
        _mark_uploaded(ctx, oid, video_id)
        outputs.append((oid, title))
    oid_a, oid_b = outputs[0][0], outputs[1][0]
    ctx.step("the two Shorts the CSV is about", outputs=outputs)

    # ---- 2. the CSV in YouTube Studio's layout, written to the scratch folder
    csv_path = ctx.scratch / CSV_NAME
    total = sum(m[0] for m in METRICS)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerow(["Total", "", "", "", total, "", "", "", "", "", "", ""])
        for (_oid, title), vid, m in zip(outputs, VIDEO_IDS, METRICS, strict=True):
            w.writerow([vid, title, "Nov 9, 2026", 54, m[0], m[1], m[2], m[3], m[5], m[6], m[7], m[8]])
    ctx.step("CSV written", path=str(csv_path), total_row_views=total)
    other = db_rows(ctx, "select count(*) from analytics where output_id not in (?, ?)", (oid_a, oid_b))[0][0]
    earlier = db_rows(ctx, "select count(*) from analytics where output_id in (?, ?)", (oid_a, oid_b))[0][0]
    ctx.step("analytics rows already on this install", other_videos=other, of_these_two_before_import=earlier)

    # ---- 3. the Results page: drop the CSV on its own drop zone
    jobs_before = _learn_job_ids(ctx)
    page = ctx.page()
    problems: list[str] = []
    warnings: list[str] = []

    def on_console(msg: Any) -> None:
        if msg.type == "error":
            problems.append(f"console error: {msg.text}")
        elif msg.type == "warning":
            warnings.append(msg.text)

    page.on("console", on_console)
    page.on("pageerror", lambda exc: problems.append(f"page error: {exc}"))
    page.goto(f"{base}/#/results")
    page.reload()
    drop = page.get_by_text("Drop your YouTube Studio analytics export").first
    drop.wait_for(timeout=60_000)
    t_import = datetime.now(UTC) - timedelta(seconds=2)
    page.locator("label", has_text="Drop your YouTube Studio analytics export").locator(
        "input[type=file]"
    ).set_input_files(str(csv_path))
    ctx.step("CSV chosen on the Results drop zone", file=CSV_NAME)

    def new_job() -> dict[str, Any] | None:
        status, jobs = ctx.api("GET", "/api/jobs?kind=learn&limit=100")
        fresh = [j for j in jobs if str(j.get("job_id")) not in jobs_before] if status == 200 else []
        return fresh[0] if fresh and fresh[0].get("status") in ("done", "failed") else None

    job = ctx.wait(new_job, 90, every=2, what="the learn job made by the drop")
    result = job.get("result") or {}
    ctx.check(
        job.get("status") == "done" and result.get("rows") == 2 and result.get("matched") == 2,
        "the import job is done: 2 rows read (the Total row is not one), 2 matched",
        job_id=job.get("job_id"),
        status=job.get("status"),
        message=job.get("message"),
        result=short(result),
    )
    ctx.check(not result.get("unmatched"), "no row is left unmatched", unmatched=result.get("unmatched"))
    saved = sorted((ctx.home / "imports" / "analytics").glob(f"*-{CSV_NAME}"))
    ctx.check(
        bool(saved) and saved[-1].read_bytes() == csv_path.read_bytes(),
        "the CSV was kept under imports/analytics, byte for byte",
        files=[p.name for p in saved][-3:],
    )

    # ---- 4. the database effects
    today = datetime.now(UTC).date()
    for oid, vid, m in zip((oid_a, oid_b), VIDEO_IDS, METRICS, strict=True):
        got = db_rows(
            ctx,
            "select date, views, viewed_pct, avg_pct_viewed, avg_view_duration_s, likes, comments, shares,"
            " subs_gained from analytics where output_id=? order by date desc limit 1",
            (oid,),
        )
        want = (m[0], m[1], m[2], m[4], m[5], m[6], m[7], m[8])
        ctx.check(
            len(got) == 1 and got[0][1:] == want and abs((today - date.fromisoformat(got[0][0])).days) <= 1,
            f"analytics row of {vid}: views, stayed %, average %, duration, likes, comments, shares, subs",
            output=oid,
            row=got,
            expected=want,
        )
    changes = result.get("changes") or []
    ctx.check(bool(changes), "the import moved pool weights", moved=len(changes))
    mine = tuple(c[0] for c in CHANNELS)
    weights = db_rows(
        ctx,
        "select channel, pool, option, weight, updated_at from pool_weights where channel in (?, ?)",
        mine,
    )
    by_key = {(w[1], w[2]) for w in weights}
    stamped = [w for w in weights if datetime.fromisoformat(str(w[4])) >= t_import]
    ctx.check(
        bool(weights) and all((c["pool"], c["option"]) in by_key for c in changes) and bool(stamped),
        "pool_weights rows of the proof channels hold every moved option, written by this import",
        rows=len(weights),
        written_now=len(stamped),
        moved=[(c["pool"], c["option"], round(float(c["delta"]), 4)) for c in changes][:12],
    )

    # ---- 5. what the page loads: GET /api/learn/summary
    status, summ = ctx.api("GET", "/api/learn/summary")
    ctx.check(status == 200 and isinstance(summ, dict), "summary answers 200", status=status)
    ctx.check(
        summ.get("matched") == 2 and summ.get("unmatched") == 0 and bool(summ.get("imported_at")),
        "summary names the last import: 2 matched, 0 unmatched",
        matched=summ.get("matched"),
        unmatched=summ.get("unmatched"),
        imported_at=summ.get("imported_at"),
    )
    videos = list(summ.get("top") or []) + list(summ.get("bottom") or [])
    # the summary (and so the page) lists at most LIST_SIZE videos in top and LIST_SIZE in bottom
    # (ns_dashboard/learn_view.py): with more than ten analysed videos a Short can be in neither, so a
    # missing one is judged against the database's per-video count, never taken as a failure by itself
    analysed = db_rows(ctx, "select count(distinct output_id) from analytics")[0][0]
    not_listed: list[str] = []
    for (_oid, title), vid, m in zip(outputs, VIDEO_IDS, METRICS, strict=True):
        hit = [
            v
            for v in videos
            if v.get("title") == title and v.get("views") == m[0] and v.get("viewed_pct") == m[1]
        ]
        if not hit and analysed > 2 * LIST_SIZE:
            not_listed.append(vid)
            ctx.step(
                f"the Short of {vid} is in neither list: the summary holds at most {LIST_SIZE} + {LIST_SIZE}",
                analysed_videos=analysed,
                expected=(m[0], m[1]),
            )
            continue
        ctx.check(
            len(hit) == 1 and hit[0].get("kind") == "short",
            f"summary lists the Short of {vid} with its views and stayed %",
            expected=(m[0], m[1]),
            found=hit,
            analysed_videos=analysed,
        )
    daily = list(summ.get("daily") or [])
    ctx.check(len(daily) == 14, "summary holds 14 days of daily views", days=len(daily))
    last = daily[-1]
    sum_changes = summ.get("changes") or []
    ctx.check(
        len(sum_changes) == len(changes)
        and {(c["pool"], c["option"]) for c in sum_changes} == {(c["pool"], c["option"]) for c in changes},
        "summary carries the weight changes of the import",
        summary_changes=len(sum_changes),
        job_changes=len(changes),
    )
    clean = other == 0 and earlier == 0  # nothing else adds to the day's or the 14 days' figures
    if clean:
        ctx.check(
            last["shorts"] + last["long"] == total == 3150
            and sum(d["shorts"] + d["long"] for d in daily) == 3150
            and last["long"] == 0,
            "daily views on the import day are the CSV's 3,150, all Shorts",
            last_day=last,
        )
    else:
        ctx.step(
            "other analytics rows on this install: page figures are compared with the API's own",
            other_videos=other,
            of_these_two_before_import=earlier,
        )

    # what the page should draw, from the API answer (Results.tsx)
    day_total = sum(d["shorts"] + d["long"] for d in daily)
    day_shorts = sum(d["shorts"] for d in daily)
    share = _js_round(day_shorts / day_total * 100) if day_total else None
    weighted = (
        _js_round(sum(v["viewed_pct"] * v["views"] for v in videos) / max(1, sum(v["views"] for v in videos)))
        if videos
        else None
    )
    week = daily[-7:]

    # ---- 6. the rendered page
    def drawn() -> bool:
        return "2 videos matched, 0 not matched" in _body(page)

    ctx.wait(drawn, 60, every=2, what="the Results page to show the import header")
    text = _body(page)
    ctx.check(
        re.search(r"From the YouTube Studio export imported\s+\S+.*2 videos matched, 0 not matched", text)
        is not None,
        "page header: from the YouTube Studio export imported, 2 videos matched, 0 not matched",
        header=text[text.find("From the YouTube") : text.find("From the YouTube") + 120],
    )
    m_views = re.search(r"Views, last 14 days\s+([\d,]+)", text)
    ctx.check(
        m_views is not None and m_views.group(1) == f"{day_total:,}",
        "page: Views, last 14 days",
        shown=m_views.group(1) if m_views else None,
        expected=f"{day_total:,}",
    )
    m_mix = re.search(r"(\d+)%\s+Mix", text)
    ctx.check(
        m_mix is not None
        and share is not None
        and int(m_mix.group(1)) == share
        and f"{share}% Shorts, {100 - share}% full videos" in text,
        "page: mix of views from Shorts",
        shown=m_mix.group(1) if m_mix else None,
        expected=share,
    )
    m_ret = re.search(r"(\d+)%\s+Retention", text)
    ctx.check(
        m_ret is not None
        and weighted is not None
        and int(m_ret.group(1)) == weighted
        and "Average, weighted by views" in text
        and f"Target {TARGET}%" in text,
        "page: retention is the view-weighted average of the stayed-to-watch figures",
        shown=m_ret.group(1) if m_ret else None,
        expected=weighted,
    )
    if clean:
        ctx.check(
            m_views is not None and m_views.group(1) == "3,150" and weighted == 66,
            "page: the box's figures, 3,150 views and 66 % retention",
            views=m_views.group(1) if m_views else None,
            retention=weighted,
        )
    for vid, m in zip(VIDEO_IDS, METRICS, strict=True):
        if vid in not_listed:
            continue
        shown = f"{m[0]:,}"
        pct = _js_round(m[1])
        found = re.search(rf"Views\s+{re.escape(shown)}\s+{pct}%", text)
        ctx.check(
            found is not None,
            f"page: published video of {vid} shows {shown} views and {pct}% watched to the end",
            expected=f"Views {shown} {pct}%",
        )
    meters = page.get_by_role("meter")
    labels = [(meters.nth(i).get_attribute("aria-label") or "") for i in range(meters.count())]
    texts = [(meters.nth(i).get_attribute("aria-valuetext") or "") for i in range(meters.count())]
    spark_ok = len(labels) == 7
    for d, label, shown in zip(week, labels, texts, strict=False):
        views = d["shorts"] + d["long"]
        day = date.fromisoformat(d["date"])
        spark_ok = (
            spark_ok
            and label.startswith(day.strftime("%a"))
            and (label.endswith(f"{views:,} views on {d['date']}") and shown == _compact(views))
        )
    ctx.check(
        spark_ok,
        "page: the 7-day spark has a well per day, with that day's views",
        wells=list(zip(labels, texts, strict=False)),
        last_day=f"{last['shorts'] + last['long']:,} on {last['date']}",
    )
    change_box = page.locator("section:has(#r-change)")
    change_text = str(change_box.inner_text()) if change_box.count() else text
    more = sum(1 for c in changes if float(c["delta"]) >= 0)
    ctx.check(
        change_text.count("More often") == more and change_text.count("Less often") == len(changes) - more,
        "page: What changes next lists each weight change, More often for a rise and Less often for a fall",
        listed_more=change_text.count("More often"),
        listed_less=change_text.count("Less often"),
        expected_more=more,
        expected_less=len(changes) - more,
    )
    lines = [s for s in (summ.get("summary") or []) if isinstance(s, str)]
    ctx.check(
        bool(lines) and all(s in text for s in lines),
        "page: What Nightshift learned shows the summary lines of the API",
        lines=lines[:3],
    )
    ctx.step("console warnings seen while the page was open", count=len(warnings), first=warnings[:3])
    ctx.check(not problems, "no console error and no page error", problems=problems[:5])
    if not_listed:
        ctx.skip(
            f"the Results page lists at most {2 * LIST_SIZE} published videos (top and bottom {LIST_SIZE}) "
            f"and {analysed} videos are analysed on this install, so the Shorts {not_listed} are not drawn: "
            "the runner needs a home with at most ten analysed videos (or a fresh data folder) for the "
            "page's per-video rows"
        )
