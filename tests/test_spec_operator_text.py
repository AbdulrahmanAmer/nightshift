"""P41: the operator-facing text (README.md, OPERATOR.md, BUILD_SPEC.md J2, 8, 16.3, 18.6, 18.11, 13) says
what the code does.

Boxes: F-H78 F-H79 F-H100 F-H119 F-H167 of docs/FEATURE_STATUS.md, plus the governor extras (F-H112) and the
derived-Shorts pair rule (F-H82) that the package brief asked to be recorded. Each test reads the text and
the code (a constant, a source line, or the command itself) so a change on either side that makes them
disagree fails here. Nothing here ticks a box.
"""

from __future__ import annotations

import inspect
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from installer.stage2 import sysinfo
from ns_common.db import Db
from ns_common.models import Pools
from ns_common.paths import Paths
from ns_dashboard import svc_engine, svc_jobs
from ns_studio import review
from ns_studio.channels import cadence, crud
from ns_studio.cli import app
from ns_studio.text import validators
from ns_studio.variation import draw
from tests.helpers import add_output, make_channel

ROOT = Path(__file__).resolve().parents[1]
SPEC = (ROOT / "BUILD_SPEC.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
GUIDE = (ROOT / "OPERATOR.md").read_text(encoding="utf-8")
STATUS = (ROOT / "docs" / "FEATURE_STATUS.md").read_text(encoding="utf-8")
UI = ROOT / "ns_dashboard" / "ui" / "src"
runner = CliRunner()
HEADING = "## If YouTube sends a warning or strike"


def spec_section(prefix: str) -> str:
    """The spec section whose heading starts with ``prefix``, up to the next heading of its level or above."""
    lines = SPEC.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(prefix))
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    out = [lines[start]]
    for ln in lines[start + 1 :]:
        if ln.startswith("#") and len(ln) - len(ln.lstrip("#")) <= level:
            break
        out.append(ln)
    return "\n".join(out)


def doc_section(text: str, heading: str) -> str:
    """A ``## `` section of README.md or OPERATOR.md."""
    assert heading in text, heading
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def decision(marker: str) -> str:
    """The section 13 row whose question cell contains ``marker`` (exactly one)."""
    rows = [ln for ln in spec_section("## 13. Decisions").splitlines() if re.match(r"^\| D\d+ \|", ln)]
    hits = [ln for ln in rows if marker in ln.split("|")[2]]
    assert len(hits) == 1, f"section 13 needs exactly one row about {marker!r}, found {len(hits)}"
    return hits[0]


def decision_id(marker: str) -> str:
    return decision(marker).split("|")[1].strip()


def cites(text: str, ident: str) -> bool:
    return re.search(rf"(?<!\w){ident}(?!\w)", text) is not None


def box(box_id: str) -> str:
    return next(ln for ln in STATUS.splitlines() if ln.startswith((f"- [ ] {box_id} ", f"- [x] {box_id} ")))


def audit_row(feature_start: str) -> list[str]:
    statuses = {"built", "partial", "missing", "spec_dropped"}
    for ln in STATUS.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) >= 7 and cells[0] in statuses and cells[1].startswith(feature_start):
            return cells
    raise AssertionError(f"no audit row starts with {feature_start!r}")


def tsx(name: str) -> str:
    return (UI / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------
# F-H119, F-H167: the incident procedure is printed in README.md and OPERATOR.md
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["README.md", "OPERATOR.md"])
def test_incident_section_has_the_four_steps_in_order(name):
    text = README if name == "README.md" else GUIDE
    body = doc_section(text, HEADING)
    steps = {
        n: re.search(rf"^{n}\. .*?(?=^\d\. |\Z)", body, re.S | re.M).group(0)  # type: ignore[union-attr]
        for n in (1, 2, 3, 4)
    }
    assert "studio pause" in steps[1] and "--reason" in steps[1]
    assert "last 10" in steps[2] and "18.2" in steps[2] and "18.4" in steps[2]
    assert "critic_bar" in steps[3] and "pools" in steps[3]
    assert "studio resume" in steps[4]


def test_operator_guide_gives_the_full_path_of_the_studio_program():
    """A terminal has no `studio` on its path: the guide names the exe the launchers run."""
    common = (ROOT / "launchers" / "_common.ps1").read_text(encoding="utf-8")
    studio_launcher = (ROOT / "launchers" / "nightshift-studio.ps1").read_text(encoding="utf-8")
    assert "('venvs\\' + $Venv + '\\Scripts\\' + $Name)" in common
    assert "Get-NsVenvExe 'studio' 'studio.exe'" in studio_launcher
    body = doc_section(GUIDE, HEADING)
    assert "venvs\\studio\\Scripts\\studio.exe" in body


def test_operator_guide_says_the_studio_shortcut_cannot_take_a_notice_with_spaces():
    """The shortcut splits what is typed at every space: `--reason "two words"` arrives as four arguments."""
    launcher = (ROOT / "launchers" / "nightshift-studio.ps1").read_text(encoding="utf-8")
    assert "-split '\\s+'" in launcher
    body = doc_section(GUIDE, HEADING)
    assert "Nightshift Studio" in body and "space" in body


def _env(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    crud.add_channel(paths, db, make_channel())
    return paths, db


def test_the_commands_of_the_section_do_what_it_says(tmp_home: Path):
    paths, db = _env(tmp_home)
    for i in range(12):  # twelve outputs; the list shows the last ten, newest first
        add_output(
            db,
            f"after-dark-2026110{i % 9 + 1}-a{i:03d}",
            "s1",
            created_at=f"2026-10-{10 + i:02d}T10:00:00+00:00",
        )
    before = cadence.plan_daily(paths, db, datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
    assert [j.channel.slug for j in before] == ["after-dark"]

    # step 1: a notice text is required, the channel is paused, `studio status` shows the incident
    assert "NS-E326" in runner.invoke(app, ["pause", "after-dark"]).output
    assert (
        runner.invoke(app, ["pause", "after-dark", "--reason", "Community Guidelines warning"]).exit_code == 0
    )
    assert crud.get_channel(paths, "after-dark").status == "paused"
    assert "Community Guidelines warning" in runner.invoke(app, ["status"]).output
    assert cadence.plan_daily(paths, db, datetime(2026, 11, 1, 12, 0, tzinfo=UTC)) == []

    # step 2: `studio resume` lists the last 10 outputs and the 18.2-18.4 reminder; "n" leaves it paused
    listed = runner.invoke(app, ["resume", "after-dark"], input="n\n")
    assert listed.exit_code == 1 and crud.get_channel(paths, "after-dark").status == "paused"
    assert len(re.findall(r"^after-dark-\d+-a\d{3}/s1\t", listed.output, re.M)) == 10
    assert "18.2-18.4" in listed.output and "critic_bar" in listed.output and "pools" in listed.output
    assert inspect.signature(review.last_outputs).parameters["n"].default == 10

    # step 3: critic_bar is edited with `studio channel edit <id> --set critic_bar=...`
    assert runner.invoke(app, ["channel", "edit", "after-dark", "--set", "critic_bar=8.5"]).exit_code == 0
    assert crud.get_channel(paths, "after-dark").critic_bar == 8.5

    # step 4: resume
    assert runner.invoke(app, ["resume", "after-dark", "--yes"]).exit_code == 0
    assert crud.get_channel(paths, "after-dark").status == "active"


def test_the_dashboard_switch_sends_no_note_and_the_assistant_can():
    """The section must not promise a note field the Channels page does not have."""
    chans = tsx("pages/Channels.tsx")
    call = re.search(
        r'api\.post\(`/channels/\$\{encodeURIComponent\(c\.slug\)\}/\$\{on \? "resume" : "pause"\}`(.*?)\)',
        chans,
    )
    assert call and call.group(1) == "", (
        "the Channels switch sends a body now: say so in the incident section"
    )
    server = (ROOT / "ns_dashboard" / "server.py").read_text(encoding="utf-8")
    assert 'dash.channels.pause, slug, body.get("note")' in server
    tools = (ROOT / "ns_dashboard" / "assistant" / "tools.py").read_text(encoding="utf-8")
    assert re.search(r'_t\("pause_channel".*?"note": _s\("why"\)', tools, re.S)
    body = " ".join(doc_section(GUIDE, HEADING).split())  # the guide wraps its lines
    assert "records no notice text" in body
    assert "Assistant" in body


def test_resume_in_the_dashboard_shows_no_review_and_the_section_says_so():
    """`studio resume` is where the last 10 outputs are listed; the Channels switch resumes at once."""
    cli = (ROOT / "ns_studio" / "cli.py").read_text(encoding="utf-8")
    assert "Check these against spec sections 18.2-18.4; raise critic_bar or trim pools if needed." in cli
    body = " ".join(doc_section(GUIDE, HEADING).split())  # the guide wraps its lines
    assert "resumes at once" in body


def test_only_story_runs_are_refused_for_a_paused_channel_and_the_texts_say_so():
    cls = svc_jobs.JobsService
    assert "_require_not_paused" in inspect.getsource(cls._run_params)
    assert "channel_paused" in inspect.getsource(cls._require_not_paused)
    assert "channel_paused" not in inspect.getsource(cls._clip_params)
    # one answer for a new run and for Retry (P47)
    assert inspect.getsource(svc_jobs).count("channel_paused") == 1
    section = doc_section(GUIDE, HEADING)
    flat = " ".join(section.split())
    assert "refuses to start a video for it" not in flat
    assert "refuses to start a story video for it" in flat
    assert "Clipper" in flat and "does not check" in flat
    d = decision("Incident procedure")
    assert "A story run request" in d and "`_clip_params`" in d and "Clipper" in d
    assert "A run request for a paused channel" not in d
    assert "refuses to start" not in doc_section(README, HEADING)


# ---------------------------------------------------------------------------------------------------------
# OPERATOR.md leftovers of the P37 break-it round
# ---------------------------------------------------------------------------------------------------------
def test_operator_guide_has_no_two_key_or_75_gb_leftovers():
    for stale in (
        "Two free keys",
        "the two stock footage keys",
        "paste the two",
        "About 75 GB",
        "about 75 GB",
    ):
        assert stale not in GUIDE, stale
    need = GUIDE.split("## What you need", 1)[1].split("## Install", 1)[0]
    for tier in ("small", "balanced", "best"):
        assert f"{sysinfo.required_free_gb(tier)} GB" in need
    assert "Your Pexels and Pixabay keys" not in GUIDE


# ---------------------------------------------------------------------------------------------------------
# F-H78, F-H79: J2 wording
# ---------------------------------------------------------------------------------------------------------
STAGE_KEYS = {
    "draft": "Writing the story",
    "voice": "Recording narration",
    "captions": "Adding captions",
    "clips": "Finding footage",
    "render": "Rendering",
    "qc": "Checking quality",
}


def test_j2_wording_names_the_queue_the_buttons_and_the_stage_words():
    j2 = spec_section("### J2")
    for gone in ("Run now", "Overview", "Loading LLM", "browser opens"):
        assert gone not in j2, gone
    assert "**Queue**" in j2 and "Full video + Shorts" in j2 and "One Short" in j2
    for word in STAGE_KEYS.values():
        assert f"`{word}`" in j2, word
    assert cites(j2, decision_id("Dashboard home"))


def test_j2_words_are_the_words_of_the_pages():
    fmt = tsx("lib/format.ts")
    words = dict(
        re.findall(r'\b(draft|voice|captions|clips|render|qc): "([^"]+)"', fmt.split("STAGE_WORDS", 1)[1])
    )
    assert {k: words[k] for k in STAGE_KEYS} == STAGE_KEYS
    create = tsx("pages/Create.tsx")
    assert 'label: "Full video + Shorts"' in create and 'label: "One Short"' in create
    assert 'label: "A new story video"' in create
    assert "Run now" not in create
    app_ts = tsx("lib/app.tsx")
    assert 'ROUTES[n] ?? "queue"' in app_ts
    shell = tsx("shell/Shell.tsx")
    for text in ('"Idle"', "Loading model", "Working: ", '"Ready"'):
        assert text in shell, text
    adapters = (ROOT / "ns_dashboard" / "adapters.py").read_text(encoding="utf-8")
    assert (
        'f"{stage} ({index + 1}/{total})"' in adapters
    )  # the progress message the bar shows after `Working: `
    engine = (ROOT / "ns_dashboard" / "svc_engine.py").read_text(encoding="utf-8")
    assert '"Services idle"' in engine
    queue = tsx("pages/Queue.tsx")
    assert "stageWords(j.stage)" in queue


def test_top_bar_step_count_is_not_in_the_spec_because_the_engine_never_sends_it():
    # Shell.tsx adds ", step N of M" only when the engine object carries step and steps ...
    shell = tsx("shell/Shell.tsx")
    assert "e.step && e.steps" in shell and ", step ${e.step} of ${e.steps}" in shell
    # ... and the engine object the API serves never carries them (only the demo data does).
    engine_src = inspect.getsource(svc_engine.EngineService.engine)
    returned = engine_src.split("return {", 1)[1].split("}", 1)[0]
    assert '"step"' not in returned and '"steps"' not in returned
    # so the spec must not promise it in the top bar; the job card on Create carries it
    d34 = decision("Dashboard home")
    top_bar = d34.split("The top bar reads", 1)[1].split("There is no", 1)[0]
    assert "Working: <stage> (i/n)" in top_bar
    assert "step i of n" not in top_bar
    j2_step2 = next(ln for ln in spec_section("### J2").splitlines() if ln.startswith("2. "))
    bar_text = j2_step2.split("The top bar reads", 1)[1].split("; the job", 1)[0]
    assert "Working: <stage> (i/n)" in bar_text and "step i of n" not in bar_text
    # the card text is stated separately, and the Create card does show it
    for text in (d34, j2_step2):
        assert "step i of n" in text.split("card", 1)[1]
    create = tsx("pages/Create.tsx")
    assert "`, step ${j.step} of ${j.steps}`" in create


def test_j2_window_is_the_app_window_not_a_browser_tab():
    launcher = (ROOT / "launchers" / "nightshift-dashboard.ps1").read_text(encoding="utf-8")
    assert "nightshift-host.ps1" in launcher and "WebView2" in launcher
    assert "an app window (no tabs or address bar) in Edge or Chrome" in launcher
    j2 = spec_section("### J2")
    assert "own" in j2 and "window" in j2 and "WebView2" in j2
    for text in (spec_section("### A.4"), README):
        assert "opens in your browser" not in text


def test_operator_first_video_steps_use_the_dashboard_words():
    steps = doc_section(GUIDE, "## Make your first video")
    for gone in ("Run now", "shorts_only", "master_first", "voicing", "Mark uploaded"):
        assert gone not in steps, gone
    for word in (
        "Create",
        "One Short",
        "Full video + Shorts",
        "Queue",
        "Approve",
        "Reject",
        *STAGE_KEYS.values(),
    ):
        assert word in steps, word
    assert "arrives in a later release" not in steps


def test_setup_summary_still_says_run_now_and_the_spec_names_it_as_open():
    summary = (ROOT / "installer" / "stage2" / "steps" / "s10_summary.py").read_text(encoding="utf-8")
    assert "press Run now to make your first video" in summary
    d34 = decision("Dashboard home")
    assert "installer/stage2/steps/s10_summary.py" in d34 and "J1 step 7" in d34
    assert "still says" in d34.split("s10_summary.py", 1)[1]
    step7 = next(ln for ln in spec_section("### J1").splitlines() if ln.startswith("7. "))
    assert "Run now" in step7 and cites(step7, decision_id("Dashboard home"))


# ---------------------------------------------------------------------------------------------------------
# F-H100: 16.3 opening-line similarity
# ---------------------------------------------------------------------------------------------------------
def test_validator_line_states_the_threshold_the_code_uses():
    line = next(ln for ln in spec_section("### 16.3").splitlines() if "opening line" in ln)
    default = inspect.signature(validators.check_opening_similarity).parameters["threshold"].default
    assert default == 0.9
    assert f"≥ {default}" in line and "≥ 0.8" not in line
    assert "last 50" in line and "dev" in line
    assert cites(line, decision_id("Opening-line similarity"))


def test_opening_similarity_is_skipped_in_dev_and_reads_the_last_50():
    master = (ROOT / "ns_studio" / "text" / "stages_master.py").read_text(encoding="utf-8")
    shorts = (ROOT / "ns_studio" / "text" / "stages_shorts.py").read_text(encoding="utf-8")
    assert re.search(r"def _past_openings.*?if is_dev\(ctx\.paths\):\s+return \[\]", master, re.S)
    assert re.search(r"if is_dev\(ctx\.paths\):\s+past = \[\]", shorts)
    for src in (master, shorts):
        assert "order by created_at desc limit 50" in src and "job_id!=?" in src
    chapter = inspect.getsource(validators.validate_chapter)
    assert "if chapter_n == 1 else None" in chapter
    d35 = decision("Opening-line similarity")
    for path in ("ns_studio/text/stages_master.py", "ns_studio/text/stages_shorts.py"):
        assert path in d35, path
    row = decision("Opening-line similarity")
    assert "0.9" in row and "0.8" in row and "dev" in row and "NS-E303" in row and "chapter 1" in row


# ---------------------------------------------------------------------------------------------------------
# 18.6 governor extras
# ---------------------------------------------------------------------------------------------------------
def test_governor_row_records_the_reviewed_minimum_and_the_weekly_raise():
    row = next(ln for ln in spec_section("### 18.6").splitlines() if ln.startswith("| Quality drop"))
    assert cadence.REJECT_MIN_REVIEWED == 5 and cadence.CRITIC_BAR_COOLDOWN_DAYS == 7
    assert cadence.CRITIC_BAR_STEP == 0.5 and cadence.REJECT_RATE_LIMIT == 0.4 and cadence.REJECT_WINDOW == 10
    assert "at least 5" in row and "once per 7 days" in row and "0.5" in row and "10" in row
    assert cites(row, decision_id("Governor quality drop"))
    d = decision("Governor quality drop")
    for needle in ("REJECT_MIN_REVIEWED", "CRITIC_BAR_COOLDOWN_DAYS", "warning", "pending", "daily.py"):
        assert needle in d, needle


def test_governor_rule_behaves_as_the_row_says(tmp_home: Path):
    """Four rejections of four do not drop (under 5 reviewed); five of five do, and the bar rises once."""
    paths, db = _env(tmp_home)
    ch = crud.get_channel(paths, "after-dark")
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def reject(first: int, last: int) -> None:
        for i in range(first, last):
            add_output(
                db,
                f"after-dark-20261001-r{i:03d}",
                "s1",
                status="rejected",
                created_at=f"2026-10-{10 + i:02d}T10:00:00+00:00",
            )

    reject(0, 4)
    assert cadence.plan_channel(db, ch, now).quality_drop is False
    reject(4, 5)
    plan = cadence.plan_channel(db, ch, now)
    assert plan.quality_drop is True and plan.reasons == ["owner rejected 5 of the last 5 outputs"]
    first = cadence.apply_quality_drop(paths, db, ch, plan, now)
    assert first is not None and first == round(ch.critic_bar + 0.5, 1)
    again = crud.get_channel(paths, "after-dark")
    assert cadence.apply_quality_drop(paths, db, again, plan, now + timedelta(days=6)) is None
    assert cadence.apply_quality_drop(paths, db, again, plan, now + timedelta(days=8)) == round(
        first + 0.5, 1
    )
    kinds = [r["kind"] for r in db.query("select kind from incidents where channel='after-dark'")]
    assert kinds == ["warning", "warning"]


# ---------------------------------------------------------------------------------------------------------
# 8: derived Shorts and the {hook, structure} window
# ---------------------------------------------------------------------------------------------------------
def test_section_8_states_the_derived_short_rule_and_the_code_does_it(tmp_home: Path):
    text = spec_section("## 8.")
    line = next(ln for ln in text.splitlines() if "derived from a master" in ln)
    assert "waived" in line and "structure" in line and cites(line, decision_id("Derived Shorts"))

    paths, db = _env(tmp_home)
    pools = Pools(
        hooks=["h1", "h2", "h3", "h4"],
        structures=["s1", "s2", "s3", "s4"],
        pov_tones=["p1"],
        pacings=["standard"],
        caption_styles=["c1", "c2"],
        visual_treatments=["v1", "v2"],
        title_formulas=["t1", "t2", "t3"],
    )
    ch = make_channel(pools=pools, length_buckets=[50])
    assert draw.ledger_window(ch.pools) == 10
    rng = random.Random(5)
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    master = draw.draw_combo(db, ch, rng=rng, now=now)
    draw.record_combo(db, ch.slug, "job-1", "long", master, now)
    pairs = [(master.hook, master.structure)]
    for k in range(1, 5):  # a master and four Shorts that share its structure: 4 hooks, 5 pairs
        when = now + timedelta(minutes=k)
        window = draw.recent_pairs(db, ch.slug, draw.ledger_window(ch.pools))
        short = draw.draw_combo(
            db, ch, rng=rng, now=when, fixed={"structure": master.structure, "pov_tone": "p1"}
        )
        assert short.structure == master.structure
        inside = (short.hook, short.structure) in window
        assert inside == (k == 4), f"Short {k}: the window holds until every hook of the structure is used"
        draw.record_combo(db, ch.slug, "job-1", f"s{k}", short, when)
        pairs.append((short.hook, short.structure))
    assert len(pairs) == 5 and len(set(pairs)) == 4


# ---------------------------------------------------------------------------------------------------------
# 18.11, J7 and the section 13 rows
# ---------------------------------------------------------------------------------------------------------
def test_incident_procedure_line_names_where_it_is_printed_and_the_real_routes():
    text = spec_section("### 18.11")
    assert "README" in text and "OPERATOR.md" in text
    assert "studio pause" in text and "studio resume" in text and "last 10" in text
    assert "Assistant" in text
    assert cites(text, decision_id("Incident procedure"))
    d = decision("Incident procedure")
    for needle in ("Channels.tsx", "svc_channels.py", "review.py", "NS-E326", "Assistant"):
        assert needle in d, needle


def test_new_decision_rows_follow_the_installer_and_runtime_rows():
    nums = [
        int(m.group(1))
        for ln in spec_section("## 13. Decisions").splitlines()
        if (m := re.match(r"^\| D(\d+) \|", ln))
    ]
    assert nums == list(range(1, len(nums) + 1))
    ids = [
        int(decision_id(m)[1:])
        for m in (
            "Dashboard home",
            "Opening-line similarity",
            "Governor quality drop",
            "Derived Shorts",
            "Incident procedure",
        )
    ]
    assert ids == sorted(ids) and len(set(ids)) == 5
    assert min(ids) > 33
    for marker in (
        "Dashboard home",
        "Opening-line similarity",
        "Governor quality drop",
        "Derived Shorts",
        "Incident procedure",
    ):
        assert len(decision(marker).split(" | ")) == 4, marker  # | # | Question | Default | Why |


BOXES = {
    "F-H78": "Dashboard home",
    "F-H79": "Dashboard home",
    "F-H100": "Opening-line similarity",
    "F-H112": "Governor quality drop",
    "F-H82": "Derived Shorts",
    "F-H119": "Incident procedure",
    "F-H167": "Incident procedure",
}


@pytest.mark.parametrize("box_id", sorted(BOXES))
def test_box_cites_its_decision_row_and_is_not_ticked(box_id):
    line = box(box_id)
    assert line.startswith("- [ ] ")
    assert f"section 13 {decision_id(BOXES[box_id])}" in line


AUDIT = {
    "J2 step 1: opening the Dashboard": "built",
    "J2 step 2: Run now": "built",
    "Validator: no opening line": "built",
    "Combo ledger: no repeated {hook, structure}": "built",
    "Cadence governor: quality drop": "partial",
    "Incident procedure: before resume": "partial",
    "Incident procedure is printed on the README": "built",
    "J7.2 README/Operator doc": "built",
}


@pytest.mark.parametrize("feature", sorted(AUDIT))
def test_audit_row_status_follows_the_text(feature):
    assert audit_row(feature)[0] == AUDIT[feature]


def test_counts_table_matches_every_area():
    statuses = ("partial", "missing", "spec_dropped")
    per_area: dict[str, dict[str, int]] = {}
    area = ""
    for ln in STATUS.splitlines():
        if ln.startswith("## "):
            area = ln
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if area and len(cells) >= 7 and cells[0] in statuses:
            per_area.setdefault(area, dict.fromkeys(statuses, 0))[cells[0]] += 1
    table = STATUS.split("## Counts", 1)[1].split("\n## ", 1)[0]
    rows = [[c.strip().strip("*") for c in ln.strip().strip("|").split("|")] for ln in table.splitlines()]
    by_name = {r[0]: r for r in rows if len(r) == 6 and r[1].isdigit()}
    assert len(per_area) == 8
    for name, counted in per_area.items():
        title = name.removeprefix("## ")
        row = next(r for n, r in by_name.items() if title.startswith(n[:24]))
        assert [int(row[i]) for i in (2, 3, 4)] == [counted[s] for s in statuses], title
        assert sum(int(row[i]) for i in (1, 2, 3, 4)) == int(row[5]), title
    total = by_name["All"]
    assert [int(total[i]) for i in (2, 3, 4)] == [sum(c[s] for c in per_area.values()) for s in statuses]
    assert sum(int(total[i]) for i in (1, 2, 3, 4)) == int(total[5])


# ---------------------------------------------------------------------------------------------------------
# P41 review round: PowerShell-safe commands, what a pause stops, 18.3, section 8, window and label words
# ---------------------------------------------------------------------------------------------------------
STUDIO_PATH = "venvs\\studio\\Scripts\\studio.exe"
STUDIO_EXE = '"<install folder>\\' + STUDIO_PATH + '"'


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("name", ["README.md", "OPERATOR.md"])
def test_a_quoted_path_with_arguments_is_safe_in_powershell(name):
    """Windows Terminal opens PowerShell, where `"path" pause x` is a parse error: it needs `& ` in front.

    Every command line in the incident section that starts with the quoted path and carries arguments must
    have the call operator before it, or be labelled "Command Prompt" right in front of the command (cmd.exe
    runs it as is). The section must show the PowerShell form for pause and for resume.
    """
    flat = _flat(doc_section(README if name == "README.md" else GUIDE, HEADING))
    assert f"& {STUDIO_EXE} pause" in flat
    assert f"& {STUDIO_EXE} resume" in flat
    assert "Command Prompt" in flat and "PowerShell" in flat
    for m in re.finditer(re.escape(STUDIO_EXE) + r" [a-z]", flat):
        before = flat[: m.start()]
        label = before[-30:]  # the list label right in front of the command, e.g. "- Command Prompt: "
        assert before.endswith("& ") or "Command Prompt" in label, flat[m.start() - 60 : m.end() + 20]


def test_readme_names_the_full_path_of_the_studio_program():
    """Nothing puts `studio` on the PATH: the README section says what to type instead."""
    flat = _flat(doc_section(README, HEADING))
    assert STUDIO_PATH in flat and "OPERATOR.md" in flat


def test_a_pause_holds_queued_and_scheduled_story_jobs_and_the_texts_say_so():
    """P47: the job worker asks `hold` before it starts a job; the texts say what a pause holds."""
    from ns_dashboard import channel_hold, handlers, jobs

    assert "hold" in inspect.getsource(jobs.JobWorker.run_next).replace("self.store.next_queued", "hold")
    assert "make_channel_hold" in inspect.getsource(handlers.build_state)
    assert channel_hold.HELD_KINDS == {"run", "series_part"}, "a Clipper job is not held: update the guide"
    flat = _flat(doc_section(GUIDE, HEADING))
    assert "only new work" not in flat and "still runs after" not in flat
    assert "already queued or scheduled" in flat and "Pick a time" in flat
    assert "does not start while the channel is paused" in flat and "also when its time comes" in flat
    assert "Cancel" in flat and "already being made" in flat
    assert "Paused: <channel> is paused since <time>; resume it to release this job" in flat
    readme = _flat(doc_section(README, HEADING))
    assert "only new work" not in readme and "waits and starts by itself after the resume" in readme
    d = decision("Incident procedure")
    assert "channel_hold.py" in d and "JobWorker" in d and "scheduled" in d and "refresh_holds" in d
    assert "has no channel-status check" not in d and "still runs after the pause" not in d


def test_retry_from_where_it_stopped_is_refused_for_a_paused_channel_and_the_texts_say_so():
    """A failed story card of a paused channel is refused like a new run: same code, same words (P47)."""
    from ns_dashboard import adapters, studio_bridge, studio_job, studio_runner

    cls = svc_jobs.JobsService
    err = tsx("ui/JobError.tsx")
    assert "Retry from where it stopped" in err and 'j.kind === "run"' in err and "studio_job_ids" in err
    assert "<JobError" in tsx("pages/Create.tsx") and "<JobError" in tsx("pages/Queue.tsx")
    server = (ROOT / "ns_dashboard" / "server.py").read_text(encoding="utf-8")
    assert "dash.jobs.create_resume, job_id" in server
    # the route and the Assistant's gate both reach `_resume_params`, which looks at the channel
    assert "_resume_params" in inspect.getsource(cls.create_resume)
    assert "_resume_params" in inspect.getsource(cls.check_resume)
    assert "_require_channel_not_paused" in inspect.getsource(cls._resume_params)
    assert "_require_not_paused" in inspect.getsource(cls._require_channel_not_paused)
    assert "paused_now" in inspect.getsource(studio_bridge.run_story_jobs)  # a count loop stops at a pause
    for mod in (adapters, studio_job, studio_runner):
        src = inspect.getsource(mod)
        assert "paused" not in src, f"{mod.__name__} checks the channel now: update the guide"
    for name, text in (
        ("OPERATOR.md", doc_section(GUIDE, HEADING)),
        ("README.md", doc_section(README, HEADING)),
        ("D38", decision("Incident procedure")),
    ):
        flat = _flat(text)
        assert "Retry from where it stopped" in flat, name
        assert "refused" in flat, name
        assert "does not refuse a paused channel" not in flat and "do not press" not in flat, name
    assert "the same answer as a new video" in _flat(doc_section(GUIDE, HEADING))


def test_a_pause_during_the_night_stops_the_nights_remaining_jobs_and_the_texts_say_so():
    from ns_studio import daily

    assert "_paused_now" in inspect.getsource(daily._tonight)
    guides = (("OPERATOR.md", doc_section(GUIDE, HEADING)), ("README.md", doc_section(README, HEADING)))
    for name, text in guides:
        assert "pause comes during the night" in _flat(text), name
    d = _flat(decision("Incident procedure"))
    assert "`_paused_now`" in d and "no further job that night" in d
    # the reports keep `skipped` for "an earlier job stopped"; a pause has its own key (P47 review round)
    assert "_hold_for_pause" in inspect.getsource(daily._tonight)
    assert '"skipped"' not in inspect.getsource(daily._hold_for_pause)
    assert "`paused`" in d and "not under `skipped`" in d and "_resume_interrupted" in d
    assert "`paused`" in _flat(doc_section(GUIDE, HEADING))


def test_a_job_that_waited_at_the_gate_is_asked_again_and_the_texts_say_so():
    from ns_dashboard import jobs

    assert "_requeue_if_held" in inspect.getsource(jobs.JobWorker._run_job)
    d = _flat(decision("Incident procedure"))
    assert "`JobWorker._requeue_if_held`" in d and "`ChannelPaused`" in d and "`run_series_part`" in d
    flat = _flat(doc_section(GUIDE, HEADING))
    assert "waiting for the engine" in flat and "puts the job back to waiting" in flat
    assert "waiting for the engine while the night run used it" in _flat(doc_section(README, HEADING))


def test_the_texts_name_the_count_loop_the_hand_typed_run_and_the_wrong_scheduled_text():
    """P47 review round: what a pause still does not reach is written down, not left to be found."""
    guide = _flat(doc_section(GUIDE, HEADING))
    assert "several videos" in guide and "does not start the next" in guide
    # P47 follow-ups: `studio run --channel` / `--resume` refuse a paused channel now, with NS-E330
    assert "`studio run`" in guide and "does not look at the pause" not in guide
    assert "refuses a paused channel with NS-E330" in guide and "`--resume <its job>`" in guide
    assert "Scheduled for <time>" in guide
    assert "several videos" in _flat(doc_section(README, HEADING))
    d = _flat(decision("Incident procedure"))
    assert "`paused_now`" in d and "`run_cmd`" in d and "Scheduled for <time>" in d
    assert "do not look at the pause" not in d and "refuse with NS-E330" in d and "`_run_jobs`" in d


def test_the_texts_name_ns_e330_the_last_night_card_and_the_engine_start():
    """P47 follow-ups: what each route now says about a paused channel is written where the owner reads."""
    from ns_dashboard import studio_bridge
    from ns_dashboard.assistant import composite
    from ns_studio import cli as studio_cli
    from ns_supervisor import scheduled

    paused = getattr(studio_bridge, "ChannelPaused", None)
    assert paused is not None and paused("x").code == "NS-E330", "a pause met by the studio child is coded"
    assert "PAUSED" in inspect.getsource(studio_cli._run_jobs)
    assert "_start_of" in inspect.getsource(composite.tonight_plan)
    assert "HELD_KINDS" in inspect.getsource(scheduled.overdue_scheduled_jobs)
    assert "r.summary?.paused" in tsx("pages/Queue.tsx")
    d = _flat(decision("Incident procedure"))
    assert "NS-E330" in d and "Queue's \"Last night\" card prints them" in d and "is paused yet" not in d
    assert "`tonight_plan`" in d and "`overdue_scheduled_jobs`" in d and "`channel_profile_unreadable`" in d
    assert "`jobs.released_text`" in d
    assert "Last night" in _flat(doc_section(GUIDE, HEADING))


def test_the_content_safety_checklist_is_not_narrower_than_18_3():
    rules = spec_section("### 18.3")
    first = next(ln for ln in rules.splitlines() if ln.startswith("- Real, named people"))
    assert "(living or dead)" in first and "real brands as villains" in first
    flat = _flat(doc_section(GUIDE, HEADING))
    assert "no real, named people (living or dead) in any role" in flat
    assert "no real brands as villains" in flat
    assert "named people or brands as villains" not in flat


def test_section_8_bullet_says_a_derived_short_gets_its_bucket_from_the_plan():
    """`draw_short_combos` fixes structure, pov_tone, pacing and length_bucket; D37 says so."""
    from ns_studio.variation import short_draw

    src = inspect.getsource(short_draw.draw_short_combos)
    fixed = src.split("fixed = {", 1)[1].split("}", 1)[0]
    for key in ("structure", "pov_tone", "pacing", "length_bucket"):
        assert f'"{key}"' in fixed, key
    for key in ("hook", "caption_style", "visual_treatment", "title_formula"):
        assert f'"{key}"' not in fixed, key
    bullet = next(ln for ln in spec_section("## 8.").splitlines() if ln.startswith("- Combo per video"))
    flat = _flat(bullet)
    assert "each Short draws its own `hook, caption_style, visual_treatment, title_formula`" in flat
    assert "draws its own `hook, caption_style, visual_treatment, title_formula, length_bucket`" not in flat
    assert "`length_bucket` is the one the Shorts plan gives" in flat
    assert cites(flat, decision_id("Derived Shorts"))


def test_the_when_control_is_called_by_its_label_and_the_window_may_be_a_browser():
    create = tsx("pages/Create.tsx")
    assert 'label="When to start"' in create and '"Pick a time"' in create
    first = _flat(doc_section(GUIDE, "## Make your first video"))
    assert "leave **When to start** on **Now**" in first and "leave **When** on now" not in first
    later = _flat(doc_section(GUIDE, "## Start a video later"))
    assert "**When** step" in later and "**Pick a time**" in later
    launcher = (ROOT / "launchers" / "nightshift-dashboard.ps1").read_text(encoding="utf-8")
    assert "otherwise the default browser" in launcher and "Start-Process $url" in launcher
    for text in (first, _flat(spec_section("### J2")), _flat(decision("Dashboard home"))):
        assert "default browser" in text


def test_the_half_made_job_of_a_paused_channel_is_promised_only_within_the_resume_age_limit():
    """P47: the next night continues a half-made job after the resume only while it is under
    RESUME_WITHIN_DAYS old (`ownership.resume_problem`); D38, the guides and the morning note say so."""
    from ns_studio import ownership

    days = ownership.RESUME_WITHIN_DAYS
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}
    assert days in words, "RESUME_WITHIN_DAYS changed: name the new number in the guides"
    assert f"it is more than {days} days old" in inspect.getsource(ownership.resume_problem).replace(
        "{RESUME_WITHIN_DAYS}", str(days)
    )
    d = _flat(decision("Incident procedure"))
    assert "the next night continues it after the resume, but only while" in d
    assert f"`RESUME_WITHIN_DAYS` ({days}) days" in d and "ownership.resume_problem" in d
    assert "press Retry in the Queue" in d
    guide = _flat(doc_section(GUIDE, HEADING))
    readme = _flat(doc_section(README, HEADING))
    for text in (guide, readme):
        assert f"no more than {words[days]} days before that night" in text
        assert "Retry" in text.split("days before that night", 1)[1]
