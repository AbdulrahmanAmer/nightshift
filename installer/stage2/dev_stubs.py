"""The download function of a dev install (docs/DEV_MODE.md): stub items are written, the rest is fetched.

``ctx.download`` puts this in front of ``downloader.download`` (or the simulated fetch) when the plan holds an
item with ``stub=True``. The progress reporter sees an ordinary file that starts and finishes, so the setup
window shows the model rows ticking off as they do in a real install.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from installer.stage2 import devmode
from installer.stage2.downloader import DownloadItem, ProgressFn, download, write_marker


def stub_fetch(
    items: Sequence[DownloadItem], inner: Callable[..., Any] | None = None
) -> Callable[..., Path]:
    """A ``download`` replacement: stub items get their placeholder file, others go to ``inner``.

    ``inner`` is the fetch the plan would have used without stubs (the real ``download`` when None). A real
    model that an earlier real install left in place is never overwritten by a stub: it is kept and marked.
    """
    stubs = {i.url for i in items if i.stub}
    real = inner or download

    def fetch(
        url: str, dest: Path, sha256: str | None, *, progress: ProgressFn | None = None, **kw: Any
    ) -> Path:
        if url not in stubs:
            return Path(real(url, dest, sha256, progress=progress, **kw))
        target = Path(dest)
        if target.is_file() and not devmode.is_stub(target) and target.stat().st_size > devmode.STUB_BYTES:
            write_marker(target, None)          # a real model is already here: leave it
            size = target.stat().st_size
        else:
            size = devmode.write_stub(target)
        if progress:
            progress(size, size)
        return target

    return fetch
