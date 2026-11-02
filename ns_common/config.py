from __future__ import annotations

import json
from pathlib import Path

import yaml

from ns_common.atomic import BACKUP_SUFFIX, quarantine, write_text_atomic
from ns_common.errors import NsError
from ns_common.models import Channel, InstallState, Manifest, Runtime
from ns_common.paths import Paths, repo_root


def _write_json(path: Path, data: dict, *, keep_backup: bool = False) -> Path:
    return write_text_atomic(
        path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", keep_backup=keep_backup
    )


def load_runtime(paths: Paths) -> Runtime:
    if not paths.runtime_file.exists():
        raise NsError("NS-E203", f"{paths.runtime_file} missing; run ns tune or the installer")
    return Runtime.model_validate_json(paths.runtime_file.read_text(encoding="utf-8"))


def save_runtime(paths: Paths, rt: Runtime) -> Path:
    return _write_json(paths.runtime_file, rt.model_dump(mode="json"))


def load_channel(paths: Paths, slug: str) -> Channel:
    f = paths.channel_file(slug)
    if not f.exists():
        raise FileNotFoundError(f)
    return Channel.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")))


def save_channel(paths: Paths, ch: Channel) -> Path:
    f = paths.channel_file(ch.slug)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        yaml.safe_dump(ch.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return f


def list_channels(paths: Paths) -> list[str]:
    if not paths.channels_dir.exists():
        return []
    return sorted(p.stem for p in paths.channels_dir.glob("*.yaml"))


def load_install_state(paths: Paths) -> InstallState:
    """`install_state.json`, tolerant of a torn file.

    A crash or power loss mid-write used to leave truncated JSON, and every later start died on the parse
    error. Now an unreadable file falls back to the last good copy (`.bak`, written before each save), and
    failing that to an empty state; the broken file is kept as `.bad` for the diagnostics zip.
    """
    f = paths.install_state_file
    if not f.exists():
        return InstallState()
    try:
        return InstallState.model_validate_json(f.read_text(encoding="utf-8-sig"))
    except ValueError:
        pass
    quarantine(f)
    try:
        return InstallState.model_validate_json(
            f.with_name(f.name + BACKUP_SUFFIX).read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        return InstallState()


def save_install_state(paths: Paths, st: InstallState) -> Path:
    """Atomic write; the previous file becomes the `.bak` the reader falls back to."""
    return _write_json(paths.install_state_file, st.model_dump(mode="json"), keep_backup=True)


def load_manifest(path: Path | None = None) -> Manifest:
    f = path or (repo_root() / "manifest.json")
    return Manifest.model_validate_json(f.read_text(encoding="utf-8"))


def notifications_enabled(paths: Paths) -> bool:
    """Settings > Notifications: `notifications` in `config/dashboard.json` (the file the dashboard owns).

    On when the file or the key is absent or unreadable, which is the dashboard's own default. Read by the
    nightly run and by the supervisor's weekly update check before they show a toast.
    """
    try:
        raw = json.loads((paths.config / "dashboard.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if isinstance(raw, dict) and isinstance(raw.get("notifications"), bool):
        return raw["notifications"]
    return True
