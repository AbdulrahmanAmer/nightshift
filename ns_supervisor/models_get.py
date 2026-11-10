r"""C2 `ns models get voicedesign|story_llm`: the optional model downloads, after setup.

Both files are pinned in `manifest.json` (repo, file, sha256, bytes). The installer's downloader does the work
(Range resume, SHA256 check, atomic rename), so a download started here resumes if it is stopped and a wrong
file is never kept. The dashboard (Settings > Models) calls this command.

* `voicedesign`: depends on the voice engine in `runtime.json` (`tts.engine`). `qwentts` (the default): the
  VoiceDesign talker(s) in `manifest.profiles.<p>.tts_optional`, into `models\tts`. `reference` (the PyTorch
  fallback): the Hugging Face weights `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` into `models\tts\hf`, where
  that engine looks for them (plus the Base weights its design mode also loads, when they are missing).
  `--all` plans both sets, for a PC that may switch engines.
* `story_llm`: the story model (`story_llm`), into `models\llm`; `runtime.json` gets `llama.story_model_path`
  so two-model mode can use it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ns_common.config import load_manifest, load_runtime, save_runtime
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import Manifest
from ns_common.paths import Paths

if TYPE_CHECKING:
    from installer.stage2.downloader import DownloadItem, PlanResult

KINDS = ("voicedesign", "story_llm")
#: The reference engine's design mode loads the VoiceDesign weights and the Base weights (`ns_tts.engine`).
REFERENCE_VOICEDESIGN = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
REFERENCE_BASE = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"

#: Lists a Hugging Face repo as download items: (repo, revision, destination folder).
HfTree = Callable[[str, str, Path], "list[DownloadItem]"]


def hf_tree(repo: str, revision: str, dest_dir: Path) -> list[DownloadItem]:
    """The real listing, through the Hugging Face tree API (SHA256 from the LFS oid)."""
    from installer.stage2 import downloader

    client = downloader._new_client()
    try:
        return downloader.plan_from_hf_tree(repo, revision, dest_dir, client)
    finally:
        client.close()


def reference_items(
    paths: Paths, tree: HfTree, revisions: dict[str, str] | None = None
) -> list[DownloadItem]:
    """The PyTorch engine's VoiceDesign weights under `models\\tts\\hf` (and Base when it is not there).

    `revisions` maps a repo to its pinned commit (`Manifest.tts_fallback_revisions`); a repo it lacks is read
    from ``main``.
    """
    revs = revisions or {}
    items = tree(
        REFERENCE_VOICEDESIGN, revs.get(REFERENCE_VOICEDESIGN, "main"),
        paths.models_tts_hf / REFERENCE_VOICEDESIGN.split("/")[1],
    )
    base_dir = paths.models_tts_hf / REFERENCE_BASE.split("/")[1]
    if not base_dir.is_dir():
        items += tree(REFERENCE_BASE, revs.get(REFERENCE_BASE, "main"), base_dir)
    return items


def _not_offered(kind: str, profile: str) -> str:
    """The detail of NS-E122: the model and this install's profile."""
    return f"The {kind} model is not available for the {profile} profile."


def unavailable(
    kind: str, manifest: Manifest, profile: str, engine: str, *, everything: bool = False
) -> str | None:
    """Why this install cannot get `kind` (what the detail of NS-E122 says), or None when it can.

    The one rule, read before any network call by `plan_items` and by the dashboard's Get button.
    `voicedesign` needs a talker in `manifest.profiles.<profile>.tts_optional` unless the voice `engine` is
    `reference` (or `everything` plans it too): the PyTorch weights come from Hugging Face on any profile.
    `story_llm` needs the profile's `story_llm`. An unknown `kind` or `profile` is not decided here
    (`plan_items` raises for them).
    """
    pm = manifest.profiles.get(profile)
    if pm is None or kind not in KINDS:
        return None
    if kind == "voicedesign":
        offered = bool(pm.tts_optional) or engine == "reference" or everything
    else:
        offered = pm.story_llm is not None
    return None if offered else _not_offered(kind, profile)


def plan_items(
    kind: str,
    manifest: Manifest,
    profile: str,
    paths: Paths,
    *,
    engine: str = "qwentts",
    everything: bool = False,
    tree: HfTree | None = None,
) -> list[DownloadItem]:
    """Download items for `kind`; raises `NsError` NS-E122 (before any network call) if the profile has none.

    `voicedesign` follows the voice `engine`: the GGUF talker for `qwentts` (and `faster`), the Hugging Face
    weights for `reference`; `everything` plans both.
    """
    from installer.stage2.downloader import _hf_item

    if kind not in KINDS:
        raise ValueError(f"unknown model {kind!r}; choose one of {', '.join(KINDS)}")
    reason = unavailable(kind, manifest, profile, engine, everything=everything)
    if reason is not None:
        raise NsError("NS-E122", reason)
    pm = manifest.profiles[profile]
    if kind == "voicedesign":
        items = []
        if engine != "reference" or everything:
            items += [_hf_item(f, paths.models_tts) for f in pm.tts_optional]
        if engine == "reference" or everything:
            items += reference_items(paths, tree or hf_tree, manifest.tts_fallback_revisions)
    else:
        items = [_hf_item(pm.story_llm, paths.models_llm)] if pm.story_llm is not None else []
    if not items:  # a listing that came back empty: still nothing to download, and nothing was written
        raise NsError("NS-E122", _not_offered(kind, profile))
    return items


class EchoReporter:
    """Prints a line when a file starts, every 10 % of it, and when it ends."""

    def __init__(self, echo: Callable[[str], None]) -> None:
        self._echo = echo
        self._last: dict[str, int] = {}

    def start(self, name: str, total: int | None) -> None:
        """A file started."""
        self._echo(f"{name}: starting")

    def update(self, name: str, done: int, total: int | None) -> None:
        """Bytes done so far."""
        if not total:
            return
        tenth = int(done * 10 / total)
        if tenth > self._last.get(name, -1):
            self._last[name] = tenth
            self._echo(f"{name}: {tenth * 10}%")

    def site_wait(self, name: str, host: str, status: int, wait_s: float, left_s: float) -> None:
        """The download site answered 5xx or 429: say so, so a wait of minutes is not silence."""
        from installer.stage2.downloader import site_wait_text

        self._echo(f"{name}: {site_wait_text(host, status, wait_s, left_s)}")

    def finish(self, name: str, outcome: str) -> None:
        """A file ended."""
        self._echo(f"{name}: {outcome}")


def get_model(
    kind: str,
    paths: Paths,
    app: Path,
    echo: Callable[[str], None],
    *,
    run_plan: Callable[..., PlanResult] | None = None,
    manifest: Manifest | None = None,
    everything: bool = False,
    tree: HfTree | None = None,
) -> dict[str, Any]:
    """Download `kind` (resumable, verified) and wire it into `runtime.json`. Returns a result dict."""
    from installer.stage2 import downloader

    man = manifest or load_manifest(app / "manifest.json")
    try:
        rt = load_runtime(paths)
        profile, engine = rt.profile, rt.tts.engine
    except NsError:
        profile, engine = "gpu", "qwentts"
    items = plan_items(kind, man, profile, paths, engine=engine, everything=everything, tree=tree)
    todo = [i for i in items if not downloader.is_done(i)]
    if todo:
        log = NsLogger("models", paths.logs, echo=False)
        (run_plan or downloader.run_plan)(todo, reporter=EchoReporter(echo), log=log)
    else:
        echo("Already downloaded and verified.")
    if kind == "story_llm":
        rt = load_runtime(paths)
        rt.llama.story_model_path = str(items[0].dest)
        save_runtime(paths, rt)
        echo("Two-model mode can now use the story model (runtime.json updated).")
    return {
        "ok": True, "model": kind, "engine": engine, "files": [str(i.dest) for i in items],
        "downloaded": len(todo),
    }
