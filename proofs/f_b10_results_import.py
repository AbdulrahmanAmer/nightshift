"""F-B10: the Results page renders the numbers of an imported YouTube Studio CSV.

Every run makes two new Shorts in this run (one per channel of its own, `run_short`) and marks them uploaded
with 11-character video ids of this run. A CSV in YouTube Studio's layout (a Total row and one row per video)
is built for exactly those two output ids and dropped on the Results page's own drop zone. The proof
then reads the effect at three levels that must agree: the `analytics` rows and `pool_weights`,
GET /api/learn/summary (what the page loads) and the text Playwright reads off the rendered page (views,
mix, weighted retention, per-video, the 7-day spark, the weight changes). Ported from runs/qa/fb10.py and
pw_shot.py (evidence log 2026-11-09, 0.1.12 and 0.1.14).

The CSV's figures are compared with the API and with the page on every run, on a fresh install and on one that
already holds analytics of earlier runs: the summary and the analytics rows are read before the import, and
what the page and the API must show is the baseline plus the CSV (3,150 views on the import day, all Shorts;
the view-weighted stayed-to-watch figure of the listed videos with the CSV's two videos among them). On an
install with no other analytics that is the box's own 3,150 views and 66 % retention.
"""

from __future__ import annotations

import csv
import math
import re
import uuid
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

# One channel per Short, new in every run (a channel's idea history refuses repeated fake ideas, NS-E304, so a
# second Short on the same channel could not be made); the niche differs so the channels pass the
# cross-channel distinctness check of POST /api/channels. The run's stamp is added to the slug and the niche.
CHANNEL_BASES = (("proof-results-a", "proof results alpha"), ("proof-results-b", "proof results beta"))
# 11-character video ids: a prefix and five characters of this run, so the import never matches an old one
VIDEO_PREFIXES = ("PrB10a", "PrB10b")
LIST_SIZE = 5  # videos in the summary's top and in its bottom (ns_dashboard/learn_view.py)
TARGET = 55  # the page's retention target, Results.tsx
BOX_VIEWS = 3150  # the box's figures for the two CSV videos: total views and view-weighted retention (%)
BOX_RETENTION = 66
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


def _fresh_short(ctx: Any, channel: str) -> tuple[str, str]:
    """(output_id, title) of a Short made now: a new Short job on the proof channel, never a former run's."""
    job = run_short(ctx, channel)
    ctx.check(
        job.get("status") == "done",
        "the Short job is done",
        channel=channel,
        job_id=job.get("job_id"),
        status=job.get("status"),
        message=job.get("message"),
    )
    ids = [str(i) for i in (job.get("result") or {}).get("output_ids") or []]
    ctx.check(bool(ids), "the Short job names its outputs", channel=channel, output_ids=ids)
    marks = ",".join("?" * len(ids))
    rows = db_rows(
        ctx,
        f"select output_id, title from outputs where output_id in ({marks}) and channel=?"
        " and profile is null and kind='short' order by created_at desc",
        (*ids, channel),
    )
    ctx.check(
        bool(rows),
        "the job left a Short on the proof channel",
        channel=channel,
        output_ids=ids,
        shorts=[r[0] for r in rows],
    )
    return str(rows[0][0]), str(rows[0][1])


def _latest_rows(ctx: Any) -> list[tuple[int, float]]:
    """(views, stayed %) of every analysed video's latest analytics row, as the Results summary reads them."""
    rows = db_rows(
        ctx,
        "select a.views, a.viewed_pct from analytics a join outputs o on o.output_id = a.output_id"
        " where a.date = (select max(date) from analytics where output_id = a.output_id)",
    )
    return [(int(r[0] or 0), round(float(r[1] or 0), 1)) for r in rows]


def _listed(rows: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """The videos the summary lists: the five most watched and the five least watched of the rest
    (ns_dashboard/learn_view.py top_bottom)."""
    ordered = sorted(rows, key=lambda r: -r[0])
    return ordered[:LIST_SIZE] + list(reversed(ordered[LIST_SIZE:]))[:LIST_SIZE]


def _weighted(rows: list[tuple[int, float]]) -> int | None:
    """The page's retention: stayed-to-watch figures weighted by views, rounded as the page's script does."""
    if not rows:
        return None
    return _js_round(sum(p * v for v, p in rows) / max(1, sum(v for v, _p in rows)))


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

    # ---- 1. two new Shorts made in this run, uploaded with video ids of this run (own channels, own ids)
    stamp = datetime.now().strftime("%m%d-%H%M%S")
    channels = tuple((f"{base}-{stamp}", f"{niche} {stamp}") for base, niche in CHANNEL_BASES)
    video_ids = tuple(f"{prefix}{uuid.uuid4().hex[:5]}" for prefix in VIDEO_PREFIXES)
    csv_name = f"proof-fb10-yt-{stamp}.csv"
    ctx.step(
        "this run's own channels, video ids and CSV (new in every run)",
        channels=[c[0] for c in channels],
        video_ids=list(video_ids),
        csv=csv_name,
    )
    outputs: list[tuple[str, str]] = []
    for (slug, niche), video_id in zip(channels, video_ids, strict=True):
        proof_channel(ctx, slug, niche=niche)
        oid, title = _fresh_short(ctx, slug)
        _mark_uploaded(ctx, oid, video_id)
        outputs.append((oid, title))
    oid_a, oid_b = outputs[0][0], outputs[1][0]
    ctx.step("the two Shorts the CSV is about", outputs=outputs)

    # ---- 2. the CSV in YouTube Studio's layout, for exactly those two output ids, in the scratch folder
    csv_path = ctx.scratch / csv_name
    total = sum(m[0] for m in METRICS)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerow(["Total", "", "", "", total, "", "", "", "", "", "", ""])
        for (_oid, title), vid, m in zip(outputs, video_ids, METRICS, strict=True):
            w.writerow([vid, title, "Nov 9, 2026", 54, m[0], m[1], m[2], m[3], m[5], m[6], m[7], m[8]])
    ctx.step("CSV written", path=str(csv_path), total_row_views=total, output_ids=[oid_a, oid_b])
    earlier = db_rows(ctx, "select count(*) from analytics where output_id in (?, ?)", (oid_a, oid_b))[0][0]
    ctx.check(
        earlier == 0,
        "the two Shorts are new: no analytics row of theirs exists before the import",
        output_ids=[oid_a, oid_b],
        analytics_rows=earlier,
    )
    # what the install already shows before the import: every figure the page and the API show afterwards is
    # this baseline plus the CSV, whatever earlier runs left on the install
    status, base = ctx.api("GET", "/api/learn/summary")
    ctx.check(
        status == 200 and isinstance(base, dict), "baseline summary read before the import", status=status
    )
    before_daily = {str(d["date"]): (int(d["shorts"]), int(d["long"])) for d in base.get("daily") or []}
    baseline_videos = _latest_rows(ctx)
    ctx.step(
        "analytics already on this install (before the import)",
        analysed_videos=len(baseline_videos),
        days_with_data=len(before_daily),
        views_in_summary_days=sum(s + lg for s, lg in before_daily.values()),
    )

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
    ctx.step("CSV chosen on the Results drop zone", file=csv_name)

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
    saved = sorted((ctx.home / "imports" / "analytics").glob(f"*-{csv_name}"))
    ctx.check(
        bool(saved) and saved[-1].read_bytes() == csv_path.read_bytes(),
        "the CSV was kept under imports/analytics, byte for byte",
        files=[p.name for p in saved][-3:],
    )

    # ---- 4. the database effects
    today = datetime.now(UTC).date()
    import_dates: list[str] = []
    for oid, vid, m in zip((oid_a, oid_b), video_ids, METRICS, strict=True):
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
        import_dates.append(str(got[0][0]))
    import_date = import_dates[0]
    ctx.check(
        len(set(import_dates)) == 1,
        "both videos were stored under one import day",
        import_dates=import_dates,
    )
    changes = result.get("changes") or []
    ctx.check(bool(changes), "the import moved pool weights", moved=len(changes))
    mine = tuple(c[0] for c in channels)
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
    for (_oid, title), vid, m in zip(outputs, video_ids, METRICS, strict=True):
        # a Short of an earlier run with the same figures may take its place in the list: what counts is a
        # listed Short with the CSV's views and stayed %, so the title is recorded, not required
        hit = [
            v
            for v in videos
            if v.get("views") == m[0] and v.get("viewed_pct") == m[1] and v.get("kind") == "short"
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
            len(hit) >= 1,
            f"summary lists a Short with the views and stayed % of {vid}",
            expected=(m[0], m[1]),
            title=title,
            found=hit,
            same_title=sum(1 for v in hit if v.get("title") == title),
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
    # the CSV against the API, on every run: the day-by-day views before the import plus the CSV's must be
    # what the summary holds now (the CSV's views land on the import day, as Shorts, and no other day moves)
    moved: dict[str, list[int]] = {}
    for d in daily:
        was = before_daily.get(str(d["date"]), (0, 0))
        change = [int(d["shorts"]) - was[0], int(d["long"]) - was[1]]
        if change != [0, 0]:
            moved[str(d["date"])] = change
    ctx.check(
        total == BOX_VIEWS and moved == {import_date: [total, 0]},
        "daily views: the CSV's 3,150 views arrived on the import day, all Shorts, and no other day moved",
        csv_total=total,
        import_date=import_date,
        moved=moved,
        days_with_data_before=len(before_daily),
        last_day=last,
    )
    expected_views = sum(
        sum(before_daily.get(str(d["date"]), (0, 0))) + (total if str(d["date"]) == import_date else 0)
        for d in daily
    )
    expected_shorts = sum(
        before_daily.get(str(d["date"]), (0, 0))[0] + (total if str(d["date"]) == import_date else 0)
        for d in daily
    )
    expected_share = _js_round(expected_shorts / expected_views * 100) if expected_views else None
    # retention: the listed videos are the baseline's analysed videos and the CSV's two, as the summary lists
    csv_rows = [(int(m[0]), round(float(m[1]), 1)) for m in METRICS]
    expected_retention = _weighted(_listed(baseline_videos + csv_rows))
    csv_retention = _weighted(csv_rows)
    ctx.check(
        csv_retention == BOX_RETENTION and sum(r[0] for r in csv_rows) == BOX_VIEWS,
        "the CSV's own figures: 3,150 views and a view-weighted stayed-to-watch of 66 %",
        csv_rows=csv_rows,
        csv_retention=csv_retention,
        csv_views=sum(r[0] for r in csv_rows),
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
    ctx.check(
        day_total == expected_views and day_shorts == expected_shorts,
        "summary: views of the last 14 days are the baseline's plus the CSV's 3,150, Shorts included",
        api_views=day_total,
        api_shorts=day_shorts,
        expected_views=expected_views,
        expected_shorts=expected_shorts,
        baseline_views=sum(sum(v) for v in before_daily.values()),
    )
    ctx.check(
        weighted is not None and weighted == expected_retention,
        "summary: the view-weighted stayed-to-watch of the listed videos is the baseline's plus the CSV's",
        api_retention=weighted,
        expected_retention=expected_retention,
        analysed_before=len(baseline_videos),
        listed=[(v.get("views"), v.get("viewed_pct")) for v in videos],
    )

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
        m_views is not None and m_views.group(1) == f"{day_total:,}" == f"{expected_views:,}",
        "page: Views, last 14 days (the baseline's plus the CSV's 3,150, and the API's own figure)",
        shown=m_views.group(1) if m_views else None,
        expected=f"{expected_views:,}",
        api=f"{day_total:,}",
    )
    m_mix = re.search(r"(\d+)%\s+Mix", text)
    ctx.check(
        m_mix is not None
        and share is not None
        and int(m_mix.group(1)) == share == expected_share
        and f"{share}% Shorts, {100 - share}% full videos" in text,
        "page: mix of views from Shorts (the baseline's plus the CSV's, and the API's own figure)",
        shown=m_mix.group(1) if m_mix else None,
        expected=expected_share,
        api=share,
    )
    m_ret = re.search(r"(\d+)%\s+Retention", text)
    ctx.check(
        m_ret is not None
        and weighted is not None
        and int(m_ret.group(1)) == weighted == expected_retention
        and "Average, weighted by views" in text
        and f"Target {TARGET}%" in text,
        "page: retention is the view-weighted average of the stayed-to-watch figures (baseline plus the CSV)",
        shown=m_ret.group(1) if m_ret else None,
        expected=expected_retention,
        api=weighted,
    )
    if not baseline_videos:  # an install with no other analytics: the box's own figures, as drawn
        ctx.check(
            m_views is not None
            and m_views.group(1) == f"{BOX_VIEWS:,}"
            and m_ret is not None
            and int(m_ret.group(1)) == BOX_RETENTION,
            "page: on an install with no other analytics, the box's figures: 3,150 views and 66 % retention",
            views=m_views.group(1) if m_views else None,
            retention=m_ret.group(1) if m_ret else None,
        )
    for vid, m in zip(video_ids, METRICS, strict=True):
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
