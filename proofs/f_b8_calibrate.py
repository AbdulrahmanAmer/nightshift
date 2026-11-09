"""F-B8: `studio calibrate` round trip.

On a channel of its own: `studio calibrate --n 6` writes stories with critic rows (files and `calibration`
table); `studio calibrate --score FILE` with human scores prints Spearman per rubric item, the critic's mean
bias and a recommended critic_bar, and writes that bar to the channel (its YAML and what the Channels page
reads). The proof computes Spearman, bias, the pass rates and the recommended critic_bar itself from the
critic scores in stories.csv and the human CSV (the rule of ns_studio/calibrate/run.py recommend_bar: the
highest candidate bar that lets 80 % of the human-passing and at most 20 % of the human-failing stories
pass, else the bar with the best gap), and compares them with what the command printed and wrote. Ported
from runs/qa/fb8.py steps (scores-fb8b.csv; evidence log 2026-11-09, 0.1.12 and 0.1.14).
"""

from __future__ import annotations

import csv
import json
import math
import re
from datetime import datetime
from typing import Any

from proofs._lib import db_rows, pauses_proof_channels, proof_channel, short

PROOF = {
    "id": "f-b8-calibrate",
    "boxes": ["F-B8"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 600,
    "fakes": [],
}

RUBRIC = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")
N_STORIES = 6
START_BAR = 6.5  # the proof channel starts here (the command may legitimately recommend this very bar, so the
# write is told by the YAML file being saved again, not by the value alone)
# human scores per story, in RUBRIC order (QA's scores-fb8b.csv: varied, 2-9)
HUMAN = {
    "cal-01": (9, 8, 8, 7, 8, 6, 8),
    "cal-02": (4, 5, 5, 4, 3, 4, 5),
    "cal-03": (7, 7, 6, 7, 7, 7, 7),
    "cal-04": (2, 3, 3, 2, 2, 3, 3),
    "cal-05": (8, 9, 7, 8, 8, 7, 8),
    "cal-06": (5, 4, 6, 5, 5, 5, 4),
}


# ---- the statistics, written again here so the product's numbers are checked against an independent copy
def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float:
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    sxx = sum((x - mx) ** 2 for x in rx)
    syy = sum((y - my) ** 2 for y in ry)
    if sxx == 0 or syy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(rx, ry, strict=True)) / math.sqrt(sxx * syy)


def _pass_rates(
    bar: float, critic: list[float], human_avg: list[float], human_min: list[float]
) -> tuple[float, float]:
    """Share of human-passing (avg >= 8, none under 6) and of human-failing stories the critic passes."""
    good = [c for c, a, m in zip(critic, human_avg, human_min, strict=True) if a >= 8 and m >= 6]
    bad = [c for c, a, m in zip(critic, human_avg, human_min, strict=True) if not (a >= 8 and m >= 6)]
    g = sum(1 for c in good if c >= bar - 1e-9) / len(good) if good else 1.0
    b = sum(1 for c in bad if c >= bar - 1e-9) / len(bad) if bad else 0.0
    return g, b


def _recommend_bar(
    critic: list[float], human_avg: list[float], human_min: list[float]
) -> tuple[float, float, float, bool]:
    """(bar, good pass rate, bad pass rate, satisfied) by the product's rule, written again from its text.

    Candidates are the critic averages cut to one decimal (floor, never up). A candidate is feasible when
    at least 80 % of the human-passing stories and at most 20 % of the human-failing ones pass it. With a
    feasible candidate and stories on both sides the highest feasible one is the bar (satisfied); otherwise
    the bar is the candidate with the best (good rate - bad rate), the higher bar on a tie (not satisfied)."""
    good = [c for c, a, m in zip(critic, human_avg, human_min, strict=True) if a >= 8 and m >= 6]
    bad = [c for c, a, m in zip(critic, human_avg, human_min, strict=True) if not (a >= 8 and m >= 6)]
    candidates = sorted({math.floor(round(c * 10, 6)) / 10 for c in critic})
    rates = {c: _pass_rates(c, critic, human_avg, human_min) for c in candidates}
    feasible = [c for c in candidates if rates[c][0] >= 0.80 and rates[c][1] <= 0.20]
    if feasible and good and bad:
        bar = max(feasible)
        return bar, rates[bar][0], rates[bar][1], True
    best = max(candidates, key=lambda c: (rates[c][0] - rates[c][1], c))
    return best, rates[best][0], rates[best][1], False


def _out(done: Any) -> str:
    return f"{done.stdout or ''}\n{done.stderr or ''}"


def _csv_rows(path: Any) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _yaml_bar(ctx: Any, slug: str) -> float | None:
    text = (ctx.home / "config" / "channels" / f"{slug}.yaml").read_text(encoding="utf-8-sig")
    found = re.search(r"(?m)^critic_bar:\s*([0-9.]+)\s*$", text)
    return float(found.group(1)) if found else None


@pauses_proof_channels
def run(ctx: Any) -> None:
    # Each run calibrates a channel of its own (its idea history refuses repeated fake ideas, NS-E304);
    # @pauses_proof_channels leaves it paused at the end, pass or fail, so the nightly never picks it up.
    slug = "proof-cal-" + datetime.now().strftime("%m%d-%H%M%S")
    _calibrate(ctx, slug)


def _calibrate(ctx: Any, slug: str) -> None:
    # a channel's idea history refuses repeated fake ideas (NS-E304): each run calibrates a channel of its own
    proof_channel(ctx, slug, niche=f"proof calibrate {slug}", critic_bar=START_BAR)
    ctx.check(
        _yaml_bar(ctx, slug) == START_BAR,
        "the proof channel starts with its own critic_bar",
        channel=slug,
        critic_bar=_yaml_bar(ctx, slug),
    )
    cal = ctx.home / "data" / "calibration" / slug

    # ---- 1. generate stories with critic rows
    done = ctx.cli(["studio", "calibrate", "--channel", slug, "--n", str(N_STORIES)], timeout=300)
    out = _out(done)
    ctx.check(
        done.returncode == 0 and f"wrote {N_STORIES} stories to" in out and slug in out,
        "calibrate --n 6 exits 0 and reports where it wrote the stories",
        exit_code=done.returncode,
        output=short(out),
    )
    ctx.check(
        "refusal rate so far: 0.0%" in out and "could not be made" not in out,
        "no story refused or failed: refusal rate 0.0%",
        output=short(out),
    )
    stories = _csv_rows(cal / "stories.csv")
    ids = [r["story_id"] for r in stories]
    ctx.check(
        ids == [f"cal-{i:02d}" for i in range(1, N_STORIES + 1)]
        and all(r["failed"] == "0" and r["refusal"] == "0" for r in stories),
        "stories.csv: six stories, none refused, none failed",
        ids=ids,
        failed=[r["failed"] for r in stories],
        refusal=[r["refusal"] for r in stories],
    )
    critic: dict[str, list[float]] = {}
    for r in stories:
        values = [float(r[k]) for k in RUBRIC]
        critic[r["story_id"]] = values
        ctx.check(
            all(1 <= v <= 10 for v in values)
            and abs(float(r["critic_avg"]) - sum(values) / len(values)) < 0.001,
            f"{r['story_id']}: critic scored every rubric item 1-10 and critic_avg is their mean",
            scores=values,
            critic_avg=r["critic_avg"],
        )
    texts = {i: (cal / f"{i}.txt").read_text(encoding="utf-8").split() for i in ids}
    ctx.check(
        all(len(words) >= 20 for words in texts.values()),
        "every story text file holds a story (20 words or more)",
        words={i: len(w) for i, w in texts.items()},
    )
    template = _csv_rows(cal / "scores.csv")
    ctx.check(
        [r["story_id"] for r in template] == ids and all(not r[RUBRIC[0]] for r in template),
        "scores.csv is an empty template for the same stories",
        rows=len(template),
    )
    cal_rows = db_rows(
        ctx, "select story_id, critic_json, human_json from calibration where channel=?", (slug,)
    )
    by_id = {r[0]: (json.loads(r[1]), r[2]) for r in cal_rows}
    ctx.check(
        sorted(by_id) == ids
        and all([by_id[i][0]["scores"][k] for k in RUBRIC] == critic[i] and by_id[i][1] is None for i in ids),
        "calibration table: a critic_json row per story with the same scores, no human scores yet",
        rows=len(cal_rows),
    )

    # ---- 2. too few human rows are refused and change nothing on the channel
    short_csv = ctx.scratch / "scores-4.csv"
    with short_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["story_id", *RUBRIC])
        for sid in ids[:4]:
            w.writerow([sid, *HUMAN[sid]])
    done = ctx.cli(["studio", "calibrate", "--channel", slug, "--score", str(short_csv)], timeout=120)
    out = _out(done)
    ctx.check(
        done.returncode != 0 and "NS-E324" in out and "need at least 5 scored stories, found 4" in out,
        "scoring four stories is refused: NS-E324, need at least 5 scored stories, found 4",
        exit_code=done.returncode,
        output=short(out),
    )
    ctx.check(
        _yaml_bar(ctx, slug) == START_BAR,
        "the refused scoring left critic_bar alone",
        critic_bar=_yaml_bar(ctx, slug),
    )

    # ---- 3. score with the human rows
    score_csv = ctx.scratch / "scores-6.csv"
    with score_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["story_id", *RUBRIC])
        for sid in ids:
            w.writerow([sid, *HUMAN[sid]])
    yaml_file = ctx.home / "config" / "channels" / f"{slug}.yaml"
    saved_before = yaml_file.stat().st_mtime_ns
    done = ctx.cli(["studio", "calibrate", "--channel", slug, "--score", str(score_csv)], timeout=120)
    out = _out(done)
    ctx.check(done.returncode == 0, "calibrate --score exits 0", exit_code=done.returncode, output=short(out))
    ctx.check(f"scored stories: {N_STORIES}" in out, "it scored all six stories", output=short(out))

    # what the numbers must be
    c_avgs = [sum(critic[i]) / len(RUBRIC) for i in ids]
    h_avgs = [sum(HUMAN[i]) / len(RUBRIC) for i in ids]
    h_mins = [float(min(HUMAN[i])) for i in ids]
    want_rho = {
        k: _spearman([critic[i][n] for i in ids], [float(HUMAN[i][n]) for i in ids])
        for n, k in enumerate(RUBRIC)
    }
    want_rho["overall"] = _spearman(c_avgs, h_avgs)
    want_bias = sum(c_avgs) / len(c_avgs) - sum(h_avgs) / len(h_avgs)

    printed = {k: float(v) for k, v in re.findall(r"spearman (\w+): ([+-]\d+\.\d{3})", out)}
    ctx.check(
        set(printed) == set(want_rho) and all(abs(printed[k] - want_rho[k]) <= 0.0006 for k in want_rho),
        "printed Spearman per rubric item and overall match the proof's own calculation",
        printed=printed,
        computed={k: round(v, 3) for k, v in want_rho.items()},
    )
    found_bias = re.search(r"critic mean bias: ([+-]\d+\.\d\d)", out)
    ctx.check(
        found_bias is not None and abs(float(found_bias.group(1)) - want_bias) <= 0.006,
        "printed critic mean bias matches critic average minus human average",
        printed=found_bias.group(1) if found_bias else None,
        computed=round(want_bias, 3),
    )
    found_bar = re.search(
        r"recommended critic_bar: ([\d.]+) \(good pass (\d+)%, bad pass (\d+)%, "
        r"(meets|does NOT meet) the 80/20",
        out,
    )
    ctx.check(
        found_bar is not None, "it prints a recommended critic_bar with its pass rates", output=short(out)
    )
    assert found_bar is not None
    bar = float(found_bar.group(1))
    want_bar, good_rate, bad_rate, want_meets = _recommend_bar(c_avgs, h_avgs, h_mins)
    ctx.check(
        abs(bar - want_bar) < 1e-9,
        "the recommended critic_bar the command printed is the one the proof recomputes from the same scores",
        printed_bar=bar,
        recomputed_bar=want_bar,
        critic_avgs=[round(c, 3) for c in c_avgs],
        human_avgs=[round(h, 3) for h in h_avgs],
        human_mins=h_mins,
        recomputed_good_pass=good_rate,
        recomputed_bad_pass=bad_rate,
        recomputed_satisfied=want_meets,
    )
    ctx.check(
        found_bar.group(2) == f"{round(good_rate, 4):.0%}"[:-1]
        and found_bar.group(3) == f"{round(bad_rate, 4):.0%}"[:-1],
        "the pass rates printed for that bar match the proof's own count",
        printed=(found_bar.group(2), found_bar.group(3)),
        computed=(f"{round(good_rate, 4):.0%}", f"{round(bad_rate, 4):.0%}"),
    )
    meets = found_bar.group(4) == "meets"
    ctx.check(
        meets == want_meets,
        "'meets the 80/20 target' is claimed exactly when the recomputed rates and bar satisfy it",
        meets=meets,
        recomputed_satisfied=want_meets,
        good=good_rate,
        bad=bad_rate,
    )
    ctx.check("refusal rate: 0.0%" in out, "refusal rate 0.0% reported", output=short(out))

    # ---- 4. the effects: report file, human rows, the channel
    report = json.loads((cal / "calibration_report.json").read_text(encoding="utf-8"))
    ctx.check(
        report["rows"] == N_STORIES
        and abs(report["recommended_bar"] - want_bar) < 1e-9
        and report["satisfied"] == meets
        and all(abs(report["per_item_spearman"][k] - want_rho[k]) <= 0.0001 for k in RUBRIC)
        and abs(report["overall_spearman"] - want_rho["overall"]) <= 0.0001
        and abs(report["mean_bias"] - want_bias) <= 0.0001
        and abs(report["good_pass_rate"] - good_rate) <= 0.0001
        and abs(report["bad_pass_rate"] - bad_rate) <= 0.0001,
        "calibration_report.json holds the same numbers",
        report=short(report, 700),
    )
    cal_rows = db_rows(ctx, "select story_id, human_json from calibration where channel=?", (slug,))
    human_db = {r[0]: json.loads(r[1]) if r[1] else None for r in cal_rows}
    ctx.check(
        all(
            human_db.get(i) is not None
            and [human_db[i]["scores"][k] for k in RUBRIC] == [float(v) for v in HUMAN[i]]
            for i in ids
        ),
        "calibration table: the human scores of all six stories are stored",
        stored=sum(1 for v in human_db.values() if v),
    )
    written = _yaml_bar(ctx, slug)
    status, card = ctx.api("GET", f"/api/channels/{slug}")
    profile = card.get("profile") if isinstance(card, dict) else None
    saved_after = yaml_file.stat().st_mtime_ns
    ctx.check(
        written == min(10.0, max(5.0, want_bar)) and saved_after > saved_before,
        "the recomputed critic_bar (kept within 5 to 10) is what the scoring wrote to the channel's YAML",
        yaml_critic_bar=written,
        printed_bar=bar,
        recomputed_bar=want_bar,
        critic_avgs=[round(c, 3) for c in c_avgs],
        human_avgs=[round(h, 3) for h in h_avgs],
        start_bar=START_BAR,
        equals_start_bar=written == START_BAR,
        yaml_saved_again=saved_after > saved_before,
    )
    ctx.check(
        status == 200 and isinstance(profile, dict) and profile.get("critic_bar") == written,
        "the Channels page's own read (GET /api/channels/{slug}) shows the new critic_bar",
        status=status,
        critic_bar=profile.get("critic_bar") if isinstance(profile, dict) else None,
    )
    if not meets:
        ctx.step(
            "observation: the bar is written although the 80/20 target is not met (a spec question)",
            bar=bar,
            good_pass=good_rate,
            bad_pass=bad_rate,
        )
