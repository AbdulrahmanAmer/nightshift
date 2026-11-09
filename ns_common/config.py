from __future__ import annotations

import json
from pathlib import Path

import yaml

from ns_common import first_start
from ns_common.atomic import BACKUP_SUFFIX, quarantine, read_text_retry, write_text_atomic
from ns_common.config_compat import disk_dict
from ns_common.errors import NsError
from ns_common.models import Channel, EmbedCfg, InstallState, Manifest, Runtime
from ns_common.paths import Paths, repo_root


def _write_text(path: Path, text: str, *, keep_backup: bool = False) -> Path:
    """Every settings file this module writes goes through here: the first start after an update an older
    updater made takes the settings copy before anything changes (`first_start.guard`, F-10c)."""
    first_start.guard(path)
    return write_text_atomic(path, text, keep_backup=keep_backup)


def _write_json(path: Path, data: dict, *, keep_backup: bool = False) -> Path:
    return _write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", keep_backup=keep_backup)


def embed_server_args(cfg: EmbedCfg) -> list[str]:
    """Context and batch flags for `llama-server --embeddings` so inputs up to `batch_size` tokens fit.

    Without `-b`/`-ub` the server rejects any single input over 512 tokens (HTTP 500 "input is too large
    to process"). The supervisor appends these where it builds the embed service command.
    """
    return ["-c", str(cfg.ctx_size), "-b", str(cfg.batch_size), "-ub", str(cfg.batch_size)]


def load_runtime(paths: Paths) -> Runtime:
    if not paths.runtime_file.exists():
        raise NsError("NS-E203", f"{paths.runtime_file} missing; run ns tune or the installer")
    return Runtime.model_validate_json(read_text_retry(paths.runtime_file))


def save_runtime(paths: Paths, rt: Runtime) -> Path:
    """A field added after 0.1.14 is written only when it is in use (`config_compat.disk_dict`)."""
    return _write_json(paths.runtime_file, disk_dict(rt))


def load_channel(paths: Paths, slug: str) -> Channel:
    f = paths.channel_file(slug)
    if not f.exists():
        raise FileNotFoundError(f)
    return Channel.model_validate(yaml.safe_load(read_text_retry(f)))


def save_channel(paths: Paths, ch: Channel) -> Path:
    f = paths.channel_file(ch.slug)
    f.parent.mkdir(parents=True, exist_ok=True)
    _write_text(f, yaml.safe_dump(disk_dict(ch), sort_keys=False, allow_unicode=True))
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
        return InstallState.model_validate_json(read_text_retry(f, "utf-8-sig"))
    except ValueError:
        pass
    first_start.guard(f)  # the torn file is moved aside: a change like any other (F-10c)
    quarantine(f)
    try:
        return InstallState.model_validate_json(
            f.with_name(f.name + BACKUP_SUFFIX).read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        return InstallState()


def save_install_state(paths: Paths, st: InstallState) -> Path:
    """Atomic write; the previous file becomes the `.bak` the reader falls back to."""
    return _write_json(paths.install_state_file, disk_dict(st), keep_backup=True)


def load_manifest(path: Path | None = None) -> Manifest:
    f = path or (repo_root() / "manifest.json")
    return Manifest.model_validate_json(f.read_text(encoding="utf-8"))


def notifications_enabled(paths: Paths) -> bool:
    """Settings > Notifications: `notifications` in `config/dashboard.json` (the file the dashboard owns).

    On when the file or the key is absent or unreadable, which is the dashboard's own default. Read by the
    nightly run and by the supervisor's weekly update check before they show a toast.
    """
    try:
        raw = json.loads(read_text_retry(paths.config / "dashboard.json"))
    except (OSError, ValueError):
        return True
    if isinstance(raw, dict) and isinstance(raw.get("notifications"), bool):
        return raw["notifications"]
    return True
