"""F-B13 (Pexels/Pixabay key check, search, download) and F-B14 (a 429 is waited out, never a failure).

Both run against the QA fake Pexels/Pixabay endpoint (proofs/_fakes/footage.py, an intercepting TLS proxy that
makes its own CA at run time). Ported from runs/qa/fb13_14.py and runs/qa/fakefootage (evidence log
2026-11-09, 0.1.12 and 0.1.14).

How the product is pointed at the fake: the stock-footage calls (the key check and the search and download)
run inside the Dashboard process and read HTTPS_PROXY and SSL_CERT_FILE from its environment, which a running
dashboard cannot be given. So the proof starts the install's own dashboard app a second time on a free port
(proofs/_fakes/dashboard_no_worker.py: `create_app(start_worker=False)`, no job worker, no event relay, its
temp folder in ctx.scratch) with
those variables set through `ctx.spawn(env=...)`, and calls the real routes on it: POST /api/settings/keys and
POST /api/library/footage/fetch. The keys, the `clips` table and the cache folder are the home's own, so the
live dashboard's Settings and Footage pages read the same effect.

The proof stores the fake key `GOODKEY` for the length of the run only (it skips when a key is already stored,
so no real key is ever replaced) and removes the keys and the clips it made at the end.
"""

from __future__ import annotations

import hashlib
import json
import random
import string
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from proofs._fakes import footage
from proofs._lib import db_rows, ffmpeg_path, short

PROOF = {
    "id": "f-b13-b14-footage-429",
    "boxes": ["F-B13", "F-B14"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 420,
    "fakes": ["footage"],
}

GOOD = "GOODKEY"  # the fake accepts this one
BAD = "BADKEY"
BURST = 3  # 429s in a row, then the API answers
BURST_WAIT_S = 3  # Retry-After of each of them
ENDLESS_WAIT_S = 2  # Retry-After while every answer is a 429
ENDLESS_TRIES = 4  # the product gives up after this many 429s
SLACK_S = 0.5
KEYS = ("pexels", "pixabay")
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_LETTERS = string.ascii_lowercase.replace("s", "")  # no "s": the keyword is stored without a trailing "s"


def _http(method: str, url: str, body: Any = None, timeout: float = 300) -> tuple[int, Any, float]:
    """One HTTP call with no proxy: (status, parsed JSON or text, seconds). Network errors are raised."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    t0 = time.monotonic()
    try:
        with _NO_PROXY.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        out: Any = json.loads(text)
    except ValueError:
        out = text
    return status, out, time.monotonic() - t0


def _call(ctx: Any, method: str, url: str, body: Any = None, timeout: float = 300) -> tuple[int, Any, float]:
    """`_http`, but no answer at all fails the proof with the URL named."""
    try:
        return _http(method, url, body, timeout)
    except OSError as exc:  # URLError and timeouts are OSErrors
        ctx.check(False, f"{method} {url} answered", error=str(exc))
        raise


def _error(answer: Any) -> dict[str, Any]:
    """The `error` object of an API error answer (empty when the answer is not one)."""
    err = answer.get("error") if isinstance(answer, dict) else None
    return err if isinstance(err, dict) else {}


def _tail(path: Path, limit: int = 600) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _token() -> str:
    return "".join(random.choices(_LETTERS, k=8))


class _Fake:
    """The running fake endpoint: its control port and the folder it works in."""

    def __init__(self, ctx: Any, folder: Path, port: int) -> None:
        self.ctx = ctx
        self.folder = folder
        self.port = port

    def ctl(self, query: str) -> dict[str, Any]:
        status, out, _ = _call(self.ctx, "GET", f"http://127.0.0.1:{self.port}/__ctl?{query}", timeout=30)
        self.ctx.check(status == 200 and isinstance(out, dict), f"fake control {query}", status=status)
        return out

    def counts(self, host: str) -> dict[str, int]:
        return dict(self.ctl("stats=1").get("counts", {}).get(host, {}))


def _start_fake(ctx: Any) -> _Fake:
    folder = ctx.scratch / "footage-fake"
    folder.mkdir(parents=True, exist_ok=True)
    proc = ctx.spawn(
        [sys.executable, str(Path(footage.__file__).resolve()), "--dir", str(folder), "--good-key", GOOD],
        name="footage-fake",
    )
    ready = folder / "ready.json"

    def listening() -> dict[str, Any] | None:
        if proc.poll() is not None:
            tail = _tail(ctx.scratch / "footage-fake.log")
            ctx.check(False, "the fake footage endpoint started", exit_code=proc.returncode, log=tail)
        return json.loads(ready.read_text(encoding="ascii")) if ready.is_file() else None

    info = ctx.wait(listening, 180, every=1, what="the fake footage endpoint to listen")
    made = [p.name for sub in ("certs", "media") for p in sorted((folder / sub).iterdir())]
    ctx.step(
        "fake Pexels/Pixabay listening (own CA and clips made in the scratch folder)",
        port=info["port"],
        folder=str(folder),
        files=made,
    )
    return _Fake(ctx, folder, int(info["port"]))


def _start_dashboard(ctx: Any, fake: _Fake, python: Path) -> str:
    """The install's dashboard app (no job worker) with the fake's proxy variables; returns its base URL."""
    tmp = ctx.scratch / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = footage.client_env(fake.folder, fake.port)
    env.update({"TEMP": str(tmp), "TMP": str(tmp)})
    port_file = ctx.scratch / "dashboard.port"
    script = Path(footage.__file__).resolve().with_name("dashboard_no_worker.py")
    proc = ctx.spawn([str(python), str(script), str(port_file), str(tmp)], env=env, name="dashboard-proof")

    def listening() -> str | None:
        if proc.poll() is not None:
            tail = _tail(ctx.scratch / "dashboard-proof.log", 1500)
            ctx.check(False, "the dashboard app started", exit_code=proc.returncode, log=tail)
        if not port_file.is_file():
            return None
        base = f"http://127.0.0.1:{port_file.read_text(encoding='ascii').strip()}"
        try:
            status, _, _ = _http("GET", base + "/api/health", timeout=5)
        except OSError:
            return None
        return base if status == 200 else None

    base = ctx.wait(listening, 120, every=1, what="the dashboard app to answer /api/health")
    ctx.step(
        "install's dashboard app started with the fake as its proxy (no job worker)",
        url=base,
        proxy=env["HTTPS_PROXY"],
        ca_bundle=env["SSL_CERT_FILE"],
        python=str(python),
    )
    return base


def _stored_keys(ctx: Any) -> dict[str, bool]:
    """What Settings shows: GET /api/settings on the live dashboard, `keys`."""
    status, settings = ctx.api("GET", "/api/settings")
    ctx.check(
        status == 200 and isinstance(settings, dict) and isinstance(settings.get("keys"), dict),
        "Settings answers with the key flags",
        status=status,
    )
    return {k: bool(v) for k, v in settings["keys"].items()}


def _set_key(ctx: Any, base: str, name: str, value: str) -> tuple[int, Any]:
    status, out, _ = _call(ctx, "POST", base + "/api/settings/keys", {"name": name, "value": value}, 120)
    return status, out


def _fetch(ctx: Any, base: str, q: str, orientation: str, count: int, source: str) -> tuple[int, Any, float]:
    body = {"q": q, "orientation": orientation, "count": count, "source": source}
    return _call(ctx, "POST", base + "/api/library/footage/fetch", body, 300)


def _check_clips(
    ctx: Any, fake: _Fake, answer: dict[str, Any], source: str, size: tuple[int, int], want: int, made: list
) -> None:
    """The fetch answer, the files on disk and the `clips` rows agree, and the files are the fake's bytes."""
    clips = answer.get("clips") or []
    portrait = size[1] > size[0]
    original = _sha(footage.media_path(fake.folder, portrait))
    folder = ctx.home / "cache" / "clips" / source  # where the product downloads a source's clips
    # Record every clip for the cleanup BEFORE any check, so a failed check never leaves a clip behind.
    made.extend((c.get("source") or source, c.get("clip_id"), Path(str(c.get("path")))) for c in clips)
    ctx.check(
        answer.get("downloaded") == want and len(clips) == want and len(answer.get("paths") or []) == want,
        f"{want} {source} clip(s) downloaded",
        downloaded=answer.get("downloaded"),
        clips=len(clips),
        note=answer.get("note"),
    )
    for clip in clips:
        path = Path(clip["path"])
        found = db_rows(
            ctx,
            "select local_path, width, height, license from clips where source=? and clip_id=?",
            (source, clip["clip_id"]),
        )
        row = (
            dict(zip(("local_path", "width", "height", "license"), found[0], strict=True)) if found else None
        )
        ctx.check(
            clip.get("source") == source
            and clip.get("new") is True
            and (clip.get("width"), clip.get("height")) == size
            and path.is_file()
            and path.resolve().is_relative_to(folder.resolve())
            and _sha(path) == original
            and row is not None
            and Path(str(row["local_path"])).resolve() == path.resolve()
            and (row["width"], row["height"]) == size,
            f"{source} clip {clip['clip_id']}: file under cache/clips/{source}, the fake's bytes, row agrees",
            path=str(path),
            expected_folder=str(folder),
            bytes=path.stat().st_size if path.is_file() else None,
            size=clip.get("size"),
            row=row,
            license=clip.get("license"),
        )


def _cleanup(ctx: Any, base: str, fake: _Fake | None, stored: list[str], made: list) -> None:
    """Remove what the proof made: its clips (DELETE route), its keys (empty value clears), the fake mode."""
    for source, clip_id, _path in made:
        try:
            status, out, _ = _call(ctx, "DELETE", f"{base}/api/library/footage/{source}/{clip_id}", None, 60)
            ctx.step("cleanup: clip deleted", clip=f"{source}/{clip_id}", status=status, answer=short(out))
        except Exception as exc:  # noqa: BLE001
            ctx.step("cleanup: clip delete failed", source=source, clip_id=clip_id, error=str(exc))
    for name in stored:
        try:
            status, out = _set_key(ctx, base, name, "")
            ctx.step("cleanup: key cleared", key=name, status=status, answer=short(out))
        except Exception as exc:  # noqa: BLE001
            ctx.step("cleanup: key clear failed", key=name, error=str(exc))
    if fake is not None:
        try:
            fake.ctl("reset=1&mode=normal")
        except Exception as exc:  # noqa: BLE001
            ctx.step("cleanup: fake mode reset failed", error=str(exc))


def run(ctx: Any) -> None:
    if ffmpeg_path() is None:
        ctx.skip("FFmpeg is not on PATH: the fake endpoint cannot make its clips (hold the laptop lock)")
    problem = footage.cert_tool_problem()
    if problem:
        ctx.skip(problem)
    python = ctx.home / "venvs" / "supervisor" / "Scripts" / "python.exe"
    if not python.is_file():
        ctx.skip(f"{python} does not exist: the dashboard app cannot be started with the proxy variables")
    before = _stored_keys(ctx)
    if any(before.get(k) for k in KEYS):
        ctx.skip(f"a stock footage key is already stored ({before}); the proof never replaces a real key")
    ctx.step("no Pexels or Pixabay key is stored yet", keys=before)

    fake = _start_fake(ctx)
    base = _start_dashboard(ctx, fake, python)
    tokens = {name: _token() for name in ("forest", "street", "harbor", "road")}
    made: list[tuple[str, str, Path]] = []
    stored: list[str] = []
    try:
        _prove_b13(ctx, fake, base, tokens, made, stored)
        _prove_b14(ctx, fake, base, tokens, made)
    finally:
        # No key was stored before the run (checked above), so both are cleared whatever happened: a failed
        # check never leaves GOODKEY in the key store, which would make every later run skip.
        _cleanup(ctx, base, fake, ["pexels", "pixabay"], made)

    after = _stored_keys(ctx)
    marks = [f"%{t}%" for t in tokens.values()]
    rows = db_rows(
        ctx, "select count(*) from clips where " + " or ".join("keywords like ?" for _ in marks), tuple(marks)
    )
    ctx.check(
        not any(after.get(k) for k in KEYS) and rows[0][0] == 0 and not any(p.exists() for _, _, p in made),
        "cleanup: the proof's keys, clip rows and clip files are gone",
        keys=after,
        clip_rows=rows[0][0],
        files_left=[str(p) for _, _, p in made if p.exists()],
    )


def _prove_b13(
    ctx: Any, fake: _Fake, base: str, tokens: dict[str, str], made: list, stored: list[str]
) -> None:
    fake.ctl("reset=1&mode=normal")
    # --- Settings > Stock footage keys: a wrong key is refused, a right one accepted
    status, out = _set_key(ctx, base, "pexels", BAD)
    err = _error(out)
    ctx.check(
        status == 422 and err.get("code") == "NS-E501" and "did not accept" in str(err.get("message")),
        "Pexels key check: a wrong key is refused with 422 NS-E501",
        status=status,
        error=short(err),
    )
    seen = fake.counts("api.pexels.com")
    ctx.check(
        seen.get("401") == 1,
        "the check reached the fake Pexels (it answered 401), so the dashboard app is pointed at the fake",
        counts=seen,
    )
    ctx.check(not _stored_keys(ctx)["pexels"], "the refused key is not stored", keys=_stored_keys(ctx))

    status, out = _set_key(ctx, base, "pexels", GOOD)
    ctx.check(
        status == 200
        and out.get("set") is True
        and out.get("checked") is True
        and out.get("message") == "Pexels accepted the key",
        "Pexels key check: the right key is accepted and stored",
        status=status,
        answer=short(out),
    )
    stored.append("pexels")
    ctx.check(_stored_keys(ctx)["pexels"], "Settings shows the Pexels key as set")

    status, out = _set_key(ctx, base, "pixabay", BAD)
    err = _error(out)
    ctx.check(
        status == 422 and err.get("code") == "NS-E502",
        "Pixabay key check: a wrong key is refused with 422 NS-E502",
        status=status,
        error=short(err),
    )
    seen = fake.counts("pixabay.com")
    ctx.check(seen.get("400") == 1, "the fake Pixabay answered 400 to the wrong key", counts=seen)
    status, out = _set_key(ctx, base, "pixabay", GOOD)
    ctx.check(
        status == 200 and out.get("checked") is True and out.get("message") == "Pixabay accepted the key",
        "Pixabay key check: the right key is accepted and stored",
        status=status,
        answer=short(out),
    )
    stored.append("pixabay")

    # --- search and download through the real sources (the stored key wins over the dev fake source)
    q = f"proof forest {tokens['forest']}"
    fake.ctl("reset=1&mode=normal")
    status, answer, secs = _fetch(ctx, base, q, "portrait", 2, "pexels")
    ctx.check(
        status == 200 and isinstance(answer, dict),
        "Pexels fetch answers 200",
        status=status,
        seconds=round(secs, 1),
        answer=None if isinstance(answer, dict) else short(answer),
    )
    _check_clips(ctx, fake, answer, "pexels", (1080, 1920), 2, made)
    pexels = fake.counts("videos.pexels.com")
    ctx.check(pexels.get("200") == 2, "the fake Pexels CDN served the 2 downloads", counts=pexels)

    q = f"proof street {tokens['street']}"
    status, answer, secs = _fetch(ctx, base, q, "landscape", 1, "pixabay")
    ctx.check(
        status == 200 and isinstance(answer, dict),
        "Pixabay fetch answers 200",
        status=status,
        seconds=round(secs, 1),
        answer=None if isinstance(answer, dict) else short(answer),
    )
    _check_clips(ctx, fake, answer, "pixabay", (1920, 1080), 1, made)

    status, listed = ctx.api("GET", f"/api/library/footage?q={tokens['forest']}&limit=20")
    mine = {c["clip_id"] for s, c, _ in made if s == "pexels"}
    rows = listed if isinstance(listed, list) else []
    shown = [c for c in rows if isinstance(c, dict) and c.get("source") == "pexels"]
    ctx.check(
        status == 200
        and mine <= {c.get("clip_id") for c in shown}
        and all(c.get("license") == "Pexels License" for c in shown),
        "Library > Footage (GET /api/library/footage) lists the fetched Pexels clips with their licence",
        status=status,
        listed=[(c.get("clip_id"), c.get("license"), c.get("width"), c.get("height")) for c in shown],
    )


def _prove_b14(ctx: Any, fake: _Fake, base: str, tokens: dict[str, str], made: list) -> None:
    # --- a burst of 429 with Retry-After is waited out, then the clip downloads
    q = f"proof harbor {tokens['harbor']}"
    fake.ctl(f"reset=1&mode=burst429&n={BURST}&retry_after={BURST_WAIT_S}")
    status, answer, secs = _fetch(ctx, base, q, "portrait", 1, "pexels")
    ctx.check(
        status == 200 and isinstance(answer, dict) and answer.get("downloaded") == 1,
        f"burst of {BURST} x 429 (Retry-After {BURST_WAIT_S}): the fetch still answers 200 with the clip",
        status=status,
        seconds=round(secs, 1),
        answer=short(answer),
    )
    ctx.check(
        BURST * BURST_WAIT_S - SLACK_S <= secs < 90,
        f"the fetch waited out {BURST} Retry-After periods ({BURST * BURST_WAIT_S} s) before it succeeded",
        seconds=round(secs, 2),
    )
    api = fake.counts("api.pexels.com")
    ctx.check(
        api.get("429") == BURST and api.get("200") == 1,
        f"the fake saw {BURST} x 429 and then one 200 on the Pexels API",
        counts=api,
    )
    _check_clips(ctx, fake, answer, "pexels", (1080, 1920), 1, made)

    # --- a 429 that never ends gives up with a coded error
    q = f"proof road {tokens['road']}"
    fake.ctl(f"reset=1&mode=always429&retry_after={ENDLESS_WAIT_S}")
    status, answer, secs = _fetch(ctx, base, q, "portrait", 1, "pexels")
    err = _error(answer)
    ctx.check(
        status == 500
        and err.get("code") == "NS-E501"
        and f"rate limited (429) after {ENDLESS_TRIES} attempts" in str(err.get("message")),
        "endless 429: the fetch fails with the coded NS-E501 'rate limited (429) after 4 attempts'",
        status=status,
        error=short(err),
    )
    ctx.check(
        ENDLESS_TRIES * ENDLESS_WAIT_S - SLACK_S <= secs < 90,
        f"it waited {ENDLESS_WAIT_S} s after each of the {ENDLESS_TRIES} answers before giving up",
        seconds=round(secs, 2),
    )
    api = fake.counts("api.pexels.com")
    cdn = fake.counts("videos.pexels.com")
    ctx.check(
        api == {"429": ENDLESS_TRIES} and not cdn,
        f"the fake saw exactly {ENDLESS_TRIES} x 429 and no download",
        api=api,
        cdn=cdn,
    )
    rows = db_rows(ctx, "select count(*) from clips where keywords like ?", (f"%{tokens['road']}%",))
    ctx.check(rows[0][0] == 0, "no clip row was written for the failed fetch", rows=rows[0][0])
    fake.ctl("reset=1&mode=normal")
