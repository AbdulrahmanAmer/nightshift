"""Check a story model the owner names: a Hugging Face repository and a GGUF file (installer-ui.md 2.3).

The setup server runs on a bare Python with only the standard library, so this module uses ``urllib`` and
cannot import the downloader. ``downloader.head_info`` is the same HEAD logic for the wizard side:
Hugging Face answers a ``resolve`` URL with a redirect whose ``x-linked-size`` header is the real file size,
so that header is read without following the redirect to the CDN. The head function is injectable;
``--simulate`` uses ``fake_head``.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

HF = "https://huggingface.co"
MAX_BYTES = 15_500_000_000          # what a 16 GB card can hold: the check refuses larger files (decimal GB)
TIMEOUT_S = 15.0
MAX_HOPS = 3
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*(/[A-Za-z0-9][A-Za-z0-9._+-]*)*\.gguf$", re.IGNORECASE)
SPLIT_RE = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)
Head = Callable[[str], tuple[int, int | None]]


def resolve_url(repo: str, file: str) -> str:
    """The URL ``s03`` downloads a custom model from (the repository's main branch)."""
    return f"{HF}/{repo}/resolve/main/{urllib.parse.quote(file)}"


def shape_problem(repo: str, file: str) -> str | None:
    """Why the repository or file name cannot be right, in plain words; None when both look fine."""
    if not REPO_RE.match(repo):
        return "Type the repository as owner/name, for example Qwen/Qwen3-14B-GGUF"
    if not FILE_RE.match(file) or ".." in file or len(file) > 240:
        return "Type the GGUF file name, for example Qwen3-14B-Q4_K_M.gguf"
    if SPLIT_RE.search(file):
        return "Split files (-00001-of-00003) are not supported. Pick a single-file version"
    return None


def too_big_message(size: int) -> str:
    """The reason a file over the limit is refused."""
    gb = f"{size / 1e9:.1f}"
    return (f"That file is {gb} GB. A 16 GB graphics card holds files up to {MAX_BYTES / 1e9:.1f} GB. "
            "Pick a smaller version, for example Q4_K_M")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand 3xx answers back as errors so their headers can be read."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def urllib_head(url: str, hops: int = MAX_HOPS) -> tuple[int, int | None]:
    """HEAD ``url``: ``(status, size)``. Raises OSError when the site cannot be reached."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "NightshiftSetup"})
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            status, headers = int(resp.status), resp.headers
    except urllib.error.HTTPError as e:
        status, headers = int(e.code), e.headers
    except urllib.error.URLError as e:
        raise OSError(str(e.reason)) from None
    if 300 <= status < 400:
        linked = headers.get("x-linked-size", "")
        if linked.isdigit():
            return 200, int(linked)
        target = urllib.parse.urljoin(url, headers.get("location", ""))
        if hops > 0 and target.startswith("https://"):
            return urllib_head(target, hops - 1)
        return status, None
    length = headers.get("content-length", "")
    return status, int(length) if length.isdigit() else None


# plain-word answers, by what went wrong
_REJECT = {
    "missing": ("That file was not found in that repository. Check the spelling of both", None),
    "login": ("That repository needs a login. Nightshift only downloads public files", None),
    "network": ("Setup could not reach Hugging Face. Check the internet, then click out of the field again",
                "NS-E105"),
    "nosize": ("Hugging Face did not say how big that file is. Try another version of the model", None),
}


def _no(kind: str) -> dict[str, Any]:
    message, code = _REJECT[kind]
    return {"ok": False, "size": None, "message": message, "code": code}


def check_model(repo: str, file: str, head: Head | None = None) -> dict[str, Any]:
    """ModelCheck for a repository and file: ``{ok, size, message, code}``.

    ``ok`` is true only for an existing public GGUF of known size that fits a 16 GB card (at most
    ``MAX_BYTES``). Every refusal carries the reason in plain words.
    """
    repo, file = (repo or "").strip(), (file or "").strip().lstrip("/")
    problem = shape_problem(repo, file)
    if problem:
        return {"ok": False, "size": None, "message": problem, "code": None}
    send = head or urllib_head
    try:
        status, size = send(resolve_url(repo, file))
    except (OSError, ValueError):
        return _no("network")
    if status in (401, 403):
        return _no("login")
    if status == 404:
        return _no("missing")
    if status >= 400:
        return _no("network")
    if not size:
        return _no("nosize")
    if size > MAX_BYTES:
        return {"ok": False, "size": size, "message": too_big_message(size), "code": None}
    return {"ok": True, "size": size, "message": None, "code": None}


# ---- simulate mode ----------------------------------------------------------------------------------------
FAKE_SIZES = (
    ("70b", 42_500_000_000), ("f16", 28_000_000_000), ("q8", 14_600_000_000), ("q6", 12_300_000_000),
    ("q5", 10_700_000_000), ("q4", 9_000_000_000), ("q3", 7_000_000_000),
)


def fake_head(url: str) -> tuple[int, int | None]:
    """Simulate mode and tests: a size from the quantization in the name ("missing" is 404, "private" 401)."""
    name = urllib.parse.unquote(url.rsplit("/", 1)[-1]).lower()
    if "missing" in name:
        return 404, None
    if "private" in name:
        return 401, None
    for key, size in FAKE_SIZES:
        if key in name:
            return 200, size
    return 200, 8_000_000_000
