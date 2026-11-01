### Task 3: Word budgets and hard validators (WP-D, `sonnet`)

**Files:**
- Create: `ns_studio/text/budget.py`, `ns_studio/text/validators.py`, `tests/test_validators.py`

**Interfaces:**
- Produces:
```python
# budget.py
PACING_WPM = {"brisk": 175, "standard": 155, "measured": 140}
@dataclass(frozen=True)
class Budget: words: int; min_words: int; max_words: int; parts: int; words_per_part: int; wpm: int
def short_budget(length_bucket_s: int, pacing: str, *, parts: int = 1, hook_words: int = 12, closing_words: int = 0) -> Budget
    # words = round(seconds*wpm/60) - hook_words - closing_words ; min/max = ±20 %
# validators.py
@dataclass(frozen=True)
class Violation: rule: str; detail: str
def load_banned() -> list[str]                                   # contracts/policy/banned_phrases.txt
def check_length(text: str, budget: Budget) -> Violation | None
def check_banned(text: str, banned: list[str]) -> Violation | None
def check_reading_level(text: str, lo: float = 5.0, hi: float = 8.0) -> Violation | None
def check_sentence_length(text: str, max_words: int = 35) -> Violation | None
def check_opening_similarity(opening: str, past_openings: list[str], embed: Embed, threshold: float = 0.8) -> Violation | None
def validate_story(text: str, budget: Budget, *, past_openings: list[str], embed: Embed) -> list[Violation]
def raise_if(violations: list[Violation]) -> None               # NsError("NS-E303", "; ".join(...))
```

- [ ] **Step 1: Write failing tests**

```python
# tests/test_validators.py
import pytest

from ns_common.embed_client import FakeEmbed
from ns_common.errors import NsError
from ns_studio.text import validators as V
from ns_studio.text.budget import short_budget

STORY = ("The doorbell camera pings at 3:12 every night. " * 12).strip()   # 108 words, grade ~2


def test_short_budget_math():
    b = short_budget(50, "standard")
    assert b.wpm == 155 and b.words == 129 - 12 and b.min_words == round(b.words * 0.8) and b.max_words == round(b.words * 1.2)
    assert short_budget(75, "brisk", parts=2).words_per_part == short_budget(75, "brisk", parts=2).words // 2


def test_length_check():
    b = short_budget(50, "standard")
    assert V.check_length("one two three", b) is not None
    assert V.check_length(" ".join(["word"] * b.words), b) is None


def test_banned_and_sentence_length():
    assert V.check_banned("It was a dark and stormy night, truly.", V.load_banned()).rule == "banned_phrase"
    long = " ".join(["word"] * 40) + "."
    assert V.check_sentence_length(long).rule == "sentence_length"
    assert V.check_sentence_length("Short one. Another short one.") is None


def test_reading_level_bounds():
    hard = "The epistemological ramifications of the aforementioned investigation remained fundamentally indeterminate throughout."
    assert V.check_reading_level(hard).rule == "reading_level"
    assert V.check_reading_level("The dog ran home. The cat sat on the mat. We went to bed early that night.", lo=0.0) is None


def test_opening_similarity():
    e = FakeEmbed()
    v = V.check_opening_similarity("The doorbell camera pings at 3:12 every night.",
                                   ["The doorbell camera pings at 3:12 every night."], e)
    assert v is not None and v.rule == "opening_similarity"
    assert V.check_opening_similarity("A new unrelated first line here.", ["Something else entirely."], e) is None


def test_raise_if():
    with pytest.raises(NsError) as ei:
        V.raise_if([V.Violation("x", "y")])
    assert ei.value.code == "NS-E303"
    V.raise_if([])
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement**

```python
# ns_studio/text/budget.py
from __future__ import annotations

from dataclasses import dataclass

PACING_WPM: dict[str, int] = {"brisk": 175, "standard": 155, "measured": 140}


@dataclass(frozen=True)
class Budget:
    words: int
    min_words: int
    max_words: int
    parts: int
    words_per_part: int
    wpm: int


def short_budget(length_bucket_s: int, pacing: str, *, parts: int = 1, hook_words: int = 12, closing_words: int = 0) -> Budget:
    wpm = PACING_WPM[pacing]
    total = round(length_bucket_s * wpm / 60) * parts
    words = max(40, total - hook_words * parts - closing_words)
    return Budget(words=words, min_words=round(words * 0.8), max_words=round(words * 1.2), parts=parts,
                  words_per_part=words // parts, wpm=wpm)
```

```python
# ns_studio/text/validators.py
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from ns_common.embed_client import Embed, cosine
from ns_common.errors import NsError
from ns_common.paths import contracts_dir
from ns_common.textstats import fk_grade, split_sentences, word_count
from ns_studio.text.budget import Budget


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str


@lru_cache(maxsize=1)
def load_banned() -> list[str]:
    lines = (contracts_dir() / "policy" / "banned_phrases.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip().lower() for ln in lines if ln.strip() and not ln.startswith("#")]


def check_length(text: str, budget: Budget) -> Violation | None:
    n = word_count(text)
    if n < budget.min_words or n > budget.max_words:
        return Violation("length", f"{n} words, allowed {budget.min_words}-{budget.max_words}")
    return None


def check_banned(text: str, banned: list[str]) -> Violation | None:
    low = text.lower()
    hits = [b for b in banned if b in low]
    return Violation("banned_phrase", ", ".join(hits)) if hits else None


def check_reading_level(text: str, lo: float = 5.0, hi: float = 8.0) -> Violation | None:
    g = fk_grade(text)
    if g < lo or g > hi:
        return Violation("reading_level", f"grade {g}, allowed {lo}-{hi}")
    return None


def check_sentence_length(text: str, max_words: int = 35) -> Violation | None:
    for s in split_sentences(text):
        if word_count(s) > max_words:
            return Violation("sentence_length", f"{word_count(s)} words: {s[:60]}…")
    return None


def check_opening_similarity(opening: str, past_openings: list[str], embed: Embed, threshold: float = 0.8) -> Violation | None:
    if not past_openings:
        return None
    vecs = embed.embed([opening, *past_openings])
    best = max(cosine(vecs[0], v) for v in vecs[1:])
    if best >= threshold:
        return Violation("opening_similarity", f"cosine {best:.2f} ≥ {threshold}")
    return None


def validate_story(text: str, budget: Budget, *, past_openings: list[str], embed: Embed) -> list[Violation]:
    opening = split_sentences(text)[0] if text.strip() else ""
    checks = [check_length(text, budget), check_banned(text, load_banned()), check_reading_level(text),
              check_sentence_length(text), check_opening_similarity(opening, past_openings, embed)]
    return [c for c in checks if c is not None]


def raise_if(violations: list[Violation]) -> None:
    if violations:
        raise NsError("NS-E303", "; ".join(f"{v.rule}: {v.detail}" for v in violations))
```

- [ ] **Step 4: Run** → 6 passed. (If `test_short_budget_math` fails on the exact number, the formula is `round(50*155/60)=129`, minus 12 hook words = 117.)

- [ ] **Step 5: Commit**

```bash
git add ns_studio/text/budget.py ns_studio/text/validators.py tests/test_validators.py
git commit -m "feat(text): word budgets and hard validators"
```

---

### Task 4: Shorts-only text stages (WP-D, `sonnet`; critic loop reviewed by `opus`)

**Files:**
- Create: `ns_studio/text/stages_shorts.py`, `tests/test_text_stages.py`

**Interfaces:**
- Produces stages (registered, appended to `FORMAT_STAGES["shorts_only"]` in this order) with `tenant="llama"` unless noted:
| name | inputs | outputs | does |
|---|---|---|---|
| `ideas` | — | `idea.json`, `ideas_all.json` | `call_json("ideas", n=8, budget, avoid_premises=<last 50 premises from db>)`; embed every premise; drop any with cosine ≥ 0.85 to a stored idea; pick index 0 of survivors; insert into `ideas` table |
| `plan` | `idea.json` | `plan.json` | `call_json("story_plan", idea, budget)` |
| `draft` | `plan.json` | `story.json` | `call_text("story_draft", plan, budget)` → `{"text", "word_count", "revisions": 0}` |
| `critic_revise` | `story.json` | `story.json`, `critic.json` | loop ≤ 2: `call_json("critic", story, banned)`; pass if avg ≥ critic_bar and min ≥ 6; else `call_text("revise", story, fixes)`; after loop run `validate_story` (raises E303); if still failing after 2 revisions → `NsError("NS-E304")`; store `critic_avg`, `revisions`; insert into `stories` table (opening_line, embedding) |
| `split` | `story.json` | `parts.json` | `call_json("split", story, budget)`; validate each part against `Part` model; recompute `word_count`/`est_seconds` locally (never trust the model's numbers) |
| `hooks` | `parts.json` | `parts.json` | per part: `call_json("hook", part_text, next_part_text)` → 5; `call_json("judge", purpose, constraints, options)` → chosen; write `part.hook`; keep `hooks_all` |
| `seo` | `parts.json` | `seo.json` (list, one per part) | per part `call_json("seo", primary_keyword=channel.seed_keywords[0], part_text, part_n, parts_total)`; then `finalize_seo()` applies §9.2 rules in code: truncate title to 100, ensure keyword in first 40 chars else prefix "<keyword>: ", append `"Original fiction."` when `fiction_label`, ensure `#Shorts` in hashtags, cap hashtags at 5, tags 10–20 and ≤ 500 chars |
| `visual_plan` | `parts.json` | `visual_plan.json` | per part: sentences via `split_sentences`; `call_json("visual_plan", sentences, setting, part_n)`; verify beats cover every sentence index exactly once, else repair by filling gaps with the previous beat's keywords |

Also: `def ideas_budget(ctx) -> Budget` = `short_budget(ctx.combo.length_bucket or 50, ctx.combo.pacing, parts=1)`; `parts` count for shorts-only is 1 unless `ctx.channel` has `series_parts` — not in this slice; always 1.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_text_stages.py
import json
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.models import Part, Seo, VisualBeat
from ns_studio.context import FIXTURES_LLM
from ns_studio.orchestrator import FORMAT_STAGES, run_job
from ns_studio.stages import REGISTRY
from ns_studio.text import stages_shorts as S  # noqa: F401  (registers)
from tests.helpers import make_ctx


def test_registration_order():
    assert FORMAT_STAGES["shorts_only"][:8] == ["ideas", "plan", "draft", "critic_revise", "split", "hooks", "seo", "visual_plan"]


def test_text_pipeline_runs_on_fixtures(tmp_home: Path, monkeypatch):
    monkeypatch.setitem(FORMAT_STAGES, "text_only", FORMAT_STAGES["shorts_only"][:8])
    ctx = make_ctx(tmp_home, fmt="text_only")
    run_job(ctx)
    idea = ctx.read_json("idea.json"); assert idea["premise"]
    story = ctx.read_json("story.json"); assert story["word_count"] > 40 and story["revisions"] >= 0
    parts = [Part.model_validate(p) for p in ctx.read_json("parts.json")]
    assert parts[0].hook and parts[0].word_count == len(parts[0].text.split())
    seo = [Seo.model_validate(s) for s in ctx.read_json("seo.json")]
    assert "#Shorts" in seo[0].hashtags and "Original fiction." in seo[0].description
    assert 10 <= len(seo[0].tags) <= 20
    beats = [VisualBeat.model_validate(b) for b in ctx.read_json("visual_plan.json")]
    covered = sorted(i for b in beats for i in range(b.sentence_start, b.sentence_end + 1))
    assert covered == list(range(covered[-1] + 1))
    assert set(ctx.prompt_versions) >= {"ideas", "story_plan", "story_draft", "critic", "split", "hook", "judge", "seo", "visual_plan"}
    assert ctx.db.one("select count(*) c from stories")["c"] == 1


def test_critic_fail_twice_raises_e304(tmp_home: Path):
    bad = {"scores": {k: 5 for k in ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")}, "fixes": ["Fix it."]}
    ctx = make_ctx(tmp_home)
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"critic": bad})
    ctx.write_json("story.json", {"text": "The doorbell camera pings at 3:12 every night. " * 10, "word_count": 90, "revisions": 0})
    with pytest.raises(NsError) as ei:
        REGISTRY["critic_revise"].run(ctx)
    assert ei.value.code == "NS-E304"
    assert len([c for c in ctx.services.llm.calls if c["prompt_name"] == "revise"]) == 2


def test_ideas_dedupes_against_history(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    fx = json.loads((FIXTURES_LLM / "ideas.json").read_text(encoding="utf-8"))
    first = fx["ideas"][0]
    from ns_common.embed_client import to_blob
    vec = ctx.services.embed.embed([first["premise"]])[0]
    ctx.db.execute("insert into ideas(channel, premise, twist, embedding, created_at, used) values (?,?,?,?,?,1)",
                   (ctx.channel.slug, first["premise"], first["twist"], to_blob(vec), "2026-10-01"))
    REGISTRY["ideas"].run(ctx)
    assert ctx.read_json("idea.json")["premise"] != first["premise"]


def test_seo_finalize_rules():
    seo = S.finalize_seo(Seo(title="A Night I Will Not Forget Any Time Soon Ever", description="d", tags=["a"], hashtags=[], keywords=[]),
                         primary_keyword="scary story", fiction_label=True, part_n=None, parts_total=1)
    assert seo.title.lower().startswith("scary story")
    assert seo.hashtags[0] == "#Shorts"
    assert seo.description.rstrip().endswith("Original fiction.")
    assert len(seo.tags) >= 10
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement `ns_studio/text/stages_shorts.py`**

```python
from __future__ import annotations

from datetime import UTC, datetime

from ns_common.embed_client import cosine, from_blob, to_blob
from ns_common.errors import NsError
from ns_common.models import Part, Seo, VisualBeat
from ns_common.textstats import est_seconds, split_sentences, word_count
from ns_studio.context import JobContext
from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.stages import register
from ns_studio.text.budget import Budget, short_budget
from ns_studio.text.prompts import call_json, call_text
from ns_studio.text.validators import load_banned, raise_if, validate_story

RUBRIC = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")


def ideas_budget(ctx: JobContext) -> Budget:
    return short_budget(ctx.combo.length_bucket or 50, ctx.combo.pacing, parts=1)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class IdeasStage:
    name, tenant, inputs, outputs = "ideas", "llama", (), ("idea.json", "ideas_all.json")

    def run(self, ctx: JobContext) -> None:
        rows = ctx.db.query("select premise, embedding from ideas where channel=? order by idea_id desc limit 50", (ctx.channel.slug,))
        out = call_json(ctx, "ideas", n=8, budget=ideas_budget(ctx), avoid_premises=[r["premise"] for r in rows])
        ideas = out["ideas"]
        ctx.write_json("ideas_all.json", ideas)
        vecs = ctx.services.embed.embed([i["premise"] for i in ideas])
        past = [from_blob(r["embedding"]) for r in rows if r["embedding"]]
        survivors = [(i, v) for i, v in zip(ideas, vecs, strict=True) if not past or max(cosine(v, p) for p in past) < 0.85]
        if not survivors:
            raise NsError("NS-E304", "all 8 ideas too similar to history")
        idea, vec = survivors[0]
        ctx.db.execute("insert into ideas(channel, job_id, premise, twist, embedding, created_at, used) values (?,?,?,?,?,?,1)",
                       (ctx.channel.slug, ctx.job_id, idea["premise"], idea["twist"], to_blob(vec), _now()))
        ctx.write_json("idea.json", idea)


class PlanStage:
    name, tenant, inputs, outputs = "plan", "llama", ("idea.json",), ("plan.json",)

    def run(self, ctx: JobContext) -> None:
        plan = call_json(ctx, "story_plan", idea=ctx.read_json("idea.json"), budget=ideas_budget(ctx))
        ctx.write_json("plan.json", plan)


class DraftStage:
    name, tenant, inputs, outputs = "draft", "llama", ("plan.json",), ("story.json",)

    def run(self, ctx: JobContext) -> None:
        text = call_text(ctx, "story_draft", plan=ctx.read_json("plan.json"), budget=ideas_budget(ctx)).strip()
        ctx.write_json("story.json", {"text": text, "word_count": word_count(text), "revisions": 0})


class CriticReviseStage:
    name, tenant, inputs, outputs = "critic_revise", "llama", ("story.json",), ("story.json", "critic.json")

    def run(self, ctx: JobContext) -> None:
        story = ctx.read_json("story.json")
        text: str = story["text"]
        bar = ctx.channel.critic_bar
        revisions = 0
        critique: dict = {}
        for attempt in range(3):
            critique = call_json(ctx, "critic", story=text, banned=load_banned())
            scores = [float(critique["scores"][k]) for k in RUBRIC]
            avg, low = sum(scores) / len(scores), min(scores)
            ctx.log.info("critic", avg=avg, low=low, attempt=attempt, job_id=ctx.job_id)
            if avg >= bar and low >= 6:
                break
            if attempt == 2:
                raise NsError("NS-E304", f"critic avg {avg:.1f} low {low:.0f} after {revisions} revisions")
            text = call_text(ctx, "revise", story=text, fixes=critique["fixes"]).strip()
            revisions += 1
        budget = ideas_budget(ctx)
        past = [r["opening_line"] for r in ctx.db.query(
            "select opening_line from stories where channel=? order by created_at desc limit 50", (ctx.channel.slug,))]
        raise_if(validate_story(text, budget, past_openings=past, embed=ctx.services.embed))
        scores = critique["scores"]
        avg = sum(float(scores[k]) for k in RUBRIC) / len(RUBRIC)
        opening = split_sentences(text)[0]
        vec = ctx.services.embed.embed([text])[0]
        ctx.db.execute(
            "insert or replace into stories(job_id, channel, format, working_title, text, word_count, opening_line, embedding, critic_avg, revisions, created_at) "
            "values (?,?,?,?,?,?,?,?,?,?,?)",
            (ctx.job_id, ctx.channel.slug, ctx.fmt, None, text, word_count(text), opening, to_blob(vec), avg, revisions, _now()))
        ctx.write_json("critic.json", critique)
        ctx.write_json("story.json", {"text": text, "word_count": word_count(text), "revisions": revisions, "critic_avg": avg})


class SplitStage:
    name, tenant, inputs, outputs = "split", "llama", ("story.json",), ("parts.json",)

    def run(self, ctx: JobContext) -> None:
        budget = ideas_budget(ctx)
        out = call_json(ctx, "split", story=ctx.read_json("story.json")["text"], budget=budget)
        parts: list[dict] = []
        for i, p in enumerate(out["parts"], start=1):
            text = p["text"].strip()
            parts.append(Part(n=i, text=text, hook=p.get("hook") or split_sentences(text)[0], word_count=word_count(text),
                              est_seconds=est_seconds(text, budget.wpm), cliffhanger=bool(p.get("cliffhanger", False))).model_dump())
        parts[-1]["cliffhanger"] = False
        ctx.write_json("parts.json", parts)


class HooksStage:
    name, tenant, inputs, outputs = "hooks", "llama", ("parts.json",), ("parts.json",)

    def run(self, ctx: JobContext) -> None:
        parts = ctx.read_json("parts.json")
        for i, p in enumerate(parts):
            if p.get("hooks_all"):
                continue
            nxt = parts[i + 1]["text"] if i + 1 < len(parts) else None
            hooks = call_json(ctx, "hook", part_text=p["text"], next_part_text=nxt)["hooks"]
            hooks = [h.strip() for h in hooks if len(h.split()) <= 12] or [split_sentences(p["text"])[0]]
            j = call_json(ctx, "judge", purpose="Pick the hook most likely to stop a scroll while staying strictly true to the part.",
                          constraints="Max 12 words. No overpromising. Style: " + ctx.combo.hook, options=hooks)
            idx = min(int(j["index"]), len(hooks) - 1)
            p["hooks_all"], p["hook"] = hooks, hooks[idx]
        ctx.write_json("parts.json", parts)


def finalize_seo(seo: Seo, *, primary_keyword: str, fiction_label: bool, part_n: int | None, parts_total: int) -> Seo:
    title = seo.title.strip()
    if primary_keyword.lower() not in title[:40].lower():
        title = f"{primary_keyword.title()}: {title}"
    if part_n and f"part {part_n}" not in title.lower():
        title = f"{title} (Part {part_n})"
    title = title[:100]
    desc_lines = [seo.description.strip()]
    if part_n:
        desc_lines.append(f"Part {part_n} of {parts_total}.")
    if fiction_label:
        desc_lines.append("Original fiction.")
    hashtags = [h if h.startswith("#") else f"#{h}" for h in seo.hashtags]
    hashtags = ["#Shorts", *[h for h in hashtags if h.lower() != "#shorts"]][:5]
    tags = list(dict.fromkeys(t.strip() for t in seo.tags if t.strip()))
    fillers = [primary_keyword, "short story", "fiction", "narrated story", "story time", "original story",
               "audio story", "storytelling", "shorts story", "suspense"]
    for f in fillers:
        if len(tags) >= 10:
            break
        if f not in tags:
            tags.append(f)
    tags = tags[:20]
    while sum(len(t) for t in tags) + len(tags) - 1 > 500:
        tags.pop()
    return Seo(title=title, description="\n".join(desc_lines), tags=tags, hashtags=hashtags, keywords=seo.keywords)


class SeoStage:
    name, tenant, inputs, outputs = "seo", "llama", ("parts.json",), ("seo.json",)

    def run(self, ctx: JobContext) -> None:
        parts = ctx.read_json("parts.json")
        kw = ctx.channel.seed_keywords[0] if ctx.channel.seed_keywords else ctx.channel.niche.split()[0]
        out: list[dict] = []
        for p in parts:
            part_n = p["n"] if len(parts) > 1 else None
            raw = Seo.model_validate(call_json(ctx, "seo", primary_keyword=kw, part_text=p["text"], part_n=part_n, parts_total=len(parts)))
            out.append(finalize_seo(raw, primary_keyword=kw, fiction_label=ctx.channel.fiction_label,
                                    part_n=part_n, parts_total=len(parts)).model_dump())
        ctx.write_json("seo.json", out)


class VisualPlanStage:
    name, tenant, inputs, outputs = "visual_plan", "llama", ("parts.json", "idea.json"), ("visual_plan.json",)

    def run(self, ctx: JobContext) -> None:
        setting = ctx.read_json("idea.json").get("setting", "")
        beats_out: list[dict] = []
        for p in ctx.read_json("parts.json"):
            sentences = split_sentences(p["text"])
            raw = call_json(ctx, "visual_plan", sentences=sentences, setting=setting, part_n=p["n"])["beats"]
            beats = [VisualBeat.model_validate(b | {"part": p["n"]}) for b in raw]
            beats.sort(key=lambda b: b.sentence_start)
            covered: set[int] = set()
            fixed: list[VisualBeat] = []
            for b in beats:
                s, e = max(0, b.sentence_start), min(len(sentences) - 1, b.sentence_end)
                if s > e or any(i in covered for i in range(s, e + 1)):
                    continue
                fixed.append(b.model_copy(update={"sentence_start": s, "sentence_end": e, "beat": len(fixed)}))
                covered.update(range(s, e + 1))
            for i in range(len(sentences)):
                if i not in covered:
                    prev = fixed[-1] if fixed else VisualBeat(part=p["n"], beat=0, sentence_start=i, sentence_end=i,
                                                              keywords=["empty room", "dim light", "window"], mood="eerie")
                    fixed.append(VisualBeat(part=p["n"], beat=len(fixed), sentence_start=i, sentence_end=i,
                                            keywords=prev.keywords, mood=prev.mood))
                    covered.add(i)
            fixed.sort(key=lambda b: b.sentence_start)
            beats_out += [b.model_dump() for b in fixed]
        ctx.write_json("visual_plan.json", beats_out)


for _st in (IdeasStage(), PlanStage(), DraftStage(), CriticReviseStage(), SplitStage(), HooksStage(), SeoStage(), VisualPlanStage()):
    register(_st)
    FORMAT_STAGES["shorts_only"].append(_st.name)
```

Note: `ns_studio/cli.py` (Task 13) must `import ns_studio.text.stages_shorts` and `ns_studio.media.stages_media` so registration happens.

- [ ] **Step 4: Run** → 5 passed. The `critic.json` fixture scores must be ≥ 8 average and ≥ 6 minimum; `revise.txt` must pass validators for a 50 s bucket (117 words ±20 % → 94–140 words), grade 5–8. Adjust the fixture text until `test_text_pipeline_runs_on_fixtures` passes; do not loosen the validators.

- [ ] **Step 5: Commit**

```bash
git add ns_studio/text/stages_shorts.py tests/test_text_stages.py tests/fixtures/llm
git commit -m "feat(text): shorts-only text stages ideas→visual plan with critic loop"
```

---

### Task 5: Exemplars (WP-D, `opus`)

**Files:**
- Create: `ns_studio/text/exemplars.py` (replace stub), `ns_studio/exemplars/<niche>/<slug>.md` ×12, `ns_studio/exemplars/<niche>/meta.yaml` ×4, `tests/test_exemplars.py`

**Interfaces:**
- Produces: `load_exemplars(niche: str) -> list[dict]` returning up to 3 `{"title", "text", "structure", "pov_tone", "why"}` for the best-matching niche folder (`horror`, `mystery`, `revenge`, `wholesome_twist`), matched by keyword in the niche string (`horror|scary|creepy` → horror; `mystery|detective|missing` → mystery; `revenge|payback|karma` → revenge; else `wholesome_twist`). `meta.yaml`: `{exemplars: [{file, title, structure, pov_tone, why}]}`.

- [ ] **Step 1: Write failing test**

```python
# tests/test_exemplars.py
from ns_common.textstats import fk_grade, word_count
from ns_studio.text.exemplars import EXEMPLARS_DIR, load_exemplars, niche_folder


def test_niche_mapping():
    assert niche_folder("original short horror fiction") == "horror"
    assert niche_folder("small-town mystery") == "mystery"
    assert niche_folder("petty revenge stories") == "revenge"
    assert niche_folder("heartwarming tales") == "wholesome_twist"


def test_each_niche_has_three_quality_exemplars():
    for folder in ("horror", "mystery", "revenge", "wholesome_twist"):
        ex = load_exemplars(folder)
        assert len(ex) == 3, folder
        for e in ex:
            assert 600 <= word_count(e["text"]) <= 900, (folder, e["title"], word_count(e["text"]))
            assert 4.0 <= fk_grade(e["text"]) <= 9.0, (folder, e["title"])
            assert e["structure"] and e["pov_tone"] and e["why"]
            low = e["text"].lower()
            assert "true story" not in low and "based on" not in low
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement the loader**

```python
# ns_studio/text/exemplars.py
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

EXEMPLARS_DIR = Path(__file__).resolve().parents[1] / "exemplars"
_MAP = [("horror", ("horror", "scary", "creepy", "haunt")), ("mystery", ("mystery", "detective", "missing", "whodunit")),
        ("revenge", ("revenge", "payback", "karma", "petty"))]


def niche_folder(niche: str) -> str:
    low = niche.lower()
    for folder, keys in _MAP:
        if any(k in low for k in keys):
            return folder
    return "wholesome_twist"


@lru_cache(maxsize=None)
def load_exemplars(niche: str) -> list[dict]:
    folder = EXEMPLARS_DIR / (niche if (EXEMPLARS_DIR / niche).is_dir() else niche_folder(niche))
    meta = yaml.safe_load((folder / "meta.yaml").read_text(encoding="utf-8"))
    out = []
    for e in meta["exemplars"][:3]:
        out.append({"title": e["title"], "text": (folder / e["file"]).read_text(encoding="utf-8").strip(),
                    "structure": e["structure"], "pov_tone": e["pov_tone"], "why": e["why"]})
    return out
```

- [ ] **Step 4: Write 12 original exemplars** (the `opus` worker writes them). Brief for each: 600–900 words, spoken rhythm, grade 5–8, first sentence is an honest hook, one clear turn, the last line echoes the first image, no real people/brands/events, implied not graphic harm, no stock phrases from `banned_phrases.txt`. Per niche, cover three different structures and at least two POV/tones from §17. `meta.yaml` lists `file, title, structure, pov_tone, why` (one sentence on the craft move). File names: `horror/doorbell.md, horror/laundromat.md, horror/night_bus.md`, `mystery/…`, `revenge/…`, `wholesome_twist/…`.

- [ ] **Step 5: Run** → 2 passed (fix word counts/grade until green).

- [ ] **Step 6: Commit**

```bash
git add ns_studio/text/exemplars.py ns_studio/exemplars tests/test_exemplars.py
git commit -m "feat(text): exemplar library with 12 original stories across 4 niches"
```

---
