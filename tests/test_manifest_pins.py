"""0.1.7: every Hugging Face file and the YuNet model come from a pinned commit, not a moving branch."""

import re
from pathlib import Path

from installer.stage2 import downloader as dl
from installer.stage2.testing import example_answers, make_test_context
from ns_common.config import load_manifest
from ns_common.models import HfFile
from ns_common.paths import repo_root
from ns_supervisor import models_get

SHA = re.compile(r"^[0-9a-f]{40}$")


def manifest():
    return load_manifest(repo_root() / "manifest.json")


def _hf_files(m):
    for name, pm in m.profiles.items():
        for key in ("llm", "llm_balanced", "llm_best", "story_llm", "embed"):
            f = getattr(pm, key)
            if f is not None:
                yield f"{name}.{key}", f
        for key in ("tts", "tts_optional"):
            for i, f in enumerate(getattr(pm, key)):
                yield f"{name}.{key}[{i}]", f


def test_every_model_file_names_a_commit_not_main() -> None:
    m = manifest()
    seen = list(_hf_files(m))
    assert len(seen) >= 12
    for where, f in seen:
        assert SHA.match(f.revision), f"{where}: revision {f.revision!r} is not a 40-hex commit"
    for name, pm in m.profiles.items():
        assert SHA.match(pm.whisper_repo.revision), f"{name}.whisper_repo: {pm.whisper_repo.revision!r}"


def test_the_pytorch_fallback_repos_are_pinned() -> None:
    m = manifest()
    for repo in (*m.tts_fallback_repos, models_get.REFERENCE_VOICEDESIGN):
        assert SHA.match(m.tts_fallback_revisions.get(repo, "")), repo


def test_no_url_in_the_manifest_reads_a_moving_branch() -> None:
    text = (repo_root() / "manifest.json").read_text(encoding="utf-8")
    assert "/raw/main/" not in text and "/resolve/main/" not in text
    assert '"revision": "main"' not in text
    yunet = manifest().bins["yunet"]
    assert yunet is not None
    assert re.search(r"/opencv_zoo/raw/[0-9a-f]{40}/models/face_detection_yunet/", yunet.url)


def test_the_model_plan_uses_each_files_revision(tmp_path: Path) -> None:
    m = manifest()
    items = dl.plan_models(m, "gpu", "balanced", tmp_path, include_voicedesign=True)
    pm = m.profiles["gpu"]
    by_name = {i.name: i for i in items}
    for f in (pm.llm_balanced, pm.embed, *pm.tts, *pm.tts_optional):
        assert f is not None
        assert by_name[Path(f.file).name].url == f"https://huggingface.co/{f.repo}/resolve/{f.revision}/{f.file}"
    whisper = by_name["whisper/model.bin"]
    assert f"/resolve/{pm.whisper_repo.revision}/model.bin" in whisper.url
    assert by_name["yunet"].url == m.bins["yunet"].url


def test_a_file_with_no_revision_falls_back_to_main(tmp_path: Path) -> None:
    f = HfFile(repo="o/r", file="sub/m.gguf", sha256="ab" * 32, bytes=5)
    assert f.revision == "main"
    assert dl._hf_item(f, tmp_path).url == "https://huggingface.co/o/r/resolve/main/sub/m.gguf"
    pinned = f.model_copy(update={"revision": "c" * 40})
    assert dl._hf_item(pinned, tmp_path).url == f"https://huggingface.co/o/r/resolve/{'c' * 40}/sub/m.gguf"


def test_the_custom_model_still_comes_from_main(tmp_path: Path) -> None:
    from ns_common.models import LlmCustom

    item = dl.custom_item(LlmCustom(repo="a/b", file="m.Q4.gguf", size=1), tmp_path)
    assert "/resolve/main/" in item.url and item.sha256 is None


def test_the_reference_engine_plan_uses_the_pinned_commits(tmp_path: Path) -> None:
    m = manifest()
    from ns_common.paths import Paths

    calls: list[tuple[str, str]] = []

    def tree(repo: str, revision: str, dest: Path) -> list[dl.DownloadItem]:
        calls.append((repo, revision))
        return []

    models_get.reference_items(Paths(tmp_path), tree, m.tts_fallback_revisions)
    assert calls == [
        (models_get.REFERENCE_VOICEDESIGN, m.tts_fallback_revisions[models_get.REFERENCE_VOICEDESIGN]),
        (models_get.REFERENCE_BASE, m.tts_fallback_revisions[models_get.REFERENCE_BASE]),
    ]
    calls.clear()
    models_get.reference_items(Paths(tmp_path / "other"), tree)
    assert [c[1] for c in calls] == ["main", "main"]


def test_the_simulated_fallback_plan_uses_the_pinned_commits(tmp_path: Path) -> None:
    from installer.stage2.fallback import fallback_items

    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    items = fallback_items(ctx)
    revs = ctx.manifest.tts_fallback_revisions
    assert items
    for it in items:
        repo = it.name.rsplit("/", 1)[0]
        assert f"/{repo}/resolve/{revs[repo]}/" in it.url
