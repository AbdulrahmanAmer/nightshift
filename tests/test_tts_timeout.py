"""Pre-tag item 1 (system-gaps row 1): the voice stage's timeout scales with the batch, the server never
stops a process before the client gives up, a client timeout cancels the batch, a retry resumes."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.tts_client import FakeTts, TtsClient, TtsItem
from ns_common.tts_timeout import (
    TTS_TIMEOUT_CAP_S,
    batch_timeout_s,
    count_lines,
    tts_timeout_s,
)
from ns_tts.engine import BatchInterrupted, FakeEngine, QwenttsEngine
from ns_tts.voices import VoiceLibrary
from tests.helpers import make_ctx
from tests.test_tts_batching import SENTENCES, _client
from tests.test_tts_batching import calls as calls  # noqa: F401  (fixtures)
from tests.test_tts_batching import eng as eng  # noqa: F401
from tests.test_tts_batching import models as models  # noqa: F401
from tests.test_tts_qwentts import RYAN, recorded
from tests.test_voice_stage import PARTS

# ---------------------------------------------------------------- the formula


def test_the_formula_is_120_s_plus_3_s_per_line_capped_at_two_hours():
    assert tts_timeout_s(0) == 120
    assert tts_timeout_s(200) == 120 + 600
    assert tts_timeout_s(10**6) == TTS_TIMEOUT_CAP_S == 7200
    assert tts_timeout_s(-5) == 120


def test_lines_are_sentences_and_every_text_counts_at_least_once():
    assert count_lines(["One. Two. Three.", "Four.", "  ", "no stop"]) == 3 + 1 + 1
    assert batch_timeout_s(["One. Two."]) == 120 + 3 * 2


# ---------------------------------------------------------------- the studio's voice stage


@pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
def test_the_voice_stage_passes_the_scaled_timeout(tmp_home: Path):
    from ns_studio.stages import REGISTRY

    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", PARTS)
    seen: list[float | None] = []
    real = ctx.services.tts.speak_batch

    def spy(items, out_dir, *, seed=None, timeout_s=None):  # type: ignore[no-untyped-def]
        seen.append(timeout_s)
        return real(items, out_dir, seed=seed, timeout_s=timeout_s)

    ctx.services.tts.speak_batch = spy  # type: ignore[method-assign]
    REGISTRY["voice"].run(ctx)
    assert seen == [batch_timeout_s([PARTS[0]["text"], PARTS[0]["hook"]])]
    assert seen[0] is not None and seen[0] > 120


# ---------------------------------------------------------------- the client


def _items() -> list[TtsItem]:
    return [TtsItem("a", "One. Two. Three.", "Ryan"), TtsItem("b", "Four.", "Ryan")]


def test_the_client_sends_its_timeout_and_resume_to_the_service(tmp_path: Path):
    bodies: list[dict[str, Any]] = []
    timeouts: list[Any] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        timeouts.append(req.extensions.get("timeout"))
        return httpx.Response(200, json={"files": []})

    cli = TtsClient("http://x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    cli.speak_batch(_items(), tmp_path)
    assert bodies[0]["timeout_s"] == 120 + 3 * 4 and bodies[0]["resume"] is True
    assert timeouts[0]["read"] == 120 + 3 * 4  # the request itself waits exactly that long


def test_a_client_timeout_asks_the_server_to_cancel_the_batch(tmp_path: Path):
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        if req.url.path.endswith("/batch"):
            raise httpx.ReadTimeout("no answer")
        return httpx.Response(200, json={"cancelled": True})

    cli = TtsClient("http://x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(NsError) as ei:
        cli.speak_batch(_items(), tmp_path)
    assert ei.value.code == "NS-E310" and "asked the service to cancel it" in ei.value.detail
    assert paths == ["/v1/audio/speech/batch", "/v1/audio/speech/cancel"]


def test_a_cancel_that_cannot_be_sent_does_not_hide_the_timeout(tmp_path: Path):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/batch"):
            raise httpx.ReadTimeout("no answer")
        raise httpx.ConnectError("down")

    cli = TtsClient("http://x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(NsError) as ei:
        cli.speak_batch(_items(), tmp_path)
    assert ei.value.code == "NS-E310" and "could not ask" in ei.value.detail


def test_another_http_error_does_not_cancel(tmp_path: Path):
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        return httpx.Response(500, text="oops")

    cli = TtsClient("http://x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(NsError):
        cli.speak_batch(_items(), tmp_path)
    assert paths == ["/v1/audio/speech/batch"]


def test_fake_tts_accepts_the_timeout(tmp_path: Path):
    assert len(FakeTts().speak_batch(_items(), tmp_path, timeout_s=300)) == 2


# ---------------------------------------------------------------- the engine


def test_the_server_limit_is_never_below_the_clients(
    tmp_path: Path, eng: QwenttsEngine, calls: Path  # noqa: F811
):
    client = _client(tmp_path, eng)
    seen: list[float] = []
    real = eng._run_stream

    def spy(cmd, **kw):  # type: ignore[no-untyped-def]
        seen.append(kw["timeout_s"])
        return real(cmd, **kw)

    eng._run_stream = spy  # type: ignore[method-assign]
    items = [{"id": "a", "input": " ".join(SENTENCES), "voice": "Ryan"}]
    body = {"items": items, "out_dir": str(tmp_path / "o")}
    assert client.post("/v1/audio/speech/batch", json=body).status_code == 200
    assert client.post("/v1/audio/speech/batch", json={**body, "timeout_s": 3000}).status_code == 200
    assert seen[0] == tts_timeout_s(5) and seen[1] == 3000.0
    assert eng.batch_timeout_floor_s == 0.0  # reset after the request


def _hang_after(monkeypatch: pytest.MonkeyPatch, n: int, limit_s: float | None = None) -> None:
    monkeypatch.setenv("NS_FAKE_QWEN_HANG_AFTER", str(n))
    if limit_s is not None:
        monkeypatch.setattr("ns_tts.engine.tts_timeout_s", lambda lines: limit_s)
        monkeypatch.setattr("ns_tts.server.tts_timeout_s", lambda lines: limit_s)


def test_a_stalled_process_is_stopped_at_the_limit_and_keeps_its_finished_utterances(
    eng: QwenttsEngine, calls: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
):
    _hang_after(monkeypatch, 2, limit_s=12)
    with pytest.raises(BatchInterrupted) as ei:
        eng.synth_batch(SENTENCES, RYAN)
    assert ei.value.code == "NS-E310" and "timed out" in ei.value.detail and not ei.value.cancelled
    assert sorted(ei.value.done) == [0, 1] and all(len(x) > 0 for x in ei.value.done.values())


def test_cancel_kills_the_running_process_and_keeps_its_finished_utterances(
    eng: QwenttsEngine, calls: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
):
    _hang_after(monkeypatch, 3)
    assert eng.cancel() is False  # nothing is running yet
    stopped: list[bool] = []

    def later() -> None:
        time.sleep(5)  # long enough for the stand-in to start and write three utterances
        stopped.append(eng.cancel())

    t = threading.Thread(target=later)
    t.start()
    t0 = time.monotonic()
    with pytest.raises(BatchInterrupted) as ei:
        eng.synth_batch(SENTENCES, RYAN)
    t.join()
    assert time.monotonic() - t0 < 60  # not the stand-in's 120 s stall
    assert stopped == [True] and ei.value.cancelled and "cancelled" in ei.value.detail
    assert sorted(ei.value.done) == [0, 1, 2]


# ---------------------------------------------------------------- the service


A = {"id": "a", "input": f"{SENTENCES[0]} {SENTENCES[1]}", "voice": "Ryan"}
B = {"id": "b", "input": f"{SENTENCES[2]} {SENTENCES[3]} {SENTENCES[4]}", "voice": "Ryan"}


def test_an_interrupted_batch_keeps_whole_items_and_the_retry_resumes_from_them(
    tmp_path: Path, eng: QwenttsEngine, calls: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
):
    client = _client(tmp_path, eng)
    out = tmp_path / "raw"
    body = {"items": [A, B], "out_dir": str(out), "resume": True}
    _hang_after(monkeypatch, 2, limit_s=12)
    r = client.post("/v1/audio/speech/batch", json=body)
    assert r.status_code == 500 and r.json()["error"] == "NS-E310"
    assert (out / "a.wav").is_file() and not (out / "b.wav").exists()  # a finished, b did not
    assert (out / "a.sig").is_file()
    first = (out / "a.wav").read_bytes()

    monkeypatch.delenv("NS_FAKE_QWEN_HANG_AFTER")
    r = client.post("/v1/audio/speech/batch", json=body)
    assert r.status_code == 200, r.text
    assert [f["id"] for f in r.json()["files"]] == ["a", "b"]
    assert (out / "a.wav").read_bytes() == first  # not synthesized again
    assert recorded(calls)[-1]["stdin"].count("\n") == 3  # only b's three sentences went to the process
    assert not list(out.glob("*.sig"))  # a finished batch leaves no resume markers


def test_without_resume_everything_is_synthesized_again(
    tmp_path: Path, eng: QwenttsEngine, calls: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
):
    client = _client(tmp_path, eng)
    body = {"items": [A, B], "out_dir": str(tmp_path / "raw")}
    _hang_after(monkeypatch, 2, limit_s=12)
    assert client.post("/v1/audio/speech/batch", json=body).status_code == 500
    monkeypatch.delenv("NS_FAKE_QWEN_HANG_AFTER")
    assert client.post("/v1/audio/speech/batch", json=body).status_code == 200
    assert recorded(calls)[-1]["stdin"].count("\n") == 5


def test_changed_text_is_not_resumed(
    tmp_path: Path, eng: QwenttsEngine, calls: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
):
    client = _client(tmp_path, eng)
    body = {"items": [A, B], "out_dir": str(tmp_path / "raw"), "resume": True}
    _hang_after(monkeypatch, 2, limit_s=12)
    assert client.post("/v1/audio/speech/batch", json=body).status_code == 500
    monkeypatch.delenv("NS_FAKE_QWEN_HANG_AFTER")
    edited = {**A, "input": "A completely different opening line for this part."}
    body["items"] = [edited, B]
    assert client.post("/v1/audio/speech/batch", json=body).status_code == 200
    assert recorded(calls)[-1]["stdin"].count("\n") == 4  # the edited a (1 line) and b (3)


def test_the_cancel_endpoint_stops_the_running_batch_and_reports_it(tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_tts.server import create_app

    class Cancelable(FakeEngine):
        stopped = 0

        def cancel(self) -> bool:
            self.stopped += 1
            return True

    e = Cancelable()
    e.load()
    c = TestClient(create_app(e, VoiceLibrary(tmp_path / "voices")))
    assert c.post("/v1/audio/speech/cancel").json() == {"cancelled": True} and e.stopped == 1
    plain = TestClient(create_app(FakeEngine(), VoiceLibrary(tmp_path / "v2")))
    assert plain.post("/v1/audio/speech/cancel").json() == {"cancelled": False}  # nothing to stop


def test_the_fake_engine_still_serves_batches_with_the_new_fields(tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_tts.server import create_app

    e = FakeEngine()
    e.load()
    c = TestClient(create_app(e, VoiceLibrary(tmp_path / "voices")))
    body = {"items": [A], "out_dir": str(tmp_path / "o"), "timeout_s": 500, "resume": True}
    r = c.post("/v1/audio/speech/batch", json=body)
    assert r.status_code == 200 and (tmp_path / "o" / "a.wav").is_file()
