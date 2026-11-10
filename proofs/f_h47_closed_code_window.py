"""Engine worked proof (H-10, box F-E30): with the simulated graphics card, the engine frees the card within
one monitor pass when the process whose pid was given at `/acquire` ends, and grants the next job at once.

Run on an engine-test install with `fake_gpu` in effect (docs/DEV_MODE.md, "A simulated graphics card"), so
the engine treats llama as a GPU tenant with the GPU PC's lock, guard, holder probe and lease code. The holder
is a stand-in process (`proofs/_fakes/console_holder.py`): it is acquired for with its pid in the same body
`launchers/nightshift-code.ps1` sends (`/acquire llama owner opencode pid`), and ended by that pid
(TerminateProcess). The effect is what the engine answers on `/status` (`gpu_owner`), and the next owner's
grant without waiting.

NOT shown, so box F-H47 is not claimed here: that closing the REAL Nightshift Code window with X ends the
pid the launcher sent, in time. The launcher, its console window and OpenCode are not run; that half stays
open for the GPU PC (F-H47 in docs/FEATURE_STATUS.md).
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROOF = {
    "id": "f-h47-closed-code-window",
    "boxes": ["F-E30"],  # not F-H47: no real launcher or window here (that is the GPU PC's open half)
    "install": "engine",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 300,
    "fakes": [],
}

#: The card says "within 5 s" for a closed console (the engine's monitor passes once a second).
FREED_WITHIN_S = 5.0
NIGHTLY = "studio:proof-fh47"


def engine(ctx, method, path, body=None, timeout=180) -> tuple[int, dict]:
    """The engine's own HTTP API on this PC, with no proxy: `(status, the JSON object)`; an answer that is not
    a JSON object comes back as `{"answer": <text>}`."""
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


def released_lines(home, owner, since):
    """`owner_released` WARN lines of the engine log for `owner` with reason `holder_gone`, after `since`."""
    found = []
    for f in sorted((Path(home) / "logs").glob("supervisor-*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if "owner_released" not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            data = rec.get("data") or {}
            if (rec.get("event") == "owner_released" and data.get("owner") == owner
                    and data.get("reason") == "holder_gone" and str(rec.get("ts", "")) >= since):
                found.append(rec)
    return found


def run(ctx):
    ctx.step(
        "scope",
        covers="POST /acquire llama owner opencode with the holder's pid, the holder ended by that pid, "
        "GET /status gpu_owner, the next owner's grant without waiting",
        not_covered="box F-H47: closing the real Nightshift Code window with X (the launcher, its console "
        "and OpenCode are not run; the /acquire body is pinned by tests/test_launchers.py), a real graphics "
        "card (docs/DEV_MODE.md: what fake_gpu does not prove)",
    )
    cfg = ctx.read_json(Path(ctx.home) / "config" / "runtime.json")
    if not (isinstance(cfg, dict) and cfg.get("fake_gpu") is True):
        ctx.skip("needs fake_gpu: true in config/runtime.json of an engine-test install (docs/DEV_MODE.md)")

    code, st = engine(ctx, "GET", "/status")
    ctx.check(code == 200 and isinstance(st, dict), "GET /status answers 200 with an object", code=code)
    gpu = st.get("gpu") or {}
    ctx.check(gpu.get("simulated") is True, "the engine reports the simulated graphics card", gpu=gpu)
    ctx.check(st.get("gpu_owner") is None, "nobody holds the card before the proof",
              owner=st.get("gpu_owner"))

    holder = ctx.spawn([sys.executable, str(Path(__file__).parent / "_fakes" / "console_holder.py"), "300"],
                       name="code-console")
    code, got = engine(ctx, "POST", "/acquire", {
        "service": "llama", "owner": "opencode", "model_alias": "code", "wait": False, "pid": holder.pid,
    })
    ctx.check(code == 200, "the Code console takes the card (llama, owner opencode, its pid)",
              code=code, got=got)
    code, st = engine(ctx, "GET", "/status")
    ctx.check(
        st.get("gpu_owner") == "opencode" and st.get("holder_pid") == holder.pid,
        "/status names opencode as the holder, with the console's pid",
        owner=st.get("gpu_owner"), holder_pid=st.get("holder_pid"), console_pid=holder.pid,
    )

    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 1))
    holder.kill()  # the console is closed with X: the process ends, no `finally` runs
    holder.wait(timeout=10)
    closed = time.monotonic()
    ctx.step("the console is closed (its process ended by its pid)", pid=holder.pid)
    took = None
    owner = "opencode"
    while time.monotonic() - closed < FREED_WITHIN_S + 5:  # looked at four times a second, a bounded wait
        code, st = engine(ctx, "GET", "/status")
        owner = st.get("gpu_owner") if code == 200 and isinstance(st, dict) else owner
        if code == 200 and owner is None:
            took = round(time.monotonic() - closed, 2)
            break
        time.sleep(0.25)
    ctx.check(
        took is not None and took <= FREED_WITHIN_S,
        f"the card is free within {FREED_WITHIN_S:.0f} s of the console closing",
        seconds=took, owner=owner,
    )
    ctx.check(
        bool(released_lines(ctx.home, "opencode", since)),
        "the engine log says owner_released opencode, reason holder_gone",
    )

    t0 = time.monotonic()
    code, got = engine(ctx, "POST", "/acquire", {"service": "llama", "owner": NIGHTLY, "wait": False})
    ctx.check(code == 200, "the next nightly job gets the card at once (wait false)", code=code, got=got,
              seconds=round(time.monotonic() - t0, 2))
    code, st = engine(ctx, "GET", "/status")
    ctx.check(st.get("gpu_owner") == NIGHTLY, "/status names the nightly job as the holder",
              owner=st.get("gpu_owner"))
    code, rel = engine(ctx, "POST", "/release", {"service": "llama", "owner": NIGHTLY})
    ctx.check(code == 200 and rel == {"ok": True}, "the proof gives the card back", code=code, got=rel)
