"""C2/C1: `ns models get voicedesign|story_llm` and the setup option for VoiceDesign (gaps 10, 14)."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from installer.stage2.downloader import PlanResult, write_marker
from installer.stage2.testing import example_answers, make_test_context
from ns_common.config import load_manifest, load_runtime, save_runtime
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths, repo_root
from ns_supervisor import cli, models_get

runner = CliRunner()
VOICEDESIGN = "qwen-talker-1.7b-voicedesign-Q8_0.gguf"


def manifest():
    return load_manifest(repo_root() / "manifest.json")


def test_voicedesign_is_the_pinned_talker_in_models_tts(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    (item,) = models_get.plan_items("voicedesign", manifest(), "gpu", paths)
    pin = manifest().profiles["gpu"].tts_optional[0]
    assert item.dest == paths.models_tts / VOICEDESIGN
    assert item.sha256 == pin.sha256 and item.size == pin.bytes
    assert item.url.endswith(f"/resolve/{pin.revision}/{VOICEDESIGN}") and pin.revision != "main"


def test_the_story_model_is_the_pinned_gguf_in_models_llm(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    (item,) = models_get.plan_items("story_llm", manifest(), "gpu", paths)
    pin = manifest().profiles["gpu"].story_llm
    assert pin is not None and item.sha256 == pin.sha256 and item.dest.parent == paths.models_llm


def test_nothing_to_get_on_a_profile_without_it_is_a_coded_error(tmp_path: Path) -> None:
    with pytest.raises(NsError) as ei:
        models_get.plan_items("story_llm", manifest(), "cpu-dev", Paths(tmp_path))
    assert ei.value.code == "NS-E122"  # not NS-E109: that one is a failed checksum
    with pytest.raises(ValueError):
        models_get.plan_items("nope", manifest(), "gpu", Paths(tmp_path))


def test_get_uses_the_installer_downloader_and_points_runtime_at_the_story_model(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    paths.ensure()
    save_runtime(paths, Runtime.default("gpu"))
    seen: list[Any] = []
    lines: list[str] = []

    def fake_run_plan(items: list[Any], *, reporter: Any = None, **kw: Any) -> PlanResult:
        seen.extend(items)
        reporter.start(items[0].name, 100)
        reporter.update(items[0].name, 55, 100)
        reporter.finish(items[0].name, "downloaded")
        return PlanResult(downloaded=[items[0].name])

    out = models_get.get_model("story_llm", paths, repo_root(), lines.append, run_plan=fake_run_plan)
    assert out["ok"] is True and out["downloaded"] == 1 and len(seen) == 1
    assert "50%" in " ".join(lines) and lines[-2].endswith("downloaded")
    assert load_runtime(paths).llama.story_model_path == str(seen[0].dest)


def test_an_already_verified_file_is_not_downloaded_again(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    (item,) = models_get.plan_items("voicedesign", manifest(), "gpu", paths)
    item.dest.parent.mkdir(parents=True)
    item.dest.write_bytes(b"x")
    write_marker(item.dest, item.sha256)
    lines: list[str] = []
    out = models_get.get_model(
        "voicedesign", paths, repo_root(), lines.append,
        run_plan=lambda *a, **k: pytest.fail("must not download a verified file"),
    )
    assert out["downloaded"] == 0 and "Already downloaded" in lines[0]


def test_the_command_prints_a_json_result_and_rejects_unknown_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        models_get, "get_model", lambda kind, paths, app, echo, **kw: {"ok": True, "model": kind, "files": []}
    )
    env = {"NIGHTSHIFT_HOME": str(tmp_path)}
    r = runner.invoke(cli.app, ["models", "get", "voicedesign"], env=env)
    assert r.exit_code == 0 and json.loads(r.output.strip().splitlines()[-1])["model"] == "voicedesign"
    assert runner.invoke(cli.app, ["models", "get", "bigger"], env=env).exit_code == 2


def test_setup_downloads_voicedesign_only_when_asked(tmp_path: Path) -> None:
    from installer.stage2.steps.s03_download_models import S03DownloadModels

    plain = make_test_context(tmp_path / "a", raw_answers=example_answers(), resolve=True)
    asked = make_test_context(
        tmp_path / "b", raw_answers=example_answers(include_voicedesign=True), resolve=True
    )
    step = S03DownloadModels()
    assert VOICEDESIGN not in [i.name for i in step._items(plain)]
    assert VOICEDESIGN in [i.name for i in step._items(asked)]


def test_the_installer_is_part_of_the_installed_packages() -> None:
    """ns.exe in the supervisor venv must be able to import the downloader."""
    text = (repo_root() / "pyproject.toml").read_text(encoding="utf-8")
    assert '"installer*"' in text


# ------------------------------------------------------------------------- the PyTorch (reference) engine
def fake_tree(calls: list[tuple[str, Path]]):  # type: ignore[no-untyped-def]
    """A Hugging Face listing with a config and one weights file per repo."""
    from installer.stage2.downloader import DownloadItem, hf_url

    def tree(repo: str, revision: str, dest_dir: Path) -> list[DownloadItem]:
        calls.append((repo, dest_dir))
        return [
            DownloadItem(
                name=f"{repo}/{f}", url=hf_url(repo, revision, f), dest=dest_dir / f, sha256="a" * 64
            )
            for f in ("config.json", "model.safetensors")
        ]

    return tree


def test_the_reference_engine_gets_the_hf_weights_in_models_tts_hf_not_the_gguf(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    calls: list[tuple[str, Path]] = []
    items = models_get.plan_items(
        "voicedesign", manifest(), "gpu", paths, engine="reference", tree=fake_tree(calls)
    )
    design_dir = paths.models_tts_hf / "Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    assert calls[0] == ("Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", design_dir)
    assert all(paths.models_tts_hf in i.dest.parents for i in items)
    assert not any(i.dest.suffix == ".gguf" for i in items)
    assert (design_dir / "model.safetensors") in [i.dest for i in items]
    # the engine's design mode also loads Base: it is planned only when it is missing
    assert [c[0] for c in calls] == [models_get.REFERENCE_VOICEDESIGN, models_get.REFERENCE_BASE]
    calls.clear()
    (paths.models_tts_hf / "Qwen3-TTS-12Hz-1.7B-Base").mkdir(parents=True)
    models_get.plan_items("voicedesign", manifest(), "gpu", paths, engine="reference", tree=fake_tree(calls))
    assert [c[0] for c in calls] == [models_get.REFERENCE_VOICEDESIGN]


def test_the_qwentts_engine_still_gets_only_the_gguf_and_never_lists_hugging_face(tmp_path: Path) -> None:
    paths = Paths(tmp_path)

    def no_tree(*a: Any) -> Any:
        pytest.fail("the GGUF plan must not call the Hugging Face tree API")

    (item,) = models_get.plan_items("voicedesign", manifest(), "gpu", paths, engine="qwentts", tree=no_tree)
    assert item.dest == paths.models_tts / VOICEDESIGN


def test_all_plans_both_sets(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    items = models_get.plan_items(
        "voicedesign", manifest(), "gpu", paths, engine="qwentts", everything=True, tree=fake_tree([])
    )
    dests = [i.dest for i in items]
    assert paths.models_tts / VOICEDESIGN in dests
    assert paths.models_tts_hf / "Qwen3-TTS-12Hz-1.7B-VoiceDesign" / "model.safetensors" in dests


def test_the_story_model_ignores_the_engine(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    (item,) = models_get.plan_items("story_llm", manifest(), "gpu", paths, engine="reference")
    assert item.dest.parent == paths.models_llm


def test_get_reads_the_engine_from_runtime_json(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    paths.ensure()
    rt = Runtime.default("gpu")
    rt.tts.engine = "reference"
    save_runtime(paths, rt)
    seen: list[Any] = []

    def fake_run_plan(items: list[Any], *, reporter: Any = None, **kw: Any) -> PlanResult:
        seen.extend(items)
        return PlanResult(downloaded=[i.name for i in items])

    out = models_get.get_model(
        "voicedesign", paths, repo_root(), lambda line: None, run_plan=fake_run_plan, tree=fake_tree([])
    )
    assert out["engine"] == "reference" and out["downloaded"] == len(seen) > 0
    assert all(paths.models_tts_hf in Path(f).parents for f in out["files"])
    # a second run finds every file verified or planned the same way, and --all adds the talker
    out_all = models_get.get_model(
        "voicedesign", paths, repo_root(), lambda line: None, run_plan=fake_run_plan, tree=fake_tree([]),
        everything=True,
    )
    assert any(f.endswith(VOICEDESIGN) for f in out_all["files"])


def test_the_all_flag_reaches_get_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_get(kind: str, paths: Paths, app: Path, echo: Any, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"ok": True, "model": kind, "files": []}

    monkeypatch.setattr(models_get, "get_model", fake_get)
    env = {"NIGHTSHIFT_HOME": str(tmp_path)}
    assert runner.invoke(cli.app, ["models", "get", "voicedesign", "--all"], env=env).exit_code == 0
    assert seen == {"everything": True}
    seen.clear()
    assert runner.invoke(cli.app, ["models", "get", "voicedesign"], env=env).exit_code == 0
    assert seen == {"everything": False}
