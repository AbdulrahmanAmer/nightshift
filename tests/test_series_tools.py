import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from ns_common.config import save_channel
from ns_common.paths import Paths
from ns_studio.series import tools as T
from ns_studio.series.cli import app
from ns_studio.series.store import StoryDir, WorkshopError
from tests.helpers import make_channel
from tests.test_series_workshop import PREMISE, make_workshop, story_files

NAMES = [
    "workshop_start",
    "workshop_concepts",
    "workshop_regenerate",
    "workshop_choose",
    "workshop_map",
    "workshop_write_part",
    "workshop_revise_part",
    "workshop_approve",
    "workshop_design",
    "workshop_produce",
    "workshop_status",
    "workshop_list",
]
SPEC_KEYS = {"name", "description", "args_schema", "side_effect", "confirm_text", "fn"}


def test_tools_expose_the_toolspec_fields():
    assert [t["name"] for t in T.TOOLS] == NAMES and set(T.TOOLS_BY_NAME) == set(NAMES)
    for t in T.TOOLS:
        assert set(t) == SPEC_KEYS, t["name"]
        assert t["description"].strip() and "\n" not in t["description"] and callable(t["fn"])
        assert isinstance(t["side_effect"], bool)
        schema = t["args_schema"]
        Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        # the confirm card only needs required arguments
        card = t["confirm_text"].format(**{k: "x" for k in schema["required"]})
        assert card.strip() and "{" not in card


def test_only_status_and_list_are_read_only():
    assert {t["name"] for t in T.TOOLS if not t["side_effect"]} == {"workshop_status", "workshop_list"}


def test_argument_schemas_accept_good_and_reject_bad_arguments():
    v = {name: Draft202012Validator(T.TOOLS_BY_NAME[name]["args_schema"]) for name in NAMES}
    assert v["workshop_map"].is_valid({"slug": "s", "parts": 6, "minutes_per_part": 15})
    assert not v["workshop_map"].is_valid({"slug": "s", "parts": 6, "minutes_per_part": 26})
    assert not v["workshop_map"].is_valid({"slug": "s"})
    assert v["workshop_approve"].is_valid({"slug": "s", "part": "all"})
    assert v["workshop_approve"].is_valid({"slug": "s", "part": 2})
    assert v["workshop_list"].is_valid({}) and not v["workshop_list"].is_valid({"x": 1})
    assert not v["workshop_concepts"].is_valid({"slug": "s", "n": 9})


def test_call_tool_checks_names_and_arguments(tmp_home: Path):
    ws = make_workshop(tmp_home)
    with pytest.raises(WorkshopError, match="unknown workshop tool"):
        T.call_tool(ws, "workshop_nope")
    with pytest.raises(WorkshopError, match="missing"):
        T.call_tool(ws, "workshop_status", {})
    with pytest.raises(WorkshopError, match="unknown"):
        T.call_tool(ws, "workshop_list", {"slug": "x"})
    assert T.call_tool(ws, "workshop_list") == []


def test_tools_drive_a_whole_workshop(tmp_home: Path):
    ws = make_workshop(tmp_home)
    call = lambda name, **args: T.call_tool(ws, name, args)  # noqa: E731
    started = call("workshop_start", channel="after-dark", premise=PREMISE)
    slug = started["slug"]
    assert started["folder"] == str(tmp_home / "stories" / slug) and started["next"] == "workshop_concepts"
    assert call("workshop_concepts", slug=slug)["letters"] == ["A", "B", "C"]
    regen = call("workshop_regenerate", slug=slug, letter="B", feedback="Warmer.")
    assert regen == {"slug": slug, "letter": "B"}
    assert call("workshop_choose", slug=slug, letter="A")["title"] == "The Doorbell Camera"
    series = call("workshop_map", slug=slug, parts=3, minutes_per_part=12)
    assert [p["n"] for p in series["parts"]] == [1, 2, 3] and series["title"] == "The Doorbell Camera"
    written = call("workshop_write_part", slug=slug, n=1)
    assert written["part"] == 1 and len(written["shorts"]) >= 4
    assert call("workshop_revise_part", slug=slug, n=1, feedback="Nothing.")["changed_chapters"] == []

    status = call("workshop_status", slug=slug)
    assert status["next_step"] == "write part 2" and "long_16x9" in status["design_choices"]["format"]
    assert call("workshop_approve", slug=slug, part=1)["parts"]["1"]["status"] == "approved"
    design = call("workshop_design", slug=slug, caption_style="boxed_dark", publish_cadence_days=3)
    assert design["caption_style"] == "boxed_dark" and design["publish_cadence_days"] == 3
    jobs = call("workshop_produce", slug=slug, part=1)
    assert [j["part"] for j in jobs] == [1] and jobs[0]["title"] == "The Doorbell Camera - Part 01"
    listed = call("workshop_list")
    assert [(s["slug"], s["status"], s["title"], s["parts"]) for s in listed] == [
        (slug, "producing", "The Doorbell Camera", 3)
    ]
    json.dumps([status, jobs])  # every result is JSON-ready


def test_read_only_tools_do_not_touch_the_folder(tmp_home: Path):
    ws = make_workshop(tmp_home)
    slug = T.call_tool(ws, "workshop_start", {"channel": "after-dark", "premise": PREMISE})["slug"]
    root = tmp_home / "stories" / slug
    before = {f: (root / f).read_bytes() for f in story_files(root)}
    T.call_tool(ws, "workshop_status", {"slug": slug})
    T.call_tool(ws, "workshop_list", {})
    assert {f: (root / f).read_bytes() for f in story_files(root)} == before


# --- command line ------------------------------------------------------------------------------------


@pytest.fixture
def runner(tmp_home: Path) -> CliRunner:
    save_channel(Paths.from_env(), make_channel(format="master_first"))
    return CliRunner()


def run(runner: CliRunner, *args: str, code: int = 0) -> str:
    result = runner.invoke(app, [*args, "--fake-services"])
    assert result.exit_code == code, result.output
    return result.output


def test_help_lists_every_command(runner: CliRunner):
    out = runner.invoke(app, ["--help"]).output
    for cmd in ("start", "concepts", "regenerate", "choose", "map", "write", "revise", "approve", "design",
                "produce", "status", "list"):  # fmt: skip
        assert cmd in out, cmd


def test_cli_runs_a_workshop_with_fake_services(runner: CliRunner, tmp_home: Path):
    assert json.loads(run(runner, "list")) == []
    slug = run(runner, "start", "after-dark", "--premise", PREMISE).strip()
    assert slug == "a-night-nurse-s-doorbell"
    assert json.loads(run(runner, "concepts", slug)) == ["A", "B", "C"]
    assert run(runner, "regenerate", slug, "B", "--feedback", "Warmer.").strip() == "B"
    assert run(runner, "choose", slug, "A").strip() == "The Doorbell Camera"
    series = json.loads(run(runner, "map", slug, "--parts", "3", "--minutes", "12"))
    assert [p["minutes"] for p in series["parts"]] == [12, 12, 12]
    written = json.loads(run(runner, "write", slug, "1"))
    assert written["chapters"] == 5 and written["script"].endswith("script.md")
    assert json.loads(run(runner, "revise", slug, "1", "--feedback", "Nothing."))["changed_chapters"] == []
    status = json.loads(run(runner, "approve", slug, "1"))
    assert status["parts"]["1"]["status"] == "approved"
    choices = json.loads(run(runner, "design", slug))
    assert "dark_slowzoom" in choices["visual_treatment"]
    design = json.loads(run(runner, "design", slug, "--caption-style", "boxed_dark", "--cadence-days", "4"))
    assert design["caption_style"] == "boxed_dark" and design["publish_cadence_days"] == 4
    jobs = json.loads(run(runner, "produce", slug, "1"))
    assert jobs[0]["part"] == 1
    overview = json.loads(run(runner, "status", slug))
    assert overview["status"] == "producing" and overview["parts"][0]["job_id"] == jobs[0]["job_id"]
    assert [s["slug"] for s in json.loads(run(runner, "list"))] == [slug]
    assert StoryDir.open(Paths.from_env(), slug).script(1).is_file()


def test_cli_reports_errors_with_exit_code_1(runner: CliRunner):
    out = run(runner, "status", "nope", code=1)
    assert "no story 'nope'" in out
    out = run(runner, "start", "after-dark", "--premise", "Taylor Swift finds a camera", code=1)
    assert "NS-E602" in out and "hint:" in out
    slug = run(runner, "start", "after-dark", "--premise", PREMISE).strip()
    assert "12-25" in run(runner, "map", slug, "--parts", "3", "--minutes", "30", code=1)
