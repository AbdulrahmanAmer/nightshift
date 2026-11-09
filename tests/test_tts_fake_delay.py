"""H-7 addition (box F-E12): a developer-only delay on the FAKE voice engine, `NS_TTS_FAKE_DELAY_S`.

The fake engine designs and clones a voice in an instant, so a Cancel could never land while "Create it now"
runs and the lease-release path for the `studio:voice-<name>` owner could not be timed on a laptop. With the
variable set the fake waits that many seconds (capped at 120) in 0.1 s slices before answering a design, a
clone or a speech request, and the wait ends at once when the request is cancelled
(`POST /v1/audio/speech/cancel`) or the client goes away. A cancelled request leaves no half-written voice.
Default 0 changes nothing, and the real engines never read the variable.
"""

from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from ns_common.logging import NsLogger
from ns_tts import engine as engine_mod
from ns_tts.audio import write_wav
from ns_tts.engine import (
    FAKE_DELAY_CAP_S,
    FAKE_DELAY_ENV,
    FakeEngine,
    QwenttsEngine,
    ReferenceEngine,
    fake_delay_from_env,
)
from ns_tts.voices import VoiceLibrary
from tests.test_tts_batching import SENTENCES
from tests.test_tts_batching import calls as calls  # noqa: F401  (fixtures)
from tests.test_tts_batching import eng as eng  # noqa: F401
from tests.test_tts_batching import models as models  # noqa: F401
from tests.test_tts_qwentts import RYAN

pytest.importorskip("fastapi")  # fastapi is not in every minimal install

from fastapi.testclient import TestClient  # noqa: E402

from ns_tts.server import create_app  # noqa: E402

TEXT = "The door was already open when I got home."


def make_service(tmp_path: Path, delay: str | None) -> tuple[TestClient, FakeEngine, Path]:
    """The real service code around a fake engine that reads `delay` the way the voice service start does."""
    mp = pytest.MonkeyPatch()
    try:
        if delay is None:
            mp.delenv(FAKE_DELAY_ENV, raising=False)
        else:
            mp.setenv(FAKE_DELAY_ENV, delay)
        engine = FakeEngine(log=NsLogger("tts", tmp_path / "logs"))
    finally:
        mp.undo()
    engine.load()
    voices = tmp_path / "voices"
    return (
        TestClient(create_app(engine, VoiceLibrary(voices), log=NsLogger("tts", tmp_path / "logs"))),
        engine,
        voices,
    )


def reference_wav(tmp_path: Path) -> Path:
    ref = tmp_path / "ref.wav"
    write_wav(ref, (0.2 * np.sin(np.arange(24000) / 10.0)).astype(np.float32), 24000)
    return ref


def request_for(kind: str, tmp_path: Path) -> tuple[str, dict[str, Any]]:
    if kind == "design":
        return "/v1/voices/design", {"name": "velvet", "description": "deep calm male narrator"}
    if kind == "clone":
        body = {"name": "velvet", "ref_audio_path": str(reference_wav(tmp_path)), "ref_text": "hi there"}
        return "/v1/voices/clone", {**body, "rights_confirmed": True}
    if kind == "speech":
        return "/v1/audio/speech", {"input": TEXT, "voice": "Ryan", "speed": 1.0}
    items = [{"id": "p1", "input": TEXT, "voice": "Ryan"}]
    return "/v1/audio/speech/batch", {"items": items, "out_dir": str(tmp_path / "raw")}


KINDS = ["design", "clone", "speech", "batch"]


def nothing_written(tmp_path: Path) -> bool:
    """No voice folder (not even the temporary one) and no clip or resume marker of a batch."""
    voices, raw = tmp_path / "voices", tmp_path / "raw"
    return not (voices.exists() and any(voices.iterdir())) and not (raw.exists() and any(raw.iterdir()))


# ---------------------------------------------------------------- the variable


def test_the_variable_is_read_as_seconds_and_capped_at_120(monkeypatch: pytest.MonkeyPatch):
    assert FAKE_DELAY_ENV == "NS_TTS_FAKE_DELAY_S" and FAKE_DELAY_CAP_S == 120.0
    for raw, want in [
        ("2.5", 2.5), ("0.1", 0.1), (" 7 ", 7.0), ("120", 120.0), ("121", 120.0), ("99999", 120.0),
        ("inf", 120.0),
        ("0", 0.0), ("", 0.0), ("-3", 0.0), ("abc", 0.0), ("nan", 0.0), ("1e2", 100.0),
    ]:  # fmt: skip
        monkeypatch.setenv(FAKE_DELAY_ENV, raw)
        assert fake_delay_from_env() == want, raw
    monkeypatch.delenv(FAKE_DELAY_ENV)
    assert fake_delay_from_env() == 0.0  # default: no delay


def test_a_value_that_is_not_a_number_is_said_once_not_ignored_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(FAKE_DELAY_ENV, "soon")
    log = NsLogger("tts", tmp_path / "logs")
    assert FakeEngine(log=log).delay_s == 0.0
    text = "".join(f.read_text(encoding="utf-8") for f in (tmp_path / "logs").glob("*.jsonl"))
    assert "fake_delay_ignored" in text and "soon" in text


def test_the_engine_reads_the_variable_once_when_it_is_made_and_the_cap_holds(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(FAKE_DELAY_ENV, "100000")
    engine = FakeEngine()
    assert engine.delay_s == 120.0
    monkeypatch.setenv(FAKE_DELAY_ENV, "1")
    assert (
        engine.delay_s == 120.0 and FakeEngine().delay_s == 1.0
    )  # a running service keeps what it started with


def test_health_names_the_delay_only_when_there_is_one(tmp_path: Path):
    c, _, _ = make_service(tmp_path / "a", "5")
    assert c.get("/health").json()["fake_delay_s"] == 5.0
    c, _, _ = make_service(tmp_path / "b", "100000")
    assert c.get("/health").json()["fake_delay_s"] == 120.0  # the cap is what runs, and what health says
    c, _, _ = make_service(tmp_path / "c", None)
    assert "fake_delay_s" not in c.get("/health").json()


# ---------------------------------------------------------------- default 0: nothing changes


@pytest.mark.parametrize("kind", KINDS)
def test_with_no_delay_every_request_answers_at_once_and_nothing_waits(tmp_path: Path, kind: str):
    c, engine, _ = make_service(tmp_path, None)
    assert engine.delay_s == 0.0
    route, body = request_for(kind, tmp_path)
    t0 = time.monotonic()
    r = c.post(route, json=body)
    assert r.status_code == 200, r.text
    assert time.monotonic() - t0 < 2.0
    assert engine.waits == 0  # the wait loop never ran
    assert c.post("/v1/audio/speech/cancel").json() == {"cancelled": False}  # nothing to stop, as before


# ---------------------------------------------------------------- the delay


@pytest.mark.parametrize("kind", KINDS)
def test_design_clone_and_speech_wait_that_long_once_and_then_answer(tmp_path: Path, kind: str):
    c, engine, _ = make_service(tmp_path, "0.6")
    route, body = request_for(kind, tmp_path)
    t0 = time.monotonic()
    r = c.post(route, json=body)
    took = time.monotonic() - t0
    assert r.status_code == 200, r.text
    assert 0.55 <= took < 4.0, took  # waited; `waits == 1` below says it was once
    assert engine.waits == 1 and engine.delay_s == 0.6


def test_the_answer_after_the_delay_is_the_same_answer(tmp_path: Path):
    slow, _, _ = make_service(tmp_path / "slow", "0.3")
    fast, _, _ = make_service(tmp_path / "fast", None)
    a = slow.post("/v1/audio/speech", json={"input": TEXT, "voice": "Ryan"})
    b = fast.post("/v1/audio/speech", json={"input": TEXT, "voice": "Ryan"})
    assert a.status_code == b.status_code == 200 and a.content == b.content


# ---------------------------------------------------------------- cancel during the delay


def run_in_thread(c: TestClient, route: str, body: dict[str, Any]) -> tuple[threading.Thread, dict[str, Any]]:
    got: dict[str, Any] = {}

    def go() -> None:
        t0 = time.monotonic()
        got["response"] = c.post(route, json=body)
        got["took"] = time.monotonic() - t0

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, got


def wait_until(check: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize("kind", KINDS)
def test_a_request_cancelled_during_the_delay_ends_promptly_and_writes_nothing(tmp_path: Path, kind: str):
    c, engine, _ = make_service(tmp_path, "30")
    route, body = request_for(kind, tmp_path)
    t, got = run_in_thread(c, route, body)
    assert wait_until(lambda: engine.waiting), "the request never reached the delay"
    time.sleep(0.25)
    cancelled = c.post("/v1/audio/speech/cancel")
    assert cancelled.json() == {"cancelled": True}  # the service says it stopped something
    t.join(5.0)
    assert not t.is_alive(), "the cancelled request is still waiting"
    assert got["took"] < 3.0, got["took"]  # well under the 30 s delay
    r = got["response"]
    assert r.status_code == 500 and r.json()["error"] == "NS-E310" and "cancelled" in r.json()["detail"]
    assert nothing_written(tmp_path)  # no voice folder, no .tmp-velvet, no wav, no .sig
    assert not engine.waiting
    assert c.post("/v1/audio/speech/cancel").json() == {"cancelled": False}  # and nothing is left to stop


def test_the_service_is_free_for_the_next_request_after_a_cancel(tmp_path: Path):
    c, engine, voices = make_service(tmp_path, "30")
    route, body = request_for("design", tmp_path)
    t, got = run_in_thread(c, route, body)
    assert wait_until(lambda: engine.waiting)
    assert c.post("/v1/audio/speech/cancel").json() == {"cancelled": True}
    t.join(5.0)
    assert not t.is_alive()
    engine.delay_s = 0.0  # a developer who stopped needing the delay
    r = c.post(route, json=body)  # the same name is free again: nothing half-written stayed behind
    assert r.status_code == 200 and (voices / "velvet" / "voice.json").is_file()


# ---------------------------------------------------------------- the client goes away during the delay


def serve(app: Any) -> tuple[Any, threading.Thread, int]:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert wait_until(lambda: server.started, 10.0), "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, port


@pytest.mark.parametrize("kind", ["design", "clone", "batch"])
def test_a_client_that_gives_up_during_the_delay_frees_the_service_and_leaves_nothing(
    tmp_path: Path, kind: str
):
    """What the dashboard's Cancel does to a running voice job: the child dies, its connection closes."""
    c, engine, voices = make_service(tmp_path, "30")
    server, thread, port = serve(c.app)
    route, body = request_for(kind, tmp_path)
    try:
        t0 = time.monotonic()
        with pytest.raises(httpx.TimeoutException):
            httpx.post(
                f"http://127.0.0.1:{port}{route}", json=body, timeout=0.7
            )  # gives up, closes the socket
        assert wait_until(lambda: engine.waits == 1 and not engine.waiting, 10.0), (
            "still waiting after the client left"
        )
        assert time.monotonic() - t0 < 12.0  # not 30 s
        assert nothing_written(tmp_path)
        engine.delay_s = 0.0
        design = httpx.post(
            f"http://127.0.0.1:{port}/v1/voices/design",
            json={"name": "after", "description": "calm"},
            timeout=10,
        )
        assert design.status_code == 200 and (voices / "after" / "voice.json").is_file()  # lock released
    finally:
        server.should_exit = True
        thread.join(10.0)


# ---------------------------------------------------------------- the real engines ignore it


def test_the_real_engines_never_read_the_variable(
    eng: QwenttsEngine,
    calls: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
):
    monkeypatch.setenv(FAKE_DELAY_ENV, "60")
    assert not hasattr(eng, "delay_s") and not hasattr(eng, "waiting")
    t0 = time.monotonic()
    out = eng.synth_batch(SENTENCES[:2], RYAN, seed=1)
    assert len(out) == 2 and time.monotonic() - t0 < 30  # nowhere near 60 s
    for cls in (QwenttsEngine, ReferenceEngine):
        assert FAKE_DELAY_ENV not in inspect.getsource(cls) and "fake_delay" not in inspect.getsource(cls)
    source = inspect.getsource(engine_mod)
    holders = {
        name
        for name, obj in vars(engine_mod).items()
        if inspect.isclass(obj)
        and obj.__module__ == engine_mod.__name__
        and FAKE_DELAY_ENV in inspect.getsource(obj)
    }
    assert holders <= {"FakeEngine"} and FAKE_DELAY_ENV in source  # only the fake engine's code names it
