"""C2 `ns models get voicedesign|story_llm`: the optional model downloads, after setup.

Both files are pinned in `manifest.json` (repo, file, sha256, bytes). The installer's downloader does the work
(Range resume, SHA256 check, atomic rename), so a download started here resumes if it is stopped and a wrong
file is never kept. The dashboard (Settings > Models) calls this command.

* `voicedesign`: the VoiceDesign talker(s) in `manifest.profiles.<p>.tts_optional`, into `models\tts`.
* `story_llm`: the story model (`story_llm`), into `models\llm`; `runtime.json` gets `llama.story_model_path`
  so two-model mode can use it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ns_common.config import load_manifest, load_runtime, save_runtime
from ns_common.errors import NsError
from ns_common.models import Manifest
from ns_common.paths import Paths

if TYPE_CHECKING:
    from installer.stage2.downloader import DownloadItem, PlanResult

KINDS = ("voicedesign", "story_llm")


def plan_items(kind: str, manifest: Manifest, profile: str, paths: Paths) -> list[DownloadItem]:
    """Download items for `kind`; raises `NsError` NS-E109 when the profile has none."""
    from installer.stage2.downloader import _hf_item

    pm = manifest.profiles[profile]
    if kind == "voicedesign":
        items = [_hf_item(f, paths.models_tts) for f in pm.tts_optional]
    elif kind == "story_llm":
        items = [_hf_item(pm.story_llm, paths.models_llm)] if pm.story_llm is not None else []
    else:
        raise ValueError(f"unknown model {kind!r}; choose one of {', '.join(KINDS)}")
    if not items:
        raise NsError("NS-E109", f"the {kind} model is not available for the {profile} profile")
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
) -> dict[str, Any]:
    """Download `kind` (resumable, verified) and wire it into `runtime.json`. Returns a result dict."""
    from installer.stage2.downloader import is_done, run_plan as real_run_plan

    man = manifest or load_manifest(app / "manifest.json")
    try:
        profile = load_runtime(paths).profile
    except NsError:
        profile = "gpu"
    items = plan_items(kind, man, profile, paths)
    todo = [i for i in items if not is_done(i)]
    if todo:
        (run_plan or real_run_plan)(todo, reporter=EchoReporter(echo))
    else:
        echo("Already downloaded and verified.")
    if kind == "story_llm":
        rt = load_runtime(paths)
        rt.llama.story_model_path = str(items[0].dest)
        save_runtime(paths, rt)
        echo("Two-model mode can now use the story model (runtime.json updated).")
    return {"ok": True, "model": kind, "files": [str(i.dest) for i in items], "downloaded": len(todo)}
