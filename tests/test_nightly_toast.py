"""Gap 14 (spec J10): the nightly run ends with a toast, honouring Settings > Notifications."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ns_common.config import notifications_enabled
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_studio import daily, jobs, notifications
from ns_studio import orchestrator as orch
from ns_studio.channels import crud
from ns_studio.cli import app
from ns_studio.stages import REGISTRY
from tests.helpers import add_output
from tests.test_cadence import _channel, _env
from tests.test_review import _env as _review_env

NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)


class Toasts:
    def __init__(self, ok: bool = True, boom: bool = False) -> None:
        self.shown: list[tuple[str, str]] = []
        self.ok, self.boom = ok, boom

    def __call__(self, title: str, message: str) -> bool:
        if self.boom:
            raise RuntimeError("no shell")
        self.shown.append((title, message))
        return self.ok


def _setup(tmp_home: Path, **kw):
    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only", shorts_per_day_max=2, **kw)  # two Shorts due tonight
    return jobs.open_env(light=True), db


def _makes_videos(db):
    def run(ctx):
        add_output(db, ctx.job_id, "s1", channel=ctx.channel.slug)
        return ["x"]

    return run


def test_finished_night_toasts_what_is_ready(tmp_home: Path):
    env, db = _setup(tmp_home)
    toasts = Toasts()
    report = daily.run_daily(env, now=NOW, rng=random.Random(1), run_fn=_makes_videos(db), notifier=toasts)
    assert len(report["jobs"]) == 2 and report["failures"] == []
    assert toasts.shown == [("Nightshift", "2 Shorts ready for review")]


def test_message_wording_matches_the_spec():
    ok = {"jobs": [{"job_id": "a"}], "failures": []}
    assert notifications.nightly_message(ok, 1, 5) == "1 master + 5 Shorts ready for review"
    assert notifications.nightly_message(ok, 0, 1) == "1 Short ready for review"
    assert notifications.nightly_message(ok, 0, 0) == "1 job finished, open Dashboard to review"
    bad = {"jobs": [], "failures": [{"code": "NS-E203"}]}
    assert notifications.nightly_message(bad, 0, 0) == "run failed (NS-E203), open Dashboard"
    mixed = {"jobs": [{"job_id": "a"}], "failures": [{"code": "NS-E304"}, {"code": "NS-E304"}]}
    assert notifications.nightly_message(mixed, 1, 5) == (
        "1 master + 5 Shorts ready for review; 2 jobs failed (NS-E304)"
    )
    assert notifications.nightly_message({"jobs": [], "failures": []}, 0, 0) is None  # nothing due: silence
    assert notifications.failure_message("NS-E202") == "run failed (NS-E202), open Dashboard"


def test_failed_night_toasts_the_error_code(tmp_home: Path):
    env, _ = _setup(tmp_home)
    toasts = Toasts()

    def fail(ctx):
        raise NsError("NS-E203", "llama not healthy")

    report = daily.run_daily(env, now=NOW, rng=random.Random(1), run_fn=fail, notifier=toasts)
    assert report["failures"]
    assert toasts.shown == [("Nightshift", "run failed (NS-E203), open Dashboard")]


def test_settings_toggle_off_suppresses_the_toast(tmp_home: Path):
    env, db = _setup(tmp_home)
    paths = Paths.from_env()
    (paths.config / "dashboard.json").write_text(json.dumps({"notifications": False}), encoding="utf-8")
    toasts = Toasts()
    daily.run_daily(env, now=NOW, rng=random.Random(1), run_fn=_makes_videos(db), notifier=toasts)
    assert toasts.shown == []


def test_no_notifier_means_no_toast_and_no_error(tmp_home: Path):
    env, db = _setup(tmp_home)
    assert daily.run_daily(env, now=NOW, rng=random.Random(1), run_fn=_makes_videos(db))["jobs"]


def test_a_failing_notifier_never_breaks_the_night(tmp_home: Path):
    env, db = _setup(tmp_home)
    report = daily.run_daily(
        env, now=NOW, rng=random.Random(2), run_fn=_makes_videos(db), notifier=Toasts(boom=True)
    )
    assert report["backup"] and len(report["jobs"]) == 2


def test_notifications_enabled_reads_the_dashboard_settings(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    f = paths.config / "dashboard.json"
    assert notifications_enabled(paths) is True  # no file: the dashboard's default is on
    f.write_text(json.dumps({"run_time": "03:00", "notifications": False}), encoding="utf-8")
    assert notifications_enabled(paths) is False
    f.write_text(json.dumps({"notifications": True}), encoding="utf-8")
    assert notifications_enabled(paths) is True
    f.write_text("{not json", encoding="utf-8")
    assert notifications_enabled(paths) is True
    f.write_text(json.dumps({"notifications": "no"}), encoding="utf-8")
    assert notifications_enabled(paths) is True


def test_default_notifier_is_the_supervisor_toast_or_note():
    from ns_supervisor.notify import toast_or_note

    assert notifications.default_notifier() is toast_or_note


class _StubStage:
    name = "d20_stub"
    tenant = None
    inputs = ()
    outputs = ("o.json",)

    def run(self, ctx):
        ctx.write_json("o.json", {})


def _nightly_cli(tmp_home: Path, monkeypatch: pytest.MonkeyPatch, toasts: Toasts, *, notifications_on: bool):
    """`studio run --daily --fake-services` (every service faked, as on a dev or engine-test install)."""
    monkeypatch.setitem(REGISTRY, "d20_stub", _StubStage())
    monkeypatch.setitem(orch.FORMAT_STAGES, "shorts_only", ["d20_stub"])
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    monkeypatch.setattr("ns_studio.policy.gate.available", lambda: False)
    monkeypatch.setattr(notifications, "default_notifier", lambda: toasts)
    paths, db = _review_env(tmp_home)
    crud.edit_channel(paths, db, "after-dark", {"format": "shorts_only"})
    (paths.config / "dashboard.json").write_text(
        json.dumps({"notifications": notifications_on}), encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["run", "--daily", "--fake-services"])
    assert result.exit_code == 0, result.output
    assert "done: 1 stages" in result.output
    logged = "".join(f.read_text(encoding="utf-8") for f in sorted(paths.logs.glob("studio-*.jsonl")))
    return [json.loads(line) for line in logged.splitlines() if line.strip()]


def test_d20_fake_services_with_notifications_on_still_toast(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    toasts = Toasts()
    events = _nightly_cli(tmp_home, monkeypatch, toasts, notifications_on=True)
    assert toasts.shown == [("Nightshift", "1 job finished, open Dashboard to review")]
    toast = [e for e in events if e["event"] == "toast"]
    assert len(toast) == 1 and toast[0]["data"]["shown"] is True
    assert not [e for e in events if e["event"] == "toast_skipped"]


def test_d20_notifications_off_skips_the_toast_and_says_why(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    toasts = Toasts()
    events = _nightly_cli(tmp_home, monkeypatch, toasts, notifications_on=False)
    assert toasts.shown == []
    skipped = [e for e in events if e["event"] == "toast_skipped"]
    assert len(skipped) == 1 and skipped[0]["data"]["reason"] == "notifications_off"


def test_d20_no_notifier_on_this_machine_is_logged(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    paths = Paths.from_env()
    paths.ensure()
    log = NsLogger("studio", paths.logs, echo=False)
    monkeypatch.setattr(notifications, "default_notifier", lambda: None)
    assert notifications.nightly_notifier(paths, log) is None
    (logfile,) = paths.logs.glob("studio-*.jsonl")
    (entry,) = [json.loads(x) for x in logfile.read_text(encoding="utf-8").splitlines()]
    assert entry["event"] == "toast_skipped" and entry["data"]["reason"] == "no_notifier"
