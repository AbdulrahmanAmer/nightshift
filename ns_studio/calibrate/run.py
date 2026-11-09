"""Calibration harness (spec section 16.5): generate stories, compare critic and human scores (C7)."""

from __future__ import annotations

import csv
import importlib
import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ns_common.db import Db
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_common.textstats import word_count
from ns_studio.channels.crud import edit_channel
from ns_studio.context import JobContext
from ns_studio.errors import StudioError

RUBRIC: tuple[str, ...] = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")
HUMAN_PASS_AVG = 8.0
HUMAN_PASS_MIN = 6.0
GOOD_PASS_TARGET = 0.80
BAD_PASS_LIMIT = 0.20
REFUSAL_LIMIT = 0.02
MIN_WORDS = 20
MIN_SCORED_ROWS = 5
_REFUSAL = re.compile(
    r"\b(i can'?t|i cannot|i'm sorry|i am sorry|i apologi[sz]e|as an ai|i won'?t|i'm not able to|"
    r"unable to (?:help|assist|comply|write))\b",
    re.IGNORECASE,
)

GeneratorFn = Callable[[JobContext], str]
CriticFn = Callable[[JobContext, str], dict]


# ------------------------------------------------------------------ statistics
def _ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks with ties sharing the average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation; 0.0 when either side has no variance or fewer than 2 points."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman rank correlation in pure Python (average ranks for ties); 0.0 without variance."""
    if len(xs) != len(ys):
        raise ValueError("spearman needs two series of equal length")
    return pearson(_ranks(xs), _ranks(ys))


def refusal_like(text: str) -> bool:
    """True for empty or very short output and for refusal / meta phrasing near the start."""
    return word_count(text) < MIN_WORDS or bool(_REFUSAL.search(text[:300]))


@dataclass(frozen=True)
class BarRecommendation:
    """Critic pass bar chosen from human scores, with the pass rates it produces."""

    bar: float
    good_pass_rate: float
    bad_pass_rate: float
    satisfied: bool


def recommend_bar(
    critic_avgs: Sequence[float], human_avgs: Sequence[float], human_mins: Sequence[float]
) -> BarRecommendation:
    """Highest bar so that >= 80 % of human-passing and <= 20 % of human-failing stories pass.

    A story is human-passing when its human average is >= 8 and no human score is below 6.
    When no bar satisfies both limits, the bar with the best (good - bad) pass-rate gap is returned
    with `satisfied=False`.
    """
    triples = list(zip(critic_avgs, human_avgs, human_mins, strict=True))
    good = [c for c, a, m in triples if a >= HUMAN_PASS_AVG and m >= HUMAN_PASS_MIN]
    bad = [c for c, a, m in triples if not (a >= HUMAN_PASS_AVG and m >= HUMAN_PASS_MIN)]
    if not critic_avgs:
        return BarRecommendation(HUMAN_PASS_AVG, 0.0, 0.0, False)
    candidates = sorted({math.floor(round(c * 10, 6)) / 10 for c in critic_avgs})

    def rates(bar: float) -> tuple[float, float]:
        g = sum(1 for c in good if c >= bar - 1e-9) / len(good) if good else 1.0
        b = sum(1 for c in bad if c >= bar - 1e-9) / len(bad) if bad else 0.0
        return g, b

    feasible = [c for c in candidates if rates(c)[0] >= GOOD_PASS_TARGET and rates(c)[1] <= BAD_PASS_LIMIT]
    if feasible and good and bad:
        bar = max(feasible)
        g, b = rates(bar)
        return BarRecommendation(bar, g, b, True)
    best = max(candidates, key=lambda c: (rates(c)[0] - rates(c)[1], c))
    g, b = rates(best)
    return BarRecommendation(best, g, b, False)


# ------------------------------------------------------------------ generation
@dataclass
class CalibrationStory:
    """One generated story with its critic scores.

    `refusal` is only for a real refusal (refusal-like or too-short text). A story that could not be made
    because of an error (dedupe stop NS-E304, LLM timeout NS-E301, engine down, critic failure) is `failed`
    with its `error_code`: it is not a refusal and stays out of the refusal rate.
    """

    story_id: str
    text: str
    scores: dict[str, float] = field(default_factory=dict)
    critic_avg: float | None = None
    refusal: bool = False
    failed: bool = False
    error_code: str = ""
    note: str = ""


def default_generator(ctx: JobContext) -> str:
    """Run the C5 stages ideas -> plan -> draft (no critic loop) and return the story text."""
    from ns_studio.stages import REGISTRY, run_stage

    for name in ("ideas", "plan", "draft"):
        stage = REGISTRY.get(name)
        if stage is None:
            raise NsError("NS-E306", f"stage {name!r} is not registered; C5 text stages are missing")
        run_stage(ctx, stage, force=True)
    return str(ctx.read_json("story.json")["text"])


def default_critic(ctx: JobContext, text: str) -> dict:
    """Score `text` with the C5 `critic` prompt (looked up lazily so C5 may land later)."""
    prompts = importlib.import_module("ns_studio.text.prompts")
    validators = importlib.import_module("ns_studio.text.validators")
    return dict(prompts.call_json(ctx, "critic", story=text, banned=validators.load_banned()))


def generate(
    ctx_factory: Callable[[int], JobContext],
    n: int,
    *,
    generator: GeneratorFn | None = None,
    critic: CriticFn | None = None,
) -> list[CalibrationStory]:
    """Generate `n` stories and score each with the critic; refusals and failures are flagged, not raised."""
    gen: GeneratorFn = generator or default_generator
    crit: CriticFn = critic or default_critic
    out: list[CalibrationStory] = []
    for i in range(1, n + 1):
        ctx = ctx_factory(i)
        story = CalibrationStory(story_id=f"cal-{i:02d}", text="")
        try:
            story.text = gen(ctx).strip()
        except NsError as e:
            story.failed, story.error_code, story.note = True, e.code, str(e)
            out.append(story)
            continue
        if refusal_like(story.text):
            story.refusal, story.note = True, "refusal or too short"
            out.append(story)
            continue
        try:
            critique = crit(ctx, story.text)
            story.scores = {k: float(critique["scores"][k]) for k in RUBRIC}
        except (NsError, KeyError, TypeError, ValueError) as e:
            story.failed = True
            story.error_code = e.code if isinstance(e, NsError) else "critic"
            story.note = f"critic failed: {e}"
            out.append(story)
            continue
        story.critic_avg = sum(story.scores.values()) / len(RUBRIC)
        out.append(story)
    return out


def refusal_rate(stories: Sequence[CalibrationStory]) -> float:
    """Share of real refusals among the stories that could be judged (failed stories are left out)."""
    judged = [s for s in stories if not s.failed]
    return sum(1 for s in judged if s.refusal) / len(judged) if judged else 0.0


def failure_summary(failed: int, codes: Sequence[str]) -> str:
    """The line printed for stories that could not be made, or "" when there are none."""
    if failed <= 0:
        return ""
    counts: dict[str, int] = {}
    for code in codes:
        counts[code or "unknown"] = counts.get(code or "unknown", 0) + 1
    detail = ", ".join(f"{code} x{n}" for code, n in sorted(counts.items())) or "unknown"
    noun = "story" if failed == 1 else "stories"
    return f"{failed} {noun} could not be made: {detail} (not counted in the refusal rate)"


def generation_report(stories: Sequence[CalibrationStory]) -> list[str]:
    """The lines `studio calibrate` prints after generating: the refusal rate and any failed stories."""
    lines = [f"refusal rate so far: {refusal_rate(stories):.1%}"]
    failed = [s for s in stories if s.failed]
    summary = failure_summary(len(failed), [s.error_code for s in failed])
    if summary:
        lines.append(summary)
    return lines


def calibration_dir(paths: Paths, slug: str) -> Path:
    """`data/calibration/<slug>/`."""
    return paths.calibration_dir / slug


def write_generation(paths: Paths, db: Db, slug: str, stories: Sequence[CalibrationStory]) -> Path:
    """Write stories.csv, one .txt per story and the scores.csv template; fill the `calibration` table."""
    d = calibration_dir(paths, slug)
    d.mkdir(parents=True, exist_ok=True)
    with (d / "stories.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["story_id", "words", "critic_avg", "refusal", "failed", "error_code", *RUBRIC, "note"])
        for s in stories:
            w.writerow(
                [
                    s.story_id,
                    word_count(s.text),
                    "" if s.critic_avg is None else f"{s.critic_avg:.3f}",
                    int(s.refusal),
                    int(s.failed),
                    s.error_code,
                    *(f"{s.scores[k]:g}" if k in s.scores else "" for k in RUBRIC),
                    s.note,
                ]
            )
    with (d / "scores.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["story_id", *RUBRIC])
        for s in stories:
            w.writerow([s.story_id, *([""] * len(RUBRIC))])
    for s in stories:
        (d / f"{s.story_id}.txt").write_text(s.text + "\n", encoding="utf-8")
        db.execute(
            "insert or replace into calibration(channel, story_id, critic_json, human_json) "
            "values (?,?,?, (select human_json from calibration where channel=? and story_id=?))",
            (
                slug,
                s.story_id,
                json.dumps(
                    {
                        "scores": s.scores,
                        "avg": s.critic_avg,
                        "refusal": s.refusal,
                        "failed": s.failed,
                        "error_code": s.error_code,
                    }
                ),
                slug,
                s.story_id,
            ),
        )
    return d


# ------------------------------------------------------------------ scoring
@dataclass
class CalibrationReport:
    """Result of `studio calibrate --score`."""

    channel: str
    rows: int
    per_item_spearman: dict[str, float]
    overall_spearman: float
    mean_bias: float
    recommended_bar: float
    good_pass_rate: float
    bad_pass_rate: float
    satisfied: bool
    refusal_rate: float
    two_model_recommended: bool
    failed: int = 0
    failed_codes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Plain dict for JSON output."""
        return asdict(self)


def _read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    except OSError as e:
        raise StudioError("NS-E324", f"{path}: {e}") from e


def _num(row: dict[str, str], key: str, where: str) -> float:
    raw = (row.get(key) or "").strip()
    try:
        value = float(raw)
    except ValueError as e:
        raise StudioError("NS-E324", f"{where}: column {key!r} is not a number ({raw!r})") from e
    if not 1.0 <= value <= 10.0:
        raise StudioError("NS-E324", f"{where}: column {key!r} must be 1-10 (got {value:g})")
    return value


def score(paths: Paths, db: Db, slug: str, scores_csv: Path) -> CalibrationReport:
    """Correlate human scores with the critic, recommend a pass bar and write it to the channel YAML."""
    d = calibration_dir(paths, slug)
    stories_file = d / "stories.csv"
    stories = {r["story_id"]: r for r in _read_rows(stories_file)} if stories_file.exists() else {}
    if not stories:
        raise StudioError("NS-E324", f"no stories.csv for channel {slug}; run 'studio calibrate' first")
    failed_rows = [r for r in stories.values() if r.get("failed") == "1"]
    judged = len(stories) - len(failed_rows)
    refusals = sum(1 for r in stories.values() if r.get("refusal") == "1" and r.get("failed") != "1")
    refusal_rate = refusals / judged if judged else 0.0

    critic: dict[str, list[float]] = {k: [] for k in RUBRIC}
    human: dict[str, list[float]] = {k: [] for k in RUBRIC}
    critic_avgs: list[float] = []
    human_avgs: list[float] = []
    human_mins: list[float] = []
    for row in _read_rows(scores_csv):
        sid = (row.get("story_id") or "").strip()
        st = stories.get(sid)
        if (
            st is None
            or st.get("refusal") == "1"
            or st.get("failed") == "1"
            or not (row.get(RUBRIC[0]) or "").strip()
        ):
            continue
        h = {k: _num(row, k, sid) for k in RUBRIC}
        c = {k: float(st[k]) for k in RUBRIC}
        for k in RUBRIC:
            critic[k].append(c[k])
            human[k].append(h[k])
        critic_avgs.append(sum(c.values()) / len(RUBRIC))
        human_avgs.append(sum(h.values()) / len(RUBRIC))
        human_mins.append(min(h.values()))
        db.execute(
            "update calibration set human_json=? where channel=? and story_id=?",
            (json.dumps({"scores": h, "avg": human_avgs[-1]}), slug, sid),
        )
    if len(critic_avgs) < MIN_SCORED_ROWS:
        raise StudioError(
            "NS-E324", f"need at least {MIN_SCORED_ROWS} scored stories, found {len(critic_avgs)}"
        )
    rec = recommend_bar(critic_avgs, human_avgs, human_mins)
    report = CalibrationReport(
        channel=slug,
        rows=len(critic_avgs),
        per_item_spearman={k: round(spearman(critic[k], human[k]), 4) for k in RUBRIC},
        overall_spearman=round(spearman(critic_avgs, human_avgs), 4),
        mean_bias=round(sum(critic_avgs) / len(critic_avgs) - sum(human_avgs) / len(human_avgs), 4),
        recommended_bar=rec.bar,
        good_pass_rate=round(rec.good_pass_rate, 4),
        bad_pass_rate=round(rec.bad_pass_rate, 4),
        satisfied=rec.satisfied,
        refusal_rate=round(refusal_rate, 4),
        two_model_recommended=refusal_rate > REFUSAL_LIMIT,
        failed=len(failed_rows),
        failed_codes=[r.get("error_code") or "" for r in failed_rows],
    )
    bar = min(10.0, max(5.0, rec.bar))
    edit_channel(paths, db, slug, {"critic_bar": bar})
    text = json.dumps(report.as_dict(), indent=2) + "\n"
    (d / "calibration_report.json").write_text(text, encoding="utf-8")
    return report
