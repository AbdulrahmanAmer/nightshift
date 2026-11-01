"""--simulate-downloads support (component C1): a scripted command runner and a fixture-file fetcher.

With these two in place the whole wizard runs in CI: nothing is installed, downloads become HEAD checks plus
tiny fixture files, and ``ns`` / ``uv`` / PowerShell calls are answered from a script and recorded.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from installer.stage2.downloader import DownloadItem, ProgressFn, write_marker
from installer.stage2.errors import make_error
from installer.stage2.runner import RunResult
from ns_common.logging import NsLogger

VERIFY_CHECKS = ("llm", "embed", "tts", "whisper", "nvenc", "mtp")


@dataclass
class Call:
    """One recorded command."""

    argv: list[str]
    tag: str
    meta: dict[str, str]
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)


Handler = Callable[[Call], RunResult]


class FakeRunner:
    """Answers installer commands without running anything.

    ``verify_failures`` names the ``ns verify --quick`` checks that report a failure. A failing ``tts``
    check passes again once a ``torch`` package was installed through uv (the PyTorch fallback venv).
    ``results`` maps a tag to a RunResult or a handler and wins over the defaults.
    """

    def __init__(
        self,
        desktop: Path,
        *,
        verify_failures: set[str] | None = None,
        results: Mapping[str, RunResult | Handler] | None = None,
    ) -> None:
        self.desktop = desktop
        self.verify_failures = set(verify_failures or ())
        self.results: dict[str, RunResult | Handler] = dict(results or {})
        self.calls: list[Call] = []
        self.user_env: dict[str, str] = {}
        self.torch_installed = False

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        capture: bool = True,
        tag: str = "",
        meta: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Record the call and answer it."""
        call = Call(list(argv), tag, dict(meta or {}), cwd, dict(env or {}))
        self.calls.append(call)
        override = self.results.get(tag)
        if override is not None:
            return override(call) if callable(override) else override
        return self._default(call)

    def called(self, tag_prefix: str) -> list[Call]:
        """Recorded calls whose tag starts with ``tag_prefix``."""
        return [c for c in self.calls if c.tag.startswith(tag_prefix)]

    # ---- defaults ----
    def _default(self, call: Call) -> RunResult:
        tag = call.tag
        if tag == "ps:set-env":
            self.user_env[call.meta["name"]] = call.meta["value"]
            return RunResult(0)
        if tag == "ps:get-env":
            return RunResult(0, self.user_env.get(call.meta["name"], "") + "\n")
        if tag == "ps:desktop":
            return RunResult(0, str(self.desktop) + "\n")
        if tag == "ps:shortcut":
            lnk = Path(call.meta["lnk"])
            lnk.parent.mkdir(parents=True, exist_ok=True)
            lnk.write_bytes(b"simulated shortcut")
            return RunResult(0)
        if tag.startswith("uv venv"):
            py = Path(call.meta["path"]) / "Scripts" / "python.exe"
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_bytes(b"")
            return RunResult(0)
        if tag.startswith("uv pip install") and any(a.startswith("torch") for a in call.argv):
            self.torch_installed = True
            return RunResult(0)
        if tag == "ns tune":
            return RunResult(0, "best: 51.2 tokens/s\n")
        if tag == "ns verify --quick":
            return RunResult(0, json.dumps(self._verify_report()))
        if tag == "ns doctor --bundle":
            self.desktop.mkdir(parents=True, exist_ok=True)
            zip_path = self.desktop / f"nightshift-diagnostics-{date.today():%Y%m%d}.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("VERSION", "simulated")
            return RunResult(0, f"wrote {zip_path}\n")
        if tag == "opencode --version":
            return RunResult(0, "1.19.5\n")
        return RunResult(0)

    def _verify_report(self) -> dict[str, Any]:
        failing = set(self.verify_failures)
        if self.torch_installed:
            failing.discard("tts")
        checks = [{"name": n, "passed": n not in failing, "value": None, "limit": None, "log": None}
                  for n in VERIFY_CHECKS]
        return {"slice": "verify", "date": date.today().isoformat(), "checks": checks, "benchmarks": {}}


def _zip_with(expect: Sequence[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SIMULATED.txt", "fixture archive")
        for name in expect:
            zf.writestr(name, "simulated")
    return buf.getvalue()


def simulated_fetch(
    items: Sequence[DownloadItem], *, head_checks: bool, log: NsLogger
) -> Callable[..., Path]:
    """A ``download`` replacement: optional HEAD check, then a tiny fixture file (or zip) is written."""
    by_url = {i.url: i for i in items}

    def fetch(
        url: str,
        dest: Path,
        sha256: str | None,
        *,
        progress: ProgressFn | None = None,
        client: httpx.Client | None = None,
        size: int | None = None,
        **_: Any,
    ) -> Path:
        if head_checks and client is not None:
            try:
                status = client.head(url).status_code
            except httpx.HTTPError as e:
                log.warn("sim_head_failed", f"{url}: {e}")
            else:
                if status == 404:
                    raise make_error("NS-E105", f"{url}: HTTP 404", status=404)
                if status >= 400:
                    log.warn("sim_head_status", f"{url}: HTTP {status}")
        item = by_url.get(url)
        if item is not None and item.kind == "zip":
            body = _zip_with(item.expect)
        else:
            body = b"nightshift-simulated:" + dest.name.encode()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        if progress:
            progress(len(body), len(body))
        write_marker(dest, sha256)
        return dest

    return fetch


