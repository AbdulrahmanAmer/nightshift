"""Engine worked proof (card vram-busy, box F-E22): another program holding the graphics card's memory is
found before a model loads, on the simulated graphics card (`fake_gpu`, H-10).

Run on an engine-test install with `fake_gpu` in effect (docs/DEV_MODE.md, "A simulated graphics card"): the
engine reads the card through the same nvidia-smi text the GPU PC prints (per-process memory `[N/A]`, as
under WDDM), so its check, its wait and its words run unchanged above it. The proof writes a program called
`game.exe` into `run\\fake_gpu.json`, holding all but 256 MiB of what is free, and then:
1. a start that is not the nightly (owner `dashboard:proof-vram`, as a Dashboard job's start or Nightshift
   Code) is refused at once with NS-E208 naming `game.exe`, and nothing is loaded;
   (each model was loaded once with the card free before, so its need is MEASURED: an estimate never
   refuses a load);
2. the nightly's load (`vram_wait: true`, as `studio run --daily` asks) waits, `/status` says
   `gpu_wait` with the Queue's sentence; the proof frees the memory in the file and the load goes ahead;
3. the same with the memory left held and a short `timeout_s` (the night's own time for the wait): NS-E208,
   saying how long it waited.

NOT shown here: the Dashboard page and the nightly run themselves (the engine is called with the bodies they
send; their words and the morning note are shown by tests/test_vram_busy.py), real driver memory numbers, a
real game or browser, and the measured need of the real story model (all the GPU PC's, box F-F16).
"""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROOF = {
    "id": "vram-busy",
    "boxes": ["F-E22"],
    "install": "engine",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 900,
    "fakes": [],
}

NIGHTLY = "studio:proof-vram"
REFUSED_WITHIN_S = 10.0
#: The engine looks again every 120 s on an install (a test's engine at once): the load goes ahead within it.
LOADED_WITHIN_S = 240.0


def engine(ctx, method, path, body=None, timeout=600) -> tuple[int, dict]:
    """The engine's own HTTP API on this PC, with no proxy: `(status, the JSON object)`."""
    url = f"http://127.0.0.1:{ctx.ports['supervisor']}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            code, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read()
    try:
        value = json.loads(raw or b"null")
    except ValueError:
        value = raw.decode("utf-8", "replace")
    return code, value if isinstance(value, dict) else {"answer": value}


def write_card(home, game_mb):
    """`run\\fake_gpu.json`: one program not Nightshift's, `game.exe`, holding `game_mb` MiB (0: none)."""
    f = Path(home) / "run" / "fake_gpu.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    others = [{"pid": 4242, "name": "game.exe", "type": "C", "mb": game_mb}] if game_mb else []
    f.write_text(json.dumps({"others": others}), encoding="utf-8")


def run(ctx):
    ctx.step(
        "scope",
        covers="POST /acquire refused with NS-E208 for a start that is not the nightly, the nightly's wait "
        "(GET /status gpu_wait) ending in a load once the memory is free, the nightly giving up at its bound",
        not_covered="the Dashboard page, the nightly run and its morning note (tests/test_vram_busy.py), a "
        "real graphics card, its driver's numbers and a real game or browser (box F-F16, the GPU PC)",
    )
    cfg = ctx.read_json(Path(ctx.home) / "config" / "runtime.json")
    if not (isinstance(cfg, dict) and cfg.get("fake_gpu") is True):
        ctx.skip("needs fake_gpu: true in config/runtime.json of an engine-test install (docs/DEV_MODE.md)")

    engine(ctx, "POST", "/stop_all", {})
    write_card(ctx.home, 0)
    code, st = engine(ctx, "GET", "/status")
    gpu = st.get("gpu") or {} if isinstance(st, dict) else {}
    ctx.check(
        code == 200 and gpu.get("simulated") is True,
        "the engine reports the simulated graphics card",
        gpu=gpu,
    )
    total, used = gpu.get("vram_total_mb"), gpu.get("vram_used_mb")
    ctx.check(
        isinstance(total, int) and isinstance(used, int), "the card's memory is read", total=total, used=used
    )
    game = int(total or 0) - int(used or 0) - 256  # all but 256 MiB of what is free: no model fits beside it
    # Only a MEASURED need refuses a load (an estimate never does): each model is loaded once with the card
    # free, which measures it, as a first night would.
    for service, alias in (("llama", "code"), ("tts", None)):
        code, got = engine(ctx, "POST", "/acquire", {"service": service, "owner": "proof-measure",
                                                     "model_alias": alias, "wait": False})  # fmt: skip
        engine(ctx, "POST", "/release", {"service": service, "owner": "proof-measure"})
        ctx.check(code == 200, f"{service} loads once with the card free (its need is measured)", got=got)
    write_card(ctx.home, game)
    ctx.step("game.exe holds the card's memory", game_mb=game, total_mb=total)

    t0 = time.monotonic()
    code, got = engine(ctx, "POST", "/acquire", {"service": "llama", "owner": "dashboard:proof-vram",
                                                 "model_alias": "code", "wait": False})  # fmt: skip
    took = round(time.monotonic() - t0, 2)
    ctx.check(
        code == 409
        and got.get("code") == "NS-E208"
        and "game.exe" in str(got.get("detail"))
        and took <= REFUSED_WITHIN_S,
        "the Dashboard's start is refused at once with NS-E208 naming game.exe",
        code=code,
        got=got,
        seconds=took,
    )
    code, st = engine(ctx, "GET", "/status")
    llama = (st.get("services") or {}).get("llama") or {}
    ctx.check(st.get("gpu_owner") is None and llama.get("state") != "ready",
              "nothing was loaded and the card is free for the next owner",
              owner=st.get("gpu_owner"), llama=llama.get("state"))  # fmt: skip

    answer: dict = {}

    def nightly(body: dict) -> None:
        answer["code"], answer["got"] = engine(ctx, "POST", "/acquire", body)
        answer["at"] = time.monotonic()

    body = {"service": "llama", "owner": NIGHTLY, "model_alias": "story", "wait": False, "vram_wait": True,
            "timeout_s": 600}  # fmt: skip
    worker = threading.Thread(target=nightly, args=(body,), daemon=True)
    worker.start()
    wait = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and wait is None and worker.is_alive():
        code, st = engine(ctx, "GET", "/status")
        wait = st.get("gpu_wait") if code == 200 else None
        time.sleep(0.25)
    said = str((wait or {}).get("said") or "")
    ctx.check(
        said.startswith(("Waiting for the graphics card's memory", "simulated (fake_gpu): Waiting"))
        and "game.exe" in said
        and "Windows does not say how much each uses" in said,
        "the nightly waits, and /status says gpu_wait with the Queue's sentence",
        said=said,
        wait=wait,
    )
    write_card(ctx.home, 1000)
    freed = time.monotonic()
    ctx.step("game.exe now holds 1000 MiB")
    worker.join(LOADED_WITHIN_S + 30)
    ok = answer.get("code") == 200 and answer.get("at", freed + 1e9) - freed <= LOADED_WITHIN_S
    ctx.check(ok, "the nightly's load goes ahead once the memory is free", answer=answer)
    code, st = engine(ctx, "GET", "/status")
    ctx.check(st.get("gpu_owner") == NIGHTLY and st.get("gpu_wait") is None,
              "/status names the nightly as the holder and no wait any more",
              owner=st.get("gpu_owner"), wait=st.get("gpu_wait"))  # fmt: skip

    engine(ctx, "POST", "/release", {"service": "llama", "owner": NIGHTLY})
    write_card(ctx.home, game)  # (the game first: the stop below then gives back only the model's memory)
    engine(ctx, "POST", "/stop_all", {})
    t0 = time.monotonic()
    code, got = engine(ctx, "POST", "/acquire", {**body, "timeout_s": 5})
    took = round(time.monotonic() - t0, 2)
    ctx.check(
        code == 409 and got.get("code") == "NS-E208" and "waited" in str(got.get("detail")) and took < 60,
        "the nightly gives up at its bound with NS-E208, saying how long it waited",
        code=code,
        got=got,
        seconds=took,
    )
    code, st = engine(ctx, "GET", "/status")
    ctx.check(st.get("gpu_owner") is None, "the card is free again after the nightly gave up",
              owner=st.get("gpu_owner"))  # fmt: skip
    write_card(ctx.home, 0)
    kept = Path(ctx.home) / "run" / "vram-need.json"
    need = ctx.read_json(kept) if kept.exists() else None
    measured = [e.get("service") for e in (need or {}).get("entries", []) if e.get("mb")]
    ctx.check({"llama", "tts"} <= set(measured), "both measured needs are kept in run/vram-need.json",
              need=need)  # fmt: skip
