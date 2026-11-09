"""What setup installs, from manifest.json (docs/design/installer-ui.md 2.1, 2.3, 5.6). Standard library only.

A stdlib mirror of ``downloader.plan_bins`` and ``plan_models``: the server has no pydantic yet, so it reads
the manifest as plain JSON. ``tests/test_installer_web_plan.py`` proves the byte totals match the planner.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import ns_common.diskneed as diskneed
from installer.stage2.devmode import MIN_RAM_GB, STUB_BYTES
from installer.stage2.web.labels import GROUP_LABELS, llm_label

LINE_MBIT = 50.0
CARD_GB = 16.0               # the graphics card the product is built for
RUNTIME_GB = 0.6            # what the runtime itself keeps in video memory
INSTALL_MIN = 15
TIER_KEYS = {"small": "llm", "balanced": "llm_balanced", "best": "llm_best"}
GROUP_ORDER = ("tools", "python", "programs", "models")


def recommended_tier(ram_gb: float) -> str:
    """The tier setup preselects: Balanced (recommended) from 32 GB of RAM, else Small (the fallback)."""
    return "balanced" if ram_gb >= 31 else "small"


def home_need(
    manifest: dict[str, Any],
    home: str | Path,
    tier: str,
    *,
    custom: dict[str, Any] | None = None,
    dev: bool = False,
    profile: str = "gpu",
    include_code: bool = True,
    llama_pair: str | None = None,
    cuda: str | None = None,
    voice: bool = True,
    voicedesign: bool = False,
    models_source: str | None = None,
) -> diskneed.Need:
    """The space this install will use at ``home`` (H-6): the files it still has to download or copy, the
    programs and environments it still has to make, and the working reserve for videos.

    ``ns_common.diskneed`` does the sum from the manifest; what is already at ``home`` (files with a good
    ``.ok`` mark, unpacked programs, working environments) costs nothing. A dev install is the fixed 10 GB.
    ``custom`` is the story model the owner named (``repo``, ``file``, ``size``).
    """
    if dev:
        return diskneed.install_need(manifest, home, profile="cpu-dev", dev=True)
    if profile == "cpu-dev":
        pair = None
    else:
        pair = llama_pair or llama_pair_for(cuda) or "cuda13"
    return diskneed.install_need(
        manifest, home, profile=profile, tier=tier, custom=custom, include_code=include_code, llama_pair=pair,
        voice=voice, voicedesign=voicedesign, models_source=models_source,
    )


def required_free_gb(
    tier: str,
    custom_bytes: int | None = None,
    *,
    dev: bool = False,
    manifest: dict[str, Any] | None = None,
    home: str | Path | None = None,
    **kw: Any,
) -> int:
    """Free space a first install needs on the home drive, whole GB (see ``home_need``)."""
    custom = {"repo": "custom/custom", "file": "custom.gguf", "size": custom_bytes} if custom_bytes else None
    return home_need(
        manifest or diskneed.shipped_manifest(), home if home is not None else diskneed.NO_HOME, tier,
        custom=custom, dev=dev, **kw,
    ).gb


def load_manifest(app_path: Path) -> dict[str, Any]:
    """manifest.json as a dict."""
    data = json.loads((app_path / "manifest.json").read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("manifest.json must hold a JSON object")
    return data


def pinned(sha: object) -> bool:
    """True for a real 64-digit SHA256 (``TBD-release`` and null are not pins)."""
    return isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdefABCDEF" for c in sha)


def llama_pair_for(cuda: str | None) -> str | None:
    """cuda13 for CUDA 13 or newer, cuda12 for 12.4 to 12.x, None below that."""
    if not cuda:
        return None
    try:
        major, minor = (int(p) for p in str(cuda).split(".")[:2])
    except ValueError:
        return None
    if major >= 13:
        return "cuda13"
    return "cuda12" if (major, minor) >= (12, 4) else None


def _row(key: str, label: str, file: str | None, size: int | None, via: str, group: str) -> dict[str, Any]:
    return {"key": key, "label": label, "file": file, "bytes": size, "via": via, "group": group}


def _hf_row(entry: dict[str, Any], label: str) -> dict[str, Any]:
    name = PurePosixPath(entry["file"]).name
    return _row(name, label, name, entry.get("bytes"), "download", "models")


def _tts_label(file: str) -> str:
    low = file.lower()
    if "customvoice" in low:
        return "Voice: preset narrators"
    if "base" in low:
        return "Voice: your own voices"
    if "tokenizer" in low:
        return "Voice codec"
    return file


def _tier_entry(pm: dict[str, Any], profile: str, tier: str) -> dict[str, Any]:
    """The LLM entry of the tier, falling back to the default LLM (the same rule as ``plan_models``)."""
    if profile == "gpu" and tier != "small" and pm.get(TIER_KEYS[tier]):
        return pm[TIER_KEYS[tier]]
    return pm["llm"]


STUB_NOTE = " (stub, dev)"


def model_rows(
    manifest: dict[str, Any], profile: str, tier: str, custom: dict[str, Any] | None = None,
    *, dev: bool = False,
) -> list[dict[str, Any]]:
    """Rows for ``plan_models``: the LLM (tier or custom GGUF), embed, TTS GGUFs, whisper, YuNet.

    ``dev``: every model but YuNet is a stub of ``STUB_BYTES`` ("stub, dev" in the label), never a custom one.
    """
    if dev:
        stubbed = model_rows(manifest, profile, "small")
        return [
            r if r["key"] == "yunet" else {**r, "label": r["label"] + STUB_NOTE, "bytes": STUB_BYTES}
            for r in stubbed
        ]
    pm = manifest["profiles"][profile]
    if custom:
        llm = {"repo": custom["repo"], "file": custom["file"], "bytes": custom["size"]}
        llm_row = _hf_row(llm, llm_label("custom"))
    else:
        llm = _tier_entry(pm, profile, tier)
        shown_tier = tier if llm is not pm["llm"] else "small"
        llm_row = _hf_row(llm, llm_label(shown_tier))
    rows = [llm_row, _hf_row(pm["embed"], "Story memory")]
    rows += [_hf_row(f, _tts_label(f["file"])) for f in pm["tts"]]
    repo_name = PurePosixPath(pm["whisper_repo"]["repo"]).name
    whisper_file = repo_name if repo_name.startswith("faster-") else "faster-" + repo_name
    rows.append(_row("whisper", "Caption timing", whisper_file, None, "download", "models"))
    yunet = (manifest.get("bins") or {}).get("yunet")
    if yunet:
        rows.append(_row("yunet", "Face finder for Clipper", PurePosixPath(yunet["url"]).name, None,
                         "download", "models"))
    return rows


def bin_rows(
    manifest: dict[str, Any], profile: str, *, include_code: bool, llama_pair: str | None
) -> list[dict[str, Any]]:
    """Rows for ``plan_bins``: llama.cpp (+ cudart), OpenCode, yt-dlp, the window files.

    A dev install passes ``profile="cpu-dev"`` and ``include_code=False``: the CPU build, no OpenCode.
    """
    bins = manifest.get("bins") or {}
    if profile == "cpu-dev":
        keys = ["llama_cpu"]
    elif llama_pair == "cuda13":
        keys = ["llama_cuda13", "cudart13"]
    else:
        keys = ["llama_cuda", "cudart"]
    if include_code:
        keys.append("opencode")
    keys.append("yt_dlp")
    labels = {"opencode": "Nightshift Code", "yt_dlp": "Video downloader for Clipper"}
    rows: list[dict[str, Any]] = []
    for key in keys:
        pin = bins.get(key)
        if not pin:
            continue
        default = "CUDA runtime" if key.startswith("cudart") else "Model runner (llama.cpp)"
        rows.append(_row(key, labels.get(key, default), PurePosixPath(pin["url"]).name, None, "download",
                         "programs"))
    web = bins.get("webview2")
    if web and pinned(web.get("sha256")):
        rows.append(_row("webview2", "Setup window files", PurePosixPath(web["url"]).name, None, "download",
                         "programs"))
    return rows


def tool_rows() -> list[dict[str, Any]]:
    """Rows for the winget and uv steps (no size). Git is not one of them: nothing in setup needs it."""
    return [
        _row("vcruntime", "Visual C++ runtime", None, None, "winget", "tools"),
        _row("ffmpeg", "FFmpeg", None, None, "winget", "tools"),
        _row("terminal", "Windows Terminal", None, None, "winget", "tools"),
        _row("setup_env", "Python packages for setup", None, None, "uv", "python"),
    ]


def estimate(total_bytes: int) -> dict[str, Any]:
    """Download minutes at 50 Mbit/s, install minutes, and the label for the Welcome card."""
    download = math.ceil(total_bytes * 8 / (LINE_MBIT * 1e6 * 60))
    total = download + INSTALL_MIN
    if 50 <= total <= 75:
        label = "About 1 hour"
    elif total < 50:
        label = f"About {max(5, int(round(total / 5.0)) * 5)} minutes"
    else:
        label = f"About {round(total / 60.0 * 2) / 2:g} hours"
    return {"downloadMin": download, "installMin": INSTALL_MIN, "label": label}


def _group(group_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    sizes = [i["bytes"] for i in items if i["bytes"] is not None]
    return {
        "id": group_id,
        "label": GROUP_LABELS[group_id],
        "bytes": sum(sizes) if sizes else None,
        "items": [{k: i[k] for k in ("key", "label", "file", "bytes", "via")} for i in items],
    }


def vram_left_gb(file_bytes: int | float) -> float:
    """Video memory a 16 GB card has left for context: 16 GB minus the model file minus 0.6 GB of runtime.

    Decimal gigabytes, one decimal. Negative when the file is larger than the card (part of it then runs from
    system memory).
    """
    return round(CARD_GB - float(file_bytes) / 1e9 - RUNTIME_GB, 1)


def tier_infos(
    manifest: dict[str, Any], profile: str, *, ram_gb: float, vram_mb: int | None, dev: bool = False,
    need_for: Callable[[str], diskneed.Need] | None = None,
) -> dict[str, Any]:
    """The three Model size options with their sizes and whether this PC may pick them.

    A dev install (``dev``) offers Small only, as a stub of ``STUB_BYTES``. ``need_for(tier)`` is the space
    that tier's install needs (H-6); without it, a first install with every default.
    """
    pm = manifest["profiles"][profile]
    base_bytes = sum(r["bytes"] or 0 for r in model_rows(manifest, profile, "small", dev=dev)[1:])
    out: dict[str, Any] = {}
    for tier in ("small", "balanced", "best"):
        entry = pm.get(TIER_KEYS[tier])
        llm_bytes = int(entry.get("bytes") or 0) if entry else 0
        if dev:
            llm_bytes = STUB_BYTES if tier == "small" else 0
        allowed, reason = True, None
        if tier != "small":
            if dev:
                allowed, reason = False, "Not available in dev mode"
            elif not entry or profile != "gpu":
                allowed, reason = False, "Not available in this version"
            elif ram_gb < 31:
                allowed, reason = False, "Needs 32 GB of memory"
            elif vram_mb is not None and vram_mb < 15000:
                allowed, reason = False, "Needs a 16 GB graphics card"
        out[tier] = {
            "repo": entry.get("repo") if entry else None,
            "file": entry.get("file") if entry else None,
            "vramLeftGb": vram_left_gb(llm_bytes) if llm_bytes and profile == "gpu" else None,
            "recommended": tier == "balanced",
            "llmBytes": llm_bytes,
            "totalBytes": llm_bytes + base_bytes,
            "needFreeGb": need_for(tier).gb if need_for else required_free_gb(tier, dev=dev, profile=profile),
            "needsRamGb": (MIN_RAM_GB if dev else 16) if tier == "small" else 32,
            "allowed": allowed,
            "reason": reason,
        }
    return out


def build_plan(
    manifest: dict[str, Any],
    *,
    profile: str,
    tier: str,
    include_code: bool,
    cuda: str | None,
    ram_gb: float,
    vram_mb: int | None,
    custom: dict[str, Any] | None = None,
    llama_pair: str | None = None,
    dev: bool = False,
    home: str | Path | None = None,
    voice: bool = True,
    voicedesign: bool = False,
    models_source: str | None = None,
) -> dict[str, Any]:
    """The InstallPlan for the Welcome and Choices screens (``custom`` replaces the tier's story model).

    ``dev`` (docs/DEV_MODE.md): the CPU build of llama.cpp, no OpenCode, stub models of the Small tier.
    ``home`` (the install folder), ``voice`` (False: the ``fake_voice`` engine test), ``voicedesign`` and
    ``models_source`` say what the space the install needs (``needFreeGb``, in words ``needMakeup``) is for.
    """
    if dev:
        profile, tier, include_code, custom, llama_pair = "cpu-dev", "small", False, None, None
    rows = tool_rows()
    pair = llama_pair or llama_pair_for(cuda)
    rows += bin_rows(manifest, profile, include_code=include_code, llama_pair=pair)
    rows += model_rows(manifest, profile, tier, custom, dev=dev)
    groups = [_group(gid, [r for r in rows if r["group"] == gid]) for gid in GROUP_ORDER]
    total = sum(r["bytes"] or 0 for r in rows)
    unknown = sum(1 for r in rows if r["bytes"] is None and r["via"] == "download")

    def need_of(chosen: str, chosen_custom: dict[str, Any] | None = None) -> diskneed.Need:
        return home_need(
            manifest, home if home is not None else diskneed.NO_HOME, chosen,
            custom=chosen_custom, dev=dev, profile=profile, include_code=include_code, llama_pair=pair,
            cuda=cuda, voice=voice, voicedesign=voicedesign, models_source=models_source,
        )

    need = need_of(tier, custom_entry(custom))
    return {
        "tier": tier,
        "profile": profile,
        "groups": groups,
        "totalBytes": total,
        "unknownCount": unknown,
        "needFreeGb": need.gb,
        "needMakeup": need.makeup(),
        "estimate": estimate(total),
        "recommendedTier": "small" if dev else recommended_tier(ram_gb),
        "tiers": tier_infos(manifest, profile, ram_gb=ram_gb, vram_mb=vram_mb, dev=dev,
                            need_for=lambda t: need_of(t)),
    }


def custom_entry(custom: dict[str, Any] | None) -> dict[str, Any] | None:
    """The story model the owner named, as ``diskneed`` wants it (``repo``, ``file``, ``size``)."""
    if not custom:
        return None
    return {
        "repo": custom.get("repo", ""),
        "file": custom.get("file", "custom.gguf"),
        "size": int(custom["size"]),
    }


def planned_sizes(
    manifest: dict[str, Any], profile: str, tier: str, *, include_code: bool, llama_pair: str | None
) -> dict[str, int | None]:
    """Row key -> bytes for every download row (what the tests compare with ``downloader``)."""
    rows = bin_rows(manifest, profile, include_code=include_code, llama_pair=llama_pair)
    rows += model_rows(manifest, profile, tier)
    return {r["key"]: r["bytes"] for r in rows}
