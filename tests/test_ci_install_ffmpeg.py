"""H-9: CI's FFmpeg install cannot fail silently.

`choco install ffmpeg` got a 504 from the Chocolatey feed, printed "Chocolatey installed 0/0 packages",
exited 0, and twelve render tests then failed as if the code were wrong. The step is now one composite
action (.github/actions/install-ffmpeg) that calls dev/ci_install_ffmpeg.py: the pinned build of
manifest.json (`bins.ffmpeg`), from the Actions cache or a verified download, Chocolatey only as a second
source, and a final `ffmpeg -version` / `ffprobe -version` that fails the step with a plain message.

Nothing here touches the network or runs FFmpeg: the transport is a fake, the zips are tiny, and the programs'
`-version` is a fake runner.
"""

from __future__ import annotations

import ast
import hashlib
import http.client
import io
import json
import re
import socket
import sys
import urllib.error
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from dev import ci_install_ffmpeg as ci
from installer.stage2.web import bootstrap
from tests import online

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION_DIR = ROOT / ".github" / "actions" / "install-ffmpeg"
ACTION_REF = "./.github/actions/install-ffmpeg"


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def workflow(name: str) -> dict[str, Any]:
    return load_yaml(WORKFLOWS / name)


def action() -> dict[str, Any]:
    return load_yaml(ACTION_DIR / "action.yml")


def step_index(job: dict[str, Any], pred: Callable[[dict[str, Any]], bool]) -> int:
    for i, s in enumerate(job["steps"]):
        if pred(s):
            return i
    raise AssertionError("no such step")


# ---- the four places use the one action, nothing installs FFmpeg by Chocolatey any more -------------------
FOUR_PLACES = [
    ("ci.yml", "test-shard"),
    ("ci.yml", "test-collect"),
    ("release.yml", "test-shard"),
    ("release.yml", "test-collect"),
]


def test_no_workflow_has_a_chocolatey_ffmpeg_install_line_left():
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files, "the workflows folder is empty?"
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"choco\s+install\s+ffmpeg", text), f"{path.name} still installs FFmpeg directly"
        for name, job in load_yaml(path)["jobs"].items():
            for s in job.get("steps", []):
                run = str(s.get("run", ""))
                assert "choco" not in run.lower(), f"{path.name} {name}: a step runs Chocolatey itself"


@pytest.mark.parametrize(("file", "job"), FOUR_PLACES)
def test_the_four_places_use_the_one_composite_action(file: str, job: str):
    steps = workflow(file)["jobs"][job]["steps"]
    uses = [s for s in steps if s.get("uses") == ACTION_REF]
    assert len(uses) == 1, f"{file} {job}: {len(uses)} uses of {ACTION_REF}"
    assert "with" not in uses[0], "the action takes no arguments: the pin comes from manifest.json"


@pytest.mark.parametrize(("file", "job"), FOUR_PLACES)
def test_the_action_runs_after_checkout_and_python_and_before_anything_that_needs_ffmpeg(file: str, job: str):
    j = workflow(file)["jobs"][job]
    here = step_index(j, lambda s: s.get("uses") == ACTION_REF)
    checkout = step_index(j, lambda s: str(s.get("uses", "")).startswith("actions/checkout@"))
    python = step_index(j, lambda s: str(s.get("uses", "")).startswith("actions/setup-python@"))
    assert checkout < here, "a local action can only be used after the repository is checked out"
    assert python < here, "the script runs with the job's Python"
    first_test = step_index(
        j, lambda s: "pytest" in str(s.get("run", "")) or "ci_shards.py" in str(s.get("run", ""))
    )
    assert here < first_test, "the install comes before the first step that runs pytest"


def test_other_jobs_that_never_render_do_not_install_ffmpeg():
    for file in ("ci.yml", "release.yml"):
        for name, job in workflow(file)["jobs"].items():
            has = any(s.get("uses") == ACTION_REF for s in job.get("steps", []))
            assert has == ((file, name) in FOUR_PLACES), f"{file} {name}"


# ---- the composite action itself --------------------------------------------------------------------------
def test_the_composite_action_file_parses_and_is_a_composite():
    a = action()
    assert a["name"] and a["description"]
    assert a["runs"]["using"] == "composite"
    assert not a.get("inputs"), "no inputs: nothing for a caller to get wrong"


def test_every_run_step_of_the_action_names_its_shell_and_runs_the_script_from_the_checkout():
    steps = action()["runs"]["steps"]
    runs = [s for s in steps if "run" in s]
    assert runs
    for s in runs:
        assert s.get("shell") == "bash", s
        assert s["run"].strip().startswith("python dev/ci_install_ffmpeg.py "), s["run"]
    assert (ROOT / "dev" / "ci_install_ffmpeg.py").is_file()
    commands = [s["run"].split("ci_install_ffmpeg.py", 1)[1].split() for s in runs]
    assert commands == [["pin"], ["install"]]


def test_the_logic_is_in_the_script_not_in_the_yaml():
    text = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
    for needle in (
        "curl",
        "Invoke-WebRequest",
        "Expand-Archive",
        "sha256",
        "Get-FileHash",
        "ffmpeg -version",
    ):
        assert needle not in text, f"{needle} belongs in dev/ci_install_ffmpeg.py"


def test_the_action_restores_before_it_installs_and_saves_only_what_it_downloaded():
    steps = action()["runs"]["steps"]
    by_id = {s["id"]: i for i, s in enumerate(steps) if "id" in s}
    assert by_id["pin"] < by_id["cache"] < by_id["install"]
    restore = steps[by_id["cache"]]
    assert restore["uses"].startswith("actions/cache/restore@")
    assert restore["with"]["key"] == "${{ steps.pin.outputs.key }}"
    assert restore["with"]["path"] == "${{ steps.pin.outputs.dir }}"
    save = next(s for s in steps if str(s.get("uses", "")).startswith("actions/cache/save@"))
    assert steps.index(save) > by_id["install"], "the cache is saved only after the install proved itself"
    assert save["with"] == restore["with"], "the same key and path that were restored"
    cond = save["if"]
    assert "steps.install.outputs.source == 'download'" in cond, "never cache a Chocolatey install"
    assert "steps.cache.outputs.cache-hit != 'true'" in cond, (
        "a key that already exists cannot be saved again"
    )


def test_the_cache_action_is_pinned_to_a_commit_or_an_exact_version_not_a_moving_tag():
    steps = action()["runs"]["steps"]
    cache_steps = [s for s in steps if str(s.get("uses", "")).startswith("actions/cache")]
    assert len(cache_steps) == 2
    for s in cache_steps:
        version = s["uses"].split("@", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{40}", version) or re.fullmatch(r"v\d+\.\d+\.\d+", version), s["uses"]
    assert {s["uses"].split("@", 1)[1] for s in cache_steps} == {cache_steps[0]["uses"].split("@", 1)[1]}, (
        "restore and save are the same release"
    )


def test_the_action_hands_the_source_to_its_callers_and_has_no_secret():
    a = action()
    assert a["outputs"]["source"]["value"] == "${{ steps.install.outputs.source }}"
    assert "secrets" not in (ACTION_DIR / "action.yml").read_text(encoding="utf-8")


# ===========================================================================================================
# dev/ci_install_ffmpeg.py: no network, a fake transport, tiny fake zips, a fake `-version`
# ===========================================================================================================
ROOT_DIR = "ffmpeg-n8.1.3-27-gtest-win64-gpl-8.1"
URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-test/ffmpeg-test.zip"
HOST = "github.com"
PLAIN = "FFmpeg could not be installed: "


def make_zip(members: dict[str, bytes] | None = None) -> bytes:
    members = members or {
        f"{ROOT_DIR}/bin/ffmpeg.exe": b"MZ-ffmpeg",
        f"{ROOT_DIR}/bin/ffprobe.exe": b"MZ-ffprobe",
        f"{ROOT_DIR}/bin/ffplay.exe": b"MZ-ffplay",
        f"{ROOT_DIR}/LICENSE.txt": b"gpl",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def pin_for(data: bytes, *, size: int | None = None) -> ci.Pin:
    return ci.Pin("n8.1.3-27-gtest", URL, hashlib.sha256(data).hexdigest(), size)


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, headers: dict[str, str] | None = None, status: int = 200) -> None:
        super().__init__(data)
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}
        self.status = status


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, f"fake {code}", {}, io.BytesIO(b""))  # type: ignore[arg-type]


class FakeTransport:
    """Answers each request with the next scripted outcome: bytes (HTTP 200), an int (that HTTP status), a
    FakeResponse, or an exception to raise."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> Any:
        self.urls.append(url)
        assert self.outcomes, "the script asked for more requests than the test scripted"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, int):
            raise http_error(outcome)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome if isinstance(outcome, FakeResponse) else FakeResponse(outcome)


def banner(exe: str) -> tuple[int, str]:
    tool = Path(exe).stem
    return 0, f"{tool} version n8.1.3-27-gtest-20261031 Copyright (c) the FFmpeg developers"


class Env:
    """The deps of one install: fake transport, recorded sleeps and log, a temp RUNNER_TEMP, GitHub files."""

    def __init__(self, tmp: Path, *outcomes: Any) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.transport = FakeTransport(*outcomes)
        self.sleeps: list[float] = []
        self.log: list[str] = []
        self.env = {
            "RUNNER_TEMP": str(tmp / "runner-temp"),
            "RUNNER_OS": "Windows",
            "RUNNER_ARCH": "X64",
            "GITHUB_OUTPUT": str(tmp / "github_output"),
            "GITHUB_PATH": str(tmp / "github_path"),
            "GITHUB_STEP_SUMMARY": str(tmp / "github_summary"),
        }
        self.on_path: dict[str, str] = {}
        self.run: Callable[[str], tuple[int, str]] = banner
        self.choco: Callable[[], tuple[int | None, str]] = lambda: (None, "")
        self.choco_calls = 0

    def deps(self) -> ci.Deps:
        def choco() -> tuple[int | None, str]:
            self.choco_calls += 1
            return self.choco()

        return ci.Deps(
            opener=self.transport,
            which=lambda name: self.on_path.get(name),
            run=lambda exe: self.run(exe),
            choco=choco,
            sleep=self.sleeps.append,
            say=self.log.append,
            env=self.env,
        )

    @property
    def folder(self) -> Path:
        return Path(self.env["RUNNER_TEMP"]) / "ns-ffmpeg" / "n8.1.3-27-gtest"

    def text(self) -> str:
        return "\n".join(self.log)

    def file(self, name: str) -> str:
        path = Path(self.env[name])
        return path.read_text(encoding="utf-8") if path.exists() else ""


def run_install(e: Env, data: bytes, **pin_kw: Any) -> ci.Result:
    return ci.install(pin_for(data, **pin_kw), e.folder, e.deps())


def choco_on_path(e: Env) -> None:
    e.on_path = {"ffmpeg": "C:/choco/ffmpeg.exe", "ffprobe": "C:/choco/ffprobe.exe"}


# ---- cache miss: download, verify, unpack, prove -----------------------------------------------------------
def test_a_cache_miss_downloads_verifies_the_sha256_and_unpacks_only_the_two_programs(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, data)
    result = run_install(e, data)
    assert result.ok and result.source == "download"
    assert e.transport.urls == [URL]
    assert sorted(p.name for p in e.folder.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]
    assert (e.folder / "ffmpeg.exe").read_bytes() == b"MZ-ffmpeg"
    assert "the sha256 matches the manifest" in e.text()
    assert "ffmpeg version n8.1.3-27-gtest" in e.text() and "ffprobe version" in e.text()
    # the folder goes on PATH only now that both programs ran, and the action learns it was a download
    assert e.file("GITHUB_PATH").strip() == str(e.folder)
    assert "source=download" in e.file("GITHUB_OUTPUT")
    assert not list((tmp_path / "runner-temp").glob("ns-ffmpeg-dl-*")), (
        "the downloaded zip is not left behind"
    )


def test_the_download_is_checked_against_a_size_in_the_manifest_when_it_has_one(tmp_path: Path):
    data = make_zip()
    assert run_install(Env(tmp_path / "right", data), data, size=len(data)).ok
    wrong = Env(tmp_path / "wrong", data)
    result = run_install(wrong, data, size=len(data) + 1)
    assert not result.ok and "the manifest pins" in result.message and "bytes" in result.message
    assert len(wrong.transport.urls) == 1, "a wrong size fails at once"


def test_a_wrong_hash_fails_with_the_plain_message_and_is_neither_retried_nor_papered_over(tmp_path: Path):
    data = make_zip()
    bad = make_zip({f"{ROOT_DIR}/bin/ffmpeg.exe": b"tampered", f"{ROOT_DIR}/bin/ffprobe.exe": b"x"})
    e = Env(tmp_path, bad)
    e.choco = lambda: (0, "Chocolatey installed 1/1 packages.")
    choco_on_path(e)  # Chocolatey would "work"
    result = run_install(e, data)
    assert not result.ok and not result.outage
    assert result.message.startswith(PLAIN + f"{HOST} answered HTTP 200 with the wrong sha256 (got ")
    assert "the manifest pins" in result.message
    assert len(e.transport.urls) == 1 and e.sleeps == [], "a wrong hash is not retried"
    assert e.choco_calls == 0, "a file that is not the pinned one is not a reason to take another source"
    assert not e.folder.exists(), "nothing unverified is left in the folder that gets cached"
    assert "source=none" in e.file("GITHUB_OUTPUT") and "source=download" not in e.file("GITHUB_OUTPUT")
    assert e.file("GITHUB_PATH") == ""


# ---- the retry rules ---------------------------------------------------------------------------------------
def test_504_504_200_is_retried_after_one_and_two_seconds_and_installs(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, 504, 504, data)
    result = run_install(e, data)
    assert result.ok and result.source == "download"
    assert e.sleeps == [1.0, 2.0]
    assert len(e.transport.urls) == 3


def test_429_a_timeout_and_a_cut_off_body_are_all_retried(tmp_path: Path):
    data = make_zip()
    cut = FakeResponse(data[: len(data) // 2], headers={"Content-Length": str(len(data))})
    e = Env(tmp_path, 429, TimeoutError("timed out"), cut, data)
    result = run_install(e, data)
    assert result.ok
    assert e.sleeps == [1.0, 2.0, 4.0]
    assert len(e.transport.urls) == 4


def test_four_504s_are_an_upstream_outage_after_1_2_and_4_seconds_and_say_so(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, 504, 504, 504, 504)
    e.choco = lambda: (
        1,
        "Unable to contact the feed (504) Gateway Time-out\nChocolatey installed 0/1 packages.",
    )
    result = run_install(e, data)
    assert not result.ok and result.outage
    assert e.sleeps == [1.0, 2.0, 4.0]
    assert len(e.transport.urls) == 4
    assert result.message.startswith(
        PLAIN + f"{HOST} answered HTTP 504 after 4 attempts; chocolatey answered exit code 1"
    )
    assert result.advice.startswith(f"upstream outage: {HOST} HTTP 504 after 4 attempts")
    assert "rerun" in result.advice
    out = e.text()
    assert f"::error::{result.message}" in out and result.advice in out


def test_404_and_403_fail_at_once_without_a_retry_and_are_not_called_an_outage(tmp_path: Path):
    data = make_zip()
    for code in (404, 403):
        e = Env(tmp_path / str(code), code)
        result = run_install(e, data)
        assert not result.ok and not result.outage
        assert result.message.startswith(PLAIN + f"{HOST} answered HTTP {code}; chocolatey answered ")
        assert len(e.transport.urls) == 1 and e.sleeps == []
        assert "Not an outage" in result.advice and "upstream outage" not in result.advice


def test_a_host_name_that_does_not_resolve_is_a_wrong_pin_and_not_retried(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")))
    result = run_install(e, data)
    assert not result.ok and not result.outage
    assert "no such host" in result.message and e.sleeps == []


# ---- Chocolatey: second source, never believed -------------------------------------------------------------
def test_choco_installed_0_of_0_packages_with_exit_0_is_caught_by_the_final_version_check(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, 504, 504, 504, 504)
    e.choco = lambda: (0, "Chocolatey v2.7.5\nUnable to contact (504)\nChocolatey installed 0/0 packages. \n")
    # nothing was installed: neither ffmpeg nor ffprobe is on PATH
    result = run_install(e, data)
    assert e.choco_calls == 1
    assert not result.ok
    assert PLAIN in result.message
    assert 'chocolatey answered exit code 0 "Chocolatey installed 0/0 packages."' in result.message
    assert "but afterwards ffmpeg is not on PATH and ffprobe is not on PATH" in result.message
    assert result.outage, "the pinned download was the outage; Chocolatey's green exit code changes nothing"


def test_a_chocolatey_that_really_installed_both_programs_is_the_second_source_and_says_it_is_unpinned(
    tmp_path: Path,
):
    data = make_zip()
    e = Env(tmp_path, 503, 503, 503, 503)
    e.choco = lambda: (0, "Chocolatey installed 1/1 packages.")
    choco_on_path(e)
    result = run_install(e, data)
    assert result.ok and result.source == "chocolatey"
    assert "::warning::FFmpeg comes from Chocolatey, not from the pinned build" in e.text()
    assert e.file("GITHUB_PATH") == "", "Chocolatey's own folder is already on PATH"
    assert "source=chocolatey" in e.file("GITHUB_OUTPUT")
    assert "chocolatey" in e.file("GITHUB_STEP_SUMMARY")


def test_a_chocolatey_exit_code_of_1_does_not_matter_when_both_programs_run(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, 404)
    e.choco = lambda: (1, "partial install, but the shims exist")
    choco_on_path(e)
    assert run_install(e, data).ok, "the check, not the exit code, decides"


def test_no_chocolatey_at_all_is_a_plain_failure_not_a_crash(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, 504, 504, 504, 504)
    result = run_install(e, data)  # choco returns (None, "")
    assert not result.ok
    assert "chocolatey answered nothing (Chocolatey is not installed on this runner)" in result.message


def test_a_flood_from_chocolatey_is_cut_to_its_last_twenty_lines(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, 404)
    e.choco = lambda: (0, "\n".join(f"line {i}" for i in range(5000)))
    run_install(e, data)
    choco_lines = [ln for ln in e.log if ln.startswith("  choco: ")]
    assert len(choco_lines) == ci.TAIL_LINES and choco_lines[-1].endswith("line 4999")
    assert len(e.log) < 60, "the whole install writes a bounded log"


# ---- the final proof ---------------------------------------------------------------------------------------
def test_a_missing_ffprobe_fails_the_step_even_though_ffmpeg_runs(tmp_path: Path):
    data = make_zip({f"{ROOT_DIR}/bin/ffmpeg.exe": b"MZ", f"{ROOT_DIR}/bin/ffplay.exe": b"MZ"})
    e = Env(tmp_path, data)
    result = run_install(e, data)
    assert not result.ok
    assert result.message.startswith(
        PLAIN + f"{HOST} answered HTTP 200 and the pinned sha256, but no ffprobe.exe in a bin folder"
    )
    assert e.file("GITHUB_PATH") == ""


def test_a_program_that_does_not_run_fails_the_step_and_is_not_cached(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, data)
    e.run = lambda exe: (1, "") if exe.endswith("ffprobe.exe") else banner(exe)
    result = run_install(e, data)
    assert not result.ok
    assert "ffprobe -version did not run (exit code 1: no output)" in result.message
    assert not e.folder.exists(), "a broken build must not be what the cache saves"
    assert "source=none" in e.file("GITHUB_OUTPUT")


def test_a_zip_that_is_not_a_zip_fails_with_the_plain_message(tmp_path: Path):
    junk = b"this is not a zip"
    e = Env(tmp_path, junk)
    result = run_install(e, junk)
    assert not result.ok and "the zip cannot be read" in result.message


def test_a_banner_that_is_not_ffmpegs_does_not_count_as_running(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, data)
    e.run = lambda exe: (0, "Microsoft Windows [Version 10.0]")
    result = run_install(e, data)
    assert not result.ok and "ffmpeg -version did not run (exit code 0: Microsoft Windows" in result.message


def test_the_message_names_the_source_and_the_status(tmp_path: Path):
    data = make_zip()
    cases = [
        ((503, 503, 503, 503), "HTTP 503 after 4 attempts"),
        ((404,), "HTTP 404"),
        ((410,), "HTTP 410"),
    ]
    for outcomes, status in cases:
        e = Env(tmp_path / str(outcomes[0]), *outcomes)
        result = run_install(e, data)
        assert f"{PLAIN}{HOST} answered {status}" in result.message, result.message


# ---- the job summary ---------------------------------------------------------------------------------------
def test_the_job_summary_gets_the_one_line_reason_for_an_outage_so_a_rerun_is_obvious(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, 502, 502, 502, 502)
    result = run_install(e, data)
    summary = e.file("GITHUB_STEP_SUMMARY")
    assert "upstream outage: rerun the failed jobs" in summary
    assert result.message in summary
    lines = [ln for ln in summary.splitlines() if ln.strip() and not ln.startswith("#")]
    assert len(lines) == 1, "one line"


def test_the_job_summary_for_a_wrong_pin_says_it_is_not_an_outage(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, 404)
    run_install(e, data)
    summary = e.file("GITHUB_STEP_SUMMARY")
    assert "not an outage" in summary and "rerun" not in summary
    assert f"{PLAIN}{HOST} answered HTTP 404" in summary


def test_a_good_install_writes_the_version_to_the_job_summary_too(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, data)
    run_install(e, data)
    assert "FFmpeg ready from download" in e.file("GITHUB_STEP_SUMMARY")
    assert "ffmpeg version n8.1.3-27-gtest" in e.file("GITHUB_STEP_SUMMARY")


def test_nothing_is_written_when_github_gave_no_files():
    # a laptop run: no GITHUB_* variables, and nothing raises
    ci.write_summary("x", ci.Deps(env={}))
    ci.write_output("k", "v", ci.Deps(env={}))


# ---- the cache ---------------------------------------------------------------------------------------------
def prefill(e: Env) -> None:
    e.folder.mkdir(parents=True)
    (e.folder / "ffmpeg.exe").write_bytes(b"cached-ffmpeg")
    (e.folder / "ffprobe.exe").write_bytes(b"cached-ffprobe")


def test_a_restored_cache_is_used_without_any_download_and_goes_on_path(tmp_path: Path):
    e = Env(tmp_path)  # a transport with nothing scripted: any request fails the test
    prefill(e)
    result = run_install(e, make_zip())
    assert result.ok and result.source == "cache"
    assert e.transport.urls == []
    assert e.file("GITHUB_PATH").strip() == str(e.folder)
    assert "source=cache" in e.file("GITHUB_OUTPUT")
    assert (e.folder / "ffmpeg.exe").read_bytes() == b"cached-ffmpeg"


def test_a_restored_cache_that_does_not_run_is_replaced_by_a_download(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, data)
    prefill(e)
    calls = {"n": 0}

    def run(exe: str) -> tuple[int, str]:
        calls["n"] += 1
        return (
            (1, "") if calls["n"] == 1 else banner(exe)
        )  # the cached ffmpeg is broken, the fresh one is fine

    e.run = run
    result = run_install(e, data)
    assert result.ok and result.source == "download"
    assert "the FFmpeg restored from the cache does not run" in e.text()
    assert (e.folder / "ffmpeg.exe").read_bytes() == b"MZ-ffmpeg"


def test_a_cache_with_only_one_program_is_a_miss(tmp_path: Path):
    data = make_zip()
    e = Env(tmp_path, data)
    e.folder.mkdir(parents=True)
    (e.folder / "ffmpeg.exe").write_bytes(b"lonely")
    assert run_install(e, data).source == "download"


def test_the_key_has_the_pinned_version_the_runner_os_and_the_start_of_the_hash():
    pin = ci.Pin("n8.1.3-27-gtest", URL, "ab" * 32)
    env = {"RUNNER_OS": "Windows", "RUNNER_ARCH": "X64", "RUNNER_TEMP": "D:\\a\\_temp"}
    key = ci.cache_key(pin, env)
    assert "n8.1.3-27-gtest" in key and "Windows" in key and "X64" in key and ("ab" * 6) in key
    assert ci.cache_key(pin, {**env, "RUNNER_OS": "Linux"}) != key
    assert ci.cache_key(ci.Pin("n8.1.4", URL, "ab" * 32), env) != key
    assert ci.cache_key(ci.Pin("n8.1.3-27-gtest", URL, "cd" * 32), env) != key
    assert ci.cache_folder(pin, env) == Path("D:\\a\\_temp") / "ns-ffmpeg" / "n8.1.3-27-gtest"


# ---- the pin and the command line --------------------------------------------------------------------------
def test_the_pin_is_the_manifest_bins_ffmpeg_entry():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["bins"]["ffmpeg"]
    pin = ci.load_pin()
    assert (pin.version, pin.url, pin.sha256) == (manifest["version"], manifest["url"], manifest["sha256"])
    assert pin.host == "github.com" and "BtbN/FFmpeg-Builds" in pin.url
    assert pin.version.startswith("n8.1.3"), "the build CI tests on is the one the product runs"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"bins": {}},
        {"bins": {"ffmpeg": {"version": "n1", "url": "http://x/y.zip", "sha256": "ab" * 32}}},
        {"bins": {"ffmpeg": {"version": "n1", "url": "https://x/y.zip", "sha256": "xyz"}}},
        {"bins": {"ffmpeg": {"version": "../../evil", "url": "https://x/y.zip", "sha256": "ab" * 32}}},
    ],
)
def test_an_unusable_manifest_entry_fails_the_step_in_plain_words(tmp_path: Path, bad: dict[str, Any]):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(bad), encoding="utf-8")
    log: list[str] = []
    code = ci.main(["install", "--manifest", str(manifest)], ci.Deps(say=log.append, env={}))
    assert code == 1
    assert log and log[0].startswith("::error::" + PLAIN)


def test_the_pin_command_writes_key_dir_and_version_for_the_cache_steps(tmp_path: Path):
    e = Env(tmp_path)
    assert ci.main(["pin"], e.deps()) == 0
    out = dict(ln.split("=", 1) for ln in e.file("GITHUB_OUTPUT").splitlines())
    pin = ci.load_pin()
    assert out["version"] == pin.version
    assert out["key"] == ci.cache_key(pin, e.env)
    assert out["dir"] == str(ci.cache_folder(pin, e.env))


def fake_manifest(tmp_path: Path, pin: ci.Pin) -> Path:
    manifest = tmp_path / "manifest.json"
    entry = {"version": pin.version, "url": pin.url, "sha256": pin.sha256}
    manifest.write_text(json.dumps({"bins": {"ffmpeg": entry}}), encoding="utf-8")
    return manifest


def test_the_install_command_exits_1_with_the_plain_error_line_and_0_when_it_works(tmp_path: Path):
    data = make_zip()
    manifest = fake_manifest(tmp_path, pin_for(data))
    bad = Env(tmp_path / "bad", 504, 504, 504, 504)
    assert ci.main(["install", "--manifest", str(manifest)], bad.deps()) == 1
    assert any(ln.startswith("::error::" + PLAIN + f"{HOST} answered HTTP 504") for ln in bad.log)
    good = Env(tmp_path / "good", data)
    assert ci.main(["install", "--manifest", str(manifest)], good.deps()) == 0
    assert "source=download" in good.file("GITHUB_OUTPUT")


def test_a_bug_in_the_script_still_fails_the_step_in_plain_words(tmp_path: Path):
    data = make_zip()
    manifest = fake_manifest(tmp_path, pin_for(data))
    e = Env(tmp_path / "run", data)

    def boom(exe: str) -> tuple[int, str]:
        raise RuntimeError("boom")

    e.run = boom
    assert ci.main(["install", "--manifest", str(manifest)], e.deps()) == 1
    assert any(ln.startswith("::error::" + PLAIN + "this script failed (RuntimeError: boom)") for ln in e.log)
    assert "install script broke" in e.file("GITHUB_STEP_SUMMARY")


# ---- unpacking mirrors the product's own unpack_ffmpeg -----------------------------------------------------
def test_unpack_takes_the_two_programs_from_a_bin_folder_and_nothing_else(tmp_path: Path):
    archive = tmp_path / "f.zip"
    archive.write_bytes(
        make_zip(
            {
                # the decoys come FIRST: without the bin-folder rule they would be written, and the real
                # members after them skipped as already written
                f"{ROOT_DIR}/doc/ffmpeg.exe": b"not in bin",
                "ffprobe.exe": b"top level, not in bin",
                f"{ROOT_DIR}/bin/ffmpeg.exe": b"MZ1",
                f"{ROOT_DIR}/bin/ffprobe.exe": b"MZ2",
                f"{ROOT_DIR}/bin/ffplay.exe": b"MZ3",
            }
        )
    )
    written = ci.unpack(archive, tmp_path / "out", HOST)
    assert sorted(written) == ["ffmpeg.exe", "ffprobe.exe"]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]
    assert (tmp_path / "out" / "ffmpeg.exe").read_bytes() == b"MZ1"
    assert (tmp_path / "out" / "ffprobe.exe").read_bytes() == b"MZ2"


def test_unpack_fails_when_the_programs_are_only_outside_a_bin_folder(tmp_path: Path):
    archive = tmp_path / "f.zip"
    members = {f"{ROOT_DIR}/doc/ffmpeg.exe": b"x", f"{ROOT_DIR}/bin/ffmpeg.exe": b"y", "ffprobe.exe": b"z"}
    archive.write_bytes(make_zip(members))
    with pytest.raises(ci.SourceError) as err:
        ci.unpack(archive, tmp_path / "out", HOST)
    assert "no ffprobe.exe" in str(err.value)


def test_the_product_unpacker_and_the_ci_unpacker_agree_on_the_same_zip(tmp_path: Path):
    archive = tmp_path / "f.zip"
    archive.write_bytes(make_zip())
    mine = ci.unpack(archive, tmp_path / "ci", HOST)
    theirs = bootstrap.unpack_ffmpeg(archive, tmp_path / "product")
    assert sorted(mine) == sorted(theirs)
    for name in mine:
        assert (tmp_path / "ci" / name).read_bytes() == (tmp_path / "product" / name).read_bytes()


# ---- the real `-version` runner never raises ---------------------------------------------------------------
def test_run_version_reports_a_program_that_cannot_start_instead_of_raising(tmp_path: Path):
    code, text = ci.run_version(str(tmp_path / "no-such-ffmpeg.exe"))
    assert code == -1 and text


@pytest.mark.skipif(sys.platform != "win32", reason="a .cmd stands in for the program")
def test_run_version_runs_a_stand_in_program_and_returns_its_first_line(tmp_path: Path):
    stand_in = tmp_path / "ffmpeg.cmd"
    stand_in.write_text("@echo ffmpeg version stand-in\r\n@echo second line\r\n", encoding="ascii")
    assert ci.run_version(str(stand_in)) == (0, "ffmpeg version stand-in")


def test_the_script_imports_only_the_standard_library():
    tree = ast.parse((ROOT / "dev" / "ci_install_ffmpeg.py").read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}, modules


# ---- the retry rules ARE the rules of tests/online.py ------------------------------------------------------
def test_the_constants_equal_those_of_tests_online():
    assert ci.RETRIES == online.RETRIES == 3
    assert ci.BACKOFF_S == online.BACKOFF_S == (1.0, 2.0, 4.0)
    assert ci.OUTAGE_MARK == online.OUTAGE_MARK


def test_the_status_classes_equal_those_of_tests_online():
    for code in range(100, 600):
        assert ci.is_transient_status(code) == online.is_transient_status(code), code


def _response(code: int) -> httpx.Response:
    return httpx.Response(code, request=httpx.Request("GET", URL))


def _status_error(code: int) -> httpx.HTTPStatusError:
    r = _response(code)
    return httpx.HTTPStatusError("x", request=r.request, response=r)


def test_the_exception_classes_equal_those_of_tests_online():
    gaierror = socket.gaierror(11001, "getaddrinfo failed")
    dns = httpx.ConnectError("dns")
    dns.__cause__ = gaierror
    refused = ConnectionRefusedError(10061, "refused")
    pairs: list[tuple[str, BaseException, BaseException, bool]] = [
        ("HTTP 504", _status_error(504), http_error(504), True),
        ("HTTP 429", _status_error(429), http_error(429), True),
        ("HTTP 404", _status_error(404), http_error(404), False),
        ("HTTP 403", _status_error(403), http_error(403), False),
        ("timeout", httpx.ReadTimeout("t"), TimeoutError("timed out"), True),
        ("refused", httpx.ConnectError("refused"), refused, True),
        ("reset", httpx.ReadError("reset"), ConnectionResetError(10054, "reset"), True),
        ("url error", httpx.ConnectError("refused"), urllib.error.URLError(refused), True),
        ("protocol", httpx.RemoteProtocolError("eof"), http.client.RemoteDisconnected("eof"), True),
        ("cut off", httpx.RemoteProtocolError("short"), http.client.IncompleteRead(b"abc", 10), True),
        ("dns", dns, urllib.error.URLError(gaierror), False),
        ("not ours", ValueError("x"), ValueError("x"), False),
    ]
    for name, theirs, mine, transient in pairs:
        assert (online.transient_detail(theirs) is not None) is transient, f"online: {name}"
        assert (ci.transient_detail(mine) is not None) is transient, f"ci: {name}"


@pytest.mark.parametrize(
    ("outcomes", "sleeps", "outage"),
    [
        ((504, 504, 200), [1.0, 2.0], False),
        ((404,), [], False),
        ((429, 200), [1.0], False),
        ((503, 503, 503, 503), [1.0, 2.0, 4.0], True),
        ((500, 502, 504, 200), [1.0, 2.0, 4.0], False),
        ((403,), [], False),
    ],
)
def test_the_retry_loop_does_what_tests_online_retry_call_does(
    outcomes: tuple[int, ...], sleeps: list[float], outage: bool
):
    # tests/online.py: one httpx response per call
    online.forget_outages()
    theirs_todo = list(outcomes)
    theirs_calls: list[int] = []
    theirs_sleeps: list[float] = []

    def theirs() -> httpx.Response:
        code = theirs_todo.pop(0)
        theirs_calls.append(code)
        return _response(code)

    theirs_outage = False
    theirs_final: int | None = None
    try:
        theirs_final = online.retry_call(theirs, host="x.example", sleep=theirs_sleeps.append).status_code
    except online.UpstreamOutageError:
        theirs_outage = True
    online.forget_outages()

    # dev/ci_install_ffmpeg.py: urllib raises for any status of 400 or more
    mine_todo = list(outcomes)
    mine_calls: list[int] = []
    mine_sleeps: list[float] = []

    def mine() -> int:
        code = mine_todo.pop(0)
        mine_calls.append(code)
        if code >= 400:
            raise http_error(code)
        return code

    mine_outage = False
    mine_final: int | None = None
    try:
        mine_final = ci.retry(mine, source="x.example", sleep=mine_sleeps.append, say=lambda _t: None)
    except ci.SourceError as e:
        mine_outage = e.transient
        if not e.transient:
            mine_final = int(e.status.split()[1])  # "HTTP 404": stopped at once, untouched

    assert mine_calls == theirs_calls
    assert mine_sleeps == theirs_sleeps == sleeps
    assert mine_outage == theirs_outage == outage
    if not outage:
        assert mine_final == theirs_final == outcomes[-1]
