"""P01: idea redraw after a critic failure, J3 regeneration for non-policy failures, QC rejects, E301 retry.

Spec 16.2 (max 3 ideas per job, else NS-E304), J3 step 2 (a failure goes to the rejected folder with a
reason and gets one regeneration), 7.7 (a Short dropped by QC is copied there), 15.12 (the NS-E301 hint
promises one retry).
"""

import json
from pathlib import Path

import httpx
import pytest

from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm, LlmClient
from ns_common.models import QcCheck, QcResult
from ns_studio import orchestrator as orch
from ns_studio.context import FIXTURES_LLM
from ns_studio.media import qc as qcmod
from ns_studio.media.qc import QcStage
from ns_studio.policy import regen
from ns_studio.stages import REGISTRY
from ns_studio.text import llm_retry
from ns_studio.text import stages_shorts as S
from ns_studio.text.prompts import call_json, call_text
from tests.helpers import make_ctx

RUBRIC = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")
BAD_CRITIC = {"scores": {k: 5 for k in RUBRIC}, "fixes": ["Fix it."]}
TEXT_PLAN = ["ideas", "plan", "draft", "critic_revise"]


def _good_critic() -> dict:
    return json.loads((FIXTURES_LLM / "critic.json").read_text(encoding="utf-8"))


def _fixture_ideas() -> list[dict]:
    return json.loads((FIXTURES_LLM / "ideas.json").read_text(encoding="utf-8"))["ideas"]


def _fixture_premises() -> list[str]:
    return [i["premise"] for i in _fixture_ideas()]


def _count(ctx, prompt: str) -> int:
    return len([c for c in ctx.services.llm.calls if c["prompt_name"] == prompt])


def _reasons(ctx) -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(ctx.paths.rejected.rglob("reason.json"))
    ]


# ---------------------------------------------------------------- idea redraw (F-H57, F-H99)
def test_three_ideas_fail_the_critic_then_the_job_ends_e304(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(orch.FORMAT_STAGES, "text_redraw", list(TEXT_PLAN))
    ctx = make_ctx(tmp_home, fmt="text_redraw")
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"critic": BAD_CRITIC})
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E304" and "3 ideas" in ei.value.detail
    failed = ctx.read_json("ideas_failed.json")
    premises = _fixture_premises()
    assert failed == premises[:3]  # three different ideas, drawn in order, none twice
    assert _count(ctx, "ideas") == 1  # the next idea comes from the batch already drawn
    assert _count(ctx, "critic") == 9  # 3 ideas x (1 critique + 2 revisions)
    assert ctx.db.one("select count(*) c from ideas where used=1")["c"] == 3
    assert len(_reasons(ctx)) == 3 and all(r["code"] == "NS-E304" for r in _reasons(ctx))
    # no infinite loop: a resumed job asks for no fourth idea
    with pytest.raises(NsError) as again:
        regen.run_job_guarded(ctx)
    assert again.value.code == "NS-E304"
    assert ctx.read_json("ideas_failed.json") == failed and _count(ctx, "ideas") == 1


def test_second_idea_passes_after_the_first_fails(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(orch.FORMAT_STAGES, "text_redraw", list(TEXT_PLAN))
    ctx = make_ctx(tmp_home, fmt="text_redraw")
    ctx.services.llm = FakeLlm(
        FIXTURES_LLM, overrides={"critic": [BAD_CRITIC, BAD_CRITIC, BAD_CRITIC, _good_critic()]}
    )
    ran = regen.run_job_guarded(ctx)
    assert ran == TEXT_PLAN
    premises = _fixture_premises()
    assert ctx.read_json("idea.json")["premise"] == premises[1]
    assert ctx.read_json("ideas_failed.json") == [premises[0]]
    assert ctx.db.one("select count(*) c from stories")["c"] == 1
    assert ctx.read_json("story.json")["critic_avg"] > 0
    assert len(_reasons(ctx)) == 1
    assert ctx.attempt == 0  # an idea redraw is not a policy regeneration


def test_the_idea_counter_survives_a_restart(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Two ideas failed, the process died: the resumed job has one idea left, not three."""
    monkeypatch.setitem(orch.FORMAT_STAGES, "text_redraw", list(TEXT_PLAN))
    ctx = make_ctx(tmp_home, fmt="text_redraw")
    premises = _fixture_premises()
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"critic": BAD_CRITIC})
    ctx.write_json("ideas_failed.json", premises[:2])
    ctx.write_json("ideas_all.json", _fixture_ideas())
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E304"
    assert ctx.read_json("ideas_failed.json") == premises[:3]
    assert _count(ctx, "critic") == 3  # only the third idea was tried


def test_critic_failure_in_a_master_redraws_the_idea_too(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """The master's `chapters_critic` follows the same rule (16.2): the stage that failed decides."""
    monkeypatch.setitem(orch.FORMAT_STAGES, "m_redraw", ["ideas", "chapters_critic"])
    calls: list[int] = []

    class FailingCritic:
        name, tenant, inputs, outputs = "chapters_critic", None, ("idea.json",), ("critic.json",)

        def run(self, ctx) -> None:
            calls.append(1)
            if len(calls) < 3:
                raise NsError("NS-E304", "chapter 1: critic avg 5.0, low 5, 2 revisions")
            ctx.write_json("critic.json", {"ok": True})

    monkeypatch.setitem(REGISTRY, "chapters_critic", FailingCritic())
    ctx = make_ctx(tmp_home, fmt="m_redraw")
    assert regen.run_job_guarded(ctx) == ["ideas", "chapters_critic"]
    assert len(calls) == 3 and len(ctx.read_json("ideas_failed.json")) == 2


def test_an_ideas_stage_e304_is_not_an_idea_redraw(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """ "All ideas too similar to history" comes from the ideas stage itself: it gets the one regeneration."""
    counts: dict[str, int] = {}
    _plan(monkeypatch, "ideas_dup", ["ideas", "draft"], counts, fail_stage="ideas", code="NS-E304", times=5)
    ctx = make_ctx(tmp_home, fmt="ideas_dup")
    with pytest.raises(NsError):
        regen.run_job_guarded(ctx)
    assert counts["ideas"] == 2 and not ctx.file("ideas_failed.json").exists()


def test_an_unrecorded_premise_cannot_make_the_redraw_loop_endless(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """No readable idea.json (or a repeated premise) leaves ideas_failed.json at the same length: the
    redraws themselves are counted in policy_attempts.json, so three redraws still end in NS-E304."""
    monkeypatch.setitem(orch.FORMAT_STAGES, "text_redraw", list(TEXT_PLAN))
    ctx = make_ctx(tmp_home, fmt="text_redraw")
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"critic": BAD_CRITIC})
    seen: list[int] = []

    def never_recorded(c) -> int:
        seen.append(1)
        assert len(seen) < 10, "the redraw loop did not stop"
        return 0

    monkeypatch.setattr(regen, "record_failed_idea", never_recorded)
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E304" and len(seen) == 3
    att = json.loads(ctx.file(regen.ATTEMPTS_FILE).read_text(encoding="utf-8"))
    assert len([h for h in att["history"] if "idea" in h]) == 3


def test_a_corrupt_attempts_file_does_not_hide_the_failure(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {}
    _plan(monkeypatch, "j3", J3_PLAN, counts, fail_stage="gate", code="NS-E403", times=1)
    ctx = make_ctx(tmp_home, fmt="j3")
    ctx.file(regen.ATTEMPTS_FILE).write_text('{"regenerations": 0, "hist', encoding="utf-8")
    assert regen.run_job_guarded(ctx) == J3_PLAN  # one regeneration, not a JSONDecodeError
    assert counts["gate"] == 2


class _Idea:
    """Stands for `ideas`: writes the idea of every pass (`payload(n)` for pass n) into idea.json."""

    name, tenant, inputs, outputs = "ideas", None, (), ("idea.json",)

    def __init__(self, payload) -> None:
        self.payload, self.passes = payload, 0

    def run(self, ctx) -> None:
        self.passes += 1
        ctx.write_json("idea.json", self.payload(self.passes))


class _AlwaysFailsCritic:
    name, tenant, inputs, outputs = "critic_revise", None, ("idea.json",), ("critic.json",)

    def __init__(self) -> None:
        self.runs = 0

    def run(self, ctx) -> None:
        self.runs += 1
        assert self.runs < 10, "the redraw loop did not stop"
        raise NsError("NS-E304", "critic avg 5.0 low 5 after 2 revisions")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(lambda n: {"twist": f"twist {n}"}, id="no-premise-key"),
        pytest.param(lambda n: {"premise": "   ", "twist": f"twist {n}"}, id="blank-premise"),
        pytest.param(lambda n: {"premise": "The same premise", "twist": f"twist {n}"}, id="shared-premise"),
    ],
)
def test_max_ideas_is_a_hard_bound_whatever_the_premise_looks_like(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, payload
):
    """The real counter: an idea without a premise, or banked ideas that share one, still end at three."""
    ideas, critic = _Idea(payload), _AlwaysFailsCritic()
    monkeypatch.setitem(REGISTRY, "ideas", ideas)
    monkeypatch.setitem(REGISTRY, "critic_revise", critic)
    monkeypatch.setitem(orch.FORMAT_STAGES, "bound", ["ideas", "critic_revise"])
    ctx = make_ctx(tmp_home, fmt="bound")
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E304" and "3 ideas" in ei.value.detail
    assert (ideas.passes, critic.runs) == (3, 3)
    att = json.loads(ctx.file(regen.ATTEMPTS_FILE).read_text(encoding="utf-8"))
    assert len([h for h in att["history"] if "idea" in h]) == 3
    assert ctx.generation == 3  # each redraw is a new pass for the cached drafts
    if "premise" not in payload(1) or not payload(1)["premise"].strip():
        assert len(ctx.read_json("ideas_failed.json")) == 3  # the list counts them too, under a placeholder


def test_a_job_folder_from_before_the_redraw_still_loads_and_redraws(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """policy_attempts.json with a history entry that has no `idea` key, no ideas_failed.json."""
    monkeypatch.setitem(orch.FORMAT_STAGES, "text_redraw", list(TEXT_PLAN))
    ctx = make_ctx(tmp_home, fmt="text_redraw")
    old = {"regenerations": 1, "history": [{"code": "NS-E606", "detail": "tag", "rejected_to": "somewhere"}]}
    ctx.write_json(regen.ATTEMPTS_FILE, old)
    assert not ctx.file("ideas_failed.json").exists()
    assert (ctx.attempt, ctx.generation) == (1, 1)
    ctx.services.llm = FakeLlm(
        FIXTURES_LLM, overrides={"critic": [BAD_CRITIC, BAD_CRITIC, BAD_CRITIC, _good_critic()]}
    )
    assert regen.run_job_guarded(ctx) == TEXT_PLAN
    att = ctx.read_json(regen.ATTEMPTS_FILE)
    assert att["regenerations"] == 1 and [bool("idea" in h) for h in att["history"]] == [False, True]
    assert ctx.read_json("ideas_failed.json") == [_fixture_premises()[0]]
    assert ctx.generation == 2


# ---------------------------------------------------------------- J3 step 2 (F-H77)
class Counter:
    def __init__(self, name: str, counts: dict[str, int], fail=None) -> None:
        self.name, self.tenant, self.inputs, self.outputs = name, None, (), (f"{name}.json",)
        self.counts, self.fail = counts, fail

    def run(self, ctx) -> None:
        n = self.counts.get(self.name, 0)
        self.counts[self.name] = n + 1
        if self.fail and n < self.fail[1]:
            raise NsError(self.fail[0], self.fail[2])
        ctx.write_json(f"{self.name}.json", {"n": n + 1})


def _plan(
    monkeypatch,
    fmt: str,
    names: list[str],
    counts,
    *,
    fail_stage: str,
    code: str,
    times: int,
    detail="seeded",
):
    for n in names:
        monkeypatch.setitem(
            REGISTRY, n, Counter(n, counts, (code, times, detail) if n == fail_stage else None)
        )
    monkeypatch.setitem(orch.FORMAT_STAGES, fmt, names)


J3_PLAN = ["ideas", "draft", "chapters_draft", "voice", "captions", "clips", "render", "gate"]


@pytest.mark.parametrize(
    ("code", "detail", "restart"),
    [
        ("NS-E303", "length: 12 words", "draft"),
        ("NS-E305", "narration is 8:12", "chapters_draft"),
        ("NS-E402", "beat 3: no clip", "clips"),
        ("NS-E403", "drift 400 ms", "voice"),
        ("NS-E404", "s1: caption_drift_ms", "voice"),
        ("NS-E404", "s1: black_frames", "clips"),
        ("NS-E404", "long: aspect", "render"),
        ("NS-E404", "s1: aspect, black_frames", "clips"),
    ],
)
def test_non_policy_failures_get_one_regeneration_from_the_earliest_stage(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, code: str, detail: str, restart: str
):
    counts: dict[str, int] = {}
    _plan(monkeypatch, "j3", J3_PLAN, counts, fail_stage="gate", code=code, times=1, detail=detail)
    ctx = make_ctx(tmp_home, fmt="j3")
    ctx.file("shorts").mkdir()
    ctx.file("shorts/s1.mp4").write_bytes(b"video")
    assert regen.run_job_guarded(ctx) == J3_PLAN
    first_redone = J3_PLAN.index(restart)
    for i, n in enumerate(J3_PLAN):
        assert counts[n] == (2 if i >= first_redone else 1), (n, counts)
    reasons = _reasons(ctx)
    assert len(reasons) == 1 and reasons[0]["code"] == code and reasons[0]["detail"] == detail
    assert list(ctx.paths.rejected.rglob("s1.mp4"))  # the rendered media went with it
    att = json.loads(ctx.file(regen.ATTEMPTS_FILE).read_text(encoding="utf-8"))
    assert att["regenerations"] == 1


@pytest.mark.parametrize("code", ["NS-E303", "NS-E305", "NS-E402", "NS-E403", "NS-E404"])
def test_a_second_failure_ends_the_job(tmp_home: Path, monkeypatch: pytest.MonkeyPatch, code: str):
    counts: dict[str, int] = {}
    _plan(monkeypatch, "j3", J3_PLAN, counts, fail_stage="gate", code=code, times=9, detail="s1: duration")
    ctx = make_ctx(tmp_home, fmt="j3")
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == code
    assert counts["gate"] == 2  # one regeneration, never a third pass
    assert len(_reasons(ctx)) == 2
    with pytest.raises(NsError):  # a resumed job does not get a second regeneration
        regen.run_job_guarded(ctx)
    assert counts["gate"] == 3


@pytest.mark.parametrize("code", ["NS-E303", "NS-E305"])
def test_text_failures_of_a_story_the_owner_wrote_stay_final(tmp_home: Path, monkeypatch, code: str):
    """The owner's script has no stage that writes the story: redoing the import would fail the same way."""
    counts: dict[str, int] = {}
    _plan(
        monkeypatch,
        "owner_j3",
        ["script_import_long", "voice", "gate"],
        counts,
        fail_stage="gate",
        code=code,
        times=1,
    )
    ctx = make_ctx(tmp_home, fmt="owner_j3")
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == code and counts == {"script_import_long": 1, "voice": 1, "gate": 1}
    assert not list(ctx.paths.rejected.rglob("reason.json"))


def test_qc_failure_gives_one_rejected_entry_and_one_regeneration(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The real QC stage fails the only Short once: its video is in the rejected folder."""
    counts: dict[str, int] = {}
    for n in ("voice", "captions"):
        monkeypatch.setitem(REGISTRY, n, Counter(n, counts))

    class Render:
        name, tenant, inputs, outputs = "render", None, (), ("render/report.json",)

        def run(self, ctx) -> None:
            counts["render"] = counts.get("render", 0) + 1
            video = ctx.subdir("shorts/s1") / "short.mp4"
            video.write_bytes(b"rendered video " + str(counts["render"]).encode())
            ctx.write_json("render/report.json", {"s1": {"path": str(video), "duration_s": 3.0}})
            ctx.write_json("captions/report.json", {"s1": {"drift_ms": 10.0, "first_caption_s": 0.0}})

    monkeypatch.setitem(REGISTRY, "render", Render())
    monkeypatch.setitem(REGISTRY, "qc", QcStage())
    monkeypatch.setitem(orch.FORMAT_STAGES, "qc_j3", ["voice", "captions", "render", "qc"])
    verdicts = iter([False, True])

    def fake_run_qc(item, video, kind, **kw):
        assert video.exists()
        ok = next(verdicts)
        check = QcCheck(passed=ok, value=1, limit="x")
        return QcResult(item=item, passed=ok, checks={"duration": check}, reason=None if ok else "duration")

    monkeypatch.setattr(qcmod, "run_qc", fake_run_qc)
    ctx = make_ctx(tmp_home, fmt="qc_j3")
    assert regen.run_job_guarded(ctx) == ["voice", "captions", "render", "qc"]
    assert counts == {"voice": 2, "captions": 2, "render": 2}
    rejected = list(ctx.paths.rejected.rglob("short.mp4"))
    assert len(rejected) == 1 and rejected[0].read_bytes() == b"rendered video 1"
    reasons = _reasons(ctx)
    assert len(reasons) == 1 and reasons[0]["code"] == "NS-E404" and "duration" in reasons[0]["detail"]
    assert ctx.read_json("qc/report.json") == {"s1": {"passed": True, "reason": None}}
    assert not ctx.file("qc/dropped.json").exists()
    assert (
        ctx.paths.rejected_dir(ctx.channel.slug, reasons[0]["ts"][:10], ctx.job_id) / "attempt1/qc/s1.json"
    ).exists()


def test_earliest_stage_reads_the_failed_qc_checks():
    names = ["ideas", "voice", "clips", "render_master", "qc"]
    assert regen.earliest_stage("NS-E404", names, "long: file_opens") == "render_master"
    assert regen.earliest_stage("NS-E404", names, "long: aspect, loudness") == "voice"
    assert regen.earliest_stage("NS-E404", names, "s2: black_frames") == "clips"
    assert regen.earliest_stage("NS-E404", names, "something unexpected") == "voice"
    assert regen.earliest_stage("NS-E999", ["first", "second"]) == "first"


TOO_LONG = "s2: 71 s exceeds the 60 s Shorts limit"  # raised by media/derive.py while a Short is cut


def test_a_derived_short_over_60_s_restarts_at_the_selection_not_at_the_voice():
    """A new voice take cannot shorten the span the selection chose; only a new selection can."""
    master = list(orch.MASTER_FIRST_STAGES)
    assert regen.earliest_stage("NS-E404", master, TOO_LONG) == "shorts_select"
    assert (
        regen.earliest_stage("NS-E404", list(orch.MASTER_FROM_OWNER_SCRIPT_STAGES), TOO_LONG)
        == "shorts_select"
    )
    assert (
        regen.earliest_stage("NS-E404", master, "s2: duration") == "voice"
    )  # a QC duration check is unchanged
    series_part = list(
        orch.MASTER_FROM_SCRIPT_STAGES
    )  # its Shorts come with the script: nothing can change them
    assert not regen.has_restart_stage("NS-E404", series_part, TOO_LONG)
    assert regen.has_restart_stage("NS-E404", series_part, "s2: aspect")


def test_a_derived_short_over_60_s_gets_one_regeneration_from_the_selection(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    counts: dict[str, int] = {}
    names = ["ideas", "shorts_select", "voice", "shorts_derive"]
    _plan(
        monkeypatch,
        "derive_j3",
        names,
        counts,
        fail_stage="shorts_derive",
        code="NS-E404",
        times=1,
        detail=TOO_LONG,
    )
    assert regen.run_job_guarded(make_ctx(tmp_home, fmt="derive_j3")) == names
    assert counts == {"ideas": 1, "shorts_select": 2, "voice": 2, "shorts_derive": 2}


def test_a_derived_short_over_60_s_in_a_plan_without_a_selection_fails_at_once(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    counts: dict[str, int] = {}
    names = ["script_import", "voice", "shorts_derive"]
    _plan(
        monkeypatch,
        "series_j3",
        names,
        counts,
        fail_stage="shorts_derive",
        code="NS-E404",
        times=1,
        detail=TOO_LONG,
    )
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(make_ctx(tmp_home, fmt="series_j3"))
    assert ei.value.code == "NS-E404" and counts == {"script_import": 1, "voice": 1, "shorts_derive": 1}


# ---------------------------------------------------------------- QC rejects (F-H70)
def _two_shorts(ctx, bad: set[str]):
    report = {}
    for item in ("s1", "s2"):
        video = ctx.subdir(f"shorts/{item}") / "short.mp4"
        video.write_bytes(f"video {item}".encode())
        report[item] = {"path": str(video), "duration_s": 3.0}
    ctx.write_json("render/report.json", report)
    ctx.write_json("captions/report.json", {i: {"drift_ms": 10.0, "first_caption_s": 0.0} for i in report})

    def fake_run_qc(item, video, kind, **kw):
        ok = item not in bad
        check = QcCheck(passed=ok, value=1, limit="x")
        return QcResult(
            item=item,
            passed=ok,
            checks={"caption_drift_ms": check},
            reason=None if ok else "caption_drift_ms",
        )

    return fake_run_qc


def test_a_short_dropped_by_qc_is_copied_to_rejected_with_a_reason(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = make_ctx(tmp_home)
    monkeypatch.setattr(qcmod, "run_qc", _two_shorts(ctx, {"s2"}))
    QcStage().run(ctx)  # no NS-E404: s1 is fine
    # the layout of section 13 stays: one file per item plus the report, and the dropped list
    assert ctx.read_json("qc/report.json")["s2"]["passed"] is False
    assert ctx.file("qc/s1.json").exists() and ctx.file("qc/s2.json").exists()
    assert list(ctx.read_json("render/report.json")) == ["s1"]
    assert ctx.read_json("qc/dropped.json") == {"s2": "caption_drift_ms"}
    reasons = [p for p in ctx.paths.rejected.rglob("reason.json")]
    assert len(reasons) == 1
    folder = reasons[0].parent
    assert folder.parts[-2:] == ("dropped", "s2")
    reason = json.loads(reasons[0].read_text(encoding="utf-8"))
    assert reason["code"] == "NS-W406" and reason["detail"] == "caption_drift_ms" and reason["item"] == "s2"
    assert (folder / "short.mp4").read_bytes() == b"video s2"  # a copy: the job keeps its own file
    assert ctx.file("shorts/s2/short.mp4").exists()
    assert json.loads((folder / "qc.json").read_text(encoding="utf-8"))["passed"] is False


def test_a_failing_evidence_copy_does_not_stop_the_qc_stage(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """The copy to rejected is evidence only: an OSError there must not fail the publish path."""
    ctx = make_ctx(tmp_home)
    monkeypatch.setattr(qcmod, "run_qc", _two_shorts(ctx, {"s2"}))

    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(qcmod.shutil, "copy2", disk_full)
    QcStage().run(ctx)  # no OSError
    assert list(ctx.read_json("render/report.json")) == ["s1"]
    assert ctx.read_json("qc/dropped.json") == {"s2": "caption_drift_ms"}


def test_the_dropped_list_is_kept_until_qc_runs_again_and_drops_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """NS-W406 points at qc/dropped.json: a rejection that does not rerun QC (metadata only) must keep it."""
    ctx = make_ctx(tmp_home)
    monkeypatch.setattr(qcmod, "run_qc", _two_shorts(ctx, {"s2"}))
    QcStage().run(ctx)
    err = NsError("NS-E606", "a disliked tag")
    regen.reject_artifacts(ctx, err, 1)
    assert ctx.read_json("qc/dropped.json") == {"s2": "caption_drift_ms"}
    monkeypatch.setattr(qcmod, "run_qc", _two_shorts(ctx, set()))
    QcStage().run(ctx)  # the next pass drops nothing: the old list would be wrong now
    assert not ctx.file("qc/dropped.json").exists()


def test_a_passing_short_is_never_copied_to_rejected(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = make_ctx(tmp_home)
    monkeypatch.setattr(qcmod, "run_qc", _two_shorts(ctx, set()))
    QcStage().run(ctx)
    assert not ctx.paths.rejected.exists() or not list(ctx.paths.rejected.rglob("*"))


# ---------------------------------------------------------------- NS-E301 retry (F-H43)
@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(llm_retry, "_sleep", slept.append)
    return slept


def _llm_over(handler) -> LlmClient:
    return LlmClient("http://llm.test", "story", client=httpx.Client(transport=httpx.MockTransport(handler)))


def _chat(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_a_5xx_is_retried_once_after_a_short_wait(tmp_home: Path, waits: list[float]):
    ctx = make_ctx(tmp_home)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(503, text="loading model") if len(seen) == 1 else _chat('{"ideas": []}')

    ctx.services.llm = _llm_over(handler)
    out = call_json(ctx, "ideas", n=8, budget=S.ideas_budget(ctx), avoid_premises=[])
    assert out == {"ideas": []} and len(seen) == 2
    assert waits == [llm_retry.RETRY_WAIT_S] and 0 < llm_retry.RETRY_WAIT_S <= 10


def test_an_unusable_answer_is_retried_once(tmp_home: Path, waits: list[float]):
    ctx = make_ctx(tmp_home)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(200, json={"choices": []}) if len(seen) == 1 else _chat("A quiet house.")

    ctx.services.llm = _llm_over(handler)
    assert call_text(ctx, "revise", story="x", fixes=["y"]) == "A quiet house."
    assert len(seen) == 2


def test_a_second_5xx_ends_as_e301_and_is_not_retried_again(tmp_home: Path, waits: list[float]):
    ctx = make_ctx(tmp_home)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(500, text="boom")

    ctx.services.llm = _llm_over(handler)
    with pytest.raises(NsError) as ei:
        call_text(ctx, "revise", story="x", fixes=["y"])
    assert ei.value.code == "NS-E301" and len(seen) == 2 and len(waits) == 1


@pytest.mark.parametrize("status", [400, 404])
def test_a_client_error_is_not_retried(tmp_home: Path, waits: list[float], status: int):
    ctx = make_ctx(tmp_home)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(status)

    ctx.services.llm = _llm_over(handler)
    with pytest.raises(NsError):
        call_text(ctx, "revise", story="x", fixes=["y"])
    assert len(seen) == 1 and not waits


def test_a_timeout_is_not_retried(tmp_home: Path, waits: list[float]):
    """A 600 s timeout retried would hold the GPU for twenty minutes before the job could fail."""
    ctx = make_ctx(tmp_home)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    ctx.services.llm = _llm_over(handler)
    with pytest.raises(NsError) as ei:
        call_json(ctx, "ideas", n=8, budget=S.ideas_budget(ctx), avoid_premises=[])
    assert ei.value.code == "NS-E301" and len(seen) == 1 and not waits
