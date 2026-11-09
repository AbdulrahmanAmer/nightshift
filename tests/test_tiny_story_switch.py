# ruff: noqa: E501
"""H-8: the `tiny_story` switch of an engine-test install (docs/DEV_MODE.md).

Same family as `fake_voice` and `fake_footage`: an `answers.json` setting, refused unless `allow_cpu` is set,
written into runtime.json by setup, shown (never editable) in the Settings payload, off in every normal
install and impossible to turn on from the dashboard. What the switch does to the text stages is in
`test_tiny_story.py`.
"""

import json
from pathlib import Path

import pytest

from installer.stage2.answers import AnswersError, build_answers, dump_blanked
from installer.stage2.runtime_cfg import build_runtime, ensure_baseline
from installer.stage2.steps.s07_configs import S07Configs
from installer.stage2.testing import example_answers, make_test_context
from ns_common.config import load_runtime, save_runtime
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_dashboard.errors import ApiError
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.supervisor_link import SupervisorLink


def engine_test_ctx(tmp_path: Path, **answers):
    return make_test_context(
        tmp_path,
        raw_answers=example_answers(allow_cpu=True, **answers),
        resolve=True,
        dev_mode=False,
        preflight={"profile": "cpu-dev"},
    )


# ------------------------------------------------------------------------------------------ the model
def test_the_runtime_switch_is_off_by_default():
    assert Runtime.default("gpu").tiny_story is False and Runtime.default("cpu-dev").tiny_story is False
    assert Runtime().model_dump()["tiny_story"] is False
    assert Runtime.model_validate({"tiny_story": True}).tiny_story is True
    props = Runtime.model_json_schema()["properties"]["tiny_story"]
    assert props["type"] == "boolean" and props["default"] is False


# ---------------------------------------------------------------------------------- answers.json
def test_the_answer_is_refused_without_allow_cpu():
    with pytest.raises(AnswersError, match="allow_cpu"):
        build_answers(example_answers(tiny_story=True))
    with pytest.raises(AnswersError, match="allow_cpu"):
        build_answers(example_answers(tiny_story=True, allow_cpu=False))
    assert build_answers(example_answers(tiny_story=True, allow_cpu=True)).tiny_story is True


def test_the_answer_is_off_unless_asked_and_is_saved_with_the_answers():
    plain = build_answers(example_answers())
    assert plain.tiny_story is False and dump_blanked(plain)["tiny_story"] is False
    asked = build_answers(example_answers(tiny_story=True, allow_cpu=True))
    assert dump_blanked(asked)["tiny_story"] is True


def test_a_wizard_run_refuses_the_answer_without_allow_cpu(tmp_path: Path):
    with pytest.raises(AnswersError, match="allow_cpu"):
        make_test_context(
            tmp_path, raw_answers=example_answers(tiny_story=True), resolve=True, dev_mode=False
        )


# ------------------------------------------------------------------------------- setup -> runtime.json
def test_a_real_engine_test_install_writes_the_switch_into_runtime_json(tmp_path: Path):
    ctx = engine_test_ctx(tmp_path / "r", tiny_story=True)
    assert ctx.tiny_story is True and ctx.fake_voice is False and ctx.fake_footage is False
    rt = ensure_baseline(ctx)
    on_disk = json.loads(ctx.paths.runtime_file.read_text(encoding="utf-8"))
    assert on_disk["tiny_story"] is True and on_disk["dev_mode"] is False
    assert rt.tiny_story is True and load_runtime(ctx.paths).tiny_story is True
    plain = engine_test_ctx(tmp_path / "p")
    assert plain.tiny_story is False and build_runtime(plain).tiny_story is False
    ensure_baseline(plain)
    assert json.loads(plain.paths.runtime_file.read_text(encoding="utf-8"))["tiny_story"] is False


def test_a_dev_install_ignores_the_answer_it_already_runs_on_a_fake_model(tmp_path: Path):
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(allow_cpu=True, tiny_story=True), resolve=True, dev_mode=True
    )
    assert ctx.tiny_story is False and build_runtime(ctx).tiny_story is False


def test_s07_applies_the_answer_to_an_existing_runtime_json_both_ways(tmp_path: Path):
    """ensure_baseline writes runtime.json only once, so a re-run must still take the answer from s07."""
    ctx = engine_test_ctx(tmp_path, tiny_story=True)
    save_runtime(ctx.paths, Runtime.default("cpu-dev"))  # an earlier install: no switch
    assert load_runtime(ctx.paths).tiny_story is False
    S07Configs().run(ctx)
    assert load_runtime(ctx.paths).tiny_story is True
    off = engine_test_ctx(tmp_path / "again")
    save_runtime(off.paths, load_runtime(ctx.paths))
    S07Configs().run(off)
    assert load_runtime(off.paths).tiny_story is False  # the answer removed: the switch follows


# ------------------------------------------------------------------------------------------- dashboard
def make_dash(tmp_home: Path) -> tuple[Dashboard, Paths]:
    paths = Paths.from_env()
    state = build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    return Dashboard(state), state.paths


def refused(fn, *args, **kw) -> ApiError:
    with pytest.raises(ApiError) as ei:
        fn(*args, **kw)
    return ei.value


def test_settings_shows_the_switch_next_to_the_other_test_switches_but_not_as_a_flag(tmp_home: Path):
    dash, paths = make_dash(tmp_home)
    save_runtime(paths, Runtime.default("cpu-dev"))
    s = dash.settings.get_settings()
    assert s["tiny_story"] is False and s["fake_footage"] is False and s["dev_mode"] is False
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"tiny_story": True}))
    s2 = dash.settings.get_settings()
    assert s2["tiny_story"] is True and not any(k.startswith("tiny_story") for k in s2["flags"])
    from ns_dashboard.runtime_flags import HIDDEN, TEST_SWITCHES

    assert set(TEST_SWITCHES) <= set(HIDDEN) and "tiny_story" in TEST_SWITCHES


@pytest.mark.parametrize("switch", ["tiny_story", "fake_footage", "dev_mode"])
def test_no_settings_call_can_turn_a_test_switch_on(tmp_home: Path, switch: str):
    dash, paths = make_dash(tmp_home)
    save_runtime(paths, Runtime.default("cpu-dev"))
    before = paths.runtime_file.read_text(encoding="utf-8")
    for body in (
        {"flags": {switch: True}, "unlock": True},  # a flat key
        {"flags": {switch: "true"}, "unlock": True},  # as text, the way a form sends it
        {"flags": {"video": {"preset": "fast"}, switch: True}, "unlock": True},  # beside a legal change
    ):
        err = refused(dash.settings.put_settings, body)
        assert err.status == 422 and err.code == "test_switch" and switch in err.message
    assert refused(dash.settings.put_settings, {"flags": {switch: True}}, unlock=True).code == "test_switch"
    assert (
        paths.runtime_file.read_text(encoding="utf-8") == before
    )  # nothing written, not even the legal part
    assert getattr(load_runtime(paths), switch) is False


def test_the_helper_that_merges_flags_refuses_a_test_switch_too():
    from ns_dashboard.runtime_flags import TEST_SWITCHES, apply_flags

    for switch in TEST_SWITCHES:
        with pytest.raises(ValueError, match=switch):
            apply_flags({"video": {"preset": "p4"}}, {switch: True})
    assert apply_flags({"video": {"preset": "p4"}}, {"video.preset": "fast"}) == {"video": {"preset": "fast"}}


def test_a_legal_runtime_edit_keeps_the_switch_an_install_was_made_with(tmp_home: Path):
    dash, paths = make_dash(tmp_home)
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"tiny_story": True}))
    out = dash.settings.put_settings({"flags": {"video": {"preset": "fast"}}, "unlock": True})
    assert out["tiny_story"] is True and load_runtime(paths).tiny_story is True
    assert load_runtime(paths).video.preset == "fast"


def test_the_http_route_refuses_it_as_well(tmp_home: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_dashboard.server import create_app

    static = tmp_home / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>Nightshift</title>", encoding="utf-8")
    (static / "app.js").write_text("// ui", encoding="utf-8")
    paths = Paths.from_env()
    state = build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    save_runtime(paths, Runtime.default("cpu-dev"))
    client = TestClient(
        create_app(state=state, start_worker=False, static_dir=static), base_url="http://127.0.0.1:8100"
    )
    for url in ("/api/settings?unlock=true", "/api/settings"):
        resp = client.put(url, json={"flags": {"tiny_story": True}, "unlock": True})
        assert resp.status_code == 422 and resp.json()["error"]["code"] == "test_switch"
    assert client.get("/api/settings").json()["tiny_story"] is False
    assert load_runtime(paths).tiny_story is False
