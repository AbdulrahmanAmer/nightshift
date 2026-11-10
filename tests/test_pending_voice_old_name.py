"""D-24: a voice queued by an older setup under a name the voice library refuses ("Night Narrator") is made
under the suggested name ("Night-Narrator"), the channel is pointed at it, and the owner is told once.

Every test enters where the product does: `voice_setup.apply_pending` with a fake TTS (`_local_engine_make`),
`studio voice apply-pending` through `CliRunner`, and the dashboard's Create it now job
(`studio_job.do_apply_voice` -> `RealVoiceMaker.apply_pending`). A run through `run_job_guarded`
(`apply_pending_voices`) skips a job whose TTS is the fake one, so that route is not entered here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from installer.stage2.answers import suggest_voice_name
from ns_common.config import load_channel, save_channel
from ns_common.errors import NsError
from ns_common.models import Voice
from ns_common.paths import Paths
from ns_dashboard.adapters import RealVoiceMaker
from ns_dashboard.events import EventHub
from ns_dashboard.jobs import JobControl
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio import jobs, voice_setup
from ns_studio.cli import app
from ns_tts.engine import FakeEngine
from ns_tts.voices import VoiceLibrary
from tests.helpers import make_channel
from tests.test_dashboard_api import make_env

runner = CliRunner()
OLD, NEW = "Night Narrator", "Night-Narrator"
SAID = f"voice {NEW} created (setup asked for '{OLD}'; voice names cannot have spaces)"


def queue(paths: Paths, **kw: Any) -> Path:
    f = paths.config / "pending_voice.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    body = {"channel": "c", "mode": "design", "name": OLD, "description": "calm", **kw}
    f.write_text(json.dumps(body), encoding="utf-8")
    return f


def channel_on(paths: Paths, name: str | None = OLD) -> None:
    voice = Voice(mode="design", name=name, description="calm")
    save_channel(paths, make_channel(slug="c", voice=voice))


def make_voice(paths: Paths, name: str) -> None:
    VoiceLibrary(paths.models_tts_voices).add_design(name, "another", engine=FakeEngine())


def voice_of(paths: Paths) -> str | None:
    return load_channel(paths, "c").voice.name


def logged(paths: Paths, event: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in paths.logs.glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["event"] == event:
                out.append(rec)
    return out


def report(env: jobs.Env) -> tuple[str, str | None] | None:
    """`apply_pending_report` as (name, renamed_from); `apply_pending`'s name where there is no report."""
    fn = getattr(voice_setup, "apply_pending_report", None)
    if fn is not None:
        return fn(env)
    name = voice_setup.apply_pending(env)
    return (name, None) if name else None


@pytest.fixture
def paths(tmp_home: Path) -> Paths:
    p = Paths.from_env()
    p.ensure()
    return p


@pytest.fixture
def env(paths: Paths) -> jobs.Env:
    e = jobs.open_env(frozenset({"tts", "llm"}))
    assert e.paths.home == paths.home
    return e


def test_a_queued_spaced_name_is_created_under_the_suggested_name(paths: Paths, env: jobs.Env):
    f = queue(paths)
    channel_on(paths)
    try:
        voice_setup.apply_pending(env)
    except NsError as e:  # the old code refuses with NS-E310; the outcome is what is asserted
        print(e)
    assert (paths.models_tts_voices / NEW / "voice.json").is_file()
    assert not f.exists()
    assert voice_of(paths) == NEW
    (rec,) = logged(paths, "pending_voice_renamed")
    assert OLD in json.dumps(rec) and NEW in json.dumps(rec)


def test_a_run_killed_after_the_voice_was_made_finishes_on_the_next_run(paths: Paths, env: jobs.Env):
    f = queue(paths, name=NEW, renamed_from=OLD)
    channel_on(paths)
    make_voice(paths, NEW)
    try:
        voice_setup.apply_pending(env)
    except NsError as e:
        print(e)
    assert voice_of(paths) == NEW
    assert not f.exists()
    assert len(list(paths.models_tts_voices.iterdir())) == 1  # nothing made twice


def test_a_run_killed_after_the_request_was_rewritten_makes_the_voice_next_time(
    paths: Paths, env: jobs.Env, monkeypatch: pytest.MonkeyPatch
):
    f = queue(paths)
    channel_on(paths)

    def killed(*a: Any, **kw: Any) -> None:
        raise RuntimeError("power cut")

    with monkeypatch.context() as m:
        m.setattr(voice_setup, "_local_engine_make", killed)
        try:
            voice_setup.apply_pending(env)
        except (RuntimeError, NsError) as e:  # the old code refuses the name before it gets that far
            print(e)
    kept = json.loads(f.read_text(encoding="utf-8"))
    assert (kept["name"], kept.get("renamed_from")) == (NEW, OLD)  # written before the voice was made
    assert not (paths.models_tts_voices / NEW / "voice.json").is_file()
    assert voice_of(paths) == OLD
    applied = report(env)
    assert applied == (NEW, OLD)
    assert voice_of(paths) == NEW and not f.exists()


def test_a_suggested_name_that_is_another_voice_is_refused_and_the_request_kept(paths: Paths, env: jobs.Env):
    f = queue(paths)
    channel_on(paths)
    make_voice(paths, NEW)
    before = f.read_bytes()
    with pytest.raises(NsError) as err:
        voice_setup.apply_pending(env)
    assert err.value.code == "NS-E310"
    text = str(err.value)
    assert OLD in text and NEW in text and "Library > Voices" in text and "Create it now" in text
    assert f.read_bytes() == before
    assert voice_of(paths) == OLD
    with pytest.raises(NsError):  # the second try says the same, and still changes nothing
        voice_setup.apply_pending(env)
    assert f.read_bytes() == before


def test_a_channel_already_on_the_suggested_name_just_drops_the_request(paths: Paths, env: jobs.Env):
    f = queue(paths)  # s07 queued the old name again after the rename
    channel_on(paths, NEW)
    make_voice(paths, NEW)
    try:
        assert voice_setup.apply_pending(env) is None
    except NsError as e:
        print(e)
    assert not f.exists()
    assert voice_of(paths) == NEW
    assert len(list(paths.models_tts_voices.iterdir())) == 1


def test_the_rename_is_said_where_the_owner_looks(paths: Paths, tmp_home: Path):
    f = queue(paths)
    channel_on(paths)
    res = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    assert SAID in res.output, res.output
    assert res.exit_code == 0 and not f.exists()
    again = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    assert "no voice is waiting" in again.output and "setup asked for" not in again.output  # once

    # the dashboard: Create it now runs the studio child (`do_apply_voice`), the job's last message says it
    from ns_dashboard import studio_job

    for d in paths.models_tts_voices.iterdir():
        for g in d.iterdir():
            g.unlink()
        d.rmdir()
    queue(paths)
    channel_on(paths)

    class Child:
        def run(self, kind: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
            return studio_job.do_apply_voice({"fake": ["tts"]})

    store = make_env(tmp_home)[1].store
    rec = store.create("apply_voice", "c", {"name": OLD})
    ctl = JobControl(rec, store, EventHub())
    maker = RealVoiceMaker(paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), runner=Child())  # type: ignore[arg-type]
    assert maker.apply_pending(ctl) == NEW
    job = store.get(rec.job_id)
    assert job is not None and SAID in job.message and job.progress == 1.0
    assert voice_of(paths) == NEW


def test_a_valid_queued_name_is_untouched(paths: Paths, env: jobs.Env):
    f = queue(paths, name=NEW)
    channel_on(paths, NEW)
    applied = report(env)
    assert applied == (NEW, None)
    assert (paths.models_tts_voices / NEW / "voice.json").is_file() and not f.exists()
    assert voice_of(paths) == NEW
    assert logged(paths, "pending_voice_renamed") == []
    res = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    assert "no voice is waiting" in res.output


def test_an_over_long_old_name_is_cut_to_40_characters(paths: Paths, env: jobs.Env):
    assert suggest_voice_name("a" * 40) == "a" * 40
    assert suggest_voice_name("a" * 41) == "a" * 40
    queue(paths, name="b" * 40)  # the longest valid name: kept as it is
    assert voice_setup.apply_pending(env) == "b" * 40
    long = "c" * 41
    queue(paths, name=long)
    channel_on(paths, long)
    applied = report(env)
    assert applied == ("c" * 40, long)
    assert voice_of(paths) == "c" * 40
    assert (paths.models_tts_voices / ("c" * 40) / "voice.json").is_file()


def test_a_channel_file_that_cannot_be_saved_keeps_the_request_and_the_next_run_finishes(
    paths: Paths, env: jobs.Env, monkeypatch: pytest.MonkeyPatch
):
    f = queue(paths)
    channel_on(paths)

    def locked(*a: Any, **kw: Any) -> None:
        raise NsError("NS-E119", "channels\\c.yaml: still open in another program")

    with monkeypatch.context() as m:
        m.setattr(voice_setup, "save_channel", locked, raising=False)
        with pytest.raises(NsError) as err:
            voice_setup.apply_pending(env)
    assert err.value.code == "NS-E119"
    assert f.exists() and voice_of(paths) == OLD
    assert (paths.models_tts_voices / NEW / "voice.json").is_file()  # made; only the pointing is owed
    applied = report(env)
    assert applied == (NEW, OLD)
    assert voice_of(paths) == NEW and not f.exists()


def test_a_profile_the_owner_changed_meanwhile_is_not_overwritten(paths: Paths, env: jobs.Env):
    f = queue(paths, name=NEW, renamed_from=OLD)
    channel_on(paths, "my-own-voice")
    make_voice(paths, NEW)
    voice_setup.apply_pending(env)
    assert voice_of(paths) == "my-own-voice" and not f.exists()


def test_a_request_for_a_channel_that_is_gone_still_makes_the_voice(paths: Paths, env: jobs.Env):
    f = queue(paths, channel="gone")
    try:
        voice_setup.apply_pending(env)
    except NsError as e:
        print(e)
    assert (paths.models_tts_voices / NEW / "voice.json").is_file() and not f.exists()
