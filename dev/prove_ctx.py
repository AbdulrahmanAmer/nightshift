"""The object a proof script gets (H-13). Standard library only; it imports nothing from the product, so a
proof imports nothing but this module (and, for the failure type, nothing at all: it uses ``ctx.check``).

A proof is ``proofs/<slug>.py`` with a module-level ``PROOF = {...}`` literal and ``def run(ctx) -> None``;
see ``dev/prove.py`` for the runner and the declared fields.

What ``ctx`` offers: ``home``, ``version``, ``kind``, ``ports``, ``scratch``; ``api``, ``cli``, ``spawn``,
``db``, ``wait``, ``step``, ``check``, ``fail``, ``skip`` and (window proofs only) ``page``. A proof ends at
an effect (a file, a database row, a text read back through the dashboard), never at a log line that says so.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

#: the only programs ``ctx.cli`` runs, and where each lives in an install (venv folder, executable name)
CLI_PROGRAMS = {"ns": ("supervisor", "ns"), "studio": ("studio", "studio")}
OUTPUT_LIMIT = 4000
MAX_BODY = 4_000_000  # ctx.api reads at most this many bytes of one answer
DEFAULT_PORTS = {"dashboard": 8100, "supervisor": 8099}


class ProofFailed(Exception):  # noqa: N818 - the name is the proof interface
    """A proof's check did not hold. The runner records the message as the failure."""


class ProofSkipped(BaseException):  # noqa: N818 - BaseException so a proof's `except Exception` cannot eat it
    """Raised by ``ctx.skip``; the runner records the proof as skipped, never as passed."""


class SetupError(Exception):
    """The home is not an install the runner can read (exit code 2, not a failed proof)."""


@dataclass(frozen=True)
class InstallInfo:
    version: str
    kind: str  # "dev" | "engine"
    ports: dict[str, int]


def read_install(home: Path) -> InstallInfo:
    """Product version, install kind and ports, read from the install's own files."""
    home = Path(home)
    runtime = home / "config" / "runtime.json"
    if not runtime.is_file():
        raise SetupError(f"{runtime} not found: {home} is not a Nightshift install")
    try:
        cfg = json.loads(runtime.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SetupError(f"{runtime} is unreadable: {exc}") from exc
    if not isinstance(cfg, dict):
        raise SetupError(f"{runtime} is not a JSON object")
    version = ""
    for candidate in (home / "app" / "VERSION", home / "VERSION"):
        if candidate.is_file():
            version = candidate.read_text(encoding="utf-8-sig").strip()
            break
    if not version:
        raise SetupError(f"no VERSION file under {home / 'app'}: cannot name the product version")
    ports = dict(DEFAULT_PORTS)
    if isinstance(cfg.get("supervisor_port"), int):
        ports["supervisor"] = cfg["supervisor_port"]
    for name in ("llama", "embed", "tts"):
        section = cfg.get(name)
        if isinstance(section, dict) and isinstance(section.get("port"), int):
            ports[name] = section["port"]
    return InstallInfo(version, "dev" if cfg.get("dev_mode") else "engine", ports)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def jsonable(value: Any) -> Any:
    """``value`` as plain JSON data (anything else becomes its str, so a step never breaks the evidence)."""
    return json.loads(json.dumps(value, default=str))


def clip(text: str, limit: int = OUTPUT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more characters]"


def kill_tree(proc: subprocess.Popen[Any]) -> None:
    """Stop exactly this process and its children."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
            timeout=30,
        )
    else:
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=10)


class Ctx:
    """Built by the runner, one per proof."""

    def __init__(
        self,
        *,
        home: Path,
        info: InstallInfo,
        scratch: Path,
        proof_id: str,
        timeout_s: float = 300,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.home = Path(home)
        self.version = info.version
        self.kind = info.kind
        self.ports = dict(info.ports)
        self.scratch = Path(scratch)
        self.proof_id = proof_id
        self.steps: list[dict[str, Any]] = []
        self.pids: list[dict[str, Any]] = []
        self.left_on_screen = "none"
        self._env = dict(os.environ if env is None else env)
        self._env["NIGHTSHIFT_HOME"] = str(self.home)
        self._clock = clock
        self._sleep = sleep
        self._deadline = clock() + timeout_s
        self._aborted = False
        self._timeout_s = timeout_s
        self._procs: list[subprocess.Popen[Any]] = []
        self._pw_factory = playwright_factory
        self._pw: Any = None
        self._browser: Any = None

    # ------------------------------------------------------------------ evidence
    def step(self, name: str, **observed: Any) -> None:
        """One step in the evidence: its name, the observed values and a timestamp."""
        self.steps.append({"name": name, "observed": jsonable(observed), "ts": now_iso()})

    def check(self, cond: object, what: str, **observed: Any) -> None:
        """Record a step with its pass or fail; a false condition ends the proof (``ProofFailed``)."""
        self.steps.append({"name": what, "ok": bool(cond), "observed": jsonable(observed), "ts": now_iso()})
        if not cond:
            raise ProofFailed(what)

    def fail(self, msg: str) -> NoReturn:
        self.steps.append({"name": msg, "ok": False, "observed": {}, "ts": now_iso()})
        raise ProofFailed(msg)

    def skip(self, reason: str) -> NoReturn:
        """End the proof as skipped (for example 'needs fake_gpu'). A skip is never a pass."""
        raise ProofSkipped(reason)

    def read_json(self, path: str | Path) -> Any:
        """A JSON file of the install (a relative path is under ``home``)."""
        p = Path(path)
        p = p if p.is_absolute() else self.home / p
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ProofFailed(f"cannot read {p}: {exc}") from exc

    # ------------------------------------------------------------------ time
    def abort(self) -> None:
        """The runner gave up on this proof (it blocked outside ``ctx``): every later ctx call fails."""
        self._aborted = True

    def _remaining(self) -> float:
        if self._aborted:
            raise ProofFailed("proof was stopped by the runner")
        left = self._deadline - self._clock()
        if left <= 0:
            raise ProofFailed(f"proof timed out after {self._timeout_s:g} s (PROOF['timeout_s'])")
        return left

    def wait(self, fn: Callable[[], Any], timeout: float, every: float = 2, what: str = "") -> Any:
        """Poll ``fn`` until it returns something truthy; on timeout the proof fails naming ``what``."""
        end = self._clock() + timeout
        while True:
            value = fn()
            if value:
                return value
            if self._clock() >= end:
                raise ProofFailed(f"timed out after {timeout:g} s waiting for {what or 'the condition'}")
            self._remaining()
            self._sleep(every)

    # ------------------------------------------------------------------ the dashboard
    @property
    def dashboard_url(self) -> str:
        return f"http://127.0.0.1:{self.ports['dashboard']}"

    def api(self, method: str, path: str, body: Any = None, timeout: float = 120) -> tuple[int, Any]:
        """The dashboard's HTTP API, no proxy: (status, parsed JSON or text). An error status is returned."""
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.dashboard_url + path, data=data, method=method.upper(), headers=headers
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        limit = min(timeout, self._remaining())
        try:
            with opener.open(req, timeout=limit) as resp:
                status, raw = resp.status, self._read_bounded(resp, limit, method, path)
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, self._read_bounded(exc, limit, method, path)
        except (urllib.error.URLError, OSError) as exc:
            raise ProofFailed(f"{method.upper()} {path}: no answer from the dashboard ({exc})") from exc
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed: Any = json.loads(text)
        except ValueError:
            parsed = text
        self.step(f"api {method.upper()} {path}", status=status, bytes=len(raw))
        return status, parsed

    def _read_bounded(self, resp: Any, limit: float, method: str, path: str) -> bytes:
        """Read an answer chunk by chunk against a wall clock, so a stream that never ends (an event stream
        that ignores ``limit``) fails the proof instead of keeping the per-read socket timeout alive."""
        end = self._clock() + limit
        reader = getattr(resp, "read1", None) or resp.read
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = reader(8192)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BODY:
                raise ProofFailed(f"{method.upper()} {path}: the answer is longer than {MAX_BODY} bytes")
            if self._clock() > end:
                raise ProofFailed(f"{method.upper()} {path}: still sending after {limit:g} s (a stream?)")
            self._remaining()

    # ------------------------------------------------------------------ the product's CLI
    def cli_program(self, name: str) -> Path:
        """``Scripts\\ns.exe`` or ``Scripts\\studio.exe`` of this home's own venv (the runner resolves it)."""
        if name not in CLI_PROGRAMS:
            raise ProofFailed(
                f"ctx.cli runs only {sorted(CLI_PROGRAMS)}, not {name!r}; proofs never pass a path"
            )
        venv, exe = CLI_PROGRAMS[name]
        scripts = self.home / "venvs" / venv / "Scripts"
        for candidate in (scripts / f"{exe}.exe", scripts / exe, self.home / "venvs" / venv / "bin" / exe):
            if candidate.is_file():
                return candidate
        raise ProofFailed(f"{name} not found: {scripts / (exe + '.exe')} does not exist in this install")

    def cli(self, argv: list[str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
        """Run ``ns`` or ``studio`` (``argv[0]``) from the install's own venv with NIGHTSHIFT_HOME set."""
        if not argv or not isinstance(argv[0], str):
            raise ProofFailed("ctx.cli needs argv like ['ns', 'backup'] or ['studio', 'status']")
        program = self.cli_program(argv[0])
        cmd = [str(program), *[str(a) for a in argv[1:]]]
        started = self._clock()
        try:
            done = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(timeout, self._remaining()),
                env=self._env,
                cwd=str(self.home),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.step(f"cli {' '.join(argv)}", NIGHTSHIFT_HOME=str(self.home), timed_out_s=timeout)
            raise ProofFailed(f"{' '.join(argv)} did not finish in {timeout:g} s") from exc
        self.step(
            f"cli {' '.join(argv)}",
            NIGHTSHIFT_HOME=str(self.home),
            program=str(program),
            exit_code=done.returncode,
            stdout=clip(done.stdout or ""),
            stderr=clip(done.stderr or ""),
            seconds=round(self._clock() - started, 2),
        )
        return done

    # ------------------------------------------------------------------ helpers the proof starts itself
    def spawn(
        self, args: list[str], env: dict[str, str] | None = None, name: str | None = None
    ) -> subprocess.Popen[Any]:
        """A detached helper (a fake proxy, a watcher). Its pid is recorded; the runner kills exactly the pids
        spawned here when the proof ends, pass or fail. Output goes to a log in ``scratch``."""
        self._remaining()  # a proof the runner has stopped starts nothing more
        label = name or Path(str(args[0])).stem
        log = self.scratch / f"{label}.log"
        child_env = dict(self._env)
        child_env.update(env or {})
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        with log.open("ab") as out:
            proc = subprocess.Popen(
                [str(a) for a in args],
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=child_env,
                cwd=str(self.scratch),
                creationflags=flags,
            )
        self._procs.append(proc)
        self.pids.append({"name": label, "pid": proc.pid, "log": str(log)})
        self.step(f"spawn {label}", pid=proc.pid, log=str(log))
        return proc

    def db(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Rows of a read-only query on ``<home>/data/studio.db`` (index or column name)."""
        path = self.home / "data" / "studio.db"
        if not path.is_file():
            raise ProofFailed(f"{path} does not exist")
        con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def page(self) -> Any:
        """Window proofs: a Playwright page on Edge (channel 'msedge'), headed, at the dashboard URL."""
        self._remaining()  # a proof the runner has stopped opens no window
        if self._browser is None:
            factory = self._pw_factory
            if factory is None:
                try:
                    from playwright.sync_api import sync_playwright  # pyright: ignore[reportMissingImports]
                except ImportError as exc:
                    raise ProofFailed("playwright is not installed in the runner's Python") from exc
                factory = sync_playwright
            self._pw = factory().start()
            self._browser = self._pw.chromium.launch(channel="msedge", headless=False)
        page = self._browser.new_page()
        page.goto(self.dashboard_url)
        self.step("page open", url=self.dashboard_url, browser="msedge (headed)")
        return page

    # ------------------------------------------------------------------ the runner's end of a proof
    def close(self) -> None:
        """Kill exactly the pids ``spawn`` started and close the browser; never raises."""
        closed: list[str] = []
        for proc in self._procs:
            try:
                kill_tree(proc)
                closed.append(f"pid {proc.pid}")
            except Exception as exc:  # the next one must still be stopped
                closed.append(f"pid {proc.pid} (kill failed: {exc})")
        self._procs = []
        if self._browser is not None:
            try:
                self._browser.close()
                closed.append("browser (msedge)")
            except Exception as exc:
                closed.append(f"browser (close failed: {exc})")
            self._browser = None
        if self._pw is not None:
            with contextlib.suppress(Exception):
                self._pw.stop()
            self._pw = None
        self.left_on_screen = (
            "none"
            if not any("browser" in c for c in closed)
            else ("closed by the runner: " + ", ".join(c for c in closed if "browser" in c))
        )
        if closed:
            self.steps.append({"name": "runner stopped", "observed": {"closed": closed}, "ts": now_iso()})
