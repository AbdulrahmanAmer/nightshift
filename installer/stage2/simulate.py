"""--simulate-downloads support (component C1): a scripted command runner and a fixture-file fetcher.

With these two in place the whole wizard runs in CI: nothing is installed, downloads become HEAD checks plus
tiny fixture files, and ``ns`` / ``uv`` / PowerShell calls are answered from a script and recorded.
"""

from __future__ import annotations

import base64
import io
import json
import struct
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
from installer.stage2.winenv import CHANGED_EXIT, user_path_output
from ns_common.logging import NsLogger
from ns_supervisor import check_names as names


def simulated_lnk(arguments: str, workdir: str = "", icon: str = "") -> bytes:
    """A minimal shell link (MS-SHLLINK: the header and Unicode StringData, no target list) that
    ``shortcut_owner.read_lnk`` reads: setup decides whose a Desktop shortcut is from its arguments, so a
    simulated one has to say them too."""
    flags = 0x80 | 0x20 | (0x10 if workdir else 0) | (0x40 if icon else 0)
    head = struct.pack("<I16sI", 0x4C, bytes.fromhex("0114020000000000c000000000000046"), flags) + bytes(52)
    body = b"".join(
        struct.pack("<H", len(text)) + text.encode("utf-16-le") for text in (workdir, arguments, icon) if text
    )
    return head + body + bytes(4)


def ps_b64(text: str) -> str:
    """What ``winenv.utf8_b64_script`` prints for ``text``: base64 of its UTF-8 bytes."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# The checks `ns verify --quick` really reports (a test runs the real verify and compares the names).
VERIFY_CHECKS = names.QUICK_CHECKS

#: Where a venv keeps its packages, and the dist-info the simulated `uv pip install -e <app>` leaves there.
SITE_PACKAGES = Path("Lib") / "site-packages"
PRODUCT_DIST_INFO = "nightshift-0-editable.dist-info"
#: The simulated user environment variables, in the simulated desktop folder (the registry outlives a run).
USER_ENV_FILE = "_simulated_user_env.json"
#: Where the simulated user environment keeps the kind of its PATH (`ExpandString` or `String`).
PATH_KIND = "Path:kind"


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

    ``verify_failures`` names the ``ns verify --quick`` checks that report a failure, spelled as ``ns verify``
    spells them (``ns_supervisor.check_names``, for example ``tts.voices``). Failing ``tts.*`` checks
    pass again once a ``torch`` package was installed through uv (the PyTorch fallback venv).
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
        # the user environment is in the registry, which outlives a process: the simulated one is on disk
        self.user_env: dict[str, str] = self._load_user_env()
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

    @property
    def _env_file(self) -> Path:
        return self.desktop / USER_ENV_FILE

    def _load_user_env(self) -> dict[str, str]:
        try:
            data = json.loads(self._env_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _save_user_env(self) -> None:
        self._env_file.parent.mkdir(parents=True, exist_ok=True)
        self._env_file.write_text(json.dumps(self.user_env), encoding="utf-8")

    def called(self, tag_prefix: str) -> list[Call]:
        """Recorded calls whose tag starts with ``tag_prefix``."""
        return [c for c in self.calls if c.tag.startswith(tag_prefix)]

    # ---- defaults ----
    def _default(self, call: Call) -> RunResult:
        tag = call.tag
        if tag == "ps:set-env":
            self.user_env[call.meta["name"]] = call.meta["value"]
            self._save_user_env()
            return RunResult(0)
        if tag == "ps:get-env":
            return RunResult(0, ps_b64(self.user_env.get(call.meta["name"], "")) + "\n")
        if tag == "ps:desktop":
            return RunResult(0, ps_b64(str(self.desktop)) + "\n")
        if tag == "ps:userpath-read":       # the simulated user PATH (`winenv.user_path`)
            kind = self.user_env.get(PATH_KIND, "ExpandString" if "Path" in self.user_env else "none")
            return RunResult(0, user_path_output(self.user_env.get("Path", ""), kind))
        if tag == "ps:userpath-write":      # written only while it still holds what was read
            if call.env.get("NS_PATH_OLD", "") != self.user_env.get("Path", ""):
                return RunResult(CHANGED_EXIT, "changed\n")
            self.user_env["Path"] = call.env.get("NS_PATH_NEW", "")
            self.user_env[PATH_KIND] = call.env.get("NS_PATH_KIND", "ExpandString")
            self._save_user_env()
            return RunResult(0, "written\n")
        if tag == "ps:shortcut":
            lnk = Path(call.meta["lnk"])
            lnk.parent.mkdir(parents=True, exist_ok=True)
            lnk.write_bytes(simulated_lnk(call.meta.get("arguments", ""), "", call.meta.get("icon", "")))
            return RunResult(0)
        if tag.startswith("uv venv"):
            py = Path(call.meta["path"]) / "Scripts" / "python.exe"
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_bytes(b"")
            (Path(call.meta["path"]) / SITE_PACKAGES).mkdir(parents=True, exist_ok=True)    # empty, like uv's
            return RunResult(0)
        if tag.startswith("uv pip install"):
            self._pip_install(call)
            if any(a.startswith("torch") for a in call.argv):
                self.torch_installed = True
            return RunResult(0)
        if tag.endswith(" import check"):
            return self._import_check(call)
        if tag == "ns tune":
            return RunResult(0, "best: 51.2 tokens/s\n")
        if tag == "ns verify --quick":
            report = self._verify_report()
            home = call.env.get("NIGHTSHIFT_HOME")
            if home:                    # the real `ns verify` leaves its report in the logs folder
                logs = Path(home) / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                (logs / "verify-report.json").write_text(json.dumps(report), encoding="utf-8")
            return RunResult(0, json.dumps(report))
        if tag.startswith("ns schedule install") and "--setup" in call.argv:
            return self._schedule_setup(call)
        # "ps:task-read" (setup's own look at the task) gets the empty default below: no answer, so a
        # simulated run never settles a record against a Windows it did not ask
        if tag == "ns doctor --bundle":
            self.desktop.mkdir(parents=True, exist_ok=True)
            zip_path = self.desktop / f"nightshift-diagnostics-{date.today():%Y%m%d}.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("VERSION", "simulated")
            return RunResult(0, f"wrote {zip_path}\n")
        if tag == "opencode models":
            return RunResult(0, "local/code\n")
        if tag == "opencode --version":
            return RunResult(0, "1.19.5\n")
        return RunResult(0)

    @staticmethod
    def _schedule_setup(call: Call) -> RunResult:
        """``ns schedule install --setup`` on a PC whose Windows holds no task: a first install that says yes
        gets one (the record ``config/schedule.json`` is written, as the real command does), everything else
        stays off. The answer is the real command's one JSON line."""
        def value(flag: str) -> str | None:
            return call.argv[call.argv.index(flag) + 1] if flag in call.argv else None

        when = value("--time") or "03:00"
        first = value("--setup") == "first"
        on = first and value("--nightly") == "yes"
        home = call.env.get("NIGHTSHIFT_HOME")
        if home and on:
            config = Path(home) / "config"
            config.mkdir(parents=True, exist_ok=True)
            record = {"run_time": when, "task_name": "Nightshift Daily", "mode": "registered"}
            (config / "schedule.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        out = {"case": "first" if first else "kept", "on": on, "run_time": when, "signed_in_only": False,
               "wanted_on": False, "wanted_off": False, "time_kept": None, "seen": value("--seen"),
               "reason": ""}
        return RunResult(0, "setup: " + json.dumps(out) + "\n")

    @staticmethod
    def _pip_install(call: Call) -> None:
        """Put the product into the venv named by ``--python``: a dist-info in its site-packages.

        Only the install of the app itself (``-e <app>[extras]``) counts. That is the install an interrupted
        setup is missing, and the one the import check looks for; the other wheels are not simulated.
        """
        if "--python" not in call.argv or "-e" not in call.argv:
            return
        venv = Path(call.argv[call.argv.index("--python") + 1]).parent.parent
        info = venv / SITE_PACKAGES / PRODUCT_DIST_INFO
        info.mkdir(parents=True, exist_ok=True)
        (info / "METADATA").write_text("Name: nightshift\n", encoding="utf-8")

    @staticmethod
    def _import_check(call: Call) -> RunResult:
        """``<venv python> -I -c <probe>``: loads the product only when the app was installed in that venv."""
        venv = Path(call.argv[0]).parent.parent
        if (venv / SITE_PACKAGES / PRODUCT_DIST_INFO).is_dir():
            return RunResult(0)
        return RunResult(1, "", "cannot load: ns_common, ns_supervisor")

    def _verify_report(self) -> dict[str, Any]:
        failing = set(self.verify_failures)
        if self.torch_installed:
            failing -= {n for n in failing if n.startswith(names.TTS_PREFIX)}
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
        elif item is not None and item.kind == "nupkg":
            body = _zip_with([candidates[0] for _, candidates in item.members])
        else:
            body = b"nightshift-simulated:" + dest.name.encode()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        if progress:
            progress(len(body), len(body))
        write_marker(dest, sha256)
        return dest

    return fetch


