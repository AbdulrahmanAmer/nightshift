"""The model files' check record, and what a setup run over a finished install owes them.

Card model-file-damaged.

Standard library only: the setup window reads it (``controller``), next to the checker and the engine.

* ``logs/model-check.json`` holds one entry per model file, keyed by its path under the install folder
  (``models/llm/x.gguf``): ``verdict`` (the checker's word: ``ok``, ``bad hash``, ``missing``, ``unreadable``,
  ``ok (no pin)``), ``sha256_ok`` (True, False, or None when no hash was read), ``bytes`` and ``mtime_ns`` of
  the file when it was checked, ``checked_at`` (UTC) and ``seconds``. The one checker
  (``installer.stage2.verify_downloads.check_item``) writes it after each file, wherever it runs: ``ns verify
  --downloads``, a Repair (s03) and the engine after a model failed to load. Written whole each time
  (``write_text_atomic``); a torn or unreadable file reads as empty, and a lost entry only means that file
  counts as not checked.
* A ``bad hash`` entry is evidence only while the file still has the size and date it was checked with: a
  file replaced since (a Repair) is not "damaged" by an old entry.
* ``repair_owed(home, planned)`` says why a setup run over a finished install has model work to do, from cheap
  reads only (stat calls, the ``.ok`` markers setup writes, the record): a recorded damaged file, a model file
  whose marker no longer matches it (gone, or its size or date changed), a damaged file set aside (``.bad``)
  by a Repair that did not finish, and every file of the plan (``planned_files``) that setup's models step
  would not take as verified: gone together with its marker, never marked, or marked for another pin.
* ``planned_files`` is the list of model files the wizard's models step plans for an install
  (``installer.stage2.downloader.plan_models`` without its optional files, plus the story model
  ``verify_downloads.story_item`` adds), each with the pin the step compares its marker with. It repeats that
  plan in the standard library (the setup window runs on the bare Python); ``tests/test_model_files.py``
  keeps the two equal.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from ns_common.atomic import read_text_retry, write_text_atomic

RECORD = "model-check.json"
BAD = "bad hash"          # verify_downloads.BAD: the one verdict that is evidence of damage
SET_ASIDE = ".bad"        # a damaged model file renamed by a Repair until its replacement verifies
#: ``downloader.WHISPER_REQUIRED``: whisper's files the step fetches even when the manifest pins no hash
WHISPER_REQUIRED = ("config.json", "tokenizer.json")
#: the manifest key of each story model tier (``plan.TIER_KEYS``); the GPU profile only, as ``plan_models``
TIER_KEYS = {"small": "llm", "balanced": "llm_balanced", "best": "llm_best"}


def record_path(home: Path) -> Path:
    """``<home>/logs/model-check.json``."""
    return Path(home) / "logs" / RECORD


def key_of(home: Path, path: Path) -> str:
    """The record's key for a file: its path under the install folder with forward slashes."""
    try:
        return Path(path).relative_to(Path(home)).as_posix()
    except ValueError:
        return Path(path).as_posix()


def read_record(home: Path) -> dict[str, dict[str, Any]]:
    """The record, or an empty one when it is missing, torn or not what it should be."""
    try:
        data = json.loads(read_text_retry(record_path(home)))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def write_entry(home: Path, key: str, entry: dict[str, Any]) -> None:
    """Put one file's entry into the record (the whole file is written again, atomically)."""
    data = read_record(home)
    data[key] = entry
    target = record_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(target, json.dumps(data, indent=2, sort_keys=True) + "\n")


def drop_entries(home: Path, keys: Iterable[str]) -> list[str]:
    """Take these files' entries out of the record; returns the keys that were in it. The file is written
    again (atomically) only when one was."""
    data = read_record(home)
    gone = [key for key in keys if key in data]
    if gone:
        for key in gone:
            del data[key]
        write_text_atomic(record_path(home), json.dumps(data, indent=2, sort_keys=True) + "\n")
    return gone


def still_same(entry: dict[str, Any], path: Path) -> bool:
    """True when ``path`` has the size and date the entry was written with."""
    try:
        st = Path(path).stat()
    except OSError:
        return False
    return entry.get("bytes") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns


def recorded_damage(home: Path) -> dict[str, dict[str, Any]]:
    """The entries that say ``bad hash`` about a file that is still the one that was checked."""
    return {
        key: entry for key, entry in read_record(home).items()
        if entry.get("verdict") == BAD and still_same(entry, Path(home) / key)
    }


def marker_moved(marker: Path) -> str | None:
    """Why the file of a setup ``.ok`` marker is no longer the file setup verified (None: it still is)."""
    target = marker.with_name(marker.name[: -len(".ok")])
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                     # not a marker setup can use: the step's own check decides
    if not isinstance(data, dict):
        return None
    try:
        st = target.stat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return None
    if data.get("size") != st.st_size:
        return "size changed"
    if data.get("mtime_ns") != st.st_mtime_ns:
        return "changed since it was downloaded"
    return None


def _name(entry: Any) -> str:
    return PurePosixPath(str(entry["file"])).name


def _story_model(home: Path) -> str | None:
    """The file name of the story model ``runtime.json`` uses (two models), or None."""
    try:
        data = json.loads(read_text_retry(Path(home) / "config" / "runtime.json", "utf-8-sig"))
        story = data["llama"]["story_model_path"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return PurePosixPath(str(story).replace("\\", "/")).name if isinstance(story, str) and story else None


def planned_files(
    home: Path, manifest: dict[str, Any], *, profile: str, tier: str, custom: dict[str, Any] | None = None,
    voicedesign: bool = False, voice: bool = True, dev: bool = False,
) -> list[tuple[Path, str | None]]:
    """``(file, pin)`` for every model file setup's models step plans for this install (see the module).

    The same choices as ``plan_models``: the tier's story model on the GPU profile (``custom`` replaces it
    and has no pin), the embed model, the voice models unless ``voice`` is False (the ``fake_voice`` engine
    test) with the voice design talker when chosen, whisper's pinned files and the ones it always needs, the
    face finder at ``models/cv/yunet.onnx``. ``dev``: every model but the face finder is a stub without a pin,
    and no story model of ``runtime.json`` is added. ``pin`` is None where the step compares none.
    """
    home = Path(home)
    pm = manifest["profiles"][profile]
    llm_dir, models = home / "models" / "llm", home / "models"
    out: list[tuple[Path, str | None]] = []
    if custom:
        out.append((llm_dir / _name(custom), None))
    else:
        entry = pm["llm"]
        if profile == "gpu" and tier in ("balanced", "best") and pm.get(TIER_KEYS[tier]):
            entry = pm[TIER_KEYS[tier]]
        out.append((llm_dir / _name(entry), entry.get("sha256")))
    out.append((models / "embed" / _name(pm["embed"]), pm["embed"].get("sha256")))
    if voice:
        out += [(models / "tts" / _name(f), f.get("sha256")) for f in pm.get("tts") or []]
        if voicedesign:
            out += [(models / "tts" / _name(f), f.get("sha256")) for f in pm.get("tts_optional") or []]
    wdir = models / "whisper" / str(pm["whisper_model"])
    pins = dict((pm.get("whisper_repo") or {}).get("files_sha256") or {})
    out += [(wdir / name, pin) for name, pin in pins.items()]
    out += [(wdir / name, None) for name in WHISPER_REQUIRED if name not in pins]
    yunet = (manifest.get("bins") or {}).get("yunet")
    if yunet:
        out.append((models / "cv" / "yunet.onnx", yunet.get("sha256")))
    if dev:
        return [(path, pin if path.name == "yunet.onnx" and path.parent.name == "cv" else None)
                for path, pin in out]
    story = pm.get("story_llm")
    if story and _story_model(home) == _name(story):      # `ns models get story_llm`: s03 checks it too
        out.append((llm_dir / _name(story), story.get("sha256")))
    return out


def not_verified(path: Path, pin: str | None) -> str | None:
    """Why setup's models step would not take ``path`` as verified (``downloader.is_verified``), or None."""
    try:
        st = Path(path).stat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return None                     # it cannot be looked at (no access): not damage; the step decides
    try:
        data = json.loads(Path(path).with_name(Path(path).name + ".ok").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "no record that setup checked it"
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return "its check record cannot be read"
    if data.get("size") != st.st_size:
        return "size changed"
    if data.get("mtime_ns") != st.st_mtime_ns:
        return "changed since it was downloaded"
    if pin is not None and str(data.get("sha256") or "").lower() != pin.lower():
        return "this version pins another file"
    return None


def repair_owed(home: Path, planned: Iterable[tuple[Path, str | None]] = ()) -> list[str]:
    """One plain line per model file a setup run over this install has to check or fetch again.

    ``planned`` (``planned_files``): a planned file setup's models step would not take as verified is owed
    too, whether or not a marker is left to say so (a file deleted together with its marker)."""
    root = Path(home) / "models"
    lines = [f"{key}: its SHA256 did not match at the check of {entry.get('checked_at') or '?'}"
             for key, entry in sorted(recorded_damage(home).items())]
    if root.is_dir():
        for folder, _dirs, files in os.walk(root):
            for name in sorted(files):
                path = Path(folder) / name
                if name.endswith(".ok"):
                    why = marker_moved(path)
                    if why is not None:
                        lines.append(f"{key_of(home, path)[: -len('.ok')]}: {why}")
                elif name.endswith(SET_ASIDE):
                    lines.append(f"{key_of(home, path)}: a damaged copy set aside by an unfinished Repair")
    named = {line.partition(": ")[0] for line in lines}
    for path, pin in planned:
        key = key_of(home, path)
        why = None if key in named else not_verified(path, pin)
        if why is not None:
            named.add(key)
            lines.append(f"{key}: {why}")
    return lines
