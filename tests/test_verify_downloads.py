"""C1/C2: `ns verify --downloads` re-hashes every download against manifest.json."""

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from installer.stage2 import downloader as dl
from installer.stage2 import verify_downloads as vd
from installer.stage2.testing import example_answers
from ns_common.paths import Paths
from ns_supervisor import cli

DATA = bytes(range(256)) * 64
SHA = hashlib.sha256(DATA).hexdigest()
runner = CliRunner()


def item(tmp_path: Path, name: str = "a.bin", **kw) -> dl.DownloadItem:
    return dl.DownloadItem(name=name, url=f"https://x.test/{name}", dest=tmp_path / "m" / name, **kw)


def put(it: dl.DownloadItem, data: bytes = DATA) -> None:
    it.dest.parent.mkdir(parents=True, exist_ok=True)
    it.dest.write_bytes(data)


def test_a_file_with_the_pinned_hash_is_ok(tmp_path: Path):
    it = item(tmp_path, sha256=SHA, size=len(DATA))
    put(it)
    row = vd.check_item(it)
    assert row.status == vd.OK and not row.failing and row.fix == ""


def test_a_missing_file_says_run_install_again(tmp_path: Path):
    row = vd.check_item(item(tmp_path, sha256=SHA))
    assert row.status == vd.MISSING and row.failing and "INSTALL.cmd" in row.fix


def test_a_stopped_download_says_how_far_it_got_and_that_it_continues(tmp_path: Path):
    it = item(tmp_path, sha256=SHA, size=len(DATA))
    it.dest.parent.mkdir(parents=True)
    it.dest.with_name("a.bin.part").write_bytes(DATA[:1024])
    row = vd.check_item(it)
    assert row.status == vd.MISSING and "stopped at 1.0 KB of 16.0 KB" in row.detail
    assert "continues from where it stopped" in row.fix


def test_a_changed_file_is_a_bad_hash_with_the_delete_and_rerun_fix(tmp_path: Path):
    it = item(tmp_path, sha256=SHA)
    put(it, DATA[:-1] + b"X")                      # same size, one byte different
    row = vd.check_item(it)
    assert row.status == vd.BAD and "expected" in row.detail
    assert "Delete a.bin and a.bin.ok" in row.fix and "INSTALL.cmd" in row.fix


def test_a_wrong_size_is_bad_without_hashing_the_file(tmp_path: Path):
    it = item(tmp_path, sha256=SHA, size=len(DATA))
    put(it, DATA[:100])

    def never(path: Path) -> str:
        raise AssertionError("a file of the wrong size must not be hashed")

    row = vd.check_item(it, hasher=never)
    assert row.status == vd.BAD and "100 bytes" in row.detail


def test_a_file_with_no_pinned_hash_is_ok_without_a_pin(tmp_path: Path):
    it = item(tmp_path, sha256=None, size=len(DATA))      # the owner's own story model
    put(it)
    assert vd.check_item(it).status == vd.NO_PIN
    tbd = item(tmp_path, "b.bin", sha256="TBD-release")
    put(tbd)
    assert vd.check_item(tbd).status == vd.NO_PIN         # a placeholder is not a pin


def test_an_optional_file_that_is_not_there_is_not_a_failure(tmp_path: Path):
    row = vd.check_item(item(tmp_path, "vocabulary.txt", optional=True))
    assert row.status == vd.OPTIONAL and not row.failing


def test_a_zip_that_was_unpacked_is_checked_by_its_files(tmp_path: Path):
    target = tmp_path / "bin" / "llama"
    it = dl.DownloadItem(name="llama_cuda", url="https://x.test/l.zip", dest=tmp_path / "l.zip", kind="zip",
                         extract_to=target, expect=("llama-server.exe",))
    assert vd.check_item(it).status == vd.MISSING
    target.mkdir(parents=True)
    (target / "llama-server.exe").write_bytes(b"x")
    (target / ".ns-llama_cuda.done").write_text("ok", encoding="utf-8")
    assert vd.check_item(it).status == vd.UNPACKED


def test_the_table_lists_every_file_and_the_fix_for_each_problem(tmp_path: Path):
    good, gone, bad = item(tmp_path, "good.bin", sha256=SHA), item(tmp_path, "gone.bin", sha256=SHA), \
        item(tmp_path, "bad.bin", sha256=SHA)
    put(good)
    put(bad, b"nope")
    rows = vd.verify_items([good, gone, bad])
    text = "\n".join(vd.format_table(rows))
    assert [r.status for r in rows] == [vd.OK, vd.MISSING, vd.BAD]
    for word in ("good.bin", "gone.bin", "bad.bin", "ok", "missing", "bad hash", "What to do:"):
        assert word in text
    assert "1 of 3 files fine, 2 to fix." in text


def test_run_returns_zero_when_everything_is_fine_and_one_when_not(tmp_path: Path, monkeypatch):
    a, b = item(tmp_path, "a.bin", sha256=SHA), item(tmp_path, "b.bin", sha256=SHA)
    put(a)
    monkeypatch.setattr(vd, "plan_for_install", lambda paths, app=None: [a])
    lines: list[str] = []
    assert vd.run(Paths(tmp_path), None, lines.append) == 0
    monkeypatch.setattr(vd, "plan_for_install", lambda paths, app=None: [a, b])
    lines.clear()
    assert vd.run(Paths(tmp_path), None, lines.append) == 1
    assert any("b.bin" in line and "missing" in line for line in lines)


def test_an_install_that_never_chose_its_models_exits_two_with_a_pointer(tmp_path: Path):
    lines: list[str] = []
    assert vd.run(Paths(tmp_path / "home"), None, lines.append) == 2
    assert "INSTALL.cmd" in "\n".join(lines)


def write_install(home: Path, **answers) -> Paths:
    paths = Paths(home)
    (home / "config").mkdir(parents=True)
    (home / "config" / "answers.json").write_text(json.dumps(example_answers(**answers)), encoding="utf-8")
    return paths


def test_the_plan_follows_the_chosen_tier_and_the_recorded_llama_build(tmp_path: Path):
    paths = write_install(tmp_path / "home", llm_tier="balanced", install_code=False)
    (paths.home / "config" / "wizard_flags.json").write_text(json.dumps({"llama_pair": "cuda13"}), "utf-8")
    names = {i.name for i in vd.plan_for_install(paths)}
    assert {"llama_cuda13", "cudart13", "yt_dlp"} <= names
    assert "opencode" not in names and "llama_cuda" not in names
    small = write_install(tmp_path / "other", llm_tier="small")
    balanced_files = {i.dest.name for i in vd.plan_for_install(paths)}
    assert {i.dest.name for i in vd.plan_for_install(small)} != balanced_files    # another tier, another LLM


def test_ns_verify_downloads_prints_the_table_and_exits_one_when_files_are_missing(tmp_path: Path):
    write_install(tmp_path / "home")
    r = runner.invoke(cli.app, ["verify", "--downloads"], env={"NIGHTSHIFT_HOME": str(tmp_path / "home")})
    assert r.exit_code == 1
    assert "missing" in r.output and "INSTALL.cmd" in r.output and "yt_dlp" in r.output


def test_ns_verify_downloads_is_listed_in_the_help(tmp_path: Path):
    r = runner.invoke(cli.app, ["verify", "--help"], env={"NIGHTSHIFT_HOME": str(tmp_path)})
    assert "--downloads" in r.output


@pytest.mark.parametrize("status", [vd.MISSING, vd.BAD])
def test_only_missing_and_bad_hash_count_as_failures(status: str):
    assert vd.Row("x", "p", status).failing
    for fine in (vd.OK, vd.NO_PIN, vd.UNPACKED, vd.OPTIONAL):
        assert not vd.Row("x", "p", fine).failing
