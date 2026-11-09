"""One way of saying how much disk space something needs and how much is free (orders H-6 and D-33).

Setup, the studio, the nightly run and the dashboard all ask this module, so they cannot disagree:

* ``install_need`` is the space an install will really use on the home's volume, worked out from the manifest
  and from what is already on the drive: the bytes of every file setup will still download or copy (zero for a
  file that is in place with a good ``.ok`` mark), the unpacked size of the programs and environments it still
  has to make, and a stated working reserve for videos.
* ``format_need``, ``short_need`` and ``clip_need`` are what one job writes, from the sizes the pipeline
  really produces (``JOB_SIZES`` below says how each was estimated). ``Need.remaining`` is the share a resumed
  job still has to write.
* ``check`` / ``require_room`` compare a need with the free space of the volume that holds a folder. A volume
  whose free space cannot be read never blocks anything: it is logged (``disk_free_unknown``) and the work
  goes on.

Standard library only (the installer's bare Python imports it). Sizes are binary: "GB" is 1024 ** 3 bytes,
what Windows shows as GB, so the number matches what a person sees in Explorer.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import zipfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ns_common.errors import NsError
from ns_common.paths import Paths, repo_root

KIB: Final = 1024
MIB: Final = 1024**2
GIB: Final = 1024**3

# ------------------------------------------------------------------------------------------- jobs (D-33)
#: How much one piece of work writes, in MiB, and how each number was estimated. These are the pipeline's own
#: figures, not guesses at a drive: ``ns_studio/media/render.py`` (CRF 23 under a 12 Mbit/s cap, AAC 192
#: kbit/s), ``clip_sources.pick_pixabay_rendition`` (a 1080p stock clip is about 10 MB for 17 s) and
#: ``clips.CACHE_DAYS``.
JOB_SIZES: Final[dict[str, str]] = {
    "clip": "8 MB: one stock clip as fetched, a 1080p rendition (about 10 MB for 17 s) and Pexels' HD files",
    "short": "100 MB: 6 clips (48), the render of a 50 s Short at about 6 Mbit/s (38), its voice (5), "
    "scratch (9)",
    "long": "176 MB a minute: 10 clips (80), the render at about 6 Mbit/s (45), the segments joined into it "
    "(45), the voice (6)",
    "clip_job": "100 MB of audio and analysis, then 50 MB a clip (60 s of 1080x1920 at about 7 Mbit/s); a "
    "link adds the downloaded video (1.5 GB, 1080p at about 4 Mbit/s for an hour), a file its own size",
}
CLIP_MIB: Final = 8
SHORT_CLIPS: Final = 6
SHORT_RENDER_MIB: Final = 38
SHORT_VOICE_MIB: Final = 5
SHORT_SCRATCH_MIB: Final = 9
LONG_CLIPS_PER_MIN: Final = 10
LONG_RENDER_MIB_PER_MIN: Final = 45
LONG_SCRATCH_MIB_PER_MIN: Final = 45  # the render is made in segments and joined: a second copy while it runs
LONG_VOICE_MIB_PER_MIN: Final = 6
CLIP_JOB_BASE_MIB: Final = 100
CLIP_JOB_PER_CLIP_MIB: Final = 50
CLIP_JOB_URL_MIB: Final = 1536
FAKE_CLIP_SHARE: Final = 0.1  # a fake clip source writes tiny placeholder files
SHORT_BYTES: Final = (SHORT_CLIPS * CLIP_MIB + SHORT_RENDER_MIB + SHORT_VOICE_MIB + SHORT_SCRATCH_MIB) * MIB
#: A resumed job whose sized stages are all done still writes its last files (sidecars, the Outbox move).
MIN_RESUME_BYTES: Final = 20 * MIB

# --------------------------------------------------------------------------------------- the install (H-6)
#: The working reserve of a first install: room for the videos it will make. Twelve weeks of the default
#: cadence (one 12-minute video with its 5 Shorts, plus 3 more Shorts, a week) by the estimates above, which
#: is also how long the clip cache keeps what it fetched (``clips.CACHE_DAYS`` is 90), plus the 2 GB headroom
#: the downloader pauses at, comes to 36 GB; the reserve is that rounded up to 38 GB, so that a first install
#: of the Small tier comes out at the spec's 60 GB. A test keeps this number >= that sum, so it follows the
#: job estimates.
RESERVE_BYTES: Final = 38 * GIB
#: What setup always leaves free, and the whole reserve of a repair or update over a finished install: the
#: downloader pauses every transfer under 2 GB free (``rows.LOW_DISK_GB``).
HEADROOM_BYTES: Final = 2 * GIB
#: A home that is not there: "nothing is in place yet" for a need asked before any folder is chosen.
NO_HOME: Final = Path("ns-no-such-home-0")
#: A dev install (docs/DEV_MODE.md) writes stubs for the models: the programs and environments, nothing else.
DEV_INSTALL_BYTES: Final = 10 * GIB
#: The PyTorch voice fallback, when the manifest pins no voice engine (``bins.qwentts`` is null).
TORCH_FALLBACK_BYTES: Final = 10 * GIB

#: Programs: key of ``manifest.bins`` -> (bytes setup downloads, bytes unpacked). Downloads are the release
#: asset sizes (GitHub release listings of llama.cpp b12032, Nightshift v0.1.4, OpenCode v1.19.5, uv 0.13.6);
#: unpacked sizes are measured on an install where one exists (llama_cpu 61 MB, opencode 202 MB, uv 31 MB,
#: yt-dlp 23 MB, window files 1 MB) and twice the zip for the CUDA bundles and the voice engine.
PROGRAM_BYTES: Final[dict[str, tuple[int, int]]] = {
    "llama_cuda": (279_952_566, 600_000_000),
    "cudart": (391_443_627, 800_000_000),
    "llama_cuda13": (165_164_493, 350_000_000),
    "cudart13": (423_535_356, 870_000_000),
    "llama_cpu": (24_582_754, 64_000_000),
    "qwentts": (632_009_200, 1_300_000_000),
    "opencode": (81_738_227, 212_000_000),
    "uv": (15_239_500, 32_000_000),
    "yt_dlp": (24_000_000, 0),
    "webview2": (8_000_000, 1_000_000),
}
UNKNOWN_PROGRAM_BYTES: Final = (100_000_000, 200_000_000)
#: Model files the manifest gives no ``bytes`` for. The face finder is 229 KB. Whisper is ``model.bin`` plus
#: its small side files, from the Hugging Face file listing at the pinned revisions; an unnamed model is
#: counted as the large one.
YUNET_BYTES: Final = 250_000
WHISPER_BYTES: Final[dict[str, int]] = {"tiny": 78_203_619, "distil-large-v3": 1_516_479_628}
UNKNOWN_WHISPER_BYTES: Final = 1_516_479_628
#: Environments, unpacked: the studio and supervisor venvs measured on QA's engine install (705 MB, 95 MB, no
#: CUDA wheels); a GPU install adds the cuBLAS and cuDNN wheels (567 MB and 693 MB on PyPI), once in the uv
#: cache under the home and once installed.
VENV_BYTES: Final[dict[str, int]] = {"studio": 750 * MIB, "supervisor": 100 * MIB}
GPU_WHEELS_BYTES: Final = 2 * (567_544_208 + 692_992_268)
VENV_MIN_ENTRIES: Final = 10  # a venv with fewer packages than this is an interrupted one (D-15)

#: ``manifest.bins`` key -> (folder under the home, files that must be there for it to count as unpacked).
_EXTRACT: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "llama_cuda": ("bin/llama", ("llama-server.exe",)),
    "llama_cuda13": ("bin/llama", ("llama-server.exe",)),
    "llama_cpu": ("bin/llama", ("llama-server.exe",)),
    "cudart": ("bin/llama", ()),
    "cudart13": ("bin/llama", ()),
    "opencode": ("bin/opencode", ("opencode.exe",)),
    "qwentts": ("bin/qwentts", ("qwen-tts.exe", "qwen-codec.exe")),
    "webview2": ("bin/webview2", ()),
}


# ------------------------------------------------------------------------------------------------ the answer
@dataclass(frozen=True)
class Part:
    """One line of a need: ``key`` groups it, ``label`` finishes the sentence "<size> <label>"."""

    key: str
    label: str
    bytes: int


def size_text(n: float) -> str:
    """``13.6 GB``, ``640 MB``, ``12 KB``: the size as a person reads it."""
    if n >= GIB:
        value = n / GIB
        text = f"{value:.0f}" if value >= 100 else f"{value:.1f}".removesuffix(".0")
        return f"{text} GB"
    if n >= MIB:
        return f"{n / MIB:.0f} MB"
    return f"{max(1, round(n / KIB))} KB"


def _free_text(free: int | None, where: str) -> str:
    if free is None:
        return f"the free space on {where} could not be read"
    return f"{where} has {size_text(free)} free"


@dataclass(frozen=True)
class Need:
    """What something needs: the parts, in the order they are told. ``unit`` rounds the headline."""

    what: str
    parts: tuple[Part, ...]
    unit: str = "GB"

    @property
    def total(self) -> int:
        """Bytes in all."""
        return sum(p.bytes for p in self.parts)

    @property
    def gb(self) -> int:
        """The headline in whole GB, rounded up (a need is never understated)."""
        return math.ceil(self.total / GIB)

    @property
    def mb(self) -> int:
        """The headline in whole MB, rounded up."""
        return math.ceil(self.total / MIB)

    def amount(self) -> str:
        """``60 GB`` for an install, ``100 MB`` for a job."""
        return f"{self.gb} GB" if self.unit == "GB" else f"{self.mb} MB"

    def makeup(self) -> str:
        """What the number is made of, in one line: ``13.6 GB of models to download, 5 GB of ... and ...``."""
        bits = [f"{size_text(p.bytes)} {p.label}" for p in self.parts if p.bytes > 0]
        if self.unit == "GB" and not any(p.key in ("download", "copy") for p in self.parts):
            bits.insert(0, "no models to download")
        if len(bits) <= 1:
            return "".join(bits)
        return ", ".join(bits[:-1]) + " and " + bits[-1]

    def sentence(self, free: int | None, where: str, *, subject: str = "This") -> str:
        """``This install needs about 60 GB (<makeup>); D: has 49 GB free`` (``free`` None: not known)."""
        return f"{subject} needs about {self.amount()} ({self.makeup()}); {_free_text(free, where)}"

    def short_sentence(self, free: int | None, where: str) -> str:
        """``needs about 100 MB; D: has 20 MB free``: the same without what it is made of (a note)."""
        return f"needs about {self.amount()}; {_free_text(free, where)}"

    def remaining(self, done: Collection[str]) -> Need:
        """The part of a job a resume still writes, given the names of the stages already done.

        The voice and clip parts go with their stages, the render with ``render``, or with ``render_master``
        and ``shorts_derive`` together, and the scratch files with ``outbox``. A Clipper job (its stages are
        ``clip_*``): the copied or downloaded video with ``clip_import``, the audio and analysis files with
        the last of ``clip_audio``, ``clip_analyze``, ``clip_transcribe``, the renders with ``clip_render``.
        Never less than ``MIN_RESUME_BYTES``: the last files still have to be written.
        """
        gone: set[str] = set()
        if "voice" in done:
            gone.add("voice")
        if "clips" in done:
            gone.add("clips")
        if "render" in done or "clip_render" in done or {"render_master", "shorts_derive"} <= set(done):
            gone.add("render")
        analysed = {"clip_audio", "clip_analyze", "clip_transcribe"} <= set(done)
        if "outbox" in done or "clip_outbox" in done or analysed:
            gone.add("scratch")
        if "clip_import" in done:
            gone.add("source")
        kept = tuple(p for p in self.parts if p.key not in gone)
        if sum(p.bytes for p in kept) < MIN_RESUME_BYTES:
            kept = (Part("resume", "to finish the last files", MIN_RESUME_BYTES),)
        return Need(self.what + " (resumed)", kept, self.unit)


@dataclass(frozen=True)
class Verdict:
    """A need compared with a volume. ``free`` None: the free space could not be read (never blocks)."""

    ok: bool
    need: Need
    free: int | None
    where: str
    subject: str = "This"

    @property
    def message(self) -> str:
        """The one-line sentence for a log, an error detail or a row."""
        return self.need.sentence(self.free, self.where, subject=self.subject)

    @property
    def short(self) -> str:
        """The same without the make-up: ``needs about 100 MB; D: has 20 MB free``."""
        return self.need.short_sentence(self.free, self.where)


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = _Unset()


def where_of(path: Path | str) -> str:
    """The volume a folder is on, as a person names it: ``D:``."""
    p = Path(path)
    return p.drive or p.anchor or str(p)


def free_bytes(path: Path | str, log: Any = None) -> int | None:
    """Free bytes on the volume that holds ``path`` (its nearest folder that exists); None if unreadable.

    A failure here is logged as ``disk_free_unknown`` and never raised: not knowing is not a reason to block.
    """
    p = Path(path)
    try:
        while not p.exists() and p != p.parent:
            p = p.parent
        return int(shutil.disk_usage(p).free)
    except (OSError, ValueError) as e:
        if log is not None:
            log.warn("disk_free_unknown", f"cannot read the free space for {path}: {type(e).__name__}: {e}")
        return None


def check(
    need: Need,
    path: Path | str,
    *,
    free: int | float | None | _Unset = UNSET,
    subject: str = "This",
    log: Any = None,
) -> Verdict:
    """Compare ``need`` with the volume of ``path``. ``free`` (bytes) replaces reading it; None is unknown."""
    if isinstance(free, _Unset):
        have = free_bytes(path, log)
    else:
        have = None if free is None else int(free)
    where = where_of(path)
    ok = have is None or have >= need.total
    return Verdict(ok, need, have, where, subject)


def require_room(
    need: Need,
    path: Path | str,
    *,
    free: int | float | None | _Unset = UNSET,
    subject: str = "This",
    log: Any = None,
) -> Verdict:
    """``check``, and NS-E104 (writing nothing) when the volume has less than ``need``."""
    verdict = check(need, path, free=free, subject=subject, log=log)
    if not verdict.ok:
        if log is not None:
            log.warn(
                "disk_room_refused",
                verdict.message,
                code="NS-E104",
                need_bytes=need.total,
                free_bytes=verdict.free,
                what=need.what,
            )
        raise NsError(
            "NS-E104",
            verdict.message,
            need_bytes=need.total,
            free_bytes=verdict.free,
            where=verdict.where,
            what=need.what,
            short=verdict.short,
        )
    return verdict


# --------------------------------------------------------------------------------------------------- jobs
def _mib(n: float) -> int:
    return round(n * MIB)


def short_need(count: int = 1, *, real_footage: bool = True) -> Need:
    """``count`` Shorts: stock clips, the render, the voice and scratch files (about 100 MB each)."""
    share = 1.0 if real_footage else FAKE_CLIP_SHARE
    return Need(
        f"{count} Short" + ("s" if count != 1 else ""),
        (
            Part("clips", "of stock clips", _mib(count * SHORT_CLIPS * CLIP_MIB * share)),
            Part("voice", "of voice", _mib(count * SHORT_VOICE_MIB)),
            Part("render", "for the render", _mib(count * SHORT_RENDER_MIB)),
            Part("scratch", "of scratch files", _mib(count * SHORT_SCRATCH_MIB)),
        ),
        "MB",
    )


def long_need(minutes: float, shorts: int = 0, *, real_footage: bool = True) -> Need:
    """A full video of ``minutes`` with ``shorts`` Shorts cut from it (176 MB a minute, 100 MB a Short)."""
    share = 1.0 if real_footage else FAKE_CLIP_SHARE
    return Need(
        f"a {minutes:g}-minute video with {shorts} Shorts",
        (
            Part(
                "clips",
                "of stock clips",
                _mib(
                    minutes * LONG_CLIPS_PER_MIN * CLIP_MIB * share + shorts * SHORT_CLIPS * CLIP_MIB * share
                ),
            ),
            Part("voice", "of voice", _mib(minutes * LONG_VOICE_MIB_PER_MIN + shorts * SHORT_VOICE_MIB)),
            Part(
                "render",
                "for the renders",
                _mib(minutes * LONG_RENDER_MIB_PER_MIN + shorts * SHORT_RENDER_MIB),
            ),
            Part(
                "scratch",
                "of scratch files",
                _mib(minutes * LONG_SCRATCH_MIB_PER_MIN + shorts * SHORT_SCRATCH_MIB),
            ),
        ),
        "MB",
    )


def format_need(
    fmt: str,
    *,
    long_minutes: tuple[int, int] = (10, 14),
    shorts_per_master: tuple[int, int] = (4, 6),
    count: int = 1,
    real_footage: bool = True,
    max_clips: int = 6,
) -> Need:
    """What a job of this format writes, at the top of the channel's ranges (the longest it may draw).

    Every format a job can have (``ns_studio.jobs.Fmt``, read back from ``job.json`` on a resume) has an
    estimate, so no resume goes unchecked; a name nobody has raises ``ValueError``.

    ``shorts_only``: ``count`` Shorts. ``master_first``: the master and its Shorts. ``series_compilation``,
    ``master_from_script`` and a series part: a master of the channel's middle length with 5 Shorts (the
    part's own design is not read). ``clip``: a Clipper job of ``max_clips`` clips from a file already on the
    drive.
    """
    if fmt == "shorts_only":
        return short_need(count, real_footage=real_footage)
    if fmt == "master_first":
        return long_need(long_minutes[1], shorts_per_master[1], real_footage=real_footage)
    if fmt in ("series_compilation", "series_part", "master_from_script"):
        return long_need((long_minutes[0] + long_minutes[1]) / 2, 5, real_footage=real_footage)
    if fmt == "clip":
        return clip_need(max_clips, real_footage=real_footage)
    raise ValueError(f"no disk estimate for format {fmt!r}")


def clip_need(
    max_clips: int = 6,
    *,
    url_source: bool = False,
    rerun: bool = False,
    real_footage: bool = True,
    source_bytes: int = 0,
) -> Need:
    """A Clipper job: analysis files, ``max_clips`` clip renders, and the video itself when it is copied in.

    ``rerun`` (another template over a finished job) writes the renders only. A link adds the downloaded video
    (``CLIP_JOB_URL_MIB``); a file on this PC adds ``source_bytes``, its own size: the import copies it
    into the home (``source.import_source``) unless it already sits there.
    """
    del real_footage  # the Clipper cuts the owner's own video: nothing is fetched
    parts = [Part("render", "for the clip renders", _mib(max_clips * CLIP_JOB_PER_CLIP_MIB))]
    if not rerun:
        parts.insert(0, Part("scratch", "of audio and analysis files", _mib(CLIP_JOB_BASE_MIB)))
        if url_source:
            parts.insert(0, Part("source", "for the downloaded video", _mib(CLIP_JOB_URL_MIB)))
        elif source_bytes > 0:
            parts.insert(0, Part("source", "for the copy of your video", int(source_bytes)))
    return Need(f"{max_clips} clips", tuple(parts), "MB")


# ------------------------------------------------------------------------------------------- the install
@dataclass(frozen=True)
class Piece:
    """One thing setup puts on the drive. ``size`` is what is written, ``dest`` where its mark is."""

    name: str
    group: str  # "models" or "programs"
    size: int
    dest: Path | None = None
    sha256: str | None = None
    kind: str = "file"  # "file" (a .ok mark), "zip" (a .ns-<name>.done flag) or "venv"
    unpacked: int = 0
    extract_to: Path | None = None
    expect: tuple[str, ...] = ()


def shipped_manifest() -> dict[str, Any]:
    """``manifest.json`` of this program folder as a dict (the setup window has no pydantic yet)."""
    data = json.loads((repo_root() / "manifest.json").read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("manifest.json must hold a JSON object")
    return data


def _is_pinned(sha: object) -> bool:
    return isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdefABCDEF" for c in sha)


def _name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _hf(entry: Mapping[str, Any], folder: Path, group: str = "models") -> Piece:
    name = _name(str(entry["file"]))
    return Piece(name, group, int(entry.get("bytes") or 0), folder / name, entry.get("sha256"))


def install_pieces(
    manifest: Mapping[str, Any],
    home: Path | str,
    *,
    profile: str = "gpu",
    tier: str = "small",
    custom: Mapping[str, Any] | None = None,
    include_code: bool = True,
    llama_pair: str | None = "cuda13",
    voice: bool = True,
    voicedesign: bool = False,
) -> list[Piece]:
    """Every file and environment a full install of this shape puts under ``home`` (it mirrors the planners).

    ``installer.stage2.downloader.plan_models`` and ``plan_bins`` name the same files at the same paths (a
    compares them); ``voice=False`` is the ``fake_voice`` engine test, which leaves out the voice models and
    the voice engine.
    """
    paths = Paths(Path(home))
    pm = manifest["profiles"][profile]
    out: list[Piece] = []
    # ---- models
    llm = pm["llm"]
    if profile == "gpu":
        llm = (
            pm.get("llm_balanced") if tier == "balanced" else pm.get("llm_best") if tier == "best" else None
        ) or llm
    if custom:
        out.append(
            Piece(
                _name(str(custom["file"])),
                "models",
                int(custom["size"]),
                paths.models_llm / _name(str(custom["file"])),
                None,
            )
        )
    else:
        out.append(_hf(llm, paths.models_llm))
    out.append(_hf(pm["embed"], paths.models_embed))
    if voice:
        out += [_hf(f, paths.models_tts) for f in pm["tts"]]
        if voicedesign:
            out += [_hf(f, paths.models_tts) for f in pm.get("tts_optional") or []]
    wdir = paths.models_whisper / str(pm["whisper_model"])
    pinned = dict((pm.get("whisper_repo") or {}).get("files_sha256") or {})
    out.append(
        Piece(
            "model.bin",
            "models",
            WHISPER_BYTES.get(str(pm["whisper_model"]), UNKNOWN_WHISPER_BYTES),
            wdir / "model.bin",
            pinned.get("model.bin"),
        )
    )
    yunet = (manifest.get("bins") or {}).get("yunet")
    if yunet:
        out.append(
            Piece(
                "yunet",
                "models",
                YUNET_BYTES,
                paths.home / "models" / "cv" / "yunet.onnx",
                yunet.get("sha256"),
            )
        )
    # ---- programs
    bins = manifest.get("bins") or {}
    if profile == "cpu-dev":
        keys = ["llama_cpu"]
    elif llama_pair == "cuda13":
        keys = ["llama_cuda13", "cudart13"]
    else:
        keys = ["llama_cuda", "cudart"]
    if include_code:
        keys.append("opencode")
    keys += ["yt_dlp", "webview2", "uv"]
    if voice and bins.get("qwentts"):
        keys.append("qwentts")
    downloads = paths.home / "bin" / "_downloads"
    for key in keys:
        pin = bins.get(key)
        if not pin:
            continue
        if key == "webview2" and not _is_pinned(pin.get("sha256")):
            continue  # the dashboard then uses the Edge or Chrome app window
        zip_bytes, unpacked = PROGRAM_BYTES.get(key, UNKNOWN_PROGRAM_BYTES)
        if key == "yt_dlp":
            out.append(
                Piece(
                    key,
                    "programs",
                    zip_bytes,
                    paths.home / "bin" / "yt-dlp" / "yt-dlp.exe",
                    pin.get("sha256"),
                )
            )
            continue
        if key == "uv":
            out.append(
                Piece(
                    key,
                    "programs",
                    zip_bytes,
                    kind="zip",
                    unpacked=unpacked,
                    extract_to=paths.bin_uv,
                    expect=("uv.exe",),
                )
            )
            continue
        folder, expect = _EXTRACT.get(key, (f"bin/{key}", ()))
        file_name = _name(str(pin.get("url") or key))
        if key == "webview2":
            file_name = f"microsoft.web.webview2.{pin.get('version')}.nupkg"
        if key == "qwentts":
            file_name = "qwentts.zip"
        out.append(
            Piece(
                key,
                "programs",
                zip_bytes,
                downloads / file_name,
                pin.get("sha256"),
                "zip",
                unpacked,
                paths.home / folder,
                expect,
            )
        )
    if voice and not bins.get("qwentts"):  # no published voice engine: s02 falls back to PyTorch
        out.append(
            Piece(
                "voice_fallback",
                "programs",
                0,
                kind="venv",
                unpacked=TORCH_FALLBACK_BYTES,
                extract_to=paths.venvs / "tts",
            )
        )
    # ---- environments
    cuda = profile == "gpu"
    for name, size in VENV_BYTES.items():
        extra = GPU_WHEELS_BYTES if cuda and name == "studio" else 0
        out.append(
            Piece(
                f"venv:{name}",
                "programs",
                0,
                kind="venv",
                unpacked=size + extra,
                extract_to=paths.venvs / name,
            )
        )
    return out


def _marker_ok(dest: Path, sha256: str | None) -> bool:
    """True when ``dest`` has a ``.ok`` mark that still matches its size and mtime (and a pinned hash).

    The same test as ``installer.stage2.downloader.is_verified``: two small reads and a stat, no hashing.
    """
    try:
        st = dest.stat()
        marker = json.loads(dest.with_name(dest.name + ".ok").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(marker, dict):
        return False
    if marker.get("size") != st.st_size or marker.get("mtime_ns") != st.st_mtime_ns:
        return False
    return sha256 is None or str(marker.get("sha256") or "").lower() == sha256.lower()


def _part_bytes(dest: Path | None) -> int:
    """Bytes a half-finished download of ``dest`` already holds (the downloader resumes from them)."""
    if dest is None:
        return 0
    try:
        return dest.with_name(dest.name + ".part").stat().st_size
    except OSError:
        return 0


def _zip_done(piece: Piece) -> bool:
    folder = piece.extract_to
    if folder is None:
        return False
    if piece.name == "uv":
        return (folder / "uv.exe").is_file()
    return (folder / f".ns-{piece.name}.done").exists() and all((folder / e).exists() for e in piece.expect)


def _venv_ready(folder: Path | None) -> bool:
    """An environment with an interpreter and some packages in it (an interrupted ``uv venv`` has neither)."""
    if folder is None:
        return False
    try:
        if not (folder / "Scripts" / "python.exe").is_file():
            return False
        with os.scandir(folder / "Lib" / "site-packages") as it:
            return sum(1 for _ in it) >= VENV_MIN_ENTRIES
    except OSError:
        return False


def _source_names(folder: Path | str | None, wanted: Collection[str], max_depth: int = 4) -> set[str]:
    """The wanted file names a models folder holds, also inside its ``*.zip`` files (the same depth the copy
    looks to, and what the window's folder check counts)."""
    found: set[str] = set()
    if not folder or not Path(folder).is_dir():
        return found
    base = len(Path(folder).parts)
    for current, dirs, files in os.walk(folder):
        if len(Path(current).parts) - base >= max_depth:
            dirs[:] = []
        found.update(n for n in files if n in wanted)
        for name in files:
            if name.lower().endswith(".zip") and not found >= set(wanted):
                try:
                    with zipfile.ZipFile(Path(current) / name) as zf:
                        found.update(leaf for i in zf.infolist() if (leaf := _name(i.filename)) in wanted)
                except (OSError, zipfile.BadZipFile):
                    continue
        if found >= set(wanted):
            break
    return found


def _installed(paths: Paths) -> bool:
    """True for a finished install: its answers are saved and its studio environment is in place."""
    return (paths.config / "answers.json").is_file() and _venv_ready(paths.venvs / "studio")


def custom_entry(custom: Any) -> dict[str, Any] | None:
    """The story model the owner named, as ``install_need`` wants it (``repo``, ``file``, ``size``).

    ``custom`` can be anything a draft, a query or an answers file held: a shape it cannot use (not an object,
    no repo or file name, a size that is not a positive whole number) is None, so the number is worked out for
    the tier instead and the answers check reports the bad entry as a field error (it never raises). One rule
    for the setup window and the console wizard.
    """
    if not isinstance(custom, dict):
        return None
    size = custom.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return None
    repo, file = custom.get("repo"), custom.get("file")
    if not isinstance(repo, str) or not isinstance(file, str) or not repo.strip() or not file.strip():
        return None
    return {"repo": repo, "file": file, "size": size}


def install_need(
    manifest: Mapping[str, Any],
    home: Path | str,
    *,
    profile: str = "gpu",
    tier: str = "small",
    custom: Mapping[str, Any] | None = None,
    include_code: bool = True,
    llama_pair: str | None = "cuda13",
    voice: bool = True,
    voicedesign: bool = False,
    models_source: Path | str | None = None,
    dev: bool = False,
) -> Need:
    """The space this install will use on the volume of ``home``, given what is already there.

    * models: the bytes of each file that is not in place with a good mark (less what a ``.part`` holds).
      A file the ``models_source`` folder has is copied, not downloaded, but the copy still lands in the home
      and takes its bytes there, so it counts in full and is told as "to copy from your folder into the
      install folder". (Head order H-6 says "zero for files ... supplied by models_source"; that is the
      download. The reading here is the physical one, for the Head to confirm.)
    * programs and environments: the download and the unpacked size of each program not yet unpacked, and each
      environment not yet made;
    * the working reserve (``RESERVE_BYTES``) of a first install; a repair or update over a finished install
      (its answers saved, its studio environment in place) keeps only ``HEADROOM_BYTES``;
    * a dev install (``dev``) is the fixed ``DEV_INSTALL_BYTES`` it always was.
    """
    paths = Paths(Path(home))
    if dev:
        return Need(
            "a dev install",
            (Part("programs", "for stub models, programs and environments", DEV_INSTALL_BYTES),),
            "GB",
        )
    pieces = install_pieces(
        manifest,
        home,
        profile=profile,
        tier=tier,
        custom=custom,
        include_code=include_code,
        llama_pair=llama_pair,
        voice=voice,
        voicedesign=voicedesign,
    )
    in_source = _source_names(models_source, {p.dest.name for p in pieces if p.group == "models" and p.dest})
    download = copy = programs = 0
    for p in pieces:
        if p.kind == "venv":
            if not _venv_ready(p.extract_to):
                programs += p.unpacked
            continue
        if p.kind == "zip":
            if not _zip_done(p):
                programs += max(0, p.size - _part_bytes(p.dest)) + p.unpacked
            continue
        if p.dest is not None and _marker_ok(p.dest, p.sha256):
            continue
        left = max(0, p.size - _part_bytes(p.dest))
        if p.group == "programs":
            programs += left
        elif p.dest is not None and p.dest.name in in_source:
            copy += left
        else:
            download += left
    parts: list[Part] = []
    if download:
        parts.append(Part("download", "of models to download", download))
    if copy:
        parts.append(Part("copy", "of models to copy from your folder into the install folder", copy))
    if programs:
        parts.append(Part("programs", "of programs and environments", programs))
    if _installed(paths):
        parts.append(Part("headroom", "of headroom", HEADROOM_BYTES))
    else:
        parts.append(Part("reserve", "working reserve for videos", RESERVE_BYTES))
    return Need("this install", tuple(parts), "GB")
