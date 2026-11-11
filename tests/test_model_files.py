"""Card model-file-damaged: a damaged model file is found, named, and downloaded again by Repair.

Every install here is real small files: the repository's manifest.json with its model pins replaced by the
SHA256 and size of 2 KiB stand-in files (``make_install``), the answers, install state and runtime.json setup
writes, and setup's ``.ok`` markers. Damage is one byte flipped in the middle of a file with its date put
back (``flip``), so size and date still match the marker. A model's real size (14.3 GB) comes only through
the size seam (``verify_downloads._size``), never as bytes: ``_disk_guard`` fails any test that leaves more
than 4 MiB under its folder. Downloads come from an in-process stand-in of the download site
(``site``: HEAD and ranged GET by URL); no test touches the network.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import random
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from installer.stage2 import downloader as dl
from installer.stage2 import verify_downloads as vd
from installer.stage2.testing import example_answers, make_test_context
from ns_common.config import load_manifest, save_install_state, save_runtime
from ns_common.errors import NsError, describe
from ns_common.models import InstallState, Runtime, StepState
from ns_common.paths import Paths, repo_root

SIZE = 2048
BIG = 15_354_508_083  # 14.3 GB (the size seam's answer for the story model in the progress test)
LIMIT_BYTES = 4 * 1024 * 1024


@pytest.fixture(autouse=True)
def _disk_guard(tmp_path: Path) -> Iterator[None]:
    yield
    sizes = [(p.stat().st_size, p) for p in tmp_path.rglob("*") if p.is_file()]
    total = sum(s for s, _ in sizes)
    assert total <= LIMIT_BYTES, f"the test left {total} bytes; largest: {max(sizes, default=(0, None))}"


def content(name: str) -> bytes:
    """Stand-in bytes of a planned file by its plan name (``x.gguf``, ``whisper/model.bin``, ``yunet``)."""
    seed = hashlib.sha256(name.encode()).digest()
    return (seed * (SIZE // len(seed) + 1))[:SIZE]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def flip(path: Path) -> None:
    """One byte changed in the middle, the date kept: what a bit-rotted or half-copied model looks like."""
    st = path.stat()
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


@dataclass
class Install:
    paths: Paths
    app: Path
    items: list[dl.DownloadItem]

    def file(self, part: str) -> Path:
        return next(Path(i.dest) for i in self.items if part in i.name)


def old_marker(dest: Path) -> None:
    """A marker as 0.1.14 wrote it: size, date and the pin, no ``hashed_at``."""
    st = dest.stat()
    dl._marker(dest).write_text(json.dumps({"size": st.st_size, "mtime_ns": st.st_mtime_ns,
                                            "sha256": sha(dest.read_bytes())}), encoding="utf-8")  # fmt: skip


def standin_manifest(big: dict[str, int] | None = None) -> dict[str, Any]:
    """The repository's manifest.json with the gpu profile's model pins (and every pinned program) replaced by
    the SHA256 and size of the 2 KiB stand-in of each file (``content``); ``big`` gives a story model tier
    another size through the manifest only."""
    man = json.loads((repo_root() / "manifest.json").read_text(encoding="utf-8"))
    gpu = man["profiles"]["gpu"]
    for key in ("llm", "llm_balanced", "llm_best", "story_llm", "embed"):
        f = gpu.get(key)
        if f:
            f["sha256"], f["bytes"] = sha(content(Path(f["file"]).name)), (big or {}).get(key, SIZE)
    for f in gpu["tts"] + gpu.get("tts_optional", []):
        f["sha256"], f["bytes"] = sha(content(Path(f["file"]).name)), SIZE
    gpu["whisper_repo"]["files_sha256"] = {
        k: sha(content(f"whisper/{k}")) for k in gpu["whisper_repo"]["files_sha256"]
    }
    for key, pin in man["bins"].items():
        if isinstance(pin, dict) and dl.pinned_sha256(pin.get("sha256")):
            pin["sha256"] = sha(content(key))
    return man


def make_install(
    root: Path,
    *,
    home: Path | None = None,
    completed: bool = True,
    files: bool = True,
    markers: str = "new",
    runtime: bool = True,
    custom: dict[str, Any] | None = None,
    big: dict[str, int] | None = None,
) -> Install:
    """A finished gpu install (small tier, voice design, the story model) whose models are 2 KiB files."""
    app = root / "app"
    app.mkdir(parents=True, exist_ok=True)
    man = standin_manifest(big)
    gpu = man["profiles"]["gpu"]
    (app / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    paths = Paths(home or root / "home")
    paths.ensure()
    extra = {"llm_custom": custom} if custom else {}
    answers = example_answers(llm_tier="small", include_voicedesign=True, install_code=False, **extra)
    (paths.config / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    save_install_state(paths, InstallState(
        app_path=str(app), profile="gpu", started_at="2026-11-01T02:00:00+00:00",
        completed_at="2026-11-01T03:00:00+00:00" if completed else None,
        steps={"s03_download_models": StepState(status="done")} if completed else {},
    ))  # fmt: skip
    if runtime:
        rt = Runtime.default("gpu")
        rt.llama.model_path = str(paths.models_llm / Path(gpu["llm"]["file"]).name)
        rt.llama.story_model_path = str(paths.models_llm / Path(gpu["story_llm"]["file"]).name)
        rt.embed.model_path = str(paths.models_embed / Path(gpu["embed"]["file"]).name)
        save_runtime(paths, rt)
    plan = vd.plan_for_install(paths, app)
    story = paths.models_llm / Path(gpu["story_llm"]["file"]).name
    if runtime and not any(Path(i.dest) == story for i in plan):  # the plan of 0.1.14 has no story model
        plan.append(dl.DownloadItem(name=story.name, url="https://huggingface.co/x/story", dest=story))
    items = [i for i in plan if i.kind == "file" and "models" in Path(i.dest).parts and not i.optional]
    for i in items if files else []:
        Path(i.dest).parent.mkdir(parents=True, exist_ok=True)
        Path(i.dest).write_bytes(content(i.name))
        if markers == "new":
            dl.write_marker(Path(i.dest), sha(content(i.name)))
        elif markers == "old":
            old_marker(Path(i.dest))
    return Install(paths, app, items)


class Down:
    base_url = "http://127.0.0.1:1"

    def is_up(self) -> bool:
        return False


def doctor(inst: Install, monkeypatch: pytest.MonkeyPatch) -> Any:
    import ns_supervisor.cli as ns_cli
    from ns_supervisor import doctor as doc
    from ns_supervisor import procs

    monkeypatch.setattr(procs, "nvidia_smi_report", lambda smi=None: None)
    monkeypatch.setattr(doc.ffmpeg, "available", lambda: True)
    monkeypatch.setattr(doc, "stock_key_rows", lambda paths: [])
    monkeypatch.setattr(ns_cli, "_client", lambda p: Down())
    return CliRunner().invoke(ns_cli.app, ["doctor"], env={"NIGHTSHIFT_HOME": str(inst.paths.home)})


def verify_downloads(inst: Install) -> Any:
    import ns_supervisor.cli as ns_cli

    return CliRunner().invoke(
        ns_cli.app, ["verify", "--downloads"], env={"NIGHTSHIFT_HOME": str(inst.paths.home)}
    )


def row(output: str, name: str) -> str:
    return next((ln for ln in output.splitlines() if re.match(rf"\[(ok|FAIL|WARN)\] {name}:", ln)), "")


def record(inst: Install) -> dict[str, Any]:
    f = inst.paths.logs / "model-check.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


# --------------------------------------------------------------------------------------------- M1, M2
def test_doctor_checks_every_model_file_and_its_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inst = make_install(tmp_path, markers="old")
    res = doctor(inst, monkeypatch)
    for name in ("language model files", "story model files", "embed model files", "voice model files",
                 "whisper model files"):
        assert row(res.output, name).startswith("[ok]"), (name, res.output)
    assert "4 files present" in row(res.output, "voice model files")  # 2 talkers, the tokenizer, voice design
    hashed = row(res.output, "model files last checked by hash")
    assert (
        hashed.startswith("[WARN]") and "never" in hashed and "Run 'ns verify --downloads' (about" in hashed
    )
    assert "here" in hashed, hashed  # the estimate names the size and the speed
    (inst.file("talker-1.7b-base")).unlink()
    model_bin = inst.file("whisper/model.bin")
    model_bin.write_bytes(model_bin.read_bytes() + b"x")
    res = doctor(inst, monkeypatch)
    voice, whisper = row(res.output, "voice model files"), row(res.output, "whisper model files")
    assert voice.startswith("[FAIL]") and "missing" in voice and "choose Repair" in voice, voice
    assert whisper.startswith("[FAIL]") and "size 2049 bytes" in whisper and "choose Repair" in whisper, (
        whisper
    )
    assert res.exit_code == 1


def test_a_custom_model_has_no_hash_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = {"repo": "someone/Tiny-GGUF", "file": "tiny-custom.gguf", "size": SIZE}
    inst = make_install(tmp_path, custom=custom, runtime=False)
    res = doctor(inst, monkeypatch)
    lang = row(res.output, "language model files")
    assert lang.startswith("[ok]") and "tiny-custom.gguf" in lang and "cannot be checked by hash" in lang, (
        lang
    )


# --------------------------------------------------------------------------------------------- M3, M4
class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_verify_downloads_reports_progress_speed_and_time_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = make_install(tmp_path, big={"story_llm": BIG})
    story = next(i for i in inst.items if "Heretic" in i.name)
    clock = FakeClock()
    real = dl.sha256_file

    def size(path: Path) -> int:
        return BIG if path.name == Path(story.dest).name else path.stat().st_size

    def hasher(path: Path, progress: Callable[[int, int | None], None] | None = None) -> str:
        total = size(path)
        for k in range(1, 11):  # ten chunks, 0.4 s each on the check's clock
            clock.t += 0.4
            if progress is not None:
                progress(total * k // 10, total)
        return real(path)

    monkeypatch.setattr(vd, "_size", size, raising=False)
    monkeypatch.setattr(vd, "_now", clock, raising=False)
    monkeypatch.setattr(vd, "sha256_file", hasher)
    res = verify_downloads(inst)
    lines = res.output.splitlines()
    total = next((n for n, ln in enumerate(lines) if ln.startswith("Checking ")), None)
    first = next((n for n, ln in enumerate(lines) if ln.startswith("[1/")), -1)
    assert total is not None and total < first and "read by hash (about" in lines[total], res.output
    mine = [ln for ln in lines if ln.startswith(f"checking {story.name} (14.3 GB): ")]
    pattern = re.compile(r": \d+% at [\d.]+ [GM]B/s, about \d+ (s|minutes?) left$")
    assert len(mine) >= 2 and all(pattern.search(ln) for ln in mine), mine
    assert len(mine) <= 5, (
        f"more than once a second over 4 s: {mine}"
    )  # 10 chunks x 0.4 s, said once a second
    rec = record(inst)
    key = Path(story.dest).relative_to(inst.paths.home).as_posix()
    assert rec[key]["verdict"] == "ok" and rec[key]["sha256_ok"] is True and rec[key]["seconds"] == 4.0, rec


def test_an_interrupted_check_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inst = make_install(tmp_path)

    def snapshot() -> dict[str, tuple[int, int, bytes]]:
        return {str(p): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
                for p in (inst.paths.home / "models").rglob("*") if p.is_file()}  # fmt: skip

    before = snapshot()

    def stopped(path: Path, progress: Any = None) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(vd, "sha256_file", stopped)
    res = verify_downloads(inst)
    assert res.exit_code == 130, res.output
    assert "stopped: nothing was changed" in res.output, res.output
    assert re.search(r"\d+ of \d+ files were checked", res.output), res.output
    assert snapshot() == before
    assert not (inst.paths.logs / "model-check.json").exists()


def test_a_damaged_file_is_named_with_the_way_out(tmp_path: Path) -> None:
    inst = make_install(tmp_path)
    llm = inst.file("Qwen3-")
    flip(llm)
    res = verify_downloads(inst)
    assert res.exit_code == 1
    assert re.search(rf"{re.escape(llm.name)}\s+bad hash", res.output), res.output
    assert f"Run INSTALL.cmd again and choose Repair: it downloads {llm.name} (2.0 KB) again." in res.output
    good = [i for i in inst.items if Path(i.dest) != llm and dl.pinned_sha256(i.sha256)]
    assert all(re.search(rf"{re.escape(i.name)}\s+ok\b", res.output) for i in good), res.output
    assert record(inst)[llm.relative_to(inst.paths.home).as_posix()]["verdict"] == "bad hash"


# ------------------------------------------------------------------------------------------------- M5
def site(served: dict[str, bytes], fetched: list[str], refused: set[str]) -> httpx.Client:
    """The download site's stand-in: HEAD gives the size, GET the bytes (ranged when asked); a refused URL
    answers 404 (the file was taken down)."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        body = served.get(url)
        if body is None or url in refused:
            return httpx.Response(404)
        if req.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(body))})
        fetched.append(url)
        rng = req.headers.get("range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            return httpx.Response(
                206,
                content=body[start:],
                headers={"content-range": f"bytes {start}-{len(body) - 1}/{len(body)}"},
            )
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def repair_run(tmp_path: Path, inst: Install, client: httpx.Client, *, free_gb: float = 500.0) -> int:
    """INSTALL.cmd over the install, unattended: the wizard with the models step (S03)."""
    from installer.stage2.steps.s03_download_models import S03DownloadModels
    from installer.stage2.wizard import Wizard

    ctx = make_test_context(tmp_path, manifest=load_manifest(inst.app / "manifest.json"), app_path=inst.app,
                            simulate=False, http=lambda: client)  # fmt: skip
    ctx.free_gb_fn = lambda _p: free_gb
    return Wizard(ctx, [S03DownloadModels()], backoff=()).run()


def counting(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every file hashed, by the checker or by the downloader."""
    seen: list[str] = []
    real = dl.sha256_file

    def hasher(path: Path, chunk: int = dl.CHUNK, **kw: Any) -> str:
        seen.append(Path(path).name.removesuffix(".part"))
        return real(path)

    monkeypatch.setattr(vd, "sha256_file", hasher)
    monkeypatch.setattr(dl, "sha256_file", hasher)
    return seen


def test_repair_checks_every_model_by_hash_and_downloads_a_damaged_one_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = make_install(tmp_path)
    llm = inst.file("Qwen3-")
    flip(llm)
    damaged = llm.read_bytes()
    served = {i.url: content(i.name) for i in inst.items}
    url = next(i.url for i in inst.items if Path(i.dest) == llm)
    fetched: list[str] = []
    hashed = counting(monkeypatch)
    bad = llm.with_name(llm.name + ".bad")
    # first try: the site has taken the file down (404): the step stops, the damaged copy is kept aside
    assert repair_run(tmp_path, inst, site(served, fetched, {url})) != 0
    pinned = [Path(i.dest).name for i in inst.items if dl.pinned_sha256(i.sha256)]
    assert sorted(hashed) == sorted(pinned), "a Repair hashes every model with a pin, once"
    assert bad.read_bytes() == damaged and not llm.exists() and fetched == []
    # the retry: only the damaged file is fetched, nothing is hashed twice, the .bad goes once it verified
    hashed.clear()
    assert repair_run(tmp_path, inst, site(served, fetched, set())) == 0
    assert fetched == [url], fetched
    assert hashed == [llm.name], hashed  # the downloader's check of the new file only
    assert llm.read_bytes() == content(llm.name) and not bad.exists()
    flags = json.loads((inst.paths.config / "wizard_flags.json").read_text(encoding="utf-8"))
    assert "models_recheck" not in flags
    assert (
        record(inst)[llm.relative_to(inst.paths.home).as_posix()]["verdict"] == "bad hash"
    )  # (the old file)


def test_a_repair_with_no_room_for_the_new_file_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = make_install(tmp_path)
    llm = inst.file("Qwen3-")
    flip(llm)
    fetched: list[str] = []
    served = {i.url: content(i.name) for i in inst.items}
    assert repair_run(tmp_path, inst, site(served, fetched, set()), free_gb=0.5) != 0
    report = json.loads((inst.paths.logs / "install-report.json").read_text(encoding="utf-8"))
    assert "NS-E104" in json.dumps(report) and llm.exists() and fetched == [], report
    assert not llm.with_name(llm.name + ".bad").exists()


def test_a_damaged_file_that_is_held_open_is_refused_with_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Written by the final reviewer, NOT RUN. Windows refuses to move a file a program has open: the Repair
    stops with NS-E119 naming the model and Stop All; the file and its marker stay, nothing is fetched."""
    from installer.stage2 import model_repair

    inst = make_install(tmp_path)
    llm = inst.file("Qwen3-")
    flip(llm)
    damaged = llm.read_bytes()

    def held(src: Path, dst: Path, **kw: Any) -> None:
        raise PermissionError(13, "The process cannot access the file because it is being used", str(src))

    monkeypatch.setattr(model_repair, "replace_atomic", held)
    fetched: list[str] = []
    served = {i.url: content(i.name) for i in inst.items}
    assert repair_run(tmp_path, inst, site(served, fetched, set())) != 0
    report = json.dumps(json.loads((inst.paths.logs / "install-report.json").read_text(encoding="utf-8")))
    assert "NS-E119" in report, report
    assert llm.read_bytes() == damaged and dl._marker(llm).is_file() and fetched == []
    assert not llm.with_name(llm.name + ".bad").exists()


def test_a_first_install_does_not_hash_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inst = make_install(tmp_path, completed=False, files=False)
    served = {i.url: content(i.name) for i in inst.items}
    fetched: list[str] = []
    hashed = counting(monkeypatch)
    assert repair_run(tmp_path, inst, site(served, fetched, set())) == 0
    downloaded = sorted(Path(i.dest).name for i in inst.items if i.url in fetched)
    assert downloaded and sorted(hashed) == downloaded, "each new file is hashed once, by the downloader"
    # the other end: the same install, finished, and INSTALL.cmd run over it (a Repair)
    from installer.stage2.state import StateStore

    store = StateStore(inst.paths, inst.app)
    store.complete()
    hashed.clear()
    fetched.clear()
    assert repair_run(tmp_path, inst, site(served, fetched, set())) == 0
    pinned = [Path(i.dest).name for i in inst.items if dl.pinned_sha256(i.sha256)]
    assert sorted(hashed) == sorted(pinned) and fetched == []


def test_a_marker_from_the_previous_release_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inst = make_install(tmp_path, markers="old", completed=False)
    hashed = counting(monkeypatch)
    from installer.stage2.state import StateStore

    StateStore(inst.paths, inst.app).complete()
    assert repair_run(tmp_path, inst, site({}, [], set())) == 0
    pinned = [i for i in inst.items if dl.pinned_sha256(i.sha256)]
    assert sorted(hashed) == sorted(Path(i.dest).name for i in pinned)
    stamps = [
        json.loads(dl._marker(Path(i.dest)).read_text(encoding="utf-8")).get("hashed_at") for i in pinned
    ]
    assert all(stamps), stamps


def test_the_setup_window_over_a_finished_install_with_a_damaged_model_opens_the_models_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_installer_app_copy import app_setup, fake_zip_folder
    from tests.test_installer_web_controller import finished_install

    zip_a = fake_zip_folder(tmp_path / "zip-a", "0.1.4")
    first = app_setup(tmp_path, monkeypatch, zip_a)
    finished_install(first, zip_a)
    done = app_setup(tmp_path, monkeypatch, zip_a).snap()
    assert done["run"] == "finished"  # nothing owed: Done, as before
    model = first.home / "models" / "llm" / "a.gguf"
    model.parent.mkdir(parents=True, exist_ok=True)  # (the finished install has its own model there)
    model.write_bytes(content("a.gguf"))
    dl.write_marker(model, sha(content("a.gguf")))
    model.write_bytes(content("a.gguf") + b"!")  # changed since setup verified it
    from tests.test_installer_web_controller import Setup

    state = Setup(tmp_path, monkeypatch).snap()  # (the first window built over the damaged install)
    steps = {st["id"]: st["status"] for st in state["steps"]}
    assert state["run"] != "finished" and steps["s03_download_models"] == "pending", steps
    assert all(s in ("done", "skipped") for k, s in steps.items() if k != "s03_download_models"), steps
    said = " ".join(line["text"] for line in state["log"])
    assert "models/llm/a.gguf: size changed" in said and "Continue checks every model file by hash" in said


class _Wire:
    """The wizard's stdout as the setup window's engine reads it: one JSON event per line, handed on."""

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self._emit, self._buf = emit, ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._emit(json.loads(line))
        return len(text)

    def flush(self) -> None:
        return None


def test_the_setup_window_brings_back_a_model_file_deleted_with_its_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 1 of the final review, written by its builder, NOT RUN. The owner emptied ``models\\embed``
    (the file and its marker gone) and the language model is damaged with its size and date kept (nothing
    has seen it). `ns doctor` says "choose Repair"; the window used to show Done from the markers. Now it
    decides from the plan: it opens on the models step, and Continue runs the real wizard's models step
    (behind the window's engine, its events on the window's wire) against the download site's stand-in:
    the Repair hashes every model, sets the damaged one aside, fetches both, deletes the ``.bad`` and
    clears its flag."""
    import io

    from installer.stage2.bridge import BridgeUi
    from installer.stage2.steps.s03_download_models import S03DownloadModels
    from installer.stage2.wizard import Wizard
    from tests.test_installer_app_copy import app_setup, fake_zip_folder
    from tests.test_installer_web_controller import finished_install

    zip_a = fake_zip_folder(tmp_path / "zip-a", "0.1.4")
    (zip_a / "manifest.json").write_text(json.dumps(standin_manifest(), indent=2), encoding="utf-8")
    first = app_setup(tmp_path, monkeypatch, zip_a)
    items = finished_install(first, zip_a, content=lambda i: content(i.name))
    healthy = app_setup(tmp_path, monkeypatch, zip_a).snap()
    assert healthy["run"] == "finished", healthy["log"]  # every planned file verified: Done, as before
    home = first.home
    embed = next(Path(i.dest) for i in items if Path(i.dest).parent.name == "embed")
    llm = next(Path(i.dest) for i in items if Path(i.dest).parent.name == "llm")
    embed.unlink()
    dl._marker(embed).unlink()                     # no marker is left to say the file is gone
    flip(llm)
    served = {i.url: content(i.name) for i in items}
    fetched: list[str] = []
    client = site(served, fetched, set())
    codes: list[int] = []

    def wizard(e: Any) -> None:
        assert e.wait_command("keys", 10)
        e.emit({"type": "hello", "pid": os.getpid(), "version": "0.1.4", "resumed": True})
        app = home / "app"
        wire: Any = _Wire(e.emit)               # (a text stream to BridgeUi: write and flush are all it uses)
        ctx = make_test_context(tmp_path, ui=BridgeUi(out=wire, inp=io.StringIO()),
                                manifest=load_manifest(app / "manifest.json"), app_path=app, simulate=False,
                                http=lambda: client)  # fmt: skip
        codes.append(Wizard(ctx, [S03DownloadModels()], backoff=()).run())
        e.exit(codes[-1])

    window = app_setup(tmp_path, monkeypatch, zip_a, wizard)
    state = window.snap()
    steps = {st["id"]: st["status"] for st in state["steps"]}
    assert state["run"] != "finished" and steps["s03_download_models"] == "pending", steps
    assert all(s in ("done", "skipped") for k, s in steps.items() if k != "s03_download_models"), steps
    said = " ".join(line["text"] for line in state["log"])
    assert f"{embed.relative_to(home).as_posix()}: missing" in said, said
    assert window.ctl.do_action("start")[0] == 200
    state = window.until(lambda st: st["run"] in ("finished", "waiting", "stopped"), timeout=60.0,
                         what="the Repair to end")  # fmt: skip
    assert codes == [0] and state["run"] == "finished", (codes, state["failure"])
    # the models step ends with its note (a damaged file was replaced): the wizard reports that row as
    # "warning" with the sentence (`Wizard._step`), and the window keeps both (`_ev_step`)
    row = next(st for st in state["steps"] if st["id"] == "s03_download_models")
    said = str(row["detail"] or "")
    assert row["status"] == "warning" and "damaged model file was downloaded again" in said, row
    assert llm.name in said, row
    assert embed.read_bytes() == content(embed.name) and dl.is_verified(embed, sha(content(embed.name)))
    assert llm.read_bytes() == content(llm.name) and dl.is_verified(llm, sha(content(llm.name)))
    assert not list((home / "models").rglob("*.bad"))
    flags = json.loads((home / "config" / "wizard_flags.json").read_text(encoding="utf-8"))
    assert "models_recheck" not in flags, flags
    assert sorted(fetched) == sorted(i.url for i in items if Path(i.dest) in (embed, llm)), fetched
    assert app_setup(tmp_path, monkeypatch, zip_a).snap()["run"] == "finished"   # the next window: Done


def test_the_windows_plan_of_model_files_is_the_models_steps_plan(tmp_path: Path) -> None:
    """Written by the builder of blocker 1, NOT RUN. The setup window (standard library) repeats the models
    step's plan (`plan_models` without optional files, plus `story_item`) to decide what a finished install
    owes: the same files with the same pins for every profile, tier, own model, voice design, engine-test and
    dev choice, and the story model once `runtime.json` uses it."""
    from installer.stage2.web import modelmarks, plan
    from ns_common.models import LlmCustom

    assert modelmarks.WHISPER_REQUIRED == dl.WHISPER_REQUIRED and modelmarks.TIER_KEYS == plan.TIER_KEYS
    raw = json.loads((repo_root() / "manifest.json").read_text(encoding="utf-8"))
    man = load_manifest(repo_root() / "manifest.json")
    paths = Paths(tmp_path / "home")
    paths.ensure()
    custom = {"repo": "someone/Tiny-GGUF", "file": "sub/tiny-custom.gguf", "size": SIZE}
    cases: list[dict[str, Any]] = [
        {"profile": "gpu", "tier": "small"}, {"profile": "gpu", "tier": "balanced"},
        {"profile": "gpu", "tier": "best"}, {"profile": "gpu", "tier": "small", "voicedesign": True},
        {"profile": "gpu", "tier": "balanced", "voice": False},
        {"profile": "gpu", "tier": "best", "custom": custom},
        {"profile": "cpu-dev", "tier": "small"}, {"profile": "cpu-dev", "tier": "small", "dev": True},
    ]

    def key(t: tuple[Path, str | None]) -> str:
        return str(t[0])

    def wizards(c: dict[str, Any]) -> list[tuple[Path, str | None]]:
        own = LlmCustom(**c["custom"]) if c["custom"] else None
        items = dl.plan_models(
            man, c["profile"], c["tier"], paths.home, custom=own,
            include_voicedesign=c["voicedesign"], dev=c["dev"], include_voice=c["voice"],
        )
        story = None if c["dev"] else vd.story_item(man, c["profile"], paths)
        return [(Path(i.dest), i.sha256) for i in items + ([story] if story else []) if not i.optional]

    for case in cases:
        c = {"custom": None, "voicedesign": False, "voice": True, "dev": False, **case}
        got = sorted(modelmarks.planned_files(paths.home, raw, **c), key=key)
        assert got == sorted(wizards(c), key=key), case
    # two models: runtime.json names the story model, and both plans add it (not on a dev install)
    rt = Runtime.default("gpu")
    rt.llama.story_model_path = str(paths.models_llm / Path(raw["profiles"]["gpu"]["story_llm"]["file"]).name)
    save_runtime(paths, rt)
    for case in ({"profile": "gpu", "tier": "small"}, {"profile": "cpu-dev", "tier": "small", "dev": True}):
        c = {"custom": None, "voicedesign": False, "voice": True, "dev": False, **case}
        got = sorted(modelmarks.planned_files(paths.home, raw, **c), key=key)
        assert got == sorted(wizards(c), key=key), case
    assert any("Heretic" in p.name for p, _pin in modelmarks.planned_files(
        paths.home, raw, profile="gpu", tier="small"))  # fmt: skip


def test_the_window_names_each_way_a_planned_model_is_not_verified(tmp_path: Path) -> None:
    """Written by the builder of blocker 1, NOT RUN. `modelmarks.repair_owed` with the plan: one line per
    file, the reason in words, nothing for a verified file or for an unplanned one that left no marker."""
    from installer.stage2.web import modelmarks

    home = tmp_path / "home"
    folder = home / "models" / "llm"
    folder.mkdir(parents=True)
    pin = sha(b"good")

    def made(name: str, data: bytes = b"good", marker: str | None = pin) -> Path:
        path = folder / name
        path.write_bytes(data)
        dl.write_marker(path, marker)
        return path

    ok = made("ok.gguf")
    gone_with_marker = made("gone1.gguf")
    gone_with_marker.unlink()
    gone = made("gone2.gguf")
    gone.unlink()
    dl._marker(gone).unlink()
    unmarked = made("unmarked.gguf")
    dl._marker(unmarked).unlink()
    torn = made("torn.gguf")
    dl._marker(torn).write_text("{", encoding="utf-8")
    repinned = made("repinned.gguf")
    nopin = made("nopin.gguf", marker=None)
    planned = [(p, pin) for p in (ok, gone_with_marker, gone, unmarked, torn, repinned)]
    planned[-1] = (repinned, sha(b"another"))
    planned.append((nopin, None))
    lines = modelmarks.repair_owed(home, planned)
    assert sorted(lines) == sorted([
        "models/llm/gone1.gguf: missing", "models/llm/gone2.gguf: missing",
        "models/llm/unmarked.gguf: no record that setup checked it",
        "models/llm/torn.gguf: its check record cannot be read",
        "models/llm/repinned.gguf: this version pins another file",
    ]), lines
    # with only verified files planned: the files that are no longer planned and have no marker left say
    # nothing; the marker whose file is gone still speaks for itself, planned or not (the Repair removes it)
    assert modelmarks.repair_owed(home, [(ok, pin), (nopin, None)]) == ["models/llm/gone1.gguf: missing"]


# ------------------------------------------------------------------------------------------------- M6
def test_a_model_that_fails_to_load_is_checked_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_supervisor.app import create_app
    from ns_supervisor.core import Supervisor
    from ns_supervisor.testing import StubTable, fast_config

    def engine(inst: Install, model: Path) -> Any:
        table = StubTable(extra={"llama": ["--exit-immediately", "-m", str(model)]})
        sup = Supervisor(
            inst.paths, Runtime.default("gpu"), config=fast_config(idle_unload_s=60), resolver=table
        )
        return TestClient(create_app(sup), base_url="http://127.0.0.1:18099")

    inst = make_install(tmp_path / "a")
    llm = inst.file("Qwen3-")
    flip(llm)
    hashed = counting(monkeypatch)
    with engine(inst, llm) as tc:
        first = tc.post("/acquire", json={"service": "llama", "owner": "studio:j1"})
        second = tc.post("/acquire", json={"service": "llama", "owner": "studio:j1"})
    for r in (first, second):
        body = r.json()
        assert r.status_code == 502 and body["code"] == "NS-E127", body
        assert (
            llm.name in body["detail"] and "2.0 KB" in body["detail"] and "language model" in body["detail"]
        )
    assert "choose Repair" in describe("NS-E127").hint
    assert hashed.count(llm.name) == 1, "a second failure of the same file in the same engine hashes nothing"
    assert record(inst)[llm.relative_to(inst.paths.home).as_posix()]["verdict"] == "bad hash"
    good = make_install(tmp_path / "b")
    with engine(good, good.file("Qwen3-")) as tc:
        r = tc.post("/acquire", json={"service": "llama", "owner": "studio:j1"})
    assert r.status_code == 502 and r.json()["code"] == "NS-E202", r.json()


# ------------------------------------------------------------------------------------------------- M7
def test_the_night_tells_a_damaged_model_once(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    from ns_studio import daily, jobs
    from ns_studio.channels import crud
    from tests import d31_driver as driver
    from tests.helpers import make_channel
    from tests.test_interrupted_nightly import _notes, _setup

    paths, db = _setup(tmp_home)
    created = (datetime.now(UTC) - timedelta(weeks=11)).isoformat(timespec="seconds")
    crud.add_channel(paths, db, make_channel(
        slug="tiny-mysteries", display_name="Tiny Mysteries", format="master_first", created_at=created,
        niche="small locked-room puzzles solved in under ten minutes", voice_notes="Dry, quick, amused.",
    ))  # fmt: skip
    driver.install(monkeypatch)
    inst = make_install(tmp_home.parent / "inst", home=tmp_home, runtime=False)
    llm = inst.file("Qwen3-")
    flip(llm)
    attempts: list[str] = []

    def load(ctx: Any) -> list[str]:  # the job loads the language model: the engine's check names it
        attempts.append(ctx.job_id)
        from ns_supervisor import modelcheck

        found = modelcheck.FileCheck(paths).after_failure("llama", ["llama-server.exe", "-m", str(llm)])
        raise found.damaged or NsError("NS-E202", "llama exited with code 1 at startup")

    toasts: list[str] = []
    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(1), run_fn=load,
                             notifier=lambda title, message: toasts.append(message) or True)  # fmt: skip
    assert len(attempts) == 1, "the night went on loading the damaged model"
    assert [f["code"] for f in report["failures"]] == ["NS-E127"], report["failures"]
    (skipped,) = report["skipped"]
    assert skipped["reason"] == "model_damaged" and skipped["jobs"] == 1, report["skipped"]
    notes = [n["message"] for n in _notes(tmp_home) if "NS-E127" in n["message"]]
    assert len(notes) == 1 and llm.name in notes[0] and "choose Repair" in notes[0], notes
    assert "Automatic runs are on" in notes[0], notes[0]
    assert toasts and "NS-E127" in toasts[0], toasts
    # the next night: the pre-check finds the recorded damage before anything loads; one note again
    attempts.clear()
    second = daily.run_daily(jobs.open_env(light=True), rng=random.Random(2), run_fn=load)
    assert attempts == [] and not second.get("skipped"), second
    (stop,) = second["failures"]                       # the Last night card: "The night: NS-E127. <the file>"
    assert stop["channel"] == "The night" and stop["code"] == "NS-E127" and llm.name in stop["error"], stop
    notes = [n["message"] for n in _notes(tmp_home) if "NS-E127" in n["message"]]
    assert len(notes) == 2 and llm.name in notes[1], notes


def test_a_file_no_night_job_loads_does_not_stop_the_night(tmp_path: Path) -> None:
    """Written by the final reviewer, changed by the builder of blocker 1; NOT RUN. The pre-check stops a
    night only for the files its jobs load (the language and story model, the embed model of the stages that
    embed text, the talkers and tokenizer of a synthesis, whisper); the design talker and the face finder are
    said beside it. A failed voice start does not look at the design talker."""
    from ns_studio import notifications
    from ns_supervisor import modelcheck
    from ns_tts import engine as tts_engine

    assert modelcheck.DESIGN_KIND == tts_engine.TALKER_KIND["design"]  # the word in the talker's file name
    inst = make_install(tmp_path)
    design = next(Path(i.dest) for i in inst.items if modelcheck.design_only(i))
    embed = next(Path(i.dest) for i in inst.items if Path(i.dest).parent.name == "embed")
    finder = inst.file("yunet")
    voice = [Path(i.dest) for i in modelcheck.files_for("tts", [], inst.items)]
    assert design not in voice and inst.file("talker-1.7b-base") in voice and len(voice) == 3, voice
    tonight = {Path(i.dest) for i in modelcheck.night_files(inst.paths, inst.items)}
    assert embed in tonight and design not in tonight and finder not in tonight, tonight
    design.unlink()
    finder.unlink()
    aside: list[str] = []
    assert modelcheck.precheck(inst.paths, inst.app, others=aside) == [], "the night was stopped"
    assert len(aside) == 2 and any(design.name in s for s in aside), aside
    assert any(str(finder) in s for s in aside), aside
    note = notifications.model_aside_note({"said": " ".join(aside)}) or ""
    assert "stopped no video" in note and "choose Repair" in note and design.name in note, note
    assert notifications.model_aside_note(None) is None
    # a failed voice start with the design talker gone: the files of that start are there, nothing is named
    check = modelcheck.FileCheck(inst.paths, app=inst.app)
    found = check.after_failure("tts", ["python.exe", "-m", "ns_tts.server"])
    assert found.damaged is None and design.name not in found.note, found
    # the embed model IS loaded by a night's jobs (`ideas`, `chapters_critic`, `continuity`, `critic_revise`,
    # the text policy gate): recorded damaged, it stops the night
    flip(embed)
    assert check.after_failure("embed", ["llama-server.exe", "-m", str(embed)]).damaged is not None
    stops = modelcheck.precheck(inst.paths, inst.app)
    assert len(stops) == 1 and stops[0].startswith("NS-E127") and embed.name in stops[0], stops
    # and so does a talker
    talker = inst.file("talker-1.7b-base")
    talker.unlink()
    stops = modelcheck.precheck(inst.paths, inst.app)
    assert len(stops) == 2 and any(talker.name in s for s in stops), stops


def test_a_file_no_night_job_loads_is_told_in_the_morning_note_and_the_night_runs(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Written by the builder of blocker 1, NOT RUN (the reviewer's NOT DONE). Through `run_daily`: the voice
    design talker is gone; the night still makes its video, fails nothing, and leaves one morning note
    (`notifications.model_aside_note`) naming the file, that it stopped no video, and Repair."""
    from datetime import UTC, datetime, timedelta

    from ns_studio import daily, jobs, notifications
    from ns_studio.channels import crud
    from ns_supervisor import modelcheck
    from tests import d31_driver as driver
    from tests.helpers import make_channel
    from tests.test_interrupted_nightly import _notes, _setup

    paths, db = _setup(tmp_home)
    created = (datetime.now(UTC) - timedelta(weeks=11)).isoformat(timespec="seconds")
    crud.add_channel(paths, db, make_channel(
        slug="tiny-mysteries", display_name="Tiny Mysteries", format="master_first", created_at=created,
        niche="small locked-room puzzles solved in under ten minutes", voice_notes="Dry, quick, amused.",
    ))  # fmt: skip
    driver.install(monkeypatch)
    inst = make_install(tmp_home.parent / "inst", home=tmp_home, runtime=False)
    design = next(Path(i.dest) for i in inst.items if modelcheck.design_only(i))
    design.unlink()
    ran: list[str] = []
    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(1),
                             run_fn=lambda ctx: ran.append(ctx.job_id) or [])  # fmt: skip
    assert ran, "the night made nothing: a file no night job loads stopped it"
    assert not [f for f in report["failures"] if f.get("code") == "NS-E127"], report["failures"]
    assert not [s for s in report.get("skipped") or [] if s.get("reason") == daily.MODEL_SKIPPED], report
    assert "model_aside" not in report and "model_damaged" not in report     # the note's words only
    notes = [n["message"] for n in _notes(tmp_home) if "stopped no video" in n["message"]]
    assert len(notes) == 1 and design.name in notes[0] and "choose Repair" in notes[0], notes
    assert notifications.model_aside_note({"said": design.name}) is not None


# --------------------------------------------------------------------------------- the seams themselves
def test_the_check_reads_a_models_size_only_through_the_size_seam() -> None:
    """No test of this file can bring a model of a real size in as bytes: the checker asks `_size`."""
    src = inspect.getsource(vd.check_item)
    assert "_size(dest)" in src and "dest.stat()" not in src
