# ruff: noqa: E501
"""D-24: a voice queued by an older setup under a name the voice library refuses ("Night Narrator") is made
under the suggested name ("Night-Narrator"), the channel is pointed at it, and the owner is told once.

One test per row of the states-and-steps table (the report's `states_and_steps`). Every test enters where
the product does: `voice_setup.apply_pending` with the in-process TTS (`_local_engine_make`), `studio voice
apply-pending` and `studio channel edit` through `CliRunner`, `cli._setup_voice` (the start of `studio run`
and of the nightly), the dashboard's Create it now job (`studio_job.do_apply_voice` ->
`RealVoiceMaker.apply_pending`), the card of Library > Voices (`adapters.read_pending_voice`), and the run
route (`regen.run_job_guarded`) with a TTS stand-in that is not `FakeTts` and knows which voices exist.

The run route stops at `run_job`, which is replaced by what the voice stage does first (`job_voice`, then
the TTS is asked to speak with that name): the stage itself needs FFmpeg, which the laptop runs without.
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
from ns_common.models import Channel, Voice
from ns_common.paths import Paths
from ns_common.tts_client import FakeTts, TtsFile, TtsItem
from ns_dashboard.adapters import RealVoiceMaker, read_pending_voice
from ns_dashboard.events import EventHub
from ns_dashboard.jobs import JobControl
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio import cli, jobs, voice_setup
from ns_studio.cli import app
from ns_studio.context import JobContext
from ns_studio.policy import regen
from ns_supervisor import notify
from ns_tts.engine import FakeEngine
from ns_tts.voices import PRESETS, VoiceLibrary
from tests.helpers import make_channel, make_ctx
from tests.test_dashboard_api import make_env
from tests.test_pending_voice_run import NOT_TTS

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()
OLD, NEW = "Night Narrator", "Night-Narrator"
SAID = f"voice {NEW} created (setup asked for '{OLD}'; voice names cannot have spaces)"
SAID_C = f"{SAID}; channel c now uses it"
AGAIN = "press Create it now in Library > Voices (or run: studio voice apply-pending)"
WAY_OUT = "Open Channels > Edit channel for c, choose a narrator under Voice"
WAY_OUT_CLI = f"studio channel edit c --set voice.name={NEW}"


def queue(paths: Paths, **kw: Any) -> Path:
    f = paths.config / "pending_voice.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    body = {"channel": "c", "mode": "design", "name": OLD, "description": "calm", **kw}
    f.write_text(json.dumps(body), encoding="utf-8")
    return f


def channel_on(paths: Paths, name: str | None = OLD, slug: str = "c") -> Channel:
    voice = Voice(mode="design", name=name, description="calm") if name else Voice()
    ch = make_channel(slug=slug, display_name=f"Channel {slug}", voice=voice)
    save_channel(paths, ch)
    return ch


def make_voice(paths: Paths, name: str) -> None:
    VoiceLibrary(paths.models_tts_voices).add_design(name, "another", engine=FakeEngine())


def voices(paths: Paths) -> list[str]:
    root = paths.models_tts_voices
    return sorted(d.name for d in root.iterdir()) if root.is_dir() else []


def voice_of(paths: Paths, slug: str = "c") -> str | None:
    return load_channel(paths, slug).voice.name


def kept(f: Path) -> tuple[str, str | None]:
    data = json.loads(f.read_text(encoding="utf-8"))
    return data["name"], data.get("renamed_from")


def logged(paths: Paths, event: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in paths.logs.glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["event"] == event:
                out.append(rec)
    return out


def notes(paths: Paths) -> list[str]:
    """The morning notes that wait for the owner (the Queue page shows them)."""
    return [n["message"] for n in notify.read_notes(paths)]


def apply(env: jobs.Env, **kw: Any) -> str | None | NsError:
    """`apply_pending`: the name made, None, or the coded stop (the base stops on every old-shaped name)."""
    try:
        return voice_setup.apply_pending(env, **kw)
    except NsError as e:
        return e


def stop_of(out: object, code: str = "NS-E310") -> str:
    """The detail of the coded stop `out` must be."""
    assert isinstance(out, NsError), out
    assert out.code == code, out
    return out.detail


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


# ---------------------------------------------------------------- the request itself: every state


def test_a_queued_spaced_name_is_created_under_the_suggested_name(paths: Paths, env: jobs.Env):
    f = queue(paths)
    channel_on(paths)
    out = apply(env)
    assert (paths.models_tts_voices / NEW / "voice.json").is_file(), out
    assert not f.exists()
    assert voice_of(paths) == NEW
    (rec,) = logged(paths, "pending_voice_renamed")
    assert OLD in json.dumps(rec) and NEW in json.dumps(rec)


def test_a_valid_queued_name_is_untouched(paths: Paths, env: jobs.Env):
    f = queue(paths, name=NEW)
    channel_on(paths, NEW)
    assert apply(env) == NEW
    assert (paths.models_tts_voices / NEW / "voice.json").is_file() and not f.exists()
    assert voice_of(paths) == NEW
    assert logged(paths, "pending_voice_renamed") == [] and notes(paths) == []
    res = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    assert "no voice is waiting" in res.output


def test_an_over_long_old_name_is_cut_to_40_characters(paths: Paths, env: jobs.Env):
    assert suggest_voice_name("a" * 40) == "a" * 40
    assert suggest_voice_name("a" * 41) == "a" * 40
    queue(paths, name="b" * 40)  # the longest valid name: kept as it is
    assert apply(env) == "b" * 40
    long = "c" * 41
    queue(paths, name=long)
    channel_on(paths, long)
    assert apply(env) == "c" * 40
    assert voice_of(paths) == "c" * 40
    assert (paths.models_tts_voices / ("c" * 40) / "voice.json").is_file()


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
    assert kept(f) == (NEW, OLD)  # written before the voice was made
    assert voices(paths) == [] and voice_of(paths) == OLD
    assert apply(env) == NEW
    assert voice_of(paths) == NEW and not f.exists()


def test_a_run_killed_after_the_voice_was_made_finishes_on_the_next_run(paths: Paths, env: jobs.Env):
    f = queue(paths, name=NEW, renamed_from=OLD)
    channel_on(paths)
    make_voice(paths, NEW)
    out = apply(env)
    assert voice_of(paths) == NEW, out
    assert not f.exists()
    assert voices(paths) == [NEW]  # nothing made twice


def test_a_run_killed_after_the_channel_was_pointed_says_the_rename_once_and_forgets(paths: Paths, tmp_home: Path):
    f = queue(paths, name=NEW, renamed_from=OLD)  # the voice is made and the channel uses it; only the
    channel_on(paths, NEW)  # request is left: the killed run never got to say the rename
    make_voice(paths, NEW)
    assert read_pending_voice(paths) is None  # nothing is owed that a button could make: no card
    res = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    assert SAID_C in res.output, res.output
    assert not f.exists() and voice_of(paths) == NEW and voices(paths) == [NEW]
    again = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    assert "no voice is waiting" in again.output and "setup asked for" not in again.output


def test_a_suggested_name_that_is_another_voice_is_refused_and_the_request_kept(paths: Paths, env: jobs.Env):
    f = queue(paths)
    channel_on(paths)
    make_voice(paths, NEW)  # the owner made it in Library > Voices on 0.1.14 and never repointed the channel
    before = f.read_bytes()
    text = stop_of(apply(env))
    assert OLD in text and NEW in text and WAY_OUT in text and "Save channel" in text and WAY_OUT_CLI in text
    assert "Rename or delete" not in text  # the Library and the CLI can do neither
    assert f.read_bytes() == before and voice_of(paths) == OLD
    assert stop_of(apply(env)) == text  # the second try says the same, and still changes nothing
    assert f.read_bytes() == before
    # the way out, as written: the command exists and ends the refusal; nothing is asked of the owner twice
    res = runner.invoke(app, ["channel", "edit", "c", "--set", f"voice.name={NEW}"])
    assert res.exit_code == 0, res.output
    assert read_pending_voice(paths) is None  # "this request then goes away by itself": the card is gone
    assert apply(env) is None
    assert not f.exists() and voice_of(paths) == NEW and voices(paths) == [NEW]


def test_the_words_of_the_way_out_are_on_the_pages_they_name():
    channels = (ROOT / "ns_dashboard/ui/src/pages/Channels.tsx").read_text(encoding="utf-8")
    library = (ROOT / "ns_dashboard/ui/src/pages/Library.tsx").read_text(encoding="utf-8")
    side = (ROOT / "ns_dashboard/ui/src/shell/SideTab.tsx").read_text(encoding="utf-8")
    assert 'label: "Channels"' in side and 'label: "Library"' in side
    assert ">Edit channel<" in channels and ">Save channel<" in channels
    assert 'title="Voice"' in channels and 'label="Narrator"' in channels
    assert 'label: "Voices"' in library and '"Create it now"' in library


def test_a_suggested_name_that_another_channel_speaks_with_is_never_taken_over(paths: Paths, env: jobs.Env):
    f = queue(paths)
    channel_on(paths)
    channel_on(paths, NEW, slug="d")  # another channel's voice
    make_voice(paths, NEW)
    before = (paths.models_tts_voices / NEW / "voice.json").read_bytes()
    text = stop_of(apply(env))
    assert WAY_OUT in text
    assert voice_of(paths) == OLD and voice_of(paths, "d") == NEW and f.exists()
    assert (paths.models_tts_voices / NEW / "voice.json").read_bytes() == before


def test_a_suggested_name_the_library_would_refuse_as_used_is_refused_before_anything_is_written(
    paths: Paths, env: jobs.Env
):
    assert "Ryan" in PRESETS
    f = queue(paths, name="Ryan !")  # the suggestion is a built-in voice
    channel_on(paths, "Ryan !")
    before = f.read_bytes()
    text = stop_of(apply(env))
    assert "'Ryan' is already another voice" in text and WAY_OUT in text
    assert f.read_bytes() == before and voices(paths) == []
    make_voice(paths, NEW.lower())  # the library compares names without letter case
    f = queue(paths)
    channel_on(paths)
    assert f"'{NEW}' is already another voice" in stop_of(apply(env))
    assert kept(f) == (OLD, None) and voices(paths) == [NEW.lower()]


def test_a_channel_already_on_the_suggested_name_just_drops_the_request(paths: Paths, env: jobs.Env):
    f = queue(paths)  # s07 queued the old name again after the rename
    channel_on(paths, NEW)
    make_voice(paths, NEW)
    assert apply(env) is None
    assert not f.exists()
    assert voice_of(paths) == NEW
    assert voices(paths) == [NEW]


@pytest.mark.parametrize(
    ("now", "taken", "why"),
    [
        ("my-own-voice", True, "channel c now uses 'my-own-voice'"),  # refused for ever before
        ("my-own-voice", False, "channel c now uses 'my-own-voice'"),
        (None, True, "channel c now uses a built-in voice"),
        ("gone", False, "channel c is gone"),
    ],
)
def test_a_request_no_channel_asks_for_any_more_is_dropped_and_says_so(
    paths: Paths, tmp_home: Path, now: str | None, taken: bool, why: str
):
    f = queue(paths)
    if now != "gone":
        channel_on(paths, now)
    if taken:
        make_voice(paths, NEW)
    had = voices(paths)
    assert read_pending_voice(paths) is None  # no card: there is nothing to press for
    res = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    said = f"the voice setup asked for ('{OLD}') is not needed any more: {why}; nothing was created"
    assert said in res.output, res.output
    assert res.exit_code == 0 and not f.exists() and voices(paths) == had
    if now != "gone":
        assert voice_of(paths) == now


def test_a_profile_the_owner_changed_meanwhile_is_not_overwritten(paths: Paths, env: jobs.Env):
    f = queue(paths, name=NEW, renamed_from=OLD)
    channel_on(paths, "my-own-voice")
    make_voice(paths, NEW)
    assert apply(env) == NEW
    assert voice_of(paths) == "my-own-voice" and not f.exists()


def test_a_profile_that_cannot_be_read_keeps_the_request_and_changes_nothing(paths: Paths, env: jobs.Env):
    f = queue(paths)
    channel_on(paths)
    channel_on(paths, None, slug="d")
    before = f.read_bytes()
    profile = paths.channel_file("c")
    good = profile.read_bytes()
    profile.write_text("slug: c\nvoice: [a hand edit with a typo\n", encoding="utf-8")
    text = stop_of(apply(env))
    assert "the profile of channel c cannot be read" in text and str(profile) in text and AGAIN in text
    assert "'studio channel list' shows what is wrong" in text
    assert f.read_bytes() == before and voices(paths) == []  # nothing made, nothing rewritten
    assert read_pending_voice(paths) is not None  # the card with Create it now is there to press
    listed = runner.invoke(app, ["channel", "list"])
    assert "c\tUNREADABLE" in listed.output, listed.output  # the command the sentence names shows it
    assert stop_of(apply(env)) == text and f.read_bytes() == before
    profile.write_bytes(good)  # the owner corrects the typo
    assert apply(env) == NEW
    assert voice_of(paths) == NEW and not f.exists()


def test_a_profile_another_program_holds_is_not_read_as_gone(
    paths: Paths, env: jobs.Env, monkeypatch: pytest.MonkeyPatch
):
    f = queue(paths)
    channel_on(paths)
    before = f.read_bytes()

    def held(*a: Any, **kw: Any) -> Channel:
        raise PermissionError(13, "The process cannot access the file", str(paths.channel_file("c")))

    with monkeypatch.context() as m:
        m.setattr(voice_setup, "load_channel", held)
        text = stop_of(apply(env))
    assert "cannot be read" in text and AGAIN in text
    assert f.read_bytes() == before and voices(paths) == [] and voice_of(paths) == OLD
    assert apply(env) == NEW and voice_of(paths) == NEW and not f.exists()


def test_a_profile_that_breaks_while_the_voice_is_made_keeps_the_request_to_repoint_it(
    paths: Paths, env: jobs.Env, monkeypatch: pytest.MonkeyPatch
):
    """Blocker 3: the voice is made, the channel cannot be read, and the request used to be deleted."""
    f = queue(paths)
    channel_on(paths)
    profile = paths.channel_file("c")
    good = profile.read_bytes()
    real = voice_setup._local_engine_make

    def make_then_typo(*a: Any, **kw: Any) -> None:
        real(*a, **kw)
        profile.write_text("slug: c\nvoice: [a hand edit with a typo\n", encoding="utf-8")

    with monkeypatch.context() as m:
        m.setattr(voice_setup, "_local_engine_make", make_then_typo)
        text = stop_of(apply(env))
    assert f"voice {NEW} is created" in text and "cannot be read" in text and AGAIN in text
    assert voices(paths) == [NEW] and kept(f) == (NEW, OLD)  # queued to repoint the channel
    assert read_pending_voice(paths) is not None
    profile.write_bytes(good)
    assert voice_of(paths) == OLD
    assert apply(env) == NEW
    assert voice_of(paths) == NEW and not f.exists() and voices(paths) == [NEW]


def test_a_channel_file_that_cannot_be_saved_keeps_the_request_and_the_next_run_finishes(
    paths: Paths, env: jobs.Env, monkeypatch: pytest.MonkeyPatch
):
    f = queue(paths)
    channel_on(paths)

    def locked(*a: Any, **kw: Any) -> None:
        raise NsError("NS-E119", "channels\\c.yaml: still open in another program")

    with monkeypatch.context() as m:
        m.setattr(voice_setup, "save_channel", locked, raising=False)
        text = stop_of(apply(env), "NS-E119")
    assert f"voice {NEW} is created" in text and "still open in another program" in text and AGAIN in text
    assert kept(f) == (NEW, OLD) and voice_of(paths) == OLD
    assert voices(paths) == [NEW]  # made; only the pointing is owed
    assert read_pending_voice(paths) is not None  # so Create it now, which the sentence names, is there
    assert apply(env) == NEW
    assert voice_of(paths) == NEW and not f.exists()


def test_a_request_file_that_cannot_be_removed_is_removed_by_the_next_run(
    paths: Paths, env: jobs.Env, monkeypatch: pytest.MonkeyPatch
):
    f = queue(paths)
    channel_on(paths)
    real = Path.unlink

    def held(self: Path, *a: Any, **kw: Any) -> None:
        if self.name == "pending_voice.json":
            raise PermissionError(13, "The process cannot access the file", str(self))
        real(self, *a, **kw)

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", held)
        text = stop_of(apply(env), "NS-E119")
    assert "could not be removed" in text and f"voice {NEW} is created" in text
    assert voices(paths) == [NEW] and voice_of(paths) == NEW and f.exists()  # the channel is right already
    assert apply(env) == NEW
    assert not f.exists() and voices(paths) == [NEW]


def test_two_routes_at_once_make_one_voice_and_neither_fails(paths: Paths, env: jobs.Env):
    """Create it now and a run at the same moment: the second arrives at the service when the first has
    finished everything (the step is held open by running the whole first route inside it)."""
    f = queue(paths)
    channel_on(paths)
    other = jobs.open_env(frozenset({"tts", "llm"}))
    first: list[object] = []

    def post(base_url: str, route: str, body: dict[str, Any]) -> dict[str, Any]:
        first.append(apply(other))
        raise NsError("NS-E310", f"voice service answered 400: a voice named '{body['name']}' already exists")

    second = apply(env, post=post)
    assert first == [NEW] and second == NEW
    assert voices(paths) == [NEW] and voice_of(paths) == NEW and not f.exists()
    assert apply(env) is None  # and nothing is left to do


# ---------------------------------------------------------------- what the owner reads, route by route


class Child:
    """The studio child of the dashboard, in process: the same `do_apply_voice` the real child runs."""

    def run(self, kind: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        from ns_dashboard import studio_job

        return studio_job.do_apply_voice({"fake": ["tts"]})


def create_it_now(paths: Paths, tmp_home: Path) -> tuple[object, str]:
    """Library > Voices > Create it now: (the name made or the coded stop, the job's last message)."""
    store = make_env(tmp_home)[1].store
    rec = store.create("apply_voice", "c", {"name": OLD})
    ctl = JobControl(rec, store, EventHub())
    maker = RealVoiceMaker(paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), runner=Child())  # type: ignore[arg-type]
    out: object
    try:
        out = maker.apply_pending(ctl)
    except NsError as e:
        out = e
    job = store.get(rec.job_id)
    assert job is not None
    return out, job.message


def test_the_rename_is_said_where_the_owner_looks(paths: Paths, tmp_home: Path):
    f = queue(paths)
    channel_on(paths)
    res = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    assert SAID_C in res.output, res.output
    assert res.exit_code == 0 and not f.exists()
    again = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    assert "no voice is waiting" in again.output and "setup asked for" not in again.output  # once

    # the dashboard: Create it now runs the studio child (`do_apply_voice`), the job's last message says it
    for d in paths.models_tts_voices.iterdir():
        for g in d.iterdir():
            g.unlink()
        d.rmdir()
    queue(paths)
    channel_on(paths)
    out, message = create_it_now(paths, tmp_home)
    assert out == NEW and SAID_C in message
    assert voice_of(paths) == NEW


def test_the_refusal_and_the_drop_read_the_same_in_the_dashboard_job(paths: Paths, tmp_home: Path):
    queue(paths)
    channel_on(paths)
    make_voice(paths, NEW)
    assert read_pending_voice(paths) is not None  # the card is there, so the button can be pressed
    out, _ = create_it_now(paths, tmp_home)
    text = stop_of(out)
    assert WAY_OUT in text and WAY_OUT_CLI in text
    assert isinstance(out, NsError) and "engine is not at fault" in str(out.data.get("hint"))  # not "check the TTS log"
    cli_says = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    assert text in cli_says.output and cli_says.exit_code == 1  # the same sentence on the CLI
    assert "engine is not at fault" in cli_says.output and "tts-*.jsonl" not in cli_says.output
    channel_on(paths, "my-own-voice")  # the owner chose another narrator while a press was on its way
    out, message = create_it_now(paths, tmp_home)
    assert out is None
    assert message == f"the voice setup asked for ('{OLD}') is not needed any more: channel c now uses 'my-own-voice'; nothing was created"


def test_the_library_card_is_there_exactly_while_the_button_has_something_to_do(paths: Paths):
    queue(paths, name=NEW)
    channel_on(paths, NEW)
    assert (read_pending_voice(paths) or {}).get("name") == NEW  # a valid name, not made yet (control)
    make_voice(paths, NEW)
    assert read_pending_voice(paths) is None
    queue(paths)
    channel_on(paths)
    assert (read_pending_voice(paths) or {}).get("name") == OLD  # refused: pressing says the way out
    channel_on(paths, NEW)
    assert read_pending_voice(paths) is None  # the owner took the way out
    queue(paths, name=NEW, renamed_from=OLD)
    channel_on(paths)
    assert (read_pending_voice(paths) or {}).get("name") == NEW  # made, the channel not pointed yet
    paths.channel_file("c").write_text("slug: [", encoding="utf-8")
    assert read_pending_voice(paths) is not None  # cannot be read: pressing says why
    paths.channel_file("c").unlink()
    assert read_pending_voice(paths) is None  # channel gone


# ---------------------------------------------------------------- the run route: nightly, Run now, studio run


class LibraryTts:
    """A TTS stand-in that knows which voices exist, like the service: the built-in names and what the
    voice library folder holds. An unknown voice is `NS-E311` in the words of `TtsClient`."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.spoken: list[str] = []
        self._tone = FakeTts()

    def _known(self, voice: str) -> None:
        if voice not in PRESETS and not (self.paths.models_tts_voices / voice / "voice.json").is_file():
            raise NsError(
                "NS-E311", f"unknown voice {voice!r}: create the voice first (Library > Voices or 'studio voice design')"
            )
        self.spoken.append(voice)

    def speak(
        self, text: str, voice: str, speed: float, out_path: Path, *, seed: int | None = None,
        language: str | None = None,
    ) -> float:
        self._known(voice)
        return self._tone.speak(text, voice, speed, out_path, seed=seed, language=language)

    def speak_batch(
        self, items: list[TtsItem], out_dir: Path, *, seed: int | None = None, timeout_s: float | None = None,
    ) -> list[TtsFile]:
        for it in items:
            self._known(it.voice)
        return self._tone.speak_batch(items, out_dir, seed=seed, timeout_s=timeout_s)

    def voices(self) -> list[dict]:
        return [{"name": n, "mode": "preset", "source": "stand-in"} for n in PRESETS]

    def health(self) -> bool:
        return True


class Service:
    """The TTS service's voice design route, standing in for `http_post`: it stores the voice the way the
    service does and answers 400 to a name that exists; `up = False` is a service that is not running."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.up = True
        self.made: list[str] = []

    def post(self, base_url: str, route: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.up:
            raise NsError("NS-E310", "voice service: connection refused")
        try:
            VoiceLibrary(self.paths.models_tts_voices).add_design(body["name"], body["description"], engine=FakeEngine())
        except ValueError as e:
            raise NsError("NS-E310", f"voice service answered 400: {e}") from e
        self.made.append(body["name"])
        return {}


@pytest.fixture
def service(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> Service:
    svc = Service(paths)
    monkeypatch.setattr(voice_setup, "http_post", svc.post)
    return svc


@pytest.fixture
def tts(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> LibraryTts:
    """One TTS for every job of the test, and `run_job` cut down to what the voice stage does first."""
    stand_in = LibraryTts(paths)

    def speak_only(ctx: JobContext, resume: bool = True) -> list[str]:
        from ns_studio.media.voice import job_voice

        voice = job_voice(ctx)
        name = voice.name or voice.speaker or "Ryan"  # as `VoiceStage.run` picks it
        ctx.services.tts.speak("the porch light was on again", name, 1.0, ctx.job_dir / "audio" / "p1.wav")
        return ["voice"]

    monkeypatch.setattr(regen, "run_job", speak_only)
    return stand_in


def night_job(tmp_home: Path, tts: LibraryTts, channel: Channel) -> JobContext:
    """A job as `make_job` builds it: the profile it was given, real (not fake) TTS."""
    ctx = make_ctx(tmp_home, fake=NOT_TTS, channel=channel)
    ctx.services.tts = tts
    assert not isinstance(ctx.services.tts, FakeTts)
    return ctx


def run(ctx: JobContext) -> list[str] | NsError:
    try:
        return regen.run_job_guarded(ctx)
    except NsError as e:
        return e


def test_the_job_of_the_run_that_renames_speaks_with_the_new_voice_and_the_morning_note_says_so(
    tmp_home: Path, paths: Paths, service: Service, tts: LibraryTts
):
    """Blocker 1: the job's profile is loaded before the voice is made; the same job asked for the old name,
    the service answered "unknown voice", and the rename was only logged."""
    f = queue(paths)
    planned = channel_on(paths)  # the profile as the night's plan holds it, for both jobs of the night
    ctx = night_job(tmp_home, tts, planned)
    assert run(ctx) == ["voice"]
    assert tts.spoken == [NEW] and service.made == [NEW]
    assert voice_of(paths) == NEW and not f.exists()
    assert notes(paths) == [SAID_C]
    client, _ = make_env(tmp_home)  # where the owner reads it in the morning: the Queue page asks /api/notes
    assert [n["message"] for n in client.get("/api/notes").json()] == [SAID_C]
    second = night_job(tmp_home, tts, planned)  # the night's second job for the channel, same stale profile
    assert second.channel.voice.name == OLD
    assert run(second) == ["voice"]
    assert tts.spoken == [NEW, NEW] and service.made == [NEW] and notes(paths) == [SAID_C]


def test_the_job_that_could_only_fail_stops_with_the_refusal_and_other_channels_run(
    tmp_home: Path, paths: Paths, service: Service, tts: LibraryTts
):
    f = queue(paths)
    channel_on(paths)
    other = channel_on(paths, None, slug="d")  # a channel with a built-in voice
    make_voice(paths, NEW)  # the owner's own voice
    before = f.read_bytes()
    out = run(night_job(tmp_home, tts, load_channel(paths, "c")))
    text = stop_of(out)
    assert WAY_OUT in text and WAY_OUT_CLI in text and tts.spoken == []  # stopped before any stage ran
    assert notes(paths) == [f"NS-E310: {text}"]
    assert run(night_job(tmp_home, tts, other)) == ["voice"] and tts.spoken == ["Ryan"]
    stop_of(run(night_job(tmp_home, tts, load_channel(paths, "c"))))  # the next night: the same stop
    assert notes(paths) == [f"NS-E310: {text}"]  # said once while the note waits, not once a night
    assert f.read_bytes() == before and service.made == []
    # the way out the sentence gives, then the next run: the request goes, the job speaks
    res = runner.invoke(app, ["channel", "edit", "c", "--set", f"voice.name={NEW}"])
    assert res.exit_code == 0, res.output
    assert run(night_job(tmp_home, tts, load_channel(paths, "c"))) == ["voice"]
    assert tts.spoken == ["Ryan", NEW] and not f.exists() and len(notes(paths)) == 1


def test_a_night_the_voice_service_is_down_leaves_a_note_and_the_next_night_finishes(
    tmp_home: Path, paths: Paths, service: Service, tts: LibraryTts
):
    f = queue(paths)
    channel_on(paths)
    service.up = False
    out = run(night_job(tmp_home, tts, load_channel(paths, "c")))
    assert kept(f) == (NEW, OLD), out  # the rename is recorded before the voice is asked for
    assert isinstance(out, NsError) and out.code == "NS-E311"  # the job went on to the voice stage, as before
    (note,) = notes(paths)
    assert note.startswith("the voice chosen during setup is not created yet (NS-E310")
    assert "connection refused" in note and note.endswith(AGAIN)
    assert voice_of(paths) == OLD and voices(paths) == []
    service.up = True
    assert run(night_job(tmp_home, tts, load_channel(paths, "c"))) == ["voice"]
    assert tts.spoken == [NEW] and voice_of(paths) == NEW and not f.exists()
    assert notes(paths) == [note, SAID_C]


def test_a_profile_held_during_the_night_still_gives_that_job_the_new_voice(
    tmp_home: Path, paths: Paths, service: Service, tts: LibraryTts, monkeypatch: pytest.MonkeyPatch
):
    f = queue(paths)
    channel_on(paths)

    def locked(*a: Any, **kw: Any) -> None:
        raise NsError("NS-E119", "channels\\c.yaml: still open in another program after 20 s")

    with monkeypatch.context() as m:
        m.setattr(voice_setup, "save_channel", locked, raising=False)
        assert run(night_job(tmp_home, tts, load_channel(paths, "c"))) == ["voice"]
    assert tts.spoken == [NEW]  # the voice the request made for the name this job holds
    assert voice_of(paths) == OLD and kept(f) == (NEW, OLD)  # still owed, and still queued
    (note,) = notes(paths)
    assert note.startswith(f"NS-E119: voice {NEW} is created") and "the next run then finishes it" in note
    assert run(night_job(tmp_home, tts, load_channel(paths, "c"))) == ["voice"]  # the next run does
    assert voice_of(paths) == NEW and not f.exists() and tts.spoken == [NEW, NEW]
    assert notes(paths) == [note, SAID_C] and service.made == [NEW]


def test_a_rename_whose_note_could_not_be_left_is_said_by_the_next_run(
    tmp_home: Path, paths: Paths, service: Service, tts: LibraryTts, monkeypatch: pytest.MonkeyPatch
):
    f = queue(paths)
    channel_on(paths)

    def held(*a: Any, **kw: Any) -> None:
        raise PermissionError(13, "The process cannot access the file", "morning_note.json")

    with monkeypatch.context() as m:
        m.setattr(notify, "leave_note", held)
        assert run(night_job(tmp_home, tts, load_channel(paths, "c"))) == ["voice"]
    assert tts.spoken == [NEW] and voice_of(paths) == NEW
    assert notes(paths) == [] and f.exists()  # not said yet, so not forgotten yet
    assert run(night_job(tmp_home, tts, load_channel(paths, "c"))) == ["voice"]
    assert notes(paths) == [SAID_C] and not f.exists() and service.made == [NEW]


def test_studio_run_and_the_nightly_say_the_same_sentence_on_the_console_and_in_the_note(
    tmp_home: Path, paths: Paths, service: Service, tts: LibraryTts, capsys: pytest.CaptureFixture[str]
):
    f = queue(paths)
    channel_on(paths)
    ctx = night_job(tmp_home, tts, load_channel(paths, "c"))
    env = jobs.Env(ctx.paths, ctx.db, ctx.runtime, ctx.services, ctx.log, frozenset())  # a real run: no fake
    cli._setup_voice(env)  # `studio run` and `studio run --daily` start here, before any channel is loaded
    assert SAID_C in capsys.readouterr().out
    assert notes(paths) == [SAID_C] and voice_of(paths) == NEW and not f.exists()
    cli._setup_voice(env)
    assert capsys.readouterr().out == "" and notes(paths) == [SAID_C]
    # a refusal: the warning and the note are the same sentence, and neither says "try: apply-pending"
    notify.dismiss(paths, [n["id"] for n in notify.read_notes(paths)])
    queue(paths)
    channel_on(paths)
    cli._setup_voice(env)
    err = capsys.readouterr().err
    (note,) = notes(paths)
    assert note.startswith("NS-E310: setup asked for a voice") and WAY_OUT in note
    assert f"warning: {note}" in err and "apply-pending" not in err
